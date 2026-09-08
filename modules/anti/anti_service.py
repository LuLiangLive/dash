"""
services/anti_service.py —— 抗跌详情相关服务

从 main.py 迁移的抗跌详情相关函数，包括：
- 抗跌弹窗缓存（LRU，30分钟TTL，最大200条）
- 长周期指标缓存（LRU，6小时TTL，最大500条）

设计原则：
- 先迁移相对独立的缓存函数
- 复杂的计算函数（_series、_resist、_rise、_enhance_detail等）暂保留在 main.py
- 未来可逐步迁移更多函数，最终将抗跌详情路由拆分到 api/anti.py
"""
from __future__ import annotations

import time
from typing import Any, Optional

# ---------------------------------------------------------------------------
# 抗跌弹窗缓存 (v0.77.0: TTL提升至10分钟, 添加LRU淘汰, 最大100条)
# v2026-08-28: TTL提升至30分钟, 最大200条, 减少非Fund库基金重复计算
# ---------------------------------------------------------------------------

_ANTI_CACHE: dict[str, tuple[float, dict]] = {}  # code -> (timestamp, result_dict)
_ANTI_CACHE_TTL = 1800  # 30分钟
_ANTI_CACHE_MAX = 200  # 最大缓存条目数


def anti_cache_get(code: str) -> Optional[dict]:
    """获取抗跌详情缓存，过期返回 None。"""
    item = _ANTI_CACHE.get(code)
    if item and (time.time() - item[0]) < _ANTI_CACHE_TTL:
        return item[1]
    if item:
        del _ANTI_CACHE[code]  # 过期删除
    return None


def anti_cache_set(code: str, result: dict) -> None:
    """写入抗跌详情缓存，LRU淘汰最旧条目。"""
    if len(_ANTI_CACHE) >= _ANTI_CACHE_MAX:
        # LRU: 删除最旧的条目
        oldest = min(_ANTI_CACHE.items(), key=lambda x: x[1][0])
        del _ANTI_CACHE[oldest[0]]
    _ANTI_CACHE[code] = (time.time(), result)


def anti_cache_stats() -> dict:
    """获取抗跌缓存统计信息。"""
    return {
        "size": len(_ANTI_CACHE),
        "ttl": _ANTI_CACHE_TTL,
        "max": _ANTI_CACHE_MAX,
    }


# ---------------------------------------------------------------------------
# 长周期指标缓存 (v2026-08-28: 性能优化, 避免重复网络请求)
# fetch_fund_returns 每次网络请求约2秒, 缓存后弹窗/对比加载速度大幅提升
# ---------------------------------------------------------------------------

_LONG_METRICS_CACHE: dict[str, tuple[float, dict]] = {}  # code -> (timestamp, {m1,m3,m6,y1})
_LONG_METRICS_CACHE_TTL = 21600  # 6小时, 阶段涨跌幅变化不频繁
_LONG_METRICS_CACHE_MAX = 500  # 最大缓存条目数


def long_metrics_cache_get(code: str) -> Optional[dict]:
    """获取长周期指标缓存，过期返回 None。"""
    item = _LONG_METRICS_CACHE.get(code)
    if item and (time.time() - item[0]) < _LONG_METRICS_CACHE_TTL:
        return item[1]
    if item:
        del _LONG_METRICS_CACHE[code]
    return None


def long_metrics_cache_set(code: str, data: dict) -> None:
    """写入长周期指标缓存，LRU淘汰最旧条目。"""
    if len(_LONG_METRICS_CACHE) >= _LONG_METRICS_CACHE_MAX:
        oldest = min(_LONG_METRICS_CACHE.items(), key=lambda x: x[1][0])
        del _LONG_METRICS_CACHE[oldest[0]]
    _LONG_METRICS_CACHE[code] = (time.time(), data)


def long_metrics_cache_stats() -> dict:
    """获取长周期指标缓存统计信息。"""
    return {
        "size": len(_LONG_METRICS_CACHE),
        "ttl": _LONG_METRICS_CACHE_TTL,
        "max": _LONG_METRICS_CACHE_MAX,
    }
