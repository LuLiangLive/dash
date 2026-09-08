"""
modules/logging/middleware.py —— 请求日志中间件

功能：
- 为每个请求生成唯一 request_id（UUID4 短格式）
- 从请求头 X-Request-ID 读取（如有则使用，否则生成）
- 将 request_id 注入响应头 X-Request-ID
- 设置线程局部请求上下文（request_id, user_id, ip, path, method, user_agent）
- 请求结束时记录结构化访问日志（含响应时间、状态码）
- 跳过静态文件和健康检查端点的详细日志
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from logging_config import (
    set_request_context,
    clear_request_context,
    generate_request_id,
    get_logger,
)

logger = get_logger("access")

# 跳过详细日志的路径前缀
_SKIP_PATHS = ("/static/", "/uploads/", "/favicon", "/api/health")


def _get_client_ip(request: Request) -> str:
    """获取客户端真实 IP（优先 X-Forwarded-For，然后 X-Real-IP，最后直接连接 IP）。"""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip
    return request.client.host if request.client else "unknown"


def _get_user_id(request: Request) -> Optional[str]:
    """从请求头或查询参数获取用户标识（如有）。"""
    # 优先 X-User-ID 头
    user_id = request.headers.get("X-User-ID")
    if user_id:
        return user_id
    # API Key 作为用户标识
    api_key = request.headers.get("X-API-Key") or request.query_params.get("api_key")
    if api_key:
        return f"key:{api_key[:8]}..."
    return None


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """请求日志中间件。

    在每个请求开始时生成 request_id 并设置上下文，
    请求结束时记录结构化访问日志。
    """

    async def dispatch(self, request: Request, call_next):
        start_time = time.perf_counter()
        status_code = 500

        # 生成或读取 request_id
        request_id = request.headers.get("X-Request-ID") or generate_request_id()

        # 收集请求上下文
        client_ip = _get_client_ip(request)
        user_id = _get_user_id(request)
        path = request.url.path
        method = request.method
        user_agent = request.headers.get("User-Agent", "")

        # 设置线程局部上下文（供所有 logger 使用）
        set_request_context(
            request_id=request_id,
            user_id=user_id,
            ip=client_ip,
            path=path,
            method=method,
            user_agent=user_agent,
        )

        try:
            response = await call_next(request)
            status_code = response.status_code
            # 注入 request_id 到响应头
            response.headers["X-Request-ID"] = request_id
            return response
        except Exception:
            status_code = 500
            raise
        finally:
            duration_ms = round((time.perf_counter() - start_time) * 1000, 2)

            # 记录访问日志（跳过静态文件等低价值路径）
            if not any(path.startswith(p) for p in _SKIP_PATHS):
                log_kwargs = {
                    "extra": {
                        "status_code": status_code,
                        "duration_ms": duration_ms,
                        "response_size": None,  # 可选：响应大小
                    }
                }

                if status_code >= 500:
                    logger.error("请求处理失败", **log_kwargs)
                elif status_code >= 400:
                    logger.warning("请求客户端错误", **log_kwargs)
                elif duration_ms > 1000:
                    logger.warning("慢请求", **log_kwargs)
                else:
                    logger.info("请求完成", **log_kwargs)

            # 清除线程局部上下文
            clear_request_context()
