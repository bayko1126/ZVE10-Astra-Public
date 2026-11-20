# ZV-E10 (Gen 4) 移植 ZV-1M2 (Gen 5 ASTRA) 符号映射、GOT 装甲与事件链路

本文档记录移植核心组件 `src/appFwBoot_g4host.c`、`libOb5.so`、`libappFwBoot_g5.so` 和 `appFw.so` 的符号隔离机制、GOT Hook 偏移及输入事件链。

---

## 1. 等长同 Hash 符号重命名机制 (`tools/build/elf_samehash_rename.py`)

### 1.1 核心原理
在 ELF 文件中，动态链接器使用 `.gnu.hash` 表检索符号：

$$\text{GNU Hash: } h = h \times 33 + c \pmod{2^{32}}$$

若直接修改符号名，会导致本库自身重定位（如 `R_ARM_GLOB_DAT`）计算 hash 不一致从而报错 `undefined symbol`。
通过将相邻两字符 $(c_i, c_{i+1})$ 替换为 $(c_i + d, c_{i+1} - 33d)$，增量 $d \times 33^k$ 与 $-33d \times 33^{k-1}$ 精确抵消，保持 `.gnu.hash` 严格不变，同时外部跨库对旧符号名的查找因 `strcmp` 不匹配而被彻底隔离。

### 1.2 关键冲突符号映射表

| 原始五代符号 | 等长同 Hash 隔离符号 | 解决的跨代冲突 |
| :--- | :--- | :--- |
| `stella_print_log` | `suDlla_print_log` | 拦截未导出的底层日志入口 |
| `_ZN2ux6gfxeng14GraphicsEngine15sys_getInstanceEv` | `_ZN2ux6gfxeng14GraphicsEngine15sys_getInstanceFU` | 隔离四代 `libObj.so`，独占绑定五代 `libOb5.so` 单例 |
| `_ZN2ux6gfxeng14GraphicsEngine28getAreaCheckExtensionManagerEv` | `_ZN2ux6gfxeng14GraphicsEngine28getAreaCheckExtensionManagerFU` | **彻底根治 `fault=0x234` 空指针崩溃**，切断向四代虚表错误调用的路径 |
| `_ZN2ux6gfxeng14GraphicsEngine30setOverflowResizeFontAttributeEhh` | `_ZN2ux6gfxeng14GraphicsEngine30setOverflowResizeFontAttributeEiG` | 文本自动缩放接口隔离 |
| `_ZN2ux6gfxeng14GraphicsEngine32setOverflowResizeLayoutAttributeEh` | `_ZN2ux6gfxeng14GraphicsEngine32setOverflowResizeLayoutAttributeFG` | 布局重叠扩展接口隔离 |
| `_ZN2ux6gfxeng25AreaCheckExtensionManager10setLogPathEPKc` | `_ZN2ux6gfxeng25AreaCheckExtensionManager10setLogPathEPLB` | 区域检查日志重定向 |
| `_ZN2ux6gfxeng25AreaCheckExtensionManager16setCheckDataPathEPKc` | `_ZN2ux6gfxeng25AreaCheckExtensionManager16setCheckDataPathEPLB` | 区域数据路径隔离 |
| `_ZN2ux6resmgr15ResourceManager11setDatabaseEPKc` | `_ZN2ux6resmgr15ResourceManager11setDatabaseEPLB` | 数据库装载隔离 |
| `_ZN2ux6resmgr15ResourceManager28sys_setFontNameWithNamespaceEt` | `_ZN2ux6resmgr15ResourceManager28sys_setFontNameWithNamespaceFS` | 字库命名空间隔离 |
| `_ZN8UXEngine6gfxengEv` | `_ZN8UXEngine6gfxengFU` | 引擎全局入口隔离 |
| `_ZN8UXEngine6resmgrEv` | `_ZN8UXEngine6resmgrFU` | 资源管理器入口隔离 |
| `_ZN2ux4core2ID4ZEROE` | `_ZN2ux4core2ID4ZERNf` | 静态零 ID 常量隔离 |

---

## 2. 关键 GOT 槽位与动态 Hook 偏移表 (`appFw.so` / `libOb5.so`)

| 模块 | GOT / 代码相对偏移 | 拦截目标函数 | 补丁/装甲行为 |
| :--- | :--- | :--- | :--- |
| `appFw.so` | `0x0377b408` | `__assert_fail` | 重定向至 `astra_assert_ignore`，防止内部非关键断言终止进程 |
| `appFw.so` | `0x0377ae50` | `condition_variable::wait` | 注入 2.0s 超时兜底，防止 `model_future::get` 永久阻塞 |
| `appFw.so` | `0x0377ad5c` | `osal_snd_sync_direct` | 合成 `0x910042` 帧同步时间 |
| `appFw.so` | `0x0377b270` | `GRM_fbrmCheckUpdateFinished` | 强制返回 `0`（表示帧已就绪） |
| `appFw.so` | `0x0377afe0` | `GraphicsEngine::drawFramebuffer` | 软光栅触发或真机 12B 非对齐跳板同步 |
| `appFw.so` | `0x02bc3174` | `notify_model_system_error` | 指令级动态修改为 `bx lr`（Thumb 机器码 `0x4770`） |
| `libOb5.so` | `0x003553f4` | `mmap` | 拦截 `flags=0x18002` 并去除内核不支持标志，返回可用虚拟内存 |

---

## 3. 跨代四代 RM 停靠机制 (`g4_handoff_real_device`)

在真机宿主 `im.elf` 中，五代启动前必须让四代独占线程安全停靠：
1. **唤醒与退出标记**：设置 `g_g4_rm_allow_exit = 1`，广播 `pthread_cond_broadcast(&g_g4_rm_cv)`。
2. **回收四代 RM**：调用四代 `libObj.so:rm_exit(0x774111)`，回收 `rm_kick_thread`、`rqueue` 与 `dmpgles2` 设备句柄。
3. **停靠阻塞线程**：通过发送无 `SA_RESTART` 的信号，使阻塞在 `sugilite_ioctl` 的四代线程交出锁并进入挂起状态。

---

## 4. 触控与按键输入事件派发链

### 4.1 触摸事件链
1. **输入源**：
   - QEMU：通过串口 socket（`/dev/ttyAM0`）接收 `TOUCH <DOWN|MOVE|UP|TAP> <x> <y>` 指令。
   - 真机：由四代 `libInfraTp`（`psid=0x07fb`）采集触控坐标与屏翻转状态，并经事件 ID `0x026c` 上报至 `appFw.so` 主事件循环（`0x0054f83e`）。
2. **派发至五代窗口系统 (`libOb5.so` `WidgetSystem`)**：
   - 获取五代 `WidgetSystem` 实例单例（`dlsym(hOb5, "_ZN2ux6wgtsys12WidgetSystem15sys_getInstanceFU")`）。
   - 分别调用 `postMouseMove(x, y)` / `postMousePress(1)` / `postMouseRelease(1)`，触发五代 `Widget` 树点击与滑动。

### 4.2 按键事件链
1. 拦截或对接按键输入（QEMU 下由 `g5_tact_key_table` 派发至 `appFw.so` 按键分发器）。
2. 切屏控制需在 `THREAD_APP` 线程安全边界内调用 `pcm::open_screen`，杜绝跨线程直接修改 UI 状态机。
