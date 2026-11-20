#!/usr/bin/env python3
# /// script
# dependencies = ["pyusb>=1.2.1", "libusb-package>=1.0.26.2"]
# ///
"""
restore_wbi_auto.py
================================================================================
Sony ZV-E10 原厂 WBI1 引导头自动修复守护程序
- 目标：将 firmware/backup/nflasha5.bin 的原厂 WBI1 头部 (512B) 写回 0x2C000
- 解决：因 0x2C000 被误置零导致 Bootloader 签名校验失败、插电池红灯不亮的硬件挂起状态
- 机制：不限时持续监听 Fastboot 设备 (18d1:4ee0)，一旦 Pico 注入成功立即毫秒级恢复
================================================================================
"""

import sys
import os
import time
import re
import struct
from pathlib import Path

HERE = Path(__file__).parent.resolve()
ROOT = HERE.parents[1]
FACTORY_A5 = ROOT / "firmware" / "backup" / "nflasha5.bin"
STAGE2 = HERE / "stage2_turbo_cmd25.bin"

WBI_SECTOR = 0x2C000
PART_START = 0x16C000

try:
    import usb.core
    import usb.util
    import libusb_package
except ImportError:
    sys.exit("请使用: uv run --with pyusb --with libusb-package python tools/flash/restore_wbi_auto.py")

sys.path.insert(0, str(HERE))
try:
    from fastboot_keepalive import FastbootKeepaliveManager
except ImportError:
    FastbootKeepaliveManager = None

LINE = re.compile(r"([0-9a-fA-F]{8}): ((?:[0-9a-fA-F]{8})(?: [0-9a-fA-F]{8}){0,3})")

def parse_peek(info, start, size):
    out = bytearray()
    for raw in info:
        line = raw.decode("ascii", "replace") if isinstance(raw, bytes) else raw
        m = LINE.fullmatch(line)
        if m is None:
            continue
        addr = int(m.group(1), 16)
        if addr != start + len(out):
            continue
        for w in m.group(2).split():
            out.extend(struct.pack("<I", int(w, 16)))
    if len(out) != size:
        raise RuntimeError(f"peek 长度不符合预期: {len(out)} != {size}")
    return bytes(out)

class FastbootDev:
    def __init__(self):
        self.dev = None
        self.out_ep = None
        self.in_ep = None

    def connect(self):
        backend = libusb_package.get_libusb1_backend()
        dev = usb.core.find(idVendor=0x18d1, idProduct=0x4ee0, backend=backend)
        if dev is None:
            return False
        try:
            try:
                dev.set_configuration()
            except Exception:
                pass
            cfg = dev.get_active_configuration()
            intf = cfg[(0, 0)]
            usb.util.claim_interface(dev, intf)
            self.dev = dev
            self.out_ep = usb.util.find_descriptor(
                intf,
                custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT
            )
            self.in_ep = usb.util.find_descriptor(
                intf,
                custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN
            )
            return True
        except Exception:
            return False

    def close(self):
        if self.dev:
            try:
                usb.util.dispose_resources(self.dev)
            except Exception:
                pass
            self.dev = None

    def cmd(self, c, keep_info=False):
        if isinstance(c, str):
            c = c.encode("ascii")
        self.out_ep.write(c, timeout=5000)
        info = []
        while True:
            resp = bytes(self.in_ep.read(64, timeout=5000))
            tag, body = resp[:4].decode("latin1"), resp[4:]
            if tag == "INFO":
                info.append(body.decode("utf-8", "replace"))
                continue
            if keep_info:
                return tag, body, info
            return tag, body

    def download(self, data):
        st, msg = self.cmd(f"download:{len(data):08x}")
        if st != "DATA":
            raise RuntimeError(f"Download 拒绝: {st} {msg}")
        self.out_ep.write(data, timeout=5000)
        resp = bytes(self.in_ep.read(64, timeout=5000))
        if resp[:4] != b"OKAY":
            raise RuntimeError(f"Data phase 失败: {resp}")

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Restore WBI1 header")
    parser.add_argument("--no-reboot", action="store_true", help="Do not reboot after restoring WBI1")
    args = parser.parse_args()

    print("==================================================================")
    print("  Sony ZV-E10 原厂 WBI1 引导头修复守护程序")
    print("==================================================================")

    assert FACTORY_A5.exists(), f"原厂备份不存在: {FACTORY_A5}"
    assert STAGE2.exists(), f"驱动不存在: {STAGE2}"

    fac_header = FACTORY_A5.read_bytes()[:512]
    assert fac_header[:4] == b"WBI1", f"原厂备份头非 WBI1: {fac_header[:4]}"
    print(f"[+] 原厂 WBI1 头已载入 (512 字节, 前 16 字节: {fac_header[:16].hex()})")

    stage2_data = STAGE2.read_bytes()
    fb = FastbootDev()

    print("[-] 正在持续监听 Fastboot 设备 (18d1:4ee0)... 请插入电池或重新通电！", flush=True)

    dots = 0
    while True:
        if fb.connect():
            print("\n[+] 成功检测并捕获到 Fastboot 设备！", flush=True)
            try:
                print("  -> 加载 CMD25 驱动...", flush=True)
                fb.download(stage2_data)
                fb.cmd("oem load:0xfe300000")
                fb.cmd(f"oem poke:0xfe320000:0x{PART_START:08x}")
                fb.cmd("oem poke:0xfe320004:0x00000001")
                st, _ = fb.cmd("oem call:0xfe300001")
                if st != "OKAY":
                    raise RuntimeError("eMMC init 失败")
                fb.cmd("oem call:0xfe300601")

                print(f"  -> 写入原厂 WBI1 引导头到扇区 0x{WBI_SECTOR:06x}...", flush=True)
                fb.download(fac_header)
                fb.cmd("oem load:0xfe400000")
                fb.cmd(f"oem poke:0xfe320000:0x{WBI_SECTOR:08x}")
                fb.cmd("oem poke:0xfe320004:0x00000001")
                st, _ = fb.cmd("oem call:0xfe300701")
                if st != "OKAY":
                    raise RuntimeError("写 WBI 失败")

                print("  -> 执行物理回读严格校验...", flush=True)
                fb.cmd(f"oem poke:0xfe320000:0x{WBI_SECTOR:08x}")
                fb.cmd("oem poke:0xfe320004:0x00000001")
                fb.cmd("oem call:0xfe300001")
                st, _, info = fb.cmd("oem peek:0xfe310000:200", keep_info=True)
                back = parse_peek(info, 0xFE310000, 512)

                if back == fac_header:
                    print("\n" + "=" * 62)
                    print("  [★] 修复成功！原厂 WBI1 引导头已 100% 恢复（逐字节一致）！")
                    print("  [★] 相机引导程序已解除挂起，供电与红灯指示系统恢复就绪！")
                    print("============================================================")
                    if not args.no_reboot:
                        try:
                            print("[*] 正在发送硬件冷启动指令...", flush=True)
                            fb.cmd("reboot")
                        except Exception:
                            pass
                        print(">>> 请拔掉 USB / 重新开机观察相机启动画面！<<<", flush=True)
                    else:
                        print("[*] 保留在 Fastboot 模式，准备提取现场日志...", flush=True)
                        if FastbootKeepaliveManager:
                            print("[*] 正在启动会话保活循环 (oem darwin, 间隔 30s)，防止看门狗超时复位... (按 Ctrl+C 安全退出)")
                            km = FastbootKeepaliveManager(interval=30.0)
                            km.reset_session()
                            km.ping(lambda c: fb.cmd(c, keep_info=True), verbose=True)
                            try:
                                while True:
                                    time.sleep(1.0)
                                    km.ping_if_due(lambda c: fb.cmd(c, keep_info=True), verbose=True)
                            except KeyboardInterrupt:
                                print("\n[*] 退出 Fastboot 保活维持。")
                    return 0
                else:
                    print(f"  [!] 回读不一致: {back[:16].hex()} != {fac_header[:16].hex()}")

            except Exception as e:
                print(f"  [!] 本轮操作异常: {e}，等待重新尝试...", flush=True)
                fb.close()
            time.sleep(1)
        else:
            time.sleep(0.3)
            dots += 1
            if dots % 10 == 0:
                print(".", end="", flush=True)

if __name__ == "__main__":
    sys.exit(main())
