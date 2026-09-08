"""
security/headers.py —— 安全响应头中间件

添加以下安全响应头：
- X-Content-Type-Options: nosniff（防止 MIME 类型嗅探）
- X-Frame-Options: DENY（防止点击劫持）
- Content-Security-Policy: 内容安全策略
- Referrer-Policy: strict-origin-when-cross-origin
- X-XSS-Protection: 1; mode=block（兼容旧浏览器）
- Permissions-Policy: 限制敏感 API 权限
- Strict-Transport-Security: HSTS（仅 HTTPS 环境生效）

CSP 策略说明：
- default-src 'self'：默认只允许同源资源
- script-src 'self' 'unsafe-inline'：允许内联脚本（Vue 应用需要）
- style-src 'self' 'unsafe-inline'：允许内联样式
- img-src 'self' data: https:：允许同源、data URI 和 HTTPS 图片
- connect-src 'self'：允许同源 API 调用
- frame-ancestors 'none'：禁止被嵌入 iframe
"""
from __future__ import annotations

import logging
import os

from fastapi import FastAPI, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

logger = logging.getLogger("security.headers")


def _env_bool(key: str, default: bool = True) -> bool:
    val = os.environ.get(key)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _env_str(key: str, default: str) -> str:
    return os.environ.get(key, default)


# 安全头配置
SECURITY_HEADERS_ENABLED = _env_bool("SECURITY_HEADERS_ENABLED", True)
CSP_ENABLED = _env_bool("CSP_ENABLED", True)
HSTS_ENABLED = _env_bool("HSTS_ENABLED", False)  # 默认关闭，HTTPS 环境开启

# CSP 策略（可通过环境变量覆盖）
CSP_POLICY = _env_str(
    "CSP_POLICY",
    (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: https: blob:; "
        "font-src 'self' data:; "
        "connect-src 'self' ws: wss:; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    ),
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """安全响应头中间件。"""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        response = await call_next(request)

        if not SECURITY_HEADERS_ENABLED:
            return response

        # 防止 MIME 类型嗅探
        response.headers["X-Content-Type-Options"] = "nosniff"

        # 防止点击劫持
        response.headers["X-Frame-Options"] = "DENY"

        # 引用策略
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"

        # XSS 保护（兼容旧浏览器）
        response.headers["X-XSS-Protection"] = "1; mode=block"

        # 权限策略（限制敏感 API）
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), "
            "payment=(), usb=(), bluetooth=(), magnetometer=(), "
            "accelerometer=(), gyroscope=()"
        )

        # 内容安全策略
        if CSP_ENABLED:
            response.headers["Content-Security-Policy"] = CSP_POLICY

        # HSTS（仅 HTTPS 环境建议开启）
        if HSTS_ENABLED:
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )

        return response


def setup_security_headers(app: FastAPI) -> None:
    """在 FastAPI 应用上注册安全头中间件。"""
    if not SECURITY_HEADERS_ENABLED:
        logger.info("安全响应头已禁用（SECURITY_HEADERS_ENABLED=false）")
        return

    # 注意：中间件按注册顺序逆序执行，安全头应在最外层
    # 所以这里先注册，确保它在最外层（最后执行）
    app.add_middleware(SecurityHeadersMiddleware)
    logger.info(
        "安全响应头已启用: CSP=%s, HSTS=%s",
        CSP_ENABLED,
        HSTS_ENABLED,
    )


def get_security_headers_config() -> dict:
    """获取安全头配置（用于诊断）。"""
    return {
        "enabled": SECURITY_HEADERS_ENABLED,
        "csp_enabled": CSP_ENABLED,
        "hsts_enabled": HSTS_ENABLED,
        "csp_policy": CSP_POLICY,
        "headers": [
            "X-Content-Type-Options: nosniff",
            "X-Frame-Options: DENY",
            "Referrer-Policy: strict-origin-when-cross-origin",
            "X-XSS-Protection: 1; mode=block",
            "Permissions-Policy: (restricted)",
            "Content-Security-Policy: (configured)",
        ],
    }
