import struct
import subprocess
import sys
from pathlib import Path
from elftools.elf.elffile import ELFFile

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "sandbox_live_verify.c"
OUT_DIR = ROOT / "build_out"
OUT_DIR.mkdir(parents=True, exist_ok=True)
OUT = OUT_DIR / "sandbox_live_verify"
TARGET_EFLAGS = 0x05000002

print(f"Target ARM EABI e_flags = 0x{TARGET_EFLAGS:08x}")

cmd = [
    sys.executable, "-m", "ziglang", "cc",
    "-target", "arm-linux-gnueabi.2.11",
    "-mcpu=cortex_a9", "-mthumb", "-O2",
    "-fPIE", "-pie", "-rdynamic",
    "-o", str(OUT), str(SRC)
]
print("[*] Compiling:", " ".join(cmd))
subprocess.run(cmd, check=True)

# Patch e_flags if needed
raw = bytearray(OUT.read_bytes())
old = struct.unpack_from("<I", raw, 36)[0]
struct.pack_into("<I", raw, 36, TARGET_EFLAGS)
OUT.write_bytes(raw)
print(f"[+] e_flags patched: 0x{old:08x} -> 0x{TARGET_EFLAGS:08x}")
print(f"[+] Output ready: {OUT} ({OUT.stat().st_size} bytes)")
