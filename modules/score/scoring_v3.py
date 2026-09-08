# -*- coding: utf-8 -*-
"""
modules/score/scoring_v3.py —— 方案C+ 评分体系（v2.1.6）

从 collector.scoring_v3 迁移（v2.5.5架构重构）
线性多因子评分体系（v2.11.2 文案与实际实现校准）：
- 收益分：6周期加权线性（EARN_CYCLE_WEIGHTS），50基准 + 加权收益×3 + 修复奖励(≤5) - 短期暴跌惩罚(≤15)
- 抗跌分：6指标线性（风险指标扣分 + 反弹加分，AD_*_COEFF，反弹加分≤15）
- 卡玛比率百分位：近6月卡玛，最大回撤<1%封顶(CALMAR_DD_CAP=1.0)
- 综合分：收益42.5% + 抗跌42.5% + 卡玛15%（卡玛百分位缺失按中位50）

所有评分均为池内百分位归一，映射至0-100分。
"""
from __future__ import annotations

import math
from typing import Optional


# ============================================================================
# 百分位归一化工具
# ============================================================================

def percentile_rank(values: list, value: float) -> float:
    """计算value在values中的百分位排名（0-100）。

    百分位 = 小于value的元素个数 / 总个数 × 100
    """
    if not values or value is None:
        return 50.0
    valid = [v for v in values if v is not None]
    if not valid:
        return 50.0
    less = sum(1 for v in valid if v < value)
    return (less / len(valid)) * 100.0


def inverse_percentile_rank(values: list, value: float) -> float:
    """反向百分位排名（值越小分越高，用于跌幅/回撤/波动等风险指标）。"""
    if not values or value is None:
        return 50.0
    valid = [v for v in values if v is not None]
    if not valid:
        return 50.0
    greater = sum(1 for v in valid if v > value)
    return (greater / len(valid)) * 100.0


def percentile_to_score(percentile: float) -> float:
    """将百分位排名转换为分段分数（v2.1.6.2调整：更严格的分段归一，高分更难获得）。

    分段规则：
    - 前0.1%：99-100分
    - 前0.5%：97-99分
    - 前1%：95-97分
    - 前2%：92-95分
    - 前5%：85-92分
    - 前10%：78-85分
    - 前20%：70-78分
    - 前40%：60-70分
    - 前60%：50-60分
    - 其他：0-50分

    Args:
        percentile: 百分位排名（0-100），值越大表示排名越靠前

    Returns:
        分段分数（0-100）
    """
    if percentile is None:
        return 50.0

    # percentile是"小于value的元素个数/总个数"，所以percentile越大表示排名越靠前
    # 转换为排名百分比（前X%）
    rank_pct = 100 - percentile  # 前X%，值越小表示排名越靠前

    if rank_pct <= 0.1:
        # 前0.1%：99-100分
        return 99 + (0.1 - rank_pct) / 0.1 * 1
    elif rank_pct <= 0.5:
        # 前0.5%：97-99分
        return 97 + (0.5 - rank_pct) / 0.4 * 2
    elif rank_pct <= 1:
        # 前1%：95-97分
        return 95 + (1 - rank_pct) / 0.5 * 2
    elif rank_pct <= 2:
        # 前2%：92-95分
        return 92 + (2 - rank_pct) / 1 * 3
    elif rank_pct <= 5:
        # 前5%：85-92分
        return 85 + (5 - rank_pct) / 3 * 7
    elif rank_pct <= 10:
        # 前10%：78-85分
        return 78 + (10 - rank_pct) / 5 * 7
    elif rank_pct <= 20:
        # 前20%：70-78分
        return 70 + (20 - rank_pct) / 10 * 8
    elif rank_pct <= 40:
        # 前40%：60-70分
        return 60 + (40 - rank_pct) / 20 * 10
    elif rank_pct <= 60:
        # 前60%：50-60分
        return 50 + (60 - rank_pct) / 20 * 10
    else:
        # 其他：0-50分
        return max(0, 50 - (rank_pct - 60) / 40 * 50)


# ============================================================================
# 收益分（方案C，零改动内部逻辑，仅调整合成方式）
# ============================================================================

# 收益分周期权重（v2.1.9.3调整：增加近期权重，短期65%）
EARN_CYCLE_WEIGHTS = {
    "d3": 0.15,    # 3日
    "d5": 0.20,    # 5日
    "d7": 0.15,    # 7日
    "d10": 0.15,   # 10日
    "m1": 0.20,    # 近1月
    "ret2m": 0.15, # 近2月
}

# 收益分合成权重
EARN_RAW_WEIGHT = 0.60      # 原始收益分权重
EARN_EXCESS_WEIGHT = 0.40   # 超额收益分权重

# 降噪阈值：3日/5日涨幅超过此值时做衰减
EARN_NOISE_THRESHOLD = 8.0   # 8%
EARN_NOISE_DECAY = 0.5       # 衰减系数（超过部分按50%计）

# 短期暴跌惩罚（v2.1.9.2调整：基于自身波动的相对阈值）
EARN_DROP_SIGMA = 0.8          # 0.8倍标准差作为暴跌阈值
EARN_DROP_PENALTY_COEFF = 2.0  # 每超过阈值1%额外扣2分
EARN_DROP_PENALTY_MAX = 15.0   # 最大惩罚15分

# 周期天数映射（用于计算理论波动）
_EARN_CYCLE_DAYS = {
    "d3": 3,
    "d5": 5,
    "d7": 7,
    "d10": 10,
}


def _compute_relative_drop_threshold(vol: float, days: int) -> float:
    """基于基金自身波动率计算相对暴跌阈值。

    Args:
        vol: 年化波动率（%）
        days: 周期天数

    Returns:
        相对阈值（%），负数表示跌幅
    """
    if vol is None or vol <= 0:
        return -5.0  # 波动率缺失时回退到固定阈值
    daily_vol = vol / (252 ** 0.5)  # 日波动率
    cycle_vol = daily_vol * (days ** 0.5)  # 周期理论波动
    return -EARN_DROP_SIGMA * cycle_vol


def _apply_noise_suppression(value: float, threshold: float = EARN_NOISE_THRESHOLD,
                              decay: float = EARN_NOISE_DECAY) -> float:
    """对极端短期脉冲暴涨做分数衰减压制。

    超过阈值的部分按decay系数衰减，避免短期波动虚高分数。
    """
    if value is None:
        return None
    if value <= threshold:
        return value
    return threshold + (value - threshold) * decay


def compute_weighted_return(metrics: dict, excess_metrics: Optional[dict] = None) -> tuple:
    """计算加权原始收益和加权超额收益。

    Returns:
        (weighted_raw, weighted_excess)
    """
    weighted_raw = 0.0
    wsum_raw = 0.0
    for k, w in EARN_CYCLE_WEIGHTS.items():
        v = metrics.get(k)
        if v is None:
            continue
        # 3日、5日做降噪
        if k in ("d3", "d5"):
            v = _apply_noise_suppression(v)
        weighted_raw += v * w
        wsum_raw += w
    weighted_raw = weighted_raw / wsum_raw if wsum_raw else 0.0

    weighted_excess = 0.0
    wsum_excess = 0.0
    if excess_metrics:
        # v2.11.4 Q6: calc_excess_rets 返回 {ex3,ex5,ex7,ex10} 键,
        # 与 EARN_CYCLE_WEIGHTS 的 d3/d5/d7/d10 不匹配, 此前 excess_metrics.get(k)
        # 恒为 None 导致 weighted_excess=0.0; 现按 dN→exN 映射读取。
        # m1/ret2m 无对应超额收益, 跳过。
        for k, w in EARN_CYCLE_WEIGHTS.items():
            if not (k.startswith("d") and k[1:].isdigit()):
                continue
            ex_key = "ex" + k[1:]
            v = excess_metrics.get(ex_key)
            if v is None:
                continue
            if k in ("d3", "d5"):
                v = _apply_noise_suppression(v)
            weighted_excess += v * w
            wsum_excess += w
        weighted_excess = weighted_excess / wsum_excess if wsum_excess else 0.0

    return weighted_raw, weighted_excess


# 收益分线性评分参数（v2.1.6.3调整：从百分位归一线性评分）
EARN_BASE_SCORE = 50.0       # 基准分
EARN_GAIN_COEFF = 3.0        # 1%加权收益→+3分（旧版是4分，调低让高分更难）
EARN_REPAIR_BONUS_MAX = 5.0  # 修复奖励上限（旧版是5分）
EARN_REPAIR_COEFF = 1.0      # 1%反弹→+1分


def compute_earn_score(metrics: dict, pool_raw_returns: list = None,
                       excess_metrics: Optional[dict] = None,
                       pool_excess_returns: Optional[list] = None,
                       repair_5d: Optional[float] = None) -> int:
    """计算收益分（0-100）。

    v2.1.9.1调整：增加短期暴跌惩罚，移除ret3m周期
    公式：收益分 = 50 + 加权收益 × 3 + 修复奖励（最多+5分） - 短期暴跌惩罚（最多-15分）

    Args:
        metrics: 基金指标 {d3, d5, d7, d10, m1, ret2m}
        pool_raw_returns: 池内所有基金的加权原始收益列表（线性评分时不需要，保留兼容）
        excess_metrics: 基金超额收益指标（可选，线性评分时不需要）
        pool_excess_returns: 池内所有基金的加权超额收益列表（线性评分时不需要）
        repair_5d: 大跌后5日平均反弹（用于修复奖励）
    """
    weighted_raw, weighted_excess = compute_weighted_return(metrics, excess_metrics)

    # v2.11.4 Q6: 接入超额收益 40% 权重, excess 缺失时退回纯原始收益(向后兼容)
    if excess_metrics and weighted_excess:
        weighted = EARN_RAW_WEIGHT * weighted_raw + EARN_EXCESS_WEIGHT * weighted_excess
    else:
        weighted = weighted_raw

    # 线性评分：基准分50分，1%加权收益→+3分
    earn = EARN_BASE_SCORE + weighted * EARN_GAIN_COEFF

    # 修复奖励：大跌后反弹能力强的基金加分，最多+5分
    if repair_5d is not None and repair_5d > 0:
        earn += min(repair_5d * EARN_REPAIR_COEFF, EARN_REPAIR_BONUS_MAX)

    # 短期暴跌惩罚：基于基金自身波动率的相对阈值
    drop_penalty = 0.0
    vol = metrics.get("vol")  # 年化波动率
    for k, days in _EARN_CYCLE_DAYS.items():
        v = metrics.get(k)
        if v is None:
            continue
        # 计算相对阈值（基于自身波动率）
        threshold = _compute_relative_drop_threshold(vol, days)
        if v < threshold:
            excess = abs(v) - abs(threshold)
            drop_penalty += excess * EARN_DROP_PENALTY_COEFF
    drop_penalty = min(drop_penalty, EARN_DROP_PENALTY_MAX)
    earn -= drop_penalty

    return int(max(0, min(100, round(earn))))


# ============================================================================
# 抗跌分（方案C+，调整内部权重）
# ============================================================================

# 抗跌分指标权重（v2.1.6调整：降低反弹权重，增加风险指标权重）
AD_METRIC_WEIGHTS = {
    "dd_avg": 0.15,        # 大跌日平均跌幅（反向：跌幅小→高分）
    "repair_5d": 0.15,     # 大跌后5日平均反弹（正向：反弹大→高分）
    "repair_10d": 0.15,    # 大跌后10日平均反弹（正向：反弹大→高分）
    "mdd": 0.25,        # 区间最大回撤（反向：回撤小→高分）
    "down_vol": 0.15,      # 下行波动率（反向：波动小→高分）
    "max_daily_drop": 0.15,        # 单日最大跌幅（反向：跌幅小→高分）
}


# 抗跌分线性评分参数（v2.1.9.1调整：降低反弹权重，提高风险权重，增加反弹上限）
# 目标：P50≈50分，P90≈70分，P10≈30分，避免反弹加分过度
AD_BASE_SCORE = 50.0        # 基准分
# 风险指标：值越小分越高，系数为负
AD_DD_AVG_COEFF = -1.2      # 大跌日平均跌幅：1%跌幅→-1.2分（P50=1.96%）
AD_MDD_COEFF = -0.8      # 最大回撤：1%回撤→-0.8分（P50=13.72%）
AD_DOWN_VOL_COEFF = -0.4    # 下行波动率：1%波动→-0.4分（P50=19.59%）
AD_MAX_DAILY_DROP_COEFF = -0.5      # 单日最大跌幅：1%跌幅→-0.5分（P50=4.80%）
# 反弹指标：值越大分越高，系数为正（修复奖励）
AD_REPAIR_5D_COEFF = 2.0    # 5日反弹：1%反弹→+2分（P50=0.75%）
AD_REPAIR_10D_COEFF = 1.5   # 10日反弹：1%反弹→+1.5分（P50=1.32%）
AD_REPAIR_BONUS_MAX = 15.0  # 反弹加分上限：最多+15分


def compute_ad_score(ad_metrics: dict, pool_ad_metrics: list = None) -> int:
    """计算抗跌分（0-100）。

    v2.1.6.3调整：从百分位归一线性评分，参考旧版算法。
    公式：抗跌分 = 50
                  - 大跌日跌幅×3
                  - 最大回撤×1.5
                  - 下行波动×1
                  - 单日最大跌幅×1
                  + 5日反弹×2
                  + 10日反弹×1.5

    Args:
        ad_metrics: 基金抗跌指标 {dd_avg, repair_5d, repair_10d, mdd, down_vol, max_daily_drop}
        pool_ad_metrics: 池内所有基金的抗跌指标列表（线性评分时不需要，保留兼容）
    """
    ad = AD_BASE_SCORE

    # 风险指标（值越小分越高）
    dd_avg = ad_metrics.get("dd_avg")
    if dd_avg is not None:
        ad += abs(dd_avg) * AD_DD_AVG_COEFF

    mdd = ad_metrics.get("mdd")
    if mdd is not None:
        ad += abs(mdd) * AD_MDD_COEFF

    down_vol = ad_metrics.get("down_vol")
    if down_vol is not None:
        ad += abs(down_vol) * AD_DOWN_VOL_COEFF

    max_daily_drop_val = ad_metrics.get("max_daily_drop")
    if max_daily_drop_val is not None:
        ad += abs(max_daily_drop_val) * AD_MAX_DAILY_DROP_COEFF

    # 反弹指标（值越大分越高，修复奖励，有上限）
    repair_bonus = 0.0
    repair_5d = ad_metrics.get("repair_5d")
    if repair_5d is not None and repair_5d > 0:
        repair_bonus += repair_5d * AD_REPAIR_5D_COEFF

    repair_10d = ad_metrics.get("repair_10d")
    if repair_10d is not None and repair_10d > 0:
        repair_bonus += repair_10d * AD_REPAIR_10D_COEFF

    # 反弹加分上限
    repair_bonus = min(repair_bonus, AD_REPAIR_BONUS_MAX)
    ad += repair_bonus

    return int(max(0, min(100, round(ad))))


# ============================================================================
# 卡玛比率百分位
# ============================================================================

# 卡玛比率边界保护：最大回撤<1%时封顶（v2.1.6.1调整：从2%降到1%，更严格）
CALMAR_DD_CAP = 1.0  # %
CALMAR_CAP_VALUE = 5.0  # 封顶后的卡玛值（从10降到5，更严格）

# 负收益惩罚：收益为负时，卡玛分直接乘以惩罚系数
CALMAR_NEGATIVE_PENALTY = 0.3  # 负收益基金卡玛分乘以0.3


def compute_calmar_6m(metrics: dict) -> Optional[float]:
    """计算近6月卡玛比率。

    近6月卡玛 = 近6月区间收益 / 近6月最大回撤绝对值

    边界保护：当近6月最大回撤<1%时，卡玛比率做封顶处理（更严格）。

    降级方案：当近6月数据不足时，使用近2月数据（ret2m/mdd）计算。
    """
    ret6m = metrics.get("ret6m")
    mdd_6m = metrics.get("mdd_6m")

    # 降级：近6月数据不足时使用近2月
    if ret6m is None:
        ret6m = metrics.get("ret2m")
        mdd_6m = metrics.get("mdd")

    if ret6m is None or mdd_6m is None:
        return None

    abs_dd = abs(mdd_6m)
    if abs_dd < 0.001:
        return CALMAR_CAP_VALUE  # 几乎无回撤，封顶

    # 边界保护：回撤<1%时封顶（更严格）
    if abs_dd < CALMAR_DD_CAP:
        # 用1%作为分母计算封顶值
        return ret6m / CALMAR_DD_CAP

    return ret6m / abs_dd


def compute_calmar_percentile(metrics: dict, pool_calmars: list) -> int:
    """计算卡玛比率百分位（0-100）。

    v2.1.6.1调整：
    - 使用分段归一，让高分更难获得
    - 增加负收益惩罚，收益为负的基金卡玛分乘以0.3

    Args:
        metrics: 基金指标 {ret6m, mdd_6m}
        pool_calmars: 池内所有基金的卡玛比率列表
    """
    calmar = compute_calmar_6m(metrics)
    percentile = percentile_rank(pool_calmars, calmar)
    score = percentile_to_score(percentile)

    # 负收益惩罚：收益为负时，卡玛分乘以惩罚系数
    ret6m = metrics.get("ret6m")
    if ret6m is None:
        ret6m = metrics.get("ret2m")
    if ret6m is not None and ret6m < 0:
        score = score * CALMAR_NEGATIVE_PENALTY

    return int(max(0, min(100, round(score))))


# ============================================================================
# 综合分（方案C+）
# ============================================================================

# 综合分权重（v2.1.6调整：降低卡玛权重，增加收益和抗跌权重）
DUAL_EARN_WEIGHT = 0.425    # 收益分权重
DUAL_AD_WEIGHT = 0.425      # 抗跌分权重
DUAL_CALMAR_WEIGHT = 0.15   # 卡玛比率百分位权重


def compute_dual_score(earn_score: int, ad_score: int, calmar_score: int) -> int:
    """计算综合分（0-100）。

    综合分 = 收益分 × 42.5% + 抗跌分 × 42.5% + 卡玛比率百分位 × 15%
    """
    dual = earn_score * DUAL_EARN_WEIGHT + ad_score * DUAL_AD_WEIGHT + calmar_score * DUAL_CALMAR_WEIGHT
    return int(max(0, min(100, round(dual))))


# ============================================================================
# 批量评分入口
# ============================================================================

def compute_pool_scores(pool: list) -> dict:
    """批量计算池内所有基金的评分。

    Args:
        pool: [{code, metrics:{d3,d5,d7,d10,m1,ret2m,ret6m,mdd_6m,mdd,down_vol,max_daily_drop},
               ad_metrics:{dd_avg,repair_5d,repair_10d}, excess_metrics:{...}}, ...]

    Returns:
        {code: {earn_score, ad_score, calmar_score, dual_score}}
    """
    n = len(pool)
    if n == 0:
        return {}

    # 1. 计算池内加权原始收益和超额收益
    pool_raw_returns = []
    pool_excess_returns = []
    for item in pool:
        metrics = item.get("metrics", {})
        excess = item.get("excess_metrics")
        wr, we = compute_weighted_return(metrics, excess)
        pool_raw_returns.append(wr)
        pool_excess_returns.append(we)

    # 2. 收集池内抗跌指标
    pool_ad_metrics = []
    for item in pool:
        m = item.get("metrics", {})
        am = item.get("ad_metrics", {})
        pool_ad_metrics.append({
            "dd_avg": am.get("dd_avg"),
            "repair_5d": am.get("repair_5d"),
            "repair_10d": am.get("repair_10d"),
            "mdd": m.get("mdd"),
            "down_vol": m.get("down_vol"),
            "max_daily_drop": m.get("max_daily_drop"),
        })

    # 3. 计算池内卡玛比率
    pool_calmars = []
    for item in pool:
        metrics = item.get("metrics", {})
        pool_calmars.append(compute_calmar_6m(metrics))

    # 4. 逐基金计算评分
    results = {}
    for i, item in enumerate(pool):
        code = item.get("code")
        metrics = item.get("metrics", {})
        ad_metrics_full = {
            "dd_avg": item.get("ad_metrics", {}).get("dd_avg"),
            "repair_5d": item.get("ad_metrics", {}).get("repair_5d"),
            "repair_10d": item.get("ad_metrics", {}).get("repair_10d"),
            "mdd": metrics.get("mdd"),
            "down_vol": metrics.get("down_vol"),
            "max_daily_drop": metrics.get("max_daily_drop"),
        }

        earn = compute_earn_score(
            metrics, pool_raw_returns,
            item.get("excess_metrics"), pool_excess_returns,
            repair_5d=item.get("ad_metrics", {}).get("repair_5d")
        )
        ad = compute_ad_score(ad_metrics_full, pool_ad_metrics)
        calmar = compute_calmar_percentile(metrics, pool_calmars)
        dual = compute_dual_score(earn, ad, calmar)

        results[code] = {
            "earn_score": earn,
            "ad_score": ad,
            "calmar_score": calmar,
            "dual_score": dual,
        }

    return results
