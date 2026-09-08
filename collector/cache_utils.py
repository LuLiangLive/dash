"""
collector/cache_utils.py —— 通用缓存工具函数

从 collector/fetcher.py 拆分（v2.5.5架构重构 - 阶段1）
负责：通用LRU缓存写入工具
"""
import time


def _lru_put(d: dict, code: str, val, maxn: int) -> None:
    """LRU缓存写入：超过上限时淘汰最旧的1/5条目。"""
    if len(d) >= maxn:
        for k in sorted(d, key=lambda k: d[k][0])[: max(1, maxn // 5)]:
            d.pop(k, None)
    d[code] = (time.time(), val)
