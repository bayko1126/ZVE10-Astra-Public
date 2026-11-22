#!/usr/bin/env python3
"""基于 dino2048/zv-e10-toolkit (stage1 + stage2-flash) 的统一高速 Fastboot 会话层。

核心能力：
1. 自动完成 stage1 ABI 指纹核验并加载 stage2-flash.bin（入口自动执行 Darwin 0x16 硬件保活，彻底消除 ~4 分钟看门狗复位）；
2. 若设备已在运行 stage2-flash 长会话，自动识别并直接复用；若运行旧版 fastboot_zve10.bin，给出明确的中文重注提示；
3. 提供标准 libsparse (CHUNK_SKIP + CHUNK_RAW) 1 MiB 分片高速写入、区间合并与 fetch: 高速回读校验；
4. 自动兼容 Windows libusb-package 后端。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import struct
import sys
import time
from pathlib import Path
from typing import Callable, Iterable, Optional

HERE = Path(__file__).resolve().parent
TOOLKIT_ROOT = HERE / "toolkit_fastboot"
sys.path.insert(0, str(TOOLKIT_ROOT / "host"))
sys.path.insert(0, str(TOOLKIT_ROOT / "tools"))

import bootstrap as bs  # noqa: E402
import check_abi  # noqa: E402

SPARSE_MAGIC = 0xED26FF3A
SPARSE_BLK = 4096
SPARSE_HDR_SIZE = 28
SPARSE_CHUNK_HDR_SIZE = 12
CHUNK_RAW = 0xCAC1
CHUNK_FILL = 0xCAC2
CHUNK_SKIP = 0xCAC3

DL_MAX = 0x100000  # stage1/stage2 1 MiB DMA staging buffer
# 每片预留 28B 文件头 + 12B SKIP + 12B RAW = 52B，最大容纳 255 个 4KB 块 (1,044,480 B <= 1 MiB)
MAX_RAW_BLKS_PER_SLICE = (DL_MAX - SPARSE_HDR_SIZE - 2 * SPARSE_CHUNK_HDR_SIZE) // SPARSE_BLK
MAX_RAW_BYTES_PER_SLICE = MAX_RAW_BLKS_PER_SLICE * SPARSE_BLK
FETCH_BATCH_BYTES = 4 * 1024 * 1024  # 每次 fetch: 最多 4 MiB，避免单次 USB 事务过长
USB_WRITE_CHUNK = 32 * 1024

STAGE2_DEFAULT_BIN = TOOLKIT_ROOT / "prebuilt" / "stage2-flash.bin"
PART_LINE_RE = re.compile(
    r"^(\S+)\s+start=([0-9a-fA-F]{8})\s+size=([0-9a-fA-F]{8})\s+dev=([0-9a-fA-F]{8})\s*$"
)


def atomic_save_json(path: Path, payload: dict) -> None:
    """带 Windows 文件锁重试的原子 JSON 写入，避免杀毒软件/索引器短时占用触发 USB 重连。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    text = json.dumps(payload)
    tmp.write_text(text, encoding="utf-8")
    for attempt in range(8):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt < 7:
                time.sleep(0.015 * (attempt + 1))
    # 若杀软持续锁定目标文件，回退直接覆写（仍失败则放弃本次落盘而不中断 USB 刷写）
    try:
        path.write_text(text, encoding="utf-8")
    except OSError:
        pass
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def format_duration(seconds: float) -> str:
    m, s = divmod(int(max(0, seconds)), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}小时{m:02d}分{s:02d}秒"
    if m > 0:
        return f"{m}分{s:02d}秒"
    return f"{s}秒"


def merge_ranges(ranges: Iterable[tuple[int, bytes]]) -> list[tuple[int, bytes]]:
    """将按字节偏移给出的连续数据块合并为尽量少的连续区间。"""
    sorted_ranges = sorted((off, data) for off, data in ranges if data)
    if not sorted_ranges:
        return []
    merged: list[tuple[int, bytearray]] = []
    for off, data in sorted_ranges:
        if merged and merged[-1][0] + len(merged[-1][1]) == off:
            merged[-1][1].extend(data)
        elif merged and off < merged[-1][0] + len(merged[-1][1]):
            raise ValueError(f"写入区间发生重叠: 0x{off:x} < 0x{merged[-1][0] + len(merged[-1][1]):x}")
        else:
            merged.append((off, bytearray(data)))
    return [(off, bytes(buf)) for off, buf in merged]


def slice_ranges_for_sparse(
    ranges: Iterable[tuple[int, bytes]],
    max_slice_bytes: int = MAX_RAW_BYTES_PER_SLICE,
) -> list[tuple[int, bytes]]:
    """合并区间并按 stage2 1 MiB 缓冲区上限切分为 4KB 对齐的写入分片。"""
    if max_slice_bytes <= 0 or max_slice_bytes % SPARSE_BLK != 0:
        raise ValueError(f"max_slice_bytes 必须为 {SPARSE_BLK} 的正整数倍: {max_slice_bytes}")
    slices: list[tuple[int, bytes]] = []
    for off, data in merge_ranges(ranges):
        if off % SPARSE_BLK != 0 or len(data) % SPARSE_BLK != 0:
            raise ValueError(
                f"Sparse 分片必须按 {SPARSE_BLK}B 对齐: offset=0x{off:x}, len={len(data)}"
            )
        pos = 0
        while pos < len(data):
            piece = data[pos : pos + max_slice_bytes]
            slices.append((off + pos, piece))
            pos += len(piece)
    return slices


def build_sparse_slice(total_partition_bytes: int, byte_offset: int, data: bytes) -> bytes:
    """构造单次可写入 stage2-flash.bin 的 libsparse 镜像（带 SKIP 偏移 + RAW 数据）。"""
    if total_partition_bytes <= 0 or total_partition_bytes % SPARSE_BLK != 0:
        raise ValueError(f"分区大小必须为 {SPARSE_BLK}B 倍数: {total_partition_bytes}")
    if byte_offset < 0 or byte_offset % SPARSE_BLK != 0:
        raise ValueError(f"写入偏移必须为 {SPARSE_BLK}B 倍数: {byte_offset}")
    if not data or len(data) % SPARSE_BLK != 0:
        raise ValueError(f"写入数据必须非空且为 {SPARSE_BLK}B 倍数: {len(data)}")
    if byte_offset + len(data) > total_partition_bytes:
        raise ValueError(
            f"写入越界: 0x{byte_offset:x} + 0x{len(data):x} > 0x{total_partition_bytes:x}"
        )

    total_blks = total_partition_bytes // SPARSE_BLK
    skip_blks = byte_offset // SPARSE_BLK
    raw_blks = len(data) // SPARSE_BLK
    total_chunks = 2 if skip_blks > 0 else 1

    parts = [
        struct.pack(
            "<IHHHHIIII",
            SPARSE_MAGIC,
            1,
            0,
            SPARSE_HDR_SIZE,
            SPARSE_CHUNK_HDR_SIZE,
            SPARSE_BLK,
            total_blks,
            total_chunks,
            0,
        )
    ]
    if skip_blks > 0:
        parts.append(struct.pack("<HHII", CHUNK_SKIP, 0, skip_blks, SPARSE_CHUNK_HDR_SIZE))
    parts.append(
        struct.pack("<HHII", CHUNK_RAW, 0, raw_blks, SPARSE_CHUNK_HDR_SIZE + len(data))
    )
    parts.append(data)
    blob = b"".join(parts)
    if len(blob) > DL_MAX:
        raise ValueError(f"Sparse 分片超出 stage2 1 MiB 缓冲区上限: {len(blob)} > {DL_MAX}")
    return blob


class FatalConfigError(RuntimeError):
    """不可通过重连解决的致命配置或 ABI 不匹配错误（如旧版 stage1 / 分区不匹配）。"""


class ToolkitFastbootClient:
    """封装 stage1->stage2-flash 接管、1 MiB Sparse 刷写与 fetch 回读校验。"""

    def __init__(self, stage2_path: Path = STAGE2_DEFAULT_BIN):
        self.stage2_path = Path(stage2_path)
        self.dev: Optional[bs.Device] = None
        self.identity: str = ""
        self.reused_stage2: bool = False
        self.max_download_size: int = DL_MAX

    def connect(
        self,
        timeout: float = 60.0,
        allow_writer: bool = True,
        verbose: bool = True,
    ) -> None:
        stage1_blob = check_abi.verify(build=False)
        stage2_blob = self.stage2_path.read_bytes()
        identity = check_abi.verify_stage2(
            stage2_blob,
            self.stage2_path.with_suffix(".elf"),
            allow_writer=allow_writer,
        )

        if verbose:
            print("[-] 正在等待 Sony ZV-E10 Fastboot 设备 (18d1:4ee0)...", flush=True)
        usb_dev = bs.wait_for_device(timeout)
        d = bs.Device(usb_dev)
        try:
            tag, body, _ = d.command("getvar:version-bootloader")
            if tag == b"OKAY" and body == identity.encode("ascii"):
                bs.verify_running_stage2(d, identity, allow_writer=allow_writer)
                self.reused_stage2 = True
                if verbose:
                    print(f"[+] 检测到相机已处于长会话 {identity}，直接复用连接", flush=True)
            else:
                try:
                    bs.handoff(d, stage2_blob, stage1_blob, identity, allow_writer=allow_writer)
                except ValueError as err:
                    msg = str(err)
                    if "Running stage 1 does not match" in msg or "Unexpected stage 1" in msg:
                        raise FatalConfigError(
                            "相机当前运行的是旧版或非配对的 stage1（例如 fastboot_zve10.bin）。"
                            "请断电或执行 reset_only.py 后，使用 04_tools_and_flash/hardware/stage1.bin 重新串口注入！"
                        ) from err
                    raise
                self.reused_stage2 = False
                if verbose:
                    print(
                        f"[+] 已加载 {identity}（Darwin 0x16 硬件看门狗保活已激活，长会话不断线）",
                        flush=True,
                    )

            max_dl_raw = self.getvar("max-download-size", _dev=d)
            if max_dl_raw:
                self.max_download_size = int(max_dl_raw, 16)
            self.dev = d
            self.identity = identity
        except Exception:
            d.close()
            raise

    def close(self) -> None:
        if self.dev is not None:
            try:
                self.dev.close()
            finally:
                self.dev = None

    def _require_dev(self) -> bs.Device:
        if self.dev is None:
            raise RuntimeError("ToolkitFastbootClient 尚未连接")
        return self.dev

    def getvar(self, name: str, _dev: Optional[bs.Device] = None) -> str:
        d = _dev or self._require_dev()
        body, _ = d.require(f"getvar:{name}")
        return body.decode("ascii", errors="replace")

    def get_partition_size(self, partition: str) -> int:
        raw = self.getvar(f"partition-size:{partition}")
        return int(raw, 16)

    def get_partitions(self) -> dict[str, tuple[int, int, int]]:
        """执行 oem partition 并返回 {name: (start_bytes, size_bytes, dev)}。"""
        d = self._require_dev()
        _, infos = d.require("oem partition")
        parts: dict[str, tuple[int, int, int]] = {}
        for line in infos:
            m = PART_LINE_RE.match(line.strip())
            if m:
                parts[m.group(1)] = (
                    int(m.group(2), 16),
                    int(m.group(3), 16),
                    int(m.group(4), 16),
                )
        return parts

    def download(self, blob: bytes, timeout: int = 15000) -> None:
        d = self._require_dev()
        if not blob or len(blob) > self.max_download_size:
            raise ValueError(f"download 长度非法: {len(blob)} (上限 {self.max_download_size})")
        tag, body, _ = d.command(f"download:{len(blob):08x}")
        if tag != b"DATA" or body != f"{len(blob):08x}".encode("ascii"):
            raise RuntimeError(f"download 被拒: {tag!r} {body!r}")
        off = 0
        while off < len(blob):
            piece = blob[off : off + USB_WRITE_CHUNK]
            wrote = d.ep_out.write(piece, timeout=timeout)
            if wrote != len(piece):
                raise RuntimeError(f"USB 写入长度不足: {wrote} != {len(piece)}")
            off += len(piece)
        tag, body, _ = d.read_reply(timeout=timeout)
        if tag != b"OKAY":
            raise RuntimeError(f"download 结束阶段失败: {tag!r} {body!r}")

    def flash_buffer(self, partition: str, timeout: int = 30000) -> None:
        d = self._require_dev()
        d.write(f"flash:{partition}".encode("ascii"))
        tag, body, _ = d.read_reply(timeout=timeout)
        if tag != b"OKAY":
            raise RuntimeError(f"flash:{partition} 失败: {tag!r} {body!r}")

    def flash_raw(self, partition: str, blob: bytes, timeout: int = 30000) -> None:
        self.download(blob, timeout=timeout)
        self.flash_buffer(partition, timeout=timeout)

    def read_block(self, lba: int) -> bytes:
        d = self._require_dev()
        d.require("oem emmc-init")
        _, infos = d.require(f"oem read-block:{lba:08x}")
        raw = bytearray()
        for idx, line in enumerate(infos):
            m = re.fullmatch(r"([0-9a-fA-F]{8}):([0-9a-fA-F]{32})", line)
            if not m or int(m.group(1), 16) != idx * 16:
                raise RuntimeError(f"oem read-block 格式异常: {line!r}")
            raw.extend(bytes.fromhex(m.group(2)))
        if len(raw) != 512:
            raise RuntimeError(f"oem read-block 长度不足 512: {len(raw)}")
        return bytes(raw)

    def fetch_exact(self, partition: str, offset: int, size: int, timeout: int = 15000) -> bytes:
        d = self._require_dev()
        d.write(f"fetch:{partition}:0x{offset:08x}:0x{size:08x}".encode("ascii"))
        tag, body, _ = d.read_reply(timeout=timeout)
        if tag != b"DATA":
            raise RuntimeError(f"fetch {partition} 被拒: {tag!r} {body!r}")
        announced = int(body, 16)
        if announced != size:
            raise RuntimeError(f"fetch {partition} 声明长度异常: {announced} != {size}")
        out = bytearray()
        while len(out) < size:
            chunk = bytes(d.ep_in.read(min(0x8000, size - len(out)), timeout=timeout))
            if not chunk:
                raise RuntimeError(f"fetch {partition} 提前结束: {len(out)}/{size}")
            out.extend(chunk)
        tag, body, _ = d.read_reply(timeout=timeout)
        if tag != b"OKAY":
            raise RuntimeError(f"fetch {partition} 收尾失败: {tag!r} {body!r}")
        return bytes(out)

    def fetch_range(
        self,
        partition: str,
        offset: int,
        size: int,
        batch_bytes: int = FETCH_BATCH_BYTES,
        progress_cb: Optional[Callable[[int, int], None]] = None,
    ) -> bytes:
        out = bytearray()
        pos = 0
        while pos < size:
            want = min(batch_bytes, size - pos)
            out.extend(self.fetch_exact(partition, offset + pos, want))
            pos += want
            if progress_cb is not None:
                progress_cb(pos, size)
        return bytes(out)

    def fetch_sha256(
        self,
        partition: str,
        total_size: int,
        batch_bytes: int = FETCH_BATCH_BYTES,
        progress_cb: Optional[Callable[[int, int], None]] = None,
    ) -> str:
        h = hashlib.sha256()
        pos = 0
        while pos < total_size:
            want = min(batch_bytes, total_size - pos)
            h.update(self.fetch_exact(partition, pos, want))
            pos += want
            if progress_cb is not None:
                progress_cb(pos, total_size)
        return h.hexdigest().lower()

    def flash_sparse_slices(
        self,
        partition: str,
        partition_size: int,
        slices: list[tuple[int, bytes]],
        progress_cb: Optional[Callable[[int, int, int, int, float], None]] = None,
    ) -> None:
        max_raw_bytes = ((self.max_download_size - SPARSE_HDR_SIZE - 2 * SPARSE_CHUNK_HDR_SIZE) // SPARSE_BLK) * SPARSE_BLK
        total_bytes = sum(len(data) for _, data in slices)
        written_bytes = 0
        for idx, (off, data) in enumerate(slices, 1):
            if len(data) > max_raw_bytes:
                raise ValueError(f"分片过大: {len(data)} > {max_raw_bytes}")
            simg = build_sparse_slice(partition_size, off, data)
            t0 = time.monotonic()
            self.download(simg)
            self.flash_buffer(partition)
            dt = time.monotonic() - t0
            written_bytes += len(data)
            if progress_cb is not None:
                progress_cb(idx, len(slices), written_bytes, total_bytes, dt)

    def reboot(self) -> None:
        if self.dev is not None:
            try:
                self.dev.write(b"reboot")
                self.dev.read_reply(timeout=5000)
            except Exception:
                pass
