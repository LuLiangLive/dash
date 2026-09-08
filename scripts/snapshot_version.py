# -*- coding: utf-8 -*-
"""
snapshot_version.py —— 创建版本代码归档（便于回滚）

每次完成功能/修复并验证通过后，用本脚本把当前代码快照归档到
backup/versions/vX.Y-code.zip（不含 fund.db），并写入归档说明。

用法:
  python scripts/snapshot_version.py v0.30 "本次改动说明"
  python scripts/snapshot_version.py v0.30 "说明" --note "补充说明"

回滚:
  需要回滚到某版本时，用 backup/versions/vX.Y-code.zip 覆盖项目目录代码
  （保留 fund.db），重启服务即可；如需回滚数据，用 backup/fund.db.pre_* 覆盖库。
  详见 docs/版本清单与回滚手册.md。
"""
from __future__ import annotations

import argparse
import os
import sys
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)

# 与 make_update_zip.py 一致的代码范围
CODE_DIRS = ("main.py", "db.py", "auth.py", "main_handler.py", "auto_update.py",
             "requirements.txt", "Dockerfile", "rendering", "collector", "static",
             "scripts", "analysis_pipeline")
EXCLUDE_DIRS = {"__pycache__", "functions", "backup", "logs", "dist", ".git",
                ".workbuddy", "node_modules", "data"}
EXCLUDE_FILES = {"fund.db", "fund.db-shm", "fund.db-wal", "invest-dashboard-deploy.zip",
                 "invest-code-update.zip", ".dockerignore"}


def main() -> int:
    ap = argparse.ArgumentParser(description="创建版本代码归档")
    ap.add_argument("version", help="版本号，如 v0.30")
    ap.add_argument("desc", help="改动说明（一句话）")
    ap.add_argument("--note", default="", help="补充说明")
    args = ap.parse_args()

    ver = args.version.strip().lower()
    if not ver.startswith("v"):
        ver = "v" + ver
    out_dir = ROOT / "backup" / "versions"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_zip = out_dir / f"{ver}-code.zip"
    out_txt = out_dir / f"{ver}-code.txt"

    zf = zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED)
    inc, n = set(), 0

    def _add(fp: str) -> None:
        nonlocal n
        arc = (fp[2:] if fp.startswith("./") or fp.startswith(".\\") else fp).replace("\\", "/")
        if arc in inc or arc in EXCLUDE_FILES:
            return
        zf.write(fp, arc)
        inc.add(arc)
        n += 1

    for p in CODE_DIRS:
        if os.path.isfile(p):
            _add(p)
            continue
        if not os.path.isdir(p):
            continue
        for root, dirs, files in os.walk(p):
            dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
            for f in files:
                if f in EXCLUDE_FILES or f.endswith((".pyc", ".log")):
                    continue
                _add(os.path.join(root, f))
    zf.close()

    # 写入版本说明
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"版本: {ver}",
        f"归档时间: {now}",
        f"改动: {args.desc}",
    ]
    if args.note:
        lines.append(f"备注: {args.note}")
    lines.append(f"文件数: {n}（不含 fund.db）")
    out_txt.write_text("\n".join(lines) + "\n", encoding="utf-8")

    size = os.path.getsize(out_zip) / 1024
    print(f"已归档: {out_zip} ({size:.0f}KB, {n} 文件)")
    print(f"说明: {out_txt}")
    print("提示: 记得在 docs/版本清单与回滚手册.md 版本总览表插入新版本行。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
