#!/usr/bin/env python3
"""MWF 资源管理器影子继承补丁（解决跨代共存时的 overlap generation 冲突）

原理：
  在四代 libMWF.so 中，MWF::RsrcMgr::InitRsrcMgr 在检测到同一 mgrId 已被注册时，
  若原实例非 shadow 则硬编码触发 overlap generation 断言并 SIGSEGV。
  本补丁将 0x2d2ac 处的 `cbz r0, #0x2d2d8` (0xb1a0) 替换为 `nop` (0xbf00)，
  使第二代组件 (五代 ASTRA UI) 能够自动继承四代宿主已建立的资源关系并创建影子副本，
  实现四代宿主硬件资源与五代新界面的无缝复用与桥接。
"""
import hashlib
from pathlib import Path
from elftools.elf.elffile import ELFFile

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "firmware" / "gen4" / "lib" / "libMWF.so"
OUT = ROOT / "build_out" / "libMWF.so"
OUT.parent.mkdir(parents=True, exist_ok=True)

def patch_mwf():
    raw = bytearray(SRC.read_bytes())
    print(f"[*] 源文件: {SRC} ({len(raw)} 字节, sha256={hashlib.sha256(raw).hexdigest()})")
    
    with open(SRC, 'rb') as f:
        elf = ELFFile(f)
        text = elf.get_section_by_name('.text')
        text_addr = text['sh_addr']
        text_off = text['sh_offset']
        
        target_vaddr = 0x2d2ac
        file_off = text_off + (target_vaddr - text_addr)
        
        cur = raw[file_off:file_off+2]
        assert cur == b"\xa0\xb1", f"指令校验失败: 期望 0xb1a0 (cbz r0), 实际 {cur.hex()}"
        
        # 替换为 nop (0xbf00)
        raw[file_off:file_off+2] = b"\x00\xbf"
        print(f"[+] 成功将 0x{target_vaddr:x} (文件偏移 0x{file_off:x}) 处的 cbz 指令替换为 nop")

    OUT.write_bytes(bytes(raw))
    print(f"[+] 输出补丁库: {OUT} ({len(raw)} 字节, sha256={hashlib.sha256(raw).hexdigest()})")

if __name__ == '__main__':
    patch_mwf()
