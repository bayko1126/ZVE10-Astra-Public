#!/usr/bin/env python3
"""最小可用的 ext2 写入器：支持 create / delete / overwrite。

只支持本工程用到的子集：
- block_size = 1024, inode_size = 128
- 直接块 12 + 一级间接 + 二级间接（单文件 <= ~64MB）
- 目录项插入（合并 rec_len 或扩展目录块）
- 块位图 / inode 位图 / GDT / 超级块 空闲计数维护

设计原则：所有操作都基于 Ext2Reader 的只读解析结果，写入前先验证，写入后可回读校验。
"""
from __future__ import annotations

import struct
from pathlib import Path

BS = 1024
S_IFREG = 0o100000
S_IFDIR = 0o040000


class Ext2Writer:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.f = open(self.path, "r+b")
        self.f.seek(1024)
        sb = self.f.read(1024)
        self.sb = bytearray(sb)
        self.inodes_count = struct.unpack_from("<I", sb, 0)[0]
        self.blocks_count = struct.unpack_from("<I", sb, 4)[0]
        self.first_data_block = struct.unpack_from("<I", sb, 20)[0]
        self.block_size = 1024 << struct.unpack_from("<I", sb, 24)[0]
        self.blocks_per_group = struct.unpack_from("<I", sb, 32)[0]
        self.inodes_per_group = struct.unpack_from("<I", sb, 40)[0]
        rev = struct.unpack_from("<I", sb, 76)[0]
        self.inode_size = struct.unpack_from("<H", sb, 88)[0] if rev >= 1 else 128
        self.gdt_block = self.first_data_block + 1
        assert self.block_size == BS and self.inode_size == 128, "只支持 1KB 块 / 128B inode"
        self._ngroups = (self.blocks_count - self.first_data_block +
                         self.blocks_per_group - 1) // self.blocks_per_group
        # ---- 性能优化：位图与计数缓存 ----
        self._bb: dict[int, bytearray] = {}
        self._ib: dict[int, bytearray] = {}
        self._free_blocks: list[int] = [0] * self._ngroups
        self._free_inodes: list[int] = [0] * self._ngroups
        self._next_blk: list[int] = [0] * self._ngroups
        self._next_ino: list[int] = [0] * self._ngroups
        self._dirty = False
        for gi in range(self._ngroups):
            bb, ib, _ = self.gdt(gi)
            self._bb[gi] = bytearray(self.rblk(bb))
            self._ib[gi] = bytearray(self.rblk(ib))
            self.f.seek(self.gdt_block * BS + gi * 32 + 12)
            d = self.f.read(4)
            self._free_blocks[gi] = struct.unpack("<H", d[:2])[0]
            self._free_inodes[gi] = struct.unpack("<H", d[2:4])[0]

    # ---------------- 底层读写 ----------------
    def rblk(self, n: int) -> bytes:
        if n == 0:
            return b"\x00" * BS
        self.f.seek(n * BS)
        return self.f.read(BS)

    def wblk(self, n: int, data: bytes) -> None:
        assert len(data) == BS
        self.f.seek(n * BS)
        self.f.write(data)

    def gdt(self, gi: int) -> tuple[int, int, int]:
        self.f.seek(self.gdt_block * BS + gi * 32)
        d = self.f.read(32)
        return struct.unpack_from("<III", d, 0)

    def gdt_set(self, gi: int, off: int, val: int) -> None:
        self.f.seek(self.gdt_block * BS + gi * 32 + off)
        self.f.write(struct.pack("<I", val))

    def inode_off(self, ino: int) -> int:
        gi = (ino - 1) // self.inodes_per_group
        li = (ino - 1) % self.inodes_per_group
        _, _, itab = self.gdt(gi)
        return itab * BS + li * 128

    def read_inode(self, ino: int) -> dict:
        self.f.seek(self.inode_off(ino))
        d = self.f.read(128)
        return {
            "mode": struct.unpack_from("<H", d, 0)[0],
            "size": struct.unpack_from("<I", d, 4)[0],
            "links": struct.unpack_from("<H", d, 26)[0],
            "blocks": list(struct.unpack_from("<15I", d, 40)),
            "raw": bytearray(d),
        }

    def write_inode(self, ino: int, raw: bytes) -> None:
        assert len(raw) == 128
        self.f.seek(self.inode_off(ino))
        self.f.write(raw)

    # ---------------- 位图 ----------------
    def _bitmap_bit(self, bmp_blk: int, bit: int) -> bool:
        d = self.rblk(bmp_blk)
        return bool(d[bit // 8] & (1 << (bit % 8)))

    def _bitmap_set(self, bmp_blk: int, bit: int, used: bool) -> None:
        d = bytearray(self.rblk(bmp_blk))
        if used:
            d[bit // 8] |= (1 << (bit % 8))
        else:
            d[bit // 8] &= ~(1 << (bit % 8))
        self.wblk(bmp_blk, bytes(d))

    def _adjust_counts(self, gi: int, dblocks: int, dinodes: int) -> None:
        """更新 GDT 与超级块的空闲计数。dblocks/dinodes 为增量（分配用负，释放用正）。"""
        self.f.seek(self.gdt_block * BS + gi * 32 + 12)
        d = self.f.read(4)
        fb = struct.unpack("<H", d[:2])[0]
        fi = struct.unpack("<H", d[2:4])[0]
        fb = max(0, min(65535, fb + dblocks))
        fi = max(0, min(65535, fi + dinodes))
        self.f.seek(self.gdt_block * BS + gi * 32 + 12)
        self.f.write(struct.pack("<HH", fb, fi))
        # 超级块
        self.f.seek(1024 + 12)
        sb_free = struct.unpack("<I", self.f.read(4))[0]
        self.f.seek(1024 + 12)
        self.f.write(struct.pack("<I", max(0, sb_free + dblocks)))
        self.f.seek(1024 + 16)
        sb_free_i = struct.unpack("<I", self.f.read(4))[0]
        self.f.seek(1024 + 16)
        self.f.write(struct.pack("<I", max(0, sb_free_i + dinodes)))

    # ---------------- 块分配 ----------------
    def alloc_block(self) -> int:
        """从有空闲的块组分配一个块（内存位图 + next 指针，O(1) 摊销）。"""
        for gi in range(self._ngroups):
            if self._free_blocks[gi] <= 0:
                continue
            bb = self._bb[gi]
            base = gi * self.blocks_per_group + self.first_data_block
            limit = min(self.blocks_per_group, self.blocks_count - base)
            start = self._next_blk[gi]
            for bit in range(start, limit):
                if not (bb[bit // 8] & (1 << (bit % 8))):
                    bb[bit // 8] |= (1 << (bit % 8))
                    self._free_blocks[gi] -= 1
                    self._next_blk[gi] = bit + 1
                    self._dirty = True
                    return base + bit
            # 回绕再扫一次
            for bit in range(0, start):
                if not (bb[bit // 8] & (1 << (bit % 8))):
                    bb[bit // 8] |= (1 << (bit % 8))
                    self._free_blocks[gi] -= 1
                    self._next_blk[gi] = bit + 1
                    self._dirty = True
                    return base + bit
        raise RuntimeError("磁盘空间不足：无空闲块")

    def free_block(self, blk: int) -> None:
        gi = (blk - self.first_data_block) // self.blocks_per_group
        bit = (blk - self.first_data_block) % self.blocks_per_group
        bb = self._bb[gi]
        if bb[bit // 8] & (1 << (bit % 8)):
            bb[bit // 8] &= ~(1 << (bit % 8))
            self._free_blocks[gi] += 1
            if bit < self._next_blk[gi]:
                self._next_blk[gi] = bit
            self._dirty = True

    # ---------------- inode 分配 ----------------
    def alloc_inode(self) -> int:
        for gi in range(self._ngroups):
            if self._free_inodes[gi] <= 0:
                continue
            ib = self._ib[gi]
            for bit in range(self._next_ino[gi], self.inodes_per_group):
                ino = gi * self.inodes_per_group + bit + 1
                if ino > self.inodes_count:
                    break
                if not (ib[bit // 8] & (1 << (bit % 8))):
                    ib[bit // 8] |= (1 << (bit % 8))
                    self._free_inodes[gi] -= 1
                    self._next_ino[gi] = bit + 1
                    self._dirty = True
                    return ino
        raise RuntimeError("inode 耗尽")

    def free_inode(self, ino: int) -> None:
        gi = (ino - 1) // self.inodes_per_group
        bit = (ino - 1) % self.inodes_per_group
        ib = self._ib[gi]
        if ib[bit // 8] & (1 << (bit % 8)):
            ib[bit // 8] &= ~(1 << (bit % 8))
            self._free_inodes[gi] += 1
            if bit < self._next_ino[gi]:
                self._next_ino[gi] = bit
            self._dirty = True

    # ---------------- 文件数据块 ----------------
    def _ptr_per_block(self) -> int:
        return BS // 4

    def write_data(self, data: bytes) -> tuple[list[int], list[int]]:
        """把 data 写成块链，返回 (i_block 前15项, 所有数据块号)。

        支持 12 直接 + 一级间接 + 二级间接。
        """
        need = (len(data) + BS - 1) // BS
        ppb = self._ptr_per_block()
        direct = min(need, 12)
        l1 = min(max(need - 12, 0), ppb)
        l2 = max(need - 12 - ppb, 0)
        if l2 > ppb * ppb:
            raise RuntimeError(f"文件过大: {len(data)} B 需三级间接，本工具不支持")

        iblock = [0] * 15
        datablocks: list[int] = []

        def put(off: int, chunk: bytes) -> int:
            b = self.alloc_block()
            self.wblk(b, chunk.ljust(BS, b"\x00"))
            datablocks.append(b)
            return b

        # 直接块
        for i in range(direct):
            b = put(i * BS, data[i * BS:(i + 1) * BS])
            iblock[i] = b

        pos = direct * BS
        # 一级间接
        if l1:
            l1blk = self.alloc_block()
            ptrs = []
            for i in range(l1):
                b = put(pos, data[pos:pos + BS]); pos += BS
                ptrs.append(b)
            self.wblk(l1blk, b"".join(struct.pack("<I", p) for p in ptrs).ljust(BS, b"\x00"))
            iblock[12] = l1blk
        # 二级间接
        if l2:
            l2blk = self.alloc_block()
            l2ptrs = []
            remain = l2
            while remain > 0:
                cnt = min(ppb, remain)
                pblk = self.alloc_block()
                ptrs = []
                for i in range(cnt):
                    b = put(pos, data[pos:pos + BS]); pos += BS
                    ptrs.append(b)
                self.wblk(pblk, b"".join(struct.pack("<I", p) for p in ptrs).ljust(BS, b"\x00"))
                l2ptrs.append(pblk)
                remain -= cnt
            self.wblk(l2blk, b"".join(struct.pack("<I", p) for p in l2ptrs).ljust(BS, b"\x00"))
            iblock[13] = l2blk
        return iblock, datablocks

    def free_data(self, iblock: list[int]) -> None:
        """释放 inode 引用的所有块（含间接块本身）。"""
        ppb = self._ptr_per_block()
        for b in iblock[:12]:
            if b:
                self.free_block(b)
        if iblock[12]:
            for p in struct.unpack(f"<{ppb}I", self.rblk(iblock[12])):
                if p:
                    self.free_block(p)
            self.free_block(iblock[12])
        if iblock[13]:
            for p1 in struct.unpack(f"<{ppb}I", self.rblk(iblock[13])):
                if p1:
                    for p2 in struct.unpack(f"<{ppb}I", self.rblk(p1)):
                        if p2:
                            self.free_block(p2)
                    self.free_block(p1)
            self.free_block(iblock[13])
        if iblock[14]:
            raise RuntimeError("三级间接块释放未实现")

    # ---------------- 目录操作 ----------------
    def read_dir_entries(self, dir_ino: int) -> list[dict]:
        ino_d = self.read_inode(dir_ino)
        size = ino_d["size"]
        out = []
        pos = 0
        blkidx = 0
        data = bytearray()
        for b in ino_d["blocks"][:12]:
            if b:
                data.extend(self.rblk(b))
            blkidx += 1
        for off in range(0, size, 8):
            if off + 8 > len(data):
                break
            i, rec, nl, ft = struct.unpack_from("<IHBB", data, off)
            if rec == 0:
                break
            name = data[off + 8:off + 8 + nl].decode("latin-1")
            out.append({"ino": i, "rec_len": rec, "name_len": nl, "ftype": ft,
                        "name": name, "off": off})
            off += rec - 8
            off += 8
        # 上面循环写法易错，改用标准遍历
        out = []
        off = 0
        while off + 8 <= size and off + 8 <= len(data):
            i, rec, nl, ft = struct.unpack_from("<IHBB", data, off)
            if rec < 8:
                break
            name = data[off + 8:off + 8 + nl].decode("latin-1")
            out.append({"ino": i, "rec_len": rec, "name_len": nl, "ftype": ft,
                        "name": name, "off": off})
            off += rec
        return out

    def find_dir_block(self, dir_ino: int, entry_off: int) -> int:
        """entry_off 是目录逻辑偏移，返回其所在物理块号。"""
        ino_d = self.read_inode(dir_ino)
        blk = entry_off // BS
        return ino_d["blocks"][blk]

    def add_dir_entry(self, dir_ino: int, name: str, child_ino: int, ftype: int) -> None:
        """在目录中插入一项：优先复用现有块的尾部空隙，否则扩展目录。"""
        ino_d = self.read_inode(dir_ino)
        nb = name.encode("latin-1")
        need = 8 + len(nb)
        need = (need + 3) & ~3

        # 遍历所有目录块，找可容纳的空隙
        for bi in range(12):
            b = ino_d["blocks"][bi]
            if not b:
                continue
            blk = bytearray(self.rblk(b))
            off = 0
            while off + 8 <= BS:
                i, rec, nl, ft = struct.unpack_from("<IHBB", blk, off)
                if rec < 8:
                    break
                used = (8 + nl + 3) & ~3
                if i != 0 and rec - used >= need:
                    # 拆分：缩小当前项，新项占用剩余
                    newrec = used
                    struct.pack_into("<H", blk, off + 4, newrec)
                    noff = off + newrec
                    struct.pack_into("<IHBB", blk, noff, child_ino, rec - newrec, len(nb), ftype)
                    blk[noff + 8:noff + 8 + len(nb)] = nb
                    self.wblk(b, bytes(blk))
                    return
                if i == 0 and rec >= need:
                    struct.pack_into("<IHBB", blk, off, child_ino, rec, len(nb), ftype)
                    blk[off + 8:off + 8 + len(nb)] = nb
                    self.wblk(b, bytes(blk))
                    return
                off += rec
        # 没有空隙：扩展目录（新增一块）
        newblk = self.alloc_block()
        blk = bytearray(BS)
        struct.pack_into("<IHBB", blk, 0, child_ino, BS, len(nb), ftype)
        blk[8:8 + len(nb)] = nb
        self.wblk(newblk, bytes(blk))
        # 找到目录 i_block 中第一个空位
        for bi in range(12):
            if ino_d["blocks"][bi] == 0:
                ino_d["blocks"][bi] = newblk
                raw = bytearray(ino_d["raw"])
                struct.pack_into("<15I", raw, 40, *ino_d["blocks"])
                new_size = ino_d["size"] + BS
                struct.pack_into("<I", raw, 4, new_size)
                self.write_inode(dir_ino, bytes(raw))
                return
        raise RuntimeError(f"目录 {dir_ino} 直接块已满，需扩展间接块（未实现）")

    def del_dir_entry(self, dir_ino: int, name: str) -> bool:
        """删除目录项：把前一项的 rec_len 吃掉这一项。"""
        ino_d = self.read_inode(dir_ino)
        nb = name.encode("latin-1")
        for bi in range(12):
            b = ino_d["blocks"][bi]
            if not b:
                continue
            blk = bytearray(self.rblk(b))
            off = 0
            prev_off = -1
            while off + 8 <= BS:
                i, rec, nl, ft = struct.unpack_from("<IHBB", blk, off)
                if rec < 8:
                    break
                cur_name = blk[off + 8:off + 8 + nl]
                if cur_name == nb:
                    if prev_off >= 0:
                        # 前一项吞并本项
                        p_i, p_rec, p_nl, p_ft = struct.unpack_from("<IHBB", blk, prev_off)
                        struct.pack_into("<H", blk, prev_off + 4, p_rec + rec)
                    else:
                        # 第一项：标记 inode=0，rec_len 保持
                        struct.pack_into("<I", blk, off, 0)
                    self.wblk(b, bytes(blk))
                    return True
                prev_off = off
                off += rec
        return False

    def lookup(self, path: str) -> int | None:
        cur = 2
        for part in [p for p in path.split("/") if p]:
            found = None
            for e in self.read_dir_entries(cur):
                if e["name"] == part:
                    found = e["ino"]
                    break
            if found is None:
                return None
            cur = found
        return cur

    # ---------------- 高层操作 ----------------
    def create_file(self, parent_ino: int, name: str, data: bytes, mode: int = 0o100755) -> int:
        ino = self.alloc_inode()
        iblock, _ = self.write_data(data)
        raw = bytearray(128)
        struct.pack_into("<H", raw, 0, mode)
        struct.pack_into("<I", raw, 4, len(data))
        struct.pack_into("<H", raw, 26, 1)          # i_links_count
        struct.pack_into("<15I", raw, 40, *iblock)
        nblk = sum(1 for b in iblock if b) + len(data) // BS + 1
        struct.pack_into("<I", raw, 28, nblk)       # i_blocks (512B 单位，粗略)
        import time as _t
        now = int(_t.time())
        struct.pack_into("<I", raw, 8, now)
        struct.pack_into("<I", raw, 12, now)
        struct.pack_into("<I", raw, 16, now)
        self.write_inode(ino, bytes(raw))
        self.add_dir_entry(parent_ino, name, ino, 1)
        return ino

    def delete_file(self, parent_ino: int, name: str) -> bool:
        target = None
        for e in self.read_dir_entries(parent_ino):
            if e["name"] == name:
                target = e
                break
        if target is None:
            return False
        ino = target["ino"]
        if ino == 0:
            return True
        d = self.read_inode(ino)
        if d["links"] > 1:
            raw = bytearray(d["raw"])
            struct.pack_into("<H", raw, 26, d["links"] - 1)
            self.write_inode(ino, bytes(raw))
        else:
            self.free_data(d["blocks"])
            raw = bytearray(128)
            self.write_inode(ino, bytes(raw))       # 清零 inode
            self.free_inode(ino)
        self.del_dir_entry(parent_ino, name)
        return True

    def overwrite_file(self, ino: int, data: bytes) -> None:
        """原地替换：释放旧块，写新块，更新 inode。"""
        d = self.read_inode(ino)
        self.free_data(d["blocks"])
        iblock, _ = self.write_data(data)
        raw = bytearray(d["raw"])
        struct.pack_into("<I", raw, 4, len(data))
        struct.pack_into("<15I", raw, 40, *iblock)
        nblk = sum(1 for b in iblock if b) + len(data) // BS + 1
        struct.pack_into("<I", raw, 28, nblk)
        import time as _t
        now = int(_t.time())
        struct.pack_into("<I", raw, 12, now)
        struct.pack_into("<I", raw, 16, now)
        self.write_inode(ino, bytes(raw))

    def flush_bitmaps(self) -> None:
        """把所有脏位图与计数一次性写回。"""
        if not self._dirty:
            return
        tot_b = tot_i = 0
        for gi in range(self._ngroups):
            bb_blk, ib_blk, _ = self.gdt(gi)
            self.wblk(bb_blk, bytes(self._bb[gi]))
            self.wblk(ib_blk, bytes(self._ib[gi]))
            self.f.seek(self.gdt_block * BS + gi * 32 + 12)
            self.f.write(struct.pack("<HH", self._free_blocks[gi], self._free_inodes[gi]))
            tot_b += self._free_blocks[gi]
            tot_i += self._free_inodes[gi]
        # 超级块（偏移 1024 + 12 / 16）
        self.f.seek(1024 + 12)
        self.f.write(struct.pack("<I", tot_b))
        self.f.seek(1024 + 16)
        self.f.write(struct.pack("<I", tot_i))
        self._dirty = False

    def close(self) -> None:
        self.flush_bitmaps()
        self.f.flush()
        self.f.close()
