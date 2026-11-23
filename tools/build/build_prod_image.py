#!/usr/bin/env python3
"""构建 Sony ZV-E10 真机生产版 /usr ext2 镜像 (含最新跨代修复与真机专用 appFwBoot_real.so)。

用法:
  uv run --with pyelftools python tools/build/build_prod_image.py [--output build_out/nflasha15_astra_prod.bin]
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import sys

from elftools.elf.elffile import ELFFile

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "build_out"
FACTORY_IMG = ROOT / "firmware" / "backup" / "nflasha15.bin"
MANIFEST_JSON = ROOT / "manifests" / "prod_image_manifest.json"
USR_TREE_JSON = ROOT / "manifests" / "usr_tree.json"
G5_SHARE_APP = ROOT / "firmware" / "gen5" / "share" / "app"
G5_IMDB = OUT / "libIMDB_g5.so"


def win_to_wsl(p: Path) -> str:
    resolved = p.resolve()
    s = str(resolved).replace("\\", "/")
    if len(s) >= 2 and s[1] == ":":
        return f"/mnt/{s[0].lower()}{s[2:]}"
    return s


def verify_real_boot_elf(path: Path) -> None:
    raw = path.read_bytes()
    eflags = struct.unpack_from("<I", raw, 36)[0]
    if eflags != 0x05000002:
        raise RuntimeError(f"{path} e_flags=0x{eflags:08x} != 0x05000002")
    with open(path, "rb") as fp:
        elf = ELFFile(fp)
        dyn = elf.get_section_by_name(".dynsym")
        exports = {
            s.name for s in dyn.iter_symbols()
            if s.name and s["st_shndx"] != "SHN_UNDEF" and s["st_info"]["bind"] == "STB_GLOBAL"
        }
    if "Application_System_Init" not in exports:
        raise RuntimeError(f"{path} 缺少 Application_System_Init 导出: {exports}")


def run_wsl_root(script_text: str) -> str:
    res = subprocess.run(
        ["wsl", "-u", "root", "-e", "bash", "-s"],
        input=script_text.encode("utf-8"),
        capture_output=True,
    )
    stdout = res.stdout.decode("utf-8", errors="replace")
    stderr = res.stderr.decode("utf-8", errors="replace")
    if res.returncode != 0:
        raise RuntimeError(f"WSL 构建命令失败 (rc={res.returncode}):\n{stdout}\n{stderr}")
    return stdout


def main() -> int:
    parser = argparse.ArgumentParser(description="生成真机最新 ASTRA 生产镜像")
    parser.add_argument(
        "--output",
        default=str(OUT / "nflasha15_astra_prod.bin"),
        help="输出 ext2 镜像路径 (默认 build_out/nflasha15_astra_prod.bin)",
    )
    args = parser.parse_args()
    out_img = Path(args.output).resolve()
    out_img.parent.mkdir(parents=True, exist_ok=True)

    appfw_boot_real = OUT / "appFwBoot_real.so"
    if not appfw_boot_real.is_file():
        raise FileNotFoundError(f"未找到真机版引导器: {appfw_boot_real}，请先执行 tools/build/build_appfwboot.py --target all")
    verify_real_boot_elf(appfw_boot_real)

    im_patched = OUT / "im_patched.elf"
    if not im_patched.is_file():
        subprocess.run([sys.executable, str(ROOT / "tools" / "build" / "patch_im_elf.py")], check=True)

    imdb_src = G5_IMDB
    if not imdb_src.is_file() or imdb_src.stat().st_size != 37044:
        raise RuntimeError(f"真机 libIMDB_g5.so 异常或缺失: {imdb_src}，请先运行 tools/build/generate_hybrid_imdb.py")

    binaries: list[tuple[Path, str]] = [
        (im_patched, "/bin/im.elf"),
        (OUT / "libMWF.so", "/lib/libMWF.so"),
        (imdb_src, "/lib/libIMDB.so"),
        (appfw_boot_real, "/lib/appFwBoot.so"),
        (OUT / "libappFwBoot_g5.so", "/lib/libappFwBoot_g5.so"),
        (OUT / "libOb5.so", "/lib/libOb5.so"),
        (OUT / "libmp5.so", "/lib/libmp5.so"),
        (OUT / "libObjShim.so", "/lib/libObjShim.so"),
        (OUT / "AdjChkConfig.so", "/lib/AdjChkConfig.so"),
        (OUT / "appFw.so", "/lib/appFw.so"),
        (appfw_boot_real, "/tool/LeakTracer.so"),
    ]
    for src, img_p in binaries:
        if not src.is_file():
            raise FileNotFoundError(f"缺少生产二进制 {src} (目标 {img_p})")

    if not FACTORY_IMG.is_file():
        raise FileNotFoundError(f"缺少出厂基准镜像 {FACTORY_IMG}")

    manifest = json.loads(MANIFEST_JSON.read_text(encoding="utf-8"))
    usr_tree = json.loads(USR_TREE_JSON.read_text(encoding="utf-8"))
    g5_share_files = sorted(p for p in G5_SHARE_APP.iterdir() if p.is_file())
    if not g5_share_files:
        raise RuntimeError(f"缺少五代资源目录文件: {G5_SHARE_APP}")

    cmds: list[str] = []
    for lib_p in manifest["deletions"]["libraries"]:
        cmds.append(f"rm {lib_p}")
    for r in usr_tree:
        if r["path"].startswith("/share/app/") and not r["is_dir"]:
            cmds.append(f"rm {r['path']}")

    for src, img_p in binaries:
        cmds.append(f"rm {img_p}")
        cmds.append(f"write {win_to_wsl(src)} {img_p}")
        cmds.append(f"sif {img_p} mode 0100755")
        cmds.append(f"sif {img_p} uid 0")
        cmds.append(f"sif {img_p} gid 0")

    for sf in g5_share_files:
        img_p = f"/share/app/{sf.name}"
        cmds.append(f"rm {img_p}")
        cmds.append(f"write {win_to_wsl(sf)} {img_p}")
        cmds.append(f"sif {img_p} mode 0100755")
        cmds.append(f"sif {img_p} uid 57285")
        cmds.append(f"sif {img_p} gid 1000")

    for sl in manifest["injections"]["symlinks"]:
        lp = sl["link_path"]
        tgt = sl["target"]
        cmds.append(f"rm {lp}")
        cmds.append(f"symlink {lp} {tgt}")
        cmds.append(f"sif {lp} uid 0")
        cmds.append(f"sif {lp} gid 0")

    cmds.append("quit")
    cmd_blob = "\n".join(cmds) + "\n"
    cmds_win_tmp = OUT / ".prod_debugfs_cmds.tmp"
    cmds_win_tmp.write_text(cmd_blob, encoding="utf-8", newline="\n")

    factory_wsl = win_to_wsl(FACTORY_IMG)
    cmds_wsl = win_to_wsl(cmds_win_tmp)
    out_wsl = win_to_wsl(out_img)

    print(f"[*] 注入组件: {len(binaries)} 个核心 .so + {len(g5_share_files)} 个 /share/app 资源")
    print(f"[*] 启动 WSL 镜像构建与 ext2 严格检查 -> {out_img}")

    wsl_script = f"""set -euo pipefail
export PATH=/usr/sbin:/sbin:/usr/bin:/bin
TMP_IMG=/tmp/zve10_prod_build.bin
DBG_LOG=/tmp/zve10_prod_debugfs.log
trap 'rm -f "$TMP_IMG" "$DBG_LOG"' EXIT
cp -f '{factory_wsl}' "$TMP_IMG"
set +e
e2fsck -f -y "$TMP_IMG" >/dev/null
E2_RC1=$?
set -e
if [ "$E2_RC1" -ge 4 ]; then
    echo "ERROR: 初始 e2fsck -f -y 失败 (rc=$E2_RC1)" >&2
    exit 1
fi
resize2fs "$TMP_IMG" 307200 >/dev/null 2>&1
debugfs -w -f '{cmds_wsl}' "$TMP_IMG" >"$DBG_LOG" 2>&1
if grep -E "^(write|symlink|sif|mkdir|rmdir|ln):" "$DBG_LOG"; then
    echo "ERROR: debugfs 写入/属性操作存在报错" >&2
    exit 1
fi
if grep -v -E "^(debugfs:? |rm: .*File not found|Allocated inode:)" "$DBG_LOG" | grep -qE "[A-Za-z0-9]"; then
    echo "ERROR: debugfs 存在未预期输出:" >&2
    cat "$DBG_LOG" >&2
    exit 1
fi
set +e
e2fsck -f -y "$TMP_IMG" >/dev/null
E2_RC2=$?
set -e
if [ "$E2_RC2" -ge 4 ]; then
    echo "ERROR: 注入后 e2fsck -f -y 失败 (rc=$E2_RC2)" >&2
    exit 1
fi
e2fsck -fn "$TMP_IMG"
echo "--- VERIFY_KEY_FILES ---"
debugfs -R "stat /bin/im.elf" "$TMP_IMG" 2>/dev/null | grep -E "Type:|Size:|Inode:"
debugfs -R "stat /lib/libObj.so" "$TMP_IMG" 2>/dev/null | grep -E "Type:|Size:|Inode:"
debugfs -R "stat /lib/libOb5.so" "$TMP_IMG" 2>/dev/null | grep -E "Type:|Size:|Inode:"
debugfs -R "stat /lib/appFwBoot.so" "$TMP_IMG" 2>/dev/null | grep -E "Type:|Size:|Inode:"
debugfs -R "stat /lib/libIMDB.so" "$TMP_IMG" 2>/dev/null | grep -E "Type:|Size:|Inode:"
debugfs -R "stat /lib/appFw.so" "$TMP_IMG" 2>/dev/null | grep -E "Type:|Size:|Inode:"
dumpe2fs -h "$TMP_IMG" 2>/dev/null | grep -E "Block count:|Free blocks:|Free inodes:"

# 严格校验 /bin/im.elf 和 /lib/libObj.so 保持四代原厂哈希
debugfs -R "dump /bin/im.elf /tmp/im_check.elf" "$TMP_IMG" 2>/dev/null
debugfs -R "dump /lib/libObj.so /tmp/libobj_check.so" "$TMP_IMG" 2>/dev/null
IM_HASH=$(sha256sum /tmp/im_check.elf | awk '{{print $1}}')
LIBOBJ_HASH=$(sha256sum /tmp/libobj_check.so | awk '{{print $1}}')
rm -f /tmp/im_check.elf /tmp/libobj_check.so
echo "CHECK_IM_HASH=$IM_HASH"
echo "CHECK_LIBOBJ_HASH=$LIBOBJ_HASH"
if [ "$IM_HASH" != "2bc18de4ef8c9e651b81072b08080a0b0b10f09b5575b184e1d52cec3f5085cd" ]; then
    echo "ERROR: /bin/im.elf 哈希与预期补丁不匹配! ($IM_HASH)" >&2
    exit 1
fi
if [ "$LIBOBJ_HASH" != "bc0deb2f0e94218752e42ce6f5a3576d7456971eb63fbde705ffbd9b3a7fdeea" ]; then
    echo "ERROR: /lib/libObj.so 哈希与出厂不匹配! ($LIBOBJ_HASH)" >&2
    exit 1
fi
echo "[+] 严格校验通过: 补丁版 im.elf (2字节自动接力) 与 原厂 libObj.so 100% 保持出厂基准"
cp -f "$TMP_IMG" '{out_wsl}'
"""
    try:
        out_txt = run_wsl_root(wsl_script)
    finally:
        cmds_win_tmp.unlink(missing_ok=True)

    print(out_txt.strip())
    size_bytes = out_img.stat().st_size
    if size_bytes != 314572800:
        raise RuntimeError(f"产物镜像长度异常: {size_bytes} != 314572800")
    sha = hashlib.sha256(out_img.read_bytes()).hexdigest()
    print(f"[+] 真机生产镜像就绪: {out_img} ({size_bytes:,} B)")
    print(f"[+] SHA-256: {sha}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
