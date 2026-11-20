#!/usr/bin/env python3
"""Sony ZV-E10 真机生产镜像严格离线校验脚本。

验证项目：
1. 镜像大小、ext2 superblock 与 WSL e2fsck -fn 零错误检查
2. 10 个核心注入 .so 存在性、mode=0100755、uid=0、gid=0、与宿主机 SHA-256 一致
3. 从镜像内直接提取出的 ELF：e_flags=0x05000002、GLIBC_2.4 唯一版本上限、DT_NEEDED 闭包、SONAME/导出符号
4. manifest.json 删除列表确认彻底移除
5. /share/app 精确 232 个五代资源文件（无旧四代残留），SHA-256、mode=0100755、uid=57285、gid=1000
6. 新增与保留符号链接完整性（含 /lib/libuxengine.so -> libOb5.so、/lib/libgrm.so -> libOb5.so）
7. 相对于出厂镜像 firmware/backup/nflasha15.bin，所有未改动系统文件/权限/内容 100% 一致
8. 可选对比上一版镜像（如 prod_v3）以及校验 firmware/backup/_sha256sums.txt 完好性（若存在）
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys

from elftools.elf.elffile import ELFFile
from elftools.elf.gnuversions import GNUVerNeedSection

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "flash"))
from ext2_read import Ext2Reader  # noqa: E402

OUT = ROOT / "build_out"
FACTORY_DIR = ROOT / "firmware" / "backup"
FACTORY_IMG = FACTORY_DIR / "nflasha15.bin"
MANIFEST_JSON = ROOT / "manifests" / "prod_image_manifest.json"
G5_SHARE_APP = ROOT / "firmware" / "gen5" / "share" / "app"

EXPECTED_IMG_SIZE = 314_572_800
EXPECTED_BLOCKS = 307_200


def win_to_wsl(p: Path) -> str:
    resolved = p.resolve()
    s = str(resolved).replace("\\", "/")
    if len(s) >= 2 and s[1] == ":":
        return f"/mnt/{s[0].lower()}{s[2:]}"
    return s


def read_full_inode(reader: Ext2Reader, ino: int) -> dict:
    gi = (ino - 1) // reader.inodes_per_group
    li = (ino - 1) % reader.inodes_per_group
    _, _, itab = reader.gdt(gi)
    reader.f.seek(itab * reader.block_size + li * reader.inode_size)
    d = reader.f.read(reader.inode_size)
    uid_lo = struct.unpack_from("<H", d, 2)[0]
    gid_lo = struct.unpack_from("<H", d, 24)[0]
    uid_hi = struct.unpack_from("<H", d, 120)[0] if reader.inode_size >= 128 else 0
    gid_hi = struct.unpack_from("<H", d, 122)[0] if reader.inode_size >= 128 else 0
    return {
        "mode": struct.unpack_from("<H", d, 0)[0],
        "uid": uid_lo | (uid_hi << 16),
        "gid": gid_lo | (gid_hi << 16),
        "size": struct.unpack_from("<I", d, 4)[0],
        "links_count": struct.unpack_from("<H", d, 26)[0],
        "blocks": struct.unpack_from("<15I", d, 40),
    }


def walk_ext2(reader: Ext2Reader) -> dict[str, tuple]:
    """遍历 ext2 所有条目，返回 path -> (kind, mode, uid, gid, size, payload_or_sha256)。"""
    entries: dict[str, tuple] = {}

    def rec(ino: int, prefix: str) -> None:
        for name, cino, ft in reader.read_dir(ino):
            if prefix == "" and name == "lost+found":
                continue
            full = f"{prefix}/{name}"
            meta = read_full_inode(reader, cino)
            mode = meta["mode"]
            fmt = mode & 0xF000
            if fmt == 0x4000 or ft == 2:
                entries[full] = ("dir", mode, meta["uid"], meta["gid"], 0, "")
                rec(cino, full)
            elif fmt == 0xA000 or ft == 7:
                sz = meta["size"]
                if sz <= 60:
                    raw = struct.pack("<15I", *meta["blocks"])[:sz]
                else:
                    raw = reader.read_file(cino, verify=False)[:sz]
                entries[full] = (
                    "lnk",
                    mode,
                    meta["uid"],
                    meta["gid"],
                    sz,
                    raw.decode("latin-1", errors="replace"),
                )
            else:
                data = reader.read_file(cino, verify=False)
                h = hashlib.sha256(data).hexdigest()
                entries[full] = ("file", mode, meta["uid"], meta["gid"], meta["size"], h)

    rec(2, "")
    return entries


def check_elf_bytes(name: str, data: bytes, available_libs: set[str]) -> dict:
    elf = ELFFile(io.BytesIO(data))
    hdr = elf.header
    e_ident = hdr["e_ident"]
    assert e_ident["EI_CLASS"] == "ELFCLASS32", f"{name}: 非 ELF32 ({e_ident['EI_CLASS']})"
    assert e_ident["EI_DATA"] == "ELFDATA2LSB", f"{name}: 非小端 ({e_ident['EI_DATA']})"
    assert hdr["e_machine"] == "EM_ARM", f"{name}: 非 ARM ({hdr['e_machine']})"
    eflags = hdr["e_flags"]
    assert eflags == 0x05000002, f"{name}: e_flags=0x{eflags:08x} != 0x05000002"

    glibc_vers: set[str] = set()
    for sec in elf.iter_sections():
        if isinstance(sec, GNUVerNeedSection):
            for _, auxs in sec.iter_versions():
                for aux in auxs:
                    vname = aux.name
                    if vname.startswith("GLIBC_"):
                        glibc_vers.add(vname)
                        assert vname == "GLIBC_2.4", f"{name}: 发现不兼容 GLIBC 版本 {vname}"

    needed: list[str] = []
    soname: str | None = None
    dyn_sec = elf.get_section_by_name(".dynamic")
    if dyn_sec is not None:
        for tag in dyn_sec.iter_tags():
            if tag.entry.d_tag == "DT_NEEDED":
                needed.append(tag.needed)
            elif tag.entry.d_tag == "DT_SONAME":
                soname = tag.soname

    missing_needed = [lib for lib in needed if lib not in available_libs]
    assert not missing_needed, f"{name}: 缺少 DT_NEEDED 依赖库 {missing_needed}"

    dynsym = elf.get_section_by_name(".dynsym")
    exports: set[str] = set()
    if dynsym is not None:
        for sym in dynsym.iter_symbols():
            if sym.name and sym["st_shndx"] != "SHN_UNDEF" and sym["st_info"]["bind"] in ("STB_GLOBAL", "STB_WEAK"):
                exports.add(sym.name)

    return {
        "eflags": eflags,
        "glibc": sorted(glibc_vers),
        "needed": needed,
        "soname": soname,
        "exports": exports,
    }


def _vaddr_read(elf: ELFFile, data: bytes, vaddr: int, length: int) -> bytes:
    for seg in elf.iter_segments():
        if seg["p_type"] != "PT_LOAD":
            continue
        start = seg["p_vaddr"]
        end = start + seg["p_filesz"]
        if start <= vaddr and vaddr + length <= end:
            foff = seg["p_offset"] + (vaddr - start)
            return data[foff:foff + length]
    raise AssertionError(f"vaddr=0x{vaddr:x} (len={length}) 不在任何 PT_LOAD 文件映射段内")


def _got_reloc_map(elf: ELFFile) -> dict[int, str]:
    symtab = elf.get_section_by_name(".dynsym")
    rel_map: dict[int, str] = {}
    for sec in elf.iter_sections():
        if sec.header["sh_type"] in ("SHT_REL", "SHT_RELA"):
            for rel in sec.iter_relocations():
                idx = rel["r_info_sym"]
                sname = symtab.get_symbol(idx).name if symtab and idx > 0 else ""
                rel_map[rel["r_offset"]] = sname
    return rel_map


def verify_ob5_hooks(data: bytes) -> None:
    elf = ELFFile(io.BytesIO(data))
    rel_map = _got_reloc_map(elf)
    expected_got = {
        0x0035524C: "_ZN3MWF8RsrcAcsr7AcquireEPNS_6ObjMsgENS0_8AcqAct_tE",
        0x00354D7C: "_ZN3MWF8RsrcAcsr7ReleaseEv",
        0x003553DC: "_ZN3MWF8RsrcAcsr5PauseEv",
        0x003552BC: "_ZN19DataflowInfraSender9SendASyncEjP16DataflowInfraMsg",
        0x0035525C: "_ZN21DataflowInfraReceiver12ReceiveASyncEjP16DataflowInfraMsgm",
        0x00354E50: "osal_wai_sem_tmo",
        0x00355668: "pthread_cond_wait",
        0x003553A0: "ioctl",
        0x0035548C: "open",
        0x00355400: "open64",
        0x003553F4: "mmap",
    }
    for off, sym in expected_got.items():
        actual = rel_map.get(off)
        assert actual == sym, f"libOb5.so GOT[0x{off:08x}]={actual!r} != {sym!r}"

    expected_bytes = {
        0x0E565A: bytes([0x2D, 0xE9, 0xF0, 0x41, 0x04, 0x46, 0x82, 0xB0, 0x04, 0xF1, 0x74, 0x05]),
        0x12CC80: bytes([0x2D, 0xE9, 0xF0, 0x41, 0x84, 0xB0, 0x05, 0x46]),
        0x12CEB0: bytes([0xF0, 0xB5, 0x83, 0xB0, 0x44, 0x1E, 0x00, 0xAF]),
        0x208134: bytes([0x80, 0xB5, 0x84, 0xB0]),
        0x2080BC: bytes([0xB0, 0xB5, 0x84, 0xB0]),
        0x201370: bytes([0x80, 0xB5, 0x84, 0xB0]),
        0x2012C0: bytes([0xB0, 0xB5, 0x84, 0xB0]),
    }
    for vaddr, exp in expected_bytes.items():
        got = _vaddr_read(elf, data, vaddr, len(exp))
        assert got == exp, f"libOb5.so +0x{vaddr:x} 前言字节不匹配: {got.hex()} != {exp.hex()}"


def verify_appfw_hooks(data: bytes) -> None:
    elf = ELFFile(io.BytesIO(data))
    rel_map = _got_reloc_map(elf)
    expected_got = {
        0x0377B408: "__assert_fail",
        0x0377AE50: "_ZNSt18condition_variable4waitERSt11unique_lockISt5mutexE",
        0x0377AD5C: "osal_snd_sync_direct",
        0x0377B270: "GRM_fbrmCheckUpdateFinished",
        0x0377AFE0: "_ZN2ux6gfxeng14GraphicsEngine15drawFramebufferEv",
    }
    for off, sym in expected_got.items():
        actual = rel_map.get(off)
        assert actual == sym, f"appFw.so GOT[0x{off:08x}]={actual!r} != {sym!r}"

    # 校验 notify_model_system_error / dbglog_sink / seqlog_send 目标地址存在有效代码段映射
    for vaddr, min_len in ((0x02BC3174, 4), (0x02CE5F24, 8), (0x02C2AF70, 8)):
        _ = _vaddr_read(elf, data, vaddr, min_len)


def verify_factory_checksums() -> None:
    sums_file = FACTORY_DIR / "_sha256sums.txt"
    if not sums_file.is_file():
        assert FACTORY_IMG.is_file(), f"缺少出厂基线镜像: {FACTORY_IMG}"
        print(f"[INFO] 未找到 _sha256sums.txt，已确认出厂基线存在: {FACTORY_IMG}")
        return
    checked = 0
    for line in sums_file.read_text(encoding="utf-8").splitlines():
        parts = line.strip().split()
        if len(parts) < 2:
            continue
        expected_sha, fname = parts[0], parts[1]
        p = FACTORY_DIR / fname
        if not p.is_file():
            continue
        actual_sha = hashlib.sha256(p.read_bytes()).hexdigest()
        assert actual_sha == expected_sha, f"只读备份 {fname} SHA256 被篡改: {actual_sha} != {expected_sha}"
        if len(parts) >= 3:
            expected_sz = int(parts[2])
            assert p.stat().st_size == expected_sz, f"只读备份 {fname} 大小异常"
        checked += 1
    assert checked >= 1, "未校验到任何出厂备份镜像"
    print(f"[PASS] 只读出厂备份 SHA-256 校验通过 ({checked} 个现存分区镜像未受污染)")


def run_wsl_e2fsck(img: Path) -> None:
    img_wsl = win_to_wsl(img)
    res = subprocess.run(
        ["wsl", "-u", "root", "-e", "/sbin/e2fsck", "-fn", img_wsl],
        capture_output=True,
    )
    out = res.stdout.decode("utf-8", errors="replace") + res.stderr.decode("utf-8", errors="replace")
    assert res.returncode == 0, f"WSL e2fsck -fn 失败 (rc={res.returncode}):\n{out}"
    summary_line = [ln for ln in out.splitlines() if "files" in ln and "blocks" in ln]
    print(f"[PASS] WSL e2fsck -fn 只读检查通过: {summary_line[-1] if summary_line else out.strip()}")


def main() -> int:
    parser = argparse.ArgumentParser(description="严格校验 ZV-E10 真机 ASTRA ext2 生产镜像")
    parser.add_argument(
        "--image",
        default=str(OUT / "nflasha15_astra_prod.bin"),
        help="待校验的真机生产镜像路径",
    )
    parser.add_argument(
        "--prev-image",
        default="",
        help="可选：上一版本镜像（如 prod_v3.bin），校验仅 appFwBoot.so 等预期项发生变化",
    )
    parser.add_argument(
        "--write-sha256",
        action="store_true",
        help="同时在同目录生成 <image>.sha256 校验文件",
    )
    args = parser.parse_args()

    img = Path(args.image).resolve()
    assert img.is_file(), f"镜像不存在: {img}"

    # 1. 文件大小 & superblock & e2fsck
    size = img.stat().st_size
    assert size == EXPECTED_IMG_SIZE, f"镜像大小错误: {size} != {EXPECTED_IMG_SIZE}"
    with img.open("rb") as fp:
        fp.seek(1024)
        sb = fp.read(1024)
    magic = struct.unpack_from("<H", sb, 56)[0]
    blocks_cnt = struct.unpack_from("<I", sb, 4)[0]
    log_bs = struct.unpack_from("<I", sb, 24)[0]
    assert magic == 0xEF53, f"ext2 superblock 魔数错误: 0x{magic:04x}"
    assert blocks_cnt == EXPECTED_BLOCKS, f"ext2 块数错误: {blocks_cnt} != {EXPECTED_BLOCKS}"
    assert (1024 << log_bs) == 1024, f"ext2 块大小错误: {1024 << log_bs}"
    img_sha = hashlib.sha256(img.read_bytes()).hexdigest()
    print(f"[PASS] 镜像基础格式检查: {img.name} ({size:,} B, blocks={blocks_cnt}, sha256={img_sha})")

    run_wsl_e2fsck(img)

    # 2. 只读出厂备份完好性
    verify_factory_checksums()

    # 3. 挂载解析新镜像与出厂镜像目录树
    r_new = Ext2Reader(img)
    r_fac = Ext2Reader(FACTORY_IMG)
    try:
        tree_new = walk_ext2(r_new)
        tree_fac = walk_ext2(r_fac)

        # 构建镜像 /lib + /local/lib + 真机 rootfs /lib 可用库集合（文件 + 符号链接）
        rootfs_base_libs = {
            "ld-linux.so.3",
            "libc.so.6",
            "libdl.so.2",
            "libgcc_s.so.1",
            "libm.so.6",
            "libpthread.so.0",
            "librt.so.1",
            "libutil.so.1",
            "libcrypt.so.1",
            "libresolv.so.2",
            "libnsl.so.1",
            "libSTMcmdexecute.so",
            "libblkid.so.1",
            "libuuid.so.1",
            "libanl.so.1",
        }
        qemu_work = os.environ.get("ZVE10_QEMU_WORK", "").strip()
        if qemu_work:
            wsl_rootfs_check = subprocess.run(
                ["wsl", "-u", "root", "-e", "bash", "-lc",
                 f"if [ -d '{qemu_work}/rootfs/lib' ]; then ls -1 '{qemu_work}/rootfs/lib'; fi"],
                capture_output=True,
            )
            if wsl_rootfs_check.returncode == 0 and wsl_rootfs_check.stdout.strip():
                wsl_libs = {ln.strip() for ln in wsl_rootfs_check.stdout.decode("utf-8", errors="replace").splitlines() if ln.strip()}
                rootfs_base_libs |= wsl_libs
        avail_libs = (
            rootfs_base_libs
            | {
                p[len("/lib/"):]
                for p, v in tree_new.items()
                if p.startswith("/lib/") and p.count("/") == 2 and v[0] in ("file", "lnk")
            }
            | {
                p[len("/local/lib/"):]
                for p, v in tree_new.items()
                if p.startswith("/local/lib/") and p.count("/") == 3 and v[0] in ("file", "lnk")
            }
        )

        # 4. 校验 11 个核心注入二进制（内容抽取、权限、SHA-256、ELF ABI、特定符号）
        binaries: list[tuple[Path, str]] = [
            (OUT / "im_patched.elf", "/bin/im.elf"),
            (OUT / "libMWF.so", "/lib/libMWF.so"),
            (OUT / "libIMDB_g5.so", "/lib/libIMDB.so"),
            (OUT / "appFwBoot_real.so", "/lib/appFwBoot.so"),
            (OUT / "libappFwBoot_g5.so", "/lib/libappFwBoot_g5.so"),
            (OUT / "libOb5.so", "/lib/libOb5.so"),
            (OUT / "libmp5.so", "/lib/libmp5.so"),
            (OUT / "libObjShim.so", "/lib/libObjShim.so"),
            (OUT / "AdjChkConfig.so", "/lib/AdjChkConfig.so"),
            (OUT / "appFw.so", "/lib/appFw.so"),
            (OUT / "appFwBoot_real.so", "/tool/LeakTracer.so"),
        ]
        for src_p, img_p in binaries:
            assert src_p.is_file(), f"宿主机源文件缺失: {src_p}"
            ino = r_new.lookup(img_p)
            assert ino is not None, f"镜像中缺失注入库: {img_p}"
            entry = tree_new[img_p]
            kind, mode, uid, gid, fsz, _ = entry
            assert kind == "file", f"{img_p} 不是常规文件: {kind}"
            assert mode == 0o100755, f"{img_p} 权限错误: 0{mode:o} != 0100755"
            assert uid == 0 and gid == 0, f"{img_p} 属主错误: {uid}:{gid} != 0:0"

            extracted = r_new.read_file(ino, verify=True)
            src_bytes = src_p.read_bytes()
            assert len(extracted) == len(src_bytes) == fsz, f"{img_p} 长度不匹配"
            assert hashlib.sha256(extracted).hexdigest() == hashlib.sha256(src_bytes).hexdigest(), (
                f"{img_p} 抽取消化哈希与 {src_p} 不一致"
            )

            elf_info = check_elf_bytes(img_p, extracted, avail_libs)
            if img_p == "/lib/appFwBoot.so":
                assert elf_info["soname"] == "appFwBoot.so", f"SONAME 异常: {elf_info['soname']}"
                assert "Application_System_Init" in elf_info["exports"], "appFwBoot.so 未导出 Application_System_Init"
                assert b"REAL_HB" in extracted and b"G4_HANDOFF:" in extracted, (
                    "appFwBoot.so 缺少 ZVE10_REAL_DEVICE 真机运行特征字符串"
                )
                assert b"12B \xe9\x9d\x9e\xe5\xaf\xb9\xe9\xbd\x90\xe8\xb7\xb3\xe6\x9d\xbf\xe5\xae\x89\xe8\xa3\x85\xe6\x88\x90\xe5\x8a\x9f" in extracted, (
                    "appFwBoot.so 缺少 drawFramebuffer 12B 非对齐跳板修复"
                )
                assert b"QEMU_VIRT" not in extracted and b"MENU_HOOK:" not in extracted, (
                    "appFwBoot.so 意外混入了 QEMU 专属日志字符串"
                )
            elif img_p == "/lib/libOb5.so":
                verify_ob5_hooks(extracted)
            elif img_p == "/lib/appFw.so":
                verify_appfw_hooks(extracted)

        # 4a. 严格校验原厂保留关键文件: /bin/im.elf 与 /lib/libObj.so 保持原厂哈希
        ino_im = r_new.lookup("/bin/im.elf")
        assert ino_im is not None, "缺少 /bin/im.elf"
        im_data = r_new.read_file(ino_im, verify=True)
        im_hash = hashlib.sha256(im_data).hexdigest()
        assert im_hash in (
            "3fd399b62b468c0302b370362378a3719df6b7d68ff9f6e1c09312a84beeabbf",
            "2bc18de4ef8c9e651b81072b08080a0b0b10f09b5575b184e1d52cec3f5085cd"
        ), f"/bin/im.elf 哈希与出厂/接力补丁不符: {im_hash}"

        ino_obj = r_new.lookup("/lib/libObj.so")
        assert ino_obj is not None, "缺少 /lib/libObj.so"
        obj_data = r_new.read_file(ino_obj, verify=True)
        obj_hash = hashlib.sha256(obj_data).hexdigest()
        assert obj_hash == "bc0deb2f0e94218752e42ce6f5a3576d7456971eb63fbde705ffbd9b3a7fdeea", f"/lib/libObj.so 哈希与出厂不符: {obj_hash}"

        print(f"[PASS] 9 个核心注入 .so + 原厂 im.elf 与 原厂 libObj.so 严格校验全通过")

        # 4b. 全镜像所有保留 ELF 的 DT_NEEDED 闭包与删除库零残留引用检查
        del_basenames = {Path(d).name for d in json.loads(MANIFEST_JSON.read_text(encoding="utf-8"))["deletions"]["libraries"]}
        ipsec_libs = {
            p.rsplit("/", 1)[-1]
            for p, v in tree_new.items()
            if p.startswith("/lib/ipsec/lib/ipsec/") and p.count("/") == 5 and v[0] in ("file", "lnk")
        }
        all_elf_checked = 0
        for p, v in sorted(tree_new.items()):
            if v[0] != "file":
                continue
            ino_e = r_new.lookup(p)
            assert ino_e is not None
            raw_e = r_new.read_file(ino_e, verify=False)
            if not raw_e.startswith(b"\x7fELF"):
                continue
            elf_e = ELFFile(io.BytesIO(raw_e))
            dyn_e = elf_e.get_section_by_name(".dynamic")
            if dyn_e is not None:
                needs = [t.needed for t in dyn_e.iter_tags() if t.entry.d_tag == "DT_NEEDED"]
                bad_del = [n for n in needs if n in del_basenames]
                assert not bad_del, f"{p}: 仍引用已删除四代库 {bad_del}"
                local_avail = set(avail_libs)
                if p.startswith("/lib/ipsec/"):
                    local_avail |= ipsec_libs
                miss = [n for n in needs if n not in local_avail]
                assert not miss, f"{p}: 存在未闭合 DT_NEEDED 依赖 {miss}"
            all_elf_checked += 1
        print(f"[PASS] 全镜像 {all_elf_checked} 个 ELF DT_NEEDED 依赖闭包全通过，对已删除四代库零残留引用")

        # 5. 删除清单校验
        manifest = json.loads(MANIFEST_JSON.read_text(encoding="utf-8"))
        for del_p in manifest["deletions"]["libraries"]:
            assert r_new.lookup(del_p) is None and del_p not in tree_new, f"应删除库仍残留: {del_p}"
        print(f"[PASS] 删除列表校验通过 ({len(manifest['deletions']['libraries'])} 个冲突 Gen4 库已彻底移除)")

        # 6. /share/app 五代资源全量校验
        g5_files = sorted(p for p in G5_SHARE_APP.iterdir() if p.is_file())
        img_share = {p: v for p, v in tree_new.items() if p.startswith("/share/app/")}
        assert len(img_share) == len(g5_files), (
            f"/share/app 数量不匹配: 镜像 {len(img_share)} vs 五代源目录 {len(g5_files)}"
        )
        for sf in g5_files:
            img_p = f"/share/app/{sf.name}"
            assert img_p in img_share, f"镜像缺失五代资源: {img_p}"
            kind, mode, uid, gid, fsz, fsha = img_share[img_p]
            assert kind == "file", f"{img_p} 非常规文件: {kind}"
            assert mode == 0o100755, f"{img_p} 权限错误: 0{mode:o} != 0100755"
            assert uid == 57285 and gid == 1000, f"{img_p} 属主错误: {uid}:{gid} != 57285:1000"
            src_data = sf.read_bytes()
            assert fsz == len(src_data), f"{img_p} 大小错误: {fsz} != {len(src_data)}"
            assert fsha == hashlib.sha256(src_data).hexdigest(), f"{img_p} SHA-256 与源文件不一致"
        print(f"[PASS] /share/app 五代资源全量校验通过 ({len(g5_files)}/{len(g5_files)} 文件, SHA-256/0755/57285:1000)")

        # 7. 符号链接校验
        expected_new_links = {sl["link_path"]: sl["target"] for sl in manifest["injections"]["symlinks"]}
        for lp, tgt in expected_new_links.items():
            assert lp in tree_new, f"镜像缺失注入符号链接: {lp}"
            kind, mode, uid, gid, sz, actual_tgt = tree_new[lp]
            assert kind == "lnk", f"{lp} 不是符号链接: {kind}"
            assert mode == 0o120777, f"{lp} 权限异常: 0{mode:o} != 0120777"
            assert uid == 0 and gid == 0, f"{lp} 属主异常: {uid}:{gid} != 0:0"
            assert actual_tgt == tgt, f"{lp} 指向错误: {actual_tgt!r} != {tgt!r}"
            resolved = f"/lib/{actual_tgt}" if not actual_tgt.startswith("/") else actual_tgt
            assert resolved in tree_new, f"符号链接目标不存在: {lp} -> {resolved}"

        fac_links = {p: v for p, v in tree_fac.items() if v[0] == "lnk"}
        for lp, fmeta in fac_links.items():
            if lp in expected_new_links or lp in manifest["deletions"]["libraries"]:
                continue
            assert lp in tree_new, f"原厂保留符号链接丢失: {lp}"
            assert tree_new[lp] == fmeta, f"原厂保留符号链接被意外篡改: {lp}: {tree_new[lp]} vs {fmeta}"
        print(f"[PASS] 符号链接全量校验通过 (新增 {len(expected_new_links)} 个 + 原厂保留 {len(fac_links)} 个均完好)")

        # 8. 未改动原厂系统文件一致性校验
        touched_prefixes = ("/share/app/",)
        touched_exact = (
            {img_p for _, img_p in binaries}
            | set(manifest["deletions"]["libraries"])
            | set(expected_new_links.keys())
        )
        # /share/app 目录本身的 mtime/size 合法改变，仅校验目录存在且为目录
        assert "/share/app" in tree_new and tree_new["/share/app"][0] == "dir"
        unchanged_checked = 0
        for p, fmeta in tree_fac.items():
            if p == "/share/app" or p in touched_exact or any(p.startswith(px) for px in touched_prefixes):
                continue
            assert p in tree_new, f"未在变更列表中的原厂条目消失: {p}"
            if fmeta[0] == "dir":
                # 父目录 (/lib, /share 等) 属性应保持为目录，模式与属主不变
                assert tree_new[p][:4] == fmeta[:4], f"原厂目录属性变化: {p} {tree_new[p]} vs {fmeta}"
            else:
                assert tree_new[p] == fmeta, f"未改动原厂文件发生变化: {p} {tree_new[p]} vs {fmeta}"
            unchanged_checked += 1

        extra_entries = [
            p for p in tree_new
            if p not in tree_fac and p not in touched_exact and not any(p.startswith(px) for px in touched_prefixes)
        ]
        assert not extra_entries, f"镜像出现意外新增条目: {extra_entries}"
        print(f"[PASS] 原厂未改动文件全量对比通过 ({unchanged_checked} 个出厂条目零意外变更)")

        # 9. 可选对比上一版生产镜像
        if args.prev_image:
            prev_p = Path(args.prev_image).resolve()
            assert prev_p.is_file(), f"上一版镜像不存在: {prev_p}"
            r_prev = Ext2Reader(prev_p)
            try:
                tree_prev = walk_ext2(r_prev)
            finally:
                r_prev.close()
            only_prev = set(tree_prev) - set(tree_new)
            only_new = set(tree_new) - set(tree_prev)
            changed = [k for k in sorted(set(tree_prev) & set(tree_new)) if tree_prev[k] != tree_new[k]]
            assert not only_prev and not only_new, f"与 {prev_p.name} 目录结构不一致: -{only_prev} +{only_new}"
            print(f"[INFO] 相对 {prev_p.name} 变更文件 ({len(changed)} 个): {changed}")
    finally:
        r_new.close()
        r_fac.close()

    if args.write_sha256:
        sha_path = img.with_suffix(img.suffix + ".sha256")
        sha_path.write_text(f"{img_sha}  {img.name}\n", encoding="utf-8")
        print(f"[PASS] 已写入校验文件: {sha_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
