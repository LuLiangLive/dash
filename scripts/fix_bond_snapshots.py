# -*- coding: utf-8 -*-
"""
fix_bond_snapshots.py —— 备选方案: 直接从 rank_snapshots 剔除债基(无需联网)

背景: 更新包在 collector/fetcher.py 新增 _is_bond_fund(债/货币/同业存单/固收/偏债/理财/现金),
但已打包的榜单快照仍含债基条目(如「中债0-3年政策性金融债指数C」)。
本脚本用同一过滤逻辑清洗 rank_snapshots,并对每个 (panel, sub) 重排 rank 序号。

用法: python scripts/fix_bond_snapshots.py [date]
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db
from collector.fetcher import _is_bond_fund

BOND_EXTRA = ("中债", "国开债", "政金债", "农发行", "信用债", "短融", "可转债", "利率债", "存单")


def is_bond_name(name: str) -> bool:
    return _is_bond_fund(name) or any(k in (name or "") for k in BOND_EXTRA)


def main() -> int:
    conn = db.get_conn()
    date = sys.argv[1] if len(sys.argv) > 1 else db.latest_rank_date()
    print(f"清洗日期: {date}")

    rows = conn.execute(
        "SELECT id, panel, sub, rank, code, name FROM rank_snapshots WHERE date=?",
        (date,),
    ).fetchall()
    total = len(rows)
    bond = [r for r in rows if is_bond_name(r["name"])]
    print(f"总条目 {total}, 债基 {len(bond)} 条:")
    for r in bond:
        print(f"  [{r['panel']}/{r['sub']}] #{r['rank']} {r['name']} ({r['code']})")

    if not bond:
        print("无债基,无需处理")
        return 0

    # 删除债基条目
    for r in bond:
        conn.execute("DELETE FROM rank_snapshots WHERE id=?", (r["id"],))

    # 每个 (panel, sub) 重排 rank
    subs = conn.execute(
        "SELECT DISTINCT panel, sub FROM rank_snapshots WHERE date=?", (date,)
    ).fetchall()
    for panel, sub in subs:
        items = conn.execute(
            "SELECT id FROM rank_snapshots WHERE date=? AND panel=? AND sub=? "
            "ORDER BY rank",
            (date, panel, sub),
        ).fetchall()
        for i, it in enumerate(items, 1):
            conn.execute(
                "UPDATE rank_snapshots SET rank=? WHERE id=?", (i, it["id"])
            )
    conn.commit()

    # 验证
    n_after = conn.execute(
        "SELECT COUNT(*) FROM rank_snapshots WHERE date=?", (date,)
    ).fetchone()[0]
    print(f"清洗后总条目 {n_after}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
