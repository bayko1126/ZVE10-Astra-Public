#!/usr/bin/env python3
"""Sony ZV-E10 / Gen 5 ASTRA QEMU 网页交互控制器。

提供本地 HTTP 服务，将 QEMU ARM 模拟器的 640x480 物理显存 (0x20000000)
通过 QMP pmemsave 实时解码为 PNG 帧流送往网页端，并将网页端点击/拖拽
转换为串口命令 (TOUCH DOWN/MOVE/UP/TAP x y) 实时注入 Guest appFwBoot.so。
"""

from __future__ import annotations

import argparse
from collections import deque
import ctypes
import hashlib
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

try:
    from PIL import Image
except ImportError as exc:
    sys.exit(f"[错误] WSL python3 缺少 Pillow (PIL): {exc}")


QEMU_WORK_DIR = os.environ.get("ZVE10_QEMU_WORK") or str(Path.home() / "zve10-qemu" / "work")
DEFAULT_QEMU_BIN = os.environ.get("ZVE10_QEMU_BIN") or f"{QEMU_WORK_DIR}/qemu-src/build-zve10/qemu-system-arm"
DEFAULT_KERNEL = os.environ.get("ZVE10_KERNEL_IMAGE") or f"{QEMU_WORK_DIR}/kbuild-ui/arch/arm/boot/Image"
DEFAULT_INITRD = os.environ.get("ZVE10_ASTRA_INITRD") or f"{QEMU_WORK_DIR}/astra.cpio"
DEFAULT_QMP_SOCK = "/tmp/qmp-astra-web.sock"
DEFAULT_SERIAL_SOCK = "/tmp/serial-astra-web.sock"
DEFAULT_FB_DUMP = "/tmp/astra_web_fb.bin"
DEFAULT_VRAM_PHYS = 0x20000000
DEFAULT_WIDTH = 640
DEFAULT_HEIGHT = 480

_C_DECODER_SRC = r"""
#include <stdint.h>

void rgba4444_to_rgba8888(const uint8_t *src, uint8_t *dst, int npixels)
{
    for (int i = 0; i < npixels; i++) {
        uint16_t v = (uint16_t)src[2 * i] | ((uint16_t)src[2 * i + 1] << 8);
        uint32_t idx = (uint32_t)i * 4u;
        dst[idx + 0] = (uint8_t)(((v >> 12) & 0xFu) * 17u);
        dst[idx + 1] = (uint8_t)(((v >> 8)  & 0xFu) * 17u);
        dst[idx + 2] = (uint8_t)(((v >> 4)  & 0xFu) * 17u);
        dst[idx + 3] = (uint8_t)((v         & 0xFu) * 17u);
    }
}
"""


class Rgba4444Decoder:
    """将小端 RGBA4444 显存高效解码为 PNG 字节流。"""

    def __init__(self, width: int, height: int) -> None:
        self.width = width
        self.height = height
        self.npixels = width * height
        self._c_fn = self._try_build_c_helper()
        self._lut: list[bytes] | None = None
        if self._c_fn is None:
            self._lut = self._build_lut()

    @staticmethod
    def _try_build_c_helper() -> Any:
        gcc = shutil.which("gcc")
        if not gcc:
            return None
        so_path = Path("/tmp/astra_rgba4444_v1.so")
        c_path = Path("/tmp/astra_rgba4444_v1.c")
        try:
            if not so_path.exists():
                c_path.write_text(_C_DECODER_SRC, encoding="utf-8")
                subprocess.run(
                    [gcc, "-O2", "-shared", "-fPIC", str(c_path), "-o", str(so_path)],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )
            lib = ctypes.CDLL(str(so_path))
            fn = lib.rgba4444_to_rgba8888
            fn.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
            fn.restype = None
            return fn
        except Exception:
            return None

    @staticmethod
    def _build_lut() -> list[bytes]:
        return [
            bytes((
                ((w >> 12) & 0xF) * 17,
                ((w >> 8) & 0xF) * 17,
                ((w >> 4) & 0xF) * 17,
                (w & 0xF) * 17,
            ))
            for w in range(65536)
        ]

    def decode_to_png(self, raw: bytes) -> bytes:
        png, _ = self.decode_to_png_info(raw)
        return png

    def decode_to_png_info(self, raw: bytes) -> tuple[bytes, bool]:
        expected = self.npixels * 2
        if len(raw) < expected:
            raise ValueError(f"显存长度不足: got {len(raw)}, need {expected}")
        if self._c_fn is not None:
            out_buf = ctypes.create_string_buffer(self.npixels * 4)
            self._c_fn(raw[:expected], out_buf, self.npixels)
            rgba_bytes = out_buf.raw
        else:
            assert self._lut is not None
            lut = self._lut
            rgba_bytes = b"".join(lut[w] for (w,) in struct.iter_unpack("<H", raw[:expected]))

        img = Image.frombytes("RGBA", (self.width, self.height), rgba_bytes)
        ext = img.getextrema()
        has_rgb = (ext[0][1] > 0) or (ext[1][1] > 0) or (ext[2][1] > 0)
        bio = io.BytesIO()
        img.save(bio, format="PNG", compress_level=1)
        return bio.getvalue(), has_rgb


class QmpClient:
    """线程外部加锁调用的持久 QMP UNIX Socket 客户端。"""

    def __init__(self, sock_path: str) -> None:
        self.sock_path = sock_path
        self._sock: socket.socket | None = None
        self._buf = bytearray()

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        self._buf.clear()

    def _readline_json(self, timeout: float = 3.0) -> dict[str, Any]:
        assert self._sock is not None
        self._sock.settimeout(timeout)
        while True:
            nl = self._buf.find(b"\n")
            if nl != -1:
                line = bytes(self._buf[:nl]).decode("utf-8", errors="replace").strip()
                del self._buf[: nl + 1]
                if not line:
                    continue
                return json.loads(line)
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError("QMP 连接被对端关闭")
            self._buf.extend(chunk)

    def ensure_connected(self) -> None:
        if self._sock is not None:
            return
        if not os.path.exists(self.sock_path):
            raise FileNotFoundError(f"QMP socket 尚未创建: {self.sock_path}")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(3.0)
        sock.connect(self.sock_path)
        self._sock = sock
        self._buf.clear()
        greeting = self._readline_json(timeout=3.0)
        if "QMP" not in greeting:
            self.close()
            raise RuntimeError(f"异常 QMP 握手头: {greeting}")
        self._send_and_wait({"execute": "qmp_capabilities"}, timeout=3.0)

    def _send_and_wait(self, payload: dict[str, Any], timeout: float = 3.0) -> dict[str, Any]:
        assert self._sock is not None
        raw = (json.dumps(payload) + "\n").encode("utf-8")
        self._sock.sendall(raw)
        deadline = time.monotonic() + timeout
        while True:
            rem = max(0.2, deadline - time.monotonic())
            msg = self._readline_json(timeout=rem)
            if "event" in msg:
                continue
            if "return" in msg:
                return msg
            if "error" in msg:
                raise RuntimeError(f"QMP 错误: {msg['error']}")

    def execute(self, command: str, arguments: dict[str, Any] | None = None, timeout: float = 3.0) -> dict[str, Any]:
        payload: dict[str, Any] = {"execute": command}
        if arguments is not None:
            payload["arguments"] = arguments
        try:
            self.ensure_connected()
            return self._send_and_wait(payload, timeout=timeout)
        except Exception:
            self.close()
            self.ensure_connected()
            return self._send_and_wait(payload, timeout=timeout)

    def snapshot_raw(self, phys: int, size: int, dump_file: str) -> bytes:
        self.ensure_connected()
        stopped = False
        try:
            self._send_and_wait({"execute": "stop"}, timeout=3.0)
            stopped = True
            self._send_and_wait(
                {"execute": "pmemsave", "arguments": {"val": phys, "size": size, "filename": dump_file}},
                timeout=4.0,
            )
        finally:
            if stopped:
                try:
                    self._send_and_wait({"execute": "cont"}, timeout=3.0)
                except Exception:
                    self.close()
        with open(dump_file, "rb") as fp:
            return fp.read(size)


class AstraQemuController:
    """管理 QEMU 实例生命周期、串口交互、帧抓取与状态同步。"""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.width = args.width
        self.height = args.height
        self.frame_bytes = self.width * self.height * 2
        self.decoder = Rgba4444Decoder(self.width, self.height)
        self.qmp = QmpClient(args.qmp_sock)

        self._lifecycle_lock = threading.Lock()
        self._qmp_lock = threading.Lock()
        self._serial_tx_lock = threading.Lock()
        self._serial_sock_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._frame_cond = threading.Condition()
        self._capture_event = threading.Event()

        self._stop_all = False
        self._gen = 0
        self._proc: subprocess.Popen[bytes] | None = None
        self._serial_sock: socket.socket | None = None

        self.phase = "booting"
        self.status_text = "正在初始化 QEMU 环境…"
        self.uart_ready = False
        self.ui_ready = False
        self.bootin_started = False
        self._restarting = False
        self.frame_seq = 0
        self.touch_sent = 0
        self.touch_ack = 0
        self.key_sent = 0
        self.key_ack = 0
        self.last_raw_digest: bytes | None = None
        self.latest_png: bytes | None = None
        self.logs: deque[str] = deque(maxlen=500)
        self._log_fp: Any = None

    @staticmethod
    def _is_ui_filtered_log(text: str) -> bool:
        stripped = text.lstrip()
        if stripped.startswith("[QEMU]"):
            stripped = stripped[6:].lstrip()
        return stripped.startswith("hwtimer_on:")

    def append_log(self, line: str) -> None:
        text = line.rstrip("\r\n")
        if not text:
            return
        with self._state_lock:
            if not self._is_ui_filtered_log(text):
                self.logs.append(text)
            if self._log_fp is not None:
                try:
                    self._log_fp.write(text + "\n")
                    self._log_fp.flush()
                except OSError:
                    pass

    def _cleanup_sockets(self) -> None:
        for p in (self.args.qmp_sock, self.args.serial_sock, self.args.fb_dump):
            try:
                if os.path.exists(p):
                    os.unlink(p)
            except OSError:
                pass

    def start(self) -> None:
        self.restart_qemu()
        t_cap = threading.Thread(target=self._capture_worker, name="astra-capture", daemon=True)
        t_cap.start()

    def shutdown(self) -> None:
        self._stop_all = True
        self._capture_event.set()
        with self._frame_cond:
            self._frame_cond.notify_all()
        self._stop_qemu_locked()

    def _stop_qemu_locked(self) -> None:
        with self._serial_sock_lock:
            if self._serial_sock is not None:
                try:
                    self._serial_sock.close()
                except OSError:
                    pass
                self._serial_sock = None
        with self._qmp_lock:
            if self._proc is not None and self._proc.poll() is None:
                try:
                    self.qmp.execute("quit", timeout=1.5)
                except Exception:
                    pass
            self.qmp.close()

        proc = self._proc
        self._proc = None
        if proc is not None and proc.poll() is None:
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2.0)

        with self._state_lock:
            self.phase = "stopped"
            self.status_text = "QEMU 已停止"
            if self._log_fp is not None:
                try:
                    self._log_fp.close()
                except OSError:
                    pass
                self._log_fp = None

    def request_restart(self) -> None:
        with self._frame_cond:
            self.frame_seq = 0
            self.latest_png = None
            self.last_raw_digest = None
            self._frame_cond.notify_all()
        with self._state_lock:
            self._gen += 1
            target_gen = self._gen
            self._restarting = True
            self.phase = "booting"
            self.status_text = "正在重启 QEMU 环境…"
            self.uart_ready = False
            self.ui_ready = False
            self.bootin_started = False
            self.touch_sent = 0
            self.touch_ack = 0
            self.key_sent = 0
            self.key_ack = 0
            self.logs.clear()
        threading.Thread(target=self.restart_qemu, args=(target_gen,), daemon=True).start()

    def restart_qemu(self, expected_gen: int | None = None) -> None:
        with self._lifecycle_lock:
            if expected_gen is not None and expected_gen != self._gen:
                return
            if expected_gen is None:
                with self._state_lock:
                    self._gen += 1
                    gen = self._gen
                    self._restarting = True
            else:
                gen = expected_gen
            self._stop_qemu_locked()
            self._cleanup_sockets()

            with self._frame_cond:
                self.frame_seq = 0
                self.latest_png = None
                self.last_raw_digest = None
                self._frame_cond.notify_all()

            with self._state_lock:
                self.phase = "booting"
                self.status_text = "正在启动 Linux 内核与 ASTRA 引擎…"
                self.uart_ready = False
                self.ui_ready = False
                self.bootin_started = False
                self.touch_sent = 0
                self.touch_ack = 0
                self.key_sent = 0
                self.key_ack = 0
                self.logs.clear()
                boot_mode = getattr(self.args, "boot_mode", "menu")
                try:
                    self._log_fp = open("/tmp/astra_web_serial.log", "w", encoding="utf-8")
                    self._log_fp.write(f"[HOST] 正在拉起 QEMU (zve10 / web_ui=1 / astra_boot={boot_mode})...\n")
                    self._log_fp.flush()
                except OSError:
                    self._log_fp = None
                self.logs.append(f"[HOST] 正在拉起 QEMU (zve10 / web_ui=1 / astra_boot={boot_mode})...")

            cmdline = (
                "console=ttyAM0,115200n8 cfg.uart=1 mem=1G@0 "
                "memrsv=0x1A5E000@0x02E68000 memrsv=128M@0x20000000:uc "
                f"rdinit=/init loglevel=4 web_ui=1 astra_boot={boot_mode}"
            )
            cmd = [
                self.args.qemu_bin,
                "-M", "zve10",
                "-cpu", "cortex-a9",
                "-smp", "1",
                "-m", "1024",
                "-kernel", self.args.kernel,
                "-initrd", self.args.initrd,
                "-append", cmdline,
                "-display", "none",
                "-monitor", "none",
                "-no-reboot",
                "-qmp", f"unix:{self.args.qmp_sock},server=on,wait=off",
                "-serial", f"unix:{self.args.serial_sock},server=on,wait=off",
            ]
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            with self._state_lock:
                self._restarting = False

            threading.Thread(target=self._stderr_worker, args=(gen, self._proc), daemon=True).start()
            threading.Thread(target=self._serial_worker, args=(gen,), daemon=True).start()
            self._capture_event.set()

    def _stderr_worker(self, gen: int, proc: subprocess.Popen[bytes]) -> None:
        if proc.stderr is None:
            return
        try:
            for raw in iter(proc.stderr.readline, b""):
                if self._stop_all or gen != self._gen:
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                if line:
                    self.append_log(f"[QEMU] {line}")
        except Exception:
            pass
        rc = proc.poll()
        if not self._stop_all and gen == self._gen and rc is not None:
            self.append_log(f"[HOST] QEMU 进程已退出 (code={rc})")
            with self._state_lock:
                self.phase = "stopped"
                self.status_text = f"QEMU 已退出 (code={rc})"

    def _serial_worker(self, gen: int) -> None:
        sock_path = self.args.serial_sock
        deadline = time.monotonic() + 15.0
        while not self._stop_all and gen == self._gen and not os.path.exists(sock_path):
            if time.monotonic() > deadline:
                self.append_log("[HOST] 等待串口 socket 超时")
                return
            time.sleep(0.05)

        if self._stop_all or gen != self._gen:
            return

        sock: socket.socket | None = None
        for _ in range(40):
            if self._stop_all or gen != self._gen:
                return
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.connect(sock_path)
                sock.settimeout(1.0)
                break
            except OSError:
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
                    sock = None
                time.sleep(0.05)

        if sock is None:
            self.append_log("[HOST] 无法连接 QEMU 串口 socket")
            return

        with self._serial_sock_lock:
            if gen != self._gen or self._stop_all:
                sock.close()
                return
            self._serial_sock = sock

        buf = bytearray()
        while not self._stop_all and gen == self._gen:
            try:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf.extend(chunk)
                while True:
                    nl = buf.find(b"\n")
                    if nl == -1:
                        break
                    line_bytes = bytes(buf[:nl])
                    del buf[: nl + 1]
                    line = line_bytes.decode("utf-8", errors="replace").strip("\r\n ")
                    if line:
                        self._handle_serial_line(line)
            except socket.timeout:
                continue
            except OSError:
                break

        with self._serial_sock_lock:
            if self._serial_sock is sock:
                self._serial_sock = None
        try:
            sock.close()
        except OSError:
            pass

    def _handle_serial_line(self, line: str) -> None:
        self.append_log(line)

        if "ASTRA_WEB_READY" in line:
            with self._state_lock:
                self.bootin_started = True
                self.uart_ready = True
                if self.ui_ready and self.frame_seq > 0:
                    self.phase = "ready"
                    self.status_text = "ASTRA UI 就绪 · 支持鼠标点击与拖拽触控"
                else:
                    self.status_text = "触控串口已就绪，等待首帧 UI 画面…"
            self._capture_event.set()
            return

        if "ASTRA_UI_READY" in line:
            with self._state_lock:
                self.bootin_started = True
                self.ui_ready = True
                if self.phase != "ready":
                    self.status_text = "ASTRA 首帧已导出，同步显存…"
            with self._frame_cond:
                self.last_raw_digest = None
            self._capture_event.set()
            return

        if "ASTRA_FB_SYNC" in line:
            with self._state_lock:
                self.bootin_started = True
            self._capture_event.set()
            return

        if "ASTRA_TOUCH_ACK" in line:
            with self._state_lock:
                self.touch_ack += 1
            self._capture_event.set()
            return

        if (
            "ASTRA_KEY_ACK" in line
            or "ASTRA_EVENT_ACK" in line
            or "ASTRA_OPEN_MENU_ACK" in line
            or "ASTRA_SWITCH_SCR_ACK" in line
        ):
            with self._state_lock:
                self.key_ack += 1
            self._capture_event.set()
            return

        if "UART_TOUCH: 已监听" in line:
            with self._state_lock:
                if not self.uart_ready:
                    self.status_text = "触控串口已挂接，等待首帧渲染…"
            return

        if "Starting bootin.elf" in line:
            with self._state_lock:
                self.bootin_started = True
                if self.phase == "booting":
                    self.status_text = "正在启动 bootin.elf 与图形渲染驱动…"
            return

        if "BOOTIN_EXIT=" in line or "Kernel panic" in line:
            with self._state_lock:
                self.status_text = f"引导异常: {line}"

    def _capture_worker(self) -> None:
        while not self._stop_all:
            # 系统稳定就绪后主要依赖 ASTRA_FB_SYNC 事件驱动，同时保留 0.8s 兜底巡检
            self._capture_event.wait(timeout=0.8)
            self._capture_event.clear()
            if self._stop_all:
                break
            with self._state_lock:
                can_poll = self.bootin_started and not self._restarting
            if can_poll:
                self.capture_frame(force=False)

    def capture_frame(self, force: bool = False) -> bytes | None:
        gen = self._gen
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return None
        if not os.path.exists(self.args.qmp_sock):
            return None

        try:
            with self._qmp_lock:
                if gen != self._gen:
                    return None
                raw = self.qmp.snapshot_raw(self.args.vram_phys, self.frame_bytes, self.args.fb_dump)
        except Exception:
            return None

        if gen != self._gen:
            return None
        if len(raw) < self.frame_bytes:
            return None

        # 首帧全黑/未初始化时跳过（force 时允许导出）
        if not force and max(raw[:65536]) == 0 and raw.count(0) == len(raw):
            return None

        digest = hashlib.blake2b(raw, digest_size=16).digest()
        with self._frame_cond:
            if gen != self._gen:
                return None
            if not force and digest == self.last_raw_digest and self.latest_png is not None:
                return self.latest_png

        png, has_rgb = self.decoder.decode_to_png_info(raw)
        if gen != self._gen:
            return None
        if not has_rgb and self.frame_seq == 0:
            # 系统尚未渲染出任何可见 UI 像素前，force 快照仅返回预览图，不污染 frame_seq
            return png if force else None
        if not force and not has_rgb:
            return None
        with self._frame_cond:
            if gen != self._gen:
                return None
            changed = digest != self.last_raw_digest or self.latest_png is None
            self.last_raw_digest = digest
            self.latest_png = png
            if changed or self.frame_seq == 0:
                self.frame_seq += 1
            self._frame_cond.notify_all()

        with self._state_lock:
            if gen != self._gen:
                return None
            if self.uart_ready and self.ui_ready and self.phase != "ready":
                self.phase = "ready"
                self.status_text = "ASTRA UI 就绪 · 支持鼠标点击与拖拽触控"
            elif not self.ui_ready and self.phase == "booting":
                if self.uart_ready:
                    self.status_text = "触控串口已就绪，等待首帧 UI 画面…"
                elif self.frame_seq > 0:
                    self.status_text = "正在等待 ASTRA 引擎初始化…"
            elif not self.uart_ready and self.frame_seq > 0 and self.phase == "booting":
                self.status_text = "画面已出图，等待触控通道就绪…"
        return png

    def send_key(self, key: str, mode: str = "PRESS") -> dict[str, Any]:
        k_up = key.strip().upper()
        valid_keys = {
            "UP", "DOWN", "LEFT", "RIGHT",
            "ENTER", "OK", "CENTER", "MENU",
            "MS_UP", "MS_DOWN", "MS_LEFT", "MS_RIGHT", "MS_ENTER",
            "FN", "PLAY", "DELETE", "DEL",
            "WHEEL_RIGHT", "WHEEL_LEFT", "DIAL_R", "DIAL_L", "LOCK",
        }
        if k_up not in valid_keys:
            raise ValueError(f"不支持的物理按键: {key}")
        m_up = mode.strip().upper()
        if m_up == "PRESS":
            cmd = f"KEY {k_up}"
        elif m_up == "DOWN":
            cmd = f"KEYDOWN {k_up}"
        elif m_up == "UP":
            cmd = f"KEYUP {k_up}"
        else:
            raise ValueError(f"不支持的按键模式: {mode}")
        self.send_uart_line(cmd)
        with self._state_lock:
            self.key_sent += 1
            sent = self.key_sent
        return {"ok": True, "key": k_up, "mode": m_up, "key_sent": sent}

    def wait_for_frame(self, after_seq: int, timeout: float) -> tuple[int, bytes | None]:
        with self._frame_cond:
            if self.latest_png is not None and self.frame_seq > after_seq:
                return self.frame_seq, self.latest_png
            self._frame_cond.wait_for(
                lambda: self._stop_all or (self.latest_png is not None and self.frame_seq > after_seq),
                timeout=timeout,
            )
            if self.latest_png is not None and self.frame_seq > after_seq:
                return self.frame_seq, self.latest_png
            return self.frame_seq, None

    def send_uart_line(self, line: str) -> None:
        with self._state_lock:
            if self._restarting:
                raise RuntimeError("QEMU 正在重启，请稍候")
        with self._serial_sock_lock:
            sock = self._serial_sock
            if sock is None:
                raise RuntimeError("QEMU 串口尚未就绪")
        payload = (line.strip() + "\n").encode("ascii")
        with self._serial_tx_lock:
            with self._serial_sock_lock:
                if self._serial_sock is None:
                    raise RuntimeError("QEMU 串口连接已断开")
                self._serial_sock.sendall(payload)

    def send_touch(self, op: str, x: int, y: int) -> dict[str, Any]:
        op_up = op.strip().upper()
        if op_up not in ("DOWN", "MOVE", "UP", "TAP"):
            raise ValueError(f"不支持的触控操作: {op}")
        cx = max(0, min(self.width - 1, int(x)))
        cy = max(0, min(self.height - 1, int(y)))
        if op_up == "TAP":
            self.send_uart_line(f"TOUCH DOWN {cx} {cy}")
            time.sleep(0.08)
            self.send_uart_line(f"TOUCH UP {cx} {cy}")
            sent_delta = 2
        else:
            self.send_uart_line(f"TOUCH {op_up} {cx} {cy}")
            sent_delta = 1
        with self._state_lock:
            self.touch_sent += sent_delta
            sent = self.touch_sent
        return {"ok": True, "op": op_up, "x": cx, "y": cy, "touch_sent": sent}

    def get_state(self) -> dict[str, Any]:
        proc = self._proc
        proc_running = proc is not None and proc.poll() is None
        with self._state_lock:
            running = proc_running or self._restarting
            phase = self.phase if running else "stopped"
            status_text = self.status_text
            uart_ready = self.uart_ready
            touch_sent = self.touch_sent
            touch_ack = self.touch_ack
            logs = list(self.logs)
        with self._frame_cond:
            fseq = self.frame_seq
        return {
            "phase": phase,
            "status_text": status_text,
            "running": running,
            "uart_ready": uart_ready,
            "frame_seq": fseq,
            "touch_sent": touch_sent,
            "touch_ack": touch_ack,
            "logs": logs,
        }


def make_handler(ctrl: AstraQemuController, html_path: Path) -> type[BaseHTTPRequestHandler]:
    class AstraWebHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send_bytes(
            self,
            status: int,
            content_type: str,
            body: bytes,
            extra_headers: dict[str, str] | None = None,
        ) -> None:
            try:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                if extra_headers:
                    for k, v in extra_headers.items():
                        self.send_header(k, v)
                self.end_headers()
                if body:
                    self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _send_json(self, status: int, data: Any) -> None:
            payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self._send_bytes(status, "application/json; charset=utf-8", payload)

        def _read_json_body(self) -> dict[str, Any]:
            clen = int(self.headers.get("Content-Length", "0") or "0")
            if clen <= 0 or clen > 65536:
                raise ValueError("无效请求体长度")
            raw = self.rfile.read(clen)
            obj = json.loads(raw.decode("utf-8"))
            if not isinstance(obj, dict):
                raise ValueError("请求体必须为 JSON 对象")
            return obj

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path

            if path in ("/", "/index.html"):
                if not html_path.is_file():
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": f"找不到网页模板: {html_path}"})
                    return
                body = html_path.read_bytes()
                self._send_bytes(HTTPStatus.OK, "text/html; charset=utf-8", body)
                return

            if path == "/favicon.ico":
                self.send_response(HTTPStatus.NO_CONTENT)
                self.end_headers()
                return

            if path == "/api/state":
                self._send_json(HTTPStatus.OK, ctrl.get_state())
                return

            if path == "/api/frame.png":
                qs = parse_qs(parsed.query)
                try:
                    after = int(qs.get("after", ["0"])[0])
                except ValueError:
                    after = 0
                try:
                    wait = float(qs.get("wait", ["4"])[0])
                except ValueError:
                    wait = 4.0
                wait = max(0.05, min(10.0, wait))

                seq, png = ctrl.wait_for_frame(after_seq=after, timeout=wait)
                if png is None:
                    self._send_bytes(HTTPStatus.NO_CONTENT, "image/png", b"")
                    return
                self._send_bytes(
                    HTTPStatus.OK,
                    "image/png",
                    png,
                    extra_headers={"X-Frame-Seq": str(seq)},
                )
                return

            if path == "/api/snapshot.png":
                png = ctrl.capture_frame(force=True)
                if png is None:
                    with ctrl._frame_cond:
                        png = ctrl.latest_png
                        seq = ctrl.frame_seq
                else:
                    seq = ctrl.frame_seq
                if png is None:
                    self._send_bytes(HTTPStatus.NO_CONTENT, "image/png", b"")
                    return
                self._send_bytes(
                    HTTPStatus.OK,
                    "image/png",
                    png,
                    extra_headers={"X-Frame-Seq": str(seq)},
                )
                return

            self._send_json(HTTPStatus.NOT_FOUND, {"error": "未定义路径"})

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            try:
                body = self._read_json_body()
            except Exception as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                return

            if path == "/api/touch":
                try:
                    op = str(body.get("op", ""))
                    x = int(round(float(body["x"])))
                    y = int(round(float(body["y"])))
                    res = ctrl.send_touch(op, x, y)
                    self._send_json(HTTPStatus.OK, res)
                except Exception as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                return

            if path == "/api/key":
                try:
                    key = str(body.get("key", ""))
                    mode = str(body.get("mode", "PRESS"))
                    res = ctrl.send_key(key, mode)
                    self._send_json(HTTPStatus.OK, res)
                except Exception as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                return

            if path == "/api/control":
                action = str(body.get("action", "")).strip().lower()
                if action in ("open_menu", "openmenu"):
                    try:
                        ctrl.send_uart_line("OPEN_MENU")
                        with ctrl._state_lock:
                            ctrl.key_sent += 1
                        self._send_json(HTTPStatus.OK, {"ok": True, "action": action, "cmd": "OPEN_MENU"})
                    except Exception as exc:
                        self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "error": str(exc)})
                    return
                if action in ("open_shoot", "openshoot"):
                    try:
                        ctrl.send_uart_line("OPEN_SHOOT")
                        with ctrl._state_lock:
                            ctrl.key_sent += 1
                        self._send_json(HTTPStatus.OK, {"ok": True, "action": action, "cmd": "OPEN_SHOOT"})
                    except Exception as exc:
                        self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "error": str(exc)})
                    return
                if action == "menu":
                    try:
                        if body.get("direct"):
                            ctrl.send_uart_line("OPEN_MENU")
                            with ctrl._state_lock:
                                ctrl.key_sent += 1
                            self._send_json(HTTPStatus.OK, {"ok": True, "action": "menu", "cmd": "OPEN_MENU"})
                        else:
                            res = ctrl.send_key("MENU", "PRESS")
                            self._send_json(HTTPStatus.OK, {"ok": True, "action": "menu", **res})
                    except Exception as exc:
                        self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "error": str(exc)})
                    return
                if action == "threads":
                    try:
                        ctrl.send_uart_line("THREADS")
                        self._send_json(HTTPStatus.OK, {"ok": True, "action": "threads"})
                    except Exception as exc:
                        self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "error": str(exc)})
                    return
                if action == "uart":
                    try:
                        cmd_line = str(body.get("cmd", "")).strip()
                        if not cmd_line:
                            raise ValueError("cmd 不能为空")
                        ctrl.send_uart_line(cmd_line)
                        self._send_json(HTTPStatus.OK, {"ok": True, "action": "uart", "cmd": cmd_line})
                    except Exception as exc:
                        self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                if action == "event":
                    try:
                        eid = str(body.get("event_id", "")).strip()
                        val = int(eid, 16)
                        ctrl.send_uart_line(f"EVENT {val:08x}")
                        self._send_json(HTTPStatus.OK, {"ok": True, "action": "event", "event_id": f"0x{val:08x}"})
                    except Exception as exc:
                        self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                if action == "refresh":
                    png = ctrl.capture_frame(force=True)
                    self._send_json(
                        HTTPStatus.OK,
                        {"ok": png is not None, "action": "refresh", "frame_seq": ctrl.frame_seq},
                    )
                    return
                if action == "ping":
                    try:
                        ctrl.send_uart_line("PING")
                        self._send_json(HTTPStatus.OK, {"ok": True, "action": "ping"})
                    except Exception as exc:
                        self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "error": str(exc)})
                    return
                if action == "restart":
                    ctrl.request_restart()
                    self._send_json(HTTPStatus.OK, {"ok": True, "action": "restart"})
                    return
                self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": f"未知控制动作: {action}"})
                return

            self._send_json(HTTPStatus.NOT_FOUND, {"error": "未定义路径"})

    return AstraWebHandler


class ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sony ZV-E10 ASTRA QEMU Web UI 交互后端")
    parser.add_argument("--host", default="0.0.0.0", help="HTTP 监听地址 (默认 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8765, help="HTTP 监听端口 (默认 8765)")
    parser.add_argument("--qemu-bin", default=DEFAULT_QEMU_BIN)
    parser.add_argument("--kernel", default=DEFAULT_KERNEL)
    parser.add_argument("--initrd", default=DEFAULT_INITRD)
    parser.add_argument("--html", default=str(Path(__file__).resolve().with_name("astra_web_ui.html")))
    parser.add_argument("--qmp-sock", default=DEFAULT_QMP_SOCK)
    parser.add_argument("--serial-sock", default=DEFAULT_SERIAL_SOCK)
    parser.add_argument("--fb-dump", default=DEFAULT_FB_DUMP)
    parser.add_argument("--vram-phys", type=lambda s: int(s, 0), default=DEFAULT_VRAM_PHYS)
    parser.add_argument("--width", type=int, default=DEFAULT_WIDTH)
    parser.add_argument("--height", type=int, default=DEFAULT_HEIGHT)
    parser.add_argument(
        "--boot-mode",
        choices=["menu", "shooting", "normal"],
        default="menu",
        help="ASTRA 启动默认目标界面 (menu=直达菜单, shooting=直接拍摄, normal=保留原生向导; 默认 menu)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    html_path = Path(args.html)
    for req in (args.qemu_bin, args.kernel, args.initrd):
        if not os.path.exists(req):
            sys.exit(
                f"[错误] 缺少必需文件: {req}\n"
                f"请先参照 docs/qemu-prerequisites.md 搭建 QEMU/内核/initramfs 环境，\n"
                f"或通过环境变量 (ZVE10_QEMU_WORK / ZVE10_QEMU_BIN / ZVE10_KERNEL_IMAGE / ZVE10_ASTRA_INITRD) 或命令行参数指定路径。"
            )
    if not html_path.is_file():
        sys.exit(f"[错误] 缺少前端网页文件: {html_path}")

    ctrl = AstraQemuController(args)
    server = ReusableThreadingHTTPServer((args.host, args.port), make_handler(ctrl, html_path))

    def _on_signal(_signum: int, _frame: Any) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    ctrl.start()
    print(f"[+] ASTRA 网页交互版 QEMU 已启动", flush=True)
    print(f"    浏览器访问: http://127.0.0.1:{args.port}/", flush=True)
    print(f"    QMP Socket: {args.qmp_sock}", flush=True)
    print(f"    Serial    : {args.serial_sock}", flush=True)

    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        ctrl.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
