# 核心架构图索引 (`zve10-astra-overview`)

完整架构说明与 4 张 Mermaid 架构图位于仓库顶层文档：[docs/architecture.md](../../../../docs/architecture.md)。

## 快速架构摘要

1. **宿主接力链**：`bootin.elf` -> `im.elf`（`0x975e` 2 字节后台接力补丁）-> `libIMDB.so`（`0x7ca4` 指向 `appFwBoot.so`）-> 定制 `appFwBoot.so`（`16MB` 独立栈线程）-> 按序 `dlopen`（`libMWF.so` -> `libOb5.so` -> `libmp5.so` -> `libObjShim.so` -> `AdjChkConfig.so` -> `libappFwBoot_g5.so` -> `appFw.so`）-> `_Z4MainPv(Application_Start)`。
2. **ELF ABI 硬门禁**：所有自编译 `.so` 必须使用 `arm-linux-gnueabi.2.4`、最高依赖 `GLIBC_2.4`、并将 ELF Header `e_flags` 写为 `0x05000002`。
3. **双目标模式**：
   - **QEMU 模式**：`src/appFwBoot_g4host.c` 默认宏，启用 GL/渲染软光栅 Hook + `Morton RGBA4444` 写入 `0x20000000` + `/dev/ttyAM0` 触控/按键注入。
   - **真机模式**：`-DZVE10_REAL_DEVICE`（或附加 `-DZVE10_USB_SANDBOX`），跳过显存软光栅，由 `g4_handoff_real_device` 回收四代 RM 并释放 `/dev/dmpgles2`，由四代 `libInfraTp` (`0x026c`) 直接驱动五代 `WidgetSystem`。
