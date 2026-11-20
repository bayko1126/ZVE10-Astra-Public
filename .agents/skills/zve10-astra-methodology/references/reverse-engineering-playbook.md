# 跨代逆向与排错实战手册 (`zve10-astra-methodology`)

本参考文件提炼自项目完整实验验证记录，面向希望理解底层推导细节或适配其它版本固件的开发者。

## 1. GNU Hash 等长改名的数学约束

`tools/build/elf_samehash_rename.py` 利用了 ELF `.gnu.hash` 的核心公式：

$$h_{i+1} = (h_i \times 33 + c_i) \bmod 2^{32}$$

只要对相邻两字符 `(c_i, c_{i+1})` 施加偏移 `(d, -33d)`，高位的 $+d \cdot 33$ 与低位的 $-33d$ 精确抵消：
- 必须确保修改后的两个字符均落在合法 C 标识符字节集 `A-Za-z0-9_` 内。
- 优先从符号名**末尾向前**搜索可替换位置（跳过 `_Z` C++ mangling 前缀），最大限度保留可读性（例如 `stella_print_log` -> `suDlla_print_log`，`...getInstanceEv` -> `...getInstanceFU`）。
- 修改后可用脚本内置的 `gnu_lookup` 校验：原符号名在 `.gnu.hash` 中查找返回 `None`（隔离成功），新符号名返回原 `dynsym` 索引（库内自引用完好）。

## 2. 真机环境 `im.elf` 接力补丁原理 (`tools/build/patch_im_elf.py`)

- 四代系统通过 `bootin.elf` 拉起 `/usr/bin/im.elf lazy-global &`。
- 在 `lazy-global` 模式下，`im.elf` 的 `THREAD_APP`（`0x9730`）在遍历完第 138 项（Barrier 8）后，调用 `0xadac` 检查命令行是否包含 `'a'` 参数；未命中则返回 `0` 导致 `sl=0`，跳过第 139 项 `appFwBoot.so` 并最终看门狗复位。
- 补丁将文件偏移 `0x175e`（虚拟地址 `0x975e`）处的 Thumb 指令 `mov r2, r0`（`0x4602`，小端 `02 46`）原地替换为 `movs r2, #1`（`0x2201`，小端 `01 22`），强制置位 `sl=1`，使后端服务就绪后自动接力加载五代 UI。

## 3. 日志防污染与熔断取证原则

- `/setting`（`nflasha2`）与 `/log`（`nflasha11`）属于持久存储分区。
- 每次编译验证 `appFwBoot_g4host.c` 应写入带唯一 Build/Run 标识的日志行，避免误将上一次失败的旧 `/log/astra/boot.log` 视为本轮结果。
- 刷入新镜像前需复位 `/setting/astra/fail_v2` 为 `0` 并移除 `/setting/astra/disable`，防止历史熔断计数器直接触发 `fallback_g4` 回退。
