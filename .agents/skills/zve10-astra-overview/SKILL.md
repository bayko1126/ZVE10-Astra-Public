---
name: zve10-astra-overview
description: >-
  Entry-point overview and context router for the ZVE10-ASTRA-Port repository
  (porting Sony ZV-1M2 Gen 5 ASTRA menu UI and touch interaction to Sony ZV-E10 Gen 4).
  Use when onboarding to the repository, checking architecture boundaries, preparing
  user-supplied firmware inputs, or routing tasks to QEMU, hardware flashing, or
  reverse-engineering methodology sub-skills.
---

# ZV-E10 ASTRA Port 项目总览与任务路由

本 Skill 作为整个开源移植仓库的全局上下文入口，帮助 Agent 快速理解目录边界、四大不可违背的工程公理，并将具体任务精准分派给对应专职 Skill。

---

## 1. 仓库结构与职责边界

```text
.
├── src/                  # 原创 C / ARM 汇编源码（appFwBoot_g4host.c, libObjShim.S 等）
├── manifests/            # 纯元数据 JSON（557 符号重命名映射表、镜像删改清单）
├── tools/
│   ├── build/            # 提取输入 (prepare_inputs.py)、补丁、交叉编译与 ext2 镜像构建
│   ├── qemu/             # QEMU (-M zve10) initramfs 打包与 Web 触控交互服务
│   └── flash/            # Fastboot 差量刷写、WBI1 修复、USB 沙盒 camtool 与救砖工具
├── docs/                 # 架构图、方法论、固件提取与 QEMU 前置搭建指南
├── firmware/             # [Git 已忽略] 复现者本地自备原厂基线 (backup/, gen4/, gen5/)
└── build_out/            # [Git 已忽略] 编译与打补丁后的输出产物
```

> **版权红线**：严禁向 Git 提交 `firmware/`、`build_out/` 或任何索尼专有 `.bin` / `.so` / `.elf` / `.cpio` 文件（仅 `tools/flash/` 下 3 个非索尼辅助载荷除外）。

---

## 2. 四大核心工程公理（严禁回退）

1. **16MB 专属栈隔离**：`appFw.so` 含 2,730 个 `.init_array` 构造函数（最大单帧 3.28MB）。只能在 `Application_System_Init` 中为 `AstraAppFw` **单一线程**分配 16MB 栈并立即返回 `0`；严禁全局调大 `RLIMIT_STACK`（`im.elf` 有 188 个线程，会撑爆 32-bit VMA）。
2. **双保险消除强符号劫持**：`libIMDB.so` 将 `Application_System_Init` 重定向至 `appFwBoot.so`（`me_psid=0x00000083`），且四代 `libObj.so` 必须经 `patch_libobj.py` 原地改名为 `Application_G4Old_Init`。
3. **`libIMDB.so` `psid=0x07fb` 语义**：`0x07fb` 表示“内聚于四代 `libObj.so`”。禁用模块必须清零 `+0x08` 处 `mode`，不得乱改 `psid`；注意区分 37,044 B（`dump`）与 37,204 B（`fw`）两版偏移。
4. **GNU Hash 等长同哈希符号隔离**：通过 `elf_samehash_rename.py` 与 `manifests/rename_plan.json` 对 557 个冲突符号原地改名（保持长度与 `.gnu.hash` 严格不变），根治 `fault=0x234` 跨代虚表崩溃。

详细架构图请查阅：[architecture-diagrams.md](references/architecture-diagrams.md) 与 [docs/architecture.md](../../../docs/architecture.md)。

---

## 3. 前置初始化检查（首次使用必看）

在执行任何编译、QEMU 或真机构建前，先确认本地输入文件是否已提取至 `firmware/`：
```powershell
uv run python tools/build/prepare_inputs.py `
  --gen4-image firmware/backup/nflasha15.bin `
  --gen5-root <你的_ZV1M2_解包根目录>
```

---

## 4. 任务路由表

| 用户诉求 | 应激活的 Skill |
| :--- | :--- |
| 在无真机情况下编译垫片、打包 `astra.cpio`、启动 Web QEMU、调试软件渲染或串口触控/按键 | **`zve10-astra-qemu`** |
| 运行真机 USB `/log/g5/` 沙盒验证、构建 300MB 生产 ext2 镜像、差量刷机、修 WBI1 或救砖 | **`zve10-astra-porting`** |
| 分析新固件版本偏移、定位符号碰撞/`fault=0x234`、追踪 `.init_array` 栈帧或排错崩溃日志 | **`zve10-astra-methodology`** |

---

## 5. 项目定位与当前已知功能边界

- **仅面向开发者**：本项目处于工程研发阶段，**严禁引导或建议普通用户将自编译固件刷入真机**，以免变砖。
- **创意外观（`FL`、`VV` 等）未打通**：目前 `FL`、`VV` 等滤镜底层尚未打通，需开发者自行从其他支持机型提取并桥接（参见 `https://github.com/751K/sony-look-bridge`）。
- **自定义 LUT 方案**：自定义 LUT 需使用 `https://github.com/Erlla/sony-a7m4-custom-firmware` 的自定义 LUT 方案进行训练。
