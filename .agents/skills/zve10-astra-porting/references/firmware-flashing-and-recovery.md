# ZV-E10 固件打包、WBI1 保护差量刷入与灾难恢复指南

本文档详细记录 Sony ZV-E10 物理 Flash 关键分区映射、WBI1 引导头安全机制、差量烧录原理及原厂固件应急回滚规程（对应仓库文档 [docs/hardware-flashing-and-recovery.md](../../../../docs/hardware-flashing-and-recovery.md)）。

---

## 1. 物理 Flash 关键分区映射（实证核对版）

| 分区设备节点 | 起始扇区 (LBA) | 典型大小 / 格式 | 挂载点 / 角色 | 保护要求 |
| :--- | :--- | :--- | :--- | :--- |
| `nflasha2` | `0x004000` | 20 MB (FAT12) | `/setting`（系统持久设置与 `fail_v2` 熔断计数） | 刷前清理 `/setting/astra/fail_v2` |
| `nflasha5` | `0x02C000` (WBI1 头) | 60 MB | 暖启动引导分区（含 `0x2C000` `WBI1` 签名） | **0x2C000 偏移绝对锁定** |
| `nflasha10` | `0x056000` | FAT12 | `/tmp`（临时日志分区） | 只读提取诊断 |
| `nflasha11` | `0x05C000` | FAT16 | `/log`（`escapelog.bin`、`boot.log`、`/log/g5/` 沙盒） | 只读提取 / USB 沙盒空间 |
| `nflasha15` | `0x16C000` | 300 MB (ext2) | `/usr` 主系统与应用库 | **本次固件移植唯一刷写目标** |

---

## 2. WBI1 引导头与 `0x2C000` 保护原理

### 2.1 为什么必须保护 `0x2C000`
- 在 `nflasha5` 分区内部，扇区 `0x2C000` 存放了索尼处理器的 **WBI1 引导头签名**（原厂仅含少量非零关键字节）。
- 若抹除了 `0x2C000`（写入全 0），处理器引导校验会失败，导致插电池无反应、底盘红灯不亮。
- `tools/flash/flash_robust.py` 在代码级别强制拦截该扇区：
  ```python
  FORBIDDEN_SECTOR = 0x2C000
  if sector == FORBIDDEN_SECTOR and not a.allow_wbi:
      raise RuntimeError("拒绝写入受保护扇区 0x2C000（nflasha5 WBI1 头部）")
  ```

### 2.2 若不慎损坏的急救手段
```powershell
uv run --with pyusb --with libusb-package python tools/flash/restore_wbi_auto.py
```
该工具从本地 `firmware/backup/nflasha5.bin` 提取原厂 512B 头部并精确写回 `0x2C000`。

---

## 3. 差量刷写机制 (`tools/flash/flash_robust.py`)

### 3.1 差量计算逻辑
- **目标数据源 (`--image`)**：新构建的五代系统镜像（如 `build_out/nflasha15_astra_prod.bin`）。
- **当前设备基准 (`--diff-from`)**：设备上正在运行的原始镜像（如 `firmware/backup/nflasha15.bin`）。
- 工具以 `256 KB`（一个 Chunk，共 1,200 块）为单位比对差异，仅写入变化块，极大降低写入时间与物理闪存损耗。
- 使用 `--diff-from` 前**必须**先运行 `tools/flash/check_image.py` 确认相机实际运行镜像与 `--diff-from` 一致。

### 3.2 掉线自动重连与看门狗会话保活
- **看门狗超时机制**：Fastboot 模式下硬件看门狗默认约 215~220 秒触发硬复位。
- **协议保活心跳 (`oem darwin`)**：在块写入间隙安全点自动发送 `oem darwin` 心跳；也可单独运行 `tools/flash/fastboot_keepalive.py` 持续维持连接。
- **断点续传**：进度保存在 `tools/flash/diagnostic/flash_robust_progress.json`。若遇物理断线，全量模式可重新进入 Fastboot 后直接续传；差分模式中断则建议改用全量或 `restore_factory_pure.py --fresh`。

---

## 4. 熔断保护机制与计数清零规范

1. `Application_System_Init` 执行时检查 `/setting/astra/fail_v2`；若 `fails >= 3`，降级启动四代原厂 UI。
2. 启动先写入 `fails + 1`，只有当五代主循环平稳存活满 **60 秒** 后才自动清零为 `0`。
3. 刷入新版镜像前务必通过 `cam.py` 清空旧熔断计数：
   ```powershell
   uv run --with pyusb --with libusb-package --with pycryptodome python tools/flash/camtool/cam.py sh `
     "> /log/astra/boot.log && echo 0 > /setting/astra/fail_v2 && rm -rf /tmp/astra* /setting/astra/disable"
   ```

---

## 5. 原厂固件恢复方案（救砖底牌）

- **方案 A：差量回退（约 1 分钟）**
  ```powershell
  uv run --with pyusb --with libusb-package python tools/flash/flash_robust.py `
    --image firmware/backup/nflasha15.bin `
    --diff-from build_out/nflasha15_astra_prod.bin `
    --flash
  ```
- **方案 B：全量还原（适用于分区损坏或状态不明时）**
  ```powershell
  uv run --with pyusb --with libusb-package python tools/flash/restore_factory_pure.py --fresh
  ```
