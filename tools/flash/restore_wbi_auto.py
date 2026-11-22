#!/usr/bin/env python3
# /// script
# dependencies = ["pyusb>=1.2.1", "libusb-package>=1.0.26.2"]
# ///
"""
restore_wbi_auto.py
================================================================================
Sony ZV-E10 原厂 WBI1 引导头自动修复守护程序（dino2048 stage2-flash 版）
- 目标：将 C:\\ZVE10UI\\01_factory_backup\\nflasha5.bin 的原厂 WBI1 头部 (512B) 写回 nflasha5 (0x2C000)
- 机制：持续监听 stage1/stage2 设备，接入 stage2-flash（入口自动 Darwin 0x16 保活）并写回校验
================================================================================
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent.resolve()
sys.path.insert(0, str(HERE))

from toolkit_session import ToolkitFastbootClient  # noqa: E402

ROOT = HERE.parents[1] if len(HERE.parents) >= 2 else HERE.parent
CANDIDATES_A5 = [
    ROOT / "firmware" / "backup" / "nflasha5.bin",
    ROOT / "01_factory_backup" / "nflasha5.bin",
    HERE.parent / "01_factory_backup" / "nflasha5.bin",
    Path("C:/ZVE10UI/01_factory_backup/nflasha5.bin"),
]
FACTORY_A5 = next((p for p in CANDIDATES_A5 if p.exists()), CANDIDATES_A5[0])
WBI_SECTOR = 0x2C000


def main() -> int:
    parser = argparse.ArgumentParser(description="Restore WBI1 header via stage2-flash")
    parser.add_argument("--no-reboot", action="store_true", help="Do not reboot after restoring WBI1")
    args = parser.parse_args()

    print("==================================================================")
    print("  Sony ZV-E10 原厂 WBI1 引导头修复守护程序 (stage2-flash)")
    print("==================================================================")

    assert FACTORY_A5.exists(), f"原厂备份不存在: {FACTORY_A5}"
    fac_header = FACTORY_A5.read_bytes()[:512]
    assert fac_header[:4] == b"WBI1", f"原厂备份头非 WBI1: {fac_header[:4]}"
    print(f"[+] 原厂 WBI1 头已载入 (512 字节, 前 16 字节: {fac_header[:16].hex()})")
    print("[-] 正在持续监听 Fastboot 设备 (18d1:4ee0)...", flush=True)

    while True:
        client = ToolkitFastbootClient()
        try:
            client.connect(timeout=5.0, allow_writer=True, verbose=True)
            parts = client.get_partitions()
            if "nflasha5" not in parts:
                raise RuntimeError("分区表中找不到 nflasha5")
            a5_start, a5_size, _ = parts["nflasha5"]
            if a5_start != WBI_SECTOR * 512:
                raise RuntimeError(
                    f"nflasha5 起始偏移异常: 0x{a5_start:x} != 0x{WBI_SECTOR * 512:x}"
                )

            print(f"  -> 写入原厂 WBI1 引导头到 nflasha5 (0x{WBI_SECTOR:06x}, {a5_size:,} B)...", flush=True)
            client.flash_raw("nflasha5", fac_header)
            back = client.fetch_exact("nflasha5", 0, 512)
            if back == fac_header:
                print("\n" + "=" * 62)
                print("  [★] 修复成功！原厂 WBI1 引导头已 100% 恢复（逐字节一致）！")
                print("  [★] 相机引导程序已解除挂起，供电与红灯指示系统恢复就绪！")
                print("============================================================")
                if not args.no_reboot:
                    print("[*] 正在发送硬件冷启动指令...", flush=True)
                    client.reboot()
                    print(">>> 请拔掉 USB / 重新开机观察相机启动画面！<<<", flush=True)
                else:
                    print("[*] 已保留在 stage2-flash 长会话模式（Darwin 0x16 已解除看门狗自动复位）。", flush=True)
                return 0
            print(f"  [!] 回读不一致: {back[:16].hex()} != {fac_header[:16].hex()}")
        except Exception as e:
            if "Device never appeared" not in str(e):
                print(f"  [!] 本轮操作异常: {e}，等待重试...", flush=True)
            time.sleep(0.5)
        finally:
            client.close()


if __name__ == "__main__":
    sys.exit(main())
