# -*- coding: utf-8 -*-
"""
make_update_zip.py —— 生成「轻量代码更新包」（不含 fund.db，<1MB）

云端轻量更新：数据留在沙箱（沙箱内跑采集/重算脚本），代码改动用本脚本
打成小 zip 上传，云端 AI 解压覆盖 + 重启即可，无需重传 64MB 数据库。

用法:
  python scripts/make_update_zip.py              # 打全部代码 → invest-code-update.zip
  python scripts/make_update_zip.py --since 2026-08-21   # 只含该日期后修改的文件

应用（发给云端 AI）:
  解压覆盖项目目录（保留 fund.db）→ pip install -r requirements.txt
  → 重启 python main.py 8002 → 重建隧道返回新链接
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)

# 代码目录/文件（排除数据/文档/备份）
CODE_DIRS = ("main.py", "db.py", "auth.py", "main_handler.py", "auto_update.py",
             "requirements.txt", "Dockerfile", "rendering", "collector", "static",
             "scripts", "analysis_pipeline")
EXCLUDE_DIRS = {"__pycache__", "functions", "backup", "logs", "dist", ".git",
                ".workbuddy", "node_modules", "data"}
EXCLUDE_FILES = {"fund.db", "fund.db-shm", "fund.db-wal", "invest-dashboard-deploy.zip",
                 "invest-code-update.zip", ".dockerignore"}


def main() -> int:
    ap = argparse.ArgumentParser(description="生成轻量代码更新包")
    ap.add_argument("--since", default="", help="只含该日期(YYYY-MM-DD)后修改的文件")
    ap.add_argument("--out", default="invest-code-update.zip", help="输出文件名")
    args = ap.parse_args()
    since_ts = 0.0
    if args.since:
        since_ts = time.mktime(time.strptime(args.since, "%Y-%m-%d"))

    zf = zipfile.ZipFile(args.out, "w", zipfile.ZIP_DEFLATED)
    inc, n = set(), 0

    def _add(fp: str) -> None:
        nonlocal n
        arc = (fp[2:] if fp.startswith("./") or fp.startswith(".\\") else fp).replace("\\", "/")
        if arc in inc or arc in EXCLUDE_FILES:
            return
        if os.path.getmtime(fp) >= since_ts:
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

    size = os.path.getsize(args.out) / 1024
    print(f"生成 {args.out}: {size:.0f}KB, 文件 {n} 个")
    if n == 0:
        print("(无改动文件，可尝试不带 --since 全量打代码包)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
