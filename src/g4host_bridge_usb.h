#ifndef G4HOST_BRIDGE_USB_H
#define G4HOST_BRIDGE_USB_H

#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif

#include <stdint.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <stdarg.h>
#include <string.h>
#include <dlfcn.h>
#include <unistd.h>
#include <sys/mman.h>
#include <errno.h>

/*
 * ============================================================================
 * ZV-E10 Gen 4 宿主移植 Gen 5 ASTRA 跨代兼容桥接层 (Phase 2)
 * 模块 4：USB 模式（Mass Storage / ExtCmd）与供电状态桥接层 (g4host_bridge_usb.h)
 *
 * 核心修复职责（基于 Gen4/Gen5 ARM/Thumb-2 指令级逆向与真机日志实锤）：
 * 1. 修正五代 USB 插拔事件码与 InfraInput::GetPhyKeyStatus (623/626) 状态校准：
 *    - USB_JACK_ON=0x02100001, USB_JACK_OFF=0x02100000
 *    - CHARGER_ON=0x020f0056, CHARGER_OFF=0x020f0055
 * 2. 修复 GetMediaIDByAttribute (0x02b1afc8) / Cmd_11008_02b1b19c (0x02b1b19c) / UpmSet (0x02b1e450)：
 *    - 真机宿主存在时透传四代 libObj::InfraMediaConfig (0x00cf016d, cmd=0x1004) 返回真实 Slot1 media_id；
 *    - QEMU 模式下安全清零完整 0x48B 响应区，避免同步队列阻塞。
 * 3. 解除 BusPower (mask=0x4, 054C:096F) 对 libUsbControl 状态机的抢占并清洗 MscConfigureFunc media_id_list：
 *    - 发送 CONFIGURE_FUNC(1) 前调用 BusPower::StopBusPowerFuctions() 优雅释放 mask=0x4；
 *    - 过滤 media_id_list 中的 0 / 0xffffffff 无效项，避免 DriveListMaking() 拒绝启动 MSC；
 *    - 消除 chkStartGadgetOK 无条件覆写掩码引起的模式切换误触发。
 * 4. 补全 libUsbControl 与 MSC/ExtCmd 关键节点 4B 对齐安全跳板与诊断日志。
 * ============================================================================
 */

#ifdef __cplusplus
extern "C" {
#endif

/* ----------------------------------------------------------------------------
 * 1. 外部依赖与防御性声明 (External / Defensive Declarations)
 * ---------------------------------------------------------------------------- */

#ifdef G4USB_STANDALONE_TEST
static inline void logf_(const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    vfprintf(stderr, fmt, ap);
    fprintf(stderr, "\n");
    va_end(ap);
}
#else
/* 宿主环境中定义为 static void logf_(const char *fmt, ...); 此处进行前向防御性声明 */
static void logf_(const char *fmt, ...);
#endif

#define g4usb_log(fmt, ...) logf_("[G4USB] " fmt, ##__VA_ARGS__)

/* 全局基址与运行态缓存 */
static uintptr_t g_g4usb_app_base = 0;
static uintptr_t g_g4usb_usb_base = 0;
static uintptr_t g_g4usb_ob5_base = 0;
static volatile int g_g4usb_real_host_ready = 0;

static void * volatile g_g4usb_usb_ctrl_this = NULL;
static volatile int g_g4usb_jack_state[5] = { 0, 0, 0, 0, 0 }; /* 索引 1..4 对应 local jack_type */
static volatile int g_g4usb_jack_seen = 0;
static volatile uint32_t g_g4usb_last_expected_mask = 0u;
static volatile uint32_t g_g4usb_last_started_mask = 0u;
static volatile uint32_t g_g4usb_cached_slot1_media_id = 0x00001000u;
static volatile uint32_t g_g4usb_cached_simulate_media_id = 0u;
static volatile int g_g4usb_real_media_seen = 0;

/* seq 0x297 (MSC) / 0x294 (MTP) / 0x298 (MSC Stop) 全链路追踪与防卡死状态 */
static volatile int g_g4usb_seq297_pending = 0;
static volatile uint32_t g_g4usb_seq297_enq_cnt = 0u;
static volatile uint32_t g_g4usb_seq294_enq_cnt = 0u;
static volatile uint32_t g_g4usb_seq297_disp_cnt = 0u;
static volatile uint32_t g_g4usb_seq294_disp_cnt = 0u;
static volatile uint32_t g_g4usb_seq298_disp_cnt = 0u;
static volatile uint32_t g_g4usb_seq297_run_cnt = 0u;
static volatile uint32_t g_g4usb_seq294_run_cnt = 0u;
static volatile uint32_t g_g4usb_qdisp_stall_ticks = 0u;

/* ----------------------------------------------------------------------------
 * 2. 常量与协议命令字定义 (Constants & IPC Signatures)
 * ---------------------------------------------------------------------------- */

/* 五代 ASTRA 精确 USB 插拔与供电事件码 (appFw.so .data @ 0x037e358c & .data.rel.ro 实锤) */
#define G5_EVENT_INPUT_KEY_USB_JACK_ON          0x02100001u /* USB 插头物理插入 */
#define G5_EVENT_INPUT_KEY_USB_JACK_OFF         0x02100000u /* USB 插头物理拔出 */
#define G5_EVENT_INPUT_KEY_USB_JACK_CHARGER_ON  0x020f0056u /* 外部充电器接入 */
#define G5_EVENT_INPUT_KEY_USB_JACK_CHARGER_OFF 0x020f0055u /* 外部充电器断开 */
#define G5_MODEL_REPROCESS_EVENT_USB_JACK_ON    0x010001aau
#define G5_MODEL_REPROCESS_EVENT_USB_JACK_OFF   0x010001abu

/* 五代 InfraInput USB 逻辑键码与底层物理键码 (appFw.so 0x00533cc0 / 0x00541138 双向映射表实锤) */
#define G5_PHY_KEYCODE_USB_JACK                 0x0000026fu /* 623 (appFw 逻辑键码) */
#define G5_PHY_KEYCODE_USB_JACK_CHARGER         0x00000272u /* 626 (appFw 逻辑键码) */
#define G5_RAW_PHY_KEYCODE_USB_JACK             0x07000001u /* Dev 7 Local 1 底层物理键码 */
#define G5_RAW_PHY_KEYCODE_USB_JACK_CHARGER     0x07000004u /* Dev 7 Local 4 底层物理键码 */

/* 四代 USB 控制器命令与状态常量 (libUsbControl.so Category 0x6103) */
#define G4_USB_CATEGORY_ID                      0x6103u
#define G4_MSGID_START_GADGET_CMD               0x00083001u
#define G4_MSGID_STOP_GADGET_CMD                0x00083002u
#define G4_MSGID_CONFIGURE_FUNC_CMD             0x00083003u
#define G4_MSGID_START_GADGET_CMP               0x00093001u

/* ----------------------------------------------------------------------------
 * 3. 底层 Thumb 挂钩与跳板基础设施 (Self-contained Hook Utilities)
 * ---------------------------------------------------------------------------- */

/* Thumb 绝对跳转安装器（8 字节：ldr.w pc, [pc, #0] + 4 字节绝对目标） */
static inline void g4usb_patch_thumb_jump(void *target, void *replacement, const char *name)
{
    uintptr_t addr = (uintptr_t)target & ~1u;
    uintptr_t page = addr & ~4095u;
    if (mprotect((void *)page, 4096 * 2, PROT_READ | PROT_WRITE | PROT_EXEC) == 0) {
        unsigned char *p = (unsigned char *)addr;
        p[0] = 0xdf; p[1] = 0xf8; p[2] = 0x00; p[3] = 0xf0; /* ldr.w pc, [pc, #0] */
        uint32_t to = (uint32_t)(uintptr_t)replacement;
        memcpy(p + 4, &to, 4);
        __builtin___clear_cache((char *)addr, (char *)addr + 8);
        g4usb_log("HOOK: %s (%p -> %p) 安装成功", name, (void *)addr, replacement);
    } else {
        g4usb_log("HOOK: %s (%p) mprotect 失败 errno=%d", name, (void *)addr, errno);
    }
}

/* 备份并跳转前 8 字节（要求 4 字节对齐且前 8B 无 PC 相对寻址或跨界指令），返回可执行原函数跳板 (+1 Thumb) */
static inline void *g4usb_make_thumb_trampoline_8(uintptr_t va, void *hook_fn, const char *name)
{
    uintptr_t addr = va & ~1u;
    if ((addr & 3u) != 0u) {
        g4usb_log("HOOK: %s (%p) 未按 4 字节对齐，无法安全生成 8B 跳板", name, (void *)addr);
        return NULL;
    }
    uint8_t *pool = (uint8_t *)mmap(NULL, 4096, PROT_READ | PROT_WRITE | PROT_EXEC,
                                    MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (pool == MAP_FAILED) {
        g4usb_log("HOOK: %s mmap 失败 errno=%d", name, errno);
        return NULL;
    }
    memcpy(pool, (const void *)addr, 8);
    uint16_t *t16 = (uint16_t *)(pool + 8);
    t16[0] = 0xF8DFu; /* ldr.w pc, [pc, #0] */
    t16[1] = 0xF000u;
    *(uint32_t *)(pool + 12) = (uint32_t)(addr + 8u + 1u);
    __builtin___clear_cache((char *)pool, (char *)pool + 16);
    g4usb_patch_thumb_jump((void *)addr, hook_fn, name);
    g4usb_log("HOOK: %s (%p -> %p, tramp=%p) 跳板安装成功", name, (void *)addr, hook_fn, (void *)pool);
    return (void *)(pool + 1);
}

/* 动态获取模块加载基址 */
static inline uintptr_t g4usb_get_module_base(const char *name)
{
    FILE *fp = fopen("/proc/self/maps", "r");
    if (!fp) return 0;
    char line[512];
    uintptr_t base = 0;
    while (fgets(line, sizeof(line), fp)) {
        if (strstr(line, name)) {
            char *dash = strchr(line, '-');
            if (dash) {
                *dash = '\0';
                base = (uintptr_t)strtoul(line, NULL, 16);
            }
            break;
        }
    }
    fclose(fp);
    return base;
}

/* ----------------------------------------------------------------------------
 * 4. 五代内部事件发送通道 (Gen 5 Event Injection Pipeline)
 * ---------------------------------------------------------------------------- */

/* 向五代 ASTRA 事件总线注入内部事件（appFw.so @ 0x0054e9d9u） */
static inline int g4usb_send_internal_event(uint32_t event_id)
{
    if (!g_g4usb_app_base) {
        g4usb_log("事件发送失败: g_g4usb_app_base 为空 (event=0x%08x)", event_id);
        return -1;
    }
    const uint8_t *fn_addr = (const uint8_t *)(g_g4usb_app_base + 0x0054e9d8u);
    static const uint8_t expected[4] = { 0x2du, 0xe9u, 0xf0u, 0x41u }; /* push.w {r4-r8, lr} */
    if (memcmp(fn_addr, expected, sizeof(expected)) != 0) {
        g4usb_log("事件发送失败: appFw+0x54e9d8 签名不匹配 (%02x %02x %02x %02x)",
                  fn_addr[0], fn_addr[1], fn_addr[2], fn_addr[3]);
        return -2;
    }
    typedef int (*g5_send_ev_fn_t)(const void *payload, uint32_t size);
    g5_send_ev_fn_t send_fn = (g5_send_ev_fn_t)(g_g4usb_app_base + 0x0054e9d9u);
    uint32_t pkt[6] = { event_id, 0u, 0u, 1u, 0u, 0u };
    int rc = send_fn(pkt, (uint32_t)sizeof(pkt));
    g4usb_log("向五代总线注入事件: event_id=0x%08x, rc=%d", event_id, rc);
    return rc;
}

/* ----------------------------------------------------------------------------
 * 5. USB Jack 插拔事件穿透、白名单转码与 InfraInput 状态校准
 * ---------------------------------------------------------------------------- */

/* 拦截四代 libUsbControl.so @ 0x00012c6d (sendJackStatusToInput) */
typedef int (*g4usb_send_jack_fn_t)(void *this_ptr, uint32_t jack_type, int status);
static g4usb_send_jack_fn_t g4usb_orig_sendJackStatusToInput = NULL;

static int g4usb_hook_sendJackStatusToInput(void *this_ptr, uint32_t jack_type, int status)
{
    int normalized = (status != 0) ? 1 : 0;
    if (this_ptr) {
        g_g4usb_usb_ctrl_this = this_ptr;
    }
    if (jack_type >= 1u && jack_type <= 4u) {
        g_g4usb_jack_state[jack_type] = normalized;
        g_g4usb_jack_seen = 1;
    }

    g4usb_log("sendJackStatusToInput 拦截: this=%p jack_type=%u status=%d", this_ptr, jack_type, status);

    int orig_rc = 0;
    if (g4usb_orig_sendJackStatusToInput) {
        orig_rc = g4usb_orig_sendJackStatusToInput(this_ptr, jack_type, status);
    }

    if (jack_type == 1u) {
        uint32_t ev = normalized ? G5_EVENT_INPUT_KEY_USB_JACK_ON : G5_EVENT_INPUT_KEY_USB_JACK_OFF;
        uint32_t reproc_ev = normalized ? G5_MODEL_REPROCESS_EVENT_USB_JACK_ON : G5_MODEL_REPROCESS_EVENT_USB_JACK_OFF;
        g4usb_log("USB_JACK 物理状态变化 (status=%d) -> 注入 0x%08x + 0x%08x", normalized, ev, reproc_ev);
        g4usb_send_internal_event(ev);
        g4usb_send_internal_event(reproc_ev);
    } else if (jack_type == 2u || jack_type == 3u) {
        uint32_t ev = normalized ? G5_EVENT_INPUT_KEY_USB_JACK_CHARGER_ON : G5_EVENT_INPUT_KEY_USB_JACK_CHARGER_OFF;
        g4usb_log("USB_CHARGER 状态变化 (jack_type=%u, status=%d) -> 注入 0x%08x", jack_type, normalized, ev);
        g4usb_send_internal_event(ev);
    }
    return orig_rc;
}

/* 拦截四代 libUsbControl.so @ 0x00012b21 (keyLocal2PhysCode)，仅捕获实例与首轮诊断 */
typedef uint32_t (*g4usb_key_local2phys_fn_t)(void *this_ptr, uint32_t local_code);
static g4usb_key_local2phys_fn_t g4usb_orig_keyLocal2PhysCode = NULL;
static volatile uint32_t g_g4usb_logged_local_mask = 0u;

static uint32_t g4usb_hook_keyLocal2PhysCode(void *this_ptr, uint32_t local_code)
{
    if (this_ptr) {
        g_g4usb_usb_ctrl_this = this_ptr;
    }
    uint32_t orig_phys = g4usb_orig_keyLocal2PhysCode ? g4usb_orig_keyLocal2PhysCode(this_ptr, local_code) : 0u;
    if (local_code < 16u) {
        uint32_t bit = 1u << local_code;
        if ((g_g4usb_logged_local_mask & bit) == 0u) {
            g_g4usb_logged_local_mask |= bit;
            g4usb_log("keyLocal2PhysCode: local=%u -> orig_phys=0x%x", local_code, orig_phys);
        }
    }
    return orig_phys;
}

/* 主动获取四代 UsbControl::GetInstance() (libUsbControl.so + 0x00011a6du) 单例并查询 GetJackStatus */
static inline void *g4usb_resolve_usb_ctrl_instance(void)
{
    void *ctrl = g_g4usb_usb_ctrl_this;
    if (!ctrl && g_g4usb_usb_base) {
        typedef void *(*g4usb_get_inst_fn_t)(void);
        static g4usb_get_inst_fn_t s_get_inst = NULL;
        static int s_inst_resolved = 0;
        if (!s_inst_resolved) {
            void *hUsb = dlopen("libUsbControl.so", RTLD_NOLOAD | RTLD_LAZY);
            if (hUsb) {
                void *sym = dlsym(hUsb, "_ZN10UsbControl11GetInstanceEv");
                if (sym) {
                    s_get_inst = (g4usb_get_inst_fn_t)sym;
                }
            }
            if (!s_get_inst) {
                /* 0x00011a6d (+1 Thumb) 来自 libUsbControl.so .dynsym */
                s_get_inst = (g4usb_get_inst_fn_t)(g_g4usb_usb_base + 0x00011a6du);
            }
            s_inst_resolved = 1;
        }
        if (s_get_inst) {
            ctrl = s_get_inst();
            if (ctrl) {
                g_g4usb_usb_ctrl_this = ctrl;
            }
        }
    }
    return ctrl;
}

/* 读取四代 libUsbControl::GetJackStatus(this, local_type) 实时状态 */
static inline int g4usb_query_g4_jack_status(uint32_t local_type)
{
    void *ctrl = g4usb_resolve_usb_ctrl_instance();
    if (ctrl && g_g4usb_usb_base) {
        typedef int (*g4usb_get_jack_fn_t)(void *this_ptr, uint32_t jack_type);
        static g4usb_get_jack_fn_t cached_fn = NULL;
        static int resolved = 0;
        if (!resolved) {
            void *hUsb = dlopen("libUsbControl.so", RTLD_NOLOAD | RTLD_LAZY);
            if (hUsb) {
                void *sym = dlsym(hUsb, "_ZN10UsbControl13GetJackStatusEj");
                if (sym) {
                    cached_fn = (g4usb_get_jack_fn_t)sym;
                }
            }
            if (!cached_fn) {
                /* 0x00012901 (+1 Thumb) 来自 libUsbControl.so .dynsym */
                cached_fn = (g4usb_get_jack_fn_t)(g_g4usb_usb_base + 0x00012901u);
            }
            resolved = 1;
        }
        if (cached_fn) {
            int st = cached_fn(ctrl, local_type);
            int norm = (st != 0) ? 1 : 0;
            if (local_type >= 1u && local_type <= 4u) {
                g_g4usb_jack_state[local_type] = norm;
            }
            return norm;
        }
    }
    if (local_type >= 1u && local_type <= 4u) {
        return g_g4usb_jack_state[local_type] ? 1 : 0;
    }
    return 0;
}

/* 判断是否为 USB_JACK (623 / 0x07000001) 或 USB_CHARGER (626 / 0x07000004) 键码 */
static inline int g4usb_keycode_to_local_jack(uint32_t keycode, uint32_t *out_local)
{
    if (keycode == G5_PHY_KEYCODE_USB_JACK || keycode == G5_RAW_PHY_KEYCODE_USB_JACK) {
        if (out_local) *out_local = 1u;
        return 1;
    }
    if (keycode == G5_PHY_KEYCODE_USB_JACK_CHARGER || keycode == G5_RAW_PHY_KEYCODE_USB_JACK_CHARGER) {
        if (out_local) *out_local = 3u;
        return 1;
    }
    return 0;
}

/* 拦截五代 appFw.so @ 0x01c7a89c (get_input_status)，直接校准 keycode=623/626 状态 */
typedef uint32_t *(*g4usb_appfw_get_input_status_fn_t)(uint32_t *out_pair, uint32_t keycode);
static g4usb_appfw_get_input_status_fn_t g4usb_orig_appfw_get_input_status = NULL;

static uint32_t *g4usb_hook_appfw_get_input_status(uint32_t *out_pair, uint32_t keycode)
{
    uint32_t *ret = g4usb_orig_appfw_get_input_status
        ? g4usb_orig_appfw_get_input_status(out_pair, keycode)
        : out_pair;

    uint32_t local = 0u;
    if (g_g4usb_real_host_ready && out_pair && g4usb_keycode_to_local_jack(keycode, &local)) {
        uint32_t orig_st = out_pair[0];
        int real_st = g4usb_query_g4_jack_status(local);
        out_pair[0] = (uint32_t)real_st;
        out_pair[1] = 0u;

        static volatile uint32_t s_gis_log_cnt = 0u;
        uint32_t c = __sync_add_and_fetch(&s_gis_log_cnt, 1u);
        if (c <= 20u || orig_st != out_pair[0]) {
            g4usb_log("appFw::get_input_status 校准: key=0x%x (%u) orig=%u -> real=%u (ctrl=%p)",
                      keycode, keycode, orig_st, out_pair[0], g_g4usb_usb_ctrl_this);
        }
    }
    return ret;
}

/* 拦截五代 libOb5.so @ 0x00208d30 (InfraInput::GetPhyKeyStatusCache) 与 0x00207724 (GetPhyKeyStatus) */
typedef int (*g4usb_ob5_get_phy_key_fn_t)(void *self, int phys_keycode, uint32_t *out_data);
static g4usb_ob5_get_phy_key_fn_t g4usb_orig_ob5_GetPhyKeyStatus      = NULL;
static g4usb_ob5_get_phy_key_fn_t g4usb_orig_ob5_GetPhyKeyStatusCache = NULL;

static int g4usb_hook_ob5_GetPhyKeyStatus(void *self, int phys_keycode, uint32_t *out_data)
{
    int orig_ret = g4usb_orig_ob5_GetPhyKeyStatus
        ? g4usb_orig_ob5_GetPhyKeyStatus(self, phys_keycode, out_data)
        : 0;

    uint32_t local = 0u;
    if (g_g4usb_real_host_ready && g4usb_keycode_to_local_jack((uint32_t)phys_keycode, &local)) {
        int real_st = g4usb_query_g4_jack_status(local);
        if (out_data) {
            out_data[0] = (uint32_t)phys_keycode;
            out_data[1] = 0u;
            out_data[2] = (uint32_t)real_st;
            out_data[3] = 0u;
        }
        if (orig_ret != real_st) {
            g4usb_log("GetPhyKeyStatus 校准: key=0x%x orig_ret=0x%x -> real_st=%d",
                      (unsigned)phys_keycode, (unsigned)orig_ret, real_st);
        }
        return real_st;
    }
    return orig_ret;
}

static int g4usb_hook_ob5_GetPhyKeyStatusCache(void *self, int phys_keycode, uint32_t *out_data)
{
    int orig_ret = g4usb_orig_ob5_GetPhyKeyStatusCache
        ? g4usb_orig_ob5_GetPhyKeyStatusCache(self, phys_keycode, out_data)
        : 0;

    uint32_t local = 0u;
    if (g_g4usb_real_host_ready && g4usb_keycode_to_local_jack((uint32_t)phys_keycode, &local)) {
        int real_st = g4usb_query_g4_jack_status(local);
        if (out_data) {
            out_data[0] = (uint32_t)phys_keycode;
            out_data[1] = 0u;
            out_data[2] = (uint32_t)real_st;
            out_data[3] = 0u;
        }
        static volatile uint32_t s_cache_log_cnt = 0u;
        uint32_t c = __sync_add_and_fetch(&s_cache_log_cnt, 1u);
        if (c <= 20u || orig_ret != real_st) {
            g4usb_log("GetPhyKeyStatusCache 校准: key=0x%x orig_ret=0x%x -> real_st=%d",
                      (unsigned)phys_keycode, (unsigned)orig_ret, real_st);
        }
        return real_st;
    }
    return orig_ret;
}

/* USB 事件白名单判别（供宿主 hook_callback_direct 调用） */
static inline bool g4usb_is_usb_allowed_event(uint32_t keycode)
{
    return (keycode == G5_EVENT_INPUT_KEY_USB_JACK_ON ||
            keycode == G5_EVENT_INPUT_KEY_USB_JACK_OFF ||
            keycode == G5_EVENT_INPUT_KEY_USB_JACK_CHARGER_ON ||
            keycode == G5_EVENT_INPUT_KEY_USB_JACK_CHARGER_OFF ||
            keycode == G5_PHY_KEYCODE_USB_JACK ||
            keycode == G5_PHY_KEYCODE_USB_JACK_CHARGER ||
            keycode == G5_RAW_PHY_KEYCODE_USB_JACK ||
            keycode == G5_RAW_PHY_KEYCODE_USB_JACK_CHARGER);
}

/* 回调事件过滤与转码：若为 USB 事件则直接转发五代，返回 0 表示已接管 */
static inline int g4usb_filter_callback_event(uint32_t keycode, int32_t status)
{
    if (!g4usb_is_usb_allowed_event(keycode)) {
        return -1; /* 非 USB 白名单事件，交由宿主继续处理 */
    }
    int norm = (status != 0) ? 1 : 0;
    uint32_t ev = keycode;
    if (keycode == G5_PHY_KEYCODE_USB_JACK || keycode == G5_RAW_PHY_KEYCODE_USB_JACK) {
        g_g4usb_jack_state[1] = norm;
        g_g4usb_jack_seen = 1;
        ev = norm ? G5_EVENT_INPUT_KEY_USB_JACK_ON : G5_EVENT_INPUT_KEY_USB_JACK_OFF;
    } else if (keycode == G5_PHY_KEYCODE_USB_JACK_CHARGER || keycode == G5_RAW_PHY_KEYCODE_USB_JACK_CHARGER) {
        g_g4usb_jack_state[3] = norm;
        g_g4usb_jack_seen = 1;
        ev = norm ? G5_EVENT_INPUT_KEY_USB_JACK_CHARGER_ON : G5_EVENT_INPUT_KEY_USB_JACK_CHARGER_OFF;
    } else if (keycode == G5_EVENT_INPUT_KEY_USB_JACK_ON) {
        g_g4usb_jack_state[1] = 1;
        g_g4usb_jack_seen = 1;
    } else if (keycode == G5_EVENT_INPUT_KEY_USB_JACK_OFF) {
        g_g4usb_jack_state[1] = 0;
        g_g4usb_jack_seen = 1;
    } else if (keycode == G5_EVENT_INPUT_KEY_USB_JACK_CHARGER_ON) {
        g_g4usb_jack_state[3] = 1;
        g_g4usb_jack_seen = 1;
    } else if (keycode == G5_EVENT_INPUT_KEY_USB_JACK_CHARGER_OFF) {
        g_g4usb_jack_state[3] = 0;
        g_g4usb_jack_seen = 1;
    }
    g4usb_log("hook_callback_direct 放行 USB 事件: keycode=0x%08x status=%d -> inject=0x%08x",
              keycode, (int)status, ev);
    g4usb_send_internal_event(ev);
    if (ev == G5_EVENT_INPUT_KEY_USB_JACK_ON) {
        g4usb_send_internal_event(G5_MODEL_REPROCESS_EVENT_USB_JACK_ON);
    } else if (ev == G5_EVENT_INPUT_KEY_USB_JACK_OFF) {
        g4usb_send_internal_event(G5_MODEL_REPROCESS_EVENT_USB_JACK_OFF);
    }
    return 0;
}

/* ----------------------------------------------------------------------------
 * 6. libBusPower (054C:096F) 优雅停机让权与 libUsbControl 全链路监控
 * ---------------------------------------------------------------------------- */

static inline void g4usb_stop_buspower_if_active(void)
{
    if (!g_g4usb_real_host_ready || !g_g4usb_usb_base) {
        return;
    }

    uint32_t cur_exp = g_g4usb_last_expected_mask;
    uint32_t cur_st  = g_g4usb_last_started_mask;
    void *ctrl = g_g4usb_usb_ctrl_this;
    if (ctrl) {
        cur_exp = *(const volatile uint32_t *)((uintptr_t)ctrl + 0x94u);
        cur_st  = *(const volatile uint32_t *)((uintptr_t)ctrl + 0x98u);
    }

    if (ctrl && (cur_st & 0x4u) == 0u && (cur_exp & 0x4u) == 0u) {
        g4usb_log("BusPower 未占用 Gadget (expected=0x%x started=0x%x)，无需停机让权", cur_exp, cur_st);
        return;
    }

    void *hBp = dlopen("libBusPower.so", RTLD_NOLOAD | RTLD_LAZY);
    if (!hBp) {
        g4usb_log("警告: 未在进程中找到已加载的 libBusPower.so，跳过 BusPower 停机让权");
        return;
    }

    typedef void *(*bp_get_inst_fn_t)(void);
    typedef int (*bp_stop_fn_t)(void *this_ptr);

    bp_get_inst_fn_t get_inst = (bp_get_inst_fn_t)dlsym(hBp, "_ZN8BusPower11GetInstanceEv");
    bp_stop_fn_t stop_all     = (bp_stop_fn_t)dlsym(hBp, "_ZN8BusPower20StopBusPowerFuctionsEv");
    bp_stop_fn_t stop_usb     = (bp_stop_fn_t)dlsym(hBp, "_ZN8BusPower28UsbCtrl_StopBusPowerFunctionEv");

    void *bp = get_inst ? get_inst() : NULL;
    if (!bp) {
        g4usb_log("警告: BusPower::GetInstance() 为空，无法调用 StopBusPowerFuctions");
        return;
    }

    g4usb_log("检测到 BusPower (mask=0x4, 054C:096F) 激活 (expected=0x%x started=0x%x)，触发优雅停机让权...",
              cur_exp, cur_st);

    int rc = -1;
    if (stop_all) {
        rc = stop_all(bp);
        g4usb_log("调用 BusPower::StopBusPowerFuctions(bp=%p) -> rc=%d", bp, rc);
    }
    if (rc != 0 && stop_usb) {
        rc = stop_usb(bp);
        g4usb_log("调用备用 BusPower::UsbCtrl_StopBusPowerFunction(bp=%p) -> rc=%d", bp, rc);
    }

    /* 等待 libUsbControl 异步卸载 BusPower Gadget 并归零掩码（最长 25 * 20ms = 500ms） */
    int cleared = 0;
    for (int i = 0; i < 25; i++) {
        usleep(20000);
        ctrl = g_g4usb_usb_ctrl_this;
        if (ctrl) {
            cur_exp = *(const volatile uint32_t *)((uintptr_t)ctrl + 0x94u);
            cur_st  = *(const volatile uint32_t *)((uintptr_t)ctrl + 0x98u);
            if ((cur_st & 0x4u) == 0u && cur_exp == 0u) {
                cleared = 1;
                g4usb_log("BusPower 停机完成 (第 %d 次轮询): expected=0x%x started=0x%x，等待 UDC 断开稳定 80ms",
                          i + 1, cur_exp, cur_st);
                usleep(80000);
                break;
            }
        }
    }
    if (!cleared) {
        g4usb_log("警告: 等待 BusPower 停机 500ms 超时 (expected=0x%x started=0x%x)，继续尝试启动 MSC",
                  cur_exp, cur_st);
    }
}

/* libUsbControl:chkStartGadgetOK (0x00010cc9) 仅在必要时同步，不破坏模式切换 */
typedef void (*g4usb_chk_start_gadget_fn_t)(void *usb_ctrl);
static g4usb_chk_start_gadget_fn_t g4usb_orig_chkStartGadgetOK = NULL;

static void g4usb_hook_chkStartGadgetOK(void *usb_ctrl)
{
    if (usb_ctrl) {
        g_g4usb_usb_ctrl_this = usb_ctrl;
        uint32_t expected = *(uint32_t *)((uintptr_t)usb_ctrl + 0x94u);
        uint32_t started  = *(uint32_t *)((uintptr_t)usb_ctrl + 0x98u);
        g_g4usb_last_expected_mask = expected;
        g_g4usb_last_started_mask  = started;

        if (expected != 0u && started != 0u && started != expected && (expected & 0x4u) == 0u) {
            g4usb_log("chkStartGadgetOK 告警校准: expected_mask=0x%x != started_mask=0x%x -> 同步为 expected",
                      expected, started);
            *(uint32_t *)((uintptr_t)usb_ctrl + 0x98u) = expected;
            g_g4usb_last_started_mask = expected;
        } else {
            g4usb_log("chkStartGadgetOK: expected_mask=0x%x started_mask=0x%x", expected, started);
        }
    }
    if (g4usb_orig_chkStartGadgetOK) {
        g4usb_orig_chkStartGadgetOK(usb_ctrl);
    }
}

typedef int (*g4usb_action_gadget_fn_t)(void *fsm, int ev, void *param);
static g4usb_action_gadget_fn_t g4usb_orig_actionGStartGadget    = NULL;
static g4usb_action_gadget_fn_t g4usb_orig_actionGConfigureFunc  = NULL;
static g4usb_action_gadget_fn_t g4usb_orig_actionGStopGadget     = NULL;

static int g4usb_hook_actionGConfigureFunc(void *fsm, int ev, void *param)
{
    void *ctrl = g_g4usb_usb_ctrl_this;
    uint32_t exp_before = ctrl ? *(uint32_t *)((uintptr_t)ctrl + 0x94u) : g_g4usb_last_expected_mask;
    uint32_t st_before  = ctrl ? *(uint32_t *)((uintptr_t)ctrl + 0x98u) : g_g4usb_last_started_mask;
    int rc = g4usb_orig_actionGConfigureFunc ? g4usb_orig_actionGConfigureFunc(fsm, ev, param) : 0;
    ctrl = g_g4usb_usb_ctrl_this;
    uint32_t exp_after = ctrl ? *(uint32_t *)((uintptr_t)ctrl + 0x94u) : g_g4usb_last_expected_mask;
    uint32_t st_after  = ctrl ? *(uint32_t *)((uintptr_t)ctrl + 0x98u) : g_g4usb_last_started_mask;
    g4usb_log("actionGConfigureFunc(ev=%d): mask before=(exp=0x%x,st=0x%x) after=(exp=0x%x,st=0x%x) rc=%d",
              ev, exp_before, st_before, exp_after, st_after, rc);
    return rc;
}

static int g4usb_hook_actionGStartGadget(void *fsm, int ev, void *param)
{
    g4usb_log("actionGStartGadget 触发: fsm=%p ev=%d param=%p", fsm, ev, param);
    int rc = g4usb_orig_actionGStartGadget ? g4usb_orig_actionGStartGadget(fsm, ev, param) : 0;
    g4usb_log("actionGStartGadget 执行完毕: rc=%d", rc);
    return rc;
}

static int g4usb_hook_actionGStopGadget(void *fsm, int ev, void *param)
{
    void *ctrl = g_g4usb_usb_ctrl_this;
    uint32_t exp_before = ctrl ? *(uint32_t *)((uintptr_t)ctrl + 0x94u) : g_g4usb_last_expected_mask;
    uint32_t st_before  = ctrl ? *(uint32_t *)((uintptr_t)ctrl + 0x98u) : g_g4usb_last_started_mask;
    int rc = g4usb_orig_actionGStopGadget ? g4usb_orig_actionGStopGadget(fsm, ev, param) : 0;
    ctrl = g_g4usb_usb_ctrl_this;
    uint32_t exp_after = ctrl ? *(uint32_t *)((uintptr_t)ctrl + 0x94u) : g_g4usb_last_expected_mask;
    uint32_t st_after  = ctrl ? *(uint32_t *)((uintptr_t)ctrl + 0x98u) : g_g4usb_last_started_mask;
    g4usb_log("actionGStopGadget(ev=%d): mask before=(exp=0x%x,st=0x%x) after=(exp=0x%x,st=0x%x) rc=%d",
              ev, exp_before, st_before, exp_after, st_after, rc);
    return rc;
}

typedef int (*g4usb_if_start_gadget_fn_t)(void *this_ptr, int mask, int param);
static g4usb_if_start_gadget_fn_t g4usb_orig_IfStartGadget = NULL;

static int g4usb_hook_IfStartGadget(void *this_ptr, int mask, int param)
{
    int rc = g4usb_orig_IfStartGadget ? g4usb_orig_IfStartGadget(this_ptr, mask, param) : 0;
    g4usb_log("UsbIfWrapper::StartGadget(mask=0x%x, param=%d) -> rc=%d", mask, param, rc);
    return rc;
}

/* ----------------------------------------------------------------------------
 * 7. 五代 appFw.so MSC 前置查询、UPM 保护、media_id_list 清洗与 BusPower 让权
 * ---------------------------------------------------------------------------- */

typedef int (*g4usb_media_by_attr_fn_t)(uint32_t *attr_in, uint32_t *resp_0x48);
static g4usb_media_by_attr_fn_t g4usb_orig_GetMediaIDByAttribute = NULL;

static inline int g4usb_is_simulate_id(uint32_t id)
{
    if (id == 0u || id == 0xffffffffu) {
        return 0;
    }
    if (g_g4usb_cached_simulate_media_id != 0u && id == g_g4usb_cached_simulate_media_id) {
        return 1;
    }
#ifdef G4HOST_BRIDGE_MEDIA_H
    if (g4media_is_simulate_media(id)) {
        return 1;
    }
#endif
    return 0;
}

static inline void g4usb_ensure_simulate_id_cached(void)
{
    if (g_g4usb_cached_simulate_media_id != 0u || !g4usb_orig_GetMediaIDByAttribute) {
        return;
    }
    uint32_t sim_attr = 0x27u;
    uint32_t sim_resp[18];
    memset(sim_resp, 0, sizeof(sim_resp));
    (void)g4usb_orig_GetMediaIDByAttribute(&sim_attr, sim_resp);
    if (sim_resp[0] > 0u && sim_resp[1] != 0u && sim_resp[1] != 0xffffffffu) {
        g_g4usb_cached_simulate_media_id = sim_resp[1];
#ifdef G4HOST_BRIDGE_MEDIA_H
        g4media_set_simulate_media_id(sim_resp[1]);
#endif
        g4usb_log("预缓存 SIMULATE media_id=0x%x (用于 attr=0x8 过滤)", sim_resp[1]);
    }
}

/* appFw.so @ 0x02b1afc8 -> GetMediaIDByAttribute(attr_in, resp_0x48) */
static int g4usb_hook_GetMediaIDByAttribute(uint32_t *attr_in, uint32_t *resp_0x48)
{
    uint32_t attr = attr_in ? *attr_in : 0u;
    if (!g_g4usb_real_host_ready || !g4usb_orig_GetMediaIDByAttribute) {
        if (resp_0x48) {
            memset(resp_0x48, 0, 0x48u); /* 含 +0x44 err_code 完整 0x48B 清零 */
        }
        g4usb_log("GetMediaIDByAttribute(QEMU安全路径): attr=0x%x -> 完整清零 0x48B", attr);
        return 0;
    }

    int rc = g4usb_orig_GetMediaIDByAttribute(attr_in, resp_0x48);
    if (resp_0x48) {
        uint32_t count = resp_0x48[0];
        uint32_t id0   = resp_0x48[1];
        uint32_t id1   = resp_0x48[2];
        uint32_t err   = resp_0x48[17]; /* +0x44 */
        g4usb_log("GetMediaIDByAttribute(attr=0x%x) -> rc=%d count=%u id=[0x%x,0x%x] err=0x%x",
                  attr, rc, count, id0, id1, err);

        if (attr == 0x27u) {
            /* SIMULATE (/nondev/simulate): 与 Slot1 严格隔离，绝不污染真实 SD 缓存 */
            if (count > 0u && id0 != 0u && id0 != 0xffffffffu) {
                g_g4usb_cached_simulate_media_id = id0;
#ifdef G4HOST_BRIDGE_MEDIA_H
                g4media_set_simulate_media_id(id0);
#endif
                g4usb_log("记录 SIMULATE media_id=0x%x (严格隔离，不写入 Slot1 缓存)", id0);
            }
        } else if (attr == 8u) {
            /* 若尚未感知 simulate_id，先查询一次 0x27 缓存，防止冷启动顺序倒置导致 0x4000 漏网 */
            g4usb_ensure_simulate_id_cached();
            /* Gen5 SLOT_1 (0x08) -> 跨代映射 Gen4 ADJ_SLOT_1 (0x48) / REMOVABLEMEDIA1 (0x42)，严禁映射到 0x27 */
            if (count > 0u && id0 != 0u && id0 != 0xffffffffu && !g4usb_is_simulate_id(id0)) {
                g_g4usb_cached_slot1_media_id = id0;
                g_g4usb_real_media_seen = 1;
#ifdef G4HOST_BRIDGE_MEDIA_H
                g4media_set_slot1_media_id(id0);
#endif
                g4usb_log("直接命中真实 Slot1 media_id=0x%x (attr=0x8)", id0);
            } else {
                static const uint32_t g4_slot1_attrs[2] = { 0x48u, 0x42u };
                uint32_t valid_ids[16];
                uint32_t valid_cnt = 0u;
                uint32_t hit_attr = 0u;

                for (size_t a = 0u; a < 2u && valid_cnt == 0u; a++) {
                    uint32_t probe_attr = g4_slot1_attrs[a];
                    uint32_t probe_resp[18];
                    memset(probe_resp, 0, sizeof(probe_resp));
                    (void)g4usb_orig_GetMediaIDByAttribute(&probe_attr, probe_resp);

                    uint32_t p_cnt = probe_resp[0];
                    if (p_cnt > 16u) {
                        p_cnt = 16u;
                    }
                    for (uint32_t i = 0u; i < p_cnt; i++) {
                        uint32_t cand_id = probe_resp[1u + i];
                        if (cand_id == 0u || cand_id == 0xffffffffu) {
                            continue;
                        }
                        if (g4usb_is_simulate_id(cand_id)) {
                            continue;
                        }
                        valid_ids[valid_cnt++] = cand_id;
                    }
                    if (valid_cnt > 0u) {
                        hit_attr = probe_attr;
                    }
                }

                if (valid_cnt > 0u) {
                    resp_0x48[0] = valid_cnt;
                    for (uint32_t i = 0u; i < valid_cnt; i++) {
                        resp_0x48[1u + i] = valid_ids[i];
                    }
                    for (uint32_t i = valid_cnt; i < 16u; i++) {
                        resp_0x48[1u + i] = 0u;
                    }
                    resp_0x48[17] = 0u;
                    err = 0u;
                    g_g4usb_cached_slot1_media_id = valid_ids[0];
                    g_g4usb_real_media_seen = 1;
#ifdef G4HOST_BRIDGE_MEDIA_H
                    g4media_set_slot1_media_id(valid_ids[0]);
#endif
                    g4usb_log("GetMediaIDByAttribute(attr=0x8) 跨代映射 attr=0x%x 命中 Slot1 (cnt=%u, id0=0x%x)",
                              hit_attr, valid_cnt, valid_ids[0]);
                } else {
                    memset(resp_0x48, 0, 0x48u);
                    err = 0u;
                    g_g4usb_real_media_seen = 0;
#ifdef G4HOST_BRIDGE_MEDIA_H
                    g_g4usb_cached_slot1_media_id = g4media_get_slot1_media_id();
#else
                    g_g4usb_cached_slot1_media_id = 0x00001000u;
#endif
                    g4usb_log("GetMediaIDByAttribute(attr=0x8) 探测 attr=0x48/0x42 无真实 Slot1");
                }
            }
        } else if (attr == 9u) {
            /* Gen5 SLOT_2 (0x09) -> 尝试 Gen4 ADJ_SLOT_2 (0x49)，单卡机无卡槽保持 count=0 并清零数组，绝不伪造或污染 Slot1 */
            if (count == 0u) {
                uint32_t probe_attr = 0x49u;
                uint32_t probe_resp[18];
                memset(probe_resp, 0, sizeof(probe_resp));
                (void)g4usb_orig_GetMediaIDByAttribute(&probe_attr, probe_resp);
                if (probe_resp[0] > 0u && probe_resp[1] != 0u && probe_resp[1] != 0xffffffffu &&
                    !g4usb_is_simulate_id(probe_resp[1])) {
                    uint32_t p_cnt = (probe_resp[0] > 16u) ? 16u : probe_resp[0];
                    resp_0x48[0] = p_cnt;
                    for (uint32_t i = 0u; i < p_cnt; i++) {
                        resp_0x48[1u + i] = probe_resp[1u + i];
                    }
                    for (uint32_t i = p_cnt; i < 16u; i++) {
                        resp_0x48[1u + i] = 0u;
                    }
                    resp_0x48[17] = 0u;
                    err = 0u;
                    g4usb_log("GetMediaIDByAttribute(attr=0x9) 映射 attr=0x49 命中 Slot2 (id0=0x%x)",
                              resp_0x48[1]);
                } else {
                    memset(resp_0x48, 0, 0x48u);
                    err = 0u;
                }
            }
        }

        /* 兜底清零 err 规避 0x02aeadd8 FATAL 断言 */
        if (err != 0u) {
            g4usb_log("警告: GetMediaIDByAttribute(attr=0x%x) 返回 err=0x%x，兜底清零 err 规避 0x02aeadd8 断言",
                      attr, err);
            resp_0x48[17] = 0u;
        }
    }
    return rc;
}

typedef int (*g4usb_media_mount_info_fn_t)(uint32_t *req, uint32_t *status_out);
static g4usb_media_mount_info_fn_t g4usb_orig_GetMediaMountInfo = NULL;

/* appFw.so @ 0x02b1b19c -> Cmd_11008 wrapper (bl #0x2b738c8, req={media_id, out_name}, status_out) */
static int g4usb_hook_GetMediaMountInfo(uint32_t *req, uint32_t *status_out)
{
    uint32_t media_id = req ? req[0] : 0u;
    if (!g_g4usb_real_host_ready || !g4usb_orig_GetMediaMountInfo) {
        if (status_out) {
            *status_out = 0u;
        }
        return 0;
    }
    int rc = g4usb_orig_GetMediaMountInfo(req, status_out);
    g4usb_log("Cmd_11008_02b1b19c(media_id=0x%x) -> rc=%d status=%u",
              media_id, rc, status_out ? *status_out : 0u);
    return rc;
}

typedef int (*g4usb_upm_set_fn_t)(uint32_t *req, uint32_t *err_out);
static g4usb_upm_set_fn_t g4usb_orig_UpmSet = NULL;

/* appFw.so @ 0x02b1e450 -> infra_wrapper_upm(req, err_out) 防止 line 139 断言 */
static int g4usb_hook_UpmSet(uint32_t *req, uint32_t *err_out)
{
    uint32_t cmd = req ? req[0] : 0u;
    uint32_t arg = req ? req[1] : 0u;
    if (!g_g4usb_real_host_ready || !g4usb_orig_UpmSet) {
        if (err_out) {
            *err_out = 0u;
        }
        return 0;
    }
    int rc = g4usb_orig_UpmSet(req, err_out);
    uint32_t err = err_out ? *err_out : 0u;
    static volatile uint32_t s_upm_log_cnt = 0u;
    uint32_t c = __sync_add_and_fetch(&s_upm_log_cnt, 1u);
    if (c <= 8u || cmd == 0u || err != 0u) {
        g4usb_log("UpmSet(cmd=%u, arg=%u) -> rc=%d err=0x%x", cmd, arg, rc, err);
    }
    if (err_out && err != 0u) {
        g4usb_log("警告: UpmSet(cmd=%u, arg=%u) err=0x%x，兜底清零规避 line 139 断言", cmd, arg, err);
        *err_out = 0u;
    }
    return rc;
}

typedef int (*g4usb_msc_2arg_fn_t)(void *a0, void *a1);
static g4usb_msc_2arg_fn_t g4usb_orig_MscConfigureFunc  = NULL;
static g4usb_msc_2arg_fn_t g4usb_orig_MscImOpen         = NULL;
static g4usb_msc_2arg_fn_t g4usb_orig_MscStartOrExtCmd  = NULL;

/* appFw.so @ 0x02b1eb7c -> MscConfigureFunc(media_req[4], out) */
static int g4usb_hook_MscConfigureFunc(uint32_t *media_req, void *out)
{
    if (!g_g4usb_real_host_ready || !g4usb_orig_MscConfigureFunc) {
        if (out) {
            *(uint32_t *)out = 0u;
        }
        g4usb_log("MscConfigureFunc(QEMU安全路径): 直接返回成功");
        return 0;
    }

    /* 动作 A: 优雅停止四代自启动的 BusPower (054C:096F) 使 libUsbControl 回到 STATE_ID_STOP */
    g4usb_stop_buspower_if_active();

    /* 动作 B: 过滤无效 media_id (0 / 0xffffffff) 并去重，防止 DriveListMaking() 返回错误 1 */
    uint32_t raw0 = media_req ? media_req[0] : 0u;
    uint32_t raw1 = media_req ? media_req[1] : 0u;
    uint32_t raw2 = media_req ? media_req[2] : 0u;
    uint32_t raw_cnt = media_req ? media_req[3] : 0u;

    uint32_t clean_req[4] = { 0u, 0u, 0u, 0u };
    uint32_t scan_cnt = (raw_cnt > 3u) ? 3u : raw_cnt;
    uint32_t out_cnt = 0u;
    if (media_req) {
        for (uint32_t i = 0u; i < scan_cnt; i++) {
            uint32_t id = media_req[i];
            if (id == 0u || id == 0xffffffffu) {
                continue;
            }
            if (g_g4usb_cached_simulate_media_id != 0u && id == g_g4usb_cached_simulate_media_id) {
                continue;
            }
#ifdef G4HOST_BRIDGE_MEDIA_H
            if (g4media_is_simulate_media(id)) {
                continue;
            }
#endif
            bool dup = false;
            for (uint32_t j = 0u; j < out_cnt; j++) {
                if (clean_req[j] == id) {
                    dup = true;
                    break;
                }
            }
            if (!dup) {
                clean_req[out_cnt++] = id;
            }
        }
    }

    uint32_t slot1_id = g_g4usb_cached_slot1_media_id;
    int phys_present = 1;
#ifdef G4HOST_BRIDGE_MEDIA_H
    slot1_id = g4media_get_slot1_media_id();
    phys_present = g4media_probe_phys_card_present_in_g4(slot1_id, NULL, 0u, NULL);
#else
    phys_present = (g_g4usb_real_media_seen != 0);
#endif

    /* 仅当确认物理卡在位时才在列表被滤空时补真实 Slot1；无卡时保持 n=0，严禁塞入假卡或 simulate */
    int used_fallback = 0;
    if (out_cnt == 0u && phys_present && (g_g4usb_real_media_seen != 0 || raw_cnt > 0u)) {
        clean_req[0] = slot1_id;
        out_cnt = 1u;
        used_fallback = 1;
    }
    clean_req[3] = out_cnt;

    g4usb_log("MscConfigureFunc: raw=[0x%x,0x%x,0x%x](n=%u) -> clean=[0x%x,0x%x,0x%x](n=%u, seen=%d, phys=%d)",
              raw0, raw1, raw2, raw_cnt, clean_req[0], clean_req[1], clean_req[2], clean_req[3],
              g_g4usb_real_media_seen, phys_present);

    int rc = g4usb_orig_MscConfigureFunc(clean_req, out);
    uint32_t out_err = out ? *(const volatile uint32_t *)out : 0u;
    if (out_cnt == 0u && !used_fallback && phys_present && (rc != 0 || out_err != 0u)) {
        clean_req[0] = slot1_id;
        clean_req[3] = 1u;
        g4usb_log("MscConfigureFunc 物理卡在位但 n=0 返回 rc=%d err=0x%x，尝试回退 Slot1=0x%x",
                  rc, out_err, clean_req[0]);
        rc = g4usb_orig_MscConfigureFunc(clean_req, out);
        out_err = out ? *(const volatile uint32_t *)out : 0u;
    }
    g4usb_log("MscConfigureFunc 调用完毕 -> rc=%d out_err=0x%x", rc, out_err);
    return rc;
}

/* 共享 MSC 2 参数 wrapper 包装辅助（QEMU 安全清零 out 并透传真机日志） */
static inline int g4usb_wrap_msc_2arg(const char *tag,
                                      g4usb_msc_2arg_fn_t orig_fn,
                                      void *a0,
                                      void *a1)
{
    if (!g_g4usb_real_host_ready || !orig_fn) {
        if (a1) {
            *(uint32_t *)a1 = 0u;
        }
        g4usb_log("%s(QEMU安全路径): 直接返回 0", tag);
        return 0;
    }
    int rc = orig_fn(a0, a1);
    uint32_t err = a1 ? *(const volatile uint32_t *)a1 : 0u;
    g4usb_log("%s -> rc=%d out=0x%x", tag, rc, err);
    return rc;
}

static g4usb_msc_2arg_fn_t g4usb_orig_MscImClose   = NULL;
static g4usb_msc_2arg_fn_t g4usb_orig_MscWrap6     = NULL;
static g4usb_msc_2arg_fn_t g4usb_orig_MscWrap10    = NULL;
static g4usb_msc_2arg_fn_t g4usb_orig_MscRegCb1    = NULL;
static g4usb_msc_2arg_fn_t g4usb_orig_MscRegCb2    = NULL;
static g4usb_msc_2arg_fn_t g4usb_orig_FsysUnmount  = NULL;
static g4usb_upm_set_fn_t  g4usb_orig_MscStartTimer = NULL;
static g4usb_upm_set_fn_t  g4usb_orig_MscStopTimer  = NULL;

static int g4usb_hook_MscImOpen(void *a0, void *a1)
{
    return g4usb_wrap_msc_2arg("MscImOpen(0x02b1eb10)", g4usb_orig_MscImOpen, a0, a1);
}

static int g4usb_hook_MscImClose(void *a0, void *a1)
{
    return g4usb_wrap_msc_2arg("MscImClose(0x02b1eb34)", g4usb_orig_MscImClose, a0, a1);
}

static int g4usb_hook_MscStartOrExtCmd(void *a0, void *a1)
{
    return g4usb_wrap_msc_2arg("MscStartOrExtCmd(0x02b1eba0)", g4usb_orig_MscStartOrExtCmd, a0, a1);
}

static int g4usb_hook_MscWrap6(void *a0, void *a1)
{
    return g4usb_wrap_msc_2arg("MscWrap6(0x02b1eb58)", g4usb_orig_MscWrap6, a0, a1);
}

static int g4usb_hook_MscWrap10(void *a0, void *a1)
{
    return g4usb_wrap_msc_2arg("MscWrap10(0x02b1ecc0)", g4usb_orig_MscWrap10, a0, a1);
}

static int g4usb_hook_MscRegCb1(void *a0, void *a1)
{
    return g4usb_wrap_msc_2arg("MscRegCb_stat_activated(0x02b1ed2c)", g4usb_orig_MscRegCb1, a0, a1);
}

static int g4usb_hook_MscRegCb2(void *a0, void *a1)
{
    return g4usb_wrap_msc_2arg("MscRegCb_conn_failed(0x02b1ed50)", g4usb_orig_MscRegCb2, a0, a1);
}

static int g4usb_hook_FsysUnmount(void *a0, void *a1)
{
    uint32_t arg0 = a0 ? *(const volatile uint32_t *)a0 : 0u;
    int rc = g4usb_wrap_msc_2arg("Fsys_Unmount(0x02b1b1e4)", g4usb_orig_FsysUnmount, a0, a1);
    if (g_g4usb_real_host_ready) {
        g4usb_log("Fsys_Unmount 详情: arg0=0x%x rc=%d", arg0, rc);
    }
    return rc;
}

/* 保护 seq_0x297::run 内的 start_timer(0x02b1ea58) / stop_timer(0x02b1ea7c)，非 0 err 会直接触发 0x02bc3174 断言 */
static inline int g4usb_wrap_msc_timer(const char *tag,
                                       g4usb_upm_set_fn_t orig_fn,
                                       uint32_t *req,
                                       uint32_t *err_out)
{
    uint32_t tid = req ? req[0] : 0u;
    if (!g_g4usb_real_host_ready || !orig_fn) {
        if (err_out) {
            *err_out = 0u;
        }
        return 0;
    }
    int rc = orig_fn(req, err_out);
    uint32_t err = err_out ? *err_out : 0u;
    if (tid == 0xcu || tid == 0xdu || err != 0u) {
        g4usb_log("%s(timer_id=0x%x) -> rc=%d err=0x%x", tag, tid, rc, err);
    }
    if (err_out && err != 0u && (tid == 0xcu || tid == 0xdu)) {
        g4usb_log("警告: %s(timer_id=0x%x) err=0x%x，兜底清零规避 0x02bc3174 断言", tag, tid, err);
        *err_out = 0u;
    }
    return rc;
}

static int g4usb_hook_MscStartTimer(uint32_t *req, uint32_t *err_out)
{
    return g4usb_wrap_msc_timer("MscStartTimer(0x02b1ea58)", g4usb_orig_MscStartTimer, req, err_out);
}

static int g4usb_hook_MscStopTimer(uint32_t *req, uint32_t *err_out)
{
    return g4usb_wrap_msc_timer("MscStopTimer(0x02b1ea7c)", g4usb_orig_MscStopTimer, req, err_out);
}

/* ----------------------------------------------------------------------------
 * 7b. 五代 seq 0x297 / 0x294 / 0x298 调度链路穿透、冲突死锁自愈与执行监控
 * ---------------------------------------------------------------------------- */

typedef int (*g4usb_seq_enq_fn_t)(void *mgr, uint32_t a1, uint32_t a2);
typedef int (*g4usb_seq_disp_fn_t)(void *item);
typedef int (*g4usb_seq_0arg_fn_t)(void);
typedef int (*g4usb_seq_1arg_fn_t)(void *arg0);
typedef int (*g4usb_add_runq_fn_t)(void *mgr, void *seq_obj, uint32_t seq_id, uintptr_t arg3, uint32_t arg4);

static g4usb_seq_enq_fn_t  g4usb_orig_seq297_enq    = NULL;
static g4usb_seq_enq_fn_t  g4usb_orig_seq294_enq    = NULL;
static g4usb_seq_disp_fn_t g4usb_orig_seq297_disp   = NULL;
static g4usb_seq_disp_fn_t g4usb_orig_seq294_disp   = NULL;
static g4usb_seq_disp_fn_t g4usb_orig_seq298_disp   = NULL;
static g4usb_seq_0arg_fn_t g4usb_orig_queue_disp    = NULL;
static g4usb_add_runq_fn_t g4usb_orig_add_runq      = NULL;
static g4usb_seq_1arg_fn_t g4usb_orig_wait_conflict = NULL;
static g4usb_seq_1arg_fn_t g4usb_orig_seq297_run    = NULL;
static g4usb_seq_1arg_fn_t g4usb_orig_seq294_run    = NULL;

/* 确保全局序列开关 [appFw + 0x06e23080] 在 USB 入队时保持开启 */
static inline void g4usb_ensure_seq_gate_open(const char *who)
{
    if (!g_g4usb_app_base) {
        return;
    }
    volatile uint8_t *gate = (volatile uint8_t *)(g_g4usb_app_base + 0x06e23080u);
    if (*gate == 0u) {
        g4usb_log("警告: %s 检测到全局序列开关 [0x06e23080]==0，强制置 1", who);
        *gate = 1u;
    }
}

static int g4usb_hook_seq297_enq(void *mgr, uint32_t a1, uint32_t a2)
{
    g4usb_ensure_seq_gate_open("seq_0x297_enq");
    uint32_t cnt = __sync_add_and_fetch(&g_g4usb_seq297_enq_cnt, 1u);
    g_g4usb_seq297_pending = 1;
    g_g4usb_qdisp_stall_ticks = 0u;
    g4usb_log(">>> seq_0x297_enq(MSC_START) #%u: mgr=%p a1=%u a2=%u", cnt, mgr, a1, a2);
    return g4usb_orig_seq297_enq ? g4usb_orig_seq297_enq(mgr, a1, a2) : 0;
}

static int g4usb_hook_seq294_enq(void *mgr, uint32_t a1, uint32_t a2)
{
    g4usb_ensure_seq_gate_open("seq_0x294_enq");
    uint32_t cnt = __sync_add_and_fetch(&g_g4usb_seq294_enq_cnt, 1u);
    g_g4usb_seq297_pending = 1;
    g_g4usb_qdisp_stall_ticks = 0u;
    g4usb_log(">>> seq_0x294_enq(MTP_START) #%u: mgr=%p a1=%u a2=%u", cnt, mgr, a1, a2);
    return g4usb_orig_seq294_enq ? g4usb_orig_seq294_enq(mgr, a1, a2) : 0;
}

static int g4usb_hook_seq297_disp(void *item)
{
    uint32_t cnt = __sync_add_and_fetch(&g_g4usb_seq297_disp_cnt, 1u);
    uint32_t sid = item ? *(const volatile uint32_t *)item : 0u;
    uint32_t p1  = item ? *(const volatile uint32_t *)((uintptr_t)item + 4u) : 0u;
    uint8_t en   = item ? *(const volatile uint8_t *)((uintptr_t)item + 0x60u) : 0u;
    g_g4usb_seq297_pending = 0;
    g4usb_log(">>> seq_0x297_disp #%u: item=%p sid=0x%x p1=%u flag60=%u", cnt, item, sid, p1, en);
    return g4usb_orig_seq297_disp ? g4usb_orig_seq297_disp(item) : 0;
}

static int g4usb_hook_seq294_disp(void *item)
{
    uint32_t cnt = __sync_add_and_fetch(&g_g4usb_seq294_disp_cnt, 1u);
    uint32_t sid = item ? *(const volatile uint32_t *)item : 0u;
    g_g4usb_seq297_pending = 0;
    g4usb_log(">>> seq_0x294_disp #%u: item=%p sid=0x%x", cnt, item, sid);
    return g4usb_orig_seq294_disp ? g4usb_orig_seq294_disp(item) : 0;
}

static int g4usb_hook_seq298_disp(void *item)
{
    uint32_t cnt = __sync_add_and_fetch(&g_g4usb_seq298_disp_cnt, 1u);
    uint32_t sid = item ? *(const volatile uint32_t *)item : 0u;
    g4usb_log(">>> seq_0x298_disp(MSC_STOP) #%u: item=%p sid=0x%x", cnt, item, sid);
    return g4usb_orig_seq298_disp ? g4usb_orig_seq298_disp(item) : 0;
}

/* appFw.so @ 0x01c1cf78 -> 主队列派发器（0 参数），仅在待执行 USB 序列滞留时输出观测 */
static int g4usb_hook_queue_disp(void)
{
    int pending_before = g_g4usb_seq297_pending;
    int rc = g4usb_orig_queue_disp ? g4usb_orig_queue_disp() : 0;
    if (pending_before && g_g4usb_seq297_pending) {
        uint32_t ticks = __sync_add_and_fetch(&g_g4usb_qdisp_stall_ticks, 1u);
        if (ticks == 1u || ticks == 10u || ticks == 50u || ticks == 200u) {
            /* 检查 0x06e230a8 队列当前项数量与首项 seq_id/flag60（vector: [0]=begin, [1]=end） */
            uintptr_t q_base = g_g4usb_app_base ? (g_g4usb_app_base + 0x06e230a8u) : 0u;
            uint32_t q_cnt = 0u;
            uint32_t head_sid = 0u;
            uint8_t head_flag60 = 0u;
            if (q_base) {
                uintptr_t q_obj = *(const volatile uintptr_t *)q_base;
                if (q_obj > 0x10000u) {
                    const volatile uintptr_t *vec = (const volatile uintptr_t *)(q_obj + 8u);
                    uintptr_t beg = vec[0];
                    uintptr_t end = vec[1];
                    if (beg > 0x10000u && end >= beg && (end - beg) <= 0x1000u && ((end - beg) & 3u) == 0u) {
                        q_cnt = (uint32_t)((end - beg) >> 2);
                        if (q_cnt > 0u) {
                            uintptr_t sp_item = *(const volatile uintptr_t *)beg;
                            if (sp_item > 0x10000u) {
                                head_sid = *(const volatile uint32_t *)sp_item;
                                head_flag60 = *(const volatile uint8_t *)(sp_item + 0x60u);
                            }
                        }
                    }
                }
            }
            g4usb_log("警告: queue_disp(0x01c1cf78) 滞留 tick=%u (0x06e230a8 q_cnt=%u head_sid=0x%x flag60=%u)",
                      ticks, q_cnt, head_sid, head_flag60);
        }
    }
    return rc;
}

/* 判断是否为需重点护航的 USB 启停序列 */
static inline int g4usb_is_usb_seq_id(uint32_t sid)
{
    return (sid == 0x297u || sid == 0x294u || sid == 0x298u);
}

/* 读取 seq_obj + 0xec/0xf0 的 12B 结构体 wait_list (stride=0xc，+8 直接为 conflict_seq_id) */
static inline uint32_t g4usb_read_wait_list(const void *seq_obj, uint32_t *out_cids, uint32_t max_cids)
{
    if (!seq_obj) {
        return 0u;
    }
    uintptr_t beg = *(const volatile uintptr_t *)((uintptr_t)seq_obj + 0xecu);
    uintptr_t end = *(const volatile uintptr_t *)((uintptr_t)seq_obj + 0xf0u);
    if (beg <= 0x10000u || end <= beg || (end - beg) > 0x1000u || ((end - beg) % 12u) != 0u) {
        return 0u;
    }
    uint32_t cnt = (uint32_t)((end - beg) / 12u);
    if (out_cids && max_cids > 0u) {
        uint32_t show = (cnt > max_cids) ? max_cids : cnt;
        for (uint32_t i = 0u; i < show; i++) {
            out_cids[i] = *(const volatile uint32_t *)(beg + (uintptr_t)i * 12u + 8u);
        }
    }
    return cnt;
}

/* appFw.so @ 0x02a0e4f0 -> 完整 5 参数 add_runq(mgr, seq_obj, seq_id, arg3, arg4) */
static int g4usb_hook_add_runq(void *mgr, void *seq_obj, uint32_t seq_id, uintptr_t arg3, uint32_t arg4)
{
    int rc = g4usb_orig_add_runq ? g4usb_orig_add_runq(mgr, seq_obj, seq_id, arg3, arg4) : 0;
    if (g4usb_is_usb_seq_id(seq_id)) {
        uint32_t cids[4] = { 0u, 0u, 0u, 0u };
        uint32_t wcnt = g4usb_read_wait_list(seq_obj, cids, 4u);
        g4usb_log("add_runq(0x02a0e4f0): seq_id=0x%x seq_obj=%p arg3=0x%lx arg4=%u -> wait_list_cnt=%u [0x%x,0x%x] rc=%d",
                  seq_id, seq_obj, (unsigned long)arg3, arg4, wcnt, cids[0], cids[1], rc);
    }
    return rc;
}

/* appFw.so @ 0x028747a8 -> wait_conflict(seq_obj)：拦截并解除 USB 序列的跨代冲突死等 */
static int g4usb_hook_wait_conflict(void *seq_obj)
{
    uint32_t sid = seq_obj ? *(const volatile uint32_t *)((uintptr_t)seq_obj + 4u) : 0u;
    if (!g4usb_is_usb_seq_id(sid)) {
        return g4usb_orig_wait_conflict ? g4usb_orig_wait_conflict(seq_obj) : 0;
    }

    uint32_t cids[4] = { 0u, 0u, 0u, 0u };
    uint32_t wcnt = g4usb_read_wait_list(seq_obj, cids, 4u);
    if (wcnt == 0u) {
        g4usb_log("wait_conflict(0x028747a8): seq_id=0x%x 无冲突等待，直接放行 run()", sid);
        return g4usb_orig_wait_conflict ? g4usb_orig_wait_conflict(seq_obj) : 0;
    }

    g4usb_log("wait_conflict(0x028747a8): 检测到 seq_id=0x%x 被 %u 个冲突序列阻塞 [0x%x,0x%x,0x%x,0x%x]，让出 80ms 后绕过 0x02c2d0b0 死等",
              sid, wcnt, cids[0], cids[1], cids[2], cids[3]);
    usleep(80000);
    return 0;
}

static int g4usb_hook_seq297_run(void *seq_obj)
{
    uint32_t cnt = __sync_add_and_fetch(&g_g4usb_seq297_run_cnt, 1u);
    g4usb_log(">>> seq_0x297::run(0x02aeac4c) ENTER #%u seq_obj=%p", cnt, seq_obj);
    int rc = g4usb_orig_seq297_run ? g4usb_orig_seq297_run(seq_obj) : 0;
    g4usb_log("<<< seq_0x297::run(0x02aeac4c) EXIT #%u rc=%d", cnt, rc);
    return rc;
}

static int g4usb_hook_seq294_run(void *seq_obj)
{
    uint32_t cnt = __sync_add_and_fetch(&g_g4usb_seq294_run_cnt, 1u);
    g4usb_log(">>> seq_0x294::run(0x02aeb894 MTP) ENTER #%u seq_obj=%p", cnt, seq_obj);
    int rc = g4usb_orig_seq294_run ? g4usb_orig_seq294_run(seq_obj) : 0;
    g4usb_log("<<< seq_0x294::run(0x02aeb894 MTP) EXIT #%u rc=%d", cnt, rc);
    return rc;
}

/* 替换 seq_dispatch_tbl[seq_id] 指针槽位的通用安全辅助函数（避免 2B 对齐跳板限制） */
static inline void *g4usb_patch_dispatch_slot(uintptr_t app_base,
                                              uint32_t seq_id,
                                              uintptr_t expected_fn_off,
                                              void *hook_fn,
                                              const char *name)
{
    uintptr_t slot_va = app_base + 0x0344e550u + (uintptr_t)seq_id * 4u;
    uintptr_t *slot = (uintptr_t *)slot_va;
    uintptr_t expected = app_base + expected_fn_off;
    if (*slot != expected) {
        g4usb_log("警告: %s slot(%p)=0x%lx != 期望 0x%lx，跳过槽位替换",
                  name, (void *)slot, (unsigned long)*slot, (unsigned long)expected);
        return NULL;
    }
    uintptr_t page = slot_va & ~4095u;
    if (mprotect((void *)page, 4096u * 2u, PROT_READ | PROT_WRITE) != 0) {
        g4usb_log("警告: %s slot(%p) mprotect 失败 errno=%d", name, (void *)slot, errno);
        return NULL;
    }
    void *orig = (void *)(*slot);
    *slot = (uintptr_t)hook_fn;
    __builtin___clear_cache((char *)slot, (char *)(slot + 1));
    g4usb_log("HOOK: %s slot(%p: 0x%lx -> %p) 替换成功",
              name, (void *)slot, (unsigned long)orig, hook_fn);
    return orig;
}

/* ----------------------------------------------------------------------------
 * 8. 通用 8B 前言校验并安装跳板的轻量辅助宏/函数
 * ---------------------------------------------------------------------------- */

static inline void *g4usb_install_tramp8_checked(uintptr_t addr,
                                                 const uint8_t sig[8],
                                                 void *hook_fn,
                                                 const char *name)
{
    if (memcmp((const void *)addr, sig, 8u) == 0) {
        return g4usb_make_thumb_trampoline_8(addr, hook_fn, name);
    }
    const uint8_t *b = (const uint8_t *)addr;
    g4usb_log("警告: %s (%p) 前言不匹配 (%02x %02x %02x %02x %02x %02x %02x %02x)，跳过跳板",
              name, (void *)addr, b[0], b[1], b[2], b[3], b[4], b[5], b[6], b[7]);
    return NULL;
}

/* ----------------------------------------------------------------------------
 * 9. 模块总入口：init_bridge_usb
 * ---------------------------------------------------------------------------- */

static inline void init_bridge_usb(uintptr_t app_base, uintptr_t usb_base)
{
    g4usb_log("=================================================");
    g4usb_log("启动 USB 模式与供电状态桥接层 (init_bridge_usb)");
    g4usb_log("=================================================");

    g_g4usb_app_base = app_base;

    /* 仅使用映射/RTLD_NOLOAD 探测已加载的 libUsbControl.so，禁止强行 dlopen 破坏 QEMU 环境 */
    if (!usb_base) {
        usb_base = g4usb_get_module_base("libUsbControl.so");
        if (!usb_base) {
            void *hUsb = dlopen("libUsbControl.so", RTLD_NOLOAD | RTLD_LAZY);
            if (hUsb) {
                usb_base = g4usb_get_module_base("libUsbControl.so");
            }
        }
    }
    g_g4usb_usb_base = usb_base;
    g_g4usb_ob5_base = g4usb_get_module_base("libOb5.so");
    g_g4usb_real_host_ready = (usb_base != 0u) ? 1 : 0;

    if (usb_base) {
        g4usb_log("检测到真机宿主 libUsbControl.so=0x%lx libOb5.so=0x%lx",
                  (unsigned long)usb_base, (unsigned long)g_g4usb_ob5_base);

        static const uint8_t sig_chk[8]      = { 0x90u, 0xb5u, 0x83u, 0xb0u, 0x04u, 0x46u, 0x00u, 0xafu };
        static const uint8_t sig_start[8]    = { 0xf0u, 0xb5u, 0x83u, 0xb0u, 0x00u, 0xafu, 0x14u, 0x46u };
        static const uint8_t sig_cfg_stop[8] = { 0xf0u, 0xb5u, 0x85u, 0xb0u, 0x00u, 0xafu, 0x14u, 0x46u };
        static const uint8_t sig_if_start[8] = { 0xb0u, 0xb5u, 0x84u, 0xb0u, 0x04u, 0x29u, 0x13u, 0x46u };
        static const uint8_t sig_jack[8]     = { 0x2du, 0xe9u, 0xf0u, 0x41u, 0x82u, 0xb0u, 0x00u, 0xafu };
        static const uint8_t sig_l2p[8]      = { 0xf8u, 0xb5u, 0x05u, 0x46u, 0xd0u, 0xf8u, 0xb8u, 0x40u };

        g4usb_orig_chkStartGadgetOK = (g4usb_chk_start_gadget_fn_t)g4usb_install_tramp8_checked(
            usb_base + 0x00010cc8u, sig_chk, (void *)g4usb_hook_chkStartGadgetOK, "libUsbControl:chkStartGadgetOK");

        g4usb_orig_actionGConfigureFunc = (g4usb_action_gadget_fn_t)g4usb_install_tramp8_checked(
            usb_base + 0x00011ee0u, sig_cfg_stop, (void *)g4usb_hook_actionGConfigureFunc, "libUsbControl:actionGConfigureFunc");

        g4usb_orig_actionGStopGadget = (g4usb_action_gadget_fn_t)g4usb_install_tramp8_checked(
            usb_base + 0x000120b0u, sig_cfg_stop, (void *)g4usb_hook_actionGStopGadget, "libUsbControl:actionGStopGadget");

        g4usb_orig_actionGStartGadget = (g4usb_action_gadget_fn_t)g4usb_install_tramp8_checked(
            usb_base + 0x000121d0u, sig_start, (void *)g4usb_hook_actionGStartGadget, "libUsbControl:actionGStartGadget");

        g4usb_orig_IfStartGadget = (g4usb_if_start_gadget_fn_t)g4usb_install_tramp8_checked(
            usb_base + 0x00012dc0u, sig_if_start, (void *)g4usb_hook_IfStartGadget, "libUsbControl:UsbIfWrapper::StartGadget");

        g4usb_orig_keyLocal2PhysCode = (g4usb_key_local2phys_fn_t)g4usb_install_tramp8_checked(
            usb_base + 0x00012b20u, sig_l2p, (void *)g4usb_hook_keyLocal2PhysCode, "libUsbControl:keyLocal2PhysCode");

        g4usb_orig_sendJackStatusToInput = (g4usb_send_jack_fn_t)g4usb_install_tramp8_checked(
            usb_base + 0x00012c6cu, sig_jack, (void *)g4usb_hook_sendJackStatusToInput, "libUsbControl:sendJackStatusToInput");

        /* 主动预解析四代 UsbControl 单例并探查初始 USB 插线状态（覆盖带线冷开机场景） */
        void *init_ctrl = g4usb_resolve_usb_ctrl_instance();
        int init_jack1 = g4usb_query_g4_jack_status(1u);
        int init_jack3 = g4usb_query_g4_jack_status(3u);
        g4usb_log("初始化探测 UsbControl 单例=%p: USB_JACK(1)=%d CHARGER(3)=%d",
                  init_ctrl, init_jack1, init_jack3);

        /* 安装 libOb5:InfraInput::GetPhyKeyStatus 与 GetPhyKeyStatusCache 校准跳板 */
        if (g_g4usb_ob5_base) {
            static const uint8_t sig_phy_key[8]       = { 0x2du, 0xe9u, 0xf0u, 0x43u, 0x95u, 0xb0u, 0x04u, 0xafu };
            static const uint8_t sig_phy_key_cache[8] = { 0xb0u, 0xb5u, 0x00u, 0xafu, 0x05u, 0x46u, 0x14u, 0x46u };
            g4usb_orig_ob5_GetPhyKeyStatus = (g4usb_ob5_get_phy_key_fn_t)g4usb_install_tramp8_checked(
                g_g4usb_ob5_base + 0x00207724u, sig_phy_key, (void *)g4usb_hook_ob5_GetPhyKeyStatus, "libOb5:GetPhyKeyStatus");
            g4usb_orig_ob5_GetPhyKeyStatusCache = (g4usb_ob5_get_phy_key_fn_t)g4usb_install_tramp8_checked(
                g_g4usb_ob5_base + 0x00208d30u, sig_phy_key_cache, (void *)g4usb_hook_ob5_GetPhyKeyStatusCache, "libOb5:GetPhyKeyStatusCache");
        }
    } else {
        g4usb_log("当前为非真机宿主模式 (usb_base=0)，GetMedia/UPM 将走 QEMU 安全路径");
    }

    /* 为五代 appFw.so MSC/媒体查询/UPM wrapper 及 seq 0x297/0x294/0x298 调度链安装 4B 对齐安全跳板 */
    if (app_base) {
        static const uint8_t sig_wrap[8]   = { 0xb0u, 0xb5u, 0x05u, 0x46u, 0x00u, 0xafu, 0x0cu, 0x46u };
        static const uint8_t sig_gis[8]    = { 0xf8u, 0xb5u, 0x06u, 0x46u, 0x00u, 0x23u, 0x00u, 0xafu };
        static const uint8_t sig_enq[8]    = { 0x2du, 0xe9u, 0xf0u, 0x41u, 0x84u, 0xb0u, 0x00u, 0xafu };
        static const uint8_t sig_qdisp[8]  = { 0x2du, 0xe9u, 0xf0u, 0x47u, 0x8cu, 0xb0u, 0x22u, 0x20u };
        static const uint8_t sig_runq[8]   = { 0x2du, 0xe9u, 0xf0u, 0x4fu, 0x8fu, 0xb0u, 0x02u, 0xafu };
        static const uint8_t sig_wait[8]   = { 0xb0u, 0xb5u, 0x00u, 0xf1u, 0xecu, 0x04u, 0x82u, 0xb0u };
        static const uint8_t sig_297run[8] = { 0x2du, 0xe9u, 0xf0u, 0x4fu, 0xf3u, 0xb0u, 0x04u, 0x46u };
        static const uint8_t sig_294run[8] = { 0x2du, 0xe9u, 0xf0u, 0x4fu, 0xadu, 0xf5u, 0x45u, 0x7du };

        g4usb_orig_appfw_get_input_status = (g4usb_appfw_get_input_status_fn_t)g4usb_install_tramp8_checked(
            app_base + 0x01c7a89cu, sig_gis, (void *)g4usb_hook_appfw_get_input_status, "appFw:get_input_status_01c7a89c");

        g4usb_orig_GetMediaIDByAttribute = (g4usb_media_by_attr_fn_t)g4usb_install_tramp8_checked(
            app_base + 0x02b1afc8u, sig_wrap, (void *)g4usb_hook_GetMediaIDByAttribute, "appFw:GetMediaIDByAttribute_02b1afc8");

        g4usb_orig_GetMediaMountInfo = (g4usb_media_mount_info_fn_t)g4usb_install_tramp8_checked(
            app_base + 0x02b1b19cu, sig_wrap, (void *)g4usb_hook_GetMediaMountInfo, "appFw:Cmd_11008_02b1b19c");

        g4usb_orig_FsysUnmount = (g4usb_msc_2arg_fn_t)g4usb_install_tramp8_checked(
            app_base + 0x02b1b1e4u, sig_wrap, (void *)g4usb_hook_FsysUnmount, "appFw:FsysUnmount_02b1b1e4");

        g4usb_orig_UpmSet = (g4usb_upm_set_fn_t)g4usb_install_tramp8_checked(
            app_base + 0x02b1e450u, sig_wrap, (void *)g4usb_hook_UpmSet, "appFw:UpmSet_02b1e450");

        g4usb_orig_MscStartTimer = (g4usb_upm_set_fn_t)g4usb_install_tramp8_checked(
            app_base + 0x02b1ea58u, sig_wrap, (void *)g4usb_hook_MscStartTimer, "appFw:MscStartTimer_02b1ea58");

        g4usb_orig_MscStopTimer = (g4usb_upm_set_fn_t)g4usb_install_tramp8_checked(
            app_base + 0x02b1ea7cu, sig_wrap, (void *)g4usb_hook_MscStopTimer, "appFw:MscStopTimer_02b1ea7c");

        g4usb_orig_MscImOpen = (g4usb_msc_2arg_fn_t)g4usb_install_tramp8_checked(
            app_base + 0x02b1eb10u, sig_wrap, (void *)g4usb_hook_MscImOpen, "appFw:MscImOpen_02b1eb10");

        g4usb_orig_MscImClose = (g4usb_msc_2arg_fn_t)g4usb_install_tramp8_checked(
            app_base + 0x02b1eb34u, sig_wrap, (void *)g4usb_hook_MscImClose, "appFw:MscImClose_02b1eb34");

        g4usb_orig_MscWrap6 = (g4usb_msc_2arg_fn_t)g4usb_install_tramp8_checked(
            app_base + 0x02b1eb58u, sig_wrap, (void *)g4usb_hook_MscWrap6, "appFw:MscWrap6_02b1eb58");

        g4usb_orig_MscConfigureFunc = (g4usb_msc_2arg_fn_t)g4usb_install_tramp8_checked(
            app_base + 0x02b1eb7cu, sig_wrap, (void *)g4usb_hook_MscConfigureFunc, "appFw:MscConfigureFunc_02b1eb7c");

        g4usb_orig_MscStartOrExtCmd = (g4usb_msc_2arg_fn_t)g4usb_install_tramp8_checked(
            app_base + 0x02b1eba0u, sig_wrap, (void *)g4usb_hook_MscStartOrExtCmd, "appFw:MscStartOrExtCmd_02b1eba0");

        g4usb_orig_MscWrap10 = (g4usb_msc_2arg_fn_t)g4usb_install_tramp8_checked(
            app_base + 0x02b1ecc0u, sig_wrap, (void *)g4usb_hook_MscWrap10, "appFw:MscWrap10_02b1ecc0");

        g4usb_orig_MscRegCb1 = (g4usb_msc_2arg_fn_t)g4usb_install_tramp8_checked(
            app_base + 0x02b1ed2cu, sig_wrap, (void *)g4usb_hook_MscRegCb1, "appFw:MscRegCb1_02b1ed2c");

        g4usb_orig_MscRegCb2 = (g4usb_msc_2arg_fn_t)g4usb_install_tramp8_checked(
            app_base + 0x02b1ed50u, sig_wrap, (void *)g4usb_hook_MscRegCb2, "appFw:MscRegCb2_02b1ed50");

        /* 全链路穿透：seq 0x297 / 0x294 入队 -> 主队列派发 -> thread[5] 冲突等待解除 -> run() */
        g4usb_orig_seq297_enq = (g4usb_seq_enq_fn_t)g4usb_install_tramp8_checked(
            app_base + 0x01c23aa4u, sig_enq, (void *)g4usb_hook_seq297_enq, "appFw:seq_0x297_enq_01c23aa4");

        g4usb_orig_seq294_enq = (g4usb_seq_enq_fn_t)g4usb_install_tramp8_checked(
            app_base + 0x01c23c00u, sig_enq, (void *)g4usb_hook_seq294_enq, "appFw:seq_0x294_enq_01c23c00");

        g4usb_orig_seq297_disp = (g4usb_seq_disp_fn_t)g4usb_patch_dispatch_slot(
            app_base, 0x297u, 0x01bebbcfu, (void *)g4usb_hook_seq297_disp, "appFw:seq_0x297_disp");

        g4usb_orig_seq294_disp = (g4usb_seq_disp_fn_t)g4usb_patch_dispatch_slot(
            app_base, 0x294u, 0x01bebc23u, (void *)g4usb_hook_seq294_disp, "appFw:seq_0x294_disp");

        g4usb_orig_seq298_disp = (g4usb_seq_disp_fn_t)g4usb_patch_dispatch_slot(
            app_base, 0x298u, 0x01bebbb3u, (void *)g4usb_hook_seq298_disp, "appFw:seq_0x298_disp");

        g4usb_orig_queue_disp = (g4usb_seq_0arg_fn_t)g4usb_install_tramp8_checked(
            app_base + 0x01c1cf78u, sig_qdisp, (void *)g4usb_hook_queue_disp, "appFw:queue_disp_01c1cf78");

        g4usb_orig_add_runq = (g4usb_add_runq_fn_t)g4usb_install_tramp8_checked(
            app_base + 0x02a0e4f0u, sig_runq, (void *)g4usb_hook_add_runq, "appFw:add_runq_02a0e4f0");

        g4usb_orig_wait_conflict = (g4usb_seq_1arg_fn_t)g4usb_install_tramp8_checked(
            app_base + 0x028747a8u, sig_wait, (void *)g4usb_hook_wait_conflict, "appFw:wait_conflict_028747a8");

        g4usb_orig_seq297_run = (g4usb_seq_1arg_fn_t)g4usb_install_tramp8_checked(
            app_base + 0x02aeac4cu, sig_297run, (void *)g4usb_hook_seq297_run, "appFw:seq_0x297_run_02aeac4c");

        g4usb_orig_seq294_run = (g4usb_seq_1arg_fn_t)g4usb_install_tramp8_checked(
            app_base + 0x02aeb894u, sig_294run, (void *)g4usb_hook_seq294_run, "appFw:seq_0x294_run_02aeb894");
    } else {
        g4usb_log("警告: app_base 为空，跳过 appFw.so MSC 桥接跳板");
    }

    g4usb_log("=================================================");
    g4usb_log("USB 模式与供电状态桥接层初始化完成 (real_host=%d)", g_g4usb_real_host_ready);
    g4usb_log("=================================================");
}

#ifdef __cplusplus
}
#endif

#endif /* G4HOST_BRIDGE_USB_H */
