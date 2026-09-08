"""
fund_detail.py —— 基金详情纯计算函数

从 main.py 提取的无外部依赖计算层，包含：
- 统计指标：stats_full, window_mdd
- 标签判定：ad_tag, momentum_decay_tag, rise_source_tag, dd_type_tag
- 反弹弹性：rebound_avg

设计要点：
- 本模块仅依赖输入参数，不导入 db / collector / main 内部函数
- main.py 的 anti_detail / watch_compare 从本模块导入这些函数
- 有外部依赖的函数(_resist, _rise, _enhance_detail, _reco_history)暂保留在 main.py
"""
from __future__ import annotations


def stats_full(vals):
    """常规指标 12 项(与线上版一致)。"""
    n = len(vals)
    if n < 2:
        return None
    dret = [vals[i] / vals[i - 1] - 1 for i in range(1, n)]
    ret2m = (vals[-1] / vals[0] - 1) * 100
    max_daily_drop = min(dret) * 100
    peak = vals[0]
    mdd = 0.0
    for v in vals:
        if v > peak:
            peak = v
        dd = (v / peak - 1) * 100
        if dd < mdd:
            mdd = dd
    mean = sum(dret) / len(dret)
    var = sum((x - mean) ** 2 for x in dret) / (len(dret) - 1)
    sd = var ** 0.5
    vol = sd * (252 ** 0.5) * 100 if sd > 0 else 0.0
    sharpe = (mean * 252 - 0.02) / (sd * (252 ** 0.5)) if sd > 0 else 0.0

    def seg(k):
        return (vals[-1] / vals[-1 - k] - 1) * 100 if n > k else None

    r1m, r2w, r5d = seg(21), seg(10), seg(5)
    up_n = sum(1 for x in dret if x > 0)
    dn_n = sum(1 for x in dret if x < 0)
    up_ratio = up_n / len(dret)
    gains = [x for x in dret if x > 0]
    losses = [x for x in dret if x < 0]
    pl = (sum(gains) / len(gains)) / abs(sum(losses) / len(losses)) if gains and losses else None
    hi = vals[0]
    hi_cnt = 0
    for v in vals[1:]:
        if v > hi:
            hi = v
            hi_cnt += 1
    return {"ret2m": round(ret2m, 2), "max_daily_drop": round(max_daily_drop, 2), "mdd": round(mdd, 2),
            "vol": round(vol, 2), "r1m": round(r1m, 2) if r1m is not None else None,
            "r2w": round(r2w, 2) if r2w is not None else None,
            "r5d": round(r5d, 2) if r5d is not None else None,
            "sharpe": round(sharpe, 2), "up_ratio": round(up_ratio * 100, 1),
            "pl": round(pl, 2) if pl is not None else None, "hi_cnt": hi_cnt}


def window_mdd(vals):
    """窗口内最大回撤(%,负值)。"""
    if not vals:
        return None
    peak = vals[0]
    mdd = 0.0
    for v in vals:
        if v > peak:
            peak = v
        dd = (v / peak - 1) * 100
        if dd < mdd:
            mdd = dd
    return mdd


def ad_tag(score) -> str:
    """抗跌分标签阈值校准:≥80 抗跌强 / 60-79 抗跌良好 / 40-59 抗跌一般 / <40 抗跌弱。"""
    if score is None:
        return "—"
    if score >= 80:
        return "抗跌强"
    if score >= 60:
        return "抗跌良好"
    if score >= 40:
        return "抗跌一般"
    return "抗跌弱"


def momentum_decay_tag(st):
    """动能衰减标签:中期(7日/2周)仍强,但短期(3日/5日)明显收窄甚至转负。"""
    if not st:
        return None
    try:
        d3 = st.get("d3") if st.get("d3") is not None else None
        d5 = st.get("d5")
        d7 = st.get("d7")
        d10 = st.get("d10")
        if None in (d3, d5, d7, d10):
            return None
        mid = (d7 + d10) / 2
        short = (d3 + d5) / 2
        if mid >= 2 and short < mid - 2:
            return "动能衰减"
        if mid >= 3 and short < 0:
            return "动能衰减"
    except (TypeError, ValueError):
        pass
    return None


def rise_source_tag(rise, st):
    """大涨来源标签:基于大涨日明细 + 区间涨幅。"""
    det = (rise or {}).get("detail") or []
    if not det:
        return None
    ret2m = st.get("ret2m")
    if not ret2m or ret2m < 3:
        return None
    funds = sorted((x.get("fund") or 0) for x in det if x.get("fund") is not None)
    if not funds:
        return None
    if len(funds) >= 2 and sum(funds[-2:]) >= ret2m * 0.6:
        return "少数大阳线驱动"
    if len(funds) >= 3 and max(funds) < ret2m * 0.4:
        return "多日稳步上涨"
    return None


def dd_type_tag(resist):
    """大跌类型标签:基于大跌日明细。"""
    det = (resist or {}).get("detail") or []
    if len(det) == 0:
        return None
    funds = [x.get("fund") for x in det if x.get("fund") is not None]
    if not funds:
        return None
    worst = min(funds)
    if len(det) <= 2 or worst <= -3:
        return "一次性集中暴跌"
    return "连续阴跌下台阶"


def rebound_avg(dates, vals, resist):
    """大跌后平均反弹弹性:每个大跌日之后 N 个交易日的平均反弹幅度。返回 {n5, n10}。"""
    det = (resist or {}).get("detail") or []
    if not det or len(vals) < 2:
        return {"n5": None, "n10": None}
    out = {}
    for lab, n in (("n5", 5), ("n10", 10)):
        rebs = []
        for x in det:
            d = x.get("date")
            if d not in dates:
                continue
            i0 = dates.index(d)
            i1 = min(i0 + n, len(vals) - 1)
            if i1 > i0 and vals[i0]:
                rebs.append((vals[i1] / vals[i0] - 1) * 100)
        out[lab] = round(sum(rebs) / len(rebs), 2) if rebs else None
    return out
