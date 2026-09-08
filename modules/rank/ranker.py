"""
ranker.py —— 指标计算 / 评分 / 抗跌专项

规格 2.3 / 2.4 / 2.5 / 2.6 / 2.7 的算法实现。采集器每次运行后输出:
- funds 指标(写入 funds 表)
- 各榜单快照(写入 rank_snapshots 表)


设计要点:
- 百分位给分:按当期基金池内排名,最优 100 / 最差 0(spec 2.4④)
- 抗跌/收益/综合三套分均池内计算,弹窗/卡片/榜单共用一份数据(spec 7.2)
- 持有结论四条件判定(spec 2.5)
"""
from __future__ import annotations

import math
from typing import Optional

from collector.filters import is_index_like, is_equity_excluded
# v0.76.2: 纯指标函数已提取到 collector.metrics
from collector.metrics import (
    METRIC_FIELD_MAP, apply_metric_field_map,
    _ret, _daily_rets, _pstdev, streak_days, risk_flags, _max_dd,
    percentile_scores, inverse_percentile_scores, _norm_weights, _cdf_phi,
    _pct_val, _ret_skipna, _reverse_pct, _forward_pct, _ma_n, _ret_pct_navs,
    _big_drop_unrepaired, _has_unrepaired_big_drop, _ret_series,
)
# v0.76.2: 评分函数已提取到 collector.scoring
from collector.scoring import (
    pool_earn_scores, _up_capture,
    _align_bench_vals, calc_excess_rets, big_rise_follow,
    _fund_bench_series, detect_yindie_deep,
    compute_scores_v2,
)
# v0.76.2: 推荐/判定函数已提取到 collector.recommendation
from collector.recommendation import (
    compute_reco_score, compute_reco_v32,
    trend_status, liq_score, reco_filter,
    reco_note_txt, reco_risk_tags_txt, kinetic_state, momentum_status,
)
# v0.76.2: 榜单构建函数已提取到 collector.ranking
from collector.ranking import watch_score_light, panel_score


# ---------------------------------------------------------------------------
# 统一字段映射 (calc_metrics 输出 → 前端/API 使用的字段名)
# 避免在 main.py 多处硬编码导致漂移
# ---------------------------------------------------------------------------



















def refresh_funds_dd20(conn=None, progress_cb=None) -> int:
    """v2.11.2 长期兜底: 按已存净值刷新全池 funds.dd20(真实距最近20交易日高)。

    增量模式下"缓存最新==目标日期"被跳过的基金不会走 run_collector 重算指标,
    若其 dd20 由旧语义(曾=dd_from_hi 距序列高点)写入则一直残留, 造成卡片
    "距20日高"与真实值不符。每次一键更新抓净值后调用本函数(纯本地、秒级),
    保证全池 dd20 与最新净值同口径, 并为未来口径变更提供自愈。
    与 calc_metrics 内 dd20 计算一致: 取最近20个有效净值最高点(窗口不足退化)。
    """
    if conn is None:
        import db
        conn = db.get_conn()
    codes = [r[0] for r in conn.execute("SELECT code FROM funds ORDER BY code")]
    updated = 0
    for i, code in enumerate(codes, 1):
        rows = conn.execute(
            "SELECT dwjz FROM nav_history WHERE code=? AND dwjz IS NOT NULL "
            "ORDER BY date DESC LIMIT 30", (code,)).fetchall()
        navs = [r[0] for r in rows][::-1]  # 转升序(仅最近30个有效点, 足够取20日窗口)
        if len(navs) < 2 or not navs[-1]:
            continue
        win = [v for v in navs[-20:] if v]
        peak = max(win) if win else None
        if not peak:
            continue
        new = round((navs[-1] / peak - 1) * 100, 2)
        cur = conn.execute("SELECT dd20 FROM funds WHERE code=?", (code,)).fetchone()
        old = cur[0] if cur else None
        if old is None or abs((old or 0) - new) >= 0.005:
            conn.execute("UPDATE funds SET dd20=? WHERE code=?", (new, code))
            updated += 1
        if progress_cb and i % 500 == 0:
            progress_cb(i, len(codes))
    try:
        conn.commit()
    except Exception:
        pass
    return updated


def _current_visible_codes(conn) -> set[str]:
    """可见池 = 当日最新榜单上榜 ∪ 自选(watchlist/watch_groups) ∪ 持仓(portfolio/records)。

    v2.11.3 两池模型: 用户可见的基金只有"榜上 + 自选 + 持仓", 这些基金在第5步会被
    全量精确重算; 其余(不可见)基金不维护展示型基础指标。返回空集时调用方应跳过清空,
    避免把全库清空(安全护栏)。
    """
    codes: set[str] = set()
    _max_date = conn.execute("SELECT MAX(date) FROM rank_snapshots").fetchone()
    if _max_date and _max_date[0]:
        latest = _max_date[0]
        for (c,) in conn.execute(
            "SELECT DISTINCT code FROM rank_snapshots WHERE date=? AND code!=''", (latest,)
        ).fetchall():
            codes.add(c)
    for tbl, col in (("watchlist", "code"), ("watch_groups", "code"), ("watch_group_items", "code")):
        try:
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({tbl})").fetchall()]
        except Exception:
            continue
        if col in cols:
            for (c,) in conn.execute(f"SELECT DISTINCT {col} FROM {tbl} WHERE {col}!=''").fetchall():
                codes.add(c)
    for tbl, col in (("portfolio", "code"), ("investment_records", "code")):
        try:
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({tbl})").fetchall()]
        except Exception:
            continue
        if col in cols:
            for (c,) in conn.execute(f"SELECT DISTINCT {col} FROM {tbl} WHERE {col}!=''").fetchall():
                codes.add(c)
    return codes


# v2.11.3 可清空列(展示型基础指标 + 评分/信号)。
# 注意: 绝不能清空 code/name/档案/净值相关, 以及 m1/m3/m6/y1(权威长周期,
# rank_full._metrics_for 会读 funds 行兜底, 清空后历史不足的基金榜单标签会变空)。
_CLEARABLE_COLS = [
    "d1", "d2", "d3", "d5", "d7", "d10", "dn7", "ms", "dd20",
    "mdd", "mdd_days", "mdd_status", "vol", "down_vol", "down_sharpe",
    "calmar", "pl", "hi_cnt", "dd_from_hi", "streak", "yindie",
    "score", "ad_score", "earn_score", "tscore", "calmar_score",
    "verdict", "suggest", "reco", "reco_score", "reco_days",
    "prev_reco", "prev_reco_days", "max_daily_drop_7d",
]


def clear_non_visible_metrics(conn=None, dry_run: bool = False) -> int:
    """v2.11.3 两池模型收尾: 清空"不可见基金"(不在当榜/自选/持仓)的展示型基础指标。

    目的: 普通基金不维护精确指标, 杜绝"挂着上一次/旧口径数值"被偶然读到造成误导;
    它们一旦上榜/被加自选/入持仓, 下一次更新由"上榜+自选"阶段全量精确重算覆盖。
    安全护栏: 可见集过小(<200)或查不到最新榜时跳过, 防误清空。
    返回实际 UPDATE 的行数(试跑返回将被清空的行数)。
    """
    if conn is None:
        import db
        conn = db.get_conn()
    # v2.11.4 C3: 校验快照日期是否为今日; 若不是, 说明本轮 _complete=False 未刷榜,
    # 用旧榜当可见池会误清活行, 直接跳过。
    import time as _time_mod
    _max_date_row = conn.execute("SELECT MAX(date) FROM rank_snapshots").fetchone()
    _max_date = _max_date_row[0] if _max_date_row else None
    _today_str = _time_mod.strftime("%Y-%m-%d")
    if _max_date != _today_str:
        return 0
    visible = _current_visible_codes(conn)
    if len(visible) < 200:
        return 0
    cols = [c for c in _CLEARABLE_COLS
            if c in {r[1] for r in conn.execute("PRAGMA table_info(funds)").fetchall()}]
    if not cols:
        return 0
    marks = ",".join("?" * len(visible))
    _placeholders = ",".join(f"{c}=?" for c in cols)
    sql = f"UPDATE funds SET {_placeholders} WHERE code NOT IN ({marks})"
    if dry_run:
        row = conn.execute(
            f"SELECT COUNT(*) FROM funds WHERE code NOT IN ({marks})", tuple(visible)).fetchone()
        return int(row[0]) if row else 0
    cur = conn.execute(sql, tuple([None] * len(cols)) + tuple(visible))
    try:
        conn.commit()
    except Exception:
        pass
    return cur.rowcount


def prune_keep_latest_snapshots(conn=None) -> int:
    """v2.11.3: 榜单只保留当日最新版 —— 删除更早日期的历史榜单行。

    前端无历史榜单/连续在榜需求, 跨日累积只会增加冗余与陈旧数据。返回删除条数。
    """
    if conn is None:
        import db
        conn = db.get_conn()
    row = conn.execute("SELECT MAX(date) FROM rank_snapshots").fetchone()
    if not row or not row[0]:
        return 0
    latest = row[0]
    cur = conn.execute("DELETE FROM rank_snapshots WHERE date < ?", (latest,))
    try:
        conn.commit()
    except Exception:
        pass
    return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0


def calc_metrics(navs: list[float], idx_map: dict = None, ddown: list = None) -> dict:
    """navs: 净值序列(升序)。返回全部常规指标。"""
    if not navs or len(navs) < 2:
        return {}
    idx_map = idx_map or {}
    ddown = ddown or []

    rets = _daily_rets(navs)
    cnt = len(rets)
    # v1.1.6: 阈值对齐 >0/<0 —— 「上涨天数占比」的直觉语义是"涨跌算上涨",
    # 旧实现用 ±0.3% 阈值, 与下方 dn7 的 <0 口径自相矛盾, 且对债基等
    # 低波动基金严重低估(占比趋近 0); 该字段仅用于展示, 不参与评分。
    up_days = [r for r in rets if r > 0]

    # v2.9.8: 长期收益指标增加数据完整性检查，数据不足窗口80%时返回None
    m1 = _ret(navs, 21, min_ratio=0.8)
    ret2w = _ret(navs, 10, min_ratio=0.8)
    ret5d = _ret(navs, 5, min_ratio=0.8)
    ret2m = _ret(navs, 42, min_ratio=0.8)
    # v2.1.6: 近3月收益（用于收益分计算）
    ret3m = _ret(navs, 63, min_ratio=0.8)
    # v2.1.5: 近6月收益和近6月最大回撤（用于卡玛比率计算）
    ret6m = _ret(navs, 126, min_ratio=0.8)
    navs_6m = navs[-126:] if len(navs) > 126 else navs
    mdd_6m, _, _, _ = _max_dd(navs_6m) if len(navs_6m) >= 2 else (None, None, None, None)
    # v2.9.8: 近2月最大回撤（用于卡玛比率，分子分母时间周期匹配）
    navs_2m = navs[-42:] if len(navs) > 42 else navs
    mdd_2m, _, _, _ = _max_dd(navs_2m) if len(navs_2m) >= 2 else (None, None, None, None)

    max_daily_drop = min(rets) if rets else None
    mdd, mdd_days, mdd_status, _mdd_seg = _max_dd(navs)

    # v2.9.8: 波动率增加数据完整性检查，至少需要60天数据（约3个月）才计算年化波动率
    vol = _pstdev(rets) * math.sqrt(252) if len(rets) >= 60 else None
    down_rets = [r for r in rets if r < 0]
    down_vol = _pstdev(down_rets) * math.sqrt(252) if len(down_rets) >= 30 else None
    down_sharpe = (ret2m / down_vol) if (ret2m is not None and down_vol and down_vol > 0) else None
    # v2.9.8: 卡玛比率修正为 2个月收益 / 近2月最大回撤（分子分母时间周期匹配）
    calmar = (ret2m / abs(mdd_2m)) if (ret2m is not None and mdd_2m and abs(mdd_2m) > 0.001) else None

    up_ratio = len(up_days) / cnt if cnt else 0

    up_chain = 1.0
    dn_chain = 1.0
    for r in rets:
        if r > 0:
            up_chain *= (1 + r / 100)
        elif r < 0:
            dn_chain *= (1 - abs(r) / 100)
    pl = up_chain / dn_chain if dn_chain else None

    hi_cnt = 0
    peak = navs[0]
    for v in navs[1:]:
        if v >= peak * 1.005:
            hi_cnt += 1
            peak = v
    dd_from_hi = (navs[-1] - peak) / peak * 100 if peak else None

    # v2.11.2: dd20 = 真实"距最近20个交易日最高净值"回撤(卡片"距20日高"口径)。
    # 独立于 dd_from_hi(距所载序列最高点,约1年窗口)。旧实现把 dd20 当作 dd_from_hi 别名,
    # 导致卡片"距20日高"数值是"距全序列高点",此处修正。窗口不足20日时退化为可用样本。
    _win20 = [v for v in navs[-20:] if v]
    _peak20 = max(_win20) if _win20 else None
    dd20 = round((navs[-1] / _peak20 - 1) * 100, 2) if _peak20 and navs[-1] else None

    # 捕获率(大盘日收益映射 idx_map['daily_ret'] = [(date, ret)])
    # 阈值从 algo_config 读取，可在「我的-算法」面板调整
    up_capture = dn_capture = None
    try:
        from algo_config import get_algo_config
        _acfg = get_algo_config()
        _up_th = _acfg.atk_up_day_th
        _dn_th = _acfg.atk_dn_day_th
    except Exception:
        _up_th = 0.5
        _dn_th = -0.5
    daily_ret = idx_map.get("daily_ret", [])
    # rets 长度为 len(navs)-1, daily_ret 需对齐到 rets 长度(去掉首日)
    # v2026-08-28 修复: 即使 daily_ret 长度不够, 也取两者最小值计算
    if daily_ret and len(daily_ret) >= 2:
        # daily_ret 第二项可能是 float 或 {"sh":..,"sz":..,"kc":..} dict, 统一取上证指数(sh)
        def _idx_ret_val(r):
            if isinstance(r, dict):
                return r.get("sh") or r.get("sz") or r.get("kc")
            return r
        _n = min(len(daily_ret), len(rets))
        idx_rets = [_idx_ret_val(r) for _, r in daily_ret[-_n:]]
        _rets_aligned = rets[-_n:] if len(rets) >= _n else rets
        idx_up = [r for r in idx_rets if r is not None and r > _up_th]
        idx_dn = [r for r in idx_rets if r is not None and r < _dn_th]
        if idx_up and _rets_aligned:
            f_up = [_rets_aligned[i] for i, r in enumerate(idx_rets) if r is not None and r > _up_th and i < len(_rets_aligned)]
            up_capture = (sum(f_up) / len(f_up)) / (sum(idx_up) / len(idx_up)) if len(f_up) else None
        if idx_dn and _rets_aligned:
            f_dn = [_rets_aligned[i] for i, r in enumerate(idx_rets) if r is not None and r < _dn_th and i < len(_rets_aligned)]
            dn_capture = (sum(f_dn) / len(f_dn)) / (sum(idx_dn) / len(idx_dn)) if len(f_dn) else None

    # 近7日形态指标(线上版口径):dn7 下跌天数 / max_daily_drop_7d 单日最大跌幅
    _r7 = rets[-7:] if len(rets) >= 7 else rets
    dn7 = sum(1 for r in _r7 if r < 0)
    # 只取下跌日的跌幅；如果近7日没有下跌，返回0（不是最小涨幅）
    _dn7_vals = [r for r in _r7 if r < 0]
    max_daily_drop_7d = min(_dn7_vals) if _dn7_vals else 0

    return {
        "ret2m": ret2m, "ret3m": ret3m, "m1": m1, "ret2w": ret2w, "ret5d": ret5d,
        "ret6m": ret6m, "mdd_6m": mdd_6m,
        "d2": _ret(navs, 2), "d3": _ret(navs, 3), "d5": _ret(navs, 5),
        "d7": _ret(navs, 7), "d10": _ret(navs, 10),
        "dn7": dn7, "max_daily_drop_7d": max_daily_drop_7d,
        "max_daily_drop": max_daily_drop, "mdd": mdd, "mdd_days": mdd_days, "mdd_status": mdd_status,
        "vol": vol, "down_vol": down_vol, "down_sharpe": down_sharpe, "calmar": calmar,
        "up_ratio": up_ratio, "pl": pl,
        "hi_cnt": hi_cnt, "dd_from_hi": dd_from_hi, "dd20": dd20,
        "up_capture": up_capture, "dn_capture": dn_capture,
    }


# ---------------------------------------------------------------------------
# 大涨日 / 大跌日(spec 2.6 / 2.4)
# ---------------------------------------------------------------------------

def _build_ddays(idx_map, nav_dates):
    """构造 (ddown, rise, 与基金净值对齐的 daily_ret) 三指数下跌/上涨日。

    大跌判定:上证≤-1% 或 创业板≤-4% 或 科创50≤-4%
    大涨判定:上证>+1% 或 创业板>+4% 或 科创50>+4%
    返回元素带 {date, sh, sz, kc}。
    """
    daily = idx_map.get("daily_ret", [])
    if not daily:
        return [], []
    ddown, rise = [], []
    for i, (dt, rets) in enumerate(daily):
        sh = rets.get("sh")
        sz = rets.get("sz")
        kc = rets.get("kc")
        if (sh is not None and sh <= -1.0) or (sz is not None and sz <= -4.0) or (kc is not None and kc <= -4.0):
            ddown.append({"date": dt, "sh": sh, "sz": sz, "kc": kc})
        if (sh is not None and sh > 1.0) or (sz is not None and sz > 4.0) or (kc is not None and kc > 4.0):
            rise.append({"date": dt, "sh": sh, "sz": sz, "kc": kc})
    return ddown, rise


def _build_idx_map(idx_series: dict, nav_dates: list) -> dict:
    """对齐三指数到共同日期轴,产出 daily_ret: [(date, {sh,sz,kc})]。
    以三指数日期交集为基准,按日期精确对齐(避免索引错位)。"""
    base = idx_series.get("sh000001", {}).get("items", [])
    if not base:
        return {}
    close_by_date = {}
    for c, key in (("sh000001", "sh"), ("sz399006", "sz"), ("sh000688", "kc")):
        items = idx_series.get(c, {}).get("items", [])
        for x in items:
            close_by_date.setdefault(x["date"], {})[key] = x["close"]
    dates = sorted(close_by_date.keys())
    daily_ret = []
    for i in range(1, len(dates)):
        dt = dates[i]
        row = {"sh": None, "sz": None, "kc": None}
        for key in ("sh", "sz", "kc"):
            prev = close_by_date.get(dates[i - 1], {}).get(key)
            cur = close_by_date.get(dt, {}).get(key)
            if prev and cur:
                row[key] = (cur - prev) / prev * 100
        daily_ret.append((dt, row))
    return {"daily_ret": daily_ret, "dates": dates}


def anti_resist(code, navs, dates, idx_map, ddown):
    """抗跌专项(spec 2.6):
    score = 50 + (1 − 基金大跌日均跌幅/大盘大跌日均跌幅) × 40
           + min(max(大跌后5日修复,0),10) × 2
    返回含 rep5/rep10 平均反弹与回撤区间(供 6 项抗跌分与阴跌识别)。"""
    if not navs or not ddown:
        return {"ad_score": 50, "earn_score": 50, "dual": 50,
                "detail": [], "avg_dd_fund": None, "avg_dd_idx": None,
                "rep5_avg": None, "rep10_avg": None, "tag": "抗跌一般", "mdd_seg": None}
    dmap = dict(zip(dates, navs))
    dd_rows = []
    fund_dd_rets, idx_dd_rets = [], []
    rep5_rets, rep10_rets = [], []
    for d in ddown:
        dt = d["date"]
        idx = dates.index(dt) if dt in dates else None
        if idx is None:
            continue
        prev = navs[idx - 1] if idx > 0 else None
        if not prev:
            continue
        f_ret = (navs[idx] - prev) / prev * 100
        fund_dd_rets.append(f_ret)
        idx_dd_rets.append(-abs(d.get("sh") or -1.0))
        row = {"date": dt, "sh": d.get("sh"), "sz": d.get("sz"), "kc": d.get("kc"), "fund": f_ret}
        # 大跌后 5 / 10 个交易日反弹(次日算起)
        if idx + 1 < len(navs) and navs[idx]:
            if idx + 5 < len(navs):
                rep = (navs[idx + 5] - navs[idx]) / navs[idx] * 100
                rep5_rets.append(rep)
                row["rep5"] = rep
            if idx + 10 < len(navs):
                rep = (navs[idx + 10] - navs[idx]) / navs[idx] * 100
                rep10_rets.append(rep)
                row["rep10"] = rep
        dd_rows.append(row)
    if not fund_dd_rets:
        return {"ad_score": 50, "earn_score": 50, "dual": 50,
                "detail": [], "avg_dd_fund": None, "avg_dd_idx": None,
                "rep5_avg": None, "rep10_avg": None, "tag": "抗跌一般", "mdd_seg": None}
    avg_f = sum(fund_dd_rets) / len(fund_dd_rets)
    avg_i = sum(idx_dd_rets) / len(idx_dd_rets)
    rep5 = sum(rep5_rets) / len(rep5_rets) if rep5_rets else 0
    rep10 = sum(rep10_rets) / len(rep10_rets) if rep10_rets else 0
    score = 50
    if avg_i != 0:
        score += (1 - avg_f / avg_i) * 40
    score += min(max(rep5, 0), 10) * 2
    ad = max(0, min(100, round(score)))
    tag = ("抗跌强" if ad >= 80 else ("抗跌良好" if ad >= 60 else ("抗跌一般" if ad >= 40 else "抗跌弱")))
    return {
        "ad_score": ad,
        "detail": dd_rows[:10],
        "avg_dd_fund": round(avg_f, 2),
        "avg_dd_idx": round(avg_i, 2),
        "rep5_avg": round(rep5, 2),
        "rep10_avg": round(rep10, 2),
        "tag": tag,
    }


# ---------------------------------------------------------------------------
# 收益分 / 抗跌分 / 综合分(池内百分位,spec 2.4)
# ---------------------------------------------------------------------------





















