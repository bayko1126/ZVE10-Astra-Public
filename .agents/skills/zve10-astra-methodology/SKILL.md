---
name: zve10-astra-methodology
description: >-
  Reverse engineering and cross-generation firmware porting methodology for Sony
  Gen 4/Gen 5 cameras. Use when analyzing new firmware versions, resolving symbol
  collisions with GNU Hash preservation, inspecting .init_array stack frames,
  diagnosing libIMDB slots, or troubleshooting runtime crashes and boot logs.
---

# Sony 跨代固件移植与逆向分析方法论

当你需要将本方案适配到**其他固件版本号**、**排查新的运行时崩溃**，或向他人讲解/复现整套逆向思路时，使用本 Skill。

---

## 1. 跨代移植标准分析流水线

1. **符号碰撞普查**：
   - 使用 `pyelftools` 导出四代宿主库（`libObj.so`、`libmpr.so`）与五代目标库（`libOb5.so`、`libmp5.so`、`AdjChkConfig.so`、`appFwBoot.so`、`appFw.so`）的 `.dynsym` 导出与未定义符号集合。
   - 剔除标准系统库（`libc.so.6` 等），对非等价实现冲突生成 `rename_plan.json`，交给 `tools/build/elf_samehash_rename.py` 原地改名。
2. **新增依赖桩闭合检查**：
   - 检查五代库引入但四代底盘缺失的符号，通过汇编/C 编写轻量空桩（参见 `src/libObjShim.S` 与 `tools/build/build_libobjshim.py`），避免运行期 Lazy Binding 崩溃。
3. **`.init_array` 栈帧审计**：
   - 若 `dlopen` 阶段在 `__dl_init` 崩在栈保护或栈底页，使用 `capstone` 反汇编 `.init_array` 函数入口的 `sub sp, #imm` / `sub.w sp, sp, #imm`，找出最大栈帧之和，按 2 倍以上余量配置独立 `pthread_attr_setstacksize`（当前为 16MB）。
4. **`libIMDB.so` 槽位逆向核验**：
   - 以 `imdb_raw` 符号为基址，按 `0x2c`（44 字节）步长遍历条目：`kind`、`phase`、`mode`、`me_psid`、`file_ptr`、`init_ptr`。
   - 修改任何复用字符串槽位前，**必须**同步清零原条目 `+0x08` (`mode`)，防止宿主误加载旧初始化函数。
5. **渐进式三层验证**：
   - **L1（离线 QEMU）**：用 `--target qemu` 验证构造、事件循环、界面渲染与基础触控。
   - **L2（USB 沙盒）**：用 `--target sandbox` + `verify_closure_safe` 在 `/log/g5/` 验证真机动态链接器符号闭合（零写入物理 Flash）。
   - **L3（真机镜像）**：用 `verify_prod_image.py` 静态通过所有 GOT/ABI/闭包断言后，再使用 `flash_robust.py --diff-from` 刷入，并通过 `extract_fat_logs.py` 提取 `/log/astra/boot.log` 里程碑验证。

---

## 2. 常见故障定位速查

| 故障特征 | 优先排查方向 | 解法参考 |
| :--- | :--- | :--- |
| `dlopen("appFw.so")` 直接段错误 | 线程栈大小 / 构造冲突 / `libInfraSKD` 密文 | 确认运行在 16MB 栈线程；确认保留四代明文 `libInfraSKD.so` |
| 崩溃在 `fault=0x234` 或虚表偏移错乱 | 跨代同名符号串台（`GraphicsEngine` / `UXEngine`） | 检查 `batch_rename_g5.py` 是否全部应用并校验 SONAME |
| 报错 `overlap generation` | `libMWF.so` 单例冲突 | 确认已应用 `tools/build/patch_mwf_shadow.py` |
| 卡在 `model_future::get` / 协处理器等待 | 缺少外设服务或信号量永不触发 | 检查 `astra_cond_wait_hook`（2s 超时）与 `notify_model_system_error` Hook |
| 插电池底盘红灯不亮 | `nflasha5` `0x2C000` `WBI1` 损坏 | Pico 注入 Fastboot 后运行 `tools/flash/restore_wbi_auto.py` |

深入逆向推导过程与实证数据请阅读：[reverse-engineering-playbook.md](references/reverse-engineering-playbook.md) 与 [docs/methodology.md](../../../docs/methodology.md)。
