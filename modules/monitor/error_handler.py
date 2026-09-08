"""
modules/monitor/error_handler.py —— 全局错误处理

功能：
- 全局异常捕获中间件
- 记录错误详情到 SQLite（monitor_errors 表）
- 错误告警阈值：5分钟内同一端点错误 > 10次触发告警
"""
from __future__ import annotations

import logging
import time
import traceback
from collections import defaultdict, deque
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

# ── 错误频率追踪（用于告警阈值判断）──────────────────────

_error_timestamps: dict[str, deque] = defaultdict(lambda: deque(maxlen=1000))
ERROR_ALERT_WINDOW_SECONDS = 300  # 5分钟
ERROR_ALERT_THRESHOLD = 10         # 同一端点5分钟内超过10次错误


def _get_client_ip(request: Request) -> str:
    """获取客户端 IP。"""
    try:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[0].strip()
        return request.client.host if request.client else "unknown"
    except Exception:
        return "unknown"


def log_error(
    message: str,
    tb: str = "",
    method: str = "",
    path: str = "",
    params: str = "",
    client_ip: str = "",
    user_agent: str = "",
    level: str = "ERROR",
):
    """记录一条错误到数据库。"""
    try:
        import db as _db
        conn = _db.get_conn()
        conn.execute(
            """INSERT INTO monitor_errors
               (timestamp, level, message, traceback, method, path, params, client_ip, user_agent, resolved)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
            (
                time.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
                level,
                message[:2000],
                tb[:8000] if tb else "",
                method,
                path,
                params[:2000] if params else "",
                client_ip,
                user_agent[:500] if user_agent else "",
            ),
        )
        conn.commit()
    except Exception as e:
        logger.debug("错误日志写入失败: %s", e)


def _check_error_alert(endpoint: str) -> bool:
    """检查是否触发错误告警阈值。返回是否触发。"""
    try:
        now = time.time()
        dq = _error_timestamps[endpoint]
        dq.append(now)
        # 清理窗口外的时间戳
        while dq and now - dq[0] > ERROR_ALERT_WINDOW_SECONDS:
            dq.popleft()
        return len(dq) > ERROR_ALERT_THRESHOLD
    except Exception:
        return False


def trigger_alert(alert_type: str, severity: str, message: str, source: str = "error_handler"):
    """触发一条告警（写入 monitor_alerts 表）。"""
    try:
        from modules.monitor.service import create_alert
        create_alert(alert_type=alert_type, severity=severity, message=message, source=source)
    except Exception as e:
        logger.debug("告警写入失败: %s", e)


class GlobalErrorHandlerMiddleware(BaseHTTPMiddleware):
    """全局异常处理中间件。

    捕获所有未处理异常，记录详情到数据库，返回统一 JSON 错误响应。
    监控功能自身失败不影响主应用。
    """

    async def dispatch(self, request: Request, call_next):
        try:
            return await call_next(request)
        except Exception as exc:
            # 记录错误
            tb_str = traceback.format_exc()
            method = request.method
            path = request.url.path
            client_ip = _get_client_ip(request)
            user_agent = request.headers.get("user-agent", "")

            # 收集请求参数
            params = ""
            try:
                if request.query_params:
                    params = str(dict(request.query_params))
            except Exception:
                pass

            log_error(
                message=str(exc)[:2000],
                tb=tb_str,
                method=method,
                path=path,
                params=params,
                client_ip=client_ip,
                user_agent=user_agent,
            )

            # 检查告警阈值
            endpoint = f"{method} {path}"
            if _check_error_alert(endpoint):
                trigger_alert(
                    alert_type="error_rate",
                    severity="critical",
                    message=f"端点 {endpoint} 在5分钟内错误超过{ERROR_ALERT_THRESHOLD}次",
                    source="error_handler",
                )

            logger.error("未处理异常 [%s %s]: %s", method, path, exc)

            return JSONResponse(
                status_code=500,
                content={"ok": False, "error": "服务器内部错误", "detail": str(exc)[:500]},
            )
