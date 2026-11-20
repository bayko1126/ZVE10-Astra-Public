# 真机沙盒预检、差量刷写与救砖手册 (`docs/hardware-flashing-and-recovery.md`)

> [!CAUTION]
> **受众限制与硬件安全红线（操作前必读）**
> 0. **仅面向开发者 · 禁止普通用户刷入**：真机刷写涉及 Pico 串口硬件注入与底层 eMMC 分区写入，**禁止普通用户自行刷入，以免操作失误导致变砖**。请先在 QEMU 环境完成全部软件验证。
> 1. **严禁触碰扇区 `0x2C000` (`nflasha5` WBI1 头)**：绝对禁止向 `/dev/nflasha5` 写入日志或调任何 `clear_wbi` 逻辑。一旦 `0x2C000` 签名被清零，相机插电池会红灯不亮假死（需通过 Pico 进入 Fastboot 后运行 `restore_wbi_auto.py` 恢复）。
> 2. **只修改 `nflasha15` (`/usr` 分区)**：严禁修改 `nflasha3`（内核/initrd/`av-cam.bin`）或 `nflasha7`（根文件系统），确保单分区 100% 可回退。
> 3. **差分刷写前置校准**：使用 `flash_robust.py --diff-from` 前**必须**先运行 `check_image.py` 确认相机内当前镜像与 `--diff-from` 指定的基线完全一致。若差分刷写中途发生 USB 中断，**严禁继续差分**，请改用全量刷写或 `restore_factory_pure.py --fresh`。

---

## 1. Windows USB 驱动与运行环境

1. **安装 `uv`**：
   ```powershell
   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
   ```
2. **Fastboot (`18D1:4EE0`) 驱动配置**：
   - 相机通过 Pico 进入 Fastboot 模式后，使用 [Zadig](https://zadig.akeo.ie/) 选中 `Android Bootloader Interface` / `ZV-E10-FASTBOOT`（`18D1:4EE0`），安装 **`libusbK`** 或 **`WinUSB`** 驱动。
3. **验证 Fastboot 通信**：
   ```powershell
   uv run --with pyusb --with libusb-package python -c "import usb.core, libusb_package; print(usb.core.find(idVendor=0x18d1, idProduct=0x4ee0, backend=libusb_package.get_libusb1_backend()) is not None)"
   ```

---

## 2. 核心工具命令速查

> 上游 USB 调试套件：`tools/flash/camtool/` 源自开源项目 [nolan2628/Sony-ZVE-10-Camtool](https://github.com/nolan2628/Sony-ZVE-10-Camtool)。

| 操作目标 | 命令（仓库根目录） | 说明 |
| :--- | :--- | :--- |
| **USB 在线调试自检 (开机模式)** | `uv run --with pyusb --with libusb-package --with pycryptodome python tools/flash/camtool/cam.py doctor` | 在原厂开机状态下检查 MSC/Jiritsu 通道与权限 |
| **Pico 串口注入 Fastboot** | `uv run --with pyserial python tools/flash/hardware/inject_payload.py --port COM12 --file tools/flash/hardware/fastboot_zve10.bin --duration 15` | 15 秒窗口内通电进入 `18D1:4EE0` Fastboot |
| **只读状态快照 (Superblock + WBI1)** | `uv run --with pyusb --with libusb-package python tools/flash/probe_state.py` | 只读核验 `nflasha15` (0x16C000) 与 `nflasha5` (0x2C000) |
| **只读判定当前镜像指纹** | `uv run --with pyusb --with libusb-package python tools/flash/check_image.py` | 差分刷机前必跑，判定当前设备上的 `nflasha15` 版本 |
| **安全差量 / 全量刷写** | `uv run --with pyusb --with libusb-package python tools/flash/flash_robust.py --image build_out/nflasha15_astra_prod.bin --diff-from firmware/backup/nflasha15.bin --flash` | 保护 `0x2C000`，支持断点续传（建议先加 `--dry-run` 预演） |
| **提取 FAT 持久日志** | `uv run --with pyusb --with libusb-package python tools/flash/extract_fat_logs.py --partitions all` | 只读拉取 `/setting` (`nflasha2`)、`/log` (`nflasha11`)、`/tmp` (`nflasha10`) |
| **一键恢复出厂 `/usr` 固件** | `uv run --with pyusb --with libusb-package python tools/flash/restore_factory_pure.py --fresh` | 全量 1200 块刷回 `firmware/backup/nflasha15.bin` 并固化 `WBI1` |
| **单项修复 WBI1 引导头** | `uv run --with pyusb --with libusb-package python tools/flash/restore_wbi_auto.py` | 将 `firmware/backup/nflasha5.bin` 原厂头写回 `0x2C000` |

---

## 3. 标准真机部署流程

### Phase A：USB `/log/g5/` 零刷机沙盒预检
1. 编译真机沙盒验证器与 `appFwBoot_sandbox.so`：
   ```powershell
   uv run --with ziglang --with pyelftools python tools/build/build_appfwboot.py --target all
   uv run --with ziglang --with pyelftools python tools/build/build_live_verify.py
   ```
2. 通过 `cam.py` 上传五代库至 `/log/g5/` 并执行 `verify_closure_safe` / `sandbox_live_verify`，确认全部 `.so` 加载闭合与 `dlsym` 通过。

### Phase B：构建并校验 300MB 生产镜像
```powershell
uv run --with pyelftools python tools/build/build_prod_image.py --output build_out/nflasha15_astra_prod.bin
uv run --with pyelftools python tools/build/verify_prod_image.py --image build_out/nflasha15_astra_prod.bin
```
确认输出体积严格等于 `314,572,800` 字节，且 `e2fsck -fn` 与 `DT_NEEDED` 全镜像校验全部通过。

### Phase C：重置持久熔断计数并差量刷入
1. 相机开机连 USB，重置可能残留的熔断计数：
   ```powershell
   uv run --with pyusb --with libusb-package --with pycryptodome python tools/flash/camtool/cam.py sh `
     "> /log/astra/boot.log && echo 0 > /setting/astra/fail_v2 && rm -rf /tmp/astra* /setting/astra/disable"
   ```
2. 拔掉电池静置 3 秒，启动 Pico 注入监听后装电池开机进入 Fastboot。
3. 先跑 `check_image.py` 与 `flash_robust.py ... --dry-run` 确认差异块数与基线一致，再执行 `--flash`。

---

## 4. 常见故障自愈

- **插电池完全无反应、底盘红灯不亮**：扇区 `0x2C000` 的 `WBI1` 受损。Pico 注入进入 Fastboot 后运行 `uv run --with pyusb --with libusb-package python tools/flash/restore_wbi_auto.py`。
- **Fastboot 刷机超过 ~3 分钟超时重启**：使用 `fastboot_keepalive.py`（每 30s 发送 `oem darwin` 心跳）；`flash_robust.py` 与 `restore_factory_pure.py` 均已内置续传与保活支持。
- **开屏未进新菜单或卡死**：进入 Fastboot 运行 `extract_fat_logs.py --partitions all` 查看 `/log/astra/boot.log` 里程碑，核对 G1~G4 阶段停在何处。
