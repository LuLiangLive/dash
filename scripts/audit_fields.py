# -*- coding: utf-8 -*-
"""字段审计：找出「抓了但没人用」的僵尸字段，以及「要用但没数据」的空跑字段。

用法:
    python3 scripts/audit_fields.py            # 全量审计
    python3 scripts/audit_fields.py --json     # 机器可读输出

判定口径（关键：必须区分读写，否则写入方引用会被误判成"在用"）:
  消费方 = static/ + rendering/ + main.py      真正把数据呈现给用户的代码
  生产方 = collector/ + analysis_pipeline/ + domain/ + scripts/  抓取/计算/落库

  ★僵尸      消费方 0 引用 且 生产方有写入  → 只写不读，可停止抓取
  ★死字段    消费方 0 引用 且 生产方无引用  → 完全没人碰
  !空跑      消费方在用 但库里全空          → 另一类问题：链路断了

v0.93.0 首轮审计结论（已修复，此脚本用于防止复发）:
  funds 列 50 个 → 僵尸 1 个(is_etf，但它是抓取优化的判定依据，保留)
  meta  键 66 个 → 僵尸 9 个: up7 / max_daily_drop_7d / dn_ratio / is_etf / rtag /
                   status / sections / periods / reco_note  → 已全部下线
"""
from __future__ import annotations

import os
import re
import json
import sqlite3
import collections
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "fund.db")

CONSUMER_DIRS = ["static", "rendering"]
CONSUMER_FILES = ["main.py"]
PRODUCER_DIRS = ["collector", "analysis_pipeline", "domain", "scripts"]

# 已知的 CSS 类名 / 局部变量，命中它们不算"数据读取"
CSS_NOISE = re.compile(r"^\s*[.#@]|--[a-z-]+:|class=")

# 前端不直接展示、但后端确实用到的字段 —— 不算僵尸，别误报
#   is_etf: rank_full._fill_listed_profiles 用它判断「是否值得为 track 打一次
#           f10 请求」(非 ETF 没有跟踪标的，白打一次)，是抓取优化的关键判据。
KEEP_ALIVE = {
    "is_etf": "后端抓取优化判据(决定要不要为 track 打 f10)，非展示字段",
}


def _load(dirs, exts=(".py", ".js", ".html")):
    files = {}
    for d in dirs:
        p = os.path.join(ROOT, d)
        if not os.path.isdir(p):
            continue
        for fn in sorted(os.listdir(p)):
            if fn.endswith(exts):
                files[f"{d}/{fn}"] = open(os.path.join(p, fn), encoding="utf-8",
                                          errors="ignore").read()
    return files


def _load_files(names):
    out = {}
    for n in names:
        p = os.path.join(ROOT, n)
        if os.path.isfile(p):
            out[n] = open(p, encoding="utf-8", errors="ignore").read()
    return out


CONS_F = _load(CONSUMER_DIRS)
CONS_F.update(_load_files(CONSUMER_FILES))
PROD_F = _load(PRODUCER_DIRS)


def _read_hits(files, key):
    """真正的『读取』：下标取值 / .get() / 属性访问 / SQL SELECT"""
    rx = re.compile(
        rf'\[\s*["\']{key}["\']\s*\]'                 # d["key"]
        rf'|\.get\(\s*["\']{key}["\']'                # d.get("key")
        rf'|\.\b{key}\b(?!\s*[=(])'                   # d.key
        rf'|SELECT[\s\S]{{0,400}}?\b{key}\b[\s\S]{{0,200}}?FROM',
        re.M)
    hits = []
    for fp, txt in files.items():
        for m in rx.finditer(txt):
            ln = txt.count("\n", 0, m.start()) + 1
            line = txt.splitlines()[ln - 1].strip()
            if CSS_NOISE.match(line):     # 排除 .fmetric.rtag 这类 CSS 选择器
                continue
            hits.append((fp, ln, line[:100]))
    return hits


def _write_hits(files, key):
    rx = re.compile(
        rf'SET\s+[^;\n]*\b{key}\s*='                  # UPDATE ... SET key=
        rf'|["\']{key}["\']\s*:\s*[^,\n}}]+\s*[,}}]'  # {"key": v}
        rf'|\b{key}\s*=\s*[^=\n]', re.M)
    hits = []
    for fp, txt in files.items():
        for m in rx.finditer(txt):
            ln = txt.count("\n", 0, m.start()) + 1
            hits.append((fp, ln, txt.splitlines()[ln - 1].strip()[:100]))
    return hits


def audit_funds(con):
    cols = [r[1] for r in con.execute("PRAGMA table_info(funds)")]
    total = con.execute("SELECT COUNT(*) FROM funds").fetchone()[0]
    rows = []
    for c in cols:
        n = con.execute(
            f"SELECT COUNT(*) FROM funds WHERE {c} IS NOT NULL "
            f"AND TRIM(CAST({c} AS TEXT))!=''").fetchone()[0]
        r, w = _read_hits(CONS_F, c), _write_hits(PROD_F, c)
        if not r and c in KEEP_ALIVE:
            verdict = "keep"          # 后端在用，仅前端不展示
        elif not r:
            verdict = "zombie" if w else "dead"
        elif n == 0:
            verdict = "empty"
        else:
            verdict = "ok"
        rows.append({"field": c, "verdict": verdict, "reads": len(r),
                     "writes": len(w), "rows": n, "pct": round(n / total * 100, 1),
                     "note": KEEP_ALIVE.get(c, "")})
    return rows


def audit_meta(con, latest_only=True):
    """只审计最新一期快照。

    库里留存着历史榜单，旧代码写下的僵尸键会一直躺在里面；若连历史一起扫，
    修完仍然会一直报僵尸，脚本就失去"回归守门"的意义。最新一期才是当前代码的产物。
    """
    if latest_only:
        row = con.execute("SELECT MAX(date) FROM rank_snapshots").fetchone()
        latest = row[0] if row else None
        sql, args = "SELECT meta FROM rank_snapshots WHERE date=?", (latest,)
    else:
        sql, args = "SELECT meta FROM rank_snapshots", ()
    keys = collections.Counter()
    filled = collections.Counter()
    for (m,) in con.execute(sql, args):
        if not m:
            continue
        try:
            d = json.loads(m)
        except Exception:
            continue
        if not isinstance(d, dict):
            continue
        for k, v in d.items():
            keys[k] += 1
            if v not in (None, "", [], {}):
                filled[k] += 1
    rows = []
    for k, cnt in keys.most_common():
        r, w = _read_hits(CONS_F, k), _write_hits(PROD_F, k)
        if not r:
            verdict = "zombie" if w else "dead"
        elif filled[k] == 0:
            verdict = "empty"
        else:
            verdict = "ok"
        rows.append({"field": k, "verdict": verdict, "reads": len(r),
                     "writes": len(w), "rows": cnt, "filled": filled[k]})
    return rows


def main():
    con = sqlite3.connect(DB)
    f = audit_funds(con)
    m = audit_meta(con)
    as_json = "--json" in sys.argv

    zf = [r["field"] for r in f if r["verdict"] in ("zombie", "dead")]
    zm = [r["field"] for r in m if r["verdict"] in ("zombie", "dead")]
    kf = [r["field"] for r in f if r["verdict"] == "keep"]
    ef = [r["field"] for r in f if r["verdict"] == "empty"]
    em = [r["field"] for r in m if r["verdict"] == "empty"]

    if as_json:
        print(json.dumps({"funds": f, "meta": m,
                          "zombie": {"funds": zf, "meta": zm},
                          "empty": {"funds": ef, "meta": em}},
                         ensure_ascii=False, indent=2))
        return 0 if not (zf or zm) else 1

    sym = {"ok": " ", "zombie": "★", "dead": "★", "empty": "!", "keep": "·"}
    print("=" * 84)
    print(f"funds 表列（{len(f)}）   消费方 = static/rendering/main.py")
    print("=" * 84)
    print(f"{'':2}{'列':<14}{'读':>4}{'写':>4}{'非空':>8}{'覆盖':>7}  判定")
    for r in f:
        v = {"zombie": "僵尸(前端不读)", "dead": "死字段(无人引用)",
             "empty": "前端要但无数据", "keep": "后端在用(有意保留)", "ok": ""}[r["verdict"]]
        print(f"{sym[r['verdict']]:2}{r['field']:<14}{r['reads']:>4}{r['writes']:>4}"
              f"{r['rows']:>8}{r['pct']:>6.1f}%  {v}")

    print()
    print("=" * 84)
    print(f"rank_snapshots.meta 键（{len(m)}）")
    print("=" * 84)
    print(f"{'':2}{'键':<18}{'读':>4}{'写':>4}{'出现':>7}{'有值':>7}  判定")
    for r in m:
        v = {"zombie": "僵尸(前端不读)", "dead": "死字段(无人引用)",
             "empty": "前端要但全空", "ok": ""}[r["verdict"]]
        print(f"{sym[r['verdict']]:2}{r['field']:<18}{r['reads']:>4}{r['writes']:>4}"
              f"{r['rows']:>7}{r['filled']:>7}  {v}")

    print()
    print("=" * 84)
    print(f"僵尸/死字段  funds: {zf or '无 ✓'}")
    print(f"僵尸/死字段  meta : {zm or '无 ✓'}")
    print(f"空跑字段    funds: {ef or '无 ✓'}")
    print(f"空跑字段    meta : {em or '无 ✓'}")
    print(f"有意保留    funds: {kf or '无'}")
    print("=" * 84)
    return 0 if not (zf or zm) else 1


if __name__ == "__main__":
    sys.exit(main())
