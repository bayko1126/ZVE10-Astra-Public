#!/usr/bin/env python3
"""修正版 ext2 读取器：完整支持一/二/三级间接块。

此前 verify_e0_*.py 中的实现仅支持直接块 + 一级间接，
对 >274,432 B（12 + 256 块）的文件会静默截断，
导致 libObj.so (21 MB) / appFw.so (64 MB) 的字符串与符号分析全部无效。
"""
from __future__ import annotations

import hashlib
import struct
from pathlib import Path

BS_DEFAULT = 1024


class Ext2Reader:
    def __init__(self, path: Path):
        self.f = open(path, "rb")
        self.f.seek(1024)
        sb = self.f.read(1024)
        self.sb = sb
        self.first_data_block = struct.unpack_from("<I", sb, 20)[0]
        self.block_size = 1024 << struct.unpack_from("<I", sb, 24)[0]
        self.blocks_count = struct.unpack_from("<I", sb, 4)[0]
        self.inodes_count = struct.unpack_from("<I", sb, 0)[0]
        self.blocks_per_group = struct.unpack_from("<I", sb, 32)[0]
        self.inodes_per_group = struct.unpack_from("<I", sb, 40)[0]
        self.free_blocks = struct.unpack_from("<I", sb, 12)[0]
        self.free_inodes = struct.unpack_from("<I", sb, 16)[0]
        rev = struct.unpack_from("<I", sb, 76)[0]
        self.inode_size = struct.unpack_from("<H", sb, 88)[0] if rev >= 1 else 128
        self.gdt_block = self.first_data_block + 1
        self._ptr_per_block = self.block_size // 4

    # ---------- 底层 ----------
    def block(self, n: int) -> bytes:
        if n == 0:
            return b"\x00" * self.block_size
        self.f.seek(n * self.block_size)
        return self.f.read(self.block_size)

    def gdt(self, gi: int) -> tuple[int, int, int]:
        self.f.seek(self.gdt_block * self.block_size + gi * 32)
        d = self.f.read(32)
        return struct.unpack_from("<III", d, 0)

    def inode(self, ino: int) -> dict:
        gi = (ino - 1) // self.inodes_per_group
        li = (ino - 1) % self.inodes_per_group
        _, _, itab = self.gdt(gi)
        self.f.seek(itab * self.block_size + li * self.inode_size)
        d = self.f.read(self.inode_size)
        return {
            "mode": struct.unpack_from("<H", d, 0)[0],
            "size": struct.unpack_from("<I", d, 4)[0],
            "blocks": struct.unpack_from("<15I", d, 40),
            "flags": struct.unpack_from("<I", d, 32)[0],
        }

    def _ptrs(self, blkno: int) -> list[int]:
        data = self.block(blkno)
        n = len(data) // 4
        return list(struct.unpack_from(f"<{n}I", data, 0))

    def _collect_blocks(self, i_block: tuple[int, ...], need: int) -> list[int]:
        """按逻辑顺序收集 need 个块号（0 表示空洞）。"""
        out: list[int] = []
        ppb = self._ptr_per_block

        # 12 个直接块
        for b in i_block[:12]:
            if len(out) >= need:
                return out[:need]
            out.append(b)

        # 一级间接
        if len(out) < need and i_block[12]:
            for b in self._ptrs(i_block[12]):
                if len(out) >= need:
                    return out[:need]
                out.append(b)
        elif len(out) < need:
            out.extend([0] * min(ppb, need - len(out)))

        # 二级间接
        if len(out) < need and i_block[13]:
            for l1 in self._ptrs(i_block[13]):
                if len(out) >= need:
                    return out[:need]
                if l1 == 0:
                    out.extend([0] * ppb)
                    continue
                for b in self._ptrs(l1):
                    if len(out) >= need:
                        return out[:need]
                    out.append(b)
        elif len(out) < need:
            out.extend([0] * min(ppb * ppb, need - len(out)))

        # 三级间接
        if len(out) < need and i_block[14]:
            for l1 in self._ptrs(i_block[14]):
                if len(out) >= need:
                    return out[:need]
                if l1 == 0:
                    out.extend([0] * (ppb * ppb))
                    continue
                for l2 in self._ptrs(l1):
                    if len(out) >= need:
                        return out[:need]
                    if l2 == 0:
                        out.extend([0] * ppb)
                        continue
                    for b in self._ptrs(l2):
                        if len(out) >= need:
                            return out[:need]
                        out.append(b)

        return out[:need]

    def read_file(self, ino: int, verify: bool = True) -> bytes:
        meta = self.inode(ino)
        size = meta["size"]
        need = (size + self.block_size - 1) // self.block_size
        blocks = self._collect_blocks(meta["blocks"], need)
        if verify:
            nonzero = sum(1 for b in blocks if b)
            expected_min = need - 0
            if nonzero < need * 0.5:
                raise RuntimeError(
                    f"inode {ino}: 仅解析到 {nonzero}/{need} 个非空块，疑似间接块解析不完整"
                )
        out = bytearray()
        for b in blocks:
            if len(out) >= size:
                break
            out.extend(self.block(b))
        return bytes(out[:size])

    # ---------- 路径 ----------
    def read_dir(self, ino: int) -> list[tuple[str, int, int]]:
        data = self.read_file(ino, verify=False)
        out = []
        pos = 0
        while pos + 8 <= len(data):
            i, rec_len, nl, ft = struct.unpack_from("<IHBB", data, pos)
            if rec_len == 0:
                break
            name = data[pos + 8 : pos + 8 + nl].decode("latin-1")
            if i != 0 and name not in (".", ".."):
                out.append((name, i, ft))
            pos += rec_len
        return out

    def lookup(self, path: str) -> int | None:
        cur = 2
        for part in [p for p in path.split("/") if p]:
            found = None
            for name, ino, _ in self.read_dir(cur):
                if name == part:
                    found = ino
                    break
            if found is None:
                return None
            cur = found
        return cur

    def close(self):
        self.f.close()


def strings_of(data: bytes, minlen: int = 5) -> list[str]:
    out, cur = [], b""
    for b in data:
        if 32 <= b < 127:
            cur += bytes([b])
        else:
            if len(cur) >= minlen:
                out.append(cur.decode("latin-1"))
            cur = b""
    if len(cur) >= minlen:
        out.append(cur.decode("latin-1"))
    return out


if __name__ == "__main__":
    import sys

    img_arg = Path(sys.argv[1]) if len(sys.argv) > 1 else (Path(__file__).resolve().parents[2] / "firmware" / "backup" / "nflasha15.bin")
    base = Ext2Reader(img_arg)
    print("=" * 74)
    print("原厂 ZV-E10 nflasha15 —— 引擎架构核查（修正版读取器）")
    print("=" * 74)
    print(f"FS: blocks={base.blocks_count} free={base.free_blocks} "
          f"({base.free_blocks*base.block_size/1048576:.1f} MiB)  "
          f"capacity={base.blocks_count*base.block_size/1048576:.1f} MiB")

    targets = [
        ("/lib/libObj.so", "libObj"),
        ("/lib/libmpr.so", "libmpr"),
        ("/lib/viewUnified2.so", "viewUnified2"),
        ("/lib/CautionConfig.so", "CautionConfig"),
    ]
    cache = {}
    print("\n--- 读取与关键词扫描 ---")
    for path, label in targets:
        ino = base.lookup(path)
        if ino is None:
            print(f"  {path}: NOT FOUND")
            continue
        meta = base.inode(ino)
        data = base.read_file(ino)
        cache[label] = data
        ok = "OK" if len(data) == meta["size"] else "TRUNCATED"
        print(f"  {path}: inode={ino} size={meta['size']:,} read={len(data):,} [{ok}]")
        print(f"    sha256={hashlib.sha256(data).hexdigest()}")
        for kw in [b"appFwBoot", b"appFw.so", b"uxengine_init2", b"uxengine",
                   b"viewUnified", b"LayoutMaster", b".uxc", b".uxb", b"libIMDB"]:
            c = data.count(kw)
            if c:
                print(f"      {kw.decode():16s}: {c}")

    # 关键判定
    print("\n" + "=" * 74)
    print("判定：ZV-E10 的 libObj.so 是否会加载 appFwBoot.so？")
    print("=" * 74)
    if "libObj" in cache:
        d = cache["libObj"]
        has_boot = b"appFwBoot" in d
        has_appfw = b"appFw" in d
        print(f"  含 'appFwBoot' 字符串 : {has_boot}")
        print(f"  含 'appFw'     字符串 : {has_appfw}")
        print(f"  含 'uxengine'  字符串 : {b'uxengine' in d}")
        print(f"  含 '.uxc'/'.uxb'      : {b'.uxc' in d or b'.uxb' in d}")
        # 找 appFw 附近的上下文
        if has_appfw:
            idx = 0
            cnt = 0
            while cnt < 8:
                i = d.find(b"appFw", idx)
                if i < 0:
                    break
                seg = d[max(0, i - 30) : i + 60]
                seg = seg.split(b"\x00")
                seg = [s for s in seg if len(s) >= 3]
                print(f"    @0x{i:06x}: {[s.decode('latin-1', 'replace') for s in seg]}")
                idx = i + 1
                cnt += 1
    base.close()
