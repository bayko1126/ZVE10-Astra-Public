# ZV-E10 固件打包、WBI1 保护差量刷入与灾难恢复指南

本文档详细记录 Sony ZV-E10 物理 Flash 分区结构、WBI1 引导头安全机制、差量烧录原理及原厂固件应急回滚手册。

---

## 1. 物理 Flash 分区表与映射

Sony ZV-E10 采用 eMMC / SPI NAND 混合存储，关键分区映射如下：

| 分区设备节点 | 物理起始 LBA | 分区大小 | 挂载点 / 角色 | 关键保护要求 |
| :--- | :--- | :--- | :--- | :--- |
| `nflasha1` | `0x000000` | 512 KB | Bootrom / 硬件配置 | **严禁触碰** |
| `nflasha2` | `0x001000` | 4 MB | 设备树 / 原厂硬件校准数据 | 只读备份 |
| `nflasha5` | `0x020000` | 16 MB | Linux 内核与 WBI 引导层 | **0x2C000 偏移绝对锁定** |
| `nflasha6` | `0x040000` | 8 MB | `/tmp/jiritsu` (系统诊断与临时运行) | 具备挂载恢复能力 |
| `nflasha15` | `0x16c000` | 300 MB | `/usr` 主系统与应用库 (ext2) | **本次固件移植刷写目标** |
| `nflasha16` | 动态 | 500 MB | `/log` 持久日志与沙盒验证区 | 用户数据与 USB 沙盒空间 |
| `nflasha17` | 动态 | 16 MB | `/setting` 相机配置与熔断标志 | 存放 `fail_v2` 计数 |

---

## 2. WBI1 引导头与 0x2C000 保护原理

### 2.1 为什么必须保护 0x2C000
* 在 `nflasha5` 分区内部，扇区 `0x2C000`（即 LBA `180224`）存放了索尼处理器的 **WBI1 引导头签名**（原厂只有 25 个非零字节）。
* 若抹除了 `0x2C000`（写入全 0），处理器二级引导器（SPL）将拒绝加载内核，直接导致相机物理变砖。
* `flash_robust.py` 在代码级别强制进行扇区拦截：
  ```python
  FORBIDDEN_SECTOR = 0x2C000
  if sector == FORBIDDEN_SECTOR and not a.allow_wbi:
      raise RuntimeError("拒绝写入受保护扇区 0x2C000（nflasha5 WBI1 头部）")
  ```

### 2.2 若不慎损坏的急救手段
若此前因旧脚本导致 WBI1 签名丢失，可通过专门恢复工具重构：
```powershell
uv run python 04_tools_and_flash\restore_wbi_auto.py
```
该工具会从本地 `01_factory_backup\nflasha5.bin` 中提取纯净的 25 字节签名并精确回写到 `0x2C000`。

---

## 3. 差量刷写机制 (`flash_robust.py`)

### 3.1 差量计算逻辑
* **目标数据源 (`--image`)**：新构建的五代系统镜像（如 `nflasha15_astra_prod_v4.bin`）。
* **当前设备基准 (`--diff-from`)**：设备上正在运行的原始镜像（如 `01_factory_backup\nflasha15.bin`）。
* 刷写工具以 `256 KB`（一个 Chunk）为单位对比两份文件，仅挑选出不相同的块生成写入清单（`remaining` 块列表）。
* 实测全量 300MB 镜像共 1,200 个 Chunk，差量仅 745 个 Chunk（约 186.2 MiB），节省 40% 的刷写时间并极大减少物理 Flash 损耗。

### 3.2 长会话看门狗免疫与 1 MiB Sparse 极速刷写 (`toolkit_fastboot` + `stage2-flash.bin`)
* **看门狗超时机制**：索尼处理器电源 MCU（Darwin）在 Fastboot 模式下默认约 4 分钟自动触发硬复位；旧方案依赖 `oem darwin` 轮询，在部分场景下仍会超时。
* **Darwin `0x16` 硬件保活**：`04_tools_and_flash\toolkit_fastboot\prebuilt\stage2-flash.bin` 在入口处自动通过 SIO3 读取电源 MCU `0x16` 帧，彻底解除 4 分钟自动复位限制，一次会话即可稳定完成 300 MB 读写与校验。
* **1 MiB Sparse 分片极速写入**：`flash_robust.py` 与 `restore_factory_pure.py` 通过 `toolkit_session.py` 将变动块合并后按 `1,044,480 B` (~1 MiB) 封装为标准 libsparse（`CHUNK_SKIP` 传偏移 + `CHUNK_RAW` 传数据）写入目标分区，并在写后自动通过 `fetch:` 回读比对或校验整分区 SHA-256。
* **断点续传**：刷写进度仍持久化记录在 `04_tools_and_flash\diagnostic\flash_robust_progress.json`。若物理拔线中断，重进 Fastboot 后重新运行相同命令即可从断点继续。

---

## 4. 熔断保护机制与计数清零规范

### 4.1 熔断逻辑
1. 固件引导器 `Application_System_Init` 执行时，首先检查 `/setting/astra/fail_v2`。
2. 若 `fails >= 3`（`ASTRA_MAX_FAIL`），系统认定五代连续崩溃，立即执行 `fallback_g4` 降级启动四代官方 UI。
3. 预先向 `/setting/astra/fail_v2` 写入 `fails + 1`，只有当五代主循环平稳存活满 **60 秒** 后，由看门狗守护线程自动清零为 `0`。

### 4.2 刷前必做清理
由于 `/setting` 分区是独立的持久存储，刷入新的 `/usr` 镜像无法重置该分区状态。在刷机前必须通过 USB 命令彻底复位：
```powershell
uv run --with pyusb --with libusb-package --with pycryptodome python 04_tools_and_flash\camtool\cam.py sh `
  "> /log/astra/boot.log && echo 0 > /setting/astra/fail_v2 && rm -rf /tmp/astra* /setting/astra/disable"
```

---

## 5. 原厂固件恢复方案（救砖底牌）

### 5.1 方案 A：差量回退（推荐，耗时约 1 分钟）
在相机进入 Fastboot 模式下执行：
```powershell
uv run --with pyusb --with libusb-package python 04_tools_and_flash\flash_robust.py `
  --image 01_factory_backup\nflasha15.bin `
  --diff-from 07_build_out\nflasha15_astra_prod_v4.bin `
  --flash
```

### 5.2 方案 B：全量还原（适用于分区损坏严重时）
```powershell
uv run python 04_tools_and_flash\restore_factory_pure.py --fresh
```
该工具会忽略差量缓存，直接对 `nflasha15` 进行扇区覆盖恢复。
