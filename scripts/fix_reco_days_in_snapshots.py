# -*- coding: utf-8 -*-
"""
fix_reco_days_in_snapshots.py
修复 rank_snapshots.meta 中 reco_days 为 NULL 的问题。
原因: pipeline 构建快照时 reco_days 还未计算(留 recompute_ad 重算),
      recompute_ad 更新了 funds 表但没回写快照 meta。
修复: 从 funds 表读取 reco_days/prev_reco/prev_reco_days, 更新到 rank_snapshots.meta。
"""
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db

def main():
    conn = db.get_conn()
    conn.row_factory = sqlite3.Row

    # 读取所有 rank_snapshots
    rows = conn.execute(
        "SELECT id, code, panel, sub, date, meta FROM rank_snapshots WHERE code != ''"
    ).fetchall()

    print(f"共 {len(rows)} 条快照记录")

    updated = 0
    null_before = 0
    null_after = 0

    for row in rows:
        code = row["code"]
        meta_str = row["meta"]

        if not meta_str:
            continue

        try:
            meta = json.loads(meta_str)
        except Exception:
            continue

        # 检查当前 meta 中的 reco_days
        if meta.get("reco_days") is None:
            null_before += 1

        # 从 funds 表读取最新的 reco 相关字段
        fund = conn.execute(
            "SELECT reco, reco_days, reco_score, prev_reco, prev_reco_days FROM funds WHERE code=?",
            (code,)
        ).fetchone()

        if not fund:
            continue

        changed = False

        # 更新 reco_days (如果 funds 表有值但 meta 中没有)
        if fund["reco_days"] is not None and meta.get("reco_days") is None:
            meta["reco_days"] = fund["reco_days"]
            changed = True

        # 更新 prev_reco / prev_reco_days
        if fund["prev_reco"] is not None and meta.get("prev_reco") is None:
            meta["prev_reco"] = fund["prev_reco"]
            changed = True

        if fund["prev_reco_days"] is not None and meta.get("prev_reco_days") is None:
            meta["prev_reco_days"] = fund["prev_reco_days"]
            changed = True

        # 同步 reco / reco_score (确保一致)
        if fund["reco"] is not None and meta.get("reco") != fund["reco"]:
            meta["reco"] = fund["reco"]
            changed = True

        if fund["reco_score"] is not None and meta.get("reco_score") is None:
            meta["reco_score"] = fund["reco_score"]
            changed = True

        if changed:
            new_meta = json.dumps(meta, ensure_ascii=False)
            conn.execute(
                "UPDATE rank_snapshots SET meta=? WHERE id=?",
                (new_meta, row["id"])
            )
            updated += 1

            if meta.get("reco_days") is None:
                null_after += 1

    conn.commit()

    # 统计修复后的情况
    remaining_null = conn.execute(
        "SELECT COUNT(*) FROM rank_snapshots WHERE code != '' AND meta IS NOT NULL AND json_extract(meta, '$.reco_days') IS NULL"
    ).fetchone()[0]

    print(f"修复前 meta 中 reco_days 为 NULL: {null_before} 条")
    print(f"已更新记录: {updated} 条")
    print(f"修复后 meta 中 reco_days 仍为 NULL: {remaining_null} 条")

    # 抽样验证
    print("\n=== 抽样验证(前5条) ===")
    samples = conn.execute(
        "SELECT code, panel, json_extract(meta, '$.reco') as reco, json_extract(meta, '$.reco_days') as reco_days FROM rank_snapshots WHERE code != '' LIMIT 5"
    ).fetchall()
    for s in samples:
        print(f"  {s['code']} ({s['panel']}): reco={s['reco']}, reco_days={s['reco_days']}")

    conn.close()
    print("\n修复完成!")

if __name__ == "__main__":
    main()
