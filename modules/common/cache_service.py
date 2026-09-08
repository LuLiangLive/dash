"""
cache_service.py —— 统一缓存抽象层

提供统一的缓存接口，支持 TTL 过期、LRU 淘汰、命名空间批量失效。
后端可插拔：MemoryBackend（默认，单进程）/ RedisBackend（多实例共享，未来扩展）。

使用方式:
    from services.cache_service import cache

    # 基本操作
    cache.set("fund:detail:000001", data, ttl=1800)
    data = cache.get("fund:detail:000001")

    # 命名空间批量失效
    cache.invalidate_namespace("fund_html")

    # 统计信息
    stats = cache.stats()

设计要点:
- 所有缓存统一走这个接口，消除12套独立缓存的重复代码
- key 用命名空间分隔（如 "anti:000001"、"long_nav:000001:260"）
- 线程安全（threading.Lock）
- 命中率统计，便于观测
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any, Optional

from config import settings


# ---------------------------------------------------------------------------
# v2.6.0 优化: 分级TTL缓存策略（按数据变化频率精细调整）
# 实时数据：秒级-分钟级；当日数据：分钟级-小时级；历史/静态：天级
# ---------------------------------------------------------------------------
CACHE_TTL_CONFIG = {
    # 实时数据：短TTL（秒级-分钟级）
    'realtime': 30,              # 实时报价 30秒（v2.6.0: 10s→30s，平衡刷新频率与API压力）
    'quote': 60,                  # 行情报价 60秒（v2.6.0: 30s→60s）
    'minute': 300,                # 分钟K线 5分钟
    'daily_nav': 60,              # 当日净值 60秒（v2.6.0: 24h→60s，净值是实时核心数据）
    'market': 30,                 # 市场行情 30秒（与config.market_cache_ttl一致）

    # 当日榜单/评分：中等TTL（5-30分钟，每日重算后变化）
    'ranking': 600,               # 榜单数据 10分钟（v2.6.0: 24h→10min，榜单每日更新但需及时反映）
    'rank': 600,                  # 榜单数据 10分钟
    'score': 1800,                # 评分数据 30分钟（v2.6.0: 24h→30min，评分每日重算）
    'signal': 1800,               # 信号数据 30分钟

    # 历史数据：长TTL（天级）
    'history_nav': 604800,        # 历史净值 7天
    'nav_history': 604800,        # 历史净值 7天
    'fund_detail': 86400,         # 基金详情 1天（v2.6.0: 7天→1天，详情含持仓等会变化的数据）
    'anti': 86400,                # 抗跌分析 1天（v2.6.0: 7天→1天）
    'compare': 86400,             # 对比数据 1天
    'metrics': 86400,             # 指标数据 1天

    # 静态/基础信息：较长TTL（小时级-天级，非永久避免数据陈旧）
    'fund_basic': 86400,          # 基金基础信息 1天（v2.6.0: 永久→1天，基金经理/规模会变）
    'basic': 86400,               # 基础信息 1天
    'holiday': None,              # 节假日历 永久（不变）
    'calendar': None,             # 交易日历 永久（不变）
}

# 默认TTL（30分钟）
DEFAULT_TTL = 1800


def get_ttl_by_namespace(key: str) -> Optional[int]:
    """根据key的命名空间自动获取分级TTL。

    匹配规则：从最长的命名空间开始匹配，找到第一个匹配的配置。
    例如 key='fund_detail:000001' 会匹配到 'fund_detail' 的TTL。

    Args:
        key: 缓存键

    Returns:
        TTL秒数，None表示永久不过期
    """
    if not key:
        return DEFAULT_TTL

    # 按命名空间长度降序排列，优先匹配更长的命名空间
    for namespace in sorted(CACHE_TTL_CONFIG.keys(), key=len, reverse=True):
        if key.startswith(namespace) or key.startswith(f"{namespace}:"):
            return CACHE_TTL_CONFIG[namespace]

    return DEFAULT_TTL


class CacheBackend:
    """缓存后端基类。"""

    def get(self, key: str) -> Optional[Any]:
        raise NotImplementedError

    def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        raise NotImplementedError

    def delete(self, key: str) -> bool:
        raise NotImplementedError

    def clear(self) -> None:
        raise NotImplementedError

    def keys(self, pattern: Optional[str] = None) -> list:
        raise NotImplementedError


class MemoryBackend(CacheBackend):
    """内存缓存后端，支持 TTL 和 LRU 淘汰。"""

    def __init__(self, max_size: int = 1000):
        self._store: OrderedDict[str, tuple] = OrderedDict()  # key -> (value, expire_at)
        self._max_size = max_size
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            item = self._store.get(key)
            if item is None:
                self._misses += 1
                return None
            value, expire_at = item
            if expire_at is not None and time.time() > expire_at:
                del self._store[key]
                self._misses += 1
                return None
            # LRU: 移动到末尾（最近使用）
            self._store.move_to_end(key)
            self._hits += 1
            return value

    def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        with self._lock:
            expire_at = time.time() + ttl if ttl is not None else None
            if key in self._store:
                self._store.move_to_end(key)
            self._store[key] = (value, expire_at)
            # LRU 淘汰
            while len(self._store) > self._max_size:
                self._store.popitem(last=False)

    def delete(self, key: str) -> bool:
        with self._lock:
            if key in self._store:
                del self._store[key]
                return True
            return False

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    def keys(self, pattern: Optional[str] = None) -> list:
        with self._lock:
            if pattern is None:
                return list(self._store.keys())
            # 简单的前缀匹配（pattern 以 * 结尾）
            if pattern.endswith("*"):
                prefix = pattern[:-1]
                return [k for k in self._store.keys() if k.startswith(prefix)]
            return [k for k in self._store.keys() if k == pattern]

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._store)

    @property
    def hits(self) -> int:
        return self._hits

    @property
    def misses(self) -> int:
        return self._misses


class CacheService:
    """统一缓存服务。"""

    def __init__(self, backend: Optional[CacheBackend] = None):
        # v2.6.0: 缓存上限从2000提升到5000，适配6000+基金库的多命名空间缓存需求
        self._backend = backend or MemoryBackend(max_size=5000)
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[Any]:
        """获取缓存值。"""
        return self._backend.get(key)

    def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        """设置缓存值，ttl 单位秒。

        v2.5.3: 当 ttl 为 None 时，自动根据 key 的命名空间获取分级TTL。
        例如 key='fund_detail:000001' 会自动使用 fund_detail 的TTL（7天）。
        """
        if ttl is None:
            ttl = get_ttl_by_namespace(key)
        self._backend.set(key, value, ttl)

    def get_or_set(self, key: str, loader, ttl: Optional[int] = None) -> Any:
        """获取缓存，不存在时调用 loader 加载并缓存。"""
        value = self.get(key)
        if value is None:
            value = loader()
            if value is not None:
                self.set(key, value, ttl)
        return value

    def delete(self, key: str) -> bool:
        """删除缓存键。"""
        return self._backend.delete(key)

    def invalidate_namespace(self, namespace: str) -> int:
        """按命名空间批量失效（删除所有以 namespace: 开头的键）。"""
        keys = self._backend.keys(f"{namespace}:*")
        count = 0
        for key in keys:
            if self._backend.delete(key):
                count += 1
        return count

    def clear(self) -> None:
        """清空所有缓存。"""
        self._backend.clear()

    def stats(self) -> dict:
        """缓存统计信息。"""
        backend = self._backend
        hits = getattr(backend, "hits", 0)
        misses = getattr(backend, "misses", 0)
        total = hits + misses
        hit_rate = (hits / total * 100) if total > 0 else 0
        return {
            "size": getattr(backend, "size", 0),
            "hits": hits,
            "misses": misses,
            "hit_rate": round(hit_rate, 2),
            "backend": type(backend).__name__,
        }


# 全局单例
cache = CacheService()


# ---------------------------------------------------------------------------
# v2.6.0: 缓存预热 —— 系统启动时预加载热门数据，避免首次请求冷启动延迟
# ---------------------------------------------------------------------------
def preheat_cache() -> dict:
    """预热缓存：预加载高频访问数据。

    预热内容：
    1. 最新净值日期、最新榜单日期（每次页面加载都查询）
    2. 基金更新统计（首页展示）
    3. 综合分Top50基金基础信息（榜单/推荐高频访问）

    Returns:
        预热结果统计 dict
    """
    import time
    stats = {"items_loaded": 0, "errors": 0, "duration_ms": 0}
    t0 = time.time()

    try:
        import db
        # 1. 日期类查询（触发内部缓存）
        db.latest_nav_date()
        db.latest_rank_date()
        stats["items_loaded"] += 2

        # 2. 基金更新统计
        db.get_fund_update_stats()
        stats["items_loaded"] += 1

        # 3. Top50 基金基础信息预热（按综合分排序）
        try:
            top_funds = db.list_funds(limit=50)
            for f in top_funds:
                code = f.get("code")
                if code:
                    cache.set(f"fund_basic:{code}", f, ttl=get_ttl_by_namespace("fund_basic"))
            stats["items_loaded"] += len(top_funds)
        except Exception:
            stats["errors"] += 1

    except Exception as e:
        stats["errors"] += 1

    stats["duration_ms"] = round((time.time() - t0) * 1000, 1)
    return stats


# ── 便捷函数：命名空间缓存 ──────────────────────────────────

class NamespacedCache:
    """命名空间缓存，自动给 key 添加前缀。"""

    def __init__(self, namespace: str, default_ttl: Optional[int] = None):
        self._namespace = namespace
        self._default_ttl = default_ttl

    def _key(self, key: str) -> str:
        return f"{self._namespace}:{key}"

    def get(self, key: str) -> Optional[Any]:
        return cache.get(self._key(key))

    def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        cache.set(self._key(key), value, ttl or self._default_ttl)

    def delete(self, key: str) -> bool:
        return cache.delete(self._key(key))

    def invalidate_all(self) -> int:
        """失效该命名空间下的所有缓存。"""
        return cache.invalidate_namespace(self._namespace)


# 预定义的命名空间缓存（对应原有的12套缓存）
html_cache = NamespacedCache("fund_html", default_ttl=None)  # 按任务ID失效，不设TTL
anti_cache = NamespacedCache("anti", default_ttl=settings.anti_cache_ttl)
long_series_cache = NamespacedCache("long_series", default_ttl=settings.long_series_cache_ttl)
long_metrics_cache = NamespacedCache("long_metrics", default_ttl=settings.long_metrics_cache_ttl)
pz_cache = NamespacedCache("pz", default_ttl=settings.pz_cache_ttl)
pz_nav_cache = NamespacedCache("pz_nav", default_ttl=settings.pz_nav_cache_ttl)
watch_detail_cache = NamespacedCache("watch_detail", default_ttl=settings.watch_detail_cache_ttl)
holdings_cache = NamespacedCache("holdings", default_ttl=settings.holdings_cache_ttl)
market_cache = NamespacedCache("market", default_ttl=settings.market_cache_ttl)
extreme_cache = NamespacedCache("extreme", default_ttl=None)  # 进程内永久缓存
long_fetch_cooldown = NamespacedCache("fetch_cooldown", default_ttl=settings.long_fetch_cooldown_ttl)
live_ctx_cache = NamespacedCache("live_ctx", default_ttl=1800)


# ---------------------------------------------------------------------------
# 首页渲染缓存（v0.95.1 perf: 避免每次请求都重新渲染954KB HTML）
# ---------------------------------------------------------------------------
_index_cache: str | None = None
_index_cache_ts: float = 0
_INDEX_CACHE_TTL = 30  # 30秒缓存


def get_index_cache() -> str | None:
    """获取首页缓存，如果未过期则返回缓存内容，否则返回None。"""
    global _index_cache, _index_cache_ts
    import time
    if _index_cache is not None and (time.time() - _index_cache_ts) < _INDEX_CACHE_TTL:
        return _index_cache
    return None


def set_index_cache(html: str) -> None:
    """设置首页缓存。"""
    global _index_cache, _index_cache_ts
    import time
    _index_cache = html
    _index_cache_ts = time.time()


def invalidate_index_cache() -> None:
    """失效首页缓存（自选列表变化、榜单更新时调用）。"""
    global _index_cache, _index_cache_ts
    _index_cache = None
    _index_cache_ts = 0
