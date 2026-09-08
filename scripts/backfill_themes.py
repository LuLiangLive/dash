# -*- coding: utf-8 -*-
"""
backfill_themes.py —— 对"核心资产"兜底基金重算持仓标签

两步走:
1. 先用更新后的关键词规则重新从名称推断(可能已能匹配到行业)
2. 仍为"核心资产"的, 抓取真实持仓用 holdings_themes 计算真实标签
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
    """判断是否为纯核心资产兜底"""
    if not themes or len(themes) != 1:
        return False
    name = themes[0].get("name", "")
    return name == "核心资产" or name.startswith("核心资产/")


def recompute_one(code: str, name: str, old_themes: list) -> tuple[list, str]:
    """重算单只基金的标签, 返回 (新标签, 来源)"""
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
                # 过滤掉全是"其他"的情况
                non_other = [t for t in new_themes if t["name"] != "其他"]
                if non_other:
                    return new_themes, "real_holdings"
    except Exception as e:
        pass

    # 步骤3: 仍然兜底, 但把"核心资产"改成更准确的"灵活配置"
    return [{"name": "灵活配置", "pct": 100.0}], "fallback_renamed"


def main():
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT code, name, themes FROM funds WHERE themes IS NOT NULL AND themes != ''"
    ).fetchall()

    core_asset_funds = []
    for r in rows:
        try:
            themes = json.loads(r["themes"])
            if is_core_asset(themes):
                core_asset_funds.append((r["code"], r["name"], themes))
        except:
            pass

    print(f"核心资产兜底基金数: {len(core_asset_funds)}")

    # 批量重算(并发8线程)
    results = {}
    stats = {"name_infer": 0, "real_holdings": 0, "fallback_renamed": 0, "error": 0}

    def work(item):
        code, name, old = item
        try:
            new, source = recompute_one(code, name, old)
            return code, name, new, source, None
        except Exception as e:
            return code, name, old, "error", str(e)

    with ThreadPoolExecutor(max_workers=8) as ex:
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
            if done % 100 == 0:
                print(f"  进度: {done}/{len(core_asset_funds)}, {stats}")
            time.sleep(0.05)  # 轻微限流

    print(f"\n重算完成: {stats}")

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

    # 统计最终兜底比例
    final_core = 0
    final_flex = 0
    total = 0
    rows_final = conn.execute("SELECT themes FROM funds WHERE themes IS NOT NULL AND themes != ''").fetchall()
    for r in rows_final:
        try:
            themes = json.loads(r["themes"])
            if isinstance(themes, list):
                for t in themes:
                    total += 1
                    name = t.get("name", "") if isinstance(t, dict) else str(t)
                    if name == "核心资产":
                        final_core += 1
                    elif name == "灵活配置":
                        final_flex += 1
        except:
            pass

    print(f"\n=== 最终统计 ===")
    print(f"总标签数: {total}")
    print(f"核心资产(剩余): {final_core} ({final_core/max(1,total)*100:.1f}%)")
    print(f"灵活配置(重命名兜底): {final_flex} ({final_flex/max(1,total)*100:.1f}%)")
    print(f"真实行业/概念: {total - final_core - final_flex} ({(total-final_core-final_flex)/max(1,total)*100:.1f}%)")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
