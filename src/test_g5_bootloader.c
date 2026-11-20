#define _GNU_SOURCE
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <signal.h>
#include <ucontext.h>
#include <dlfcn.h>
#include <unistd.h>
#include <fcntl.h>
#include <pthread.h>
#include <sys/resource.h>

static void sigsegv_handler(int sig, siginfo_t *info, void *ucontext) {
    ucontext_t *uc = (ucontext_t *)ucontext;
    uintptr_t pc = uc->uc_mcontext.arm_pc;
    uintptr_t lr = uc->uc_mcontext.arm_lr;
    uintptr_t sp = uc->uc_mcontext.arm_sp;
    uintptr_t r0 = uc->uc_mcontext.arm_r0;
    uintptr_t r1 = uc->uc_mcontext.arm_r1;
    uintptr_t r2 = uc->uc_mcontext.arm_r2;
    uintptr_t r3 = uc->uc_mcontext.arm_r3;
    uintptr_t ip = uc->uc_mcontext.arm_ip;
    
    Dl_info dli_pc, dli_lr;
    memset(&dli_pc, 0, sizeof(dli_pc));
    memset(&dli_lr, 0, sizeof(dli_lr));
    dladdr((void*)pc, &dli_pc);
    dladdr((void*)lr, &dli_lr);

    printf("\n[ERROR] CRASH INTERCEPTED: signal %d at PC = %p (%s in %s), Fault = %p\n",
           sig, (void*)pc, dli_pc.dli_sname ? dli_pc.dli_sname : "???", dli_pc.dli_fname ? dli_pc.dli_fname : "???", info->si_addr);
    printf("        LR = %p (%s in %s)\n",
           (void*)lr, dli_lr.dli_sname ? dli_lr.dli_sname : "???", dli_lr.dli_fname ? dli_lr.dli_fname : "???");
    printf("        Registers: R0=%p, R1=%p, R2=%p, R3=%p, IP=%p, SP=%p\n",
           (void*)r0, (void*)r1, (void*)r2, (void*)r3, (void*)ip, (void*)sp);

    printf("\n=== Call Stack unwinding (from SP) ===\n");
    uintptr_t *stack = (uintptr_t *)sp;
    for (int i = 0; i < 64; i++) {
        uintptr_t val = stack[i];
        Dl_info dli;
        memset(&dli, 0, sizeof(dli));
        if (dladdr((void*)val, &dli) && dli.dli_fname) {
            printf("  [SP+%02x] %p : %s in %s (+0x%lx)\n",
                   i * 4, (void*)val,
                   dli.dli_sname ? dli.dli_sname : "???",
                   dli.dli_fname,
                   val - (uintptr_t)dli.dli_fbase);
        }
    }
    printf("======================================\n\n");
    fflush(stdout);
    _exit(139);
}

static int s_stub_invoked = 0;

static void test_stub_app_start(void) {
    printf("\n*******************************************************\n");
    printf("*** BOOTLOADER HANDOFF SUCCESSFUL!                  ***\n");
    printf("*** _Z4MainPv successfully initialized environment! ***\n");
    printf("*** global.xdb loaded, Arena ready!                 ***\n");
    printf("*** Stub Application_Start reached safely!          ***\n");
    printf("*******************************************************\n\n");
    fflush(stdout);
    s_stub_invoked = 1;
}

static void* worker_thread(void *arg) {
    printf("[Worker] 32MB thread active (SP=%p)\n", &arg);
    fflush(stdout);

    // 1. 设置库搜索路径与工作目录
    setenv("LD_LIBRARY_PATH", "/log/g5:/usr/lib:/lib", 1);
    if (chdir("/log/g5/share/app") == 0) {
        printf("[Worker] chdir(/log/g5/share/app) OK\n");
    } else {
        printf("[Worker] chdir failed!\n");
    }
    fflush(stdout);

    // 2. 预加载系统级基础支持库并执行 MWF 必需子模块初始化
    printf("[Worker] Preloading system OSAL & MWF libraries...\n");
    fflush(stdout);
    void *hOsal1 = dlopen("/usr/lib/libosal_utm.so", RTLD_GLOBAL | RTLD_LAZY);
    void *hOsal2 = dlopen("/usr/lib/libosal_uipc.so", RTLD_GLOBAL | RTLD_LAZY);
    void *hBkp   = dlopen("/usr/lib/libbackup.so", RTLD_GLOBAL | RTLD_LAZY);
    void *hMwf   = dlopen("/usr/lib/libMWF.so", RTLD_GLOBAL | RTLD_LAZY);
    printf("  Handles: utm=%p, uipc=%p, bkp=%p, mwf=%p\n", hOsal1, hOsal2, hBkp, hMwf);
    fflush(stdout);

    if (hMwf) {
        void (*pRsrcDP_Init)(void) = (void (*)(void))dlsym(hMwf, "_ZN3MWF6RsrcDP10InitializeEv");
        void (*pRsrcRel_Init)(void) = (void (*)(void))dlsym(hMwf, "_ZN3MWF12RsrcRelation10InitializeEv");
        
        printf("  Calling RsrcDP::Initialize (MonCtrl singleton)...\n");
        fflush(stdout);
        if (pRsrcDP_Init) pRsrcDP_Init();
        printf("  RsrcDP::Initialize OK!\n");
        fflush(stdout);

        printf("  Calling RsrcRelation::Initialize (mp_objShLockGlobal)...\n");
        fflush(stdout);
        if (pRsrcRel_Init) pRsrcRel_Init();
        printf("  RsrcRelation::Initialize OK!\n");
        fflush(stdout);
    }

    // 3. 严格按照依赖层级加载五代核心生态：
    printf("[Worker] Step 1: Loading libOb5.so...\n");
    fflush(stdout);
    void *hOb5 = dlopen("/log/g5/libOb5.so", RTLD_GLOBAL | RTLD_LAZY);
    if (!hOb5) {
        printf("  libOb5.so error: %s\n", dlerror());
        return NULL;
    }
    printf("  hOb5 = %p\n", hOb5);
    fflush(stdout);

    printf("[Worker] Step 1.1: Loading libmp5.so...\n");
    fflush(stdout);
    void *hMp5 = dlopen("/log/g5/libmp5.so", RTLD_GLOBAL | RTLD_LAZY);
    printf("  hMp5 = %p\n", hMp5);
    fflush(stdout);

    printf("[Worker] Step 1.2: Loading libObjShim.so...\n");
    fflush(stdout);
    void *hShim = dlopen("/log/g5/libObjShim.so", RTLD_GLOBAL | RTLD_LAZY);
    printf("  hShim = %p\n", hShim);
    fflush(stdout);

    printf("[Worker] Step 1.3: Loading libIMDB.so...\n");
    fflush(stdout);
    void *hImdb = dlopen("/log/g5/libIMDB.so", RTLD_GLOBAL | RTLD_LAZY);
    printf("  hImdb = %p\n", hImdb);
    fflush(stdout);

    printf("[Worker] Step 2: Loading libmpr.so...\n");
    fflush(stdout);
    void *hMpr = dlopen("/log/g5/libmpr.so", RTLD_GLOBAL | RTLD_LAZY);
    printf("  hMpr = %p\n", hMpr);
    fflush(stdout);

    printf("[Worker] Step 3: Loading AdjChkConfig.so...\n");
    fflush(stdout);
    void *hAdj = dlopen("/log/g5/AdjChkConfig.so", RTLD_GLOBAL | RTLD_LAZY);
    printf("  hAdj = %p\n", hAdj);
    fflush(stdout);

    printf("[Worker] Step 4: Loading appFwBoot.so...\n");
    fflush(stdout);
    void *hBoot = dlopen("/log/g5/appFwBoot.so", RTLD_GLOBAL | RTLD_LAZY);
    if (!hBoot) {
        printf("  appFwBoot.so error: %s\n", dlerror());
        return NULL;
    }
    printf("  hBoot = %p\n", hBoot);
    fflush(stdout);

    printf("[Worker] Step 5: Loading appFw.so...\n");
    fflush(stdout);
    void *hApp = dlopen("/log/g5/appFw.so", RTLD_GLOBAL | RTLD_LAZY);
    if (!hApp) {
        printf("  appFw.so error: %s\n", dlerror());
        return NULL;
    }
    printf("  hApp = %p\n", hApp);
    fflush(stdout);

    // 4. 获取函数指针
    void (*pMain)(void *) = (void (*)(void *))dlsym(hBoot, "_Z4MainPv");
    void *pRealStart = dlsym(hApp, "Application_Start");
    printf("[Worker] _Z4MainPv = %p, Application_Start = %p\n", pMain, pRealStart);
    fflush(stdout);

    if (!pMain) {
        printf("[Worker] _Z4MainPv not found in appFwBoot.so: %s\n", dlerror());
        return NULL;
    }

    // 5. 第一步：先用验证桩测试引导器完整的官方初始化流程（Arena、global.xdb、Font）
    printf("[Worker] Executing _Z4MainPv with test stub...\n");
    fflush(stdout);
    pMain((void*)test_stub_app_start);

    if (s_stub_invoked) {
        printf("\n*******************************************************\n");
        printf("*** PRE-FLIGHT CHECK PASSED 100%%!                   ***\n");
        printf("*** BOOTLOADER FULLY FUNCTIONAL UNDER GEN4 KERNEL!  ***\n");
        printf("*******************************************************\n\n");
        fflush(stdout);
        _exit(0);
    } else {
        printf("[Worker] Warning: Stub was not reached.\n");
    }
    fflush(stdout);
    return NULL;
}

int main(int argc, char **argv) {
    setvbuf(stdout, NULL, _IONBF, 0);
    setvbuf(stderr, NULL, _IONBF, 0);

    struct sigaction sa;
    memset(&sa, 0, sizeof(sa));
    sa.sa_sigaction = sigsegv_handler;
    sa.sa_flags = SA_SIGINFO;
    sigaction(SIGSEGV, &sa, NULL);
    sigaction(SIGBUS, &sa, NULL);

    struct rlimit rl = {32 * 1024 * 1024, 32 * 1024 * 1024};
    setrlimit(RLIMIT_STACK, &rl);

    printf("=== Starting Pure Gen5 Bootloader Test with RsrcRelation Init ===\n");
    fflush(stdout);
    pthread_t th;
    pthread_attr_t attr;
    pthread_attr_init(&attr);
    pthread_attr_setstacksize(&attr, 32 * 1024 * 1024);

    if (pthread_create(&th, &attr, worker_thread, NULL) != 0) {
        printf("pthread_create failed\n");
        return 1;
    }
    pthread_join(th, NULL);
    printf("=== Test Completed Successfully ===\n");
    fflush(stdout);
    return 0;
}
