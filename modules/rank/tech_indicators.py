"""
modules/rank/tech_indicators.py —— 基金技术指标计算（v2.9.12）

基于净值序列计算技术形态指标，用于榜单筛选和评分。
注意：基金没有成交量，所有指标均基于净值计算。

指标清单：
1. 均线系列：MA5/MA10/MA20
2. 均线多头排列：MA5 > MA10 > MA20
3. 均线粘合：MA5/MA10/MA20 差距 < 阈值
4. 突破N日高点：当前净值 > 近N日最高净值
5. 趋势强度：净值线性回归斜率（年化）
6. 盈利稳定性：净值曲线平滑度（R²）、回撤修复天数
"""

import math
from typing import Optional


def calc_ma(navs: list[tuple[str, float]], period: int) -> Optional[float]:
    """计算N日移动平均净值。

    Args:
        navs: [(date, nav), ...] 按日期升序排列
        period: 均线周期

    Returns:
        最新的MA值，数据不足返回None
    """
    if len(navs) < period:
        return None
    recent = navs[-period:]
    return sum(n for _, n in recent) / period


def is_ma_bullish(navs: list[tuple[str, float]]) -> bool:
    """均线多头排列：MA5 > MA10 > MA20。

    适用于ETF/指数基金，确认趋势健康。
    """
    ma5 = calc_ma(navs, 5)
    ma10 = calc_ma(navs, 10)
    ma20 = calc_ma(navs, 20)
    if ma5 is None or ma10 is None or ma20 is None:
        return False
    return ma5 > ma10 > ma20


def is_ma_converged(navs: list[tuple[str, float]], threshold: float = 0.02) -> bool:
    """均线粘合：MA5/MA10/MA20 相对差距 < 阈值。

    Args:
        navs: 净值序列
        threshold: 相对差距阈值，默认2%（(max-min)/mean < threshold）

    适用于筑底判断：多均线粘合说明多空平衡，变盘在即。
    """
    ma5 = calc_ma(navs, 5)
    ma10 = calc_ma(navs, 10)
    ma20 = calc_ma(navs, 20)
    if ma5 is None or ma10 is None or ma20 is None:
        return False
    mas = [ma5, ma10, ma20]
    mean_ma = sum(mas) / len(mas)
    if mean_ma == 0:
        return False
    rel_range = (max(mas) - min(mas)) / mean_ma
    return rel_range < threshold


def is_breakout_high(navs: list[tuple[str, float]], period: int = 20) -> bool:
    """突破N日高点：当前净值 > 近N日最高净值（不含当日）。

    适用于ETF/指数基金的反弹确认。
    """
    if len(navs) < period + 1:
        return False
    current = navs[-1][1]
    # 近N日（不含当日）的最高净值
    recent_high = max(n for _, n in navs[-(period + 1):-1])
    return current > recent_high


def calc_trend_strength(navs: list[tuple[str, float]], period: int = 20) -> Optional[float]:
    """趋势强度：近N日净值的线性回归斜率（日收益率，百分比）。

    正值=上升趋势，负值=下降趋势，绝对值越大趋势越强。
    类似ADX的简化版，基于净值斜率。

    Returns:
        日均斜率（百分比），如0.15表示日均涨0.15%
    """
    if len(navs) < period:
        return None
    recent = navs[-period:]
    n = len(recent)
    # x = 0,1,2,...,n-1
    # y = nav
    sum_x = sum(range(n))
    sum_y = sum(nav for _, nav in recent)
    sum_xy = sum(i * nav for i, (_, nav) in enumerate(recent))
    sum_x2 = sum(i * i for i in range(n))
    denominator = n * sum_x2 - sum_x * sum_x
    if denominator == 0:
        return None
    slope = (n * sum_xy - sum_x * sum_y) / denominator
    # 转换为百分比斜率（相对于均值）
    mean_y = sum_y / n
    if mean_y == 0:
        return None
    return (slope / mean_y) * 100


def calc_profit_stability(navs: list[tuple[str, float]], period: int = 60) -> Optional[float]:
    """盈利稳定性：净值曲线的线性回归R²（决定系数）。

    R²越接近1，说明净值曲线越稳定（沿趋势线走，波动小）。
    R²低说明净值大起大落，盈利不稳定。

    Returns:
        R²值（0-1），越接近1越稳定
    """
    if len(navs) < period:
        return None
    recent = navs[-period:]
    n = len(recent)
    sum_x = sum(range(n))
    sum_y = sum(nav for _, nav in recent)
    sum_xy = sum(i * nav for i, (_, nav) in enumerate(recent))
    sum_x2 = sum(i * i for i in range(n))
    sum_y2 = sum(nav * nav for _, nav in recent)
    denominator = (n * sum_x2 - sum_x * sum_x) * (n * sum_y2 - sum_y * sum_y)
    if denominator == 0:
        return None
    r = (n * sum_xy - sum_x * sum_y) / math.sqrt(denominator)
    return r * r  # R²


def calc_drawdown_recovery_days(navs: list[tuple[str, float]]) -> Optional[int]:
    """最大回撤修复天数：从最近一次最大回撤谷值恢复到前高所需天数。

    Returns:
        修复天数，若仍在水下返回None
    """
    if len(navs) < 10:
        return None
    # 找最近的最大回撤谷值
    peak = navs[0][1]
    mdd_val = 0
    trough_idx = 0
    peak_idx = 0
    current_peak_idx = 0
    for i, (_, nav) in enumerate(navs):
        if nav > peak:
            peak = nav
            current_peak_idx = i
        dd = (peak - nav) / peak
        if dd > mdd_val:
            mdd_val = dd
            trough_idx = i
            peak_idx = current_peak_idx
    # 从谷值往后找是否恢复到前高
    peak_nav = navs[peak_idx][1]
    for i in range(trough_idx, len(navs)):
        if navs[i][1] >= peak_nav:
            return i - trough_idx
    return None  # 仍在水下


def calc_vol_ratio(navs: list[tuple[str, float]], short: int = 10, long: int = 30) -> Optional[float]:
    """波动率比率：短期波动 / 长期波动。

    <1 表示波动率收窄（筑底信号），>1 表示波动率放大。
    """
    if len(navs) < long + 1:
        return None
    # 计算日收益率
    rets = []
    for i in range(1, len(navs)):
        if navs[i - 1][1] > 0:
            rets.append((navs[i][1] / navs[i - 1][1] - 1) * 100)
    if len(rets) < long:
        return None
    short_rets = rets[-short:]
    long_rets = rets[-long:]
    short_vol = math.sqrt(sum(r * r for r in short_rets) / len(short_rets))
    long_vol = math.sqrt(sum(r * r for r in long_rets) / len(long_rets))
    if long_vol == 0:
        return None
    return short_vol / long_vol
