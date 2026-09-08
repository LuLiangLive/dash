# -*- coding: utf-8 -*-
"""
recompute_earn.py —— 按方案B(百分位×60% + 绝对涨幅×40%)重算收益分/综合分

只重算 funds.earn_score/score 与 rank_snapshots.meta 中的对应字段,不重跑网络采集。
用法: python scripts/recompute_earn.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db
from collector.ranker import pool_earn_scores


def main() -> int:
    conn = db.get_conn()
    rows = conn.execute("SELECT code, d3, d5, d7, d10, m1 FROM funds").fetchall()

    def _ret2m(code):
        navs = [r["dwjz"] for r in conn.execute(  # v0.86.0: 用单位净值
            "SELECT dwjz FROM nav_history WHERE code=? ORDER BY date DESC LIMIT 41",
            (code,),
        ).fetchall()]
        if len(navs) >= 2 and navs[-1]:
            return (navs[0] / navs[-1] - 1) * 100
        return None

    metrics = [{"d3": r["d3"], "d5": r["d5"], "d7": r["d7"],
                "d10": r["d10"], "m1": r["m1"], "ret2m": _ret2m(r["code"])}
               for r in rows]
    scores = pool_earn_scores(metrics)
    for i, r in enumerate(rows):
        conn.execute("UPDATE funds SET earn_score=? WHERE code=?", (scores[i], r["code"]))
    conn.execute("UPDATE funds SET score=ROUND(earn_score*0.5 + ad_score*0.5)")
    conn.commit()

    # 同步 rank_snapshots.meta(卡片渲染依赖)
    snap_updated = 0
    for s in conn.execute("SELECT id, code, meta FROM rank_snapshots").fetchall():
        try:
            meta = json.loads(s["meta"] or "{}")
        except (TypeError, ValueError):
            continue
        f = conn.execute(
            "SELECT earn_score, ad_score, score FROM funds WHERE code=?",
            (s["code"],),
        ).fetchone()
        if f is None:
            continue
        changed = False
        for k in ("earn_score", "ad_score", "score"):
            if meta.get(k) != f[k]:
                meta[k] = f[k]
                changed = True
        # tscore:若原来等于旧 score,则跟随新 score(排行榜卡"综合 N")
        if meta.get("tscore") is not None and meta.get("score") == f["score"] \
                and abs((meta.get("tscore") or 0) - f["score"]) > 1:
            meta["tscore"] = f["score"]
            changed = True
        if changed:
            conn.execute(
                "UPDATE rank_snapshots SET meta=? WHERE id=?",
                (json.dumps(meta, ensure_ascii=False), s["id"]),
            )
            snap_updated += 1
    conn.commit()

    # 统计新分布
    dist = conn.execute('''SELECT
        SUM(earn_score>=90) g90, SUM(earn_score>=70) g70, SUM(earn_score>=50) g50,
        ROUND(AVG(earn_score),1) avg_e, ROUND(AVG(ad_score),1) avg_a, ROUND(AVG(score),1) avg_s
        FROM funds''').fetchone()
    print(f"收益分>=90: {dist['g90']}  >=70: {dist['g70']}  >=50: {dist['g50']}")
    print(f"平均: 收益={dist['avg_e']} 抗跌={dist['avg_a']} 综合={dist['avg_s']}")
    print(f"榜单快照同步 {snap_updated} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
