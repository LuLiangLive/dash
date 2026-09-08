# -*- coding: utf-8 -*-
"""验证一键更新不会清空已补齐的档案字段。

用法:
    python3.11 scripts/verify_no_profile_loss.py             # 跑一次一键更新并对比
    python3.11 scripts/verify_no_profile_loss.py --skip-run  # 只打印当前覆盖率

背景:
    v0.92.0 修复前, 一键更新会把 est/manager 从 100% 打到 4.3%、m1 从 99.8% 打到 3.7%
    —— 因为 db.upsert_fund / recompute_ad 用的是无条件覆盖(excluded.k / ?), 而
    pipeline 构造的 dict 里根本没有这些字段(None 覆盖了真值)。
    本脚本把「补齐 → 更新 → 复查」固化成可重复执行的回归测试。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

WATCH = ("est", "manager", "track", "m1", "m3", "m6", "y1", "scale")


def snapshot(conn: sqlite3.Connection) -> dict[str, int]:
    n = conn.execute("SELECT COUNT(*) FROM funds").fetchone()[0]
    out = {"_total": n}
    for k in WATCH:
        out[k] = conn.execute(
            f"SELECT COUNT(*) FROM funds WHERE {k} IS NOT NULL AND {k} != ''"
        ).fetchone()[0]
    return out


def show(tag: str, s: dict) -> None:
    n = s["_total"]
    print(f"\n[{tag}] 基金总数 {n}")
    for k in WATCH:
        v = s[k]
        print(f"   {k:9s} {v:5d}/{n}  {v / n * 100:5.1f}%")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-run", action="store_true", help="不跑更新, 只打印覆盖率")
    args = ap.parse_args()

    conn = sqlite3.connect(ROOT / "fund.db")
    conn.row_factory = sqlite3.Row

    before = snapshot(conn)
    show("更新前", before)

    if args.skip_run:
        conn.close()
        return 0

    print("\n>>> 触发一键更新(增量)...")
    from collector.fetch_manager import get_manager
    m = get_manager()
    t0 = time.time()
    m.start(scope="incremental")
    while True:
        s = m.status() if hasattr(m, "status") else m.state
        if s.get("status") in ("done", "error", "stopped"):
            break
        time.sleep(2)
    print(f">>> 更新完成, 耗时 {time.time() - t0:.1f}s  状态={s.get('status')}")

    conn = sqlite3.connect(ROOT / "fund.db")
    conn.row_factory = sqlite3.Row
    after = snapshot(conn)
    show("更新后", after)

    print("\n=== 对比 ===")
    lost = []
    for k in WATCH:
        delta = after[k] - before[k]
        pct = (after[k] - before[k]) / max(1, before[k]) * 100
        flag = "❌ 丢失" if delta < -0.02 * max(1, before[k]) else "✅"
        if "❌" in flag:
            lost.append(k)
        print(f"   {k:9s} {before[k]:5d} -> {after[k]:5d}  ({pct:+.1f}%)  {flag}")

    conn.close()
    if lost:
        print(f"\n❌ 以下字段在更新后显著丢失: {', '.join(lost)}")
        return 1
    print("\n✅ 更新未造成档案字段丢失")
    return 0


if __name__ == "__main__":
    sys.exit(main())
