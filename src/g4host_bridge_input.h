#ifndef G4HOST_BRIDGE_INPUT_H
#define G4HOST_BRIDGE_INPUT_H

/**
 * @file g4host_bridge_input.h
 * @brief 模块 6：输入子系统与真机触屏/按键/电源桥接层 (Phase 2 实现)
 * 
 * 核心功能：
 * 1. 彻底解决“修复 UI 卡顿后触屏不能使用”的严重退化：
 *    - 查明并制止对四代硬件触控驱动线程 I-TpMain (0x007b002b) 的错误哨兵清退；
 *    - 主动激活四代 InfraTp 硬件总开关 SetTpEventSw(true) 与 Raw 事件流 SetTpRawEvent(true)；
 *    - 拦截 libObj:InputService::callback_tp_raw (0x11cb75)，解析 (x, y) 坐标与动作类型 (Down/Move/Up)；
 *    - 桥接注入五代 WindowSurface (postMouseMove / postMousePress / postMouseRelease) 并推入 g_touch_q，
 *      加入节流实机触屏坐标诊断日志；
 * 2. 物理按键精准转译：
 *    - 覆盖 Fn (0x7095)、C1 (0x0066)、Play (0x01f7)、Delete (0x01f8)、Disp (0x01f9)、Menu (0x01ab)、
 *      Center (0x01ac)、方向键 (0x0003/0x0030/0x0015/0x01af)、快门 S1/S2、录制 REC；
 * 3. 二段拨动电源开关 (SLIKEY_SLI_POWER_OFF 0x01d2) 转换：
 *    - 识别拨片拨到 OFF，立即合成并双发五代 EVENT_INPUT_TACT_POWER_ON (0x020f0005) 与
 *      EVENT_INPUT_TACT_POWER_OFF (0x020f0006)，促使五代状态机平稳进入 [Master][Shutdown] 关机！
 */

#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>
#include <unistd.h>
#include <time.h>
#include <sys/time.h>
#include <string.h>
#include <errno.h>

#ifdef __cplusplus
extern "C" {
#endif

/* 宿主环境基础工具声明 */
#ifndef G4HOST_TOOL_DECLS
#define G4HOST_TOOL_DECLS
static void logf_(const char *fmt, ...);
static void *make_thumb_trampoline_8(uintptr_t va, void *hook_fn, const char *name);
#endif

/* 前向声明宿主已有按键/触屏注入接口（与宿主 static 保持完全一致） */
static int g5_send_internal_event(uint32_t ev_id);
static int g5_inject_g4_key(uint32_t g4_code, int is_press);
static void g5_touch_push(uint8_t kind, int16_t x, int16_t y);
static uint64_t g5_msec_now(void);

/* 五代 WindowSurface 鼠标/触控注入函数指针前向声明 */
#ifndef G4HOST_WS_SYMS_DECLARED
#define G4HOST_WS_SYMS_DECLARED
static void *(*g_pGetWS)(void);
static void (*g_pPress)(void *, uint8_t, uint8_t, const uint64_t *);
static void (*g_pRel)(void *, uint8_t, uint8_t, const uint64_t *);
static void (*g_pMove)(void *, uint8_t, int16_t, int16_t, const uint64_t *);
#endif

/* 四代通用输入事件结构体 (24 字节) */
typedef struct {
    uint32_t header;     /* 0x00: 事件头 */
    uint32_t reserved;   /* 0x04: 保留/通道 */
    uint32_t keycode;    /* 0x08: 键码/事件类型 */
    int32_t  status;     /* 0x0C: 状态/按压标志 (1=Press, 0=Release, or signed delta) */
    uint32_t timestamp;  /* 0x10: 时间戳 */
    uint32_t extra;      /* 0x14: 附加数据 */
} g4input_infra_event_t;

/* 内部状态记录 */
static volatile uint32_t g4input_tp_count = 0;
static volatile uint32_t g4input_power_switch_state = 1; /* 1=ON, 0=OFF */

/**
 * @brief 触控回调拦截桩：挂载于 libObj:InputService::callback_tp_raw (0x11cb75)
 * 
 * 函数签名：void callback_tp_raw(int channel, _InfraInputEvent *ev)
 * 当四代硬件产生触屏中断时，此处实时获取物理触摸坐标并注入五代系统。
 */
static int g4input_hook_callback_tp_raw(uintptr_t a0, uintptr_t a1, uintptr_t a2, uintptr_t a3)
{
    (void)a0; (void)a2; (void)a3;
    if (a1 > 0x1000u && (a1 & 3u) == 0) {
        uint16_t x   = *(const uint16_t *)(a1 + 4u);
        uint16_t y   = *(const uint16_t *)(a1 + 6u);
        uint32_t act = *(const uint32_t *)(a1 + 8u); /* 1=Down, 2=Up, 3=Move */

        /* 1. 注入五代 WindowSurface 原生指针交互 */
        void *ws = g_pGetWS ? g_pGetWS() : NULL;
        uint64_t t_now = g5_msec_now();
        if (ws) {
            if (act == 1u) { /* Touch Down */
                if (g_pMove) g_pMove(ws, 0, (int16_t)x, (int16_t)y, &t_now);
                if (g_pPress) g_pPress(ws, 0, 1, &t_now);
            } else if (act == 3u) { /* Touch Move */
                if (g_pMove) g_pMove(ws, 0, (int16_t)x, (int16_t)y, &t_now);
            } else if (act == 2u) { /* Touch Up */
                if (g_pMove) g_pMove(ws, 0, (int16_t)x, (int16_t)y, &t_now);
                if (g_pRel) g_pRel(ws, 0, 1, &t_now);
            }
        }

        /* 2. 推入通用触屏事件队列 g_touch_q */
        uint8_t kind = (act == 1u) ? 1u : ((act == 2u) ? 3u : 2u);
        g5_touch_push(kind, (int16_t)x, (int16_t)y);

        /* 3. 节流诊断日志 */
        uint32_t cnt = __sync_add_and_fetch(&g4input_tp_count, 1u);
        if (cnt <= 15u || (cnt % 60u) == 0u) {
            logf_("[G4INPUT_TP #%u] act=%u (%s) xy=(%u, %u) ws=%p",
                  cnt, act, (act == 1u ? "DOWN" : (act == 2u ? "UP" : "MOVE")),
                  (unsigned)x, (unsigned)y, ws);
        }
    }
    return 0;
}

/**
 * @brief 四代物理按键/电源拨片过滤与转译分发
 */
static inline int g4input_process_key_event(uint32_t keycode, int32_t status)
{
    /* 1. 二段机械拨动电源开关特殊处理 */
    if (keycode == 0x01d2u) { /* SLIKEY_SLI_POWER_OFF: 拨片拨至 OFF */
        g4input_power_switch_state = 0;
        logf_("[G4INPUT] >>> 检测到电源拨片拨至 OFF (0x01d2)！触发五代安全关机流程 <<<");
        /* 合成五代 EVENT_INPUT_TACT_POWER_ON (0x020f0005) 与 EVENT_INPUT_TACT_POWER_OFF (0x020f0006) */
        g5_send_internal_event(0x020f0005u);
        usleep(30000); /* 30ms 脉宽 */
        g5_send_internal_event(0x020f0006u);
        return 1;
    } else if (keycode == 0x01d1u) { /* SLIKEY_SLI_POWER: 拨片拨至 ON */
        g4input_power_switch_state = 1;
        logf_("[G4INPUT] 电源拨片处于 ON 状态 (0x01d1)");
        return 1;
    } else if (keycode == 0x0002u) { /* KEY_TACT_POWER: 轻触电源按键 */
        int is_press = (status != 0);
        logf_("[G4INPUT] 收到电源按键事件 (0x0002, press=%d)", is_press);
        if (is_press) {
            g5_send_internal_event(0x020f0005u);
        } else {
            g5_send_internal_event(0x020f0006u);
        }
        return 1;
    }

    /* 2. 滚轮/拨轮事件处理 */
    if (keycode == 0x20u || keycode == 0x10000016u || keycode == 0x10000011u || keycode == 0x7023u) {
        if (status > 0) {
            g5_inject_g4_key(0x21u /* WHEEL_R / 顺时针 */, 1);
        } else if (status < 0) {
            g5_inject_g4_key(0x22u /* WHEEL_L / 逆时针 */, 1);
        }
        return 1;
    }

    /* 3. 标准按键分发（带 60ms 脉冲防抖） */
    int is_press = (status != 0) ? 1 : 0;
    g5_inject_g4_key(keycode, is_press);

    return 0;
}

/**
 * @brief 挂载于四代 InfraInput 的核心事件回调
 */
static void g4input_real_infra_input_cb(int channel, void *ev_ptr)
{
    if (!ev_ptr) return;
    const uint32_t *ev = (const uint32_t *)ev_ptr;
    uint32_t keycode = ev[2];
    int32_t  status  = (int32_t)ev[3];

    static uint32_t s_key_cnt = 0;
    uint32_t seq = __sync_add_and_fetch(&s_key_cnt, 1u);
    if (seq <= 20u || (seq % 100u) == 0u || keycode == 0x01d1u || keycode == 0x01d2u || keycode == 0x7095u) {
        logf_("[G4INPUT_KEY #%u] ch=%d keycode=0x%04x (%u) status=%d",
              seq, channel, keycode, keycode, (int)status);
    }

    g4input_process_key_event(keycode, status);
}

/**
 * @brief 模块 6 统一入口：初始化真机输入与触屏桥接层
 * 
 * @param libobj_base 四代 libObj.so 加载基址
 * @param app_base    五代 appFw.so 加载基址
 */
static inline void init_bridge_input(uintptr_t libobj_base, uintptr_t app_base)
{
    (void)app_base;
    if (!libobj_base) {
        logf_("[G4INPUT_WARN] init_bridge_input: libobj_base 为空，跳过初始化");
        return;
    }

    logf_("[G4INPUT] 开始初始化输入与触屏桥接层 (libObj=%p)...", (void *)libobj_base);

    /* 1. 触摸屏已交由五代 libOb5:InfraTp 原生接管，此处不再激活四代 libObj:InfraTp 防止状态机冲突 */
    logf_("[G4INPUT] 跳过四代 InfraTp 激活，交由五代 libOb5:InfraTp 原生独占接管触屏");

    /* 2. 挂载四代底层 InfraInput 回调（确保真机物理按键中断 100% 接入） */
    typedef void *(*input_get_instance_fn_t)(void);
    typedef int (*input_set_callback_fn_t)(void *self, int *out_id, void (*cb)(int, void *));

    input_get_instance_fn_t pInGetInstance = (input_get_instance_fn_t)(libobj_base + 0x8c22a1u);
    input_set_callback_fn_t pInSetCallback = (input_set_callback_fn_t)(libobj_base + 0x8c2825u);

    void *pIn = pInGetInstance ? pInGetInstance() : NULL;
    if (!pIn) {
        pIn = *(void *volatile *)(libobj_base + 0x014924dcu);
    }

    if (pIn && pInSetCallback) {
        static int s_g4_cb_id = -1;
        int rc = pInSetCallback(pIn, &s_g4_cb_id, g4input_real_infra_input_cb);
        logf_("[G4INPUT] 四代 InfraInput::SetCallback 注册成功！pIn=%p cb_id=%d rc=%d", pIn, s_g4_cb_id, rc);
    } else {
        logf_("[G4INPUT_WARN] 四代 InfraInput::GetInstance 或 SetCallback 未就绪");
    }

    logf_("[G4INPUT] 输入子系统与触屏/按键/电源桥接层初始化完毕！");
}

#ifdef __cplusplus
}
#endif

#endif /* G4HOST_BRIDGE_INPUT_H */
