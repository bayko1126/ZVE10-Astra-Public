# ZV-E10 QEMU 移植 ZV-1M2 (Gen 5 ASTRA) 技术参考与偏移表

本文档记录 `src/appFwBoot_g4host.c`、`tools/build/build_appfwboot.py`、`tools/qemu/prepare_astra_qemu.sh` 及 `tools/qemu/astra_web_qemu.py` 中 **仅适用于 QEMU (`-M zve10`)** 的精确符号、偏移量、线程模型与渲染/触摸链路细节。

---

## 1. 编译工具链与 ELF 约束 (`tools/build/build_appfwboot.py`)

- **目标参数**：`--target qemu`
- **编译器目标**：Zig Clang `-target arm-linux-gnueabi.2.4`
- **版本限制**：`MAX_GLIBC = (2, 4)`，严禁引用高版本 glibc 符号，以匹配 QEMU 根文件系统中 Gen4 宿主的 `/lib/libc.so.6` ABI。
- **ELF 头标志**：`e_flags -> 0x05000002`（ARM EABI5 soft-float）。
- **输出与符号去重**：
  1. 编译输出 `build_out/appFwBoot.so`
  2. 通过 `tools/build/elf_samehash_rename.py` 处理符号冲突，输出 `build_out/libappFwBoot_g5.so`（含 12 个重命名符号，例如 `stella_print_log -> suDlla_print_log`、`UXEngine::gfxeng()` 等）。

---

## 2. `astra.cpio` 注入清单与 `/init` 启动链 (`tools/qemu/prepare_astra_qemu.sh`)

- **工作基线**：从 `$ZVE10_QEMU_WORK/initramfs-ui`（默认 `~/zve10-qemu/work/initramfs-ui`）复制到 `initramfs-astra` 后打包 `astra.cpio`。
- **注入 `/usr/lib/` 动态库（10 个 `.so` + 2 个符号链接）**：
  - `.so`：`libObj.so`、`libMWF.so`、`libIMDB.so`（来源 `libIMDB_qemu_astra.so`）、`appFwBoot.so`、`libappFwBoot_g5.so`、`libOb5.so`、`libmp5.so`、`libObjShim.so`、`AdjChkConfig.so`、`appFw.so`
  - 符号链接：`libuxengine.so -> libOb5.so`、`libgrm.so -> libOb5.so`
- **清理与资源**：删除 `viewUnified*.so`、`Caution*Config.so`、`ModelCautionConfig.so`；从 `firmware/gen5/share/app` 写入 `/usr/share/app` 界面资源文件。
- **内核模块加载顺序 (`/init`)**：
  1. `devno.ko`、`dmac.ko`、`osal_utm.ko`、`osal_uipc.ko`、`dma330_stub.ko`
  2. `backup.ko Backup_load=1 Backup_save=0`、`utimer.ko`、`osal_ulogio.ko ulogio_initfile=/setting/ulogio.bin`
  3. `kikilog.ko`、`stream.ko`、`stream2.ko`、`dmm.ko DmmConfig=/setting/DmmConfig.bin`、`grm_ma.ko`、`grm_gles.ko`
- **字符设备节点**：`uipc (251, 207)`、`backup (251, 208)`、`dmpgles2 (248, 29)`、`stream (249, 7)`、`stream2 (249, 8)`、`ldec (251, 205)`
- **用户态拉起**：`( ulimit -s 1024 ...; /usr/bin/bootin.elf 'target=qemu ' dmode=3 amode=normal ) &`

---

## 3. Gen4 宿主底盘初始化与五代隔离引导

1. **线程与异常免疫**：
   - `Application_System_Init` 创建 **16MB 栈**的 `AstraAppFw` 线程，规避 Gen5 `appFw.so` 深层调用链下的默认 1MB 栈溢出。
   - 安装全局 `__assert_fail` / `abort` 免疫及 `crash_handler`。
2. **Gen4 `libObj.so` 稳定化**：
   - `init_libobj_context_stub()` 写入 `libObj+0x148a6bc` 并调用 `SetContextGetter`（`libObj+0x854671`）。
   - `park_libobj_rm_thread` Hook `libObj+0x775121`（`rm_wait`）与 `libObj+0x7755b1`（`rm_wait_sig`），令四代 RM 线程交出 `mlock(2)` 并休眠。
3. **依赖串行加载与入口**：
   - 按 `libMWF.so`（初始化 `RsrcDP::Initialize`、`RsrcRelation::Initialize`）-> `libOb5.so` -> `libmp5.so` -> `libObjShim.so` -> `AdjChkConfig.so` -> `libappFwBoot_g5.so` -> `appFw.so` 顺序调用 `dlopen(..., RTLD_LAZY | RTLD_GLOBAL)`，完成 2,730 个 `.init_array` 静态构造。
   - 随后通过 `libappFwBoot_g5.so` 中的 `_Z4MainPv(Application_Start)` 启动五代应用主循环。

---

## 4. `appFw.so` 与 `libOb5.so` 精确 Hook 偏移表

### 4.1 `appFw.so` GOT 与函数偏移

| 偏移 | 目标符号 / 位置 | QEMU 下的 Hook 逻辑 |
| :--- | :--- | :--- |
| `0x0377b408` (GOT) | `__assert_fail` | 免疫非致命断言，阻止进程中止 |
| `0x0377ae50` (GOT) | `std::condition_variable::wait` | `astra_cond_wait_hook`：识别来自 `0x25c9590..0x25c97a0`（`model_future::get`）与 `0x277b808..0x277b8e0`（`model_setting_value::commit`）的阻塞等待，施加 2s 超时放行；超时时清零 `appFw+0x07a41e4e` |
| `0x0377ad5c` (GOT) | `osal_snd_sync_direct` | `g5_hook_osal_snd_sync_direct`：在 QEMU 无真实音频/编码同步返回时兜底 `0x910042` 帧时间 |
| `0x0377b270` (GOT) | `GRM_fbrmCheckUpdateFinished` | `g5_hook_grm_check_update_finished`：QEMU 下若原返回非 `0`，强制放行返回 `0` |
| `0x0377afe0` (GOT) | `GraphicsEngine::drawFramebuffer` | 对接 QEMU 帧缓冲绘制逻辑 |
| `0x02bc3174` | `notify_model_system_error` | Thumb/ARM 跳板直返（`bx lr`），吞掉 QEMU 缺硬件报错 |
| `0x02ce5f24` | `dbglog_sink` | 重定向/捕获应用内部调试日志 |
| `0x02c2af70` | `seqlog_send` | 捕获或过滤序列日志 |
| `0x03c8fb4` / `0x03c8fc0` 等 | VDF IF 协处理器信号量 | `install_g5_vdf_if_hooks` 跳过 DSP 协处理器阻塞等待 |

### 4.2 `libOb5.so` GOT 与图形 Hook 偏移

| 偏移 | 目标符号 / 位置 | QEMU 下的 Hook 逻辑 |
| :--- | :--- | :--- |
| `0x003553f4` (GOT) | `mmap` | `g5_hook_ob5_mmap`：剥离 `0x18000` 并为设备映射提供匿名内存页 + `pread` 兜底 |
| `0x0035548c` (GOT) | `open` | 拦截 `/dev/dmpgles2` 等图形设备打开 |
| `0x003553a0` (GOT) | `ioctl` | 仿真图形驱动 ioctl 应答 |
| `0x003552a4` (GOT) | `close` | 拦截模拟 fd 关闭 |
| `0x12cc80` | `rm_present` | 截获渲染提交并同步显存 |
| `0x12ceb0` | `rm_wait` | 仿真垂直同步/渲染完成等待 |
| `0x182410` | `sw_dmp_fill` | 软件填充光栅加速 |
| `0x1824a0` | `sw_dmp_blit` | 软件位块传输光栅 |
| `0x124bb4` | `update_records` | 追踪渲染记录 |
| `0x19b7e0` / `0x19b31c` / `0x19b034` | `gemini_solid` / `gemini_rect` / `gemini_quad` | 2D 几何图元软件绘制 |
| `0x16d9a0` | `glBindTexture` | 影子纹理状态追踪（动态解析 `libOb5` 基址后安装，下同） |
| `0x168780` / `0x1682f0` | `glTexImage2D` / `glTexSubImage2D` | 影子纹理像素数据上载与局部更新 |
| `0x16d400` | `glClear` | `g5_hook_gl_clear`：结合裁剪区清屏 |
| `0x19ecf8` | `s->vt[+0x10]` (`surface_clear`) | `g5_hook_surface_clear`：调用 `g5_clear_fb_rect` |
| `0x196504` | `s->vt[+0x18]` (`swap_buffers`) | `g5_hook_swap_buffers`：执行 Morton RGBA4444 打包并同步物理显存 |

### 4.3 渲染裁剪与 Morton 显存打包关键机制

- **Scissor 裁剪清屏保护**：`g5_clear_fb_rect` 在清屏前调用 `g5_get_scissor_clip`，确保局部重绘（例如拍摄界面 `NO CARD` 图标闪烁重绘）仅清理裁剪矩形内部，避免把整张菜单/OSD 背景刷黑。
- **Morton 显存输出**：`g5_hook_swap_buffers` 调用 `g5_pack_fb_rgba4444_morton`，将 `640×480` 软件渲染画布交错编码为 Morton (Z-order) RGBA4444 写回 `G5_MAIN_PRESENT_SRC_PHYS` 并同步至保留物理显存基址 `0x20000000`，同时输出串口同步标记 `ASTRA_FB_SYNC` / `ASTRA_UI_READY`。宿主机 `astra_web_qemu.py` 通过 QMP `pmemsave` 读取 `0x20000000` 并用内嵌 C 解码器还原 PNG 显示在 Web UI。

---

## 5. 新菜单冷启动直达与运行时切屏 (`g5_install_menu_boot_hooks`)

### 5.1 关键屏幕 ID 与偏移

- **屏幕 ID**：
  - `ASTRA_SCR_INVALID = 0x3f6`
  - `ASTRA_SCR_BOOT_CTRL = 0x2f0`
  - `ASTRA_SCR_SHOOTING = 0x2ec`
  - `ASTRA_SCR_MENU_CTRL = 0x2ef`
  - `ASTRA_SCR_MENU_SUB = 0x300`
  - `ASTRA_SCR_MENU_UI = 0x27f`（运行时可见子屏 ID 如 `0x280`、`0x281`）
- **`appFw.so` 偏移**：
  - `APPFW_OFF_PCM_OPEN_SCREEN = 0x0167b514`（`pcm::open_screen`，**注意：必须以 `pcm` 原函数指针/原型直接调用**）
  - `APPFW_OFF_PCM_GET_TOP_SCR = 0x0165d9f6`
  - `APPFW_OFF_SCR0039_OPEN = 0x0184e4e4`
  - `APPFW_OFF_REP_BOOT_INIT = 0x017f95a8`
  - `APPFW_OFF_REP_BOOT_CAN_OPEN = 0x017f91b0`
  - `APPFW_OFF_APP_GET_NEXT_EVT = 0x0054f548`
  - `APPFW_OFF_BOOT_INFO_BSS = 0x0742d18c`

### 5.2 冷启与动态切屏状态机

1. **避免 `Not support boot key` 卡死**：QEMU 默认 `boot_info[0]` 为 `0x1b`，会直接导致启动引导报错挂起。`g5_hook_rep_boot_init` 根据内核参数 `astra_boot` 改写 `boot_info[0]`：
   - `menu` -> `0x10`（直达 `rep_menu` 进入五代新菜单）
   - `shooting` -> `0x00`（直达拍摄取景屏 `0x2ec`）
2. **跳过开机设置向导**：在 `astra_boot != normal` 时，`g5_hook_scr0039_open` 跳过语言/时钟向导并直入 `0x2f0`；若 `menu` 模式下意外切向向导屏，`g5_hook_pcm_open_screen` 会触发 `BOOT_MENU_FALLBACK` 回落至菜单控制屏。
3. **线程安全切屏 (`OPEN_MENU` / `OPEN_SHOOT`)**：
   - 串口线程收到 `OPEN_MENU`/`OPENMENU` 或 `OPEN_SHOOT`/`OPENSHOOT` 后，不直接跨线程操作 UI 状态机，而是设置 `g_pending_switch_target` 并调用 `g5_send_internal_event(ASTRA_WAKE_EVT_ID = 0x5a564551)` 唤醒主应用线程 `THREAD_APP`。
   - `THREAD_APP` 在 `g5_hook_app_get_next_evt` 安全点消费挂起切换目标，调用 `g_orig_pcm_open_screen(pcm, &cur, &next, &opt, 0, 1, 0, 0, 0)` 完成切屏。
