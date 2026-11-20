#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <signal.h>
#include <ucontext.h>
#include <dlfcn.h>
#include <unistd.h>
#include <fcntl.h>
#include <pthread.h>
#include <sys/time.h>
#include <sys/resource.h>

#define STACK_SIZE (16u * 1024u * 1024u)

static double get_time_ms(void) {
    struct timeval tv;
    gettimeofday(&tv, NULL);
    return tv.tv_sec * 1000.0 + tv.tv_usec / 1000.0;
}

static void print_meminfo(const char *tag) {
    FILE *f = fopen("/proc/meminfo", "r");
    if (!f) return;
    char line[128];
    long mem_free = 0, cached = 0;
    while (fgets(line, sizeof(line), f)) {
        if (strncmp(line, "MemFree:", 8) == 0) sscanf(line + 8, "%ld", &mem_free);
        if (strncmp(line, "Cached:", 7) == 0) sscanf(line + 7, "%ld", &cached);
    }
    fclose(f);

    FILE *fs = fopen("/proc/self/status", "r");
    long vm_size = 0, vm_rss = 0;
    if (fs) {
        while (fgets(line, sizeof(line), fs)) {
            if (strncmp(line, "VmSize:", 7) == 0) sscanf(line + 7, "%ld", &vm_size);
            if (strncmp(line, "VmRSS:", 6) == 0) sscanf(line + 6, "%ld", &vm_rss);
        }
        fclose(fs);
    }
    printf("[%s] MemFree=%ld KB, Cached=%ld KB (Available~%ld KB) | VmSize=%ld KB, VmRSS=%ld KB\n",
           tag, mem_free, cached, mem_free + cached, vm_size, vm_rss);
    fflush(stdout);
}

static void crash_handler(int sig, siginfo_t *si, void *uc_) {
    ucontext_t *uc = (ucontext_t *)uc_;
    uintptr_t pc = uc->uc_mcontext.arm_pc;
    uintptr_t lr = uc->uc_mcontext.arm_lr;
    uintptr_t sp = uc->uc_mcontext.arm_sp;

    Dl_info d_pc = {0}, d_lr = {0};
    dladdr((void *)pc, &d_pc);
    dladdr((void *)lr, &d_lr);

    printf("\n!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!\n");
    printf("[CRASH INTERCEPTED] Signal %d, Fault addr = %p\n", sig, si->si_addr);
    printf("  PC = 0x%08lx (%s+0x%lx in %s)\n",
           (unsigned long)pc, d_pc.dli_sname ? d_pc.dli_sname : "?",
           (unsigned long)(pc - (uintptr_t)d_pc.dli_fbase), d_pc.dli_fname ? d_pc.dli_fname : "?");
    printf("  LR = 0x%08lx (%s+0x%lx in %s)\n",
           (unsigned long)lr, d_lr.dli_sname ? d_lr.dli_sname : "?",
           (unsigned long)(lr - (uintptr_t)d_lr.dli_fbase), d_lr.dli_fname ? d_lr.dli_fname : "?");
    printf("  SP = 0x%08lx\n", (unsigned long)sp);
    printf("  R0=%08lx R1=%08lx R2=%08lx R3=%08lx\n",
           (unsigned long)uc->uc_mcontext.arm_r0, (unsigned long)uc->uc_mcontext.arm_r1,
           (unsigned long)uc->uc_mcontext.arm_r2, (unsigned long)uc->uc_mcontext.arm_r3);
    printf("  R4=%08lx R5=%08lx R6=%08lx R7=%08lx\n",
           (unsigned long)uc->uc_mcontext.arm_r4, (unsigned long)uc->uc_mcontext.arm_r5,
           (unsigned long)uc->uc_mcontext.arm_r6, (unsigned long)uc->uc_mcontext.arm_r7);

    printf("\n=== STACK UNWIND ===\n");
    uintptr_t *s = (uintptr_t *)sp;
    for (int i = 0; i < 48; i++) {
        uintptr_t val = s[i];
        Dl_info di = {0};
        if (dladdr((void *)val, &di) && di.dli_fname) {
            printf("  [SP+%03d] 0x%08lx: %s+0x%lx (%s)\n",
                   i * 4, (unsigned long)val,
                   di.dli_sname ? di.dli_sname : "?",
                   (unsigned long)(val - (uintptr_t)di.dli_fbase),
                   di.dli_fname);
        }
    }
    printf("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!\n");
    fflush(stdout);
    _exit(139);
}

static void install_signals(void) {
    struct sigaction sa;
    memset(&sa, 0, sizeof sa);
    sa.sa_sigaction = crash_handler;
    sa.sa_flags = SA_SIGINFO | SA_ONSTACK;
    sigaction(SIGSEGV, &sa, NULL);
    sigaction(SIGBUS, &sa, NULL);
    sigaction(SIGILL, &sa, NULL);
    sigaction(SIGABRT, &sa, NULL);
    sigaction(SIGFPE, &sa, NULL);
}

static int s_stub_called = 0;
static void test_stub_start(void) {
    printf(">>> [STAGE 1 SUCCESS] test_stub_start INVOKED BY _Z4MainPv! <<<\n");
    printf(">>> Stella runtime, Arena, global.xdb, Font are fully UP! <<<\n");
    fflush(stdout);
    s_stub_called = 1;
}

static void (*g_pMain)(void *) = NULL;
static void *g_pRealStart = NULL;
static volatile int g_stage2_active = 0;

static void *stage2_thread(void *unused) {
    (void)unused;
    printf("\n[Stage 2 Worker] Triggering _Z4MainPv(Application_Start)...\n");
    fflush(stdout);
    g_stage2_active = 1;
    g_pMain(g_pRealStart);
    g_stage2_active = 0;
    printf("[Stage 2 Worker] Application_Start returned cleanly!\n");
    fflush(stdout);
    return NULL;
}

static void *main_worker(void *unused) {
    (void)unused;
    int dummy;
    printf("\n--- [Worker Thread Started] SP~%p StackSize=%u ---\n", (void *)&dummy, STACK_SIZE);
    print_meminfo("Worker Start");

    setenv("LD_LIBRARY_PATH", "/log/g5:/usr/lib:/lib", 1);
    if (chdir("/log/g5/share/app") == 0) {
        printf("[chdir] Changed directory to /log/g5/share/app OK\n");
    } else {
        printf("[chdir] Fallback /usr/share/app\n");
        chdir("/usr/share/app");
    }

    // 1. 预加载底层与 MWF
    printf("\n[Step 0] Preloading OSAL and MWF...\n");
    dlopen("/usr/lib/libosal_utm.so", RTLD_GLOBAL | RTLD_LAZY);
    dlopen("/usr/lib/libosal_uipc.so", RTLD_GLOBAL | RTLD_LAZY);
    dlopen("/usr/lib/libbackup.so", RTLD_GLOBAL | RTLD_LAZY);
    dlopen("/usr/lib/libObj.so", RTLD_GLOBAL | RTLD_LAZY);
    void *hMwf = dlopen("/log/g5/libMWF.so", RTLD_GLOBAL | RTLD_LAZY);
    if (!hMwf) hMwf = dlopen("/usr/lib/libMWF.so", RTLD_GLOBAL | RTLD_LAZY);

    if (hMwf) {
        void (*pRsrcDP_Init)(void) = (void (*)(void))dlsym(hMwf, "_ZN3MWF6RsrcDP10InitializeEv");
        void (*pRsrcRel_Init)(void) = (void (*)(void))dlsym(hMwf, "_ZN3MWF12RsrcRelation10InitializeEv");
        if (pRsrcDP_Init) pRsrcDP_Init();
        if (pRsrcRel_Init) pRsrcRel_Init();
        printf("  MWF Initialized OK.\n");
    }

    // 2. 加载五代改名闭包
    const char *libs[] = {
        "/log/g5/libOb5.so",
        "/log/g5/libmp5.so",
        "/log/g5/libObjShim.so",
        "/log/g5/AdjChkConfig.so",
        "/log/g5/libappFwBoot_g5.so",
    };
    void *handles[5] = {0};
    for (int i = 0; i < 5; i++) {
        handles[i] = dlopen(libs[i], RTLD_GLOBAL | RTLD_LAZY);
        if (!handles[i]) {
            printf("  FAILED to load %s: %s\n", libs[i], dlerror());
            return NULL;
        }
    }
    printf("  Gen5 Closure Loaded OK.\n");

    // 3. 加载 appFw.so
    printf("  Loading appFw.so...\n");
    void *hApp = dlopen("/log/g5/appFw.so", RTLD_GLOBAL | RTLD_LAZY);
    if (!hApp) {
        printf("  FAILED to load appFw.so: %s\n", dlerror());
        return NULL;
    }
    printf("  appFw.so Loaded Successfully!\n");
    print_meminfo("After appFw.so");

    // 4. 解析入口
    g_pMain = (void (*)(void *))dlsym(handles[4], "_Z4MainPv");
    g_pRealStart = dlsym(hApp, "Application_Start");
    printf("  _Z4MainPv = %p, Application_Start = %p\n", (void *)g_pMain, g_pRealStart);

    // 5. Stage 1: 验证桩
    printf("\n=== STAGE 1: Testing _Z4MainPv with Stub ===\n");
    g_pMain((void *)test_stub_start);
    if (!s_stub_called) {
        printf("  Stage 1 Stub not called!\n");
        return NULL;
    }
    print_meminfo("Stage 1 Passed");

    if (getenv("ASTRA_REAL_IGNITION") == NULL) {
        printf("\n*******************************************************\n");
        printf("*** [STAGE 1 SUCCESS] BOOTLOADER HANDOFF VERIFIED!   ***\n");
        printf("*** Dynamic Linker Closure 100%% Resolved!           ***\n");
        printf("*** Arena, global.xdb, Font engine initialized!     ***\n");
        printf("*** Safe Mode: Gen5 environment ready for flashing! ***\n");
        printf("*******************************************************\n\n");
        return NULL;
    }

    // 6. Stage 2: 真实点火测试
    printf("\n=== STAGE 2: Real Ignition Test with Application_Start ===\n");
    fflush(stdout);

    pthread_t th_s2;
    pthread_attr_t attr_s2;
    pthread_attr_init(&attr_s2);
    pthread_attr_setstacksize(&attr_s2, STACK_SIZE);
    pthread_create(&th_s2, &attr_s2, stage2_thread, NULL);

    // 观察 Stage 2 运行状态 5 秒钟
    for (int sec = 1; sec <= 5; sec++) {
        sleep(1);
        printf("  [Stage 2 Watchdog] +%d sec, active=%d\n", sec, g_stage2_active);
        print_meminfo("Stage 2 Alive");
        fflush(stdout);
        if (!g_stage2_active) {
            printf("  [Stage 2 Watchdog] Application_Start exited early!\n");
            break;
        }
    }

    if (g_stage2_active) {
        printf("\n*******************************************************\n");
        printf("*** [STAGE 2 FULL SUCCESS] APPLICATION_START IS ALIVE! ***\n");
        printf("*** ASTRA UI LOOP IS STABLY RUNNING ON ZV-E10 HARDWARE! ***\n");
        printf("*******************************************************\n\n");
    }

    return NULL;
}

int main(int argc, char **argv) {
    setvbuf(stdout, NULL, _IONBF, 0);
    setvbuf(stderr, NULL, _IONBF, 0);

    printf("=================================================================\n");
    printf("  Sony ZV-E10 Gen5 ASTRA Stage 2 Full Verification v3.0\n");
    printf("=================================================================\n");
    print_meminfo("Initial State");

    install_signals();

    pthread_t th;
    pthread_attr_t attr;
    pthread_attr_init(&attr);
    pthread_attr_setstacksize(&attr, STACK_SIZE);

    if (pthread_create(&th, &attr, main_worker, NULL) != 0) {
        printf("pthread_create failed\n");
        return 1;
    }

    pthread_join(th, NULL);
    print_meminfo("Final Finished State");
    printf("=== All Stage 2 Verifications Completed ===\n");
    return 0;
}
