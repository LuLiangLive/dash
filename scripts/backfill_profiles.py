# -*- coding: utf-8 -*-
"""全量补齐 funds 表档案字段(manager / est / track / scale / m3 / m6 / y1)。

背景:
    v0.92.0 之前, funds 表 6001 只基金中:
        manager 100% 为空 / track 100% 为空 / est 4% / scale 4% / m3·m6·y1 仅 3%
    根因是 nav_history 只有 63~125 条(126~251 条为 0 只), 从净值序列推不出近6月/近1年;
    而 f10 概况页一次请求就能给出 成立日+跟踪标的+基金经理+规模 等 9 个字段, 却从未被写入。

    rank_full._fill_listed_profiles 会在每次更新时补齐「上榜+自选」约 250 只,
    但未上榜基金点开弹窗时仍是空的。本脚本做一次全量补齐, 之后:
      - 静态字段(est/track/manager)永久有效, 不必再抓
      - 时变字段(m1/m3/m6/y1/scale)由日常更新维护

用法:
    python3.11 scripts/backfill_profiles.py                 # 全量补齐
    python3.11 scripts/backfill_profiles.py --limit 200     # 只补 200 只(试跑)
    python3.11 scripts/backfill_profiles.py --static-only   # 只补静态字段(est/track/manager)
    python3.11 scripts/backfill_profiles.py --workers 4     # 指定并发(默认4, 实测最优)

实测: 并发4 时 pingzhongdata 约 20ms/只、f10 约 51ms/只; 并发再高会触发东方财富限流
      (并发10 退化到 220ms/只)。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 静态字段: 一次补齐, 长期有效
STATIC_FIELDS = ("est", "track", "manager")
# 时变字段: 每个交易日都变
VOLATILE_FIELDS = ("m1", "m3", "m6", "y1", "scale")
ALL_FIELDS = STATIC_FIELDS + VOLATILE_FIELDS


def _plan(conn: sqlite3.Connection, only_missing_static: bool):
    """返回 [(code, need_tuple), ...]"""
    rows = conn.execute(
        "SELECT code, name, is_etf, est, track, manager, m3, m6, y1 FROM funds ORDER BY code"
    ).fetchall()
    plan = []
    for r in rows:
        code, name, is_etf = r["code"], r["name"] or "", r["is_etf"]
        need = []
        for k in STATIC_FIELDS:
            if not r[k]:
                # track 只有指数/ETF 才有, 主动基金不必强求(省一次 f10 请求)
                if k == "track" and not is_etf and "ETF" not in name and "指数" not in name and "联接" not in name:
                    continue
                need.append(k)
        if not only_missing_static:
            need.extend(VOLATILE_FIELDS)
        if need:
            plan.append((code, tuple(dict.fromkeys(need))))
    return plan


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="最多处理多少只(0=不限)")
    ap.add_argument("--workers", type=int, default=4, help="并发数(默认4, 实测最优)")
    ap.add_argument("--static-only", action="store_true", help="只补静态字段")
    args = ap.parse_args()

    from collector import fetcher

    conn = sqlite3.connect(ROOT / "fund.db")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")

    plan = _plan(conn, args.static_only)
    if args.limit:
        plan = plan[: args.limit]
    total = len(plan)
    if not total:
        print("✅ 无需补齐, 所有字段已齐全")
        return 0
    print(f"待补齐 {total} 只 (并发 {args.workers}, {'仅静态' if args.static_only else '静态+时变'})")

    def _work(item):
        code, need = item
        try:
            return code, need, fetcher.fetch_profile(code, need=need)
        except Exception:
            return code, need, {}

    t0 = time.time()
    done = ok = 0
    lock = __import__("threading").Lock()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(_work, it) for it in plan]
        for fu in as_completed(futs):
            code, need, prof = fu.result()
            done += 1
            if not prof:
                if done % 200 == 0:
                    print(f"  ... {done}/{total} ({time.time()-t0:.0f}s)")
                continue
            cols, vals = [], []
            for k in ALL_FIELDS:
                if prof.get(k) is not None and k in need:
                    cols.append(f"{k}=?")
                    vals.append(prof[k])
            if cols:
                vals.append(code)
                with lock:
                    conn.execute(
                        f"UPDATE funds SET {','.join(cols)} WHERE code=?", vals)
                ok += 1
            if done % 200 == 0:
                with lock:
                    conn.commit()
                print(f"  ... {done}/{total} 成功{ok} ({time.time()-t0:.0f}s)")
    conn.commit()
    dt = time.time() - t0
    print(f"\n完成: 处理 {done} 只 · 成功 {ok} 只 · 耗时 {dt:.1f}s "
          f"({dt/max(1,done)*1000:.0f}ms/只)")

    # 复核覆盖率
    n = conn.execute("SELECT COUNT(*) FROM funds").fetchone()[0]
    print(f"\nfunds 全表 {n} 只 —— 补齐后字段覆盖率:")
    for k in ALL_FIELDS:
        c = conn.execute(
            f"SELECT COUNT(*) FROM funds WHERE {k} IS NOT NULL AND {k} != ''").fetchone()[0]
        print(f"   {k:9s} {c:5d}/{n}  {c/n*100:5.1f}%")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
