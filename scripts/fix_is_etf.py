# -*- coding: utf-8 -*-
"""修复 funds.is_etf 全为 0 的问题。

问题:
    funds 表 6001 只基金中 is_etf 列有 6000 只为 0、1 只为 NULL,
    但 ftype 列明明标了 'ETF联接C'(887 只) / '指数C'(883 只), 名称里也带
    'ETF'/'指数'/'联接'(合计约 1770 只)。说明建池时 is_etf 的推断结果没有落库。

影响:
    1. collector/filters.py:27  is_etf_filter 直接读该列 -> ETF 筛选失效
    2. rank_full.py:272/330/428  纯 `if f.get("is_etf")` 分支 -> 永不命中
    3. v0.92.0 档案补齐: track(跟踪标的)仅对 is_etf 基金抓取 -> 覆盖率恒为 0
    (rank_full.py:206 的 is_idx_fund 有名称兜底, 所以 ETF 榜还能出数, 掩盖了问题)

修复策略(与 rank_full.is_idx_fund 同口径, 避免两套标准):
    ftype 含 'ETF'/'指数'  或  名称含 'ETF'/'指数'/'联接'  ->  is_etf = 1
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _infer_is_etf(name: str | None, ftype: str | None) -> int:
    n = name or ""
    t = ftype or ""
    if "ETF" in t or "指数" in t:
        return 1
    if "ETF" in n or "指数" in n or "联接" in n:
        return 1
    return 0


def main() -> int:
    db_path = ROOT / "fund.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    before = dict(conn.execute("SELECT IFNULL(is_etf,-1), COUNT(*) FROM funds GROUP BY 1").fetchall())
    print(f"修复前 is_etf 分布: {before}")

    rows = conn.execute("SELECT code, name, ftype FROM funds").fetchall()
    plan = []
    for r in rows:
        new = _infer_is_etf(r["name"], r["ftype"])
        plan.append((new, r["code"]))

    conn.executemany("UPDATE funds SET is_etf=? WHERE code=?", plan)
    conn.commit()

    after = dict(conn.execute("SELECT IFNULL(is_etf,-1), COUNT(*) FROM funds GROUP BY 1").fetchall())
    print(f"修复后 is_etf 分布: {after}")
    changed = sum(1 for new, _ in plan if new == 1)
    print(f"标记为指数/ETF 类: {changed} 只 / 共 {len(plan)} 只")

    # 抽查
    print("\n抽查:")
    for r in conn.execute(
        "SELECT code, name, ftype, is_etf FROM funds WHERE is_etf=1 LIMIT 5"
    ).fetchall():
        print(f"   {r['code']}  {r['name'][:26]:28s} ftype={r['ftype']:10s} is_etf={r['is_etf']}")
    for r in conn.execute(
        "SELECT code, name, ftype, is_etf FROM funds WHERE is_etf=0 LIMIT 3"
    ).fetchall():
        print(f"   {r['code']}  {r['name'][:26]:28s} ftype={r['ftype']:10s} is_etf={r['is_etf']}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
