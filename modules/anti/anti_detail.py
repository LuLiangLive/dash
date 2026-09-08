"""
services/anti_detail.py — 抗跌详情计算服务

从 main.py 拆分，负责基金详情弹窗的核心计算逻辑：
- 抗跌专项（大跌日明细/平均跌幅/修复幅度/抗跌得分）
- 大涨专项
- 增强字段（超额收益/分数演化/推荐信号历史/极端回撤警示）
- 统一指标口径合并

调用入口：anti_detail(code) -> {ok, fund: {...}}
"""
from __future__ import annotations

import bisect
import importlib
import re
import time
from typing import Optional

import db
from modules.fund.fund_detail import (
    stats_full as _stats_full,
    ad_tag as _ad_tag,
    momentum_decay_tag as _momentum_decay_tag,
    rise_source_tag as _rise_source_tag,
    dd_type_tag as _dd_type_tag,
    rebound_avg as _rebound_avg,
)
from modules.anti.anti_service import anti_cache_get, anti_cache_set, long_metrics_cache_get, long_metrics_cache_set
from modules.fund.fund_service import load_idx as _load_idx, get_fund_full_data
from modules.nav.nav_series import series, long_series, inject_long_metrics, window_mdd
from modules.common.excess import excess_rets, pick_bench, bench_series, bench_name

# ---------------------------------------------------------------------------
# 极端历史事件警示缓存（v2.5.5架构重构: 从fund_service.py移到这里，定义和使用在一起）
# ---------------------------------------------------------------------------

# 极端历史事件警示缓存 (v0.52.9: 避免重复查询数据库)
EXTREME_CACHE: dict = {}  # code -> result dict or None

# 缓存未命中标记（用于区分"缓存中有None"和"缓存中没有key"）
_EXTREME_CACHE_MISSING = object()


def extreme_cache_get(code: str):
    """获取极端历史事件缓存（v2.5.4: 封装缓存读取接口）。
    返回_MISSING表示缓存未命中，返回None表示缓存命中但值为None。
    """
    if code in EXTREME_CACHE:
        return EXTREME_CACHE[code]
    return _EXTREME_CACHE_MISSING


def extreme_cache_set(code: str, value):
    """设置极端历史事件缓存（v2.5.4: 封装缓存写入接口）。"""
    EXTREME_CACHE[code] = value


def extreme_cache_clear(codes: list = None) -> int:
    """清理极端历史事件缓存（v2.5.4: 封装缓存清理接口）。"""
    if codes is None:
        count = len(EXTREME_CACHE)
        EXTREME_CACHE.clear()
        return count
    cleared = 0
    for code in codes:
        if code in EXTREME_CACHE:
            del EXTREME_CACHE[code]
            cleared += 1
    return cleared

_NFM = None


def _load_nfm():
    """动态加载 analysis_pipeline/night_fund_monitor.py（线上版采集脚本）。"""
    global _NFM
    if _NFM is None:
        _NFM = importlib.import_module("analysis_pipeline.night_fund_monitor")
    return _NFM


def _name_of(code: str) -> str:
    """获取基金名称（委托 services.fund_service.name_of 实现）。"""
    from modules.fund.fund_service import name_of
    return name_of(code)


def _calc_merge(code, dates, vals, idx_map, ddown_local):
    """用 nfm.calc_metrics 计算统一指标口径并合并进 stats/resist。"""
    nfm = _load_nfm()
    navs = [{"date": d, "dwjz": v} for d, v in zip(dates, vals)]
    if not navs:
        return {}
    _all_daily = idx_map.get("daily_ret", []) if isinstance(idx_map, dict) else []
    if _all_daily:
        _daily_by_date = dict(_all_daily)
        _aligned_asc = [(_d, _daily_by_date.get(_d)) for _d in dates if _d in _daily_by_date]
        if _aligned_asc:
            _aligned_map = {"daily_ret": _aligned_asc}
            cm = nfm.calc_metrics(navs, idx_map=_aligned_map, ddown=ddown_local) or {}
        else:
            cm = nfm.calc_metrics(navs, idx_map=idx_map, ddown=ddown_local) or {}
    else:
        cm = nfm.calc_metrics(navs, idx_map=idx_map, ddown=ddown_local) or {}
    return cm


def _resist(fdates, fvals, idx_map, ddown_map=None):
    """抗跌专项:大跌日(上证≤-1% 或 创业板≤-4% 或 科创50≤-4%)明细/平均跌幅/大跌后5日修复/阶段抗跌得分。"""
    if not idx_map:
        return None
    _items = [(d, m) for d, m in idx_map.items() if isinstance(m, dict)]
    ddown = [(d, m.get("sh", 0.0)) for d, m in sorted(_items)
             if m.get("sh", 0.0) <= -0.01 or m.get("cyb", 0.0) <= -0.04 or m.get("kc", 0.0) <= -0.04]
    if len(ddown) < 3:
        ddown = [(d, m.get("sh", 0.0)) for d, m in sorted(_items)
                 if m.get("sh", 0.0) < 0 or m.get("cyb", 0.0) < 0 or m.get("kc", 0.0) < 0]
    if not ddown:
        return {"down_days": 0, "detail": [], "fund_avg": None, "idx_avg": None,
                "repair": None, "score": None, "tag": "无大跌日"}
    lo, hi = fdates[0], fdates[-1]
    in_range = [(d, r) for d, r in ddown if lo <= d <= hi]
    if not in_range:
        return {"down_days": len(ddown), "detail": [], "fund_avg": None, "idx_avg": None,
                "repair": None, "score": None, "tag": "区间内无大跌日"}
    fret = {}
    for i in range(1, len(fdates)):
        fret[fdates[i]] = (fvals[i] / fvals[i - 1] - 1) * 100
    detail = []
    f_avgs = []
    for d, ir in in_range:
        # 只有当基金在大盘大跌日当天有净值时才计算涨跌幅
        # 否则返回 None，显示为"无数据"，避免用之前日期的涨跌幅代替
        dd = d if d in fdates else None
        fv = fret.get(dd) if dd else None
        _im = idx_map.get(d, {})
        detail.append({"date": d, "idx": round(ir * 100, 2),
                       "cyb": round(_im.get("cyb", 0) * 100, 2) if _im.get("cyb") is not None else None,
                       "kc": round(_im.get("kc", 0) * 100, 2) if _im.get("kc") is not None else None,
                       "fund": round(fv, 2) if fv is not None else None,
                       "abnormal": bool(fv is not None and abs(fv) > 5)})
        if fv is not None:
            f_avgs.append(fv)
    fund_avg = sum(f_avgs) / len(f_avgs) if f_avgs else None
    idx_avg = sum(r for _, r in in_range) / len(in_range) * 100
    repairs = []
    for d, _ in in_range:
        if d in fdates:
            i0 = fdates.index(d)
            i1 = min(i0 + 5, len(fvals) - 1)
            if i1 > i0:
                repairs.append((fvals[i1] / fvals[i0] - 1) * 100)
    repair = sum(repairs) / len(repairs) if repairs else None
    score, tag = None, "—"
    if fund_avg is not None and idx_avg < 0:
        ratio = fund_avg / idx_avg if fund_avg < 0 else 0.0
        score = 50 + (1 - ratio) * 40
        if repair is not None:
            score += min(max(repair, 0), 10) * 2
        score = max(0, min(100, round(score)))
        tag = _ad_tag(score)
    return {"down_days": len(in_range), "detail": detail,
            "fund_avg": round(fund_avg, 2) if fund_avg is not None else None,
            "idx_avg": round(idx_avg, 2), "repair": round(repair, 2) if repair is not None else None,
            "score": score, "tag": tag}


def _rise(fdates, fvals, idx_map, up_map=None):
    """大涨日专项:上证>+1% 或 创业板>+4% 或 科创50>+4%。"""
    if not idx_map:
        return None
    _items = [(d, m) for d, m in idx_map.items() if isinstance(m, dict)]
    updays = [(d, m.get("sh", 0.0)) for d, m in sorted(_items)
              if m.get("sh", 0.0) > 0.01 or m.get("cyb", 0.0) > 0.04 or m.get("kc", 0.0) > 0.04]
    if len(updays) < 3:
        updays = [(d, m.get("sh", 0.0)) for d, m in sorted(_items)
                  if m.get("sh", 0.0) > 0 or m.get("cyb", 0.0) > 0 or m.get("kc", 0.0) > 0]
    if not updays:
        return {"up_days": 0, "detail": [], "fund_avg": None, "idx_avg": None, "tag": "无大涨日"}
    lo, hi = fdates[0], fdates[-1]
    in_range = [(d, r) for d, r in updays if lo <= d <= hi]
    if not in_range:
        return {"up_days": len(updays), "detail": [], "fund_avg": None, "idx_avg": None, "tag": "区间内无大涨日"}
    fret = {}
    for i in range(1, len(fdates)):
        fret[fdates[i]] = (fvals[i] / fvals[i - 1] - 1) * 100
    detail = []
    f_avgs = []
    for d, ir in in_range:
        # 只有当基金在大盘大涨日当天有净值时才计算涨跌幅
        # 否则返回 None，显示为"无数据"，避免用之前日期的涨跌幅代替
        dd = d if d in fdates else None
        fv = fret.get(dd) if dd else None
        _im = idx_map.get(d, {})
        detail.append({"date": d, "idx": round(ir * 100, 2),
                       "cyb": round(_im.get("cyb", 0) * 100, 2) if _im.get("cyb") is not None else None,
                       "kc": round(_im.get("kc", 0) * 100, 2) if _im.get("kc") is not None else None,
                       "fund": round(fv, 2) if fv is not None else None})
        if fv is not None:
            f_avgs.append(fv)
    fund_avg = sum(f_avgs) / len(f_avgs) if f_avgs else None
    idx_avg = sum(r for _, r in in_range) / len(in_range) * 100
    return {"up_days": len(in_range), "detail": detail,
            "fund_avg": round(fund_avg, 2) if fund_avg is not None else None,
            "idx_avg": round(idx_avg, 2), "tag": "大涨日(上证>+1% 或 创业板>+4% 或 科创50>+4%)"}


def _score_series(code, dates, vals):
    """分数演化:近 2 个月滚动窗口内每个交易日的 收益分/抗跌分/综合加权分(0-100)。

    v2026-09-01: 改用与卡片/弹窗一致的统一分数算法(anti_score/earn_score/dual_score),
    确保历史分数演化与当前分数口径一致。
    """
    n = len(vals)
    earn_s, ad_s, comp_s = [], [], []
    events = []
    try:
        nfm = importlib.import_module("analysis_pipeline.night_fund_monitor")
        _ddown_full = nfm.fetch_ddown()  # 完整三元组 (dates, pcts, idx_map)
    except Exception:
        nfm = None
        _ddown_full = ([], [], {})

    for i in range(1, n):
        w = vals[max(0, i - 9):i + 1]
        if len(w) < 2 or not w[0]:
            earn_s.append(None)
            ad_s.append(None)
            comp_s.append(None)
            continue
        # 使用统一分数算法
        if nfm and _ddown_full:
            try:
                _navs_list = [{"date": dates[j], "dwjz": vals[j]} for j in range(max(0, i - 119), i + 1)]
                _ma = nfm.anti_metrics(code, _ddown_full, navs=_navs_list)
                # 使用完整净值序列计算周期涨幅（避免10日窗口导致d10为None）
                _full_vals = vals[:i + 1]  # 从开始到当前点的完整序列
                def _calc_ret(nav_list, n):
                    if len(nav_list) < n + 1 or not nav_list[-n-1]:
                        return None
                    return (nav_list[-1] / nav_list[-n-1] - 1) * 100
                _xa = {
                    "code": code,
                    "d3": _calc_ret(_full_vals, 3),
                    "d5": _calc_ret(_full_vals, 5),
                    "d7": _calc_ret(_full_vals, 7),
                    "d10": _calc_ret(_full_vals, 10),
                }
                # v2.9.57: 统一使用scoring_v3线性评分（历史逐点回溯，卡玛用默认50）
                _ad_metrics = {
                    "dd_avg": _ma.get("fund_avg"),
                    "repair_5d": _ma.get("repair"),
                    "repair_10d": _ma.get("repair_10d"),
                }
                from collector.scoring_v3 import compute_ad_score, compute_earn_score, compute_dual_score
                ad = compute_ad_score(_ad_metrics)
                earn = compute_earn_score(_xa, repair_5d=_ma.get("repair"))
                _calmar = 50  # 历史序列逐点计算无池内卡玛数据
                comp = compute_dual_score(earn, ad, _calmar)
            except Exception:
                # 降级:使用简单算法
                ret10 = (w[-1] / w[0] - 1) * 100
                earn = max(0, min(100, 50 + ret10 / 8 * 50))
                peak = w[0]
                mdd = 0.0
                for v in w:
                    if v > peak:
                        peak = v
                    dd = (v / peak - 1) * 100
                    if dd < mdd:
                        mdd = dd
                ad = max(0, min(100, 100 + mdd / 8 * 100))
                comp = round(earn * 0.425 + ad * 0.425 + 50 * 0.15)  # v2.11.2: V3综合分口径(卡玛缺省50)
        else:
            # 降级:使用简单算法
            ret10 = (w[-1] / w[0] - 1) * 100
            earn = max(0, min(100, 50 + ret10 / 8 * 50))
            peak = w[0]
            mdd = 0.0
            for v in w:
                if v > peak:
                    peak = v
                dd = (v / peak - 1) * 100
                if dd < mdd:
                    mdd = dd
            ad = max(0, min(100, 100 + mdd / 8 * 100))
            comp = round(earn * 0.425 + ad * 0.425 + 50 * 0.15)  # v2.11.2: V3综合分口径(卡玛缺省50)

        earn_s.append(round(earn) if earn is not None else None)
        ad_s.append(round(ad) if ad is not None else None)
        comp_s.append(round(comp) if comp is not None else None)
        if i >= 1 and vals[i - 1]:
            chg = (vals[i] / vals[i - 1] - 1) * 100
            if chg >= 3:
                events.append({"date": dates[i], "type": "up", "v": round(chg, 2)})
            elif chg <= -3:
                events.append({"date": dates[i], "type": "down", "v": round(chg, 2)})
    return {"dates": dates[1:], "earn": earn_s, "ad": ad_s, "comp": comp_s,
            "events": events, "weights": {"attack": 0.7, "stable": 0.3}}


def _extreme_warn_tag(code, vals, dates):
    """极端历史事件警示:规避 2 个月滚动窗口洗白问题。"""
    _cached = extreme_cache_get(code)
    if _cached is not _EXTREME_CACHE_MISSING:
        return _cached
    rows = db.get_nav(code, limit=252)
    rows = list(reversed(rows))
    from collector import fetcher as _fpe
    _pairs = _fpe.nav_pairs(rows, min_pts=8)
    extremes = []
    for i in range(1, len(_pairs)):
        _pd, _pv = _pairs[i - 1]
        _cd, _cv = _pairs[i]
        if _pv:
            chg = (_cv / _pv - 1) * 100
            if chg <= -6:
                extremes.append((_cd, round(chg, 2)))
    result = None
    if extremes:
        last_d, last_v = extremes[-1]
        in_win = last_d in dates
        result = {"date": last_d, "drop": last_v, "in_window": in_win,
                  "msg": f"历史极端回撤警示: {last_d} 单日 {last_v:+.2f}%"}
    extreme_cache_set(code, result)
    return result


_LIVE_CTX = {"ts": 0, "idx_series": None, "bench_items": None}


def _live_ctx():
    """指数序列 + 中证800基准(30分钟缓存, 供推荐建议实时回放)。"""
    now = time.time()
    if _LIVE_CTX["idx_series"] is None or now - _LIVE_CTX["ts"] > 1800:
        conn = db.get_conn()
        idx_series = {}
        for ick in ("sh000001", "sz399006", "sh000688"):
            rows = db.get_nav(ick, limit=120)
            rows = list(reversed(rows))
            idx_series[ick] = {"items": [{"date": r["date"], "close": r["ljjz"]} for r in rows]}
        rows = db.get_nav("sh000906", limit=120)
        rows = list(reversed(rows))
        bench_items = [(r["date"], r["ljjz"]) for r in rows]
        _LIVE_CTX.update({"ts": now, "idx_series": idx_series, "bench_items": bench_items})
    return _LIVE_CTX["idx_series"], _LIVE_CTX["bench_items"]


def _reco_history(code: str, days: int = 42, window: int = 75) -> list:
    """推荐建议历史(实时回放, 点击弹窗时抓取数据计算, 上榜/自选基金均可用)。"""
    try:
        conn = db.get_conn()
        rows = conn.execute(
            "SELECT date, dwjz FROM nav_history WHERE code=? ORDER BY date DESC LIMIT 110",
            (code,)).fetchall()
        rows = list(reversed(rows))
        if len(rows) < 10:
            return []
        navs = [float(r["dwjz"]) for r in rows]
        dates = [r["date"] for r in rows]
        idx_series, bench_items = _live_ctx()
        ranker = importlib.import_module("collector.ranker")
        idx_map = ranker._build_idx_map(idx_series, dates)
        ddown, _rise = ranker._build_ddays(idx_map, dates)
        _idx_items = (idx_series or {}).get("sh000001", {}).get("items", [])
        if len(_idx_items) >= 2 and _idx_items[-1].get("close") and _idx_items[-60].get("close"):
            idx60 = (_idx_items[-1]["close"] / _idx_items[-60]["close"] - 1) * 100
        else:
            idx60 = None
        out = []
        prev_lvl, prev_days = None, 0
        play_start = 0
        start = max(0, len(navs) - days)
        _stored_cur = conn.execute(
            "SELECT reco, reco_days, nav_date FROM funds WHERE code=?", (code,)).fetchone()
        for i in range(play_start, len(navs)):
            w = navs[:i + 1]
            wd = dates[:i + 1]
            tail = w[-window:]
            m = ranker.calc_metrics(tail, idx_map, ddown)
            m["d1"] = ranker._ret(tail, 1)
            m["d2"] = ranker._ret(tail, 2)
            m["rzf"] = m["d1"]
            m["m3"] = ranker._ret(tail, 63)
            m["m6"] = ranker._ret(tail, 126)
            m["y1"] = ranker._ret(tail, 252)
            m["dn7"] = m.get("dn7", 0)
            # v2.11.2: calc_metrics 已输出真实近20日高 dd20, 无需再用 dd_from_hi 覆盖; 仅作兜底兼容旧 metrics
            if m.get("dd20") is None:
                m["dd20"] = m.get("dd_from_hi")
            _score, lvl, _mode, _note = ranker.compute_reco_v32(
                m, tail, prev_reco=prev_lvl, prev_reco_days=prev_days, idx60=idx60)
            chg = (lvl != prev_lvl)
            cur_days = 1 if chg else (prev_days + 1)
            note = ""
            if chg and prev_lvl is not None:
                note = "上一档:%s%s天" % (prev_lvl, prev_days)
            if i == len(navs) - 1 and _stored_cur and _stored_cur["reco"]:
                _cur_date = wd[-1]
                if (not _stored_cur["nav_date"]) or _cur_date == _stored_cur["nav_date"]:
                    _stored_reco = _stored_cur["reco"]
                    _stored_days = _stored_cur["reco_days"]
                    _stored_prev = conn.execute(
                        "SELECT prev_reco, prev_reco_days FROM funds WHERE code=?", (code,)).fetchone()
                    _sp_reco = _stored_prev["prev_reco"] if _stored_prev else None
                    _sp_days = _stored_prev["prev_reco_days"] if _stored_prev else None
                    if _stored_reco != lvl:
                        lvl = _stored_reco
                        if _sp_reco and _sp_reco != _stored_reco:
                            chg = True
                            note = "上一档:%s%s天" % (_sp_reco, _sp_days or 0)
                            cur_days = _stored_days or cur_days
                        else:
                            chg = (lvl != prev_lvl)
                            note = ""
                            cur_days = _stored_days or cur_days
                    elif _stored_days and abs((_stored_days or 0) - cur_days) >= 2:
                        cur_days = _stored_days or cur_days
            if i >= start:
                out.append({"date": wd[-1], "reco": lvl, "days": cur_days,
                            "note": note, "chg": chg})
            prev_lvl, prev_days = lvl, cur_days
        if out and _stored_cur and _stored_cur["reco"]:
            _last = out[-1]
            _sd = _stored_cur["reco_days"] or 0
            if _sd != (_last.get("days") or 0):
                try:
                    conn.execute(
                        "UPDATE funds SET reco_days=? WHERE code=?",
                        (_last.get("days"), code))
                    conn.commit()
                except Exception:
                    pass
        return out
    except Exception:
        return []


def _enhance_detail(code, dates, vals, st, resist, rise, idx_map):
    """汇总所有增强字段(收益/抗跌表格补充 + 标签 + 分数演化)。"""
    bench_ick = pick_bench(code)
    bench_pts = bench_series(bench_ick)
    for fallback in ("sh000906", "sh000300", "sh000688"):
        if len(bench_pts) >= 10:
            break
        bench_ick = fallback
        bench_pts = bench_series(fallback)
    out = {
        "excess": excess_rets(dates, vals, bench_pts, 42),
        "excess_bench": bench_ick,
        "excess_bench_name": bench_name(bench_ick),
        "momentum_tag": _momentum_decay_tag(st),
        "rise_tag": _rise_source_tag(rise, st),
        "dd_count": (resist or {}).get("down_days", 0),
        "rebound": _rebound_avg(dates, vals, resist),
        "dd_type": _dd_type_tag(resist),
        "extreme_warn": _extreme_warn_tag(code, vals, dates),
        "score_series": _score_series(code, dates, vals),
        "reco_history": _reco_history(code),
    }
    return out


def anti_detail_sync(code: str):
    """抗跌详情同步计算（弹窗数据源，字段与线上版一致）。"""
    if not re.fullmatch(r"\d{6}", code):
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="请输入 6 位基金代码")
    _cached = anti_cache_get(code)
    if _cached is not None:
        return _cached
    nfm = _load_nfm()
    dates, vals = series(code, days=120)
    if len(vals) < 2:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=f"基金 {code} 净值样本不足")
    idx_map, idx_rows = _load_idx()
    st = _stats_full(vals)
    rs = _resist(dates, vals, idx_map)
    rup = _rise(dates, vals, idx_map)
    _items_idx = [(d, m) for d, m in idx_map.items() if isinstance(m, dict)]
    _ddown_local = [(d, m.get("sh", 0.0)) for d, m in sorted(_items_idx)
                    if m.get("sh", 0.0) <= -0.01 or m.get("cyb", 0.0) <= -0.04 or m.get("kc", 0.0) <= -0.04]
    if len(_ddown_local) < 3:
        _ddown_local = [(d, m.get("sh", 0.0)) for d, m in sorted(_items_idx)
                        if m.get("sh", 0.0) < 0 or m.get("cyb", 0.0) < 0 or m.get("kc", 0.0) < 0]
    cm = _calc_merge(code, dates, vals, idx_map, _ddown_local)
    if st is not None and cm:
        if cm.get("up_cap") is not None:
            cm["up_capture"] = cm["up_cap"]
        if cm.get("dn_cap") is not None:
            cm["dn_capture"] = cm["dn_cap"]
        st.update({k: cm[k] for k in ("ret2m", "max_daily_drop", "mdd", "vol",
                                      "hi_cnt", "dd_from_hi", "pl", "pl_txt", "mdd_days", "mdd_status",
                                      "down_vol", "down_sharpe", "calmar", "up_capture", "dn_capture",
                                      "repair_tag") if k in cm})
        if cm.get("up_ratio") is not None:
            st["up_ratio"] = round(cm["up_ratio"] * 100, 1)
        st["r1m"] = cm.get("m1")
        st["r2w"] = cm.get("d10")
        st["r5d"] = cm.get("d5")
        for _k in ("d3", "d5", "d7", "d10", "m1", "m3", "ret2m"):
            if _k in cm and st.get(_k) is None:
                st[_k] = cm[_k]
        if rs is not None:
            rs["repair_tag"] = cm.get("repair_tag")
            rs["up_capture"] = cm.get("up_capture")
            rs["dn_capture"] = cm.get("dn_capture")
    f = get_fund_full_data(code)
    inject_long_metrics(code, st)
    # v2026-09-02: 根治数据不一致问题 - 长期指标(m3/m6/y1/ret2m)统一从funds表取
    # 原因: nav_history只存储最近60天数据，不足以计算m6/y1；funds表是一键更新时从天天基金网拉取的完整数据
    # 效果: 弹窗/基金卡片/对比页面的长期指标完全一致
    # v2.9.8: 扩展统一数据源范围，把更多指标也统一从funds表取，确保三个页面完全一致
    if f:
        # 收益指标
        for _k in ("d1", "d2", "m1", "m3", "m6", "y1", "ret2m", "d3", "d5", "d7", "d10"):
            if f.get(_k) is not None:
                st[_k] = f[_k]
        # 风险指标
        for _k in ("mdd", "mdd_days", "mdd_status", "vol", "down_vol", "down_sharpe", "calmar"):
            if f.get(_k) is not None:
                st[_k] = f[_k]
        # 其他指标(v2.11.2: 加入 ms, 弹窗动能与卡片同源)
        for _k in ("pl", "hi_cnt", "dd_from_hi", "up_capture", "dn_capture", "up_ratio", "ms"):
            if f.get(_k) is not None:
                st[_k] = f[_k]
    _scale = (f or {}).get("scale")
    _est = (f or {}).get("est")
    if not _scale or not _est:
        try:
            from collector import fetcher as _ft
            _info = _ft.fetch_basic(code)
            if _info:
                if not _scale:
                    _scale = _info[0]
                if not _est:
                    _est = _info[1]
            if _scale or _est:
                try:
                    _patch = dict(f) if f else {}
                    if _scale and not _patch.get("scale"):
                        _patch["scale"] = _scale
                    if _est and not _patch.get("est"):
                        _patch["est"] = _est
                    _patch["code"] = code
                    db.upsert_fund(_patch)
                except Exception:
                    pass
        except Exception:
            pass
    # v2.9.43: 全局统一使用 compute_scores_v2 算法
    # 一键更新阶段4 recompute_ad 对上榜+自选基金批量调用 compute_scores_v2 计算后写入数据库
    # 弹窗/对比页/卡片都从数据库读取同一套分数，确保100%一致
    # 非上榜/非自选基金数据库中可能没有分数，显示None（用户也看不到这些基金）
    ad_db = (f or {}).get("ad_score")
    er_db = (f or {}).get("earn_score")
    du_db = (f or {}).get("score")
    tag = _ad_tag(ad_db)
    if rs is not None:
        rs["score"] = ad_db
        rs["tag"] = tag
    enh = _enhance_detail(code, dates, vals, st or {}, rs, rup, idx_map)
    _fund_name = _name_of(code)
    _result = {
        "ok": True,
        "fund": {
            "code": code,
            "name": _fund_name,
            "scale": _scale,
            "est": _est,
            "yindie": (f or {}).get("yindie"),
            "dates": dates,
            "navs": [round(v, 4) for v in vals],
            "nav_ret": [round((v / vals[0] - 1) * 100, 2) for v in vals] if vals and vals[0] else [],
            "stats": st,
            "resist": {
                "ad_score": ad_db, "earn_score": er_db, "dual": du_db, "tag": tag,
                "down_days": (rs or {}).get("down_days", 0),
                "detail": (rs or {}).get("detail") or [],
                "fund_avg": (rs or {}).get("fund_avg"),
                "idx_avg": (rs or {}).get("idx_avg"),
                "repair": (rs or {}).get("repair"),
                "repair_tag": (rs or {}).get("repair_tag"),
                "up_capture": (rs or {}).get("up_capture"),
                "dn_capture": (rs or {}).get("dn_capture"),
                "score": (rs or {}).get("score"),
            },
            "rise": rup or {"detail": [], "fund_avg": None, "idx_avg": None, "up_days": 0, "tag": "无大涨日"},
            "rise_avg": (rup or {}).get("fund_avg"),
            "enhanced": enh,
        },
        "index_note": "大跌日 = 上证≤-1% 或 创业板指≤-4% 或 科创50≤-4%(任一满足)",
    }
    anti_cache_set(code, _result)
    return _result





