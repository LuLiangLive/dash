#!/usr/bin/env python3
"""批量回填上榜基金的档案 + 持仓数据到 funds 表。

背景:
  funds 表的 scale/est(规模/成立日) 与 stocks/themes(持仓/主题) 长期大面积为空, 导致:
  1) 弹窗(anti)每次为补 scale/est 发起网络请求(~0.15s)
  2) 自选添加为补持仓发起网络请求(实测单只可达 1.57s)
  3) ETF 卡片缺「规模/成立」展示

  这几类数据变化都极慢(档案几乎不变, 持仓按季度披露), 预填一次可长期受益。

用法: python3 scripts/backfill_fund_profile.py [并发数]
"""
from __future__ import annotations

import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import db  # noqa: E402
from collector import fetcher  # noqa: E402


def collect_codes() -> list[str]:
    """上榜基金(最新交易日全部榜单) + 自选, 去重。"""
    con = db.get_conn()
    codes: set[str] = set()
    try:
        latest = con.execute("SELECT MAX(date) FROM rank_snapshots").fetchone()[0]
        if latest:
            for r in con.execute(
                "SELECT DISTINCT code FROM rank_snapshots WHERE date=?", (latest,)
            ):
                codes.add(r["code"])
    except Exception as e:
        print("读取榜单失败:", e)
    try:
        for r in con.execute("SELECT code FROM watchlist"):
            codes.add(r["code"])
    except Exception:
        pass
    return sorted(codes)


def _as_list(v) -> list:
    """funds 表里的 stocks/themes 可能是 JSON 字符串或 list。"""
    import json as _j
    if isinstance(v, str):
        try:
            return _j.loads(v) if v else []
        except Exception:
            return []
    return v if isinstance(v, list) else []


def main() -> int:
    workers = int(sys.argv[1]) if len(sys.argv) > 1 else 8
    codes = collect_codes()
    todo_profile, todo_holdings = [], []
    for c in codes:
        f = db.get_fund(c) or {}
        if not f.get("scale") or not f.get("est"):
            todo_profile.append(c)
        if not _as_list(f.get("stocks")):
            todo_holdings.append(c)
    print(f"上榜+自选 {len(codes)} 只")
    print(f"  待补档案(scale/est): {len(todo_profile)} 只")
    print(f"  待补持仓(stocks)  : {len(todo_holdings)} 只")
    if not todo_profile and not todo_holdings:
        print("无需回填")
        return 0

    t0 = time.time()

    def _profile(code: str):
        try:
            return code, fetcher.fetch_basic(code)
        except Exception:
            return code, None

    def _holdings(code: str):
        try:
            raw = fetcher.fetch_holdings_w(code)
            return code, [[s.get("name"), s.get("pct")] for s in (raw or [])[:10] if s.get("name")]
        except Exception:
            return code, []

    ok_p = ok_h = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(_profile, c) for c in todo_profile]
        futs += [ex.submit(_holdings, c) for c in todo_holdings]
        for i, f in enumerate(as_completed(futs), 1):
            code, data = f.result()
            if not data:
                continue
            try:
                f = db.get_fund(code) or {}
                patch = dict(f)
                patch["code"] = code
                # 档案: (scale, est) 元组
                if isinstance(data, tuple):
                    scale, est = data[0], data[1]
                    if scale and not patch.get("scale"):
                        patch["scale"] = scale
                    if est and not patch.get("est"):
                        patch["est"] = est
                    ok_p += 1
                # 持仓: [[name, pct], ...]
                elif isinstance(data, list):
                    patch["stocks"] = data
                    if not _as_list(patch.get("themes")):
                        try:
                            th = fetcher.holdings_themes(data) or []  # [(label, weight), ...]
                            patch["themes"] = [
                                # v2.9.0 fix: holdings_themes returns tuples, t.get() raised AttributeError
                                [t[0], t[1]] for t in th[:6] if t and t[0]
                            ]
                        except Exception:
                            pass
                    ok_h += 1
                db.upsert_fund(patch)
            except Exception:
                pass
            if i % 60 == 0:
                print(f"  ...{i}/{len(futs)}  用时 {time.time() - t0:.1f}s")

    print(f"回填完成: 档案 {ok_p} 只 / 持仓 {ok_h} 只, 用时 {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
