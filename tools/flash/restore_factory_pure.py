#!/usr/bin/env python3
# /// script
# dependencies = ["pyusb>=1.2.1", "libusb-package>=1.0.26.2"]
# ///
"""
restore_factory_pure.py
================================================================================
Sony ZV-E10 官方原厂固件全量 300MB 极速恢复程序（dino2048 stage2-flash 版）
- 恢复目标: nflasha15 (扇区 0x16C000 ~ 0x202000, 300MB)
- 原厂底包: C:\\ZVE10UI\\01_factory_backup\\nflasha15.bin (SHA256: cb15bc410800add6ad605a3433773c7a287703140c85b1b2939192976240bc60)
- 写入引擎: stage2-flash.bin (1 MiB Sparse 分片极速写入 + 入口自动 Darwin 0x16 保活)
- 写后校验: 通过 fetch:nflasha15 全分区回读 SHA-256 逐字节核验
================================================================================
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent.resolve()
sys.path.insert(0, str(HERE))

from toolkit_session import (  # noqa: E402
    MAX_RAW_BYTES_PER_SLICE,
    FatalConfigError,
    ToolkitFastbootClient,
    atomic_save_json,
    format_duration,
)

ROOT = HERE.parents[1] if len(HERE.parents) >= 2 else HERE.parent
CANDIDATES_A15 = [
    ROOT / "firmware" / "backup" / "nflasha15.bin",
    ROOT / "01_factory_backup" / "nflasha15.bin",
    HERE.parent / "01_factory_backup" / "nflasha15.bin",
    Path("C:/ZVE10UI/01_factory_backup/nflasha15.bin"),
]
FACTORY_IMAGE = next((p for p in CANDIDATES_A15 if p.exists()), CANDIDATES_A15[0])
CANDIDATES_A5 = [
    ROOT / "firmware" / "backup" / "nflasha5.bin",
    ROOT / "01_factory_backup" / "nflasha5.bin",
    HERE.parent / "01_factory_backup" / "nflasha5.bin",
    Path("C:/ZVE10UI/01_factory_backup/nflasha5.bin"),
]
FACTORY_A5 = next((p for p in CANDIDATES_A5 if p.exists()), CANDIDATES_A5[0])
PROGRESS_FILE = HERE / "factory_restore_progress.json"

PARTITION_NAME = "nflasha15"
PARTITION_START_BLOCK = 0x16C000
IMAGE_SIZE = 314_572_800
EXPECTED_SHA256 = "cb15bc410800add6ad605a3433773c7a287703140c85b1b2939192976240bc60"


def load_progress() -> set[int]:
    if PROGRESS_FILE.exists():
        try:
            data = json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
            if data.get("engine") == "toolkit_sparse":
                return set(data.get("completed_offsets", []))
        except Exception as e:
            print(f"[WARN] 无法读取进度文件: {e}")
    return set()


def save_progress(completed_offsets: set[int]) -> None:
    atomic_save_json(
        PROGRESS_FILE,
        {"engine": "toolkit_sparse", "completed_offsets": sorted(completed_offsets)},
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Sony ZV-E10 官方原厂固件全量 300MB 极速恢复")
    parser.add_argument("--fresh", action="store_true", help="重置断点，从偏移 0 开始全新刷写")
    parser.add_argument("--skip-wbi", action="store_true", default=True, help="跳过 0x2C000 WBI1 写入（默认跳过）")
    parser.add_argument("--write-wbi", action="store_true", help="强制写回 nflasha5 (0x2C000) WBI1 引导头")
    parser.add_argument("--no-verify", action="store_true", help="跳过刷写后全盘 SHA-256 回读校验")
    parser.add_argument("--ping", type=float, default=0.0, help="兼容保留参数（stage2-flash 已内置 Darwin 0x16 保活）")
    args = parser.parse_args()

    print("==================================================================")
    print("  Sony ZV-E10 官方原厂固件【全量 300MB】极速恢复程序 (stage2-flash)")
    print("==================================================================")

    assert FACTORY_IMAGE.exists(), f"原厂镜像不存在: {FACTORY_IMAGE}"
    img_size = FACTORY_IMAGE.stat().st_size
    assert img_size == IMAGE_SIZE, f"镜像尺寸异常: {img_size} != {IMAGE_SIZE}"

    print("[*] 正在校验官方原厂底包 SHA-256 哈希...")
    h = hashlib.sha256()
    with FACTORY_IMAGE.open("rb") as f:
        while chunk := f.read(4 * 1024 * 1024):
            h.update(chunk)
    actual_hash = h.hexdigest().lower()
    assert actual_hash == EXPECTED_SHA256, f"哈希校验不匹配: {actual_hash} != {EXPECTED_SHA256}"
    print(f"[+] 原厂底包校验通过: {actual_hash}")

    if args.fresh and PROGRESS_FILE.exists():
        PROGRESS_FILE.unlink()
        print("[*] 断点已重置，将从起始偏移 0 开始全新恢复。")

    completed_offsets = load_progress()
    slice_offsets = list(range(0, IMAGE_SIZE, MAX_RAW_BYTES_PER_SLICE))
    remaining_offsets = [off for off in slice_offsets if off not in completed_offsets]

    print(f"[*] Sparse 分片数: {len(slice_offsets)} 片 (每片上限 {MAX_RAW_BYTES_PER_SLICE // 1024} KiB)")
    print(f"[*] 剩余待写入片: {len(remaining_offsets)} 片")

    t_start = time.monotonic()
    while remaining_offsets:
        client = ToolkitFastbootClient()
        try:
            client.connect(timeout=60.0, allow_writer=True, verbose=True)
            parts = client.get_partitions()
            if PARTITION_NAME not in parts:
                raise FatalConfigError(f"分区表中未找到 {PARTITION_NAME}")
            part_start, part_size, _ = parts[PARTITION_NAME]
            if part_start != PARTITION_START_BLOCK * 512 or part_size != IMAGE_SIZE:
                raise FatalConfigError(
                    f"分区 {PARTITION_NAME} 参数异常: start=0x{part_start:x} size={part_size}"
                )

            round_start = time.monotonic()
            with FACTORY_IMAGE.open("rb") as f:
                for idx, off in enumerate(list(remaining_offsets), 1):
                    want = min(MAX_RAW_BYTES_PER_SLICE, IMAGE_SIZE - off)
                    f.seek(off)
                    data = f.read(want)
                    if len(data) != want:
                        raise RuntimeError(f"读取镜像偏移 0x{off:x} 截断")

                    t0 = time.monotonic()
                    client.flash_sparse_slices(PARTITION_NAME, IMAGE_SIZE, [(off, data)])
                    dt = time.monotonic() - t0

                    completed_offsets.add(off)
                    remaining_offsets.pop(0)
                    if len(completed_offsets) % 5 == 0 or not remaining_offsets:
                        save_progress(completed_offsets)

                    done_cnt = len(completed_offsets)
                    pct = done_cnt / len(slice_offsets) * 100.0
                    speed = (want / (1024 * 1024)) / dt if dt > 0 else 0.0
                    el = time.monotonic() - round_start
                    eta = (el / idx) * len(remaining_offsets) if idx > 0 else 0.0
                    if done_cnt % 10 == 0 or not remaining_offsets:
                        print(
                            f"[{done_cnt:3d}/{len(slice_offsets)} | {pct:5.1f}%] off=0x{off:08x} "
                            f"OKAY ({dt:.2f}s, {speed:5.2f} MB/s) | 剩余: {format_duration(eta)}",
                            flush=True,
                        )

            if not args.no_verify:
                print("[*] 正在通过 fetch:nflasha15 执行全分区 SHA-256 回读核验...", flush=True)
                readback_sha = client.fetch_sha256(
                    PARTITION_NAME,
                    IMAGE_SIZE,
                    progress_cb=lambda got, tot: (
                        print(f"    [verify] {got * 100.0 / tot:5.1f}% ({got // (1024 * 1024)}/300 MiB)", flush=True)
                        if got == tot or (got % (32 * 1024 * 1024) == 0)
                        else None
                    ),
                )
                if readback_sha != EXPECTED_SHA256:
                    raise RuntimeError(f"回读校验失败: {readback_sha} != {EXPECTED_SHA256}")
                print(f"[+] 全盘回读 SHA-256 核验 100% 一致: {readback_sha}", flush=True)

            if args.write_wbi and FACTORY_A5.exists():
                print("[*] 正在写回原厂 WBI1 引导头 (nflasha5 @ 0x2C000)...", flush=True)
                a5_start, _, _ = parts["nflasha5"]
                if a5_start != 0x2C000 * 512:
                    raise RuntimeError(f"nflasha5 起始偏移异常: 0x{a5_start:x}")
                fac_wbi = FACTORY_A5.read_bytes()[:512]
                client.flash_raw("nflasha5", fac_wbi)
                back_wbi = client.fetch_exact("nflasha5", 0, 512)
                if back_wbi != fac_wbi:
                    raise RuntimeError("WBI1 回读校验不一致")
                print("[+] 原厂 WBI1 引导头固化并回读校验通过！", flush=True)
            else:
                print("[*] [SAFETY] 已跳过 nflasha5 (0x2C000 WBI1) 写入，保持原厂引导头完好无损。", flush=True)

        except FatalConfigError as e:
            print(f"\n[FATAL] {e}", flush=True)
            return 1
        except Exception as e:
            if completed_offsets:
                save_progress(completed_offsets)
            print(f"\n[!] 通道异常 ({e})，等待重新连接...", flush=True)
            time.sleep(1.0)
        finally:
            client.close()

    print("\n==================================================================")
    print(f" [★] 恭喜！Sony ZV-E10 全量 300MB 原厂固件已 100% 恢复完成 (耗时 {format_duration(time.monotonic() - t_start)})！")
    print("  请拔掉 USB 数据线，取出电池静置 3 秒后重新装入开机。")
    print("==================================================================\n", flush=True)

    if PROGRESS_FILE.exists():
        PROGRESS_FILE.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
