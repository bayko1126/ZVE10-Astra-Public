#!/usr/bin/env python3
# /// script
# dependencies = [
#     "pyusb>=1.2.1",
#     "libusb-package>=1.0.26.2",
# ]
# ///
"""Read-Only FAT Log Extractor from Fastboot.

Extracts persistent camera logs directly from:
  1. nflasha2  (/setting, LBA 0x04000, FAT12) -> ulogio.bin, filter.bin, kemco.txt, etc.
  2. nflasha11 (/log,     LBA 0x5C000, FAT16) -> escapelog.bin, ulogio_*.dat/txt, threads.bin
  3. nflasha10 (/tmp,     LBA 0x56000, FAT12) -> tmp logs if any

Zero-intrusion: pure read-only via stage2.bin. Never writes eMMC, never modifies files.
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
    Transport as ReadTransport, ProtocolError, decode_reply, parse_peek,
    validate_stage2_result, SOURCE, STAGE2_SHA256, VID, PID
)

WORKSPACE = Path(__file__).resolve().parent
DRIVER_READ = WORKSPACE / "stage2.bin" if (WORKSPACE / "stage2.bin").exists() else SOURCE / "stage2.bin"

PARTITIONS = {
    "nflasha2":  {"name": "nflasha2 (/setting)", "lba": 0x04000, "expected_fat": "FAT12"},
    "nflasha11": {"name": "nflasha11 (/log)",     "lba": 0x5C000, "expected_fat": "FAT16"},
    "nflasha10": {"name": "nflasha10 (/tmp)",     "lba": 0x56000, "expected_fat": "FAT12"},
}


class FastbootReadTransport(ReadTransport):
    def command(self, command: str):
        allowed_exact = {
            "getvar:product", "getvar:version",
            "oem darwin",
            "oem load:0xfe300000", "oem call:0xfe300001",
            "oem poke:0xfe320004:0x00000008",
            "oem peek:0xfe308000:50", "oem peek:0xfe310000:1000",
        }
        if command not in allowed_exact:
            if not re.fullmatch(r"oem (peek:0x[0-9a-fA-F]+:[0-9a-fA-F]+|poke:0xfe320000:0x[0-9a-fA-F]{8})", command):
                raise ValueError(f"Command not permitted during read: {command}")
        self.write(command.encode("ascii"), "command")
        return self.reply()


def read_8_sectors(transport: FastbootReadTransport, sector: int) -> bytes:
    transport.command(f"oem poke:0xfe320000:0x{sector:08x}")
    transport.command("oem poke:0xfe320004:0x00000008")
    transport.command("oem call:0xfe300001")
    res = transport.peek("oem peek:0xfe308000:50", 0xFE308000, 80)
    validate_stage2_result(res, sector)
    return transport.peek("oem peek:0xfe310000:1000", 0xFE310000, 4096)


# Fastboot 会话约 215 秒超时，长读取必须周期性发 `oem darwin` 续期。
# 只在「8 扇区读完成」这个安全点插入，绝不在 bulk 传输中并发发送。
PING_INTERVAL = 60.0
_last_ping = [0.0]


def _ping_if_due(transport: FastbootReadTransport) -> None:
    now = time.monotonic()
    if now - _last_ping[0] < PING_INTERVAL:
        return
    try:
        transport.command("oem darwin")
        _last_ping[0] = now
    except Exception:
        pass


def read_sector_range(transport: FastbootReadTransport, start_sec: int, num_sec: int) -> bytes:
    out = bytearray()
    for cur in range(start_sec, start_sec + num_sec, 8):
        _ping_if_due(transport)
        count = min(8, start_sec + num_sec - cur)
        chunk = read_8_sectors(transport, cur)
        out.extend(chunk[: count * 512])
    return bytes(out)


class FatVolume:
    def __init__(self, transport: FastbootReadTransport, lba_start: int, name: str):
        self.transport = transport
        self.lba_start = lba_start
        self.name = name

        # 1. 读引导扇区 (BPB)
        boot = read_sector_range(self.transport, self.lba_start, 1)
        if len(boot) < 512:
            raise RuntimeError(f"无法读取 {name} 引导扇区")

        self.bps = struct.unpack("<H", boot[11:13])[0]
        self.spc = boot[13]
        self.res_sec = struct.unpack("<H", boot[14:16])[0]
        self.fats = boot[16]
        self.roots = struct.unpack("<H", boot[17:19])[0]
        tot16 = struct.unpack("<H", boot[19:21])[0]
        spf16 = struct.unpack("<H", boot[22:24])[0]
        tot32 = struct.unpack("<I", boot[32:36])[0]
        spf32 = struct.unpack("<I", boot[36:40])[0] if len(boot) >= 40 else 0

        self.total_sectors = tot32 if tot16 == 0 else tot16
        self.spf = spf32 if spf16 == 0 else spf16

        self.root_dir_sectors = (self.roots * 32 + self.bps - 1) // self.bps if self.roots else 0
        self.fat_start_sec = self.lba_start + self.res_sec
        self.root_dir_sec = self.fat_start_sec + self.fats * self.spf
        self.data_start_sec = self.root_dir_sec + self.root_dir_sectors

        data_sectors = self.total_sectors - (self.res_sec + self.fats * self.spf + self.root_dir_sectors)
        self.clusters = data_sectors // self.spc if self.spc else 0
        self.fat_type = "FAT12" if self.clusters < 4085 else ("FAT16" if self.clusters < 65525 else "FAT32")

        # 缓存 FAT 表
        print(f"[*] 解析 {name}: 类型={self.fat_type}, 每簇={self.spc}扇区({self.spc*self.bps//1024}KB), 根项={self.roots}, 总扇区={self.total_sectors}")
        # 读取第一个 FAT 表
        self.fat_bytes = read_sector_range(self.transport, self.fat_start_sec, min(self.spf, 64))

    def next_cluster(self, cl: int) -> int:
        if self.fat_type == "FAT12":
            offset = cl + (cl // 2)
            if offset + 1 >= len(self.fat_bytes):
                return 0x0FFF
            if cl % 2 == 0:
                val = (self.fat_bytes[offset] | (self.fat_bytes[offset + 1] << 8)) & 0x0FFF
            else:
                val = (self.fat_bytes[offset] >> 4) | (self.fat_bytes[offset + 1] << 4)
            return val
        elif self.fat_type == "FAT16":
            offset = cl * 2
            if offset + 1 >= len(self.fat_bytes):
                return 0xFFFF
            return struct.unpack("<H", self.fat_bytes[offset:offset + 2])[0]
        return 0xFFFF

    def is_eof(self, cl: int) -> bool:
        if self.fat_type == "FAT12":
            return cl >= 0x0FF8
        elif self.fat_type == "FAT16":
            return cl >= 0xFFF8
        return True

    def cluster_to_sec(self, cl: int) -> int:
        return self.data_start_sec + (cl - 2) * self.spc

    def read_directory(self, dir_sec: int, sec_count: int) -> list[dict]:
        raw = read_sector_range(self.transport, dir_sec, sec_count)
        entries = []
        for i in range(0, len(raw), 32):
            ent = raw[i:i + 32]
            if not ent or ent[0] in [0x00, 0xe5]:
                continue
            attr = ent[11]
            if attr == 0x0F:  # Skip LFN for directory parsing
                continue
            name = ent[:8].decode("latin1", "replace").strip()
            ext = ent[8:11].decode("latin1", "replace").strip()
            cl = struct.unpack("<H", ent[26:28])[0]
            size = struct.unpack("<I", ent[28:32])[0]
            full_name = f"{name}.{ext}" if ext else name
            is_dir = bool(attr & 0x10)
            entries.append({
                "name": full_name,
                "cluster": cl,
                "size": size,
                "is_dir": is_dir,
            })
        return entries

    def read_directory_chain(self, start_cluster: int) -> list[dict]:
        """沿 FAT 链读完整目录（多簇目录不再被截断）。"""
        if start_cluster < 2:
            return []
        entries: list[dict] = []
        cl = start_cluster
        visited = set()
        while cl >= 2 and not self.is_eof(cl) and cl not in visited:
            visited.add(cl)
            sec = self.cluster_to_sec(cl)
            raw = read_sector_range(self.transport, sec, self.spc)
            stop = False
            for i in range(0, len(raw), 32):
                ent = raw[i:i + 32]
                if not ent or len(ent) < 32:
                    continue
                if ent[0] == 0x00:      # 目录结束标记
                    stop = True
                    break
                if ent[0] == 0xE5:      # 已删除
                    continue
                attr = ent[11]
                if attr == 0x0F:        # LFN
                    continue
                name = ent[:8].decode("latin1", "replace").strip()
                ext = ent[8:11].decode("latin1", "replace").strip()
                cl2 = struct.unpack("<H", ent[26:28])[0]
                size = struct.unpack("<I", ent[28:32])[0]
                entries.append({
                    "name": f"{name}.{ext}" if ext else name,
                    "cluster": cl2,
                    "size": size,
                    "is_dir": bool(attr & 0x10),
                })
            if stop:
                break
            cl = self.next_cluster(cl)
        return entries

    def read_file(self, start_cluster: int, size: int) -> bytes:
        if start_cluster < 2:
            return b""
        buf = bytearray()
        cl = start_cluster
        visited = set()
        while len(buf) < size and not self.is_eof(cl):
            if cl in visited:
                break
            visited.add(cl)
            sec = self.cluster_to_sec(cl)
            chunk = read_sector_range(self.transport, sec, self.spc)
            buf.extend(chunk)
            cl = self.next_cluster(cl)
        return bytes(buf[:size])


def safe_decode_text(data: bytes) -> str:
    # 尝试 utf-8，若有乱码以 replace 替换，并过滤纯打印字符
    return data.decode("utf-8", errors="replace")


def extract_all(partitions=None, all_files: bool = False):
    if partitions is None:
        selected_partitions = ["nflasha2"]
    elif "all" in partitions:
        selected_partitions = ["nflasha2", "nflasha11", "nflasha10"]
    else:
        selected_partitions = [p.strip() for p in partitions if p.strip() in PARTITIONS]
        if not selected_partitions:
            selected_partitions = ["nflasha2"]

    print("=" * 68)
    print("  Sony ZV-E10 Fastboot 纯只读 FAT 持久日志提取器")
    print(f"  目标分区: {', '.join(selected_partitions)}")
    print("=" * 68)

    if not DRIVER_READ.exists():
        raise FileNotFoundError(f"读取驱动不存在: {DRIVER_READ}")

    driver = DRIVER_READ.read_bytes()
    if len(driver) != 1316 or hashlib.sha256(driver).hexdigest() != STAGE2_SHA256:
        raise ValueError("CMD17 读取驱动哈希不匹配")

    out_root = WORKSPACE / "diagnostic" / "fat_logs_extracted"
    out_root.mkdir(parents=True, exist_ok=True)
    session_dir = Path(tempfile.mkdtemp(prefix=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-"), dir=out_root))
    print(f"[*] 输出保存目录: {session_dir}\n")

    transport = None
    extracted_summary = []

    try:
        with (session_dir / "usb_packets.jsonl").open("x", encoding="utf-8") as transcript:
            transport = FastbootReadTransport(transcript)
            print("[+] 连接到 Fastboot 设备，初始化 Stage2 CMD17 读取驱动...", flush=True)
            transport.download_read_driver(driver)
            transport.command("oem load:0xfe300000")
            print("[+] 读取驱动就绪！开始只读探测持久日志分区...\n", flush=True)

            target_keywords = [
                "ulogio", "filter", "escape", "log", "klog", "tlog", "dlog", "blog",
                "thread", "kemco", "probe", "marker", "e0", "e1", "exec", "snap", "live",
                "txt", "status", "wchan", "dmesg",
                # 访问日志（ACCESSLOG.DAT / ACCESS~1.DAT）—— 可独立证明系统引导过
                "access", "escape"
            ]

            for part_key in selected_partitions:
                part_cfg = PARTITIONS[part_key]
                print(f">>> 正在探测分区: {part_cfg['name']} (LBA: 0x{part_cfg['lba']:05x}) <<<")
                try:
                    vol = FatVolume(transport, part_cfg["lba"], part_key)
                except Exception as e:
                    print(f"[-] 分区引导读取异常: {e}\n")
                    continue

                # 读根目录
                root_entries = vol.read_directory(vol.root_dir_sec, min(vol.root_dir_sectors, 32))
                print(f"[+] 根目录包含 {len(root_entries)} 个文件/目录条目:")

                dirs_to_explore = []
                files_to_dump = []

                for ent in root_entries:
                    name_lower = ent["name"].lower()
                    tag = "[DIR] " if ent["is_dir"] else "[FILE]"
                    print(f"    {tag} {ent['name']:20s} (簇={ent['cluster']:5d}, 大小={ent['size']:8d} B)")
                    if ent["is_dir"]:
                        if any(k in name_lower for k in ["probe", "log", "ulog", "appfw", "mode", "sen", "astra"]):
                            dirs_to_explore.append(ent)
                    else:
                        if all_files or any(k in name_lower for k in target_keywords):
                            files_to_dump.append(ent)

                # 探测关键子目录
                for d in dirs_to_explore:
                    print(f"\n  [*] 探索子目录: /{d['name']} (簇 {d['cluster']})...")
                    sub_entries = vol.read_directory_chain(d["cluster"])
                    print(f"      (目录链读取: {len(sub_entries)} 个条目)")
                    for sub in sub_entries:
                        sub_tag = "[DIR] " if sub["is_dir"] else "[FILE]"
                        print(f"      {sub_tag} {sub['name']:20s} (簇={sub['cluster']:5d}, 大小={sub['size']:8d} B)")
                        if not sub["is_dir"]:
                            files_to_dump.append({
                                "name": f"{d['name']}_{sub['name']}",
                                "cluster": sub["cluster"],
                                "size": sub["size"],
                            })

                # 提取目标文件数据
                part_dir = session_dir / part_key
                part_dir.mkdir(parents=True, exist_ok=True)

                for f in files_to_dump:
                    fname = f["name"]
                    fsize = f["size"]
                    fcl = f["cluster"]
                    if fsize == 0:
                        save_path = part_dir / fname
                        save_path.touch()
                        print(f"  [+] 空文件登记: {fname} (0 字节)")
                        extracted_summary.append({
                            "partition": part_key,
                            "file": fname,
                            "size": 0,
                            "preview": "<空文件>",
                        })
                        continue
                    if fcl < 2:
                        continue

                    # 大文件默认跳过：4 KB/次传输约 20 KB/s，读 1 MB 要 50 秒，
                    # 极易撞上 fastboot 会话窗口并拖垮整轮提取。
                    if fsize > 512 * 1024 and not all_files:
                        print(f"  [跳过] {fname} ({fsize:,} B) — 超过 512 KB，"
                              f"加 --all-files 才读")
                        continue

                    # 限制单文件最大读 1024 KiB
                    read_limit = min(fsize, 1024 * 1024)
                    print(f"  [+] 提取文件: {fname} ({fsize} 字节)...", end="", flush=True)
                    try:
                        content = vol.read_file(fcl, read_limit)
                    except Exception as e:
                        print(f" 跳过（读取失败: {type(e).__name__}）")
                        print(f"      → 该文件过大或 USB 中断，不影响其他文件")
                        continue
                    save_path = part_dir / fname
                    save_path.write_bytes(content)
                    print(f" 已保存 ({len(content)} B)")

                    # 尝试文本解析与关键信息展示
                    text_content = safe_decode_text(content[:4096])
                    preview = "\n".join([line for line in text_content.splitlines() if line.strip()][:15])
                    extracted_summary.append({
                        "partition": part_key,
                        "file": fname,
                        "size": fsize,
                        "preview": preview,
                    })

                print()

            print("=" * 68)
            print("  [SUCCESS] 持久 FAT 日志只读提取完成！")
            print(f"  所有提取的文件已保存至: {session_dir}")
            print("=" * 68 + "\n")

            # 打印关键日志摘要
            print("=== 关键持久日志摘要展示 ===")
            for item in extracted_summary:
                print(f"\n--- [{item['partition']}] {item['file']} ({item['size']} 字节) ---")
                if item["preview"]:
                    print(item["preview"][:1000])
                else:
                    print("<二进制数据>")

    finally:
        if transport is not None:
            transport.close()


def main():
    parser = argparse.ArgumentParser(description="Sony ZV-E10 FAT Log Extractor")
    parser.add_argument("--all-files", action="store_true",
                        help="导出分区根目录下的全部文件（不看关键字），用于做完整启动指纹")
    parser.add_argument("--partitions", nargs="+", default=["nflasha2"],
                        help="Partitions to extract: nflasha2, nflasha11, nflasha10, or all (default: nflasha2)")
    args = parser.parse_args()
    extract_all(partitions=args.partitions, all_files=args.all_files)


if __name__ == "__main__":
    main()
