#!/usr/bin/env python3
"""构建完整版 libObjShim.so 并进行 ELF 属性对齐与符号检验"""

import hashlib
import subprocess
import sys
from pathlib import Path
from elftools.elf.elffile import ELFFile

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "libObjShim.S"
OUT_DIR = ROOT / "build_out"
OUT_DIR.mkdir(parents=True, exist_ok=True)
OUT = OUT_DIR / "libObjShim.so"
PY = sys.executable

ZIG = [PY, "-m", "ziglang", "cc"]

CFLAGS = [
    "-target", "arm-linux-gnueabi",
    "-fPIC", "-shared", "-nostdlib", "-O2",
    "-fno-builtin", "-fno-stack-protector", "-fomit-frame-pointer",
    "-fno-unwind-tables", "-fno-asynchronous-unwind-tables", "-fno-exceptions",
    "-Wl,-soname,libObjShim.so",
    "-Wl,--no-undefined",
    "-Wl,-s",
]

TARGET_FLAGS = 0x05000002  # EABI5 | EF_ARM_HASENTRY，与索尼官方固件一致

def main():
    print(f"[1] 交叉编译 {SRC.name} -> {OUT.name}")
    cmd = ZIG + CFLAGS + ["-o", str(OUT), str(SRC)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print("STDOUT:", r.stdout)
        print("STDERR:", r.stderr)
        raise SystemExit("编译失败")
    print(f"    产物生成成功: {OUT.stat().st_size:,} 字节")

    print("[2] 对齐 e_flags 至 0x05000002")
    raw = bytearray(OUT.read_bytes())
    cur = int.from_bytes(raw[36:40], "little")
    if cur != TARGET_FLAGS:
        raw[36:40] = TARGET_FLAGS.to_bytes(4, "little")
        OUT.write_bytes(bytes(raw))
        print(f"    ELF e_flags 对齐: 0x{cur:08x} -> 0x{TARGET_FLAGS:08x}")
    else:
        print(f"    ELF e_flags 已是对齐值 0x{TARGET_FLAGS:08x}")

    print("[3] 验证导出符号")
    with open(OUT, "rb") as f:
        elf = ELFFile(f)
        dynsym = elf.get_section_by_name(".dynsym")
        exports = []
        for s in dynsym.iter_symbols():
            if s.name and s["st_shndx"] != "SHN_UNDEF":
                exports.append(s.name)
        print(f"    导出符号总数: {len(exports)} 个")

    blob = OUT.read_bytes()
    print("[4] 指纹校验")
    print(f"    SHA-256: {hashlib.sha256(blob).hexdigest()}")
    print(f"    MD5:     {hashlib.md5(blob).hexdigest()}")

if __name__ == "__main__":
    main()
