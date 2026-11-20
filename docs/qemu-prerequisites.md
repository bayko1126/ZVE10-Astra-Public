# QEMU 仿真前置环境搭建指南 (`docs/qemu-prerequisites.md`)

本仓库的 `tools/qemu/` 脚本负责将改造后的五代 ASTRA 组件打包进 `astra.cpio` 并提供浏览器触控交互后端；而底层 **`qemu-system-arm`（支持 `-M zve10` 机器模型）**、**Sony ARM Linux 内核 `Image`** 以及 **基础 `initramfs-ui`** 属于外部前置环境，需按本指南提前准备。

---

## 1. 上游依赖与下载清单

| 资源名称 | 官方 / 上游来源 | 建议本地存放路径 |
| :--- | :--- | :--- |
| **Sony Linux 内核源码** | [linux-kernel.tar.gz](https://prodgpl.blob.core.windows.net/download/DI/ZV-E10_Ver.2.02/linux-kernel.tar.gz) | `work/linux-kernel.tar.gz` |
| **QEMU 11.1.2 源码** | [qemu-11.1.2.tar.xz](https://download.qemu.org/qemu-11.1.2.tar.xz) | `work/qemu-11.1.2.tar.xz` |
| **ARM 交叉工具链** | [Bootlin armv7-eabihf，2020.08-1](https://toolchains.bootlin.com/downloads/releases/toolchains/armv7-eabihf/tarballs/armv7-eabihf--glibc--stable-2020.08-1.tar.bz2) | `work/armv7-toolchain.tar.bz2` |
| **ZV-E10 2.03 固件包** | [Sony 官方下载页面](https://www.sony.com/electronics/support/e-mount-body-zv-e-series/zv-e10/downloads) | `Update_ZVE10V203.dmg` |
| **ZV-E10 QEMU 主仓库** | [chocotan/zve10-qemu](https://github.com/chocotan/zve10-qemu) | 提供 `-M zve10` QEMU 机器模型、构建脚本与基础 `initramfs-ui` |
| **QEMU 机器模型参考** | [chocotan/a7c2-qemu](https://github.com/chocotan/a7c2-qemu) | 参考其中的 Sony SoC / QEMU 外设补丁与 initramfs 构建思路 |
| **逆向与固件研究参考** | [DavidBuchanan314/ILCE-7M4-RE](https://github.com/DavidBuchanan314/ILCE-7M4-RE)<br>[erlla/sony-a7m4-custom-firmware](https://github.com/erlla/sony-a7m4-custom-firmware) | 用于固件提取、解包与内核/模块研究 |

---

## 2. 推荐目录结构与环境变量

`tools/qemu/prepare_astra_qemu.sh` 和 `tools/qemu/astra_web_qemu.py` 默认查找 WSL/Linux 下的 `~/zve10-qemu/work/`，结构如下：

```text
~/zve10-qemu/work/
├── qemu-src/build-zve10/qemu-system-arm    # 支持 -M zve10 的 QEMU ARM 模拟器
├── kbuild-ui/arch/arm/boot/Image           # 编译好的 ZV-E10 ARM Linux 内核
├── initramfs-ui/                           # 四代基础根文件系统目录（含 /kmod, /usr/kmod 等）
└── astra.cpio                              # 由 prepare_astra_qemu.sh 自动生成
```

如果你的目录不在默认路径，可以通过以下环境变量随时覆盖（无需修改脚本源码）：

| 环境变量 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `ZVE10_QEMU_WORK` | `~/zve10-qemu/work` | QEMU 工作根路径 |
| `ZVE10_QEMU_BIN` | `$ZVE10_QEMU_WORK/qemu-src/build-zve10/qemu-system-arm` | `qemu-system-arm` 可执行文件路径 |
| `ZVE10_KERNEL_IMAGE` | `$ZVE10_QEMU_WORK/kbuild-ui/arch/arm/boot/Image` | ARM 内核 `Image` 路径 |
| `ZVE10_ASTRA_INITRD` | `$ZVE10_QEMU_WORK/astra.cpio` | 打包后的 `astra.cpio` 路径 |
| `ZVE10_BUILD_OUT` | `<repo>/build_out` | 本地交叉编译产物目录 |
| `ZVE10_G5_SHARE` | `<repo>/firmware/gen5/share/app` | 五代 `share/app` UI 资源目录 |

---

## 3. QEMU 启动参数与宿主机依赖

- **WSL / Linux 依赖**：需要安装 `cpio`、`fakeroot`（可选）与 Python `Pillow`（`PIL`），若已安装 `gcc` 则会自动在 `/tmp` 编译 C 版 RGBA4444 解码加速库。
- **核心模拟硬件参数**：
  - 机器配置：`-M zve10 -cpu cortex-a9 -smp 1 -m 1024`
  - 显存保留段：`memrsv=128M@0x20000000:uc`（用于 Guest 软光栅输出 640×480 Morton RGBA4444 帧缓冲，宿主机通过 QMP `pmemsave` 抓帧）
  - 串口控制：`/dev/ttyAM0` 绑定 `/tmp/serial-astra-web.sock`，用于向 Guest 注入 `TOUCH`、`KEY`、`OPEN_MENU`、`OPEN_SHOOT`、`PING`。
