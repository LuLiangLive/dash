"""重算近7日单日最大回撤(dd7)并同步 funds 表 + rank_snapshots.meta。
口径: 近7个交易日内单日最大跌幅(单日回撤), nav 最早在前。
用法: python scripts/recompute_dd7.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db


def calc_dd7(navs_latest_first):
    """navs_latest_first: 最新在前的净值列表 → 近7日单日最大回撤(%)。"""
    r7 = navs_latest_first[:8]  # 8个点=7段单日变化
    dd7 = None
    for i in range(1, len(r7)):
        prev, cur = r7[i], r7[i - 1]  # i-1 更新, i 更旧
        if prev:
            chg = (cur - prev) / prev * 100
            if chg < 0 and (dd7 is None or chg < dd7):
                dd7 = chg
    return dd7


def main() -> int:
    conn = db.get_conn()
    codes = [r["code"] for r in conn.execute("SELECT code FROM funds").fetchall()]
    upd = snap = 0
    # 预取每只基金最近 12 条净值(最新在前),避免逐条查询
    rows = conn.execute(
        "SELECT code, date, dwjz FROM nav_history ORDER BY code, date DESC"  # v0.86.0: 用单位净值
    ).fetchall()
    by_code = {}
    for r in rows:
        by_code.setdefault(r["code"], []).append(r["dwjz"])
    # 先重算 funds.dd7
    for code in codes:
        navs = by_code.get(code) or []
        dd7 = calc_dd7(navs)
        if dd7 is not None:
            conn.execute("UPDATE funds SET max_daily_drop_7d=? WHERE code=?", (dd7, code))
            upd += 1
    conn.commit()
    print(f"funds.dd7 更新 {upd}/{len(codes)}")
    # 同步 rank_snapshots.meta 里的 dd7
    snap_rows = conn.execute(
        "SELECT rowid, meta FROM rank_snapshots WHERE meta LIKE '%dd7%'"
    ).fetchall()
    for r in snap_rows:
        try:
            m = json.loads(r[1])
        except Exception:
            continue
        code = m.get("code")
        navs = by_code.get(code)
        if not navs:
            continue
        dd7 = calc_dd7(navs)
        if dd7 is not None and m.get("max_daily_drop_7d") != dd7:
            m["max_daily_drop_7d"] = dd7
            conn.execute(
                "UPDATE rank_snapshots SET meta=? WHERE rowid=?",
                (json.dumps(m, ensure_ascii=False), r[0]),
            )
            snap += 1
    conn.commit()
    print(f"rank_snapshots.meta dd7 同步 {snap} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
