#!/usr/bin/env python3
"""Fastboot 会话保活机制单元与集成测试套件。"""
import sys
import time
import unittest
from pathlib import Path

HERE = Path(__file__).parent.resolve()
sys.path.insert(0, str(HERE))

from fastboot_keepalive import (
    FastbootKeepaliveManager,
    FastbootSession,
    format_duration,
    DEFAULT_PING_INTERVAL,
)


class TestFastbootKeepalive(unittest.TestCase):
    def test_format_duration(self):
        self.assertEqual(format_duration(0), "00m 00s")
        self.assertEqual(format_duration(45), "00m 45s")
        self.assertEqual(format_duration(125), "02m 05s")
        self.assertEqual(format_duration(3665), "01h 01m 05s")

    def test_keepalive_timing_and_ping(self):
        # 设置较短的间隔进行精确测试
        km = FastbootKeepaliveManager(interval=0.1, command="oem darwin")
        km.reset_session()

        # 刚重置后，should_ping 应为 False
        self.assertFalse(km.should_ping())

        sent_commands = []

        def mock_send(cmd: str):
            sent_commands.append(cmd)
            return "OKAY", b"keepalive ack", []

        # 未到时间，ping_if_due 应返回 False
        res = km.ping_if_due(mock_send, verbose=False)
        self.assertFalse(res)
        self.assertEqual(len(sent_commands), 0)

        # 等待超过 0.1s
        time.sleep(0.12)
        self.assertTrue(km.should_ping())

        # 到期执行保活
        res = km.ping_if_due(mock_send, verbose=False)
        self.assertTrue(res)
        self.assertEqual(len(sent_commands), 1)
        self.assertEqual(sent_commands[0], "oem darwin")
        self.assertEqual(km.ping_count, 1)
        self.assertEqual(km.ping_errors, 0)

        # 执行完后，立即判定应该为 False
        self.assertFalse(km.should_ping())

    def test_keepalive_error_handling(self):
        km = FastbootKeepaliveManager(interval=0.05)
        km.reset_session()

        def mock_fail(cmd: str):
            raise IOError("USB device disconnected")

        time.sleep(0.06)
        res = km.ping_if_due(mock_fail, verbose=False)
        self.assertFalse(res)
        self.assertEqual(km.ping_errors, 1)

    def test_background_worker(self):
        km = FastbootKeepaliveManager(interval=0.08)
        pings = []

        def mock_send(cmd: str):
            pings.append(time.monotonic())
            return "OKAY", b"", []

        km.start_background(mock_send, verbose=False)
        # 等待后台执行至少 2 次
        time.sleep(0.25)
        km.stop_background()

        self.assertGreaterEqual(len(pings), 2)
        # 确认停止后不再增加
        count_after = len(pings)
        time.sleep(0.15)
        self.assertEqual(len(pings), count_after)

    def test_restore_factory_pure_cli_args(self):
        import subprocess
        res = subprocess.run(
            ["uv", "run", "--with", "pyusb", "--with", "libusb-package", "python", str(HERE / "restore_factory_pure.py"), "--help"],
            capture_output=True,
            text=True,
            encoding="utf-8"
        )
        self.assertEqual(res.returncode, 0)
        self.assertIn("--ping", res.stdout)
        self.assertIn("Darwin 0x16", res.stdout)

    def test_fastboot_keepalive_cli_args(self):
        import subprocess
        res = subprocess.run(
            ["uv", "run", "--with", "pyusb", "--with", "libusb-package", "python", str(HERE / "fastboot_keepalive.py"), "--help"],
            capture_output=True,
            text=True,
            encoding="utf-8"
        )
        self.assertEqual(res.returncode, 0)
        self.assertIn("--interval", res.stdout)
        self.assertIn("--oneshot", res.stdout)
        self.assertIn("--probe", res.stdout)
        self.assertIn("--reboot", res.stdout)


if __name__ == "__main__":
    unittest.main()
