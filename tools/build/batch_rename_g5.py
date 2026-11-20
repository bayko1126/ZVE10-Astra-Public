#!/usr/bin/env python3
"""批量对五代核心库执行等长同 GNU Hash 符号改名，输出到 build_out/"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLAN_JSON = ROOT / "manifests" / "rename_plan.json"
RENAME_TOOL = ROOT / "tools" / "build" / "elf_samehash_rename.py"
OUT_DIR = ROOT / "build_out"
SRC_DIR = ROOT / "firmware" / "gen5" / "lib"

OUT_DIR.mkdir(exist_ok=True)

with open(PLAN_JSON, "r", encoding="utf-8") as f:
    plan = json.load(f)

by_file = plan["by_file"]

for filename, mapping in by_file.items():
    src_file = SRC_DIR / filename
    out_file = OUT_DIR / filename
    if not src_file.exists():
        print(f"[!] 源文件不存在: {src_file}")
        continue
    
    print(f"\n[*] 正在处理: {filename} -> {out_file.name} (改名符号数: {len(mapping)})")
    # 生成临时的 mapping json
    tmp_map = OUT_DIR / f"_tmp_map_{filename}.json"
    with open(tmp_map, "w", encoding="utf-8") as f:
        json.dump(mapping, f, indent=2)
    
    cmd = [
        sys.executable, str(RENAME_TOOL),
        str(src_file), str(out_file),
        "--map", str(tmp_map),
        "--report", str(OUT_DIR / f"rename_report_{filename}.json")
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    tmp_map.unlink(missing_ok=True)
    if res.returncode != 0:
        print(f"[ERROR] {filename} 改名失败:\n{res.stderr}\n{res.stdout}")
        sys.exit(1)
    else:
        print(f"[+] 成功: {filename} ({out_file.stat().st_size:,} 字节)")

print("\n[+] 全部五代库同 Hash 符号改名成功完成！")
