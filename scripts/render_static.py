"""预渲染完整静态 index.html(注入 WATCH_DATA + 榜单 HTML + 全量净值/指数数据)供 CloudStudio 静态部署。
静态版前端据此实现弹窗/对比本地计算 + 实时添加(JSONP)能力。
用法: python scripts/render_static.py → 输出 dist/index.html
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db

STATIC = ROOT / "static"
OUT = ROOT / "dist"

IDX_CODES = [("sh", "sh000001"), ("cyb", "sz399006"), ("kc", "sh000688")]
NAV_DAYS = 42  # 近2个月(约42交易日)
META_KEYS = [
    "name", "ftype", "score", "ad_score", "earn_score", "ms", "sec",
    "scale", "est", "manager", "track",
    "d3", "d5", "d7", "d10", "max_daily_drop_7d", "dn7", "dd20", "m1", "m3", "m6", "y1",
    "mdd", "mdd_days", "mdd_status", "vol", "down_vol", "down_sharpe",
    "calmar", "pl", "hi_cnt", "dd_from_hi", "streak", "themes",
]


def build_static_nav() -> dict:
    """导出 日期轴 + 三指数日涨跌 + 全基金净值 + 指标 meta。"""
    conn = db.get_conn()
    # 日期轴: 上证最近 43 条(升序) → 后 42 个日期
    rows43 = conn.execute(
        "SELECT date, ljjz FROM nav_history WHERE code='sh000001' ORDER BY date DESC LIMIT %d" % (NAV_DAYS + 1)
    ).fetchall()
    rows43 = list(reversed(rows43))
    if len(rows43) < 3:
        return {}
    dates = [r["date"] for r in rows43[1:]]  # 42 个日期
    # 三指数日涨跌幅(对齐 dates)
    idx = {}
    for key, ick in IDX_CODES:
        rows = conn.execute(
            "SELECT date, ljjz FROM nav_history WHERE code=? ORDER BY date DESC LIMIT %d" % (NAV_DAYS + 1), (ick,)
        ).fetchall()
        rows = list(reversed(rows))
        vals = [r["ljjz"] for r in rows]
        dmap = {r["date"]: r["ljjz"] for r in rows}
        arr = []
        for d in dates:
            if d in dmap:
                ii = [x for x in range(1, len(rows)) if rows[x]["date"] == d]
                if ii and vals[ii[0] - 1]:
                    arr.append(round((vals[ii[0]] / vals[ii[0] - 1] - 1) * 100, 4))
                else:
                    arr.append(None)
            else:
                arr.append(None)
        idx[key] = arr
    # 全基金净值(近 NAV_DAYS+20 条, 对齐 dates, 缺失 null)
    navs = {}
    fund_rows = conn.execute(
        "SELECT code, date, ljjz FROM nav_history "
        "WHERE code NOT IN ('sh000001','sz399006','sh000688') ORDER BY code, date DESC"
    ).fetchall()
    by = {}
    for r in fund_rows:
        by.setdefault(r["code"], []).append((r["date"], r["ljjz"]))
    for code, items in by.items():
        items = items[: NAV_DAYS + 20]
        dmap = dict(items)
        arr = [dmap.get(d) for d in dates]
        if any(v is not None for v in arr):
            navs[code] = [round(v, 4) if v is not None else None for v in arr]
    # 指标 meta
    funds = {}
    for r in conn.execute("SELECT * FROM funds").fetchall():
        d = dict(r)
        funds[d["code"]] = {k: d.get(k) for k in META_KEYS}
    # 净值只保留 funds 表存在的 code(剔除历史 A 类残留,减小体积)
    navs = {c: v for c, v in navs.items() if c in funds}
    return {"dates": dates, "idx": idx, "navs": navs, "funds": funds, "codes": list(navs.keys())}


def fund_html_content() -> str:
    date = db.latest_rank_date()
    if not date:
        return '<div style="padding:40px;text-align:center;color:var(--muted)">📭 暂无榜单数据</div>'
    from rendering.md_builder import build_fund_markdown
    from rendering.fund_page import render_fund_view
    md = build_fund_markdown(date)
    return render_fund_view(md, ts=time.strftime("%Y-%m-%d %H:%M:%S"))


def main() -> int:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    watch_data = {}
    for w in db.list_watchlist():
        f = db.get_fund(w["code"])
        if f:
            f["_v"] = 2
            watch_data[w["code"]] = f
    static_nav = build_static_nav()
    html = html.replace("__WATCH_DATA_JSON__", json.dumps(watch_data, ensure_ascii=False))
    html = html.replace("__STATIC_NAV_JSON__", json.dumps(static_nav, ensure_ascii=False))
    html = html.replace("__FUND_HTML__", fund_html_content())
    html = html.replace("__NOW__", time.strftime("%Y-%m-%d %H:%M:%S"))
    OUT.mkdir(exist_ok=True)
    (OUT / "index.html").write_text(html, encoding="utf-8")
    print(f"dist/index.html: {len(html)//1024}KB | dates={len(static_nav.get('dates', []))} "
          f"navs={len(static_nav.get('navs', {}))} funds_meta={len(static_nav.get('funds', {}))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
