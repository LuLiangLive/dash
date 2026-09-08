# -*- coding: utf-8 -*-
"""
reco_align.py —— 推荐建议/持续天数对齐(实时回放口径)

背景: 基金卡片上的推荐买入建议标签来自 funds 表(reco/reco_days/prev_reco/prev_reco_days),
由 recompute_ad 的「每次运行 +1」增量维护, 与真实交易日不同步导致天数漂移;
而详情弹窗的推荐买入建议来自 _reco_history 实时回放(逐日重算, 天数=阶段持续交易日数)。
两者档位一致但天数不一致(如 008280 卡片 11 天 vs 回放 1 天)。

本脚本: 对全库基金跑与弹窗完全同源的 _reco_history 回放,
用回放结果(当前档位/持续天数/上一阶段)覆盖 funds 表, 并同步:
  - rank_snapshots.meta(reco/reco_days/prev_reco/prev_reco_days)
  - fund_reco_history(最新交易日快照, note=上一档)

用法: python scripts/reco_align.py            # 全库对齐(首次)
      python scripts/reco_align.py --only-boards   # 只对齐当日上榜基金(日常维护, 轻量)
      python scripts/reco_align.py --codes 008280,006503  # 只对齐指定基金
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db
from modules.anti.anti_detail import _reco_history  # v2.1.3: 从 main.py 迁移到 services.anti_detail


def main() -> int:
    ap = argparse.ArgumentParser(description="推荐建议/天数对齐(实时回放口径)")
    ap.add_argument("--only-boards", action="store_true", help="只对齐当日上榜基金(rank_snapshots 最新日期)")
    ap.add_argument("--codes", default="", help="逗号分隔的基金代码(只对齐这些)")
    args = ap.parse_args()

    conn = db.get_conn()
    if args.codes:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    elif args.only_boards:
        d = conn.execute("SELECT MAX(date) d FROM rank_snapshots").fetchone()
        codes = [r["code"] for r in conn.execute(
            "SELECT DISTINCT code FROM rank_snapshots WHERE date=? AND code!='' AND panel!='rot'",
            (d["d"],)).fetchall() if r["code"]]
        print(f"上榜基金 {len(codes)} 只(日期 {d['d']})")
    else:
        codes = [r["code"] for r in conn.execute("SELECT code FROM funds ORDER BY code").fetchall()]
    total = len(codes)
    latest_date = conn.execute("SELECT MAX(date) FROM nav_history").fetchone()[0]
    print(f"基金池 {total} 只, 最新净值日 {latest_date}")

    # 预取 rank_snapshots(code -> [(id, meta)])
    snap_by_code = {}
    for s in conn.execute("SELECT id, code, meta FROM rank_snapshots").fetchall():
        try:
            meta = json.loads(s["meta"] or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(meta, dict):
            continue
        snap_by_code.setdefault(s["code"], []).append((s["id"], meta))

    t0 = time.time()
    upd_f = upd_meta = upd_hist = skip = 0
    for i, code in enumerate(codes, 1):
        try:
            rh = _reco_history(code)
        except Exception:
            rh = []
        if not rh:
            skip += 1
            if i % 500 == 0:
                print(f"  {i}/{total} 对齐中(跳过 {skip})... 耗时 {time.time()-t0:.0f}s")
            continue
        last = rh[-1]
        # 上一阶段: 回放窗口内倒数第二段 [chg[-2], chg[-1]); 仅一段时窗口外不可知 → None
        cgi = [j for j, r in enumerate(rh) if r.get("chg")]
        prev_reco, prev_days = None, None
        if len(cgi) >= 2:
            p0, p1 = cgi[-2], cgi[-1]
            prev_reco, prev_days = rh[p0]["reco"], p1 - p0
        conn.execute(
            "UPDATE funds SET reco=?, reco_days=?, prev_reco=?, prev_reco_days=? WHERE code=?",
            (last["reco"], last["days"], prev_reco, prev_days, code),
        )
        upd_f += 1

        for sid, meta in snap_by_code.get(code, []):
            changed = False
            for k, v in (("reco", last["reco"]), ("reco_days", last["days"]),
                         ("prev_reco", prev_reco), ("prev_reco_days", prev_days)):
                if meta.get(k) != v:
                    meta[k] = v
                    changed = True
            if changed:
                conn.execute(
                    "UPDATE rank_snapshots SET meta=? WHERE id=?",
                    (json.dumps(meta, ensure_ascii=False), sid),
                )
                upd_meta += 1

        note = ""
        if prev_reco is not None:
            note = "上一档:%s%s天" % (prev_reco, prev_days or 1)
        conn.execute(
            "INSERT OR REPLACE INTO fund_reco_history(code,date,reco,days,note) VALUES(?,?,?,?,?)",
            (code, latest_date, last["reco"], last["days"] or 1, note),
        )
        upd_hist += 1

        if i % 500 == 0:
            conn.commit()
            print(f"  {i}/{total} 已对齐 {upd_f} 只(跳过 {skip}), 耗时 {time.time()-t0:.0f}s")

    conn.commit()
    print(f"完成: funds {upd_f} / meta {upd_meta} / history {upd_hist} / 跳过 {skip}, "
          f"总耗时 {time.time()-t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())

