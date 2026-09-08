"""
security/rate_limiter.py —— API 接口限流模块

使用 slowapi 实现基于 IP 的限流。
限流策略可通过环境变量配置：
- RATE_LIMIT_ENABLED: 是否启用限流（默认 true）
- RATE_LIMIT_DEFAULT: 默认限流策略（默认 "60/minute"）
- RATE_LIMIT_SEARCH: 搜索接口限流（默认 "30/minute"）
- RATE_LIMIT_MARKET: 行情接口限流（默认 "120/minute"）

限流时返回 429 状态码和 Retry-After 头。
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("security.rate_limiter")

# 限流日志记录器
rate_limit_logger = logging.getLogger("rate_limit")


def _env_bool(key: str, default: bool = True) -> bool:
    val = os.environ.get(key)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _env_str(key: str, default: str) -> str:
    return os.environ.get(key, default)


# 限流配置
RATE_LIMIT_ENABLED = _env_bool("RATE_LIMIT_ENABLED", True)
# v2.8.1: 默认 60→120/minute（个人单用户工具，前端自选页/市场页批量刷新在修复后仍可能瞬时多请求，
# 避免自家前端把自家接口限流成 429）
RATE_LIMIT_DEFAULT = _env_str("RATE_LIMIT_DEFAULT", "120/minute")
RATE_LIMIT_SEARCH = _env_str("RATE_LIMIT_SEARCH", "30/minute")
RATE_LIMIT_MARKET = _env_str("RATE_LIMIT_MARKET", "120/minute")
RATE_LIMIT_ALERT = _env_str("RATE_LIMIT_ALERT", "20/minute")
RATE_LIMIT_ADMIN = _env_str("RATE_LIMIT_ADMIN", "10/minute")

# 创建限流器（基于客户端 IP）
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[RATE_LIMIT_DEFAULT] if RATE_LIMIT_ENABLED else [],
    enabled=RATE_LIMIT_ENABLED,
    storage_uri="memory://",  # 内存存储（单进程部署足够）
)


def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded):
    """
    自定义限流异常处理器。
    返回 429 状态码、友好提示和 Retry-After 头。
    同时记录限流日志。
    """
    # 记录限流日志
    client_ip = get_remote_address(request)
    path = request.url.path
    rate_limit_logger.warning(
        "Rate limit exceeded: ip=%s path=%s method=%s limit=%s",
        client_ip,
        path,
        request.method,
        getattr(exc, "limit", "unknown"),
    )

    # 计算 Retry-After（默认 60 秒）
    retry_after = 60
    limit_str = getattr(exc, "limit", "")
    if isinstance(limit_str, str) and "/minute" in limit_str:
        retry_after = 60
    elif isinstance(limit_str, str) and "/second" in limit_str:
        retry_after = 1

    return JSONResponse(
        status_code=429,
        content={
            "ok": False,
            "error": "rate_limit_exceeded",
            "message": "请求过于频繁，请稍后再试",
            "retry_after": retry_after,
        },
        headers={
            "Retry-After": str(retry_after),
            "X-RateLimit-Limit": str(limit_str),
        },
    )


def setup_rate_limiter(app: FastAPI) -> None:
    """
    在 FastAPI 应用上配置限流。
    - 注册限流器状态
    - 注册限流异常处理器
    """
    if not RATE_LIMIT_ENABLED:
        logger.info("API 限流已禁用（RATE_LIMIT_ENABLED=false）")
        return

    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)
    logger.info(
        "API 限流已启用: default=%s, search=%s, market=%s, alert=%s, admin=%s",
        RATE_LIMIT_DEFAULT,
        RATE_LIMIT_SEARCH,
        RATE_LIMIT_MARKET,
        RATE_LIMIT_ALERT,
        RATE_LIMIT_ADMIN,
    )


def get_limiter() -> Limiter:
    """获取限流器实例。"""
    return limiter


def get_rate_limit_config() -> dict:
    """获取限流配置（用于诊断）。"""
    return {
        "enabled": RATE_LIMIT_ENABLED,
        "default": RATE_LIMIT_DEFAULT,
        "search": RATE_LIMIT_SEARCH,
        "market": RATE_LIMIT_MARKET,
        "alert": RATE_LIMIT_ALERT,
        "admin": RATE_LIMIT_ADMIN,
        "storage": "memory",
        "key_func": "client_ip",
    }
