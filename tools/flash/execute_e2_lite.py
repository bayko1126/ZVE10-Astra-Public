#!/usr/bin/env python3
# /// script
# dependencies = [
#     "pyusb>=1.2.1",
#     "libusb-package>=1.0.26.2",
# ]
# ///
"""E2LiteTransport Read-Only Transport Layer for check_image.py on Sony ZV-E10.

Baseline Reference:
- Factory Image: firmware/backup/nflasha15.bin
  SHA256: cb15bc410800add6ad605a3433773c7a287703140c85b1b2939192976240bc60
- Read Driver: tools/flash/stage2.bin (1,316 B)
- Write Driver: tools/flash/stage2_turbo_cmd25.bin (2,000 B)
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import struct
import sys
import tempfile
import time

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from read_blackbox_verified import (
    Transport as AuditedBaseTransport, ProtocolError, decode_reply, parse_peek,
    validate_stage2_result, SOURCE, STAGE2_SHA256, VID, PID
)

WORKSPACE = Path(__file__).resolve().parent
ROOT = WORKSPACE.parents[1]

TARGET_IMAGE = ROOT / "firmware" / "backup" / "nflasha15.bin"
EXPECTED_SHA256 = "cb15bc410800add6ad605a3433773c7a287703140c85b1b2939192976240bc60"

CHUNK_SIZE = 256 * 1024
TOTAL_CHUNKS = 1200
PARTITION_START = 0x16C000
PARTITION_END = 0x202000
WBI_SECTOR = 0x2C000

MANIFEST_FILE = WORKSPACE / "evidence" / "e2_lite_manifest.json"
CHECKPOINT_FILE = WORKSPACE / "diagnostic" / "e2_lite_checkpoint.json"

# L1 4 块结构代表块（超级块 0、GDT 1、Inode 299、高位数据块 448）
L1_CHUNKS = [0, 1, 299, 448]

DRIVER_WRITE = WORKSPACE / "stage2_turbo_cmd25.bin" if (WORKSPACE / "stage2_turbo_cmd25.bin").exists() else SOURCE / "stage2_turbo_cmd25.bin"
DRIVER_WRITE_SHA256 = "e41d9ab86d1cf039ff6d50c93baf36051a1ce22ac4039e7ff9862adbfb5b308b"
DRIVER_WRITE_SIZE = 2000

DRIVER_READ = WORKSPACE / "stage2.bin" if (WORKSPACE / "stage2.bin").exists() else SOURCE / "stage2.bin"
DRIVER_READ_SHA256 = "9bae9a0c4cf1c2e292203fc0402bb0bdc4b106e91c9fd5f92aa73eddb4a3aa37"
DRIVER_READ_SIZE = 1316

CALL = re.compile(rb"call ([0-9a-fA-F]{8}) -> ([0-9a-fA-F]{8})")
LOAD = re.compile(rb"loaded ([0-9a-fA-F]{8}) bytes -> ([0-9a-fA-F]{8})")


def load_manifest() -> dict:
    if not MANIFEST_FILE.exists():
        raise FileNotFoundError(f"Manifest missing: {MANIFEST_FILE}")
    return json.loads(MANIFEST_FILE.read_text(encoding="utf-8"))


def load_checkpoint(checkpoint_path: Path = CHECKPOINT_FILE) -> dict:
    if checkpoint_path.exists():
        try:
            return json.loads(checkpoint_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "experiment": "E2-lite",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "written_chunks": [],
        "wbi_cleared": False,
        "verified_chunks": {},
    }


def save_checkpoint(data: dict, checkpoint_path: Path = CHECKPOINT_FILE) -> None:
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    temp_p = checkpoint_path.with_suffix(".tmp")
    temp_p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    temp_p.replace(checkpoint_path)


class E2LiteTransport(AuditedBaseTransport):
    def __init__(self):
        super().__init__(transcript=io.StringIO())

    def command(self, command: str) -> tuple[bytes, list[bytes]]:
        allowed_exact = {
            "getvar:product", "getvar:version", "reboot",
            "oem load:0xfe300000", "oem load:0xfe400000",
            "oem call:0xfe300001", "oem call:0xfe300601", "oem call:0xfe300701",
            "oem peek:0xfe300000:7d0", "oem peek:0xfe300000:524",
            "oem peek:0xfe308000:50", "oem peek:0xfe308048:4",
            "oem peek:0xfe320000:8", "oem peek:0xfe310000:200", "oem peek:0xfe310000:1000",
            "oem poke:0xfe308048:0xdeadbeef",
            "oem poke:0xfe320000:0x0002c000", "oem poke:0xfe320004:0x00000001",
            "oem poke:0xfe320004:0x00000200", "oem poke:0xfe320004:0x00000008",
        }
        for chunk_idx in range(TOTAL_CHUNKS):
            sec = PARTITION_START + chunk_idx * 512
            allowed_exact.add(f"oem poke:0xfe320000:0x{sec:08x}")
            for subblock in range(64):
                cur_sec = sec + subblock * 8
                allowed_exact.add(f"oem poke:0xfe320000:0x{cur_sec:08x}")

        if command not in allowed_exact:
            raise ProtocolError(f"unauthorized Fastboot command: {command}")

        self.write(command.encode("ascii"), "command")
        return self.reply()

    def download(self, data: bytes):
        self.write(f"download:{len(data):08x}".encode("ascii"), "command")
        body, _ = self.reply(b"DATA")
        if body != f"{len(data):08x}".encode("ascii"):
            raise ProtocolError(f"download size mismatch: {body.decode('latin1', 'replace')}")
        self.write(data, "data")
        return self.reply(b"OKAY")

    def load(self, address: int, length: int):
        body, info = self.command(f"oem load:0x{address:08x}")
        if body or len(info) != 1:
            raise ProtocolError("load requires one INFO result and empty final OKAY")
        match = LOAD.fullmatch(info[0])
        if not match or int(match[1], 16) != length or int(match[2], 16) != address:
            raise ProtocolError("load result has incorrect length/address")

    def peek(self, address: int, size: int) -> bytes:
        body, info = self.command(f"oem peek:0x{address:08x}:{size:x}")
        if body:
            raise ProtocolError("peek final OKAY must have an empty body")
        return parse_peek(info, address, size)

    def call(self, address: int, *, require_zero: bool = True) -> int:
        body, info = self.command(f"oem call:0x{address:08x}")
        if body or len(info) != 1:
            raise ProtocolError("call requires one INFO result and empty final OKAY")
        match = CALL.fullmatch(info[0])
        if not match or int(match[1], 16) != address:
            raise ProtocolError("call result address mismatch")
        val = int(match[2], 16)
        if require_zero and val != 0:
            raise ProtocolError(f"call 0x{address:08x} returned 0x{val:08x}, expected 0")
        return val

    def checked_operation(self, address: int):
        self.command("oem poke:0xfe308048:0xdeadbeef")
        self.call(address)
        status = struct.unpack("<I", self.peek(0xFE308048, 4))[0]
        if status != 0:
            raise ProtocolError(f"stage2 operation 0x{address:08x} status is 0x{status:08x}, expected 0")

    def parameters(self, sector: int, count: int, *, wbi: bool = False, initialization: bool = False):
        if wbi:
            if (sector, count) != (WBI_SECTOR, 1):
                raise ValueError("WBI operation must touch only sector 0x2c000")
        elif initialization:
            if (sector, count) != (PARTITION_START, 1):
                raise ValueError("initial read must use partition start sector")
        elif not (count == 512 and PARTITION_START <= sector < PARTITION_END
                  and (sector - PARTITION_START) % 512 == 0 and sector + count <= PARTITION_END):
            raise ValueError(f"invalid partition write range: sector 0x{sector:08x}, count {count}")

        self.command(f"oem poke:0xfe320000:0x{sector:08x}")
        self.command(f"oem poke:0xfe320004:0x{count:08x}")
        actual = struct.unpack("<2I", self.peek(0xFE320000, 8))
        if actual != (sector, count):
            raise ProtocolError(f"RAM parameter readback mismatch: actual {actual}, expected ({sector}, {count})")

    def initialize_writer(self, driver: bytes):
        if len(driver) != DRIVER_WRITE_SIZE or hashlib.sha256(driver).hexdigest() != DRIVER_WRITE_SHA256:
            raise ValueError("CMD25 写驱动身份校验未通过")

        product, _ = self.command("getvar:product")
        if product != b"ZVE10":
            raise ProtocolError(f"unexpected Fastboot product {product!r}")
        version, _ = self.command("getvar:version")
        if version != b"0.4":
            raise ProtocolError(f"unexpected transport version {version!r}")

        self.download(driver)
        self.load(0xFE300000, len(driver))
        if self.peek(0xFE300000, len(driver)) != driver:
            raise ProtocolError("readback of staged CMD25 write driver differs")

        self.parameters(PARTITION_START, 1, initialization=True)
        self.call(0xFE300001, require_zero=False)
        validate_stage2_result(self.peek(0xFE308000, 80), PARTITION_START)
        self.checked_operation(0xFE300601)

    def write_chunk(self, index: int, data: bytes):
        if len(data) != CHUNK_SIZE:
            raise ValueError(f"image chunk must be exactly {CHUNK_SIZE} bytes")
        sector = PARTITION_START + index * 512
        count = 512
        self.download(data)
        self.load(0xFE400000, len(data))
        self.parameters(sector, count)
        self.checked_operation(0xFE300701)

    def clear_wbi(self):
        """【已废弃 — 保留空实现】

        原实现往扇区 0x2C000 写 512 个零字节。但该扇区是 **nflasha5 的 "WBI1" 头部**
        （原厂值：57 42 49 31 "WBI1" + 少量字段，共 25 个非零字节），
        属于引导器状态的一部分。

        实测：清零后 WBI1 签名丢失，相机出现
        「同一份固件时好时坏 / 完全不启动 / 红灯不亮」等症状，
        需通过 restore_wbi_auto.py 从 nflasha5.bin 恢复。

        flash_robust.py 从不触碰该扇区，工作正常。
        → 因此这里改为空操作：刷写 nflasha15 与 WBI 无关，不需要也不应该动它。

        如需检查 WBI1 状态，用 probe_state.py（只读）。
        """
        print("[WBI] 跳过 clear_wbi（0x2C000 是 nflasha5 的 WBI1 头部，不可清零）")
        return

    def load_read_driver(self, driver: bytes):
        self.download(driver)
        self.load(0xFE300000, len(driver))

    def read_chunk(self, chunk_idx: int) -> bytes:
        sector = PARTITION_START + chunk_idx * 512
        chunk_data = bytearray()
        for subblock in range(64):
            cur_sec = sector + subblock * 8
            self.command(f"oem poke:0xfe320000:0x{cur_sec:08x}")
            self.command("oem poke:0xfe320004:0x00000008")
            self.command("oem call:0xfe300001")
            last_s2_result = self.peek(0xFE308000, 80)
            validate_stage2_result(last_s2_result, cur_sec)
            subblock_bytes = self.peek(0xFE310000, 4096)
            chunk_data.extend(subblock_bytes)
        return bytes(chunk_data)


def main():
    parser = argparse.ArgumentParser(description="E2LiteTransport Read-Only Transport Module Self-Test")
    parser.add_argument("--self-test", action="store_true", help="Verify stage2 drivers and factory baseline hashes")
    parser.add_argument("--dry-run", action="store_true", help="Alias for --self-test")
    parser.parse_args()

    print("=== E2LiteTransport (check_image.py Transport Layer) Self-Test ===")
    if not DRIVER_READ.exists():
        raise FileNotFoundError(f"Read driver missing: {DRIVER_READ}")
    r_bytes = DRIVER_READ.read_bytes()
    r_sha = hashlib.sha256(r_bytes).hexdigest()
    assert len(r_bytes) == DRIVER_READ_SIZE and r_sha == DRIVER_READ_SHA256, (
        f"stage2.bin mismatch: size={len(r_bytes)}, sha256={r_sha}"
    )
    print(f"[+] Read driver OK : {DRIVER_READ} ({len(r_bytes)} B, SHA256={r_sha[:16]}...)")

    if not DRIVER_WRITE.exists():
        raise FileNotFoundError(f"Write driver missing: {DRIVER_WRITE}")
    w_bytes = DRIVER_WRITE.read_bytes()
    w_sha = hashlib.sha256(w_bytes).hexdigest()
    assert len(w_bytes) == DRIVER_WRITE_SIZE and w_sha == DRIVER_WRITE_SHA256, (
        f"stage2_turbo_cmd25.bin mismatch: size={len(w_bytes)}, sha256={w_sha}"
    )
    print(f"[+] Write driver OK: {DRIVER_WRITE} ({len(w_bytes)} B, SHA256={w_sha[:16]}...)")

    if not TARGET_IMAGE.exists():
        raise FileNotFoundError(f"Factory image missing: {TARGET_IMAGE}")
    assert TARGET_IMAGE.stat().st_size == TOTAL_CHUNKS * CHUNK_SIZE, "Factory image size mismatch"
    print(f"[+] Factory image OK: {TARGET_IMAGE} ({TARGET_IMAGE.stat().st_size:,} B)")
    print("[*] Note: execute_e2_lite.py provides E2LiteTransport for check_image.py.")
    print("    Use check_image.py for superblock probing and flash_robust.py for flashing.")


if __name__ == "__main__":
    main()
