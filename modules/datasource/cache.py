"""
modules/datasource/cache.py —— 数据源级缓存

为第三方数据源请求提供内存缓存，避免重复请求同一基金的相同数据。
缓存按 (数据源, 方法, 参数) 维度隔离，支持 TTL 过期。

缓存策略：
- 基金基本信息：6小时 TTL（变化不频繁）
- 最新净值：5分钟 TTL（交易日内更新）
- 历史净值：6小时 TTL（历史数据不变）
- 持仓：24小时 TTL（季度更新）
"""
from __future__ import annotations

import time
from typing import Any, Optional


class DataSourceCache:
    """
    数据源请求缓存（线程安全的内存缓存）。

    用法：
        cache = DataSourceCache()
        cache.set("eastmoney", "fund_info", "000001", data, ttl=21600)
        data = cache.get("eastmoney", "fund_info", "000001")
    """

    # 默认 TTL（秒）
    DEFAULT_TTL = {
        "fund_info": 21600,      # 6小时
        "latest_nav": 300,       # 5分钟
        "nav_history": 21600,    # 6小时
        "holdings": 86400,       # 24小时
        "score": 21600,          # 6小时
    }

    def __init__(self, max_entries: int = 2000):
        self._cache: dict[str, tuple[Any, float]] = {}  # key -> (value, expire_at)
        self._max_entries = max_entries
        self._hits = 0
        self._misses = 0

    def _make_key(self, source: str, method: str, *args) -> str:
        """构建缓存键。"""
        parts = [source, method] + [str(a) for a in args]
        return "|".join(parts)

    def get(self, source: str, method: str, *args) -> Optional[Any]:
        """获取缓存值，过期或不存在返回 None。"""
        key = self._make_key(source, method, *args)
        entry = self._cache.get(key)
        if entry is None:
            self._misses += 1
            return None
        value, expire_at = entry
        if time.time() > expire_at:
            # 过期删除
            del self._cache[key]
            self._misses += 1
            return None
        self._hits += 1
        return value

    def set(self, source: str, method: str, key_arg: str,
            value: Any, ttl: Optional[int] = None) -> None:
        """设置缓存值。

        Args:
            source: 数据源名称
            method: 方法名（fund_info/latest_nav/nav_history/holdings）
            key_arg: 主要参数（通常是基金代码）
            value: 要缓存的值
            ttl: 过期时间（秒），None 则使用默认 TTL
        """
        if ttl is None:
            ttl = self.DEFAULT_TTL.get(method, 21600)
        key = self._make_key(source, method, key_arg)
        self._cache[key] = (value, time.time() + ttl)
        # 缓存容量控制：超过上限时清理过期项
        if len(self._cache) > self._max_entries:
            self._evict_expired()

    def invalidate(self, source: str, method: str, *args) -> None:
        """使指定缓存失效。"""
        key = self._make_key(source, method, *args)
        self._cache.pop(key, None)

    def invalidate_source(self, source: str) -> int:
        """清除指定数据源的所有缓存，返回清除数量。"""
        keys_to_remove = [k for k in self._cache if k.startswith(f"{source}|")]
        for k in keys_to_remove:
            del self._cache[k]
        return len(keys_to_remove)

    def clear(self) -> None:
        """清空所有缓存。"""
        self._cache.clear()
        self._hits = 0
        self._misses = 0

    def _evict_expired(self) -> int:
        """清理所有过期缓存项，返回清理数量。"""
        now = time.time()
        expired = [k for k, (_, exp) in self._cache.items() if now > exp]
        for k in expired:
            del self._cache[k]
        return len(expired)

    def get_stats(self) -> dict:
        """返回缓存统计信息。"""
        total = self._hits + self._misses
        hit_rate = (self._hits / total * 100) if total > 0 else 0
        return {
            "entries": len(self._cache),
            "max_entries": self._max_entries,
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(hit_rate, 1),
        }


# 全局单例
_cache_instance: Optional[DataSourceCache] = None


def get_cache() -> DataSourceCache:
    """获取全局缓存单例。"""
    global _cache_instance
    if _cache_instance is None:
        _cache_instance = DataSourceCache()
    return _cache_instance
