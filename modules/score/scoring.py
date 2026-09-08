# -*- coding: utf-8 -*-
"""
modules/score/scoring.py —— 收益分 / 抗跌分 / 综合分 / 动能5档 评分模块

从 collector.scoring 迁移（v2.5.5架构重构）
从 collector/ranker.py 提取的评分层。

核心函数：
- pool_earn_scores: 池内百分位收益分（旧版，保留兼容）
- compute_earn_scores: 收益分 v0.36（池内前15%基准锚定）
- compute_scores_v2: 抗跌r1统一评分入口（抗跌分已统一为scoring_v3线性评分）
- calc_excess_rets / big_rise_follow: 超额收益与大涨跟随弹性
- detect_yindie_deep: 阴跌/深调识别

设计要点：
- 本模块仅依赖 collector.metrics 中的纯函数，不依赖 ranker 其他模块
- ranking.py 和 recommendation.py 从本模块导入评分函数
"""
from __future__ import annotations

import math
from typing import Optional

from collector.metrics import (
    percentile_scores, inverse_percentile_scores,
    _pstdev, _pct_val, _cdf_phi, _ret_skipna,
    _reverse_pct, _forward_pct, _max_dd,
)
from algo_config import get_algo_config


# ---------------------------------------------------------------------------
# 收益分 / 抗跌分（池内百分位, spec 2.4 旧版）
# ---------------------------------------------------------------------------

def pool_earn_scores(items):
    """收益分 = 池内百分位 × earn_pct_weight + 绝对涨幅映射 × earn_abs_weight。

    加权值 = 各周期涨幅 × 对应权重（从 algo_config 读取，可在设置页面调整）;
    绝对涨幅分 = 加权值 0%→0分, earn_abs_scale%+→100分(线性);
    负收益基金即使排名靠前,绝对分低,收益分被拉回合理区间。
    """
    cfg = get_algo_config()
    w = cfg.earn_weights
    pct_w = cfg.earn_pct_weight
    abs_w = cfg.earn_abs_weight
    abs_scale = cfg.earn_abs_scale

    weighted = []
    for m in items:
        wv = (m.get("d3") or 0) * w["d3"] + (m.get("d5") or 0) * w["d5"] + \
             (m.get("d7") or 0) * w["d7"] + (m.get("d10") or 0) * w["d10"] + \
             (m.get("m1") or 0) * w["m1"] + (m.get("ret2m") or 0) * w["ret2m"]
        weighted.append(wv)
    pctl = percentile_scores(weighted)

    def _abs_earn(wv):
        return min(100, max(0, round(wv * (100.0 / abs_scale))))  # 加权值 0%→0 分, abs_scale%+→100 分

    out = {}
    for i, wv in enumerate(weighted):
        out[i] = round((pctl.get(i, 50) or 50) * pct_w + _abs_earn(wv) * abs_w)
    return out


# ---------------------------------------------------------------------------
# 抗跌r1算法 —— 统一评分模块
# ---------------------------------------------------------------------------

def _up_capture(dates, navs, rise):
    """上涨捕获率:大涨日基金涨幅均值 / 大盘(上证)涨幅均值。
    大涨日 = 上证>+1% 或 创业板>+4% 或 科创50>+4%(rise 由 _build_ddays 产出)。
    用于收益分约束:大涨跑输大盘(捕获率<0.8/0.6)时收益分上限受限。"""
    if not rise or not navs:
        return None
    dmap = dict(zip(dates, navs))
    f_rets, i_rets = [], []
    for r in rise:
        dt = r["date"]
        if dt not in dmap or dt not in dates:
            continue
        idx = dates.index(dt)
        if idx <= 0:
            continue
        prev = navs[idx - 1]
        if not prev:
            continue
        f_rets.append((navs[idx] / prev - 1) * 100)
        i_rets.append(r.get("sh") or 0)
    if not f_rets or not i_rets:
        return None
    f_avg = sum(f_rets) / len(f_rets)
    i_avg = sum(i_rets) / len(i_rets)
    if i_avg <= 0:
        return None
    return f_avg / i_avg
def _align_bench_vals(dates, bench_items):
    """基金日期序列对齐基准(中800)净值:取不晚于该日期的最近基准交易日。"""
    bd = [x[0] for x in bench_items]
    bv = [x[1] for x in bench_items]
    out = []
    j = 0
    for dt in dates:
        while j < len(bd) - 1 and bd[j + 1] <= dt:
            j += 1
        if j < len(bd) and bd[j] <= dt:
            out.append(bv[j])
        else:
            out.append(None)
    return out


def calc_excess_rets(navs, dates, bench_items):
    """基金各周期收益 - 基准(中800)同期收益 → {ex3,ex5,ex7,ex10}。"""
    if not navs or len(navs) < 11 or not bench_items:
        return {}
    bv = _align_bench_vals(dates, bench_items)
    if sum(1 for v in bv if v is not None) < 11:
        return {}
    out = {}
    for k in (3, 5, 7, 10):
        f = _ret_skipna(navs, k)
        b = _ret_skipna(bv, k)
        if f is not None and b is not None:
            out["ex%d" % k] = round(f - b, 2)
    return out


def big_rise_follow(navs, dates, idx_map):
    """大涨跟随弹性:近10日内大盘(上证)涨幅≥1%的交易日,基金平均涨幅/大盘平均涨幅。
    idx_map 为 _build_idx_map 产出(daily_ret: [(date, {sh,sz,kc})], 与指数日期对齐)。"""
    daily = idx_map.get("daily_ret", [])
    if not daily:
        return None
    recent = daily[-10:]
    dmap = {dt: i for i, dt in enumerate(dates)}
    pairs = []
    for dt, rets in recent:
        sh = rets.get("sh")
        if sh is not None and sh >= 1.0:
            j = dmap.get(dt)
            if j is not None and j > 0 and j < len(navs):
                f_ret = (navs[j] / navs[j - 1] - 1) * 100
                pairs.append((f_ret, sh))
    if not pairs:
        return None
    f_avg = sum(p[0] for p in pairs) / len(pairs)
    i_avg = sum(p[1] for p in pairs) / len(pairs)
    if i_avg <= 0:
        return None
    return round(f_avg / i_avg, 3)




# ---------------------------------------------------------------------------
# 抗跌分 v2 + 阴跌识别 + 统一评分入口
# ---------------------------------------------------------------------------

def _fund_bench_series(pool):
    """池内类基准日序列:每天池内基金平均净值涨幅。
    返回 {date: 累计涨幅%} (基准从第一共同日归一为0)。日期取池内共同交易日。
    """
    # 按日期聚合池内每只基金当日日涨幅均值(简单均值)
    daily = {}
    for m in pool:
        dates = m.get("dates") or []
        navs = m.get("navs") or []
        if len(dates) < 2 or len(navs) < 2:
            continue
        for i in range(1, len(dates)):
            prev, cur = navs[i - 1], navs[i]
            if prev:
                daily.setdefault(dates[i], []).append((cur - prev) / prev * 100)
    if not daily:
        return {}
    return {dt: (sum(v) / len(v)) for dt, v in sorted(daily.items())}


def detect_yindie_deep(pool, bench_series=None):
    """阴跌(已完成回撤)与进行中深调识别。
    阴跌(同时满足):1.mdd>5% 2.mdd_days>20 且已创新高(修复) 3.回撤区间内 基金跌幅−基准>2%
    进行中深调(同时满足):1.当前回撤>5%(未修复) 2.回撤区间(起点→今日) 基金跌幅−基准>3%
    基准:池内类基准(日序列累计)。
    返回 {code: (flag, detail)} flag ∈ 阴跌/进行中深调/None。
    """
    bench_series = bench_series or _fund_bench_series(pool)
    if not bench_series:
        return {}
    bdates = sorted(bench_series.keys())
    # 基准累计:从第一日=0
    bench_cum = {}
    acc = 0.0
    for dt in bdates:
        acc += bench_series[dt]
        bench_cum[dt] = acc
    out = {}
    for m in pool:
        code = m["code"]
        mm = m.get("metrics", {})
        navs = m.get("navs") or []
        dates = m.get("dates") or []
        if len(navs) < 2 or len(dates) < 2:
            continue
        pts = list(zip(dates, navs))
        mdd = mm.get("mdd")
        mdd_days = mm.get("mdd_days")
        mdd_status = mm.get("mdd_status")
        dd20 = mm.get("dd_from_hi")
        if mdd is None or mdd >= -0.5:
            continue
        # 回撤区间 = 最大回撤段 [peak日, trough日](用 _max_dd 的 seg)
        _dd, _days, _st, seg = _max_dd(navs)
        if not seg:
            continue
        seg_start, seg_end = seg
        # 基金区间跌幅:起点=峰值日,终点=最低点(已完成) 或 最新(进行中)
        fund_seg = (navs[seg_end] / navs[seg_start] - 1) * 100 if navs[seg_start] else None
        if fund_seg is None:
            continue
        # 基准区间涨幅(同日期)
        d0, d1 = dates[seg_start], dates[seg_end]
        b0 = bench_cum.get(d0)
        b1 = bench_cum.get(d1)
        bench_seg = (b1 - b0) if (b0 is not None and b1 is not None) else 0.0
        # 超额 = 基金跌幅 − 基准跌幅(基金相对基准多跌多少,正数=跑输基准)
        excess = abs(fund_seg) - abs(bench_seg) if fund_seg is not None else 0.0
        # 已完成回撤的阴跌
        if abs(mdd) > 5 and (mdd_days or 0) > 20 and mdd_status == "已修复" and excess > 2:
            out[code] = ("阴跌", {"seg_ret": round(fund_seg, 2), "bench_seg": round(bench_seg, 2),
                                  "excess": round(excess, 2), "mdd": round(mdd, 2), "days": mdd_days})
        # 进行中深调(未修复):当前回撤极深(距峰值>15%)且大幅跑输基准(多跌>10%)
        elif dd20 is not None and dd20 < -15 and mdd_status == "修复中":
            seg_end2 = len(navs) - 1
            fund_seg2 = (navs[seg_end2] / navs[seg_start] - 1) * 100 if navs[seg_start] else None
            d1b = dates[seg_end2]
            b1b = bench_cum.get(d1b)
            bench_seg2 = (b1b - b0) if (b0 is not None and b1b is not None) else 0.0
            excess2 = abs(fund_seg2) - abs(bench_seg2) if fund_seg2 is not None else 0.0
            if fund_seg2 is not None and excess2 > 10:
                out[code] = ("进行中深调", {"seg_ret": round(fund_seg2, 2), "bench_seg": round(bench_seg2, 2),
                                            "excess": round(excess2, 2), "cur_dd": round(dd20, 2)})
    return out


def compute_scores_v2(pool):
    """抗跌r1统一评分入口。pool: [{code, metrics, resist, navs, dates, is_etf, name}]
    返回 (earn_map, ad_map, yindie_map):
      earn_map: {code: earn_score}
      ad_map:   {code: ad_score}
      yindie_map: {code: (flag, detail)}
    v2.9.60: 收益分和抗跌分全部统一使用scoring_v3线性评分
    """
    yindie = detect_yindie_deep(pool)
    # v2.9.60: 收益分统一使用scoring_v3线性评分（替代旧版compute_earn_scores百分位加权）
    from modules.score.scoring_v3 import compute_earn_score as _v3_compute_earn_score
    from modules.score.scoring_v3 import compute_ad_score as _v3_compute_ad_score
    earn_map = {}
    for m in pool:
        _metrics = m.get("metrics") or {}
        earn_map[m["code"]] = _v3_compute_earn_score(_metrics)
    # v2.9.57: 抗跌分统一使用scoring_v3线性评分（替代旧版compute_ad_scores_v2百分位+阴跌惩罚）
    ad_map = {}
    for m in pool:
        _resist = m.get("resist") or {}
        _metrics = m.get("metrics") or {}
        _ad_metrics = {
            "dd_avg": _resist.get("avg_dd_fund"),
            "repair_5d": _resist.get("rep5_avg"),
            "repair_10d": _resist.get("rep10_avg"),
            "mdd": _metrics.get("mdd"),
            "down_vol": _metrics.get("down_vol"),
            "max_daily_drop": _metrics.get("max_daily_drop"),
        }
        ad_map[m["code"]] = _v3_compute_ad_score(_ad_metrics)
    return earn_map, ad_map, yindie
