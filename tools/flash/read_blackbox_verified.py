#!/usr/bin/env python3
"""Read the 16 KiB ZV-E10 blackbox using the hash-pinned CMD17 stage2.

Default: print the exact plan without importing USB libraries or opening devices.
--self-test: offline protocol checks only. --read: explicitly access Fastboot.
This does not enter Fastboot, pulse reset, erase, write eMMC, or reboot. It does
upload a reviewed read driver to RAM and write its two RAM parameter words.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import struct
import sys
import tempfile
import time

WORKSPACE = Path(__file__).resolve().parent
SOURCE = WORKSPACE
STAGE2 = WORKSPACE / "stage2.bin" if (WORKSPACE / "stage2.bin").exists() else SOURCE / "stage2.bin"
STAGE2_SHA256 = "9bae9a0c4cf1c2e292203fc0402bb0bdc4b106e91c9fd5f92aa73eddb4a3aa37"
START_SECTOR, SECTORS = 0x2C001, 32
DATA_ADDRESS, RESULT_ADDRESS = 0xFE310000, 0xFE308000
VID, PID = 0x18D1, 0x4EE0
EXPECTED_BUILD_ID = 'zve10-bbox-20261025-r6'
LINE = re.compile(r"([0-9a-fA-F]{8}): ((?:[0-9a-fA-F]{8})(?: [0-9a-fA-F]{8}){0,3})")


class ProtocolError(RuntimeError):
    pass


def render_blackbox(data):
    """Keep the binary authoritative; remove only each known slot's NUL padding."""
    if len(data) != SECTORS * 512:
        raise ValueError('blackbox rendering requires the complete 16384 bytes')
    if not any(data):
        return '[VERIFIED READ: all 16384 bytes are 0x00; cause not established]\n'
    if b'zve10-bbox' not in data[:512]:
        return ('[No matching boot canary; this may be older or unrelated data]\n'
                + data.rstrip(b'\0').decode('utf-8', 'replace'))
    sections = [
        ('BOOT CANARY (0x2C001 / seek 1)', 0, 512),
        ('RUNNING / EXIT HEADER (0x2C002 / seek 2)', 512, 1024),
        ('EXIT DATA LOGS (0x2C003..0x2C00F / seek 3..15)', 1024, 7680),
        ('HEARTBEAT T0 (0x2C010..0x2C012 / seek 16..18, 0ms immediate)', 7680, 9216),
        ('HEARTBEAT T0.2 (0x2C013..0x2C015 / seek 19..21, +200ms)', 9216, 10752),
        ('HEARTBEAT T0.5 (0x2C016..0x2C018 / seek 22..24, +300ms, cum 500ms)', 10752, 12288),
        ('HEARTBEAT T1.5 (0x2C019..0x2C01B / seek 25..27, +1s, cum 1.5s)', 12288, 13824),
        ('HEARTBEAT T3.5 (0x2C01C..0x2C01E / seek 28..30, +2s, cum 3.5s)', 13824, 15360),
        ('RESERVED SLOT (0x2C01F / seek 31)', 15360, 15872),
        ('ADJACENT WARM-BOOT SECTOR (0x2C020 / ignored)', 15872, 16384),
    ]
    lines = []
    for name, start, end in sections:
        chunks = [data[s:min(s + 512, end)].rstrip(b'\0') for s in range(start, end, 512)]
        part = b'\n'.join(c for c in chunks if c)
        if not part:
            lines.append(f'----- {name} -----\n[EMPTY / ALL 0x00]')
        else:
            lines.append(f'----- {name} -----\n' + part.decode('utf-8', 'replace'))
    return '\n\n'.join(lines) + '\n'


def decode_reply(packets, expected_tag=b"OKAY"):
    """Validate an already bounded complete response sequence."""
    info = []
    for index, packet in enumerate(packets):
        if not 4 <= len(packet) <= 64:
            raise ProtocolError("reply packet length outside 4..64")
        tag, body = packet[:4], packet[4:]
        if tag == b"INFO":
            info.append(body)
            continue
        if index != len(packets) - 1:
            raise ProtocolError("packets present after terminal reply")
        if tag != expected_tag:
            raise ProtocolError(f"expected {expected_tag!r}, received {tag!r}: {body!r}")
        return body, info
    raise ProtocolError("terminal acknowledgement missing")


def parse_peek(info, start, size):
    """INFO address rows contain little-endian 32-bit words, not bytes."""
    if start % 4 or size <= 0 or size % 4 or size > 4096:
        raise ValueError("peek requires aligned address and 4..4096 byte length")
    out = bytearray()
    for raw in info:
        try:
            line = raw.decode("ascii")
        except UnicodeDecodeError as exc:
            raise ProtocolError("non-ASCII peek row") from exc
        match = LINE.fullmatch(line)
        if match is None:
            raise ProtocolError(f"invalid peek row: {line!r}")
        address = int(match[1], 16)
        if address != start + len(out):
            raise ProtocolError(f"non-contiguous address 0x{address:08x}")
        words = match[2].split()
        remaining = size - len(out)
        if remaining <= 0 or len(words) * 4 != min(16, remaining):
            raise ProtocolError("peek row has incorrect word count")
        for word in words:
            out.extend(struct.pack("<I", int(word, 16)))
    if len(out) != size:
        raise ProtocolError(f"peek length {len(out)}, expected {size}")
    return bytes(out)


class Transport:
    def __init__(self, transcript):
        # No USB imports or discovery occur in plan or self-test mode.
        import usb.core
        import usb.util
        import libusb_package
        self.util = usb.util
        self.log = transcript
        self.dev = None
        self.intf = None
        self.claimed = False
        devices = list(usb.core.find(find_all=True, idVendor=VID, idProduct=PID,
                                    backend=libusb_package.get_libusb1_backend()))
        if len(devices) != 1:
            raise RuntimeError(f"expected one Fastboot device; found {len(devices)}")
        self.dev = devices[0]
        try:
            # Do not reset or change the active USB configuration.
            cfg = self.dev.get_active_configuration()
            self.intf = cfg[(0, 0)]
            self.util.claim_interface(self.dev, self.intf)
            self.claimed = True
            endpoints = [e for e in self.intf if e.bmAttributes & 3 == 2]
            self.ep_in = next(e for e in endpoints if e.bEndpointAddress & 0x80)
            self.ep_out = next(e for e in endpoints if not e.bEndpointAddress & 0x80)
        except Exception:
            self.close()
            raise

    def record(self, direction, data, kind):
        self.log.write(json.dumps({"utc": datetime.now(timezone.utc).isoformat(),
                                   "direction": direction, "kind": kind,
                                   "hex": data.hex()}) + "\n")
        self.log.flush()

    def write(self, data, kind):
        self.record("out", data, kind)
        count = self.ep_out.write(data, timeout=10000)
        if count != len(data):
            raise ProtocolError(f"short USB write: {count}/{len(data)}")

    def reply(self, expected=b"OKAY"):
        packets = []
        deadline = time.monotonic() + 60
        while len(packets) < 1024:
            if time.monotonic() > deadline:
                raise ProtocolError("response deadline exceeded")
            packet = bytes(self.ep_in.read(64, timeout=25000))
            self.record("in", packet, "reply")
            packets.append(packet)
            if not 4 <= len(packet) <= 64:
                raise ProtocolError("invalid USB response length")
            if packet[:4] != b"INFO":
                return decode_reply(packets, expected)
        raise ProtocolError("too many INFO packets")

    def command(self, command):
        # Only the fixed read workflow can issue commands; no CLI raw command.
        allowed = {
            "oem load:0xfe300000", "oem call:0xfe300001",
            "oem poke:0xfe320004:0x00000008",
            "oem peek:0xfe308000:50", "oem peek:0xfe310000:1000",
        }
        allowed.update(f"oem poke:0xfe320000:0x{START_SECTOR+i:08x}"
                       for i in range(0, SECTORS, 8))
        if command not in allowed:
            raise ValueError("command outside the fixed read plan")
        self.write(command.encode("ascii"), "command")
        return self.reply()

    def download_read_driver(self, data):
        if len(data) != 1316 or hashlib.sha256(data).hexdigest() != STAGE2_SHA256:
            raise ValueError("read-driver identity mismatch")
        self.write(f"download:{len(data):08x}".encode("ascii"), "command")
        body, _ = self.reply(b"DATA")
        if body != f"{len(data):08x}".encode("ascii"):
            raise ProtocolError("device requested an unexpected download length")
        self.write(data, "reviewed-read-driver")
        self.reply()

    def peek(self, command, address, size):
        body, info = self.command(command)
        if body:
            raise ProtocolError("peek final OKAY must have an empty body")
        return parse_peek(info, address, size)

    def close(self):
        if self.dev is not None:
            try:
                if self.claimed:
                    self.util.release_interface(self.dev, self.intf)
            finally:
                self.util.dispose_resources(self.dev)
                self.dev = None


def validate_stage2_result(data, sector):
    # struct stage2_result in include/sdhci.h:94-109 is 20 uint32 values.
    values = struct.unpack("<20I", data)
    if values[0] != 0x3031455A:
        raise ProtocolError("stage2 result magic mismatch")
    if not values[1] & 0x80000000 or values[2] != 0 or values[18] != 0:
        raise ProtocolError(f"stage2 did not finish a successful read: {values!r}")
    if values[19] != sector:
        raise ProtocolError("stage2 result reports a different starting sector")
    return values


def perform_read():
    driver = STAGE2.read_bytes()
    if len(driver) != 1316 or hashlib.sha256(driver).hexdigest() != STAGE2_SHA256:
        raise ValueError("stage2.bin does not match the reviewed CMD17 driver")
    root = WORKSPACE / "blackbox_reads"
    root.mkdir(exist_ok=True)
    out = Path(tempfile.mkdtemp(prefix=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-"), dir=root))
    print(f"Output: {out}", flush=True)
    transport = None
    metadata = {"stage2_sha256": STAGE2_SHA256, "start_sector": START_SECTOR,
                "sectors": SECTORS, "hardware_validated": False,
                "status": "incomplete", "read_results": []}
    try:
        with (out / "usb_packets.jsonl").open("x", encoding="utf-8") as transcript:
            transport = Transport(transcript)
            transport.download_read_driver(driver)
            transport.command("oem load:0xfe300000")
            data = bytearray()
            for offset in range(0, SECTORS, 8):
                sector = START_SECTOR + offset
                transport.command(f"oem poke:0xfe320000:0x{sector:08x}")
                transport.command("oem poke:0xfe320004:0x00000008")
                transport.command("oem call:0xfe300001")
                result = transport.peek("oem peek:0xfe308000:50", RESULT_ADDRESS, 80)
                values = validate_stage2_result(result, sector)
                metadata["read_results"].append(list(values))
                chunk = transport.peek("oem peek:0xfe310000:1000", DATA_ADDRESS, 4096)
                (out / f"sector_{sector:08x}.bin").write_bytes(chunk)
                data.extend(chunk)
            if len(data) != SECTORS * 512:
                raise ProtocolError("final blackbox length mismatch")
            (out / "blackbox.bin").write_bytes(data)
            readable = render_blackbox(bytes(data))
            (out / "blackbox.txt").write_text(readable, encoding="utf-8")
            metadata.update(status="read-complete", bytes=len(data),
                            sha256=hashlib.sha256(data).hexdigest(),
                            all_zero=not any(data),
                            expected_build_id=EXPECTED_BUILD_ID,
                            build_id_matched=EXPECTED_BUILD_ID.encode('ascii') in data[:512])
            print(f"Read {len(data)} bytes; SHA256 {metadata['sha256']}")
            try:
                print(readable, flush=True)
            except UnicodeEncodeError:
                print(readable.encode("ascii", errors="backslashreplace").decode("ascii"), flush=True)
    except Exception as exc:
        metadata["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        try:
            if transport is not None:
                transport.close()
        finally:
            (out / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")


def self_test():
    assert 'all 16384 bytes' in render_blackbox(bytes(16384))
    fixture = bytearray(16384)
    canary = (EXPECTED_BUILD_ID + ' BOOT_CANARY').encode('ascii')
    fixture[:len(canary)] = canary
    fixture[512:516] = b'EXIT'
    fixture[2560:2568] = b'SNAPSHOT'
    rendered = render_blackbox(bytes(fixture))
    assert 'BOOT CANARY' in rendered and 'EXIT' in rendered and 'SNAPSHOT' in rendered
    assert '\0' not in rendered
    data = bytes(range(64))
    info = []
    for off in range(0, len(data), 16):
        words = struct.unpack("<4I", data[off:off+16])
        info.append((f"{DATA_ADDRESS+off:08x}: " + " ".join(f"{word:08x}" for word in words)).encode("ascii"))
    body, decoded = decode_reply([b"INFO" + row for row in info] + [b"OKAY"])
    assert body == b"" and parse_peek(decoded, DATA_ADDRESS, 64) == data
    cases = [
        lambda: decode_reply([b"INFO" + info[0]]),
        lambda: decode_reply([b"FAILread failed"]),
        lambda: decode_reply([b"OKAY", b"INFOlate"]),
        lambda: decode_reply([b"BAD"]),
        lambda: parse_peek(info[:-1], DATA_ADDRESS, 64),
        lambda: parse_peek([info[1], info[0]], DATA_ADDRESS, 32),
        lambda: parse_peek([info[0], info[0]], DATA_ADDRESS, 32),
        lambda: parse_peek([b"fe310000: 01 02 03 04"], DATA_ADDRESS, 16),
    ]
    for case in cases:
        try:
            case()
        except ProtocolError:
            pass
        else:
            raise AssertionError("invalid reply accepted")
    values = [0] * 20
    values[0], values[1], values[19] = 0x3031455A, 0x80000007, START_SECTOR
    validate_stage2_result(struct.pack("<20I", *values), START_SECTOR)
    values[18] = 0xFFFFFFFC
    try:
        validate_stage2_result(struct.pack("<20I", *values), START_SECTOR)
    except ProtocolError:
        pass
    else:
        raise AssertionError("stage2 failure accepted")
    print("PASS: offline word/endianness, address/length, ACK and stage2-result checks")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--read", action="store_true", help="explicitly read an already connected Fastboot device")
    mode.add_argument("--self-test", action="store_true", help="offline parser validation; never open USB")
    args = parser.parse_args()
    if args.self_test:
        self_test()
    elif args.read:
        perform_read()
    else:
        print(f"PLAN ONLY; no USB imports or access. Driver: {STAGE2}")
        print(f"Require SHA256: {STAGE2_SHA256}")
        print("With --read: upload 1316-byte CMD17 read driver to RAM fe300000;")
        print("set RAM parameters fe320000/fe320004; call only fe300001;")
        print("check stage2 result and read four 4096-byte INFO/word responses.")
        print(f"eMMC source: 0x{START_SECTOR:x}..0x{START_SECTOR+SECTORS:x} (exclusive end).")
        print(f"Outputs: {WORKSPACE / 'blackbox_reads'} (raw packets, binary, text, metadata).")
        print("No BootROM injection, reset, eMMC write, erase or reboot. Do not use concurrently with another Fastboot client.")
        print("USB workflow has not been tested on hardware; only offline checks were run.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
