#!/usr/bin/env python3
"""为 im.elf 注入常驻后台接力拉起五代 ASTRA UI 的 2 字节补丁。

分析结论：
四代系统开机通过 bootin.elf 启动 `/usr/bin/im.elf lazy-global &`。
在 lazy-global 模式下，im.elf 的 THREAD_APP (0x9730) 在遍历第 138 项 (Barrier 8) 后，
通过 0xadac 检查命令行是否含 'a' 参数。因未传 'a'，0xadac 返回 0，导致 sl=0，
直接跳过了第 139 项 (appFwBoot.so) 及后续项，五代 UI 引擎从未被调用，导致看门狗超时硬复位。

本补丁在 0x975e (文件偏移 0x175e) 将 `mov r2, r0` (0x4602) 修改为 `movs r2, #1` (0x2201)。
使 THREAD_APP 在后台基础服务初始化完毕后，以 sl=1 自动加载第 139 项 appFwBoot.so，
顺利拉起五代 ASTRA UI 与 Context 护航。
"""

from pathlib import Path
import hashlib
from capstone import Cs, CS_ARCH_ARM, CS_MODE_THUMB

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "firmware" / "gen4" / "bin" / "im.elf"
OUT = ROOT / "build_out" / "im_patched.elf"

raw = bytearray(SRC.read_bytes())

# 校验原文件偏移 0x175e 处的机器码是否为 02 46 (mov r2, r0)
patch_offset = 0x175e
if raw[patch_offset : patch_offset + 2] != b"\x02\x46":
    raise RuntimeError(f"0x{patch_offset:x} 处机器码非 02 46: {raw[patch_offset : patch_offset + 2].hex()}")

# 修改为 01 22 (movs r2, #1)
raw[patch_offset : patch_offset + 2] = b"\x01\x22"

OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_bytes(raw)

sha = hashlib.sha256(raw).hexdigest()
print(f"[+] 补丁版 im.elf 生成成功: {OUT} ({len(raw)} B)")
print(f"[+] SHA-256: {sha}")

# 反汇编验证 0x9750 到 0x9768
md = Cs(CS_ARCH_ARM, CS_MODE_THUMB)
pc = 0x9750
text_off = 0x1750 # t_offset is 0x0, t_addr is 0x8000
for ins in md.disasm(raw[text_off : text_off + 26], pc):
    print(f"  0x{ins.address:04x}: {ins.mnemonic:8} {ins.op_str}")
