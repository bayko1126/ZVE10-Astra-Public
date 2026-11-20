# 跨代固件移植核心方法论 (`docs/methodology.md`)

本文档总结了将 Sony 五代 ASTRA（ZV-1M2）交互系统移植到四代底盘（ZV-E10）过程中通过二进制逆向、动态追踪与真机实测确立的**四大技术公理**与**工程排错方法论**，供复现者深入理解原理或迁移到其他相近机型。

---

## 1. 四大核心技术公理

### 公理一：`appFw.so` `.init_array` 巨型栈帧与 16MB 专属线程栈策略

- **现象与根因**：`appFw.so`（64.3 MB）在 `.init_array`（`0x03447000`）中包含 **2,730 个 C++ 全局构造函数**，在 `dlopen("appFw.so")` 时由动态链接器同步执行。反汇编显示其中至少有 17 个巨型栈帧构造函数（例如 `.init_array[1873]` 单函数分配 **3.28 MB** 栈空间，`.init_array[1863]` 分配 **1.48 MB**）。
- **为什么原版引导会崩溃**：原版 `appFwBoot.so` 在 `im.elf` 的默认小栈工作线程中直接调用 `dlopen("appFw.so")`，尚未创建大栈 UI 线程就已栈溢出 `SIGSEGV`。
- **为什么不能全局扩大栈上限**：`im.elf` 运行期间会创建多达 **188 个线程**；在 32 位 ARM 进程（3GB 用户态 VMA）下若通过 `ulimit -s` 或 `RLIMIT_STACK` 全局调大栈，会瞬间耗尽虚拟地址空间。
- **标准解法**：在 `appFwBoot_g4host.c` 的 `Application_System_Init` 中**不阻塞且不直接 `dlopen("appFw.so")`**，而是立即用 `pthread_attr_setstacksize(&attr, 16 * 1024 * 1024)` 创建一个**单独的 16MB 栈线程**后返回 `0`（保证 `im.elf` 的 500ms 硬件看门狗不超时），再在该 16MB 专属栈线程内按序加载五代依赖闭包并启动 `_Z4MainPv(Application_Start)`。

### 公理二：消除四代 `libObj.so` 强符号劫持

- **现象与根因**：四代 `libObj.so`（21.3 MB）导出了强符号 `Application_System_Init`（用于拉起四代 `viewUnified2.so`），而五代 `appFw.so` 只导出 `Application_Start`。如果直接让 `im.elf` 对 `appFw.so` 调用 `dlsym(..., "Application_System_Init")`，动态链接器会在全局符号表中回退命中四代 `libObj.so` 的旧入口，导致启动旧 UI 或因旧库已删而崩溃。
- **标准解法（双保险）**：
  1. 在 `libIMDB.so` 中将 `Application_System_Init` 条目指向定制的 `appFwBoot.so`，且将 `me_psid` 设为 `0x00000083`（不再是 `0x07fb`）。
  2. 通过 `patch_libobj.py` 在四代 `libObj.so` 的 `.dynstr` 中将 `"Application_System_Init"`（22 字符 + NUL = 23 字节）原地等长替换为 `"Application_G4Old_Init"`，彻底断绝全局符号劫持。

### 公理三：`libIMDB.so` 中 `psid = 0x07fb` 语义与双版本精准裁剪

- **真实语义**：逆向 `im.elf` 证实，模块表中的 `psid = 0x07fb` 并不表示“禁用”，而是表示 **“该模块已静态合入 `/usr/lib/libObj.so`”**，加载器会直接从 `libObj.so` 句柄中执行 `dlsym`。
- **工程规则**：
  - 所有四代硬件/传感器/镜头通信/触控驱动（`libInfraTp`）条目**必须保持 `psid = 0x07fb`**，由四代 `libObj.so` 承载。
  - 独立加载的五代模块（`appFwBoot.so`、`libuxengine.so`、`libgrm.so`）将 `psid` 置为 `0x0000`（后两者通过软链接指向 `libOb5.so`）。
  - 需要停用的模块（如 `sata_drv`、`EthernetService_Init`、`gps_*`、`fan_infra_init`、`vssapp_init` 以及字符串槽位被借用的 `testcmd_scn_init`），**严禁乱改 `psid`**，而是将其条目 `+0x08` 处的 `mode` 掩码设为 `0x00000000`。
  - 注意区分 **37,044 B（真机 `nflasha15.bin` 内置版）** 与 **37,204 B（官方固件解包版）** 两个版本的偏移差异（详见 `tools/build/generate_hybrid_imdb.py`）。

### 公理四：GNU Hash 等长同哈希符号隔离（解决 `fault=0x234`）

- **问题本质**：四代 `libObj.so` / `libmpr.so` 与五代 `libOb5.so` / `libmp5.so` / `appFw.so` 之间存在 **557 个独立同名冲突符号**。在全局符号表 `RTLD_GLOBAL` 共存时，五代函数内部的 GOT/PLT 调用会误绑定到四代同名旧实现（虚表布局不同），直接触发 `fault=0x234` 空指针/偏移错位崩溃。
- **核心算法（`tools/build/elf_samehash_rename.py`）**：
  - ELF `.gnu.hash` 使用 `h = h * 33 + c (mod 2^32)` 计算符号哈希。
  - 若将符号名相邻两字节 `(c_i, c_{i+1})` 分别调整为 `(c_i + d, c_{i+1} - 33 * d)`（且均落在合法标识符字符集内），其哈希增量 `d * 33^k + (-33 * d) * 33^(k-1)` 精确抵消为 `0`。
  - 这使得我们**无需重建 `.gnu.hash`、Bloom Filter、Bucket 与 Chain 表**，即可原地修改 `.dynstr`（例如 `stella_print_log` → `suDlla_print_log`），实现零结构破坏的跨代符号完全隔离。

---

## 2. 跨代硬件与交互桥接方法论

### 2.1 `libMWF.so` 影子继承与四代 RM 交接
- 真机宿主 `im.elf` 早已初始化过四代 `libMWF.so` 的 MonCtrl 与资源管理器；若五代再次尝试重复注册相同 `mgrId`，`MWF::RsrcMgr::InitRsrcMgr` 会触发 `overlap generation` 断言崩溃。
- 通过 `tools/build/patch_mwf_shadow.py` 将 `0x2d2ac` 处的 `cbz r0, #0x2d2d8` (`0xb1a0`) 改写为 `nop` (`0xbf00`)，使五代自动走影子继承路径。
- 在真机上加载五代 UI 前，由 `g4_handoff_real_device` 停靠四代渲染管理器（`rm_exit`、`rm_kick_thread`、`rqueue`），释放 `/dev/dmpgles2` 与显存控制权。

### 2.2 全界面触控打通机理
- **真机环境**：四代 `libObj.so` 内的 `libInfraTp`（`psid=0x07fb`）继续负责硬件触摸屏采样和侧翻屏坐标方向（`InfraTp::SetTpDirection`），并以事件 ID `0x026c` 上报 640×480 坐标；五代 `appFw.so` 主事件循环（`0x0054f664`）在 `0x0054f83e` 原生消费 `0x026c` 并调用 `libOb5.so` 导出的 `WidgetSystem::postMousePress/Move/Release`。
- **QEMU 环境**：`appFwBoot_g4host.c` 启动 `uart_touch_rx_thread` 读取 `/dev/ttyAM0` 文本指令（`TOUCH DOWN/MOVE/UP/TAP`、`KEY`、`OPEN_MENU`、`OPEN_SHOOT`），直接调用 `libOb5.so` 的 `WidgetSystem` 与 `appFw.so` 按键派发器，并经软光栅 Hook + `Morton Z-order RGBA4444` 编码写入保留物理显存 `0x20000000`。

### 2.3 运行期安全兜底装甲
- **动态指令 Hook**：将 `__assert_fail` 与 `abort` 入口覆写为 `bx lr`（`0x4770`），避免五代因非致命设备差异断言杀掉整个 `im.elf` 进程。
- **异步死锁兜底**：通过 GOT Hook 拦截 `std::condition_variable::wait`（`astra_cond_wait_hook`），在等待超时（2 秒）时清理相关等待状态标志，防止缺少后台服务或协处理器响应导致界面线程永久卡死。
- **后台服务自愈补丁 (`patch_im_elf.py`)**：在 `im.elf` 偏移 `0x175e`（`0x975e`）将 `mov r2, r0` (`0x4602`) 改为 `movs r2, #1` (`0x2201`)，确保后台 `THREAD_APP` 在没有 `'a'` 参数时仍可接力拉起 `appFwBoot.so`，防止看门狗超时硬复位。

---

## 3. 迁移到其它固件版本或相近机型的通用步骤

如果你打算将本方法论应用到 ZV-E10 其它固件版本或相近 Sony Gen4/Gen5 机型：
1. **符号冲突普查**：提取宿主与目标 `.so` 的导出/导入符号表，找出同名不同实现的碰撞集合，使用 `elf_samehash_rename.py` 生成新版 `rename_plan.json`。
2. **ABI 与 EABI 门禁**：任何自编译 `.so` 必须校验 `GLIBC` 版本需求不高于目标机（ZV-E10 为 `2.11.2`，构建脚本强制限制 `<= GLIBC_2.4`），并将 ELF Header `e_flags` 对齐为 `0x05000002`。
3. **分阶段最小风险验证**：严格遵循 **QEMU 离线验证 → 真机 USB `/log/g5/` 沙盒闭包预检 → 保护 `0x2C000` 差量刷机 → `/log/astra/boot.log` 带唯一 Run-ID 取证** 的递进流程。
