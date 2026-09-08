# -*- coding: utf-8 -*-
"""
fetch_holdings_full.py —— 全量补抓真实持仓主题占比(天天基金 fundf10 jjcc)

把全部(或前 N 只)基金的前十大持仓抓下来,按 THEME_KWS 聚合细分主题占比,
写入 funds.stocks(真实重仓股) 与 funds.themes(多主题带占比),并同步榜单快照。
格式示例: "通信设备1.9% · CPO/光模块1.9% · 半导体材料1.3%"

用法:
  python scripts/fetch_holdings_full.py --top                 # 只抓上榜基金(推荐,榜单排名完成后用)
  python scripts/fetch_holdings_full.py --limit 200           # 只抓 200 只(优先榜单/自选)
  python scripts/fetch_holdings_full.py                       # 全量 6000 只(约 1~1.5 小时,一般不必要)
  python scripts/fetch_holdings_full.py --sleep 0.2           # 调快(限流风险高)
断点续传: stocks 非空的自动跳过,可分批跑。
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
from collector.fetcher import fetch_holdings_w, holdings_themes
from collector.sector import infer_themes


def main() -> int:
    ap = argparse.ArgumentParser(description="抓取基金真实持仓主题")
    ap.add_argument("--top", action="store_true", help="只抓当日上榜基金(rank_snapshots 去重)")
    ap.add_argument("--limit", type=int, default=0, help="只抓前 N 只(0=全部)")
    ap.add_argument("--sleep", type=float, default=0.4, help="抓取前等待秒(限流保护)")
    ap.add_argument("--delay", type=float, default=0.5, help="抓取后等待秒")
    args = ap.parse_args()

    conn = db.get_conn()
    if args.top:
        # 只抓当日上榜基金
        today = conn.execute("SELECT MAX(date) d FROM rank_snapshots").fetchone()["d"]
        prio = {r[0] for r in conn.execute(
            "SELECT DISTINCT code FROM rank_snapshots WHERE date=? AND code != ''",
            (today,),
        ).fetchall()}
        all_rows = conn.execute("SELECT code, name FROM funds WHERE code IN (%s)" % ",".join("?" * len(prio)), list(prio)).fetchall() if prio else []
        rows = sorted(all_rows, key=lambda r: r["code"])
    else:
        # 榜单/自选涉及的基金优先
        prio = set()
        for r in conn.execute("SELECT DISTINCT code FROM rank_snapshots").fetchall():
            prio.add(r["code"])
        for r in conn.execute("SELECT code FROM watchlist").fetchall():
            prio.add(r["code"])
        all_rows = conn.execute("SELECT code, name FROM funds").fetchall()
        rows = sorted(all_rows, key=lambda r: (r["code"] not in prio, r["code"]))
    if args.limit:
        rows = rows[: args.limit]

    done = ok = empty = 0
    t0 = time.time()
    for r in rows:
        cur = conn.execute("SELECT stocks FROM funds WHERE code=?", (r["code"],)).fetchone()
        if cur and cur["stocks"] not in ("[]", "", None):
            done += 1
            continue  # 断点续传
        try:
            hs = fetch_holdings_w(r["code"])
            th = holdings_themes(hs) if hs else []
            th = [t for t in th if t[1] >= 0.5]  # 过滤过小占比(ETF联接底层零星持仓)  # v2.9.0 fix: tuple
            if not th:
                th = infer_themes(r["name"])  # ETF联接/解析失败兜底:名称推断细分
            stocks = [[str(s[0]), float(s[1])] for s in hs]
            conn.execute(
                "UPDATE funds SET stocks=?, themes=? WHERE code=?",
                (json.dumps(stocks, ensure_ascii=False),
                 json.dumps(th, ensure_ascii=False), r["code"]),
            )
            if th:
                ok += 1
            else:
                empty += 1
        except Exception as e:  # noqa: BLE001 单只失败不中断
            print(f"!! {r['code']} {r['name']}: {e}", flush=True)
            empty += 1
        done += 1
        if done % 25 == 0:
            conn.commit()
            cost = time.time() - t0
            print(f"... {done}/{len(rows)} 有主题{ok} 空{empty} "
                  f"耗时{cost:.0f}s 预计余{max(0, len(rows)-done)*cost/max(done,1):.0f}s",
                  flush=True)
        time.sleep(args.sleep + args.delay)
    conn.commit()
    print(f"抓取完成: 共处理 {done}, 有主题 {ok}, 无主题 {empty}")

    # 同步 rank_snapshots.meta 的 themes(榜单行渲染依赖)
    snap_updated = 0
    for s in conn.execute("SELECT id, code, meta FROM rank_snapshots").fetchall():
        try:
            meta = json.loads(s["meta"] or "{}")
        except (TypeError, ValueError):
            continue
        row = conn.execute("SELECT themes FROM funds WHERE code=?", (s["code"],)).fetchone()
        if row is None:
            continue
        new_th = json.loads(row["themes"] or "[]")
        if meta.get("themes") != new_th:
            meta["themes"] = new_th
            conn.execute(
                "UPDATE rank_snapshots SET meta=? WHERE id=?",
                (json.dumps(meta, ensure_ascii=False), s["id"]),
            )
            snap_updated += 1
    conn.commit()
    print(f"榜单快照同步 {snap_updated} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
