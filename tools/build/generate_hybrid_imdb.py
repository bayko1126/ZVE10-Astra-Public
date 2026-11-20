#!/usr/bin/env python3
"""生成基于 ZV-E10 原厂的五代 ASTRA UI 调度库 libIMDB_g5.so（支持 37,044 B 真机内置版与 37,204 B 固件解包版）"""
import argparse
import hashlib
import struct
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASE_LIB = ROOT / "firmware" / "gen4" / "lib"
OUT_DIR = ROOT / "build_out"

VARIANTS = {
    "dump": {
        "src": BASE_LIB / "libIMDB_dump_37044.so",
        "expected_size": 37044,
        "str_off": 0x5530,
        "testcmd_off": 0x7FBC,
        "appfw_off": 0x7CA4,
        "appfw_orig_file": 0x51D9,
        "disable_entries": [
            ("sata_drv", 0x6754, b"/kmod/sata_drv.ko"),
            ("EthernetService_Init", 0x76CC, b"libInfraEthernetService.so"),
            ("gps_rsrc_init", 0x782C, b"libgps_rsrc.so"),
            ("gps_init", 0x7858, b"libgps.so"),
            ("fan_infra_init", 0x7A10, b"libfan.so"),
            ("vssapp_init", 0x7CFC, b"/usr/local/lib/libVssApp.so"),
        ],
    },
    "fw": {
        "src": BASE_LIB / "libIMDB_fw_37204.so",
        "expected_size": 37204,
        "str_off": 0x55A3,
        "testcmd_off": 0x805C,
        "appfw_off": 0x7D44,
        "appfw_orig_file": 0x524C,
        "disable_entries": [
            ("sata_drv", 0x67C8, b"/kmod/sata_drv.ko"),
            ("EthernetService_Init", 0x776C, b"libInfraEthernetService.so"),
            ("gps_rsrc_init", 0x78CC, b"libgps_rsrc.so"),
            ("gps_init", 0x78F8, b"libgps.so"),
            ("fan_infra_init", 0x7AB0, b"libfan.so"),
            ("vssapp_init", 0x7D9C, b"/usr/local/lib/libVssApp.so"),
        ],
    },
}


def cstr(buf: bytes, off: int) -> bytes:
    return buf[off:].split(b"\x00", 1)[0]


def build_hybrid_imdb(variant: str = "dump", out_path: Path = OUT_DIR / "libIMDB_g5.so") -> bytes:
    cfg = VARIANTS[variant]
    src_path: Path = cfg["src"]
    raw = bytearray(src_path.read_bytes())
    assert len(raw) == cfg["expected_size"], f"文件大小不匹配: {len(raw)} != {cfg['expected_size']}"
    print(f"[*] 基准版本: {variant} ({src_path.name}, {len(raw):,} B, SHA256: {hashlib.sha256(raw).hexdigest()})")

    # 1. 借用 libtestcmd_scn.so 字符串槽位写入 "appFwBoot.so\0"，并同步将 testcmd_scn_init 的 mode (+0x08) 清零
    str_off = cfg["str_off"]
    old_str = cstr(raw, str_off)
    assert old_str == b"libtestcmd_scn.so", f"字符串槽位校验失败 @ 0x{str_off:x}: {old_str!r}"
    new_str = b"appFwBoot.so\x00"
    raw[str_off:str_off + len(new_str)] = new_str

    tc_off = cfg["testcmd_off"]
    tc_tag, tc_ver, tc_mode, tc_me_psid, tc_f, tc_i = struct.unpack_from("<6I", raw, tc_off)
    assert tc_f == str_off, f"testcmd_scn_init file 指针异常: 0x{tc_f:x} != 0x{str_off:x}"
    struct.pack_into("<I", raw, tc_off + 8, 0x00000000)
    print(f"[1] 0x{str_off:04x} 覆写 {old_str.decode()} -> appFwBoot.so，并将 testcmd_scn_init (0x{tc_off:04x}) mode 清零 (0x{tc_mode:08x} -> 0x00000000)")

    # 2. 修改 Application_System_Init 条目: me_psid -> 0x00000083, file -> str_off
    ap_off = cfg["appfw_off"]
    tag, ver, mode, me_psid, f_ptr, i_ptr = struct.unpack_from("<6I", raw, ap_off)
    assert tag == 0x20000004, f"Application_System_Init tag 异常: 0x{tag:08x}"
    assert me_psid == 0x07FB0083, f"Application_System_Init me_psid 异常: 0x{me_psid:08x}"
    assert f_ptr == cfg["appfw_orig_file"], f"Application_System_Init file 指针异常: 0x{f_ptr:x}"
    struct.pack_into("<I", raw, ap_off + 12, 0x00000083)
    struct.pack_into("<I", raw, ap_off + 16, str_off)
    print(f"[2] Application_System_Init (0x{ap_off:04x}): me_psid 0x{me_psid:08x} -> 0x00000083, file 0x{f_ptr:04x} -> 0x{str_off:04x} (appFwBoot.so)")

    # 3. 禁用 6 个无用外设/服务模块（+0x08 mode 设为 0x00000000，绝不乱改 psid）
    for mod_name, off, exp_file in cfg["disable_entries"]:
        m_tag, m_ver, m_mode, m_me_psid, m_f, m_i = struct.unpack_from("<6I", raw, off)
        actual_file = cstr(raw, m_f)
        assert actual_file == exp_file, f"{mod_name} @ 0x{off:04x} 文件名不匹配: {actual_file!r} != {exp_file!r}"
        struct.pack_into("<I", raw, off + 8, 0x00000000)
        print(f"[3] 禁用模块 {mod_name:22s} @ 0x{off:04x} ({actual_file.decode()}): mode 0x{m_mode:08x} -> 0x00000000")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(bytes(raw))
    print(f"[+] 生成产物: {out_path} ({len(raw):,} B, SHA256: {hashlib.sha256(raw).hexdigest()})")
    return bytes(raw)


def main():
    parser = argparse.ArgumentParser(description="生成 ZV-E10 五代 ASTRA 混合调度表 libIMDB_g5.so")
    parser.add_argument("--variant", choices=["dump", "fw"], default="dump",
                        help="选择基底版本: dump (37,044 B nflasha15.bin 内置版，默认) 或 fw (37,204 B 固件解包版)")
    parser.add_argument("--out", type=Path, default=None, help="输出文件路径 (默认 build_out/libIMDB_g5.so 或 libIMDB_g5_fw.so)")
    args = parser.parse_args()
    default_name = "libIMDB_g5.so" if args.variant == "dump" else "libIMDB_g5_fw.so"
    out_p = args.out if args.out is not None else (OUT_DIR / default_name)
    build_hybrid_imdb(args.variant, out_p)


if __name__ == "__main__":
    main()
