#!/usr/bin/env python3
# /// script
# dependencies = ["pyusb>=1.2.1", "libusb-package>=1.0.26.2"]
# ///
"""稳健极速刷写器（基于 dino2048/zv-e10-toolkit stage2-flash Sparse + Darwin 0x16 长会话）。

核心特性：
  1. **stage2-flash Darwin 0x16 硬件保活**：入口自动解除电源 MCU 约 4 分钟自动复位，彻底消除看门狗断连；
  2. **1 MiB Sparse 分片极速写入（CHUNK_SKIP + CHUNK_RAW）**：
     - 全量 300 MB 镜像仅约 302 次传输（~40 秒完成）；
     - 差量模式合并相邻变动块，仅发送变动区间，几秒完成；
  3. **分区边界硬锁 + WBI1 保护**：
     - 通过 stage2 分区表核对目标分区（默认 nflasha15）起始地址与长度，绝不越界写入 nflasha5 (0x2C000 WBI1)；
  4. **写后自动回读校验（fetch:）**：默认开启，差量逐段比对字节，全量核验整分区 SHA-256。

用法：
  uv run --with pyusb --with libusb-package python 04_tools_and_flash/flash_robust.py --image <path> --dry-run
  uv run --with pyusb --with libusb-package python 04_tools_and_flash/flash_robust.py --image <path> --diff-from <base> --flash
  uv run --with pyusb --with libusb-package python 04_tools_and_flash/flash_robust.py --image <path> --flash --reset-progress
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parent
sys.path.insert(0, str(WORKSPACE))

from toolkit_session import (  # noqa: E402
    MAX_RAW_BYTES_PER_SLICE,
    SPARSE_BLK,
    FatalConfigError,
    ToolkitFastbootClient,
    atomic_save_json,
    format_duration,
    slice_ranges_for_sparse,
)

PROGRESS_FILE = WORKSPACE / "diagnostic" / "flash_robust_progress.json"
PARTITION_START_BLOCK = 0x16C000
FORBIDDEN_SECTOR = 0x2C000  # nflasha5 WBI1 头部，绝对不许写


def load_progress(key: str) -> set[int]:
    if PROGRESS_FILE.exists():
        try:
            d = json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
            if d.get("key") == key:
                return set(d.get("done", []))
        except Exception:
            pass
    return set()


def save_progress(key: str, done: set[int]) -> None:
    atomic_save_json(PROGRESS_FILE, {"key": key, "done": sorted(done)})


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(4 * 1024 * 1024):
            h.update(chunk)
    return h.hexdigest().lower()


def build_slices_for_chunks(
    img_path: Path,
    chunk_indices: list[int],
    chunk_size: int,
    max_slice_bytes: int = MAX_RAW_BYTES_PER_SLICE,
) -> list[tuple[int, bytes, list[int]]]:
    """将待写 chunk 合并为连续区间，并按 stage2 1 MiB 上限切分，同时记录每片覆盖的 chunk 索引。"""
    if not chunk_indices:
        return []
    raw_ranges: list[tuple[int, bytes]] = []
    with img_path.open("rb") as f:
        for idx in sorted(chunk_indices):
            f.seek(idx * chunk_size)
            data = f.read(chunk_size)
            if len(data) != chunk_size:
                raise RuntimeError(f"镜像块 {idx} 读取截断: {len(data)} != {chunk_size}")
            raw_ranges.append((idx * chunk_size, data))

    slices = slice_ranges_for_sparse(raw_ranges, max_slice_bytes=max_slice_bytes)
    out: list[tuple[int, bytes, list[int]]] = []
    for off, data in slices:
        first_idx = off // chunk_size
        last_idx = (off + len(data) - 1) // chunk_size
        covered = [i for i in range(first_idx, last_idx + 1)]
        out.append((off, data, covered))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="ZV-E10 stage2-flash 极速 Sparse 刷写器")
    ap.add_argument("--image", required=True)
    ap.add_argument("--partition", default="nflasha15", help="目标分区名（默认 nflasha15）")
    ap.add_argument(
        "--start-lba",
        default="0x16C000",
        help="期望分区起始 LBA 核对值（默认 0x16C000 = nflasha15）",
    )
    ap.add_argument(
        "--allow-wbi",
        action="store_true",
        help="允许写入 nflasha5 (0x2C000 WBI1 头部)。仅当恢复 WBI1 时使用",
    )
    ap.add_argument("--chunk-kb", type=int, default=256, choices=[64, 128, 256, 512])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--flash", action="store_true")
    ap.add_argument("--reset-progress", action="store_true")
    ap.add_argument("--max-rounds", type=int, default=10, help="异常断开时最多重连轮数")
    ap.add_argument("--range", default="", help="只写块范围，如 160:600（配合 --chunk-kb）")
    ap.add_argument(
        "--ping",
        type=float,
        default=0.0,
        help="兼容保留参数（stage2-flash 已在入口执行 Darwin 0x16 保活，无需周期发心跳）",
    )
    ap.add_argument(
        "--diff-from",
        default="",
        help="只写与基准镜像不同的块（前提：设备当前内容必须等于该基准）",
    )
    ap.add_argument(
        "--no-verify",
        action="store_true",
        help="跳过写入后的 fetch 回读校验（默认执行回读校验）",
    )
    a = ap.parse_args()
    if not (a.dry_run or a.flash):
        ap.error("需 --dry-run 或 --flash")

    expected_start_lba = int(a.start_lba, 16)
    if (a.partition == "nflasha5" or expected_start_lba == FORBIDDEN_SECTOR) and not a.allow_wbi:
        print(
            "[FATAL] 拒绝写入受保护分区 nflasha5 / 扇区 0x2C000（WBI1 头部）。"
            "若确实要恢复 WBI1，请加 --allow-wbi 或使用 restore_wbi_auto.py。"
        )
        return 1

    img = Path(a.image)
    if not img.exists():
        print(f"[FATAL] 镜像不存在: {img}")
        return 1

    chunk_size = a.chunk_kb * 1024
    blocks_per_chunk = chunk_size // 512
    size = img.stat().st_size
    if size == 0 or size % SPARSE_BLK != 0:
        print(f"[FATAL] 镜像大小必须为 {SPARSE_BLK}B 倍数: {size}")
        return 1
    total = size // chunk_size
    if size % chunk_size != 0:
        print(f"[FATAL] 镜像大小 {size} 不是 chunk_size={chunk_size} 的整数倍")
        return 1

    print("=" * 74)
    print("  ZV-E10 极速稳健刷写器 (dino2048 stage2-flash + 1MiB Sparse + Darwin 0x16)")
    print("=" * 74)
    print(f"  目标分区: {a.partition} (期望起始 LBA: 0x{expected_start_lba:06x})")
    print(f"  镜像    : {img.name}  {size:,} B")
    print(f"  块大小  : {a.chunk_kb} KB   共 {total} 块 (Sparse 单片上限 ~{MAX_RAW_BYTES_PER_SLICE // 1024} KiB)")
    print(f"  保护扇区: 0x{FORBIDDEN_SECTOR:06x}（nflasha5 WBI1 头部）{'已解锁' if a.allow_wbi else '永不写入'}")

    if a.reset_progress and PROGRESS_FILE.exists():
        PROGRESS_FILE.unlink()
        print("  [*] 已清除历史进度")

    pkey = f"toolkit:{a.partition}:{img.name}@{a.chunk_kb}k"
    done = load_progress(pkey)
    lo, hi = 0, total
    if a.range:
        lo, hi = (int(x) for x in a.range.split(":"))
        hi = min(hi, total)

    if a.diff_from:
        base = Path(a.diff_from)
        if not base.exists():
            print(f"[FATAL] 基准镜像不存在: {base}")
            return 1
        if base.stat().st_size != size:
            print("[FATAL] 基准镜像大小不一致")
            return 1
        diff: list[int] = []
        with img.open("rb") as f1, base.open("rb") as f2:
            for i in range(total):
                b1 = f1.read(chunk_size)
                b2 = f2.read(chunk_size)
                if lo <= i < hi and b1 != b2:
                    diff.append(i)
        print(f"  差量模式: 与 {base.name} 相比 {len(diff)} 个块不同")
        print("  ⚠️  前提：设备当前内容必须等于该基准镜像，否则结果会是混合状态")
        remaining_chunks = [i for i in diff if i not in done]
    else:
        remaining_chunks = [i for i in range(lo, hi) if i not in done]

    slices = build_slices_for_chunks(img, remaining_chunks, chunk_size)
    print(f"  已完成  : {len(done)} 块    剩余 {len(remaining_chunks)} 块 ({len(slices)} 次 Sparse 传输)")

    if a.dry_run:
        total_mib = len(remaining_chunks) * a.chunk_kb / 1024
        print(f"\n[DRY-RUN] 将写入 {len(remaining_chunks)} 块 = {total_mib:.1f} MiB ({len(slices)} 次 Sparse 传输)")
        print(
            f"[DRY-RUN] 第一块扇区 0x{expected_start_lba:06x}，"
            f"最后一块扇区 0x{expected_start_lba + (total - 1) * blocks_per_chunk:06x}"
        )
        return 0

    if not remaining_chunks:
        print("\n[+] 全部块已写完 —— 本次没有执行任何写入")
        print(f"    原因：进度文件 {PROGRESS_FILE.name} 里已有本镜像（{pkey}）的完整记录。")
        print("    如需强制重刷：加 --reset-progress")
        return 0

    t_start = time.monotonic()
    all_target_slices = list(slices)
    written_total_bytes = 0
    expected_total_bytes = sum(len(data) for _, data, _ in all_target_slices)

    rounds = 0
    while remaining_chunks and rounds < a.max_rounds:
        rounds += 1
        client = ToolkitFastbootClient()
        try:
            client.connect(timeout=60.0, allow_writer=True, verbose=True)
            parts = client.get_partitions()
            if a.partition not in parts:
                raise FatalConfigError(f"分区表中找不到 {a.partition}: {sorted(parts)}")
            part_start, part_size, _ = parts[a.partition]
            if part_start != expected_start_lba * 512:
                raise FatalConfigError(
                    f"分区 {a.partition} 起始偏移不匹配: 设备 0x{part_start:x} != 期望 0x{expected_start_lba * 512:x}"
                )
            if part_size != size:
                raise FatalConfigError(
                    f"分区 {a.partition} 容量不匹配: 设备 {part_size} B != 镜像 {size} B"
                )
            if not a.allow_wbi and part_start <= FORBIDDEN_SECTOR * 512 < part_start + part_size:
                raise FatalConfigError("目标分区覆盖受保护 WBI1 扇区 0x2C000，已中止")

            max_raw_bytes = (
                (client.max_download_size - 52) // SPARSE_BLK
            ) * SPARSE_BLK
            slices = build_slices_for_chunks(
                img, remaining_chunks, chunk_size, max_slice_bytes=max_raw_bytes
            )
            print(
                f"[+] 第 {rounds} 轮连接成功，分区 {a.partition} 校验通过 ({part_size:,} B)，"
                f"开始写入 {len(slices)} 个 Sparse 分片...",
                flush=True,
            )

            for s_idx, (off, data, covered_chunks) in enumerate(slices, 1):
                t0 = time.monotonic()
                client.flash_sparse_slices(a.partition, part_size, [(off, data)])
                dt = time.monotonic() - t0
                for cidx in covered_chunks:
                    done.add(cidx)
                save_progress(pkey, done)
                written_total_bytes += len(data)

                speed = (len(data) / (1024 * 1024)) / dt if dt > 0 else 0.0
                pct = written_total_bytes / expected_total_bytes * 100.0
                rem_bytes = expected_total_bytes - written_total_bytes
                el = time.monotonic() - t_start
                eta = (el / written_total_bytes) * rem_bytes if written_total_bytes > 0 else 0.0
                print(
                    f"  [{s_idx:3d}/{len(slices)} | {pct:5.1f}%] off=0x{off:08x} "
                    f"{len(data) / 1024:6.1f} KiB OKAY ({dt:.2f}s, {speed:5.2f} MB/s) | 剩余 {format_duration(eta)}",
                    flush=True,
                )

            remaining_chunks = []

            if not a.no_verify:
                if a.diff_from or a.range:
                    print(f"[*] 正在执行 write-verify 回读比对 ({len(all_target_slices)} 段)...", flush=True)
                    t_v0 = time.monotonic()
                    for off, expected_data, _ in all_target_slices:
                        actual = client.fetch_range(a.partition, off, len(expected_data))
                        if actual != expected_data:
                            raise RuntimeError(
                                f"回读校验失败: offset=0x{off:08x} len={len(expected_data)}"
                            )
                    print(
                        f"[+] 回读逐字节校验 100% 一致！({len(all_target_slices)} 段, "
                        f"用时 {time.monotonic() - t_v0:.2f}s)",
                        flush=True,
                    )
                else:
                    print("[*] 正在执行整分区 fetch SHA-256 回读核验...", flush=True)
                    t_v0 = time.monotonic()
                    expect_sha = sha256_file(img)
                    actual_sha = client.fetch_sha256(
                        a.partition,
                        part_size,
                        progress_cb=lambda got, tot: (
                            print(f"    [verify] {got * 100.0 / tot:5.1f}% ({got // (1024 * 1024)}/{tot // (1024 * 1024)} MiB)", flush=True)
                            if got == tot or (got % (32 * 1024 * 1024) == 0)
                            else None
                        ),
                    )
                    if actual_sha != expect_sha:
                        raise RuntimeError(f"全盘 SHA-256 不一致: 设备 {actual_sha} != 镜像 {expect_sha}")
                    print(
                        f"[+] 全盘回读 SHA-256 校验 100% 一致: {actual_sha} ({time.monotonic() - t_v0:.1f}s)",
                        flush=True,
                    )

        except FatalConfigError as e:
            print(f"\n[FATAL] {e}", flush=True)
            return 1
        except Exception as e:
            print(f"\n[!] 操作异常或中断: {e}", flush=True)
            remaining_chunks = [i for i in remaining_chunks if i not in done]
            if not remaining_chunks:
                raise
            print(f"[*] 断点已保存，剩余 {len(remaining_chunks)} 块，准备重试...", flush=True)
            time.sleep(1.0)
        finally:
            client.close()

    print("\n" + "=" * 74)
    if remaining_chunks:
        print(f"  ⚠️  未完成，剩余 {len(remaining_chunks)} 块")
        print("     → 重新进 Fastboot 后再跑同一条命令即可续传")
        return 2

    print(
        f"  ✅ 刷写完成：写入 {expected_total_bytes / (1024 * 1024):.2f} MiB，"
        f"总耗时 {format_duration(time.monotonic() - t_start)}"
    )
    print(f"     已使用 stage2-flash 长会话（Darwin 0x16 保活）且未触碰 0x{FORBIDDEN_SECTOR:06x} WBI1 扇区")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    sys.exit(main())
