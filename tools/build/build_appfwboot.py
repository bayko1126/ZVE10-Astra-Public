#!/usr/bin/env python3
"""编译五代引导器 appFwBoot.so（宿主 im.elf 版，支持 QEMU/真机/USB沙盒三目标）并校验 ABI

用法：uv run --with ziglang --with pyelftools python tools/build/build_appfwboot.py [--target qemu|real|sandbox|all] [--stack-mb 16]
产物：
  - --target qemu (默认): build_out/appFwBoot.so
  - --target real: build_out/appFwBoot_real.so（真机 /usr/lib 生产版）
  - --target sandbox: build_out/appFwBoot_sandbox.so（真机 USB /log/g5 沙盒版）
  - build_out/libappFwBoot_g5.so（原版五代引导器符号对齐副本）
"""
import argparse
import hashlib
import struct
import subprocess
import sys
from pathlib import Path

from elftools.elf.elffile import ELFFile

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "appFwBoot_g4host.c"
OUT = ROOT / "build_out"
G5_BOOT = ROOT / "firmware" / "gen5" / "lib" / "appFwBoot.so"
TARGET_EFLAGS = 0x05000002
MAX_GLIBC = (2, 4)


def compile_and_verify(out_name: str, stack_mb: int, extra_defs: list[str]) -> Path:
    out = OUT / out_name
    cmd = [
        sys.executable, "-m", "ziglang", "cc", "-target", "arm-linux-gnueabi.2.4",
        "-mcpu=cortex_a9", "-mthumb", "-shared", "-fPIC", "-O2", "-s",
        "-Wall", "-Wextra", "-Wno-unused-function",
        "-fvisibility=hidden", f"-DASTRA_STACK_SIZE=({stack_mb}u*1024u*1024u)",
        *extra_defs,
        "-Wl,-z,lazy", "-Wl,-z,norelro", "-Wl,-z,max-page-size=0x1000",
        "-Wl,-soname,appFwBoot.so", "-o", str(out), str(SRC),
    ]
    print("[*]", " ".join(cmd))
    subprocess.run(cmd, check=True)

    raw = bytearray(out.read_bytes())
    old = struct.unpack_from("<I", raw, 36)[0]
    struct.pack_into("<I", raw, 36, TARGET_EFLAGS)
    out.write_bytes(raw)
    print(f"[+] {out.name}: e_flags 0x{old:08x} -> 0x{TARGET_EFLAGS:08x}")

    with open(out, "rb") as fp:
        e = ELFFile(fp)
        dyn = e.get_section_by_name(".dynsym")
        exports = sorted(
            s.name for s in dyn.iter_symbols()
            if s.name and s["st_shndx"] != "SHN_UNDEF" and s["st_info"]["bind"] == "STB_GLOBAL"
        )
        undef = sorted(s.name for s in dyn.iter_symbols() if s.name and s["st_shndx"] == "SHN_UNDEF")
        vers = set()
        vr = e.get_section_by_name(".gnu.version_r")
        if vr:
            for _, auxs in vr.iter_versions():
                for x in auxs:
                    vers.add(x.name)
    bad = [v for v in vers if v.startswith("GLIBC_") and tuple(map(int, v[6:].split(".")[:2])) > MAX_GLIBC]
    print(f"[+] {out.name} 导出:", exports)
    print(f"[+] {out.name} 外部依赖:", undef)
    print(f"[+] {out.name} 版本需求:", sorted(vers))
    expected_exports = {"Application_System_Init"}
    assert set(exports) == expected_exports, f"导出表异常: {exports}"
    assert not bad, f"存在高于 glibc 2.11 的版本需求: {bad}"
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", choices=["qemu", "real", "sandbox", "all"], default="qemu")
    ap.add_argument("--stack-mb", type=int, default=16)
    a = ap.parse_args()
    OUT.mkdir(exist_ok=True)

    outputs: list[Path] = []
    if a.target in ("qemu", "all"):
        outputs.append(compile_and_verify("appFwBoot.so", a.stack_mb, []))
    if a.target in ("real", "all"):
        outputs.append(compile_and_verify("appFwBoot_real.so", a.stack_mb, ["-DZVE10_REAL_DEVICE"]))
    if a.target in ("sandbox", "all"):
        outputs.append(compile_and_verify("appFwBoot_sandbox.so", a.stack_mb, ["-DZVE10_REAL_DEVICE", "-DZVE10_USB_SANDBOX"]))

    # 将五代引导器中的冲突符号等长同hash改名后输出为 libappFwBoot_g5.so
    if not G5_BOOT.is_file():
        print(f"[!] 跳过 libappFwBoot_g5.so 生成：未找到 {G5_BOOT}（请先运行 tools/build/prepare_inputs.py）")
    else:
        map_file = ROOT / "manifests" / "rename_map_g5boot.json"
        rename_py = ROOT / "tools" / "build" / "elf_samehash_rename.py"
        target_g5_boot = OUT / "libappFwBoot_g5.so"
        rename_cmd = [sys.executable, str(rename_py), str(G5_BOOT), str(target_g5_boot), "--map", str(map_file)]
        print("[*] 重命名五代引导器符号:", " ".join(rename_cmd))
        subprocess.run(rename_cmd, check=True)
        outputs.append(target_g5_boot)
    for p in outputs:
        print(f"[+] {p.name:22s} {p.stat().st_size:8d} B  sha256={hashlib.sha256(p.read_bytes()).hexdigest()}")


if __name__ == "__main__":
    main()
