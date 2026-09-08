# -*- coding: utf-8 -*-
"""
recompute_dd20.py —— 一次性回填 funds.dd20 为"真实距最近20交易日高点回撤"

背景: v2.11.2 前 funds.dd20 被当作 dd_from_hi(距序列高点)的别名落库;
增量模式下缓存最新==目标日期的基金不再重算, 导致升级后 dd20 仍是旧口径,
自选/榜单卡片"距20日高"与真实近20日高对不上。本脚本对全库逐只
按 nav_history 最近20个交易日(dwjz 非空)重算 dd20 并回写。

用法: python scripts/recompute_dd20.py [--commit]
     不带 --commit 为试跑(只统计, 不写库)
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db


def dd20_from_nav(navs: list[float]) -> float | None:
    """最近20个交易日(不足则用可用样本)的最高净值回撤, 与 ranker.calc_metrics 同口径。"""
    if not navs:
        return None
    win = [v for v in navs[-20:] if v]
    peak = max(win) if win else None
    if not peak or not navs[-1]:
        return None
    return round((navs[-1] / peak - 1) * 100, 2)


def main() -> int:
    commit = "--commit" in sys.argv
    conn = db.get_conn()
    codes = [r[0] for r in conn.execute("SELECT code FROM funds ORDER BY code").fetchall()]
    upd = stale = missing = 0
    for code in codes:
        rows = conn.execute(
            "SELECT dwjz FROM nav_history WHERE code=? AND dwjz IS NOT NULL ORDER BY date",
            (code,),
        ).fetchall()
        navs = [r[0] for r in rows]
        if len(navs) < 2:
            missing += 1
            continue
        new = dd20_from_nav(navs)
        if new is None:
            missing += 1
            continue
        cur = conn.execute("SELECT dd20 FROM funds WHERE code=?", (code,)).fetchone()
        old = cur[0] if cur else None
        if old is None or abs((old or 0) - new) >= 0.005:
            stale += 1
            if commit:
                conn.execute("UPDATE funds SET dd20=? WHERE code=?", (new, code))
    if commit:
        conn.commit()
    print(f"基金总数 {len(codes)}: 需更新/修正 {stale} 只, 无净值可算 {missing} 只")
    print("已" if commit else "(试跑未写库, 加 --commit 生效)")
    return 0


if __name__ == "__main__":
    main()
