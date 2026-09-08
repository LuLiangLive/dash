"""
run.py —— 采集器 CLI 单次执行入口

用法:
    python collector/run.py                 # 全量采集
    python collector/run.py --max 5         # 限制基金数(调试)
    python collector/run.py --log 10        # 采集后打印最近任务日志
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db
from collector.pipeline import run_collector


def main():
    ap = argparse.ArgumentParser(description="投研看板数据采集器(单次)")
    ap.add_argument("--max", type=int, default=20, help="最多抓取基金数")
    ap.add_argument("--log", type=int, default=0, help="打印最近 N 条任务日志")
    args = ap.parse_args()

    print("=" * 46)
    print("投研看板采集器 · 单次执行")
    print("=" * 46)
    result = run_collector(max_funds=args.max, verbose=True)
    print("-" * 46)
    print("采集结果:", json.dumps(result, ensure_ascii=False, indent=2))
    if args.log:
        print("-" * 46)
        for log in db.recent_logs(args.log):
            print(f"#{log['id']} {log['status']:8s} {log['started_at']} "
                  f"{log['message'][:80]}")


if __name__ == "__main__":
    main()
