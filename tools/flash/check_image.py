#!/usr/bin/env python3
# /// script
# dependencies = ["pyusb>=1.2.1", "libusb-package>=1.0.26.2"]
# ///
"""判定设备上当前是哪个 nflasha15 镜像：只读 8 个扇区，解析 ext2 超级块。

原理：每个镜像的空闲块数不同，是天然指纹。
  原厂       blocks=242,416  free=10,957
  P1         blocks=242,416  free=10,953
  E19        blocks=242,416  free=7,003
  E18        blocks=307,200  free=76,247

只读，不写任何东西。
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parent
sys.path.insert(0, str(WORKSPACE))

from execute_e2_lite import E2LiteTransport, PARTITION_START, DRIVER_READ

PART_START = PARTITION_START
PART_END = PARTITION_START + 1200 * 512

KNOWN = {
    (242416, 10957): "原厂 nflasha15",
    (242416, 10953): "P1 canary",
    (242416, 7003): "E19 uxprobe",
    (242416, 6995): "E19b dualcanary",
    (307200, 6731): "E20 appfw64",
    (307200, 6722): "E22 stub",
    (307200, 3741): "E23 watchdog",
    (307200, 3739): "E24 no-ctor",
    (307200, 76231): "E25 freemem",
    (307200, 75972): "E27 真机（删四代UI+五代闭包）",
    (307200, 41748): "E26 screenoff",
    (307200, 2880): "E21 providers",
    (307200, 76247): "E18 gen5ui",
    (307200, 73700): "e1_pre_resize（空底座）",
}


def read8(t, sector: int) -> bytes:
    if sector % 8 != 0:
        raise ValueError(f"扇区未对齐: 0x{sector:x}")
    if not (PART_START <= sector < PART_END):
        raise ValueError(f"扇区 0x{sector:06x} 越界")
    t.command(f"oem poke:0xfe320000:0x{sector:08x}")
    t.command("oem poke:0xfe320004:0x00000008")
    t.command("oem call:0xfe300001")
    return t.peek(0xFE310000, 4096)


def main() -> int:
    print("=" * 68)
    print("  设备镜像判定（只读超级块）")
    print("=" * 68)
    t = None
    try:
        t = E2LiteTransport()
        print("[+] Fastboot 已连接")
        t.load_read_driver(DRIVER_READ.read_bytes())
        print("[+] 读驱动就绪")

        buf = read8(t, PART_START)
        sb = buf[1024:2048]
        magic = struct.unpack_from("<H", sb, 56)[0]
        if magic != 0xEF53:
            print(f"[!] 超级块 magic = 0x{magic:04x}，不是 ext2")
            return 2

        inodes = struct.unpack_from("<I", sb, 0)[0]
        blocks = struct.unpack_from("<I", sb, 4)[0]
        free_b = struct.unpack_from("<I", sb, 12)[0]
        free_i = struct.unpack_from("<I", sb, 16)[0]
        bs = 1024 << struct.unpack_from("<I", sb, 24)[0]

        print()
        print(f"  inodes      {inodes:,}")
        print(f"  blocks      {blocks:,}   (文件系统 {blocks*bs/1048576:.0f} MB)")
        print(f"  free blocks {free_b:,}   ({free_b*bs/1048576:.1f} MB)")
        print(f"  free inodes {free_i:,}")
        print()
        name = KNOWN.get((blocks, free_b))
        if name:
            print(f"  ==> 设备上是：{name}")
        else:
            print("  ==> 未匹配已知指纹（可能是其它实验镜像，或文件系统已被改写）")
        return 0
    except Exception as e:
        print(f"[!] 失败: {e}")
        return 1
    finally:
        if t is not None:
            try:
                t.close()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
