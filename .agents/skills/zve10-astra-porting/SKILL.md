---
name: zve10-astra-porting
description: >-
  Comprehensive guide and end-to-end engineering workflow for porting Sony ZV-1M2
  (Gen 5 ASTRA) new menu UI and full touch/key interaction to Sony ZV-E10 (Gen 4),
  covering firmware input extraction, USB memory sandbox verification, ext2 production
  firmware building, WBI1-protected differential flashing, and factory disaster recovery.
---

# Sony ZV-E10 移植 ZV-1M2 (Gen 5 ASTRA) 真机工程完整指南

本 Skill 提供将 Sony ZV-1M2（五代 ASTRA 系统）新版菜单 UI 与触控/按键交互移植到 Sony ZV-E10（四代系统）的真机端到端工程流程，涵盖本地固件提取、USB 零触碰沙盒预检、生产 ext2 镜像构建、带 `0x2C000` (`WBI1`) 保护的差量烧录以及原厂一键救砖。

> **重要限制与受众声明**：
> - 本项目**仅面向具备硬件救砖能力的开发者**，**禁止普通用户直接刷入真机**，以免变砖。
> - 当前 `FL`、`VV` 等创意外观滤镜底层尚未打通，需自行从其他机型提取（`https://github.com/751K/sony-look-bridge`）；自定义 LUT 需使用 `https://github.com/Erlla/sony-a7m4-custom-firmware` 的方案进行训练。

---

## 1. 核心架构与技术要点

```text
                    ┌──────────────────────────────────────────────┐
                    │     Sony ZV-E10 官方宿主进程 (im.elf)       │
                    └──────────────────────┬───────────────────────┘
                                           │  调用 Application_System_Init
                    ┌──────────────────────▼───────────────────────┐
                    │    引导器垫片 (src/appFwBoot_g4host.c)       │
                    │  * 注入 16MB 独立栈 (避免 2730 构造爆栈)      │
                    │  * 构筑 __assert_fail / abort 异常免疫装甲    │
                    │  * 四代 RM 独占线程安全停靠 (g4_handoff)      │
                    │  * 注入 libObj 上下文保护桩 (防 0x148a6bc 崩溃)│
                    └──────────────────────┬───────────────────────┘
                                           │  按序 dlopen 加载闭包
       ┌─────────────────┬─────────────────┼─────────────────┬─────────────────┐
       ▼                 ▼                 ▼                 ▼                 ▼
 ┌───────────┐     ┌───────────┐     ┌───────────┐     ┌───────────┐     ┌───────────┐
 │ libOb5.so │     │ libmp5.so │     │libObjShim │     │AdjChkCfg  │     │libappFw   │
 │ (五代对象 │     │(五代参数/ │     │(跨代差异  │     │(机型参数  │     │Boot_g5.so │
 │  UI 引擎) │     │ sidb 库)  │     │ 桥接垫片) │     │ 配置文件) │     │(五代引导) │
 └─────┬─────┘     └─────┬─────┘     └─────┬─────┘     └─────┬─────┘     └─────┬─────┘
       └─────────────────┴─────────────────┼─────────────────┴─────────────────┘
                                           ▼
                    ┌──────────────────────────────────────────────┐
                    │         appFw.so (64.3MB 五代核心固件)        │
                    │  * 2,730 个 C++ 全局对象完成初始化           │
                    │  * 进入 _Z4MainPv(Application_Start)         │
                    │  * 运行全新 ASTRA 触控菜单系统               │
                    └──────────────────────────────────────────────┘
```

1. **等长同 Hash 符号隔离 (`tools/build/elf_samehash_rename.py`)**：对 557 个冲突符号改名为等长同哈希符号，彻底根治 `fault=0x234` 空指针崩溃。
2. **规避真机 MWF 重复初始化**：真机宿主 `im.elf` 启动时已初始化 `libMWF.so` 的 MonCtrl 单例；`appFwBoot_real.so` 严禁重复调用 `RsrcDP::Initialize`，同时对 `libMWF.so` 施以影子继承补丁（`tools/build/patch_mwf_shadow.py`）。
3. **断言与死锁免疫**：Hook `__assert_fail` / `abort` 并为 `condition_variable::wait` 注入 2s 超时兜底。
4. **四代硬件交接 (`g4_handoff_real_device`)**：回收四代 RM（`rm_exit`、`rm_kick_thread`、`rqueue`），释放 `/dev/dmpgles2` 与显存后点燃五代主循环。

详细符号与 GOT 映射表请查阅：[symbol-mapping-and-relocs.md](references/symbol-mapping-and-relocs.md)。

---

## 2. 关键工程文件索引

| 文件路径 | 职责说明 |
| :--- | :--- |
| `tools/build/prepare_inputs.py` | 从用户自备 `nflasha15.bin` 与 ZV-1M2 解包目录提取输入基线至 `firmware/` |
| `src/appFwBoot_g4host.c` | 核心跨代引导垫片源码（支持 QEMU / 真机生产 / USB 沙盒三目标） |
| `tools/build/build_appfwboot.py` | 引导垫片构建脚本（基于 `ziglang cc`，强制 `GLIBC_2.4` 与 `0x05000002`） |
| `tools/build/elf_samehash_rename.py` | ELF `.dynstr` 等长同 Hash 符号重命名工具 |
| `tools/build/build_prod_image.py` | 真机 `/usr` 分区生产镜像构建流水线（WSL ext2 注入打包） |
| `tools/build/verify_prod_image.py` | 生产镜像全量 ABI/GOT/闭包/资源属主静态校验器 |
| `src/verify_closure_safe.c` | 真机 USB 沙盒纯闭包安全验证执行器 |
| `tools/flash/camtool/cam.py` | 真机 USB Jiritsu 协议通信与控制工具 |
| `tools/flash/flash_robust.py` | **稳健差量刷写器（保护 `0x2C000` WBI1 头部，支持掉线断点续传）** |
| `tools/flash/restore_factory_pure.py` | 原厂全量纯净救砖还原工具 |
| `tools/flash/restore_wbi_auto.py` | WBI1 引导头单项自动修复工具 |
| `firmware/backup/nflasha15.bin` | 自备原厂 `/usr` 系统分区出厂备份（300MB，Git 已忽略） |
| `build_out/nflasha15_astra_prod.bin` | 最终生成的生产刷机镜像（300MB，Git 已忽略） |

---

## 3. 标准操作工作流

### 前置阶段：准备固件输入与预处理补丁
```powershell
uv run python tools/build/prepare_inputs.py `
  --gen4-image firmware/backup/nflasha15.bin `
  --gen5-root <你的_ZV1M2_解包根目录>

uv run --with capstone python tools/build/patch_im_elf.py
uv run --with pyelftools python tools/build/patch_mwf_shadow.py
uv run --with pyelftools python tools/build/patch_libobj.py
uv run --with pyelftools python tools/build/generate_hybrid_imdb.py --variant dump
uv run --with pyelftools python tools/build/p2_patch_soname.py
uv run --with pyelftools python tools/build/batch_rename_g5.py
uv run --with ziglang --with pyelftools python tools/build/build_libobjshim.py
```

### 阶段一：QEMU 仿真预演
参见 **`zve10-astra-qemu`** Skill 验证新菜单、触控与切屏，确保离线无已知问题。

### 阶段二：真机 USB 沙盒安全预检（零触碰物理 Flash）
在真机正常开机（USB 接入电脑，MSC / Jiritsu 通道可用）状态下利用 `/log/g5/` 测试加载闭包：

1. **健康诊断与通道探测**：
   ```powershell
   uv run --with pyusb --with libusb-package --with pycryptodome python tools/flash/camtool/cam.py doctor
   ```
2. **编译并运行沙盒验证器**：
   ```powershell
   uv run --with ziglang --with pyelftools python tools/build/build_live_verify.py
   uv run --with pyusb --with libusb-package --with pycryptodome python tools/flash/camtool/cam.py sh `
     "chmod 755 /log/camtool/verify_closure_safe && /log/camtool/verify_closure_safe"
   ```

### 阶段三：真机生产镜像构建、验证与熔断重置
1. **编译真机与沙盒目标引导垫片**：
   ```powershell
   uv run --with ziglang --with pyelftools python tools/build/build_appfwboot.py --target all
   ```
2. **构建并严格校验生产 ext2 镜像**：
   ```powershell
   uv run --with pyelftools python tools/build/build_prod_image.py --output build_out/nflasha15_astra_prod.bin
   uv run --with pyelftools python tools/build/verify_prod_image.py --image build_out/nflasha15_astra_prod.bin
   ```
   确认大小严格等于 `314,572,800` 字节（300MB）且全部检查项输出 `[PASS]`。
3. **清理真机持久熔断计数**：
   ```powershell
   uv run --with pyusb --with libusb-package --with pycryptodome python tools/flash/camtool/cam.py sh `
     "> /log/astra/boot.log && echo 0 > /setting/astra/fail_v2 && rm -rf /tmp/astra* /setting/astra/disable"
   ```

### 阶段四：Fastboot 差量稳健刷入
1. **Pico 串口注入进入 Fastboot (`18D1:4EE0`)**：
   ```powershell
   uv run --with pyserial python tools/flash/hardware/inject_payload.py --port COM12 --file tools/flash/hardware/fastboot_zve10.bin --duration 15
   ```
2. **先判定当前设备镜像指纹 + Dry-Run 校验**：
   ```powershell
   uv run --with pyusb --with libusb-package python tools/flash/check_image.py
   uv run --with pyusb --with libusb-package python tools/flash/flash_robust.py `
     --image build_out/nflasha15_astra_prod.bin `
     --diff-from firmware/backup/nflasha15.bin `
     --dry-run
   ```
3. **执行差量刷写**（保护扇区 `0x2C000` 永不写入，支持断点续传）：
   ```powershell
   uv run --with pyusb --with libusb-package python tools/flash/flash_robust.py `
     --image build_out/nflasha15_astra_prod.bin `
     --diff-from firmware/backup/nflasha15.bin `
     --flash
   ```

### 阶段五：原厂应急灾难恢复（救砖底牌）
- **快速差量回退**：
  ```powershell
  uv run --with pyusb --with libusb-package python tools/flash/flash_robust.py `
    --image firmware/backup/nflasha15.bin `
    --diff-from build_out/nflasha15_astra_prod.bin `
    --flash
  ```
- **全量纯净救砖**：
  ```powershell
  uv run --with pyusb --with libusb-package python tools/flash/restore_factory_pure.py --fresh
  ```
- **单项修复红灯不亮 (`0x2C000` WBI1 损坏)**：
  ```powershell
  uv run --with pyusb --with libusb-package python tools/flash/restore_wbi_auto.py
  ```

更多分区保护细节与故障排除请参见：[firmware-flashing-and-recovery.md](references/firmware-flashing-and-recovery.md)。
