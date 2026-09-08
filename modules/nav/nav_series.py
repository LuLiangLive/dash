"""
services/nav_series.py — 净值序列处理服务

从 main.py 拆分，负责基金净值序列的获取、缓存、补拉和长周期指标注入：
- series: 120天净值序列（DB优先，不足时东方财富补拉）
- long_series: 260天长净值序列（带LRU缓存）
- inject_long_metrics: 长周期指标注入（m1/m3/m6/y1/mdd6/mdd1y）
- window_mdd: 窗口最大回撤计算

调用入口：series(code, days=120) -> (dates, vals)
"""
from __future__ import annotations

import time
from typing import Optional

import db

# ---------------------------------------------------------------------------
# 长净值序列缓存（v2.5.5架构重构: 从fund_service.py移到这里，定义和使用在一起）
# ---------------------------------------------------------------------------

# 长净值序列缓存 (v2026-08-29 性能优化: 弹窗加载慢治理)
LONG_SERIES_CACHE: dict = {}  # "code:days" -> (timestamp, [ljjz...])
LONG_SERIES_CACHE_TTL = 21600   # 6 小时(历史净值当日不变, 可长缓存)
LONG_SERIES_CACHE_MAX = 800

# 补拉冷却表: 新基金(成立不足 6 个月)客观上不存在 260 条净值, 补拉必然空手而归。
LONG_FETCH_COOLDOWN: dict = {}  # code -> timestamp
LONG_FETCH_COOLDOWN_TTL = 86400  # 24 小时


def series(code: str, days: int = 120):
    """
    获取基金净值序列（DB优先，不足时东方财富补拉并落库）。
    返回 (dates, vals) 两个列表，长度相同。
    """
    rows = db.get_nav(code, limit=days)
    rows = list(reversed(rows))
    if len(rows) >= 40:
        dates = [r["date"] for r in rows]
        vals = [float(r["dwjz"]) for r in rows]
        return dates, vals
    # DB不足40条，从东方财富补拉
    try:
        from collector import fetcher as _ft
        new_rows = _ft.fetch_nav_history(code, limit=days)
        if new_rows:
            db.upsert_navs(code, new_rows)
            rows = db.get_nav(code, limit=days)
            rows = list(reversed(rows))
    except Exception:
        pass
    if len(rows) < 2:
        return [], []
    dates = [r["date"] for r in rows]
    vals = [float(r["dwjz"]) for r in rows]
    return dates, vals


def long_series(code: str, days: int = 260):
    """
    获取长净值序列（260天），带LRU缓存。
    用于长周期指标计算（m6/y1/mdd6/mdd1y）。
    """
    now = time.time()
    cached = LONG_SERIES_CACHE.get(code)
    if cached and (now - cached["ts"]) < LONG_SERIES_CACHE_TTL:
        return cached["dates"], cached["vals"]
    # 冷却检查：避免短时间内重复网络请求
    cooldown = LONG_FETCH_COOLDOWN.get(code)
    if cooldown and (now - cooldown) < LONG_FETCH_COOLDOWN_TTL:
        # 冷却期内，用DB数据（可能不完整）
        rows = db.get_nav(code, limit=days)
        rows = list(reversed(rows))
        dates = [r["date"] for r in rows]
        vals = [float(r["dwjz"]) for r in rows]
        return dates, vals
    # 从DB获取
    rows = db.get_nav(code, limit=days)
    rows = list(reversed(rows))
    if len(rows) < 60:
        # DB不足，从东方财富补拉
        try:
            from collector import fetcher as _ft
            new_rows = _ft.fetch_nav_history(code, limit=days)
            if new_rows:
                db.upsert_navs(code, new_rows)
                rows = db.get_nav(code, limit=days)
                rows = list(reversed(rows))
                LONG_FETCH_COOLDOWN[code] = now
        except Exception:
            pass
    dates = [r["date"] for r in rows]
    vals = [float(r["dwjz"]) for r in rows]
    # 写入缓存
    if len(LONG_SERIES_CACHE) >= LONG_SERIES_CACHE_MAX:
        # 淘汰最旧的
        oldest = min(LONG_SERIES_CACHE.items(), key=lambda x: x[1]["ts"])
        del LONG_SERIES_CACHE[oldest[0]]
    LONG_SERIES_CACHE[code] = {"ts": now, "dates": dates, "vals": vals}
    return dates, vals


def clear_cache(codes: list = None) -> int:
    """
    清理long_series缓存。

    v2.5.4新增: 封装缓存清理接口，避免其他模块直接操作LONG_SERIES_CACHE全局变量。

    Args:
        codes: 要清理的基金代码列表，为None时清理全部缓存

    Returns:
        清理的缓存条目数
    """
    if codes is None:
        count = len(LONG_SERIES_CACHE)
        LONG_SERIES_CACHE.clear()
        return count
    cleared = 0
    for code in codes:
        if code in LONG_SERIES_CACHE:
            del LONG_SERIES_CACHE[code]
            cleared += 1
    return cleared


def _ret_from_series(vals: list, n: int) -> Optional[float]:
    """从净值序列计算n日收益率（百分比）。"""
    if len(vals) <= n or not vals[0]:
        return None
    return (vals[-1] / vals[-n - 1] - 1) * 100


def _window_mdd(vals: list, window: int, min_data_ratio: float = 0.8) -> Optional[float]:
    """计算窗口内最大回撤（百分比，负数）。
    
    v2.9.7: 增加数据完整性检查，如果数据不足窗口的 min_data_ratio（默认80%），返回None。
    避免用不足的数据冒充长期指标（如用6个月数据冒充1年最大回撤）。
    """
    if len(vals) < 2:
        return None
    # 数据完整性检查：如果数据不足窗口的80%，返回None
    if len(vals) < window * min_data_ratio:
        return None
    tail = vals[-window:] if len(vals) > window else vals
    peak = tail[0]
    mdd = 0.0
    for v in tail:
        if v > peak:
            peak = v
        dd = (v / peak - 1) * 100
        if dd < mdd:
            mdd = dd
    return mdd


def inject_long_metrics(code: str, stats: dict):
    """
    注入长周期指标到stats字典：
    - m1/m3/m6/y1: 优先从pingzhongdata API取，不足时净值序列计算
    - mdd6/mdd1y: 近6月/近1年最大回撤
    6字段LRU缓存，避免重复网络请求。
    """
    if stats is None:
        return
    # 长周期收益指标
    long_keys = ["m1", "m3", "m6", "y1"]
    need_fetch = any(stats.get(k) is None for k in long_keys)
    if need_fetch:
        try:
            from collector import fetcher as _ft
            ping = _ft.fetch_pingzhongdata(code)
            if ping:
                if stats.get("m1") is None and ping.get("syl_1y") is not None:
                    stats["m1"] = ping["syl_1y"]  # 注意：pingzhongdata的syl_1y实际是近1月
                if stats.get("m3") is None and ping.get("syl_3y") is not None:
                    stats["m3"] = ping["syl_3y"]
                if stats.get("m6") is None and ping.get("syl_6y") is not None:
                    stats["m6"] = ping["syl_6y"]
                if stats.get("y1") is None and ping.get("syl_1n") is not None:
                    stats["y1"] = ping["syl_1n"]
        except Exception:
            pass
    # 净值序列计算兜底
    dates, vals = long_series(code, days=260)
    if vals and len(vals) >= 2:
        data_days = len(vals)
        # v2.9.7: 数据完整性标记，记录实际可用数据天数
        stats["_data_days"] = data_days
        
        if stats.get("m1") is None:
            stats["m1"] = _ret_from_series(vals, 21)
        if stats.get("m3") is None:
            stats["m3"] = _ret_from_series(vals, 63)
        if stats.get("m6") is None:
            stats["m6"] = _ret_from_series(vals, 126)
        if stats.get("y1") is None:
            stats["y1"] = _ret_from_series(vals, 252)
        # 长周期最大回撤（v2.9.7: 数据不足时返回None，不再用不足数据冒充）
        if stats.get("mdd6") is None:
            stats["mdd6"] = _window_mdd(vals, 126)
        if stats.get("mdd1y") is None:
            stats["mdd1y"] = _window_mdd(vals, 252)
        
        # v2.9.7: 数据完整性标记，如果数据不足，标注实际计算区间
        if data_days < 252:
            stats["_calc_period"] = f"近{data_days}天（成立不足1年）"
        elif data_days < 504:
            stats["_calc_period"] = f"近{data_days}天"


def window_mdd(vals: list, window: int) -> Optional[float]:
    """公开接口：计算窗口内最大回撤。"""
    return _window_mdd(vals, window)
