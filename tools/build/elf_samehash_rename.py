#!/usr/bin/env python3
"""ELF .dynstr 等长且保持 GNU hash 不变的符号改名工具（总指挥编写）

背景：
  直接把 .dynstr 中的符号名改掉会使该符号的 GNU hash 与 .gnu.hash 表中记录的值不一致，
  导致 *本库自身* 对该符号的重定位（如 R_ARM_GLOB_DAT）在 dlopen 时查找失败 → undefined symbol。
  （实证：四代 libObj.so 在 .rel.dyn 0x13de678 处有一条 R_ARM_GLOB_DAT 引用 Application_System_Init。）

原理：
  GNU hash: h = h*33 + c (mod 2^32)。把相邻两字符 (c_i, c_{i+1}) 改成 (c_i + d, c_{i+1} - 33*d)，
  两处增量 d*33^k 与 -33d*33^(k-1) 精确抵消，hash 不变 → .gnu.hash/bloom/bucket/chain 全部无需重建，
  而旧名字查找因 strcmp 失败而查不到。

用法：
  uv run --with pyelftools python elf_samehash_rename.py IN.so OUT.so --sym Application_System_Init [--sym ...]
  uv run --with pyelftools python elf_samehash_rename.py IN.so OUT.so --map rename_map.json
"""
import argparse
import hashlib
import json
import struct
import sys
from pathlib import Path

from elftools.elf.elffile import ELFFile
from elftools.elf.dynamic import DynamicSection

ALLOWED = set(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_")


def gnu_hash(name: bytes) -> int:
    h = 5381
    for c in name:
        h = (h * 33 + c) & 0xFFFFFFFF
    return h


def candidates(name: bytes):
    """按“越靠后越好”的顺序生成等长同 hash 候选名（保留 _Z 前缀与开头若干字节可读性）。"""
    n = len(name)
    start = 4 if name.startswith(b"_Z") else 1
    for i in range(n - 2, start - 1, -1):
        for d in (1, -1, 2, -2, 3, -3):
            a, b = name[i] + d, name[i + 1] - 33 * d
            if a in ALLOWED and b in ALLOWED:
                new = bytearray(name)
                new[i], new[i + 1] = a, b
                yield bytes(new)


def pick_new_name(name: bytes, forbidden: set) -> bytes:
    for cand in candidates(name):
        if cand not in forbidden:
            assert gnu_hash(cand) == gnu_hash(name) and len(cand) == len(name)
            return cand
    raise RuntimeError(f"找不到可用的同 hash 改名: {name!r}")


def dynstr_refs(elf):
    """收集所有指向 .dynstr 的引用偏移（dynsym st_name、DT_NEEDED/SONAME/RPATH、verneed/verdef 名称）。"""
    refs = []
    dynsym = elf.get_section_by_name(".dynsym")
    for i, s in enumerate(dynsym.iter_symbols()):
        refs.append(("sym", i, s["st_name"]))
    for sec in elf.iter_sections():
        if isinstance(sec, DynamicSection):
            for t in sec.iter_tags():
                if t.entry.d_tag in ("DT_NEEDED", "DT_SONAME", "DT_RPATH", "DT_RUNPATH"):
                    refs.append((t.entry.d_tag, 0, t.entry.d_val))
    for secname in (".gnu.version_r", ".gnu.version_d"):
        sec = elf.get_section_by_name(secname)
        if sec is None:
            continue
        if secname == ".gnu.version_r":
            for verneed, auxs in sec.iter_versions():
                refs.append(("verneed", 0, verneed["vn_file"]))
                for aux in auxs:
                    refs.append(("vernaux", 0, aux["vna_name"]))
        else:
            for verdef, auxs in sec.iter_versions():
                for aux in auxs:
                    refs.append(("verdaux", 0, aux["vda_name"]))
    return refs


def gnu_lookup(elf, raw: bytes, name: bytes):
    """在 .gnu.hash 中模拟 glibc 查找，返回 dynsym 索引或 None。"""
    sec = elf.get_section_by_name(".gnu.hash")
    dynsym = elf.get_section_by_name(".dynsym")
    dynstr = elf.get_section_by_name(".dynstr")
    off = sec["sh_offset"]
    nbuckets, symoffset, bloom_size, shift = struct.unpack_from("<4I", raw, off)
    word = 4 if elf.elfclass == 32 else 8
    bloom_off = off + 16
    buckets_off = bloom_off + bloom_size * word
    chain_off = buckets_off + nbuckets * 4
    h = gnu_hash(name)
    bits = word * 8
    bw = int.from_bytes(raw[bloom_off + ((h // bits) % bloom_size) * word:][:word], "little")
    if not ((bw >> (h % bits)) & 1 and (bw >> ((h >> shift) % bits)) & 1):
        return None
    idx = struct.unpack_from("<I", raw, buckets_off + (h % nbuckets) * 4)[0]
    if idx == 0:
        return None
    ent = dynsym["sh_entsize"]
    while True:
        ch = struct.unpack_from("<I", raw, chain_off + (idx - symoffset) * 4)[0]
        if (ch | 1) == (h | 1):
            st_name = struct.unpack_from("<I", raw, dynsym["sh_offset"] + idx * ent)[0]
            s0 = dynstr["sh_offset"] + st_name
            if raw[s0:raw.index(b"\x00", s0)] == name:
                return idx
        if ch & 1:
            return None
        idx += 1


def rename(in_path: Path, out_path: Path, names: list, forbidden_extra: set = frozenset(), mapping: dict = None):
    raw = bytearray(in_path.read_bytes())
    elf = ELFFile(open(in_path, "rb"))
    dynstr = elf.get_section_by_name(".dynstr")
    dynsym = elf.get_section_by_name(".dynsym")
    ds_off, ds_size = dynstr["sh_offset"], dynstr["sh_size"]
    existing = {s.name.encode() for s in dynsym.iter_symbols() if s.name}
    forbidden = existing | set(forbidden_extra)
    refs = dynstr_refs(elf)
    result = {}
    for nm in names:
        name = nm.encode() if isinstance(nm, str) else nm
        hits = [(k, i, o) for (k, i, o) in refs if k == "sym" and raw[ds_off + o: raw.index(b"\x00", ds_off + o)] == name]
        if not hits:
            raise SystemExit(f"[!] {in_path.name}: dynsym 中找不到 {name!r}")
        str_offs = {o for (_, _, o) in hits}
        if len(str_offs) != 1:
            raise SystemExit(f"[!] {name!r} 对应多个 dynstr 偏移 {str_offs}，需人工处理")
        o = str_offs.pop()
        new = (mapping or {}).get(name.decode())
        new = new.encode() if new else pick_new_name(name, forbidden)
        assert len(new) == len(name) and gnu_hash(new) == gnu_hash(name), "映射名必须等长且同 GNU hash"
        changed = [j for j in range(len(name)) if name[j] != new[j]]
        lo, hi = o + changed[0], o + changed[-1]
        # 后缀共享检查：任何其它引用若指向 [o+1, hi] 区间内，会被连带改坏
        for (k, i, ro) in refs:
            if ro != o and o < ro <= hi:
                raise SystemExit(f"[!] 后缀共享冲突: {name!r} 被 {k}#{i} (dynstr+0x{ro:x}) 共享，放弃")
        raw[ds_off + o: ds_off + o + len(new)] = new
        forbidden.add(new)
        result[name.decode()] = {"new": new.decode(), "dynstr_off": hex(o), "sym_indices": [i for (_, i, _) in hits],
                                 "gnu_hash": hex(gnu_hash(name))}
    out_path.write_bytes(bytes(raw))
    # 校验：新名可查到（若是导出符号）、旧名查不到、文件大小不变
    elf2 = ELFFile(open(out_path, "rb"))
    ds2 = elf2.get_section_by_name(".dynsym")
    for old, info in result.items():
        is_defined = any(ds2.get_symbol(idx)["st_shndx"] != "SHN_UNDEF" for idx in info["sym_indices"])
        if is_defined:
            assert gnu_lookup(elf2, bytes(raw), info["new"].encode()) is not None, f"新名查找失败 {info['new']}"
            assert gnu_lookup(elf2, bytes(raw), old.encode()) is None, f"旧名仍可查到 {old}"
    assert len(raw) == in_path.stat().st_size
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inp", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--sym", action="append", default=[])
    ap.add_argument("--map", type=Path, help="JSON {old: new} 指定映射（须等长同 hash）或 [old,...] 列表")
    ap.add_argument("--report", type=Path)
    a = ap.parse_args()
    mapping = None
    names = list(a.sym)
    if a.map:
        m = json.loads(a.map.read_text(encoding="utf-8"))
        if isinstance(m, dict):
            mapping = m; names += list(m)
        else:
            names += m
    res = rename(a.inp, a.out, names, mapping=mapping)
    for k, v in res.items():
        print(f"[+] {k} -> {v['new']}  (dynstr {v['dynstr_off']}, hash {v['gnu_hash']})")
    print(f"[+] 输出 {a.out} SHA256={hashlib.sha256(a.out.read_bytes()).hexdigest()}")
    if a.report:
        a.report.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
