#!/usr/bin/env python3
# /// script
# dependencies = ["pyusb>=1.2.1", "libusb-package>=1.0.26.2"]
# ///
"""设备状态快照（dino2048 stage2-flash 版）：一次连接探测两个关键位置与分区表。

  1. nflasha15 超级块 (扇区 0x16C000 起 4096 字节)
     ext2 超级块在分区内偏移 1024，magic 0xEF53 @ +56，blocks_count @ +4
     · 原厂 fs : blocks = 242,416 / 243,264（238 MB）
     · E12b  fs : blocks = 307,200（300 MB）

  2. nflasha5 WBI1 头部 (扇区 0x2C000 起 512 字节)
"""
from __future__ import annotations

import struct
import sys
import time
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parent
sys.path.insert(0, str(WORKSPACE))

from toolkit_session import ToolkitFastbootClient  # noqa: E402

ROOT = WORKSPACE.parents[1] if len(WORKSPACE.parents) >= 2 else WORKSPACE.parent
CANDIDATES_A5 = [
    ROOT / "firmware" / "backup" / "nflasha5.bin",
    ROOT / "01_factory_backup" / "nflasha5.bin",
    WORKSPACE.parent / "01_factory_backup" / "nflasha5.bin",
    Path("C:/ZVE10UI/01_factory_backup/nflasha5.bin"),
]
FACTORY_A5 = next((p for p in CANDIDATES_A5 if p.exists()), CANDIDATES_A5[0])


def attempt(n: int) -> bool:
    print(f"\n--- 第 {n} 次探测 ---", flush=True)
    client = ToolkitFastbootClient()
    try:
        client.connect(timeout=20.0, allow_writer=True, verbose=True)
        parts = client.get_partitions()
        a15 = parts.get("nflasha15")
        a5 = parts.get("nflasha5")
        print(f"  stage2 identity : {client.identity}")
        if a15:
            print(f"  nflasha15       : start=0x{a15[0]:08x} size={a15[1]:,} B")
        if a5:
            print(f"  nflasha5 (WBI1) : start=0x{a5[0]:08x} size={a5[1]:,} B")

        # ---- 1. nflasha15 超级块 ----
        print("\n  [1] fetch nflasha15 超级块 (0x16C000)...", flush=True)
        blk = client.fetch_exact("nflasha15", 0, 4096)
        magic = struct.unpack_from("<H", blk, 1024 + 56)[0]
        blocks = struct.unpack_from("<I", blk, 1024 + 4)[0]
        inodes = struct.unpack_from("<I", blk, 1024 + 0)[0]
        state = struct.unpack_from("<H", blk, 1024 + 58)[0]
        print(f"      magic = 0x{magic:04x}  {'✅ ext2' if magic == 0xEF53 else '❌ 不是 ext2'}")
        print(f"      blocks = {blocks:,}   inodes = {inodes:,}   state = {state}")
        if blocks == 242_416:
            print("      → ✅ 原厂 dump 文件系统（238 MB，设备真实原厂值）")
        elif blocks == 243_264:
            print("      → ⚠️ unpacked 镜像的文件系统（238 MB，非设备原厂 dump）")
        elif blocks == 307_200:
            print("      → E12b/ASTRA 系文件系统（300 MB）")
        elif blocks == 0:
            print("      → ❌ 超级块全零，文件系统被破坏")
        else:
            print("      → ⚠️ 未知尺寸")

        # ---- 2. WBI1 ----
        print("\n  [2] fetch nflasha5 WBI1 头部 (0x2C000)...", flush=True)
        wbi = client.fetch_exact("nflasha5", 0, 512)
        fac = FACTORY_A5.read_bytes()[:512] if FACTORY_A5.exists() else b""
        print(f"      前 32 字节: {wbi[:32].hex()}")
        asc = "".join(chr(c) if 32 <= c < 127 else "." for c in wbi[:32])
        print(f"             ascii: {asc}")
        print(f"             非零 : {sum(1 for c in wbi if c)}/512")
        print(
            f"      magic = {wbi[:4]!r}  "
            f"{'✅ WBI1 完好' if wbi[:4] == b'WBI1' else '❌ 已丢失'}"
        )
        print(f"      与原厂一致: {wbi == fac}")
        print("\n  " + "=" * 58)
        return True
    except Exception as e:
        print(f"  [失败] {type(e).__name__}: {e}", flush=True)
        return False
    finally:
        client.close()


if __name__ == "__main__":
    for i in range(1, 4):
        if attempt(i):
            sys.exit(0)
        time.sleep(2)
    print("\n三次尝试均失败")
    sys.exit(1)
