#!/usr/bin/env python3
# /// script
# dependencies = ["pyusb>=1.2.1", "libusb-package>=1.0.26.2"]
# ///
"""Sony ZV-E10 Fastboot 会话保活模块与守护工具 (Fastboot Session Keep-Alive)

针对索尼 BIONZ X (CXD90045) 硬件看门狗在 Fastboot 阶段约 215~220 秒（3 分 40 秒）自动超时复位机制，
提供统一的协议层保活心跳 (`oem darwin`) 支持。

既可作为独立命令行守护程序运行，也可作为统一的基础会话库被救砖、刷写与分析脚本直接导入复用。
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
from typing import Any, Callable, Optional

try:
    import usb.core
    import usb.util
    import libusb_package
except ImportError:
    pass  # 支持离线测试导入；真机连接时需安装依赖


DEFAULT_PING_INTERVAL = 30.0
FASTBOOT_VID = 0x18D1
FASTBOOT_PID = 0x4EE0


def format_duration(seconds: float) -> str:
    """格式化持续时间为可读文本 (e.g. 02m 30s)"""
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h:02d}h {m:02d}m {s:02d}s"
    return f"{m:02d}m {s:02d}s"


class FastbootKeepaliveManager:
    """Fastboot 会话心跳与保活管理器。

    提供自适应时间差判定，确保在数据块写入间隙或空闲阶段安全插入 `oem darwin`，
    杜绝看门狗超时（~215s）导致的 USB 中断。
    """

    def __init__(self, interval: float = DEFAULT_PING_INTERVAL, command: str = "oem darwin"):
        self.interval = float(interval)
        self.command = command
        self.last_ping_time = 0.0
        self.session_start_time = 0.0
        self.ping_count = 0
        self.ping_errors = 0
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def reset_session(self) -> None:
        """重置会话计时器"""
        with self._lock:
            now = time.monotonic()
            self.session_start_time = now
            self.last_ping_time = now
            self.ping_count = 0
            self.ping_errors = 0

    def should_ping(self) -> bool:
        """判定当前是否达到保活心跳触发条件"""
        if self.interval <= 0:
            return False
        return (time.monotonic() - self.last_ping_time) >= self.interval

    def ping(self, send_cmd_fn: Callable[[str], tuple[str, bytes, list[str]]], verbose: bool = True) -> bool:
        """执行一次保活心跳交互"""
        with self._lock:
            now = time.monotonic()
            if self.session_start_time == 0.0:
                self.session_start_time = now
            try:
                tag, body, info = send_cmd_fn(self.command)
                self.last_ping_time = now
                self.ping_count += 1
                uptime = now - self.session_start_time
                if verbose:
                    body_text = body.decode("ascii", "replace").strip()
                    extra = f" ({body_text})" if body_text else ""
                    print(f"    [keepalive] 心跳保活 #{self.ping_count} ({self.command} -> {tag}{extra}) | 会话已存活: {format_duration(uptime)}", flush=True)
                return tag == "OKAY"
            except Exception as e:
                self.ping_errors += 1
                if verbose:
                    print(f"    [keepalive] ⚠️ 保活心跳发送失败: {e}", flush=True)
                return False

    def ping_if_due(self, send_cmd_fn: Callable[[str], tuple[str, bytes, list[str]]], verbose: bool = True) -> bool:
        """若已到保活周期，则安全执行一次心跳并刷新时间戳"""
        with self._lock:
            if self.should_ping():
                return self.ping(send_cmd_fn, verbose=verbose)
            return False

    def start_background(self, send_cmd_fn: Callable[[str], tuple[str, bytes, list[str]]], verbose: bool = True) -> None:
        """启动后台线程自主心跳（适用于纯等待/现场分析状态）"""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self.reset_session()

            def _worker():
                while not self._stop_event.is_set():
                    wait_step = max(0.01, min(self.interval, 1.0))
                    self._stop_event.wait(timeout=wait_step)
                    if self._stop_event.is_set():
                        break
                    if self.should_ping():
                        self.ping(send_cmd_fn, verbose=verbose)

            self._thread = threading.Thread(target=_worker, name="FastbootKeepaliveThread", daemon=True)
            self._thread.start()

    def stop_background(self) -> None:
        """停止后台心跳线程"""
        with self._lock:
            self._stop_event.set()
            if self._thread is not None:
                self._thread.join(timeout=3.0)
                self._thread = None


class FastbootSession:
    """统一的 Fastboot 会话客户端，原生内置保活心跳与看门狗免疫。"""

    def __init__(self, ping_interval: float = DEFAULT_PING_INTERVAL):
        self.dev = None
        self.ep_out = None
        self.ep_in = None
        self.keepalive = FastbootKeepaliveManager(interval=ping_interval)

    @property
    def lock(self) -> threading.RLock:
        return self.keepalive.lock

    def connect(self, timeout: Optional[float] = None) -> bool:
        """连接 Fastboot USB 设备 (18D1:4EE0)"""
        backend = libusb_package.get_libusb1_backend()
        start = time.time()
        first_msg = True
        while True:
            dev = usb.core.find(idVendor=FASTBOOT_VID, idProduct=FASTBOOT_PID, backend=backend)
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
                        intf,
                        custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT
                    )
                    self.ep_in = usb.util.find_descriptor(
                        intf,
                        custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN
                    )
                    self.keepalive.reset_session()
                    return True
                except Exception:
                    pass
            if first_msg:
                print("[-] 正在等待 Sony ZV-E10 Fastboot 设备上线 (18d1:4ee0)...", flush=True)
                first_msg = False
            if timeout and (time.time() - start > timeout):
                return False
            time.sleep(min(0.2, timeout) if timeout else 0.5)

    def close(self) -> None:
        """断开连接并释放资源"""
        self.keepalive.stop_background()
        if self.dev is not None:
            try:
                usb.util.dispose_resources(self.dev)
            except Exception:
                pass
            self.dev = None
            self.ep_out = None
            self.ep_in = None

    def send_cmd(self, c: str | bytes, timeout: int = 10000) -> tuple[str, bytes, list[str]]:
        """发送单条 Fastboot 命令并接收响应"""
        with self.lock:
            if not self.ep_out or not self.ep_in:
                raise RuntimeError("Fastboot 未连接")
            if isinstance(c, str):
                c = c.encode("ascii")
            self.ep_out.write(c, timeout=timeout)
            info_lines: list[str] = []
            while True:
                resp = bytes(self.ep_in.read(64, timeout=timeout))
                tag, body = resp[:4], resp[4:]
                if tag == b"INFO":
                    info_lines.append(body.decode("utf-8", "replace"))
                    continue
                return tag.decode("latin1"), body, info_lines

    def do_download(self, blob: bytes, timeout: int = 10000) -> None:
        """下载数据块到设备 RAM"""
        with self.lock:
            st, msg, _ = self.send_cmd(f"download:{len(blob):08x}", timeout=timeout)
            if st != "DATA":
                raise RuntimeError(f"Download rejected: {st} {msg}")
            chunk_size = 16384
            sent = 0
            while sent < len(blob):
                piece = blob[sent:sent + chunk_size]
                self.ep_out.write(piece, timeout=timeout)
                sent += len(piece)
            while True:
                resp = bytes(self.ep_in.read(64, timeout=timeout))
                tag, body = resp[:4], resp[4:]
                if tag == b"INFO":
                    continue
                if tag != b"OKAY":
                    raise RuntimeError(f"Data phase failed: {tag} {body}")
                break

    def ping(self, verbose: bool = True) -> bool:
        """立即执行一次保活心跳"""
        return self.keepalive.ping(self.send_cmd, verbose=verbose)

    def ping_if_due(self, verbose: bool = True) -> bool:
        """到期安全执行保活心跳"""
        return self.keepalive.ping_if_due(self.send_cmd, verbose=verbose)

    def start_keepalive(self, interval: Optional[float] = None, verbose: bool = True) -> None:
        """开启后台自主保活"""
        if interval is not None:
            self.keepalive.interval = interval
        self.keepalive.start_background(self.send_cmd, verbose=verbose)

    def stop_keepalive(self) -> None:
        """停止后台自主保活"""
        self.keepalive.stop_background()

    def get_var(self, var_name: str) -> Optional[str]:
        """查询 Fastboot 变量"""
        st, body, _ = self.send_cmd(f"getvar:{var_name}")
        if st == "OKAY":
            return body.decode("ascii", "replace").strip()
        return None

    def reboot(self) -> None:
        """重启设备"""
        try:
            self.send_cmd("reboot")
        except Exception:
            pass


def run_daemon(interval: float = 30.0, max_duration: float = 0.0, reboot_exit: bool = False) -> int:
    """作为独立保活守护进程运行"""
    print("=" * 68)
    print("  Sony ZV-E10 Fastboot 会话保活守护程序 (Session Keep-Alive)")
    print(f"  心跳指令: oem darwin | 设定间隔: {interval:.1f} 秒")
    print("=" * 68)

    session = FastbootSession(ping_interval=interval)
    try:
        if not session.connect():
            print("[FATAL] 无法连接到 Fastboot 设备")
            return 1

        prod = session.get_var("product") or "CXD90045"
        ver = session.get_var("version") or "unknown"
        print(f"[+] 设备已就绪: Product={prod}, FastbootVersion={ver}")
        print(f"[+] 正在启动保活维持循环（按 Ctrl+C 可安全退出）...\n")

        # 立即发送首发心跳确认状态
        session.ping(verbose=True)

        t_start = time.monotonic()
        while True:
            time.sleep(1.0)
            now = time.monotonic()
            if max_duration > 0 and (now - t_start) >= max_duration:
                print(f"\n[+] 达到指定保活时长 ({max_duration}s)，正常退出。")
                break
            session.ping_if_due(verbose=True)

    except KeyboardInterrupt:
        print("\n[*] 接收到中断信号，正在终止保活守护...")
    except Exception as e:
        print(f"\n[!] 守护循环异常中断: {e}")
        return 1
    finally:
        if reboot_exit:
            print("[*] 正在向相机发送重启指令...", flush=True)
            session.reboot()
        session.close()
        print("[+] Fastboot 会话已安全关闭。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Sony ZV-E10 Fastboot 会话保活模块与守护工具")
    parser.add_argument("--interval", type=float, default=DEFAULT_PING_INTERVAL,
                        help=f"保活心跳发送周期（秒，默认 {DEFAULT_PING_INTERVAL}）")
    parser.add_argument("--duration", type=float, default=0.0,
                        help="最长维持秒数（默认 0，表示无限期维持直到 Ctrl+C）")
    parser.add_argument("--oneshot", action="store_true",
                        help="仅发送一次 oem darwin 心跳并检查返回后退出")
    parser.add_argument("--probe", action="store_true",
                        help="探测当前 Fastboot 环境变量并打印")
    parser.add_argument("--reboot", action="store_true",
                        help="退出时发送 reboot 指令重启相机")
    args = parser.parse_args()

    if args.oneshot or args.probe:
        sess = FastbootSession(ping_interval=args.interval)
        if not sess.connect(timeout=10.0):
            print("[FATAL] 连接 Fastboot 超时失败")
            return 1
        try:
            if args.probe:
                print(f"[+] product : {sess.get_var('product')}")
                print(f"[+] version : {sess.get_var('version')}")
            ok = sess.ping(verbose=True)
            return 0 if ok else 1
        finally:
            if args.reboot:
                sess.reboot()
            sess.close()

    return run_daemon(interval=args.interval, max_duration=args.duration, reboot_exit=args.reboot)


if __name__ == "__main__":
    sys.exit(main())
