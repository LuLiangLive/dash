# -*- coding: utf-8 -*-
"""字段对齐自检: 展示字段 vs 系统字段管理 vs 实际落库覆盖率。

回答三个问题:
  1. 前端到底展示了哪些字段?（从 static/ 与 rendering/ 源码扫描）
  2. 这些字段在「字段字典 FIELD_MAP / funds 表 / 榜单 meta」里是否都有定义?
  3. 实际库里覆盖率如何, 缺哪些?

用法:
    python3.11 scripts/check_field_alignment.py              # 全量报告
    python3.11 scripts/check_field_alignment.py --json       # 机器可读
    python3.11 scripts/check_field_alignment.py --top 20     # 只看问题最大的 20 项

退出码: 0=无严重缺口, 1=存在覆盖率 <50% 的展示字段
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 前端 + 渲染层会用到的字段全集(v0.92.0 审计结果)
DISPLAY_FIELDS = [
    # 区间涨幅
    "d1", "d2", "d3", "d5", "d7", "d10", "m1", "m3", "m6", "y1", "ret2m",
    # 回撤与形态
    "max_daily_drop_7d", "dn7", "dd20", "mdd", "mdd_days", "mdd_status", "dd_from_hi",
    "hi_cnt", "up7", "max_daily_drop_7d", "max_daily_drop", "streak",
    # 风险
    "vol", "down_vol", "down_sharpe", "calmar", "pl",
    "up_ratio", "dn_ratio", "up_cap", "dn_cap",
    # 评分与信号
    "score", "tscore", "ad_score", "earn_score", "ms", "yindie",
    "reco", "reco_score", "reco_days", "prev_reco", "prev_reco_days",
    "verdict", "suggest",
    # 基金档案
    "name", "code", "sec", "ftype", "themes", "stocks",
    "scale", "est", "manager", "track", "nav", "nav_date", "is_etf",
    # 榜单附加
    # v2.11.4 C2/Q8: board_days 全链路下线, 从字段清单中移除
    "cros", "risk", "tstatus", "rtag",
]

# 仅在榜单 meta 里存在、funds 表没有的字段(不参与 funds 覆盖率考核)
META_ONLY = {"cros", "risk", "tstatus", "rtag", "tscore",
             "up7", "max_daily_drop_7d", "max_daily_drop", "ret2m", "up_cap", "dn_cap",
             "up_ratio", "dn_ratio"}

# 预期就不会 100% 的字段 —— 不是缺陷, 自检时只提示不判失败
# 否则每次跑都会报 5 个"缺口", 久而久之就没人看这个报告了
EXPECTED_PARTIAL = {
    "rtag": "运行时注入(main.py:103 / db.py:491 单独计算), 不落 meta, 故库里恒为空",
    "track": "仅指数/ETF 类基金有跟踪标的, 主动型本就为空(占全市场约 30%)",
    "cros": "同榜提醒, 只有同时登上多个日榜的基金才有值",
    "tscore": "仅稳涨/强趋势面板计算 tscore, 其他面板无此分",
    "yindie": "阴跌识别标签, 只有符合阴跌特征的基金才打标",
}


def scan_source_usage() -> dict[str, set[str]]:
    """扫描前端/渲染层源码, 返回 {字段: {出现的文件}}。"""
    files = []
    for sub in ("static", "rendering"):
        p = ROOT / sub
        if not p.exists():
            continue
        for f in p.rglob("*"):
            if f.suffix in (".html", ".js", ".css", ".py"):
                files.append(f)

    usage: dict[str, set[str]] = {}
    for f in files:
        try:
            s = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for c in DISPLAY_FIELDS:
            pats = [rf"\.{c}\b", rf"['\"]{c}['\"]", rf"\{{{c}\}}",
                    rf"\b{c}\s*:", rf"data-{c}\b"]
            for p in pats:
                if re.search(p, s):
                    usage.setdefault(c, set()).add(f.name)
                    break
    return usage


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--top", type=int, default=0, help="只显示问题最大的 N 项")
    args = ap.parse_args()

    try:
        from rendering.field_dict import FIELD_MAP
    except Exception as e:
        print(f"无法加载 FIELD_MAP: {e}")
        FIELD_MAP = {}

    conn = sqlite3.connect(ROOT / "fund.db")
    conn.row_factory = sqlite3.Row

    n_funds = conn.execute("SELECT COUNT(*) FROM funds").fetchone()[0]
    fund_cols = {r["name"] for r in conn.execute("PRAGMA table_info(funds)")}

    # --- meta 覆盖率 ---
    try:
        mrows = conn.execute(
            "SELECT meta FROM rank_snapshots WHERE date=(SELECT MAX(date) FROM rank_snapshots)"
        ).fetchall()
    except Exception:
        mrows = []
    n_meta = len(mrows)
    meta_filled: Counter = Counter()
    for (m,) in mrows:
        try:
            d = json.loads(m or "{}")
        except Exception:
            d = {}
        for k, v in d.items():
            if v not in (None, "", "—", [], {}):
                meta_filled[k] += 1

    # --- funds 覆盖率 ---
    usage = scan_source_usage()
    report = []
    for f in DISPLAY_FIELDS:
        used_in = sorted(usage.get(f, ()))
        in_map = "db" in FIELD_MAP.get(f, {}) if f in FIELD_MAP else False
        in_funds_col = f in fund_cols
        if in_funds_col:
            try:
                c = conn.execute(
                    f"SELECT COUNT(*) FROM funds WHERE {f} IS NOT NULL AND {f} != ''"
                ).fetchone()[0]
                fund_cov = c / n_funds * 100 if n_funds else 0.0
            except Exception:
                fund_cov = -1.0
        else:
            fund_cov = None
        meta_cov = meta_filled[f] / n_meta * 100 if n_meta else 0.0

        # 判定: 该字段的有效覆盖率取 funds / meta 中较高者
        eff = max([v for v in (fund_cov, meta_cov if f in meta_filled else 0.0)
                   if v is not None] or [0.0])
        if f in META_ONLY:
            eff = meta_cov
        if f in EXPECTED_PARTIAL:
            status = "BY-DESIGN"
        else:
            status = "OK" if eff >= 80 else ("LOW" if eff >= 30 else
                                             ("EMPTY" if eff <= 0 else "WARN"))
        report.append({
            "field": f, "used_in": used_in, "in_field_map": in_map,
            "in_funds_col": in_funds_col,
            "funds_cov": round(fund_cov, 1) if fund_cov is not None else None,
            "meta_cov": round(meta_cov, 1),
            "effective": round(eff, 1), "status": status,
        })

    conn.close()

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    order = {"EMPTY": 0, "WARN": 1, "LOW": 2, "OK": 3, "BY-DESIGN": 4}
    report.sort(key=lambda r: (order[r["status"]], r["effective"]))
    if args.top:
        report = report[: args.top]

    print(f"基金全表 {n_funds} 只 · 最新榜单快照 {n_meta} 行")
    print(f"字段字典 FIELD_MAP: {len(FIELD_MAP)} 项 · 审计展示字段 {len(DISPLAY_FIELDS)} 项")
    print()
    print(f"{'字段':13s} {'有效':>6s} {'funds':>7s} {'meta':>7s} {'字典':>5s} {'状态':>6s}  使用位置")
    print("-" * 96)
    for r in report:
        fc = "—" if r["funds_cov"] is None else f"{r['funds_cov']:.1f}%"
        mc = f"{r['meta_cov']:.1f}%"
        mp = "✓" if r["in_field_map"] else "✗"
        loc = ",".join(r["used_in"][:2]) or "(未检出)"
        print(f"{r['field']:13s} {r['effective']:5.1f}% {fc:>7s} {mc:>7s} {mp:>5s} "
              f"{r['status']:>6s}  {loc}")

    designed = [r for r in report if r["status"] == "BY-DESIGN"]
    if designed:
        print(f"\nℹ️  {len(designed)} 个字段按设计就是部分覆盖(非缺陷):")
        for r in designed:
            print(f"   - {r['field']} ({r['effective']}%): {EXPECTED_PARTIAL[r['field']]}")

    bad = [r for r in report if r["status"] in ("EMPTY", "WARN", "LOW")]
    print()
    if bad:
        print(f"⚠️  {len(bad)} 个字段覆盖率不足(需处理):")
        for r in bad:
            print(f"   - {r['field']}: 有效 {r['effective']}% "
                  f"(funds {r['funds_cov']}%, meta {r['meta_cov']}%)")
        return 1
    print("✅ 所有展示字段覆盖率达标(预期部分覆盖的字段除外)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
