#ifndef G4HOST_BRIDGE_SCHED_H
#define G4HOST_BRIDGE_SCHED_H

/**
 * @file g4host_bridge_sched.h
 * @brief 模块 1：原生硬件 GPU 调度与冷启动健康复位模块 (v14_ui_smooth)
 *
 * 根因修复说明：
 * 1. libOb5.so @ 0x12ceb0 (rm_wait) 并非每帧一次的 VSYNC，而是 DMP GPU 单条异步
 *    渲染命令的同步等待原语（正常耗时 0~3 ms，每帧调用 3~4 次，切菜单/重绘连续调用 60~125 次）。
 * 2. 彻底废除在 rm_wait 上注入 usleep(16666) 的伪限流补丁，让 DMP GPU/GPE 队列
 *    100% 原生高速直通，消除 58ms 帧卡顿与 1.2~2.5s 冻屏。
 * 3. 冷启动安全计数保护：迁移至真实 drawFramebuffer 完成回调 g4sched_on_frame_drawn()，
 *    稳定绘制满 100 帧后原子单次覆写 FAILCNT_PATH 为 "0\n"。
 */

#include <stdint.h>
#include <stddef.h>
#include <unistd.h>
#include <fcntl.h>

#ifdef __cplusplus
extern "C" {
#endif

/* =========================================================================
 * 宿主环境基础工具声明与防御性适配
 * ========================================================================= */
#ifndef G4HOST_TOOL_DECLS
#define G4HOST_TOOL_DECLS
static void logf_(const char *fmt, ...);
static void patch_thumb_jump(void *target, void *replacement, const char *name);
static void *make_thumb_trampoline_8(uintptr_t va, void *hook_fn, const char *name);
#endif

/* 冷启动健康清零一次性原子标记 */
static volatile uint32_t g4sched_fail_cleared = 0u;

/**
 * @brief 冷启动安全计数保护：稳定渲染满 100 帧后，单次覆写容灾标记清除故障累积
 */
static inline void g4sched_clear_fail_v2_if_healthy(uint32_t frame_idx)
{
    if (frame_idx < 100u) {
        return;
    }
    /* 严格保证在进程生命周期内仅原子执行一次 */
    if (!__sync_bool_compare_and_swap(&g4sched_fail_cleared, 0u, 1u)) {
        return;
    }

#ifdef FAILCNT_PATH
    const char *fail_path = FAILCNT_PATH;
#else
    const char *fail_path = "/setting/astra/fail_v2";
#endif

    int success_cnt = 0;
    int fd = open(fail_path, O_WRONLY | O_CREAT | O_TRUNC, 0666);
    if (fd >= 0) {
        ssize_t nw = write(fd, "0\n", 2);
        close(fd);
        if (nw == 2) {
            success_cnt++;
        }
    }

    logf_("[G4SCHED] 稳定渲染满 100 帧，单次复位启动故障标记 (%s -> 0, ok=%d)",
          fail_path, success_cnt);
}

/**
 * @brief 真实 drawFramebuffer 完成回调：驱动冷启动健康计数清零
 *
 * @param frame_idx 累计真实完成绘制的帧索引（从 1 开始）
 */
static inline void g4sched_on_frame_drawn(uint32_t frame_idx)
{
    g4sched_clear_fail_v2_if_healthy(frame_idx);
}

/**
 * @brief 模块 1 统一入口：初始化原生硬件 GPU 调度状态
 *
 * @param ob5_base libOb5.so 的加载基地址
 */
static inline void init_bridge_sched(uintptr_t ob5_base)
{
    if (!ob5_base) {
        logf_("[G4SCHED_WARN] init_bridge_sched: 传入 ob5_base 为空，跳过初始化");
        return;
    }

    logf_("[G4SCHED] 原生硬件 GPU 调度模式就绪 (已解除 rm_wait 阻塞，由真实 drawFramebuffer 计数触发健康复位)");
}

#ifdef __cplusplus
}
#endif

#endif /* G4HOST_BRIDGE_SCHED_H */
