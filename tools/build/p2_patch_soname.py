#!/usr/bin/env python3
"""按 DT_SONAME 精确改写 SONAME，用于让五代库与四代同名库并存。

只改 .dynstr 中 SONAME 指向的那一段字符串，不动任何其它字节。
新名字长度必须 <= 原名长度。
"""
import io
import struct
import sys
from pathlib import Path

from elftools.elf.elffile import ELFFile

ROOT = Path(__file__).resolve().parents[2]
G5 = ROOT / "firmware" / "gen5" / "lib"
OUT = G5

# 源文件 -> (输出名, 新 SONAME)
JOBS = [
    ("libObj_g5.so", "libOb5.so", "libOb5.so"),          # 9 <= 9
    ("libmpr_g5.so", "libmp5.so", "libmp5.so"),          # 9 <= 9
    ("libInfraFsys_g5.so", "libFsys5.so", "libFsys5.so"),  # 11 <= 15
]


def find_soname_offset(blob):
    """返回 (.dynstr 文件偏移, SONAME 字符串在 .dynstr 内的偏移, 原字符串)"""
    e = ELFFile(io.BytesIO(blob))
    dynstr = e.get_section_by_name(".dynstr")
    if dynstr is None:
        raise SystemExit("没有 .dynstr")
    soname_idx = None
    for seg in e.iter_segments():
        if seg["p_type"] == "PT_DYNAMIC":
            for t in seg.iter_tags():
                if t.entry.d_tag == "DT_SONAME":
                    soname_idx = t.entry.d_val
    if soname_idx is None:
        raise SystemExit("没有 DT_SONAME")
    base = dynstr["sh_offset"]
    raw = dynstr.data()
    s = raw[soname_idx:].split(b"\x00")[0]
    return base, soname_idx, s


def main():
    for src_name, out_name, new_soname in JOBS:
        src = G5 / src_name
        if not src.exists():
            raise SystemExit(f"缺少 {src}")
        blob = bytearray(src.read_bytes())
        base, idx, old = find_soname_offset(blob)
        new = new_soname.encode()
        if len(new) > len(old):
            raise SystemExit(f"{src_name}: 新名 {new!r} 比原名 {old!r} 长，无法原地替换")
        off = base + idx
        # 用新名覆盖，剩余字节补零（保持 NUL 结尾，不影响后续字符串）
        blob[off:off + len(old)] = new + b"\x00" * (len(old) - len(new))
        (OUT / out_name).write_bytes(bytes(blob))

        # 校验
        e = ELFFile(io.BytesIO(bytes(blob)))
        got = None
        for seg in e.iter_segments():
            if seg["p_type"] == "PT_DYNAMIC":
                for t in seg.iter_tags():
                    if t.entry.d_tag == "DT_SONAME":
                        got = t.soname
        print(f"  {src_name:20s} {old.decode():18s} -> {got:14s} "
              f"{len(blob):>9,} B  {'OK' if got == new_soname else 'FAIL'}")


if __name__ == "__main__":
    main()
