# -*- coding: utf-8 -*-
"""
recompute_ad.py —— 抗跌r1算法(6项不等权)全库重算

适用:改动 ranker 抗跌分算法后重算 funds.ad_score/earn_score/score。
与 recompute_ranks 共用同一评分模块 ranker.compute_scores_v2,保证口径一致。

用法: python scripts/recompute_ad.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db
import collector.ranker as ranker
from collector.recommendation import kinetic_state

IDX_CODES = ("sh000001", "sz399006", "sh000688")


def refresh_tscores_for_today(conn, today: str) -> int:
    """v0.42.5: 按面板类型重算今日 rank_snapshots 的 tscore (面板特定综合分).

    使用 ranker.panel_score (与 build_ranks 同一套公式), 保证:
      - 稳涨/强趋势/ETF: tscore = composite_score(趋势40+支撑30+稳健20+平衡10)
      - 自选算法榜单:    tscore = watch_score_light
      - 抗跌榜单:        tscore = ad*0.5 + earn*0.5 (= funds.score)
      - 开仓榜单:        tscore = reco_score(六维)

    用于 build_ranks 当日没跑 (网络中断/覆盖不足) 时的 tscore refresh 兜底,
    保证页面加载/一键更新/榜单重排三条链路 tscore 一致.

    返回 refresh 计数."""
    import sqlite3 as _sqlite3
    import collector.ranker as ranker
    conn.row_factory = _sqlite3.Row   # 局部设置 row_factory (与 db.get_conn 对齐)

    # 取今日所有 snapshots
    snaps = conn.execute(
        "SELECT id, panel, sub, code, meta FROM rank_snapshots WHERE date=? AND code!=''",
        (today,),
    ).fetchall()
    if not snaps:
        return 0

    # 重构 fund 池 (按 panel+sub 计算需要的 metrics/navs/scale/ftype/sec)
    codes = list({s["code"] for s in snaps})
    marks = ",".join("?" * len(codes))
    rows = conn.execute(
        f"SELECT code, scale, ftype, sec, ad_score, earn_score, score, "
        f"d3, d5, d7, d10, m1, m3, m6, y1, mdd, dn7, dd20, vol, down_vol, is_etf "
        f"FROM funds WHERE code IN ({marks})", codes
    ).fetchall()
    fund_meta = {}
    for r in rows:
        d = dict(r)
        fund_meta[d["code"]] = d

    # 重构 sec_m5_map (用于开仓榜单 reco_score, 与 build_ranks 同口径:
    # 板块内指数类 ETF 基金 d5 均值)
    idx_items = conn.execute(
        "SELECT date, ljjz FROM nav_history WHERE code='sh000001' ORDER BY date"
    ).fetchall()
    sec_d5 = {}
    sec_set = {dict(r)["sec"] for r in rows if dict(r)["sec"] and dict(r)["sec"] != "其他"}
    for r in rows:
        d = dict(r)
        if not d.get("is_etf") or d.get("sec") not in sec_set:
            continue
        nav_rows = conn.execute(
            "SELECT date, ljjz FROM nav_history WHERE code=? ORDER BY date DESC LIMIT 6",
            (r["code"],),
        ).fetchall()
        if len(nav_rows) < 2:
            continue
        navs = [x["ljjz"] for x in reversed(list(nav_rows))]
        if len(navs) < 2:
            continue
        try:
            d5 = (navs[-1] / navs[-6] - 1) * 100 if len(navs) >= 6 and navs[-6] else None
        except Exception:
            d5 = None
        if d5 is not None:
            sec = r["sec"]
            sec_d5.setdefault(sec, []).append(d5)
    sec_m5_map = {s: round(sum(v) / len(v), 2) for s, v in sec_d5.items() if v}

    # 加载 nav 序列 (自选算法榜单 + 开仓榜单 需要 navs)
    nav_data = {}    # code -> (navs, dates)
    for c in codes:
        nav_rows = conn.execute(
            "SELECT date, dwjz FROM nav_history WHERE code=? ORDER BY date DESC LIMIT 60",
            (c,),
        ).fetchall()
        if len(nav_rows) < 8:
            continue
        navs = [x["dwjz"] for x in reversed(list(nav_rows))]  # v0.86.0: 用单位净值
        dates = [x["date"] for x in reversed(list(nav_rows))]
        nav_data[c] = (navs, dates)

    refreshed = 0
    for snap in snaps:
        panel, sub, code = snap["panel"], snap["sub"], snap["code"]
        f = fund_meta.get(code)
        if not f:
            continue
        # v2.11.4 Q4/Q5: 自选/质量/超跌筑底榜 tscore 由阶段2 各自算法产出
        # (watch_score_light / qscore*100 / 独立评分), 阶段4 不重写;
        # 只有日榜/抗跌/攻防榜的 tscore 走 V3 综合分口径
        # 攻防榜 4 个 sub(高弹性/攻守兼备/强抗跌/反弹先锋) 不在此白名单,
        # 由 Q3 决定统一为 dual, panel_score → funds.score 与 dual 语义近似
        if sub in ("自选", "质量", "超跌筑底", "自选算法榜单"):
            continue
        navs, dates = nav_data.get(code, ([], []))
        if navs:
            idx_map = ranker._build_idx_map(
                {"sh000001": {"items": [{"date": r["date"], "close": r["ljjz"]} for r in idx_items]}},
                dates,
            )
            try:
                m = ranker.calc_metrics(navs, idx_map, [])
                m["d1"] = ranker._ret(navs, 1)
                m["d2"] = ranker._ret(navs, 2)
                m["rzf"] = m.get("d1")
                m["m3"] = ranker._ret(navs, 63)
                m["dn7"] = f.get("dn7") or 0
                m["dd20"] = f.get("dd20")
            except Exception:
                m = {}
        else:
            m = {}
        f2 = {
            "metrics": m,
            "scale": f.get("scale"),
            "ftype": f.get("ftype") or "",
            "sec": f.get("sec") or "其他",
            "navs": navs,
            "score": f.get("score"),       # 抗跌榜单 tscore = funds.score
        }
        new_tscore = ranker.panel_score(sub, f2, sec_m5_map)
        if new_tscore is None:
            continue
        try:
            meta = json.loads(snap["meta"] or "{}")
        except (TypeError, ValueError):
            continue
        old = meta.get("tscore")
        if old is None or abs((old or 0) - new_tscore) > 0.5:
            meta["tscore"] = new_tscore
            conn.execute(
                "UPDATE rank_snapshots SET meta=? WHERE id=?",
                (json.dumps(meta, ensure_ascii=False), snap["id"]),
            )
            refreshed += 1
    conn.commit()
    return refreshed


def refresh_day_panel_for_today(conn, today: str) -> int:
    """v0.46.0+: 增量模式下 day panel 兜底刷新。

    重算今日 rank_snapshots 中 panel='day' 的 d1/d2/d3/d7, 并按新值重排 rank 字段。
    解决 _complete=False 时 build_ranks 被跳过 → day panel meta 永远不刷新的问题
    (典型场景: 一键更新 6 只补抓, 但上榜基金 018751 永远拿不到当日 meta.d1 重算)。

    Args:
        conn: sqlite3.Connection (主调方传入, 与 db.get_conn 同源)
        today: 榜单日期 YYYY-MM-DD

    Returns: 写入的快照行数
    """
    period_map = {"当日": ("d1", 1), "两日": ("d2", 2), "三日": ("d3", 3), "七日": ("d7", 7)}

    # 1) 取 day panel 所有快照
    snaps = conn.execute(
        "SELECT id, sub, code, meta FROM rank_snapshots WHERE date=? AND panel='day' AND code!=''",
        (today,)).fetchall()
    if not snaps:
        return 0

    # 2) 按 code 批量加载 nav_history, 重算 d1/d2/d3/d7
    code_metrics: dict[str, dict] = {}
    codes = list({s["code"] for s in snaps})
    for c in codes:
        nav_rows = conn.execute(
            "SELECT date, dwjz FROM nav_history WHERE code=? ORDER BY date DESC LIMIT 30",
            (c,)).fetchall()
        if len(nav_rows) < 8:
            continue
        navs = [r["dwjz"] for r in reversed(nav_rows)]  # v0.86.0: 用单位净值
        m: dict[str, float | None] = {}
        for sub_name, (key, n) in period_map.items():
            if len(navs) > n and navs[-1 - n] and navs[-1]:
                m[key] = round((navs[-1] / navs[-1 - n] - 1) * 100, 4)
            else:
                m[key] = None
        code_metrics[c] = m

    # 3) 写回每条 snap 的 meta.d1/d2/d3/d7
    refreshed = 0
    for snap in snaps:
        m = code_metrics.get(snap["code"])
        if not m:
            continue
        try:
            meta = json.loads(snap["meta"] or "{}")
        except (TypeError, ValueError):
            meta = {}
        for sub_name, (key, _) in period_map.items():
            if m.get(key) is not None:
                meta[key] = m[key]
        conn.execute(
            "UPDATE rank_snapshots SET meta=? WHERE id=?",
            (json.dumps(meta, ensure_ascii=False), snap["id"]))
        refreshed += 1

    # 4) 按新 d1/d2/d3/d7 重排 rank 字段(每 sub 独立排序, None 排最后)
    # rank_snapshots 表有 UNIQUE(date,panel,sub,rank) 约束, 直接 UPDATE 可能冲突.
    # 分两步: 先把全部 rank 加 10000 偏移解除约束, 再 UPDATE 到正确新 rank.
    # v0.46.0+: 同时同步 meta.rank 字段(API 端 /api/ranks 返回的是 meta.rank)
    for sub_name, (key, _) in period_map.items():
        rows = conn.execute(
            "SELECT id, meta FROM rank_snapshots WHERE date=? AND panel='day' AND sub=? AND code!=''",
            (today, sub_name)).fetchall()
        ranked: list[tuple[float, int]] = []
        for r in rows:
            try:
                val = json.loads(r["meta"] or "{}").get(key)
            except Exception:
                val = None
            ranked.append((-(val if val is not None else -1e9), r["id"]))
        ranked.sort()  # 关键: 按 -(d1) 升序 → 实际 d1 降序排列
        # 第一步: 全部偏移 +10000 (每 sub 独立, 不会超过 UNIQUE 范围)
        for _, snap_id in ranked:
            conn.execute(
                "UPDATE rank_snapshots SET rank=rank+10000 WHERE id=?",
                (snap_id,))
        # 第二步: 设新 rank(此时所有旧 rank 都 >= 10000, 无 UNIQUE 冲突)
        #          同时把 meta.rank 也同步, 因为 API 端读 meta.rank
        for new_rank, (_, snap_id) in enumerate(ranked, 1):
            # 先把 meta 读出来(避免覆盖其他字段)
            cur = conn.execute("SELECT meta FROM rank_snapshots WHERE id=?", (snap_id,))
            row = cur.fetchone()
            try:
                meta = json.loads(row["meta"] or "{}") if row and row["meta"] else {}
            except (TypeError, ValueError):
                meta = {}
            meta["rank"] = new_rank
            conn.execute(
                "UPDATE rank_snapshots SET rank=?, meta=? WHERE id=?",
                (new_rank, json.dumps(meta, ensure_ascii=False), snap_id))

    conn.commit()
    return refreshed


def _load_idx_series(conn) -> dict:
    out = {}
    for c in IDX_CODES:
        rows = conn.execute(
            "SELECT date, ljjz FROM nav_history WHERE code=? ORDER BY date", (c,)
        ).fetchall()
        out[c] = {"name": c, "items": [{"date": r["date"], "close": r["ljjz"]} for r in rows]}
    return out


def _load_bench_items(conn) -> list:
    """中证800(sh000906)净值升序 [(date, ljjz)]。超额收益基准(统一中800)。"""
    rows = conn.execute(
        "SELECT date, ljjz FROM nav_history WHERE code='sh000906' ORDER BY date"
    ).fetchall()
    return [(r["date"], r["ljjz"]) for r in rows]


def _idx_ret60(idx_series):
    """上证指数(sh000001)近60日涨幅(%)。样本不足返回 None。"""
    items = (idx_series.get("sh000001") or {}).get("items", [])
    if len(items) < 2 or items[-60]["close"] is None:
        return None
    base = items[-60]["close"]
    if not base:
        return None
    return (items[-1]["close"] / base - 1) * 100


def main(progress_cb=None, cancel_flag=None, skip_panel_overwrite: bool = False) -> int:
    """v2.10.0: 重算上榜+自选基金的 ad/earn/score 精确分数, 并同步回榜单快照。
    reco/reco_days 已由 pipeline 阶段1全池75天精确回放算好, 此处不再计算。
    progress_cb: 可选 fn(stage, done, total, code, msg); cancel_flag: threading.Event。
    v0.46.0: 末尾追加 task_log 让 _HTML_CACHE 自动失效 (cache_key 含 latest_task_id, 会变)

    v0.47.0+: skip_panel_overwrite=True 时跳过以下 panel 写入(留给 rank_full 全量接管):
      - refresh_tscores_for_today (重写所有 tscore)
      - refresh_day_panel_for_today (重写 day meta + 重排)
      - 抗跌/开仓/综合推荐 顺序重排
      - meta 表回填
    仅保留: funds 表 ad_score/earn_score/score/yindie 精确重算 + 同步 themes/stocks.
    """
    _v46_log_id = None
    _v46_started = None
    try:
        import time as _t
        _v46_started = _t.time()
        _v46_log_id = db.log_start("rebalance")
    except Exception:
        pass
    def _prog(stage, done, total, code="", msg=""):
        if progress_cb:
            try:
                progress_cb(stage, done, total, code, msg)
            except Exception:
                pass
    conn = db.get_conn()
    # 确保 v0.37 新字段存在
    _cols = [r[1] for r in conn.execute("PRAGMA table_info(funds)").fetchall()]
    if "reco" not in _cols:
        conn.execute("ALTER TABLE funds ADD COLUMN reco TEXT")
    if "reco_score" not in _cols:
        conn.execute("ALTER TABLE funds ADD COLUMN reco_score INTEGER")
    conn.commit()
    # 推荐买入建议 · 历史操作标签表(每日重算快照, 供详情弹窗展示)
    conn.execute("""CREATE TABLE IF NOT EXISTS fund_reco_history (
        code TEXT, date TEXT, reco TEXT, days INTEGER, note TEXT,
        PRIMARY KEY(code, date))""")
    conn.commit()
    idx_series = _load_idx_series(conn)
    idx_map = ranker._build_idx_map(idx_series, [])
    bench_items = _load_bench_items(conn)   # 中证800,超额收益基准
    idx60 = _idx_ret60(idx_series)          # V3.2 大盘环境维度输入
    # v0.42.5: 取今日日期 (refresh_tscores_for_today 需要). funds 表用 nav_date, rank_snapshots 用 date
    _max_date_sql = (
        "SELECT MAX(date) FROM ("
        "  SELECT MAX(nav_date) AS date FROM funds"
        "  UNION ALL"
        "  SELECT MAX(date) AS date FROM rank_snapshots"
        ")"
    )
    _today_dt = conn.execute(_max_date_sql).fetchone()[0]
    today = _today_dt or __import__("datetime").date.today().isoformat()
    print(f"今日日期: {today}, 上证近60日涨幅: {idx60}%")
    # V3.2: 推荐买入建议仅对「网页显示」的基金生效(上榜 + 自选), 不处理全池
    _disp = set(x[0] for x in conn.execute(
        "SELECT DISTINCT code FROM rank_snapshots WHERE code!='' AND panel!='rot'"))
    for x in conn.execute("SELECT code FROM watchlist"):
        _disp.add(x[0])
    rows = [r for r in conn.execute("SELECT * FROM funds ORDER BY code") if r[0] in _disp]
    print(f"上榜+自选基金: {len(rows)} 只(全池 {conn.execute('SELECT COUNT(*) FROM funds').fetchone()[0]})")


    funds_meta = []
    for r in rows:
        if cancel_flag is not None and cancel_flag.is_set():
            print("已收到停止信号, 中止信号重算")
            break
        d = dict(r)
        code = d["code"]
        _prog("reco", len(funds_meta), len(rows), code, "重算分数中")
        # v2.11.4 Q1: 统一 dwjz 口径(与阶段1/2/4a 一致), 窗口 75→260 天与阶段2 _fetch_navs 对齐;
        # bench_items(中证800)仍用 ljjz 因指数行 dwjz 为 NULL
        nav_hist = [{"date": x["date"], "dwjz": x["dwjz"]} for x in conn.execute(
            "SELECT date, dwjz FROM nav_history WHERE code=? AND dwjz IS NOT NULL "
            "ORDER BY date DESC LIMIT 260", (code,)
        ).fetchall()]
        nav_hist.reverse()
        if len(nav_hist) < 8:
            # v2.11.1: 数据不足基金动能兜底「走弱」(4档规范), 清掉旧5档残留
            if d.get("ms") not in (None, "强劲", "维持", "衰减", "走弱"):
                conn.execute("UPDATE funds SET ms='走弱' WHERE code=?", (code,))
            continue
        navs = [x["dwjz"] for x in nav_hist]
        dates = [x["date"] for x in nav_hist]
        m = ranker.calc_metrics(navs, idx_map, [])
        idx_map_f = ranker._build_idx_map(idx_series, dates) if idx_series else {}
        ddown, rise = ranker._build_ddays(idx_map_f, dates)
        resist = ranker.anti_resist(code, navs, dates, idx_map_f, ddown)
        # v0.37 新指标: 超额收益(中800)/大涨跟随弹性/7日环比 → 动能5档 + 推荐打分
        excess = ranker.calc_excess_rets(navs, dates, bench_items)
        follow = ranker.big_rise_follow(navs, dates, idx_map_f)
        prev7 = ranker._ret_skipna(navs[:-7], 7) if len(navs) > 14 else None
        momentum = kinetic_state(m)
        # v2.9.44: reco/reco_score/reco_days 在此处计算（pipeline已移除全池reco计算）
        # 对上榜+自选基金做75天精确回放, 与弹窗 _reco_history 同口径
        m["excess"] = excess
        funds_meta.append({
            "code": code, "name": d["name"], "ftype": d.get("ftype") or "",
            "is_etf": 1 if d.get("is_etf") else 0,
            "metrics": m, "navs": navs, "dates": dates,
            "resist": resist, "rise": rise,
            "momentum": momentum,
            "idx_map_f": idx_map_f,
            "old_reco": d.get("reco"),
            "old_reco_days": d.get("reco_days"),
            "prev_reco": d.get("prev_reco"),
            "prev_reco_days": d.get("prev_reco_days"),
        })
    print(f"有效基金: {len(funds_meta)}")

    earn_map, ad_map, yindie = ranker.compute_scores_v2(funds_meta)
    upd = 0
    # v2.9.44: reco_days/prev_reco 在此处计算（pipeline已移除全池reco计算）
    # 保留 ALTER TABLE 兜底(确保旧库字段存在).
    _rd_cols = [r[1] for r in conn.execute("PRAGMA table_info(funds)").fetchall()]
    for col, ddl in (("reco_days", "INTEGER"), ("prev_reco", "TEXT"), ("prev_reco_days", "INTEGER")):
        if col not in _rd_cols:
            conn.execute(f"ALTER TABLE funds ADD COLUMN {col} {ddl}")
    conn.commit()

    for fm in funds_meta:
        c = fm["code"]
        _e = earn_map.get(c, (50, None))
        earn = _e[0] if isinstance(_e, (tuple, list)) else _e
        ad = ad_map.get(c, 50)
        # v2.11.2: 综合分统一为 V3 口径 = 收益42.5% + 抗跌42.5% + 卡玛15%（单只/无池内百分位时
        # 卡玛缺省中位50），等价于 collector.scoring_v3.compute_dual_score(earn, ad, 50)，
        # 与 rank_full._anti_score_simple / fund_metrics 同口径。旧实现 earn*0.5+ad*0.5 已废弃。
        score = round(earn * 0.425 + ad * 0.425 + 50 * 0.15)
        flag = (yindie.get(c) or (None, None))[0]
        mm = fm["metrics"]
        _navs = fm.get("navs") or []
        # v2.9.44: reco/reco_days/prev_reco/prev_reco_days/reco_score 在此处计算
        # (pipeline已移除全池reco计算, reco唯一计算点=阶段4 recompute_ad)
        _reco_val = None
        _reco_score_val = None
        _reco_days_val = None
        _prev_reco_val = fm.get("prev_reco")
        _prev_reco_days_val = fm.get("prev_reco_days")
        try:
            if _navs and len(_navs) >= 8:
                _old_reco = fm.get("old_reco")
                _old_days = fm.get("old_reco_days")
                _sc, _rlv, _rmode, _rnote = ranker.compute_reco_v32(
                    mm, _navs,
                    prev_reco=fm.get("prev_reco"),
                    prev_reco_days=fm.get("prev_reco_days"),
                    idx60=idx60,
                )
                _reco_val = _rlv
                _reco_score_val = _sc
                # 75天精确回放 reco_days
                _dates_r = fm.get("dates") or []
                _idx_map_r = fm.get("idx_map_f") or {}
                _ddown_r, _ = ranker._build_ddays(_idx_map_r, _dates_r)
                _prev_lvl_r, _cur_days_r = None, 0
                _prev_seg_lvl_r, _prev_seg_days_r = None, 0
                _window_r = 75
                _start_r = max(0, len(_navs) - _window_r)
                for _i_r in range(_start_r, len(_navs)):
                    _tail_r = _navs[max(0, _i_r + 1 - _window_r):_i_r + 1]
                    _m_r = ranker.calc_metrics(_tail_r, _idx_map_r, _ddown_r)
                    _m_r["d1"] = ranker._ret(_tail_r, 1)
                    _m_r["d2"] = ranker._ret(_tail_r, 2)
                    _m_r["rzf"] = _m_r["d1"]
                    _m_r["m3"] = ranker._ret(_tail_r, 63)
                    _m_r["m6"] = ranker._ret(_tail_r, 126)
                    _m_r["y1"] = ranker._ret(_tail_r, 252)
                    _m_r["dn7"] = _m_r.get("dn7", 0)
                    # v2.11.2: calc_metrics 已输出真实近20日高 dd20, 回放不再覆盖为 dd_from_hi
                    if _m_r.get("dd20") is None:
                        _m_r["dd20"] = _m_r.get("dd_from_hi")
                    _, _lvl_now_r, _, _ = ranker.compute_reco_v32(
                        _m_r, _tail_r, prev_reco=_prev_lvl_r, prev_reco_days=_cur_days_r, idx60=idx60)
                    if _lvl_now_r != _prev_lvl_r and _prev_lvl_r is not None:
                        _prev_seg_lvl_r, _prev_seg_days_r = _prev_lvl_r, _cur_days_r
                        _cur_days_r = 1
                    else:
                        _cur_days_r += 1
                    _prev_lvl_r = _lvl_now_r
                _reco_days_val = _cur_days_r
                _prev_reco_val, _prev_reco_days_val = _prev_seg_lvl_r, _prev_seg_days_r
                # 回放窗口内无变更段: 沿用旧值兜底
                if _prev_reco_val is None and _old_reco is not None:
                    if _old_reco and _old_reco != _rlv:
                        _prev_reco_val, _prev_reco_days_val = _old_reco, _old_days or 1
                    elif _old_reco == _rlv:
                        _prev_reco_val, _prev_reco_days_val = fm.get("prev_reco"), fm.get("prev_reco_days")
                if _prev_reco_val is None and _old_reco is not None and _old_reco and _old_reco != _rlv:
                    _prev_reco_val, _prev_reco_days_val = _old_reco, _old_days or 1
        except Exception as _reco_e:
            print(f"  [reco计算失败] {c}: {_reco_e}")
        conn.execute(
            # v0.92.0: m1 / nav 改用 COALESCE —— mm.get("m1") 是从净值序列算的,
            # 净值历史不足时(次新基金)为 None, 直接写会抹掉 pingzhongdata 取到的
            # 权威区间收益。实测一键更新后 funds.m1 从 99.8% 掉到 3.7%。
            "UPDATE funds SET ad_score=?, earn_score=?, score=?, yindie=?, "
            "mdd=?, mdd_days=?, mdd_status=?, down_vol=?, "
            "d3=?, d5=?, d7=?, d10=?, dd20=?, m1=COALESCE(?, m1), nav=COALESCE(?, nav), "
            "ms=?, reco=?, reco_score=?, reco_days=?, prev_reco=?, prev_reco_days=? "
            "WHERE code=?",
            (ad, earn, score, flag,
             mm.get("mdd"), mm.get("mdd_days"), mm.get("mdd_status"),
             mm.get("down_vol"),
             mm.get("d3"), mm.get("d5"), mm.get("d7"), mm.get("d10"), mm.get("dd20"),
             mm.get("m1"), _navs[-1] if _navs else None,
             fm["momentum"],
             _reco_val, _reco_score_val, _reco_days_val, _prev_reco_val, _prev_reco_days_val,
             c),
        )
        upd += 1
    conn.commit()
    print(f"已更新 {upd} 只基金分数")

    # 同步 rank_snapshots.meta (v0.42.5: 修正 tscore 误覆盖 bug)
    # tscore 是面板特定综合分, 不同面板不同算法:
    #   - 抗跌榜单: tscore = ad*0.5 + earn*0.5 = funds.score
    #   - 稳涨/强趋势/ETF: tscore = composite_score(趋势40+支撑30+稳健20+平衡10)
    #   - 自选算法榜单: tscore = watch_score_light(中长期35+近期25+回撤15+波动16+夏普9)
    #   - 开仓榜单: tscore = reco_score(六维推荐)
    #   - 综合推荐: tscore = reco_score
    # 因此 sync 阶段只能更新"通用"字段(earn_score/ad_score/score/ms),
    # v2.9.65修复: reco/reco_score/reco_days/prev_reco/prev_reco_days 加入同步（此前漏掉导致前端信号天数不显示）;
    # 不能触碰 tscore (面板特定综合分, 由 build_ranks 写入), 否则稳涨/强趋势/自选/开仓
    # 面板的 tscore 会被错误覆盖为 funds.score.
    snap_updated = 0
    for s in conn.execute("SELECT id, code, meta FROM rank_snapshots").fetchall():
        try:
            meta = json.loads(s["meta"] or "{}")
        except (TypeError, ValueError):
            continue
        f = conn.execute(
            "SELECT earn_score, ad_score, score, ms, reco, reco_score, reco_days, prev_reco, prev_reco_days FROM funds WHERE code=?",
            (s["code"],),
        ).fetchone()
        if f is None:
            continue
        changed = False
        for k in ("earn_score", "ad_score", "score", "ms", "reco", "reco_score", "reco_days", "prev_reco", "prev_reco_days"):
            if meta.get(k) != f[k]:
                meta[k] = f[k]
                changed = True
        # tscore 不在通用 sync 范围, 由 refresh_tscores_for_today 阶段(见下方)重算
        if changed:
            conn.execute(
                "UPDATE rank_snapshots SET meta=? WHERE id=?",
                (json.dumps(meta, ensure_ascii=False), s["id"]),
            )
            snap_updated += 1
    conn.commit()
    print(f"meta 通用字段同步 {snap_updated} 条 (tscore 已迁出通用 sync)")

    # v0.42.5: tscore 重算阶段 —— 按 panel+sub 调用 ranker 公共函数,
    # 保证 build_ranks 没跑或跑晚了时, 面板特定综合分仍是最新.
    if skip_panel_overwrite:
        # v0.51.1 修复: skip_panel_overwrite=True 时仍需同步 tscore + 重排 score/reco 依赖面板.
        # 此前 rank_full 已写入 tscore=全池score, 但本函数上方同步了
        # rank_snapshots.meta.score 为小池 recompute 值, 导致 tscore 与 score 不一致、
        # 排名顺序与最新 score 脱节. 这里同步 tscore + 重排, 保证与最新 score 对齐.
        print("[recompute_ad] skip_panel_overwrite=True: 跳过 day panel, 仅同步 tscore + 重排 score/reco 面板")
        refreshed = refresh_tscores_for_today(conn, today)
        refreshed_day = 0
    else:
        refreshed = refresh_tscores_for_today(conn, today)
        # v0.46.0+: 增量模式 day panel d1/d2/d3/d7 兜底刷新
        refreshed_day = refresh_day_panel_for_today(conn, today)
        print(f"tscore 重算 {refreshed} 条 (按面板类型用 ranker._panel_score)")

    # v0.46.0: 重排三个依赖 funds.score/reco 的面板 (抗跌/开仓/综合推荐).
    # v0.51.1: 无论 skip_panel_overwrite 是否为 True 都执行, 保证排名与最新 score/reco 一致.
    # 修复一键更新 build_ranks 跑在 recompute_ad 之前, 导致 rank 顺序与最新 score/reco 脱节
    # (开仓榜单残留「回避」组排在前面, 抗跌/综合推荐 tscore 与 rank 不一致).
    # 仅重排这三个面板: 其他面板 (稳涨/强趋势/ETF/自选算法/day) 排序键是
    # composite_score/watch_score_light/d1..d7 — 由 pipeline 阶段 6 用最新 metrics 排好,
    # 不依赖 funds.score/reco, 顺序天然正确, 此处不动避免误改面板特定算法排序.
    rank_realigned = 0
    try:
            _prog("rank", 0, 3, "", "重排 score/reco 依赖面板")
            # v2.11.4 B2: 旧名 (抗跌榜单/开仓榜单/综合推荐) 全在 rank_config.DEPRECATED_RANKS,
            # 0 命中导致重排空转; 当前实际存在的需重排榜只有 "抗跌"
            _RE_RANK_SUBS = ("抗跌",)
            # 取面板当前 snapshot 的 (panel, sub, code, name, meta)
            _marks = ",".join("?" * len(_RE_RANK_SUBS))
            _rows = conn.execute(
                f"SELECT id, panel, sub, rank, code, name, meta FROM rank_snapshots "
                f"WHERE date=? AND panel='reco' AND sub IN ({_marks}) AND code!=''",
                (today, *_RE_RANK_SUBS),
            ).fetchall()
            # 按 sub 分组, 每组保留与 build_ranks 一致的剔除/同分规则后按 funds 最新字段重排
            _groups = {s: [] for s in _RE_RANK_SUBS}
            _ids_to_delete = []
            for r in _rows:
                try:
                    _meta = json.loads(r["meta"] or "{}")
                except Exception:
                    _meta = {}
                _groups[r["sub"]].append({"id": r["id"], "code": r["code"],
                                           "name": r["name"], "meta": _meta})
            _BONDS = ("债", "货币", "同业存单", "固收", "偏债", "理财", "现金", "纯债", "中短债")
            for sub in _RE_RANK_SUBS:
                pool = []
                for it in _groups[sub]:
                    f = conn.execute(
                        "SELECT code, name, ad_score, earn_score, score, reco, "
                        "reco_score, reco_days, is_etf FROM funds WHERE code=?",
                        (it["code"],),
                    ).fetchone()
                    if not f:
                        _ids_to_delete.append(it["id"])
                        continue
                    # 同步通用字段到 meta (与 rank_snapshots sync 阶段同口径)
                    it["meta"].update({
                        "earn_score": f["earn_score"], "ad_score": f["ad_score"],
                        "score": f["score"], "reco": f["reco"],
                        "reco_score": f["reco_score"],
                    })
                    _name = f["name"] or ""
                    # v2.11.4 B2: 仅保留 "抗跌" 分支; 开仓榜单/综合推荐 已废弃,
                    # 相关 elif 分支删除
                    # build_ranks.is_excluded: ETF/指数/联接/债券/固收+
                    _excluded = bool(f["is_etf"]) or "指数" in _name \
                        or "联接" in _name or "ETF" in _name \
                        or any(k in _name for k in _BONDS)
                    if _excluded:
                        _ids_to_delete.append(it["id"])
                        continue
                    it["sort_key"] = f["score"] or 0
                    it["tier"] = 0
                    pool.append(it)
                # 按 tier / -sort_key 排序
                pool.sort(key=lambda it: (
                    it.get("tier", 0),
                    -(it.get("sort_key") or 0),
                ))
                # 先删子面板所有旧行 (避免 UNIQUE(date,panel,sub,rank) 冲突),
                # 再按新顺序 INSERT. 旧行 id 入栈统一删除.
                for it in pool:
                    _ids_to_delete.append(it["id"])
                conn.execute(
                    "DELETE FROM rank_snapshots WHERE date=? AND panel='reco' AND sub=?",
                    (today, sub),
                )
                for i, it in enumerate(pool, 1):
                    _meta = it["meta"]
                    # tscore = 排序键 (与 build_ranks.item() 写入值一致)
                    _meta["tscore"] = it.get("sort_key")
                    # v0.46.0: 同步 meta.rank 为新 rank. 否则 md_builder 用 it.get("rank")
                    # 排序时会拿到 build_ranks 阶段写入的旧值, 出现"DB row #7 但页面显示 #1"的错位.
                    _meta["rank"] = i
                    conn.execute(
                        "INSERT INTO rank_snapshots "
                        "(date, panel, sub, rank, code, name, meta, created_at) "
                        "VALUES (?, 'reco', ?, ?, ?, ?, ?, ?)",
                        (today, sub, i, it["code"], it["name"],
                         json.dumps(_meta, ensure_ascii=False), today),
                    )
                    rank_realigned += 1
            conn.commit()
            print(f"score/reco 依赖面板重排 {rank_realigned} 条 "
                  f"(今日 {today} · 与 recompute_ad 同源)")
    except Exception as _e:
        import traceback as _tb
        print(f"⚠ score/reco 面板重排失败 (面板特定刷新已生效, 不影响打分): {_e}")
        _tb.print_exc()


    dist = conn.execute('''SELECT
        SUM(ad_score>=90) g90, SUM(ad_score>=70) g70, SUM(ad_score>=50) g50,
        ROUND(AVG(ad_score),1) avg_a, ROUND(AVG(earn_score),1) avg_e,
        ROUND(AVG(score),1) avg_s
        FROM funds''').fetchone()
    print(f"抗跌分布: >=90:{dist['g90']} >=70:{dist['g70']} >=50:{dist['g50']}")
    print(f"平均: 抗跌={dist['avg_a']} 收益={dist['avg_e']} 综合={dist['avg_s']}")
    print(f"榜单快照同步 {snap_updated} 条")

    for c in ("025415",):
        f = conn.execute("SELECT name, ad_score, earn_score, score, yindie FROM funds WHERE code=?", (c,)).fetchone()
        if f:
            print(f"{c} {f['name'][:16]}: 抗跌={f['ad_score']} 收益={f['earn_score']} 综合={f['score']} 阴跌={f['yindie']}")
    # v0.46.0: 写 task_log 让 _HTML_CACHE 自动失效. cache_key 含 latest_task_id,
    # run_collector + recompute_ad 都各自 log_start/log_finish → cache 自然 miss.
    if _v46_log_id is not None:
        try:
            import time as _t
            db.log_finish(_v46_log_id, "ok",
                          f"V0.46.0 重排 {rank_realigned} 条 + meta.rank 同步",
                          _v46_started or _t.time())
        except Exception as _e:
            print(f"⚠ task_log 写入失败 (不影响重排): {_e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
