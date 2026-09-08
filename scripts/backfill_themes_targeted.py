# -*- coding: utf-8 -*-
"""
backfill_themes_targeted.py —— 只对用户可见的基金重算持仓标签

范围: 当前榜单(rank_snapshots) + 自选 + 持仓
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db
from modules.market.sector import infer_themes
from modules.fund.holdings import fetch_holdings_w_full, holdings_themes


def is_core_asset(themes: list) -> bool:
    if not themes or len(themes) != 1:
        return False
    name = themes[0].get("name", "")
    return name == "核心资产" or name.startswith("核心资产/")


def recompute_one(code: str, name: str, old_themes: list) -> tuple[list, str]:
    # 步骤1: 用更新后的关键词规则重新从名称推断
    new_by_name = infer_themes(name)
    if not is_core_asset(new_by_name):
        return new_by_name, "name_infer"

    # 步骤2: 抓取真实持仓
    try:
        holdings = fetch_holdings_w_full(code)
        if holdings:
            themes_raw = holdings_themes(holdings, limit=3)
            if themes_raw:
                new_themes = [{"name": n, "pct": round(p, 1)} for n, p in themes_raw]
                non_other = [t for t in new_themes if t["name"] != "其他"]
                if non_other:
                    return new_themes, "real_holdings"
    except Exception:
        pass

    # 步骤3: 兜底改名为"灵活配置"
    return [{"name": "灵活配置", "pct": 100.0}], "fallback_renamed"


def main():
    conn = db.get_conn()

    # 收集目标基金: 当前榜单(rank_snapshots) + 自选 + 持仓
    target_codes = set()

    # 当前榜单: 从 rank_snapshots 收集
    try:
        snap_rows = conn.execute("SELECT DISTINCT code FROM rank_snapshots").fetchall()
        for r in snap_rows:
            target_codes.add(r["code"])
        print(f"榜单基金数: {len(snap_rows)}")
    except Exception as e:
        print(f"读取榜单失败: {e}")

    # 自选
    try:
        watch_rows = conn.execute("SELECT code FROM watchlist").fetchall()
        for r in watch_rows:
            target_codes.add(r["code"])
        print(f"自选基金数: {len(watch_rows)}")
    except:
        pass

    # 持仓
    try:
        port_rows = conn.execute("SELECT code FROM portfolio").fetchall()
        for r in port_rows:
            target_codes.add(r["code"])
        print(f"持仓基金数: {len(port_rows)}")
    except:
        pass

    print(f"目标基金总数: {len(target_codes)}")

    # 筛选出其中是"核心资产"兜底的
    core_asset_funds = []
    for code in target_codes:
        row = conn.execute("SELECT code, name, themes FROM funds WHERE code=?", (code,)).fetchone()
        if not row:
            continue
        try:
            themes = json.loads(row["themes"] or "[]")
            if is_core_asset(themes):
                core_asset_funds.append((row["code"], row["name"], themes))
        except:
            pass

    print(f"其中核心资产兜底: {len(core_asset_funds)} 只")

    # 批量重算(并发4线程)
    results = {}
    stats = {"name_infer": 0, "real_holdings": 0, "fallback_renamed": 0, "error": 0}

    def work(item):
        code, name, old = item
        try:
            new, source = recompute_one(code, name, old)
            return code, name, new, source, None
        except Exception as e:
            return code, name, old, "error", str(e)

    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = {ex.submit(work, item): item for item in core_asset_funds}
        done = 0
        for fut in as_completed(futures):
            code, name, new, source, err = fut.result()
            done += 1
            if err:
                stats["error"] += 1
            else:
                stats[source] += 1
                results[code] = new
            if done % 20 == 0:
                print(f"  进度: {done}/{len(core_asset_funds)}, {stats}")
            time.sleep(0.1)

    print(f"重算完成: {stats}")

    # 更新数据库
    updated = 0
    for code, new_themes in results.items():
        conn.execute(
            "UPDATE funds SET themes=? WHERE code=?",
            (json.dumps(new_themes, ensure_ascii=False), code),
        )
        updated += 1
    conn.commit()
    print(f"数据库更新: {updated} 只")

    # 同步 rank_snapshots.meta
    rows_new = conn.execute(
        "SELECT code, themes FROM funds WHERE themes IS NOT NULL AND themes != ''"
    ).fetchall()
    themes_by_code = {r["code"]: json.loads(r["themes"] or "[]") for r in rows_new}
    snap = conn.execute("SELECT id, code, meta FROM rank_snapshots").fetchall()
    snap_updated = 0
    for s in snap:
        try:
            meta = json.loads(s["meta"] or "{}")
        except:
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
    print(f"榜单快照同步: {snap_updated} 条")

    # 显示更新示例
    print("\n=== 更新示例 ===")
    for code, new_themes in list(results.items())[:15]:
        row = conn.execute("SELECT name FROM funds WHERE code=?", (code,)).fetchone()
        labels = [f"{t['name']}{t['pct']}%" for t in new_themes]
        print(f"  {code} {row['name'][:20]}: {', '.join(labels)}")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
