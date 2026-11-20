#!/usr/bin/env python3
"""
build_patch_writer.py - 编译 camtool 核心载荷 patch_writer (纯汇编静态系统调用工具)
目标平台: ARMv7-A Linux (支持任何旧版与新版 Linux 内核，零 libc 依赖，零动态依赖)
"""

import os
import sys
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
PAYLOAD_DIR = os.path.join(HERE, "payload")
SRC_S = os.path.join(PAYLOAD_DIR, "patch_writer.s")
OUT_BIN = os.path.join(PAYLOAD_DIR, "patch_writer")

def build():
    if not os.path.exists(SRC_S):
        print(f"[!] 找不到源文件: {SRC_S}")
        return False
    print(f"[*] 正在编译静态系统调用载荷: {SRC_S} -> {OUT_BIN}...")
    cmd = [
        "uv", "run", "--with", "ziglang", "python", "-m", "ziglang", "cc",
        "-target", "arm-linux-musleabi",
        "-nostdlib",
        "-O2",
        "-Wl,--entry=_start",
        SRC_S,
        "-o", OUT_BIN
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        print(f"[!] 编译失败:\n{res.stderr}")
        return False
    sz = os.path.getsize(OUT_BIN)
    print(f"[+] 编译成功！输出大小: {sz} 字节 (纯静态 ELF，零外部依赖)")
    return True

if __name__ == "__main__":
    sys.exit(0 if build() else 1)
