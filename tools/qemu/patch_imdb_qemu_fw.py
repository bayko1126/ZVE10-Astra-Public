import argparse
import struct
from io import BytesIO
from pathlib import Path
from elftools.elf.elffile import ELFFile

ROOT = Path(__file__).resolve().parents[2]


def patch_imdb(src_path: Path | None = None, out_path: Path | None = None):
    src_path = src_path or (ROOT / "build_out" / "libIMDB_g5_fw.so")
    out_path = out_path or (ROOT / "build_out" / "libIMDB_qemu_astra.so")
    raw = bytearray(src_path.read_bytes())
    elf = ELFFile(BytesIO(raw))
    assert elf.elfclass == 32 and elf['e_machine'] == 'EM_ARM'

    def file_offset(address):
        for segment in elf.iter_segments():
            if segment['p_type'] == 'PT_LOAD' and segment['p_vaddr'] <= address < segment['p_vaddr'] + segment['p_filesz']:
                return segment['p_offset'] + address - segment['p_vaddr']
        raise ValueError(f'Unmapped address {address:#x}')

    def string(address):
        start = file_offset(address)
        return raw[start:raw.index(b'\0', start)].decode('latin1')

    symbols = {s.name: s for s in elf.get_section_by_name('.dynsym').iter_symbols()}
    base = symbols['imdb_raw']['st_value']
    print(f"[*] imdb_raw base: 0x{base:x}")

    exclude = {
        'libUsbBusPower.so', 'libInfraUsbCmn.so', 'libsensapi.so', 'libUsbControl.so',
        'libBusPower.so', 'libHotplug.so', 'libInfraCrypt.so', 'libInfraMediaPwrMgr.so',
        'libNetServiceTethering.so', 'libTetheringResource.so',
        '/usr/kmod/liro.ko',
    }

    changed = []
    found_main_app = False
    for index in range(300):
        offset = file_offset(base + 0x84 + index * 0x2c)
        kind, phase, targets, identity, name_pointer = struct.unpack_from('<5I', raw, offset)
        if kind == 40:
            break
        name = string(name_pointer)
        if name in exclude:
            if targets & 2:
                struct.pack_into('<I', raw, offset + 8, targets & ~2)
                changed.append(name)
                print(f"[-] QEMU 禁用未模拟模块: {name} (mode {hex(targets)} -> {hex(targets & ~2)})")
        if name == 'appFwBoot.so' and kind == 0x20000004:
            found_main_app = True
            print(f"[+] 发现 appFwBoot.so 主启动条目 @ index {index}: kind={hex(kind)}, phase={phase}, targets={hex(targets)}")
            assert targets & 2, "appFwBoot.so 必须在 QEMU 下启用 (targets & 2)"

    assert found_main_app, "未能在 libIMDB 中找到 appFwBoot.so 主启动项!"
    print(f"[*] 成功修改了 {len(changed)} 个 QEMU 排除项")
    
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(bytes(raw))
    print(f"[+] 已写入: {out_path} ({len(raw)} 字节)")

if __name__ == '__main__':
    ap = argparse.ArgumentParser(description="生成 QEMU 专用的 libIMDB_qemu_astra.so（禁用未模拟硬件模块）")
    ap.add_argument("--src", type=Path, default=None, help="输入 libIMDB_g5_fw.so (默认 build_out/libIMDB_g5_fw.so)")
    ap.add_argument("--out", type=Path, default=None, help="输出 libIMDB_qemu_astra.so (默认 build_out/libIMDB_qemu_astra.so)")
    args = ap.parse_args()
    patch_imdb(args.src, args.out)
