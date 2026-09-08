# -*- coding: utf-8 -*-
"""
recompute_reco_days_all.py —— 全池基金 reco_days 重算

背景: pipeline 采集阶段 reco_days 沿用 funds 表旧值, 删库后旧值丢失;
      recompute_ad.py 只处理上榜+自选基金(约300只), 不处理全池(6000+只)。
      本脚本对全池基金执行 75 天净值回放, 重算 reco_days / prev_reco / prev_reco_days。

用法: python scripts/recompute_reco_days_all.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db
import collector.ranker as ranker

# 直接从 recompute_ad 导入已验证的辅助函数
sys.path.insert(0, str(ROOT / "scripts"))
from recompute_ad import _load_idx_series, _idx_ret60, IDX_CODES


def main():
    conn = db.get_conn()

    # 确保字段存在
    cols = [r[1] for r in conn.execute("PRAGMA table_info(funds)").fetchall()]
    for col, ddl in (("reco_days", "INTEGER"), ("prev_reco", "TEXT"), ("prev_reco_days", "INTEGER")):
        if col not in cols:
            conn.execute(f"ALTER TABLE funds ADD COLUMN {col} {ddl}")
    conn.commit()

    idx_series = _load_idx_series(conn)
    idx_map = ranker._build_idx_map(idx_series, []) if idx_series else {}
    idx60 = _idx_ret60(idx_series)
    print(f"上证指数: {len(idx_series)}天, 近60日涨幅: {idx60}%")

    # 取全池有reco信号且reco_days为NULL的基金
    rows = conn.execute(
        "SELECT code, name, reco FROM funds WHERE reco IS NOT NULL AND (reco_days IS NULL OR reco_days = 0) ORDER BY code"
    ).fetchall()
    total = len(rows)
    print(f"需重算 reco_days 的基金: {total}只")

    if total == 0:
        print("无需重算，全部基金已有 reco_days")
        return

    updated = 0
    skipped = 0
    start_time = time.time()

    for i, r in enumerate(rows):
        code = r["code"]
        name = r["name"]
        old_reco = r["reco"]

        # 取最近75天净值
        nav_rows = conn.execute(
            "SELECT date, ljjz FROM nav_history WHERE code=? ORDER BY date DESC LIMIT 75",
            (code,)
        ).fetchall()

        if len(nav_rows) < 8:
            skipped += 1
            continue

        navs = [r["ljjz"] for r in reversed(nav_rows)]
        dates = [r["date"] for r in reversed(nav_rows)]

        # 75天回放计算reco_days
        _ddown, _ = ranker._build_ddays(idx_map, dates)
        _prev_lvl, _cur_days = None, 0
        _prev_seg_lvl, _prev_seg_days = None, 0
        _window = 75
        _start = max(0, len(navs) - _window)

        for _i in range(_start, len(navs)):
            _tail = navs[max(0, _i + 1 - _window):_i + 1]
            _m = ranker.calc_metrics(_tail, idx_map, _ddown)
            _m["d1"] = ranker._ret(_tail, 1)
            _m["d2"] = ranker._ret(_tail, 2)
            _m["rzf"] = _m["d1"]
            _m["m3"] = ranker._ret(_tail, 63)
            _m["m6"] = ranker._ret(_tail, 126)
            _m["y1"] = ranker._ret(_tail, 252)
            _m["dn7"] = _m.get("dn7", 0)
            # v2.11.2: calc_metrics 已输出真实近20日高 dd20, 不再用 dd_from_hi 覆盖(仅兜底)
            if _m.get("dd20") is None:
                _m["dd20"] = _m.get("dd_from_hi")
            _, _lvl_now, _, _ = ranker.compute_reco_v32(
                _m, _tail, prev_reco=_prev_lvl, prev_reco_days=_cur_days, idx60=idx60)
            if _lvl_now != _prev_lvl and _prev_lvl is not None:
                _prev_seg_lvl, _prev_seg_days = _prev_lvl, _cur_days
                _cur_days = 1
            else:
                _cur_days += 1
            _prev_lvl = _lvl_now

        _days = _cur_days
        _prev_reco = _prev_seg_lvl
        _prev_days = _prev_seg_days

        # 更新funds表
        conn.execute(
            "UPDATE funds SET reco_days=?, prev_reco=?, prev_reco_days=? WHERE code=?",
            (_days, _prev_reco, _prev_days, code)
        )
        updated += 1

        if (i + 1) % 500 == 0:
            conn.commit()
            elapsed = time.time() - start_time
            rate = (i + 1) / elapsed if elapsed > 0 else 0
            eta = (total - i - 1) / rate if rate > 0 else 0
            print(f"  进度: {i+1}/{total} ({(i+1)/total*100:.1f}%), 已更新{updated}, 跳过{skipped}, 速度{rate:.0f}只/秒, 预计剩余{eta:.0f}秒")

    conn.commit()
    elapsed = time.time() - start_time
    print(f"\n完成! 共更新 {updated} 只基金, 跳过 {skipped} 只(净值不足), 耗时 {elapsed:.1f}秒")

    # 验证结果
    null_count = conn.execute(
        "SELECT COUNT(*) FROM funds WHERE reco IS NOT NULL AND (reco_days IS NULL OR reco_days = 0)"
    ).fetchone()[0]
    has_days = conn.execute(
        "SELECT COUNT(*) FROM funds WHERE reco IS NOT NULL AND reco_days > 0"
    ).fetchone()[0]
    print(f"验证: 有reco且reco_days>0: {has_days}只, 仍为NULL/0: {null_count}只")


if __name__ == "__main__":
    main()
