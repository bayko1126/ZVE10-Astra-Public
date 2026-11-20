#!/usr/bin/env python3
"""从用户自备的 ZV-E10 原厂 nflasha15.bin 与 ZV-1M2 解包目录提取构建基线到 firmware/。

用法:
  uv run python tools/build/prepare_inputs.py ^
    --gen4-image firmware/backup/nflasha15.bin ^
    --gen5-root <ZV-1M2 解包根目录> ^
    [--gen4-fw-imdb <可选: 官方固件解包版 37204B libIMDB.so>]

说明:
  本脚本只读取本地文件并提取至 Git 忽略目录 firmware/gen4 与 firmware/gen5，
  不联网、不随仓库分发任何索尼专有二进制。
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "flash"))
from ext2_read import Ext2Reader  # noqa: E402

GEN4_DIR = ROOT / "firmware" / "gen4"
GEN5_DIR = ROOT / "firmware" / "gen5"
BACKUP_DIR = ROOT / "firmware" / "backup"

EXPECTED_NFLASH15_SHA256 = "cb15bc410800add6ad605a3433773c7a287703140c85b1b2939192976240bc60"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fp:
        for chunk in iter(lambda: fp.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def extract_gen4_from_ext2(img_path: Path, fw_imdb: Path | None) -> None:
    if not img_path.is_file():
        raise FileNotFoundError(f"找不到 Gen4 ext2 镜像: {img_path}")

    size = img_path.stat().st_size
    if size != 314_572_800:
        print(f"[WARN] nflasha15.bin 大小为 {size:,} B（标准出厂镜像为 314,572,800 B）")

    img_sha = sha256_file(img_path)
    if img_sha == EXPECTED_NFLASH15_SHA256:
        print(f"[+] nflasha15.bin 出厂基线 SHA-256 校验通过: {img_sha}")
    else:
        print(f"[WARN] nflasha15.bin SHA-256={img_sha}，与默认 2.03 基线 ({EXPECTED_NFLASH15_SHA256}) 不同")

    (GEN4_DIR / "bin").mkdir(parents=True, exist_ok=True)
    (GEN4_DIR / "lib").mkdir(parents=True, exist_ok=True)

    reader = Ext2Reader(img_path)
    try:
        extract_targets = [
            ("/bin/im.elf", GEN4_DIR / "bin" / "im.elf"),
            ("/bin/bootin.elf", GEN4_DIR / "bin" / "bootin.elf"),
            ("/bin/sen.elf", GEN4_DIR / "bin" / "sen.elf"),
            ("/lib/libObj.so", GEN4_DIR / "lib" / "libObj.so"),
            ("/lib/libMWF.so", GEN4_DIR / "lib" / "libMWF.so"),
            ("/lib/libInfraSKD.so", GEN4_DIR / "lib" / "libInfraSKD.so"),
            ("/lib/libInfraLensCommunicator.so", GEN4_DIR / "lib" / "libInfraLensCommunicator.so"),
            ("/lib/libmpr.so", GEN4_DIR / "lib" / "libmpr.so"),
            ("/lib/libmprctrl.so", GEN4_DIR / "lib" / "libmprctrl.so"),
        ]
        for img_rel, dst in extract_targets:
            ino = reader.lookup(img_rel)
            if ino is None:
                raise FileNotFoundError(f"镜像 {img_path} 内缺失 {img_rel}")
            data = reader.read_file(ino, verify=True)
            dst.write_bytes(data)
            print(f"  [Gen4] {img_rel:36s} -> {dst.relative_to(ROOT)} ({len(data):,} B)")

        imdb_ino = reader.lookup("/lib/libIMDB.so")
        if imdb_ino is None:
            raise FileNotFoundError(f"镜像 {img_path} 内缺失 /lib/libIMDB.so")
        imdb_data = reader.read_file(imdb_ino, verify=True)
        if len(imdb_data) == 37044:
            imdb_dst = GEN4_DIR / "lib" / "libIMDB_dump_37044.so"
        elif len(imdb_data) == 37204:
            imdb_dst = GEN4_DIR / "lib" / "libIMDB_fw_37204.so"
        else:
            imdb_dst = GEN4_DIR / "lib" / f"libIMDB_{len(imdb_data)}.so"
            print(f"[WARN] 提取到的 libIMDB.so 长度为 {len(imdb_data)} B，非已知 37044/37204 B")
        imdb_dst.write_bytes(imdb_data)
        print(f"  [Gen4] /lib/libIMDB.so                     -> {imdb_dst.relative_to(ROOT)} ({len(imdb_data):,} B)")
    finally:
        reader.close()

    if fw_imdb is not None:
        if not fw_imdb.is_file():
            raise FileNotFoundError(f"找不到指定的 fw 版 libIMDB.so: {fw_imdb}")
        dst_fw = GEN4_DIR / "lib" / "libIMDB_fw_37204.so"
        shutil.copy2(fw_imdb, dst_fw)
        print(f"  [Gen4] 外部 fw libIMDB.so                  -> {dst_fw.relative_to(ROOT)} ({dst_fw.stat().st_size:,} B)")


def _find_candidate(root: Path, names: list[str]) -> Path | None:
    for rel in names:
        p = root / rel
        if p.is_file():
            return p
    return None


def prepare_gen5(gen5_root: Path) -> None:
    if not gen5_root.is_dir():
        raise FileNotFoundError(f"找不到 Gen5 解包目录: {gen5_root}")

    lib_out = GEN5_DIR / "lib"
    share_out = GEN5_DIR / "share" / "app"
    lib_out.mkdir(parents=True, exist_ok=True)
    share_out.mkdir(parents=True, exist_ok=True)

    mapping: list[tuple[list[str], str, bool]] = [
        (["lib/appFw.so", "usr/lib/appFw.so", "appFw.so"], "appFw.so", True),
        (["lib/appFwBoot.so", "usr/lib/appFwBoot.so", "appFwBoot.so"], "appFwBoot.so", True),
        (["lib/AdjChkConfig.so", "usr/lib/AdjChkConfig.so", "AdjChkConfig.so"], "AdjChkConfig.so", True),
        (["lib/libObj_g5.so", "lib/libObj.so", "usr/lib/libObj.so", "libObj_g5.so", "libObj.so"], "libObj_g5.so", True),
        (["lib/libmpr_g5.so", "lib/libmpr.so", "usr/lib/libmpr.so", "libmpr_g5.so", "libmpr.so"], "libmpr_g5.so", True),
        (["lib/libInfraFsys_g5.so", "lib/libInfraFsys.so", "usr/lib/libInfraFsys.so", "libInfraFsys_g5.so"], "libInfraFsys_g5.so", False),
    ]
    for candidates, dst_name, required in mapping:
        src = _find_candidate(gen5_root, candidates)
        if src is None:
            if required:
                raise FileNotFoundError(f"在 {gen5_root} 中未找到五代核心库（尝试路径: {candidates}）")
            continue
        dst = lib_out / dst_name
        shutil.copy2(src, dst)
        print(f"  [Gen5] {src.name:28s} -> {dst.relative_to(ROOT)} ({dst.stat().st_size:,} B)")

    share_src = None
    for cand in ("share/app", "usr/share/app", "app"):
        p = gen5_root / cand
        if p.is_dir() and any(p.iterdir()):
            share_src = p
            break
    if share_src is None:
        raise FileNotFoundError(f"在 {gen5_root} 中未找到五代 UI 资源目录 share/app")

    count = 0
    for f in sorted(share_src.iterdir()):
        if f.is_file():
            shutil.copy2(f, share_out / f.name)
            count += 1
    print(f"  [Gen5] {share_src.relative_to(gen5_root)} -> {share_out.relative_to(ROOT)} ({count} 个文件)")


def main() -> int:
    parser = argparse.ArgumentParser(description="提取并准备 ZV-E10 ASTRA 移植所需本地固件输入")
    parser.add_argument(
        "--gen4-image",
        type=Path,
        default=BACKUP_DIR / "nflasha15.bin",
        help="ZV-E10 原厂 nflasha15.bin 路径 (默认 firmware/backup/nflasha15.bin)",
    )
    parser.add_argument(
        "--gen5-root",
        type=Path,
        required=True,
        help="ZV-1M2 (Gen 5 ASTRA) 解包根目录（含 lib/ 与 share/app/）",
    )
    parser.add_argument(
        "--gen4-fw-imdb",
        type=Path,
        default=None,
        help="可选：官方固件解包版 37,204 B libIMDB.so（用于生成 QEMU 版 libIMDB_g5_fw.so）",
    )
    parser.add_argument(
        "--copy-backup",
        action="store_true",
        help="若 --gen4-image 不在 firmware/backup/nflasha15.bin，自动复制一份到该默认位置",
    )
    args = parser.parse_args()

    gen4_img = args.gen4_image.resolve()
    if args.copy_backup:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        default_img = BACKUP_DIR / "nflasha15.bin"
        if gen4_img != default_img.resolve():
            shutil.copy2(gen4_img, default_img)
            print(f"[+] 已复制出厂基线至 {default_img.relative_to(ROOT)}")

    print("[1/2] 提取 Gen4 (ZV-E10) 宿主与底层库...")
    extract_gen4_from_ext2(gen4_img, args.gen4_fw_imdb.resolve() if args.gen4_fw_imdb else None)

    print("[2/2] 整理 Gen5 (ZV-1M2) ASTRA 引擎与 UI 资产...")
    prepare_gen5(args.gen5_root.resolve())

    print("\n[+] 全部固件基线已就绪于 firmware/（已被 .gitignore 忽略）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
