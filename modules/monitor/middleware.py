"""
modules/monitor/middleware.py —— APM 性能监控中间件

功能：
- 监控所有 API 请求的响应时间
- 记录慢查询（>1秒）到内存环形缓冲区（最近10000条）
- 统计 API 调用次数和错误率（按端点分组）
- 性能数据存储在内存中，不阻塞主流程
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)

# ── 全局性能数据存储（线程安全）──────────────────────────

_lock = threading.Lock()

# 慢查询环形缓冲区：最近 10000 条
_slow_queries: deque = deque(maxlen=10000)

# 按端点统计：{(method, path): {"count": int, "errors": int, "durations": deque(maxlen=1000)}}
_endpoint_stats: dict[tuple[str, str], dict[str, Any]] = {}

# 全局请求计数
_total_requests = 0
_total_errors = 0

# 慢查询阈值（毫秒）
SLOW_QUERY_THRESHOLD_MS = 1000


def _percentile(sorted_values: list[float], pct: float) -> float:
    """计算百分位数。"""
    if not sorted_values:
        return 0.0
    k = (len(sorted_values) - 1) * pct
    f = int(k)
    c = min(f + 1, len(sorted_values) - 1)
    if f == c:
        return sorted_values[f]
    return sorted_values[f] + (sorted_values[c] - sorted_values[f]) * (k - f)


def record_request(method: str, path: str, duration_ms: float, status_code: int):
    """记录一次请求的性能数据（线程安全）。"""
    global _total_requests, _total_errors
    try:
        with _lock:
            _total_requests += 1
            is_error = status_code >= 500
            if is_error:
                _total_errors += 1

            key = (method, path)
            if key not in _endpoint_stats:
                _endpoint_stats[key] = {
                    "count": 0,
                    "errors": 0,
                    "durations": deque(maxlen=1000),
                }
            stat = _endpoint_stats[key]
            stat["count"] += 1
            if is_error:
                stat["errors"] += 1
            stat["durations"].append(duration_ms)

            # 慢查询记录
            if duration_ms > SLOW_QUERY_THRESHOLD_MS:
                _slow_queries.append({
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
                    "method": method,
                    "path": path,
                    "duration_ms": round(duration_ms, 2),
                    "status_code": status_code,
                })
    except Exception as e:
        logger.debug("APM 记录失败: %s", e)


def get_performance_overview() -> dict:
    """获取性能数据概览（平均响应时间、P50/P95/P99、请求量、错误率，按端点分组）。"""
    try:
        with _lock:
            endpoints = []
            for (method, path), stat in sorted(_endpoint_stats.items()):
                durations = sorted(stat["durations"])
                count = stat["count"]
                errors = stat["errors"]
                avg = sum(durations) / len(durations) if durations else 0.0
                endpoints.append({
                    "endpoint": path,
                    "method": method,
                    "request_count": count,
                    "error_count": errors,
                    "error_rate": round(errors / count * 100, 2) if count > 0 else 0.0,
                    "avg_response_time": round(avg, 2),
                    "p50": round(_percentile(durations, 0.50), 2),
                    "p95": round(_percentile(durations, 0.95), 2),
                    "p99": round(_percentile(durations, 0.99), 2),
                })
            overall_avg = 0.0
            all_durations = []
            for stat in _endpoint_stats.values():
                all_durations.extend(stat["durations"])
            if all_durations:
                overall_avg = sum(all_durations) / len(all_durations)
            return {
                "total_requests": _total_requests,
                "total_errors": _total_errors,
                "overall_error_rate": round(_total_errors / _total_requests * 100, 2) if _total_requests > 0 else 0.0,
                "overall_avg_response_time": round(overall_avg, 2),
                "endpoints": endpoints,
            }
    except Exception as e:
        logger.error("获取性能概览失败: %s", e)
        return {"total_requests": 0, "total_errors": 0, "overall_error_rate": 0.0,
                "overall_avg_response_time": 0.0, "endpoints": []}


def get_slow_queries(limit: int = 20) -> list[dict]:
    """获取慢查询列表。"""
    try:
        with _lock:
            items = list(_slow_queries)
        items.reverse()  # 最新的在前
        return items[:limit]
    except Exception as e:
        logger.error("获取慢查询失败: %s", e)
        return []


def get_error_rate_stats() -> list[dict]:
    """获取错误率统计（按端点分组）。"""
    try:
        with _lock:
            result = []
            for (method, path), stat in sorted(_endpoint_stats.items()):
                count = stat["count"]
                errors = stat["errors"]
                result.append({
                    "time_bucket": time.strftime("%Y-%m-%dT%H:00:00+08:00"),
                    "endpoint": f"{method} {path}",
                    "request_count": count,
                    "error_count": errors,
                    "error_rate": round(errors / count * 100, 2) if count > 0 else 0.0,
                })
            return result
    except Exception as e:
        logger.error("获取错误率统计失败: %s", e)
        return []


class APMMiddleware(BaseHTTPMiddleware):
    """APM 性能监控中间件。

    记录每个请求的响应时间、状态码，异步写入内存统计。
    不阻塞主流程：记录操作在 try-except 中，失败不影响响应。
    """

    async def dispatch(self, request: Request, call_next):
        start = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        except Exception:
            status_code = 500
            raise
        finally:
            duration_ms = (time.perf_counter() - start) * 1000
            # 跳过静态文件和监控自身端点的详细统计（但仍计入总量）
            path = request.url.path
            method = request.method
            # 异步记录不阻塞：直接在 finally 中调用（内存操作极快）
            try:
                record_request(method, path, duration_ms, status_code)
            except Exception:
                pass
