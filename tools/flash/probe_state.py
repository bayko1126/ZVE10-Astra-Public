#!/usr/bin/env python3
# /// script
# dependencies = ["pyusb>=1.2.1", "libusb-package>=1.0.26.2"]
# ///
"""设备状态快照：一次连接读两个关键位置。

  1. 扇区 0x16C000 起的 4096 字节 → nflasha15 的超级块
     ext2 超级块在分区内偏移 1024，magic 0xEF53 @ +56，blocks_count @ +4
     · 原厂 fs : blocks = 243,264（238 MB）
     · E12b  fs : blocks = 307,200（300 MB）
     据此判断 nflasha15 到底是原厂、E12b，还是被写成了混合状态。

  2. 扇区 0x2C000 起的 512 字节 → nflasha5 的 WBI1 头部
"""
from __future__ import annotations

import re
import struct
import sys
import time
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parent
sys.path.insert(0, str(WORKSPACE))

import usb.core
import usb.util
import libusb_package

STAGE2 = WORKSPACE / "stage2_turbo_cmd25.bin"
ROOT = WORKSPACE.parents[1]
FACTORY_A5 = ROOT / "firmware" / "backup" / "nflasha5.bin"

PART_START = 0x16C000
WBI_SECTOR = 0x2C000
LINE = re.compile(r"([0-9a-fA-F]{8}): ((?:[0-9a-fA-F]{8})(?: [0-9a-fA-F]{8}){0,3})")


def parse_peek(info, start, size):
    out = bytearray()
    for raw in info:
        m = LINE.fullmatch(raw.decode("ascii", "replace"))
        if m is None:
            raise RuntimeError(f"无效 peek 行: {raw!r}")
        if int(m.group(1), 16) != start + len(out):
            raise RuntimeError("地址不连续")
        for w in m.group(2).split():
            out.extend(struct.pack("<I", int(w, 16)))
    if len(out) != size:
        raise RuntimeError(f"peek 长度 {len(out)} != {size}")
    return bytes(out)


class Dev:
    def __init__(self):
        self.dev = self.out = self.inn = None

    def open(self, timeout=25):
        backend = libusb_package.get_libusb1_backend()
        t0 = time.time()
        while time.time() - t0 < timeout:
            d = usb.core.find(idVendor=0x18D1, idProduct=0x4EE0, backend=backend)
            if d is not None:
                try:
                    try:
                        d.set_configuration()
                    except Exception:
                        pass
                    cfg = d.get_active_configuration()
                    intf = cfg[(0, 0)]
                    usb.util.claim_interface(d, intf)
                    self.dev = d
                    self.out = usb.util.find_descriptor(
                        intf, custom_match=lambda e: usb.util.endpoint_direction(
                            e.bEndpointAddress) == usb.util.ENDPOINT_OUT)
                    self.inn = usb.util.find_descriptor(
                        intf, custom_match=lambda e: usb.util.endpoint_direction(
                            e.bEndpointAddress) == usb.util.ENDPOINT_IN)
                    return True
                except Exception:
                    try:
                        usb.util.dispose_resources(d)
                    except Exception:
                        pass
            time.sleep(0.3)
        return False

    def close(self):
        if self.dev is not None:
            try:
                usb.util.dispose_resources(self.dev)
            except Exception:
                pass
            self.dev = None

    def cmd(self, c, timeout=8000, keep_info=False):
        if isinstance(c, str):
            c = c.encode()
        self.out.write(c, timeout=timeout)
        info = []
        while True:
            r = bytes(self.inn.read(64, timeout=timeout))
            tag, body = r[:4], r[4:]
            if tag == b"INFO":
                info.append(body)
                continue
            return (tag.decode("latin1"), body, info) if keep_info else (tag.decode("latin1"), body)

    def download(self, blob, timeout=8000):
        st, body = self.cmd(f"download:{len(blob):08x}", timeout)
        if st != "DATA":
            raise RuntimeError(f"download 被拒: {st}")
        sent = 0
        while sent < len(blob):
            self.out.write(blob[sent:sent + 16384], timeout=timeout)
            sent += 16384
        while True:
            r = bytes(self.inn.read(64, timeout=timeout))
            if r[:4] == b"INFO":
                continue
            if r[:4] != b"OKAY":
                raise RuntimeError(f"数据阶段失败: {r[:4]}")
            return

    def read_sector(self, sector: int, size: int) -> bytes:
        self.cmd(f"oem poke:0xfe320000:0x{sector:08x}")
        self.cmd(f"oem poke:0xfe320004:0x{size // 512:08x}")
        st, _ = self.cmd("oem call:0xfe300001")
        if st != "OKAY":
            raise RuntimeError("读失败")
        st, body, info = self.cmd(f"oem peek:0xfe310000:{size:x}", keep_info=True)
        if st != "OKAY" or body:
            raise RuntimeError(f"peek 失败: {st}")
        return parse_peek(info, 0xFE310000, size)


def attempt(n: int) -> bool:
    print(f"\n--- 第 {n} 次尝试 ---", flush=True)
    d = Dev()
    try:
        if not d.open(20):
            print("  未找到设备", flush=True)
            return False
        print("  已连接", flush=True)
        d.download(STAGE2.read_bytes())
        d.cmd("oem load:0xfe300000")
        d.cmd(f"oem poke:0xfe320000:0x{PART_START:08x}")
        d.cmd("oem poke:0xfe320004:0x00000001")
        st, _ = d.cmd("oem call:0xfe300001")
        if st != "OKAY":
            raise RuntimeError("eMMC init 失败")
        d.cmd("oem call:0xfe300601")
        print("  eMMC 就绪", flush=True)

        # ---- 1. nflasha15 超级块 ----
        print("\n  [1] 读 nflasha15 超级块 (0x16C000)...", flush=True)
        blk = d.read_sector(PART_START, 4096)
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
            print("      → E12b 系文件系统（300 MB）")
        elif blocks == 0:
            print("      → ❌ 超级块全零，文件系统被破坏")
        else:
            print("      → ⚠️ 未知尺寸")

        # ---- 2. WBI1 ----
        print("\n  [2] 读 nflasha5 WBI1 头部 (0x2C000)...", flush=True)
        wbi = d.read_sector(WBI_SECTOR, 512)
        fac = FACTORY_A5.read_bytes()[:512]
        print(f"      前 32 字节: {wbi[:32].hex()}")
        asc = "".join(chr(c) if 32 <= c < 127 else "." for c in wbi[:32])
        print(f"             ascii: {asc}")
        print(f"             非零 : {sum(1 for c in wbi if c)}/512")
        print(f"      magic = {wbi[:4]!r}  "
              f"{'✅ WBI1 完好' if wbi[:4] == b'WBI1' else '❌ 已丢失'}")
        print(f"      与原厂一致: {wbi == fac}")
        print("\n  " + "=" * 58)
        return True
    except Exception as e:
        print(f"  [失败] {type(e).__name__}: {e}", flush=True)
        return False
    finally:
        d.close()


if __name__ == "__main__":
    for i in range(1, 4):
        if attempt(i):
            sys.exit(0)
        time.sleep(2)
    print("\n三次尝试均失败")
    sys.exit(1)
