# Sony ZV-E10 原生运行五代 ASTRA (`appFw.so`)：运行手册 (`ZVE10-ASTRA-Port`)
*(Sony ZV-E10 Gen 5 ASTRA Porting: Operations Runbook)*

---

## 1. 核心命令速查表 (Quick Reference)

所有 Python 操作均统一通过 `uv` 工具链执行。在终端进入新项目根目录 `ZVE10-ASTRA-Port` 后执行：

| 操作目标 | 终端执行命令 (`ZVE10-ASTRA-Port`) | 说明 |
| :--- | :--- | :--- |
| **触发 Pico 串口注入 Fastboot** | `uv run --with pyserial python tools/flash/hardware\inject_payload.py --port COM7 --file tools/flash/hardware\stage1.bin --duration 15 --record-gap 0.002` | 通过 Pico 串口向 BootROM 注入配套 `stage1.bin` (`18D1:4EE0`, 15秒窗口，30秒内连接相机 USB 到电脑) |
| **设备超级块与 WBI1 状态探针** | `uv run --with pyusb --with libusb-package python tools/flash/probe_state.py` | 自动加载 `stage2-flash.bin` 并读取 `nflasha15` (0x16C000) 超级块与 `nflasha5` (0x2C000) `WBI1` 引导头 |
| **只读判定当前设备镜像指纹** | `uv run --with pyusb --with libusb-package python tools/flash/check_image.py` | 差分刷写前只读检查设备当前 `nflasha15` 镜像 |
| **差分 / 全量安全刷入 `/usr` 镜像** | `uv run --with pyusb --with libusb-package python tools/flash/flash_robust.py --image <目标镜像> --diff-from <基准镜像> --flash` | 基于 `stage2-flash` 1 MiB Sparse 分片极速写入与回读校验，入口自动执行 Darwin `0x16` 保活，永不触碰 `0x2C000` (`WBI1`) |
| **一键恢复官方出厂 `/usr` 固件** | `uv run --with pyusb --with libusb-package python tools/flash/restore_factory_pure.py --fresh` | 100% 刷回 `firmware/backup/nflasha15.bin`，1 MiB Sparse 极速写入 + 全盘 `fetch` SHA-256 核验 |
| **独立修复 WBI1 引导头** | `uv run --with pyusb --with libusb-package python tools/flash/restore_wbi_auto.py` | 通过 `stage2-flash` 将 `firmware/backup/nflasha5.bin` 原厂 `WBI1` 头部写回 `nflasha5` (0x2C000) 并回读核验 |
| **长会话握手诊断 (toolkit 原生)** | `uv run --with pyusb --with libusb-package python tools/flash/toolkit_fastboot\host\bootstrap.py --usb-only` | 校验 `stage1` ABI 后加载 `stage2-flash.bin` 并激活 Darwin `0x16` 长会话 |
| **只读提取 FAT 持久日志** | `uv run --with pyusb --with libusb-package python tools/flash/extract_fat_logs.py --partitions all` | 从 Fastboot 只读提取 `/setting` (`nflasha2`)、`/log` (`nflasha11`)、`/tmp` (`nflasha10`) 日志 |
| **Jiritsu USB 在线调试 (开机模式)** | `uv run --with pyusb --with libusb-package --with pycryptodome python tools/flash/camtool\cam.py doctor` | 在原厂开机（MSC `054C:0D95` / Jiritsu `054C:0336`）下自检与执行真机 Shell |

---

## 2. 系统环境与 USB 驱动配置

### 步骤 1：安装 uv 工具链
在 PowerShell 中执行官方一键安装：
```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

### 步骤 2：配置 ZV-E10 Fastboot 的 Windows USB 驱动
当相机通过 Pico 注入进入 Fastboot（`18D1:4EE0`）时，Windows 需将其识别为 `libusbK` 或 `WinUSB`：
1. 打开 [Zadig](https://zadig.akeo.ie/)，`Options` -> 勾选 `List All Devices`；
2. 选中 `Android Bootloader Interface` 或 `ZV-E10-FASTBOOT`（硬件 ID `18D1:4EE0`）；
3. 目标驱动选择 **`libusbK`** 或 **`WinUSB`**，点击 **`Install Driver`** / **`Replace Driver`**。

### 步骤 3：验证 Fastboot USB 通信
```powershell
uv run --with pyusb --with libusb-package python -c "import usb.core, libusb_package; print(usb.core.find(idVendor=0x18d1, idProduct=0x4ee0, backend=libusb_package.get_libusb1_backend()) is not None)"
```

---

## 3. 进入 Fastboot 模式标准规程

1. **接线准备**：Pico 注入器 USB 插入电脑（确认串口号如 `COM12`），相机 Type-C 连接注入器转接板与电脑 USB；
2. **启动串口注入监听（15 秒窗口，注入配套 `stage1.bin`）**：
   ```powershell
   cd ZVE10-ASTRA-Port
   uv run --with pyserial python tools/flash/hardware\inject_payload.py --port COM7 --file tools/flash/hardware\stage1.bin --duration 15 --record-gap 0.002
   ```
3. **通电握手**：在 15 秒窗口内，相机装入电池并将开关拨至 **ON**，30 秒内将相机 USB-C 改接电脑，确认枚举出 `18D1:4EE0`；
4. **执行探测、刷写或恢复**：直接运行 `probe_state.py`、`flash_robust.py`、`restore_factory_pure.py` 或 `restore_wbi_auto.py`（脚本会自动校验 `stage1` ABI 并加载 `stage2-flash.bin` 激活长会话）。

---

## 4. `ZVE10-ASTRA-Port` 工作空间目录结构

```text
ZVE10-ASTRA-Port\
├── PLAN.md                           # 新工作空间完整架构公理、偏移表与四阶段实施计划
├── OPERATIONS_RUNBOOK.md             # 本运行手册与操作规程
├── firmware/backup/                # 【救砖圣杯】ZV-E10 官方出厂原始分区备份 (只读基线)
│   ├── nflasha15.bin                 # 300MB 出厂 /usr 镜像 (SHA256: cb15bc41...)
│   ├── nflasha5.bin                  # 60MB 出厂 WBI1 暖启动引导分区 (含 0x2C000 WBI1 头)
│   ├── nflasha2.bin                  # 20MB 出厂 /setting 分区备份
│   ├── _sha256sums.txt               # 出厂备份哈希校验表
│   └── BACKUP_MANIFEST.md            # 分区布局与备份血统说明
├── 02_gen4_zve10_base\               # ZV-E10（四代）核心明文宿主、硬件服务与镜头协议
│   ├── bin\                          # im.elf, sen.elf, bootin.elf
│   ├── lib\                          # 四代 libObj.so, libIMDB (37044/37204), libInfraSKD.so, libInfraLensCommunicator.so, libmpr*.so
│   └── sabin\                        # 四代 E 卡口光学标定与 DSP 参数表 (77 个文件)
├── 03_gen5_zv1m2_astra\              # ZV-1M2（五代 ASTRA）核心 UI 引擎与触控资产
│   ├── lib\                          # appFw.so (64.3MB), appFwBoot.so, libOb5.so, libmp5.so, libFsys5.so, AdjChkConfig.so 等
│   └── share\app\                    # 五代 ASTRA UI 完整资源目录 (81.6MB, 232 个文件)
├── tools/flash/               # 硬件注入、极速刷机、状态探针、日志提取与 Jiritsu 工具链
│   ├── hardware\                     # inject_payload.py, inspect_pico.py, fastboot_zve10.bin
│   ├── camtool\                      # cam.py, jiritsu_lib.py, patch_engine.py, payload/, recipes/, vendor/pmca/
│   ├── stage2_turbo_cmd25.bin        # CMD25 24MHz 4-bit 极速写驱动 (2,000 B)
│   ├── stage2.bin                    # CMD17 只读提取驱动 (1,316 B)
│   ├── flash_robust.py               # 差分与全量安全刷写器 (保护 0x2C000)
│   ├── restore_factory_pure.py       # 一键恢复出厂 nflasha15 + WBI1 固化 (原生内置 30s 保活心跳)
│   ├── restore_wbi_auto.py           # WBI1 引导头自动修复程序 (支持常驻保活)
│   ├── fastboot_keepalive.py         # 统一 Fastboot 会话保活库与独立维持守护程序
│   ├── probe_state.py / check_image.py / execute_e2_lite.py
│   └── extract_fat_logs.py / read_blackbox_verified.py / ext2_read.py / ext2_write.py
└── 05_build_and_patch\               # 核心逆向补丁、符号桩与 16MB 大栈引导参考
    ├── test_g5_bootloader.c          # 真机验证通过的五代大栈线程加载序列参考源码
    ├── p2_disasm_appfwboot.py / parse_exact_boot.py / build_fixed_appfwboot.py
    ├── generate_hybrid_imdb.py / libIMDB_g5.so
    └── p2_patch_soname.py / p2_closure_check.py / p2_gen_stub.py / build_libobjshim.py / libObjShim.S / libObjShim.so
```

---

## 5. 常见故障自愈指南 (Troubleshooting)

### Q1：插入电池后相机完全没反应，底盘红灯也不亮？
- **根因**：扇区 `0x2C000`（`nflasha5` 分区头部）的 `WBI1` 签名受损。
- **解法**：通过 Pico 注入进入 Fastboot 后运行：
  ```powershell
  uv run --with pyusb --with libusb-package python tools/flash/restore_wbi_auto.py
  ```

### Q2：刷机过程中报 `USBError` 或连接中断？
- **解法**：`flash_robust.py` 与 `restore_factory_pure.py` 均内置逐块断点续传。若为全量刷写中断，重新进 Fastboot 后再次运行同一命令即可续传；若在 `--diff-from` 差分刷写中途中断且不确定基准状态，请先用 `restore_factory_pure.py --fresh` 或全量刷写恢复确定基线。
