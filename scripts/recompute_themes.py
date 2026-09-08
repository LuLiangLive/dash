# -*- coding: utf-8 -*-
"""
recompute_themes.py —— 重算全部基金持仓主题(不重跑网络采集)

背景: 全量库(FULL_MARKET=1)不抓真实持仓, 多数 themes 来自基金名称推断,
      单一主题显示"XX 100%"较笼统。本脚本按新逻辑重算:
        - 仅处理「单一主题且 pct>=99」的基金(笼统显示), 保留多主题/分散持仓
        - "宽基/核心资产"等大主题 → 用名称重新推断(提取具体指数名)
        - 行业单一主题 → 用 THEME_LEADERS 细分代表重仓股

用法:
  python scripts/recompute_themes.py                 # 重算
  python scripts/recompute_themes.py --restore <db>  # 先从备份库恢复 themes(回滚)
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db
from collector.sector import THEME_LEADERS, infer_themes

BIG_THEMES = {"宽基", "核心资产"}


def restore_from(backup_db: Path) -> int:
    """从备份库恢复 funds.themes 与 rank_snapshots.meta(覆盖当前库)。"""
    conn = db.get_conn()
    src = sqlite3.connect(str(backup_db))
    src.row_factory = sqlite3.Row
    rows = src.execute("SELECT code, themes FROM funds").fetchall()
    restored = 0
    for r in rows:
        cur = conn.execute("SELECT themes FROM funds WHERE code=?", (r["code"],)).fetchone()
        if cur is None or cur["themes"] != r["themes"]:
            conn.execute(
                "UPDATE funds SET themes=? WHERE code=?",
                (r["themes"], r["code"]),
            )
            restored += 1
    snaps = src.execute("SELECT id, meta FROM rank_snapshots").fetchall()
    snap_restored = 0
    for s in snaps:
        cur = conn.execute("SELECT meta FROM rank_snapshots WHERE id=?", (s["id"],)).fetchone()
        if cur is None or cur["meta"] != s["meta"]:
            conn.execute(
                "UPDATE rank_snapshots SET meta=? WHERE id=?",
                (s["meta"], s["id"]),
            )
            snap_restored += 1
    conn.commit()
    src.close()
    print(f"恢复完成: funds {restored} 只, rank_snapshots {snap_restored} 条")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--restore", type=str, default="", help="从备份库恢复 themes 后退出")
    args = ap.parse_args()
    if args.restore:
        return restore_from(Path(args.restore))

    conn = db.get_conn()
    rows = conn.execute(
        "SELECT code, name, themes FROM funds WHERE themes IS NOT NULL AND themes != '[]'"
    ).fetchall()
    updated = unchanged = skipped = 0
    for r in rows:
        old = json.loads(r["themes"])
        # 仅处理「单一主题且 pct>=99」的笼统显示;多主题/分散持仓保留原值
        if len(old) != 1 or old[0].get("pct", 0) < 99:
            skipped += 1
            continue
        nm = old[0]["name"]
        if "/" in nm:
            unchanged += 1  # 已有细分
            continue
        new_name = None
        if nm in BIG_THEMES:
            # 宽基/核心资产 → 用名称重新推断(优先提取具体指数名)
            th = infer_themes(r["name"])
            if th and th[0]["name"] != nm:
                new_name = th[0]["name"]
        else:
            leads = THEME_LEADERS.get(nm)
            if leads:
                new_name = f"{nm}/{('/'.join(leads))}"
        if new_name and new_name != nm:
            conn.execute(
                "UPDATE funds SET themes=? WHERE code=?",
                (json.dumps([{"name": new_name, "pct": 100.0}], ensure_ascii=False), r["code"]),
            )
            updated += 1
            if updated <= 15:
                print(f"  {r['code']} {r['name']}\n    旧: {old[:2]}\n    新: [{ {'name': new_name, 'pct': 100.0} }]")
        else:
            unchanged += 1
    # 同步 rank_snapshots.meta 中的 themes 快照(榜单行渲染依赖它,重新读重算后的最新值)
    rows_new = conn.execute(
        "SELECT code, themes FROM funds WHERE themes IS NOT NULL AND themes != '[]'"
    ).fetchall()
    themes_by_code = {r["code"]: json.loads(r["themes"] or "[]") for r in rows_new}
    snap = conn.execute("SELECT id, code, meta FROM rank_snapshots").fetchall()
    snap_updated = 0
    for s in snap:
        try:
            meta = json.loads(s["meta"] or "{}")
        except (TypeError, ValueError):
            continue
        new_th = themes_by_code.get(s["code"])
        if new_th is not None and meta.get("themes") != new_th:
            meta["themes"] = new_th
            conn.execute(
                "UPDATE rank_snapshots SET meta=? WHERE id=?",
                (json.dumps(meta, ensure_ascii=False), s["id"]),
            )
            snap_updated += 1
    conn.commit()
    print(f"\n完成: 细分更新 {updated} 只, 已细分/无需改 {unchanged} 只, 多主题保留 {skipped} 只, "
          f"榜单快照同步 {snap_updated} 条, 共 {len(rows)} 只")
    return 0


if __name__ == "__main__":
    sys.exit(main())
