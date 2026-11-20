#!/usr/bin/env python3
# /// script
# dependencies = ["pyusb>=1.2.1", "libusb-package>=1.0.26.2"]
# ///
"""稳健刷写器：支持 CMD25 极速写入、断点续传与差量刷写。

与旧版实验脚本的三个关键差异：

  1. **绝不触碰扇区 0x2C000**
     那是 nflasha5 的 "WBI1" 头部（原厂值只有 25 个非零字节）。
     旧脚本的 clear_wbi() 每次都往里写 512 个零，把签名抹掉 ——
     若该扇区受损，需通过 restore_wbi_auto.py 从 nflasha5 原厂备份恢复引导头。

  2. **USB 掉线自动重连**
     整轮写入包在 while 循环里，任何 USB 异常都只是「本轮中断」，
     进度已落盘，重连后从断点继续。

  3. **每块落一次进度**
     掉线最多损失 1 块（而不是 25 块）。

用法：
  uv run --with pyusb --with libusb-package python tools/flash/flash_robust.py --image <path> --dry-run
  uv run --with pyusb --with libusb-package python tools/flash/flash_robust.py --image <path> --flash [--chunk-kb 256]
  uv run --with pyusb --with libusb-package python tools/flash/flash_robust.py --image <path> --flash --reset-progress
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import usb.core
import usb.util
import libusb_package

WORKSPACE = Path(__file__).resolve().parent
STAGE2_TURBO_BIN = WORKSPACE / "stage2_turbo_cmd25.bin"
PROGRESS_FILE = WORKSPACE / "diagnostic" / "flash_robust_progress.json"
PARTITION_START_BLOCK = 0x16C000
FORBIDDEN_SECTOR = 0x2C000          # nflasha5 WBI1 头部，绝对不许写


class FastbootDevice:
    def __init__(self):
        self.dev = None
        self.ep_out = None
        self.ep_in = None

    def connect(self, timeout=None):
        backend = libusb_package.get_libusb1_backend()
        start = time.time()
        first = True
        while True:
            dev = usb.core.find(idVendor=0x18d1, idProduct=0x4ee0, backend=backend)
            if dev is not None:
                try:
                    try:
                        dev.set_configuration()
                    except Exception:
                        pass
                    cfg = dev.get_active_configuration()
                    intf = cfg[(0, 0)]
                    usb.util.claim_interface(dev, intf)
                    self.dev = dev
                    self.ep_out = usb.util.find_descriptor(
                        intf, custom_match=lambda e: usb.util.endpoint_direction(
                            e.bEndpointAddress) == usb.util.ENDPOINT_OUT)
                    self.ep_in = usb.util.find_descriptor(
                        intf, custom_match=lambda e: usb.util.endpoint_direction(
                            e.bEndpointAddress) == usb.util.ENDPOINT_IN)
                    return True
                except Exception:
                    pass
            if first:
                print("[-] 等待 Fastboot 设备 (18d1:4ee0)...", flush=True)
                first = False
            if timeout and (time.time() - start > timeout):
                return False
            time.sleep(min(0.2, timeout) if timeout else 0.5)

    def close(self):
        if self.dev:
            try:
                usb.util.dispose_resources(self.dev)
            except Exception:
                pass
            self.dev = None

    def send_cmd(self, c, timeout=10000):
        if isinstance(c, str):
            c = c.encode()
        self.ep_out.write(c, timeout=timeout)
        info = []
        while True:
            resp = bytes(self.ep_in.read(64, timeout=timeout))
            tag, body = resp[:4], resp[4:]
            if tag == b"INFO":
                info.append(body.decode("utf-8", "replace"))
                continue
            return tag.decode("latin1"), body, info

    def do_download(self, blob, timeout=10000):
        st, msg, _ = self.send_cmd(f"download:{len(blob):08x}", timeout=timeout)
        if st != "DATA":
            raise RuntimeError(f"Download rejected: {st} {msg}")
        PIECE = 16384
        sent = 0
        while sent < len(blob):
            self.ep_out.write(blob[sent:sent + PIECE], timeout=timeout)
            sent += PIECE
        while True:
            resp = bytes(self.ep_in.read(64, timeout=timeout))
            tag, body = resp[:4], resp[4:]
            if tag == b"INFO":
                continue
            if tag != b"OKAY":
                raise RuntimeError(f"Data phase failed: {tag} {body}")
            break


def load_progress(image_name: str) -> set[int]:
    if PROGRESS_FILE.exists():
        try:
            d = json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
            if d.get("image") == image_name:
                return set(d.get("completed", []))
        except Exception as e:
            print(f"[WARN] 进度文件读取失败: {e}")
    return set()


def save_progress(image_name: str, done: set[int]) -> None:
    PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = PROGRESS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"image": image_name,
                               "completed": sorted(done)}, indent=2))
    os.replace(tmp, PROGRESS_FILE)


def fmt(sec: float) -> str:
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m {s:02d}s" if h else f"{m}m {s:02d}s"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", required=True)
    ap.add_argument("--start-lba", default="0x16C000",
                    help="分区起始 LBA（默认 0x16C000 = nflasha15）")
    ap.add_argument("--allow-wbi", action="store_true",
                    help="允许写入 0x2C000（WBI1 头部）。仅当整盘刷回 nflasha5 原厂备份时使用")
    ap.add_argument("--chunk-kb", type=int, default=256, choices=[64, 128, 256, 512])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--flash", action="store_true")
    ap.add_argument("--reset-progress", action="store_true")
    ap.add_argument("--max-rounds", type=int, default=200, help="最多重连轮数")
    ap.add_argument("--range", default="", help="只写块范围，如 160:600（配合 --chunk-kb）")
    ap.add_argument("--ping", type=float, default=30.0,
                    help="每 N 秒在块间发一次 oem darwin 保活（默认 30，0=关闭）")
    ap.add_argument("--diff-from", default="",
                    help="只写与基准镜像不同的块（前提：设备当前内容必须等于该基准）")
    a = ap.parse_args()
    if not (a.dry_run or a.flash):
        ap.error("需 --dry-run 或 --flash")

    global PARTITION_START_BLOCK
    PARTITION_START_BLOCK = int(a.start_lba, 16)

    img = Path(a.image)
    if not img.exists():
        print(f"[FATAL] 镜像不存在: {img}")
        return 1
    if not STAGE2_TURBO_BIN.exists():
        print(f"[FATAL] 极速驱动不存在: {STAGE2_TURBO_BIN}")
        return 1

    chunk_size = a.chunk_kb * 1024
    blocks_per_chunk = chunk_size // 512
    size = img.stat().st_size
    total = (size + chunk_size - 1) // chunk_size

    print("=" * 74)
    print("  稳健刷写器（不触碰 WBI1，掉线自动重连）")
    print("=" * 74)
    print(f"  镜像    : {img.name}  {size:,} B")
    print(f"  块大小  : {a.chunk_kb} KB   共 {total} 块")
    print(f"  起始 LBA: 0x{PARTITION_START_BLOCK:06x}")
    if a.allow_wbi:
        print(f"  ⚠️  WBI 保护已解除：将写入 0x{FORBIDDEN_SECTOR:06x}"
              f"（仅在刷 nflasha5 原厂备份时使用）")
    else:
        print(f"  保护扇区: 0x{FORBIDDEN_SECTOR:06x}（nflasha5 WBI1 头部）永不写入")

    if a.reset_progress and PROGRESS_FILE.exists():
        PROGRESS_FILE.unlink()
        print("  [*] 已清除历史进度")

    pkey = f"{img.name}@{a.chunk_kb}k"
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
        diff = []
        with img.open("rb") as f1, base.open("rb") as f2:
            for i in range(total):
                if f1.read(chunk_size) != f2.read(chunk_size):
                    diff.append(i)
        print(f"  差量模式: 与 {base.name} 相比 {len(diff)} 个块不同")
        print(f"  ⚠️  前提：设备当前内容必须等于该基准镜像，否则结果会是混合状态")
        remaining = [i for i in diff if i not in done]
    else:
        remaining = [i for i in range(lo, hi) if i not in done]
    print(f"  已完成  : {len(done)} 块    剩余 {len(remaining)} 块")

    if a.dry_run:
        print(f"\n[DRY-RUN] 将写入 {len(remaining)} 块 = "
              f"{len(remaining) * a.chunk_kb / 1024:.1f} MiB")
        print(f"[DRY-RUN] 第一块扇区 0x{PARTITION_START_BLOCK:06x}，"
              f"最后一块扇区 0x{PARTITION_START_BLOCK + (total - 1) * blocks_per_chunk:06x}")
        return 0

    if not remaining:
        print("\n[+] 全部块已写完 —— 本次没有执行任何写入")
        print(f"    原因：进度文件 {PROGRESS_FILE.name} 里已有本镜像（{pkey}）的完整记录。")
        print(f"    如需强制重刷：加 --reset-progress，或 --range 0:{total}")
        print("    如需确认设备上真的是这个镜像：uv run python tools/flash/probe_state.py（读超级块）")
        return 0

    stage2 = STAGE2_TURBO_BIN.read_bytes()
    f = img.open("rb")
    fb = FastbootDevice()
    rounds = 0
    t_start = time.time()
    last_ping = [t_start]

    while remaining and rounds < a.max_rounds:
        rounds += 1
        try:
            fb.connect()
            print(f"[+] 已连接（第 {rounds} 轮）", flush=True)
            fb.do_download(stage2)
            fb.send_cmd("oem load:0xfe300000")
            fb.send_cmd(f"oem poke:0xfe320000:0x{PARTITION_START_BLOCK:08x}")
            fb.send_cmd("oem poke:0xfe320004:0x00000001")
            st, msg, _ = fb.send_cmd("oem call:0xfe300001")
            if st != "OKAY":
                raise RuntimeError(f"eMMC Init 失败: {st} {msg}")
            st, msg, _ = fb.send_cmd("oem call:0xfe300601")
            if st == "OKAY":
                print("[+] 极速总线已激活", flush=True)

            session = 0
            while remaining:
                idx = remaining[0]
                sector = PARTITION_START_BLOCK + idx * blocks_per_chunk
                if sector == FORBIDDEN_SECTOR and not a.allow_wbi:
                    raise RuntimeError(
                        "拒绝写入受保护扇区 0x2C000（nflasha5 WBI1 头部）。"
                        "若确实要整盘刷回 nflasha5 原厂备份，加 --allow-wbi")

                f.seek(idx * chunk_size)
                data = f.read(chunk_size)
                if len(data) != chunk_size:
                    raise RuntimeError(f"镜像块 {idx} 读取截断")

                t0 = time.time()
                fb.do_download(data)
                fb.send_cmd("oem load:0xfe400000")
                fb.send_cmd(f"oem poke:0xfe320000:0x{sector:08x}")
                fb.send_cmd(f"oem poke:0xfe320004:0x{blocks_per_chunk:08x}")
                st, msg, _ = fb.send_cmd("oem call:0xfe300701")
                dt = time.time() - t0
                if st != "OKAY":
                    raise RuntimeError(f"块 {idx} 写入失败: {st} {msg}")

                # ---- 保活：只在「块写完成」这个安全点插入，绝不在 bulk 传输中并发发送 ----
                if a.ping > 0:
                    now = time.time()
                    if now - last_ping[0] >= a.ping:
                        try:
                            fb.send_cmd("oem darwin")
                            last_ping[0] = now
                            print(f"    [ping] oem darwin  ({now - t_start:.0f}s)", flush=True)
                        except Exception as e:
                            print(f"    [ping] 失败: {e}", flush=True)

                done.add(idx)
                save_progress(pkey, done)
                remaining.pop(0)
                session += 1

                if session % 10 == 0 or not remaining:
                    pct = len(done) / total * 100
                    el = time.time() - t_start
                    eta = (el / len(done)) * len(remaining) if done else 0
                    print(f"  [{len(done):4d}/{total} {pct:5.1f}%] 块 #{idx:4d} "
                          f"OK ({dt:.2f}s) 本轮 {session}  剩余 {fmt(eta)}", flush=True)

        except Exception as e:
            print(f"\n[!] 通道中断: {e}", flush=True)
            print(f"[*] 本轮写入完成 {len(done)}/{total}，断点已保存，等待重连...",
                  flush=True)
            fb.close()
            time.sleep(0.5)

    f.close()
    fb.close()
    print("\n" + "=" * 74)
    if remaining:
        print(f"  ⚠️  未完成，剩余 {len(remaining)} 块")
        print("     → 重新上电进 Fastboot 后再跑同一条命令即可续传")
    else:
        print(f"  ✅ 写入完成：实际写 {len(done)} 块（镜像共 {total} 块），"
              f"用时 {fmt(time.time() - t_start)}")
        print("     注意：本工具不会清 WBI。若引导异常，用 tools/flash/restore_wbi_auto.py 恢复原厂头部")
    print("=" * 74)
    return 0 if not remaining else 2


if __name__ == "__main__":
    sys.exit(main())
