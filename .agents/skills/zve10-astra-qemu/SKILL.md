---
name: zve10-astra-qemu
description: >-
  Build, run, debug, and verify the ZV-1M2 (Gen 5 ASTRA) new menu UI and touch/key
  interaction ported to Sony ZV-E10 in QEMU-only mode (`-M zve10`), covering
  `src/appFwBoot_g4host.c` compilation (`--target qemu`), `astra.cpio` initramfs
  packaging, software Morton RGBA4444 framebuffer sync, cold-boot menu routing,
  PL011 UART touch/key injection, and the Web QEMU UI.
---

# ZV-E10 移植 ZV-1M2 (Gen 5 ASTRA) 新菜单与触摸的 QEMU 实现

> **适用范围声明**：本 Skill **严格限定于 QEMU (`-M zve10`) 仿真环境**的编译、initramfs 打包、软件渲染、菜单直达、串口触摸/按键交互与 Web UI 验收，不包含任何物理设备刷机流程。

## 1. 核心架构概览

在 ZV-E10 (Gen 4) QEMU 仿真环境上运行 ZV-1M2 (Gen 5 ASTRA) 的 `appFw.so` 与 `libOb5.so` 需解决四类跨代与仿真环境差异：

1. **符号隔离与加载顺序 (`src/appFwBoot_g4host.c` -> `appFwBoot.so` + `libappFwBoot_g5.so`)**
   - `bootin.elf` 启动四代底座初始化（`Application_System_Init`），随后创建 **16MB 专属栈**的 `AstraAppFw` 线程，全局拦截 `__assert_fail` / `abort` 与致命信号，并停靠 `libObj.so` 的四代 RM 线程以免争抢渲染锁。
   - 使用 `tools/build/elf_samehash_rename.py` 生成的 `libappFwBoot_g5.so` 对与四代冲突的 12 个符号（如 `stella_print_log -> suDlla_print_log`、`UXEngine::gfxeng()` 等）做同长度哈希改名。
   - 按 `libMWF.so -> libOb5.so -> libmp5.so -> libObjShim.so -> AdjChkConfig.so -> libappFwBoot_g5.so -> appFw.so` 顺序以 `RTLD_LAZY | RTLD_GLOBAL` 加载，安全执行 `appFw.so` 的 2,730 个 `.init_array` 构造函数并进入 `_Z4MainPv(Application_Start)`。
2. **QEMU 硬件与后端缺口兜底**
   - 拦截 `std::condition_variable::wait`（`astra_cond_wait_hook`）：对 `model_future::get` 与 `model_setting_value::commit` 等待施加 2s 超时兜底（超时时清理状态标志 `appFw+0x07a41e4e`）。
   - 兜底 `osal_snd_sync_direct`（合成 `0x910042` 帧时间）、`GRM_fbrmCheckUpdateFinished`（QEMU 下强制放行返回 `0`）、`libOb5.so` 的 `mmap`（剥离 `0x18000` 并提供匿名页 + `pread` 兜底）以及 VDF DSP 协处理器等待。
3. **纯软件渲染与 Morton RGBA4444 显存同步**
   - Hook `libOb5.so` 的渲染与 GL 接口（`rm_present`, `sw_dmp_fill`, `sw_dmp_blit`, `gemini_solid/rect/quad`, `glBindTexture`, `glTexImage2D`, `glTexSubImage2D`, `glClear`, `s->vt[+0x10]` surface clear, `s->vt[+0x18]` swap buffers）。
   - `g5_clear_fb_rect` 严格遵循 `g5_get_scissor_clip`，防止拍摄 OSD（如 `NO CARD` 闪烁）局部重绘时误清整屏。
   - `g5_hook_swap_buffers` 调用 `g5_pack_fb_rgba4444_morton` 将画布编码为 **640×480 Morton Z-order RGBA4444** 并写入保留物理显存（`0x20000000`），向串口输出 `ASTRA_FB_SYNC` / `ASTRA_UI_READY`。
4. **菜单直达、运行时切屏与 PL011 串口触控/按键**
   - 在 `boot_info_bss`（默认 `0x1b` 会卡死）按内核参数 `astra_boot` 注入 `boot_key`（`menu -> 0x10` 直达菜单，`shooting -> 0x00`），并在非 `normal` 模式下跳过开机向导（`scr0039`）。
   - 宿主机 Web UI / 脚本通过 QEMU 串口 socket 发送 `TOUCH`、`KEY`、`OPEN_MENU`、`OPEN_SHOOT`、`PING` 指令；Guest 内 `uart_touch_rx_thread` 读取 `/dev/ttyAM0`，驱动 `libOb5.so` 的 `WidgetSystem`（`postMouseMove` / `postMousePress` / `postMouseRelease`）与 `appFw.so` 按键事件分发器，并通过 `THREAD_APP` 线程安全边界执行 `pcm::open_screen` 切屏。

详细偏移表、Hook 机制与数据结构请参见 [architecture-and-offsets.md](references/architecture-and-offsets.md)。

---

## 2. 关键文件索引

| 文件路径 | 职责 |
| :--- | :--- |
| `src/appFwBoot_g4host.c` | Gen4 宿主引导垫片、GOT/ARM/Thumb Hook、QEMU 软光栅、切屏、串口触摸与按键派发 |
| `tools/build/build_appfwboot.py` | 交叉编译脚手架（`--target qemu` 生成 `build_out/appFwBoot.so` 与 `build_out/libappFwBoot_g5.so`） |
| `tools/qemu/patch_imdb_qemu_fw.py` | 基于 37,204 B 版 `libIMDB` 禁用 QEMU 未模拟硬件模块，生成 `build_out/libIMDB_qemu_astra.so` |
| `tools/qemu/prepare_astra_qemu.sh` | 在 WSL/Linux 中基于 `initramfs-ui` 构建注入五代 `.so` 与 `/share/app` 资源的 `astra.cpio` |
| `tools/qemu/astra_web_qemu.py` | QEMU 进程守护、QMP `pmemsave` 抓帧、RGBA4444 C 解码、串口控制与 HTTP Web UI 服务 |
| `tools/qemu/start-astra-web.ps1` | Windows 一键启动 WSL Web QEMU 的入口脚本 |

前置要求：请先确认已通过 `tools/build/prepare_inputs.py` 准备好 `firmware/`，并已按 [docs/qemu-prerequisites.md](../../../docs/qemu-prerequisites.md) 部署 `qemu-system-arm`（`-M zve10`）、内核 `Image` 与 `initramfs-ui`。

---

## 3. 标准构建、打包与启动流程

### Step 1：生成前置补丁库与编译 QEMU 目标垫片（强制 GLIBC 2.4 与 EABI4）
若尚未生成五代改名闭包与四代去劫持/MWF 补丁，先执行构建前置步骤，再编译 `--target qemu`：

```powershell
# 前置四代/五代处理（首次运行一次即可）
uv run --with pyelftools python tools/build/patch_mwf_shadow.py
uv run --with pyelftools python tools/build/patch_libobj.py
uv run --with pyelftools python tools/build/p2_patch_soname.py
uv run --with pyelftools python tools/build/batch_rename_g5.py
uv run --with ziglang --with pyelftools python tools/build/build_libobjshim.py
uv run --with pyelftools python tools/build/generate_hybrid_imdb.py --variant fw
uv run --with pyelftools python tools/qemu/patch_imdb_qemu_fw.py

# 编译 QEMU 目标垫片
uv run --with ziglang --with pyelftools python tools/build/build_appfwboot.py --target qemu
```

### Step 2：构建 QEMU initramfs (`astra.cpio`)
在 WSL 中将 `build_out/` 核心库、软链接及 `firmware/gen5/share/app` 注入 `initramfs-ui` 副本并打包 `astra.cpio`（可用 `ZVE10_QEMU_WORK` 覆盖默认 `~/zve10-qemu/work`）：

```powershell
wsl -e bash tools/qemu/prepare_astra_qemu.sh
```

### Step 3：启动 QEMU Web UI 服务
方式 A（Windows 快捷脚本）：
```powershell
.\tools\qemu\start-astra-web.ps1 -Port 8765 -OpenBrowser
```

方式 B（直接调用 WSL 中的 Python 服务并指定启动模式）：
```powershell
wsl -e python3 tools/qemu/astra_web_qemu.py --port 8765 --boot-mode menu
```

支持的 `--boot-mode`：
- `menu`（默认）：冷启动直接进入 ZV-1M2 新菜单
- `shooting`：冷启动进入拍摄取景/OSD 界面
- `normal`：走标准引导向导路径

---

## 4. QEMU 启动配置与内核参数

`astra_web_qemu.py` 支持通过环境变量（`ZVE10_QEMU_BIN`、`ZVE10_KERNEL_IMAGE`、`ZVE10_ASTRA_INITRD`）或参数（`--qemu-bin`、`--kernel`、`--initrd`）覆盖，核心配置：
- **机器与显存**：`-M zve10 -cpu cortex-a9 -smp 1 -m 1024`，保留显存基址 `0x20000000`（`640×480` RGBA4444 Morton 格式）
- **通讯通道**：
  - QMP Socket：`/tmp/qmp-astra-web.sock`（执行 `pmemsave 0x20000000 <size> <dump_path>` 截取帧缓冲）
  - Serial Socket：`/tmp/serial-astra-web.sock`（连接 `/dev/ttyAM0`，双向传输日志与触摸/按键/切屏命令）
- **内核 `cmdline`**：
  ```text
  console=ttyAM0,115200n8 cfg.uart=1 mem=1G@0 memrsv=0x1A5E000@0x02E68000 memrsv=128M@0x20000000:uc rdinit=/init loglevel=4 web_ui=1 astra_boot=<menu|shooting|normal>
  ```

---

## 5. 串口与 HTTP 控制协议

### 串口文本指令（Host -> `/dev/ttyAM0`）
| 指令 | 含义与行为 |
| :--- | :--- |
| `PING` | 存活探测，Guest 回复对应心跳日志 |
| `OPEN_MENU` / `OPENMENU` | 通过 `THREAD_APP` 安全边界调用 `pcm::open_screen` 切换至菜单屏（`0x2ef`） |
| `OPEN_SHOOT` / `OPENSHOOT` | 安全切换至拍摄界面（`0x2ec`） |
| `TOUCH DOWN <x> <y>` | 注入触摸按下事件（640×480 坐标空间） |
| `TOUCH MOVE <x> <y>` | 注入触摸拖拽事件（队列对连续 `MOVE` 做最新值折叠消抖） |
| `TOUCH UP <x> <y>` | 注入触摸抬起事件 |
| `TOUCH TAP <x> <y>` | 注入一次完整点击序列 |
| `KEY <name>` | 经 `g5_tact_key_table` 派发物理按键事件（如 `UP`、`DOWN`、`ENTER`、`MENU` 等） |

### Web HTTP API（`astra_web_qemu.py`）
- `POST /api/touch`：接收画布交互坐标并转发 `TOUCH` 指令至串口 socket。
- `POST /api/key`：接收按键名称并转发 `KEY <name>` 至串口 socket。
- `POST /api/control`：用于重启/模式切换及发送 `OPEN_MENU`、`OPEN_SHOOT`、`PING` 等控制命令。

---

## 6. 调试与验收检查清单

修改 `src/appFwBoot_g4host.c` 或资源注入脚本后，按以下顺序验证 QEMU 行为：
1. **编译检查**：`build_appfwboot.py --target qemu` 零报错完成，确认未引入高于 `GLIBC_2.4` 的符号。
2. **启动就绪标志**：观察串口输出出现 `ASTRA_FB_SYNC` / `ASTRA_UI_READY`，且 `bootin.elf` 未触发 `Not support boot key` 卡死（确认 `boot_info[0]` 已按 `astra_boot` 改写）。
3. **首屏画面验证**：`--boot-mode menu` 下 QMP `pmemsave` 抓帧解码后应直接呈现 ZV-1M2 五代菜单，无半屏残影或局部清屏导致的整屏黑屏闪烁。
4. **触摸交互验证**：在 Web UI 点击/拖拽菜单项，串口收到 `TOUCH` 指令后菜单焦点与子页应即时刷新并同步显存。
5. **切屏与按键验证**：发送 `OPEN_SHOOT` 与 `OPEN_MENU` 能在拍摄界面的 OSD 与五代新菜单间无崩溃往返切换；发送 `KEY MENU` / `KEY UP` / `KEY DOWN` / `KEY ENTER` 响应正常。
