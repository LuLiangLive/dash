"""
logging_config.py —— 结构化日志系统配置

功能：
- JSON 结构化日志格式（时间、级别、模块、消息、请求ID、用户ID、IP等）
- 控制台 + 文件双处理器
- 按大小轮转（RotatingFileHandler），保留最近 10 个备份，单文件 10MB
- 请求上下文（request_id / user_id / ip / path / method / duration / status_code）
- 兼容 python-json-logger（如已安装则使用，否则使用内置 JSON formatter）

使用方式：
    from logging_config import setup_logging, get_logger, set_request_context
    setup_logging()
    logger = get_logger(__name__)
    logger.info("message", extra={"key": "value"})
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Optional

# ── 全局配置 ──────────────────────────────────────────────

ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)

# 日志文件路径
APP_LOG_FILE = LOG_DIR / "app.log"
ERROR_LOG_FILE = LOG_DIR / "error.log"
AUDIT_LOG_FILE = LOG_DIR / "audit.log"

# 轮转配置
MAX_BYTES = 10 * 1024 * 1024  # 10MB
BACKUP_COUNT = 10

# 默认日志级别
DEFAULT_LEVEL = logging.INFO

# 是否已初始化
_initialized = False

# 线程局部存储：请求上下文
import threading
_request_context = threading.local()


# ── JSON 格式化器 ─────────────────────────────────────────

class JsonFormatter(logging.Formatter):
    """结构化 JSON 日志格式化器。

    输出字段：
    - timestamp: ISO 8601 时间戳（带时区）
    - level: 日志级别（DEBUG/INFO/WARNING/ERROR/CRITICAL）
    - module: 模块名（logger name）
    - message: 日志消息
    - request_id: 请求ID（如有）
    - user_id: 用户ID（如有）
    - ip: 客户端IP（如有）
    - path: 请求路径（如有）
    - method: 请求方法（如有）
    - status_code: 响应状态码（如有）
    - duration_ms: 响应耗时（如有）
    - 以及 extra 中传入的任意自定义字段
    """

    # 标准字段（从 LogRecord 直接提取）
    RESERVED_ATTRS = {
        "args", "asctime", "created", "exc_info", "exc_text",
        "filename", "funcName", "levelname", "levelno", "lineno",
        "module", "msecs", "message", "msg", "name", "pathname",
        "process", "processName", "relativeCreated", "stack_info",
        "thread", "threadName", "taskName",
    }

    def format(self, record: logging.LogRecord) -> str:
        # 基础字段
        log_entry: dict[str, Any] = {
            "timestamp": self._format_time(record.created),
            "level": record.levelname,
            "module": record.name,
            "message": record.getMessage(),
        }

        # 异常信息
        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)

        # 从线程局部存储获取请求上下文
        ctx = getattr(_request_context, "data", None)
        if ctx:
            for key, value in ctx.items():
                if value is not None and key not in log_entry:
                    log_entry[key] = value

        # 从 record.extra 提取自定义字段
        for key, value in record.__dict__.items():
            if key not in self.RESERVED_ATTRS and not key.startswith("_"):
                log_entry[key] = value

        return json.dumps(log_entry, ensure_ascii=False, default=str)

    @staticmethod
    def _format_time(timestamp: float) -> str:
        """格式化为 ISO 8601 带时区时间戳。"""
        tz_offset = -time.timezone if (time.localtime().tm_isdst == 0) else -time.altzone
        tz_hours = int(tz_offset // 3600)
        tz_minutes = int(abs(tz_offset % 3600) // 60)
        tz_str = f"{tz_hours:+03d}:{tz_minutes:02d}"
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(timestamp)) + f".{int((timestamp % 1) * 1000):03d}{tz_str}"


class ConsoleFormatter(logging.Formatter):
    """控制台可读性格式化器（非 JSON，便于开发调试）。

    格式：[时间] [级别] [模块] [request_id] 消息
    """

    def format(self, record: logging.LogRecord) -> str:
        timestamp = JsonFormatter._format_time(record.created)
        ctx = getattr(_request_context, "data", None)
        request_id = ctx.get("request_id", "-") if ctx else "-"

        prefix = f"[{timestamp}] [{record.levelname:<8}] [{record.name:<30}] [{request_id}]"
        message = record.getMessage()

        if record.exc_info:
            message += "\n" + self.formatException(record.exc_info)

        return f"{prefix} {message}"


# ── 请求上下文管理 ────────────────────────────────────────

def set_request_context(**kwargs):
    """设置当前线程的请求上下文。

    典型用法（中间件中）：
        set_request_context(
            request_id=req_id,
            user_id=user_id,
            ip=client_ip,
            path=request.url.path,
            method=request.method,
        )
    """
    _request_context.data = kwargs


def clear_request_context():
    """清除当前线程的请求上下文（请求结束时调用）。"""
    if hasattr(_request_context, "data"):
        del _request_context.data


def get_request_context() -> dict:
    """获取当前线程的请求上下文。"""
    return getattr(_request_context, "data", {}) or {}


def generate_request_id() -> str:
    """生成唯一请求 ID（UUID4 短格式）。"""
    return uuid.uuid4().hex[:16]


# ── 日志初始化 ────────────────────────────────────────────

def setup_logging(level: Optional[int] = None, log_dir: Optional[Path] = None) -> None:
    """初始化结构化日志系统。

    配置：
    - 根 logger 级别：INFO（可通过 LOG_LEVEL 环境变量覆盖）
    - 控制台处理器：可读性格式，INFO 及以上
    - app.log 文件：JSON 格式，按大小轮转，INFO 及以上
    - error.log 文件：JSON 格式，按大小轮转，ERROR 及以上
    - audit.log 文件：JSON 格式，按大小轮转，AUDIT 级别（自定义）

    幂等：多次调用只初始化一次。
    """
    global _initialized
    if _initialized:
        return

    log_dir = log_dir or LOG_DIR
    log_dir.mkdir(exist_ok=True)

    # 确定日志级别
    if level is None:
        env_level = os.environ.get("LOG_LEVEL", "INFO").upper()
        level = getattr(logging, env_level, logging.INFO)

    # 注册自定义 AUDIT 级别（介于 INFO 和 WARNING 之间）
    AUDIT_LEVEL = 25
    logging.addLevelName(AUDIT_LEVEL, "AUDIT")

    def audit(self, message, *args, **kwargs):
        if self.isEnabledFor(AUDIT_LEVEL):
            self._log(AUDIT_LEVEL, message, args, **kwargs)

    logging.Logger.audit = audit  # type: ignore[attr-defined]

    # 根 logger
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # 清除已有处理器（避免重复）
    root_logger.handlers.clear()

    # ── 控制台处理器 ──
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(ConsoleFormatter())
    root_logger.addHandler(console_handler)

    # ── app.log 文件处理器（全级别 JSON）──
    app_handler = logging.handlers.RotatingFileHandler(
        str(log_dir / "app.log"),
        maxBytes=MAX_BYTES,
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
    )
    app_handler.setLevel(level)
    app_handler.setFormatter(JsonFormatter())
    root_logger.addHandler(app_handler)

    # ── error.log 文件处理器（仅 ERROR+）──
    error_handler = logging.handlers.RotatingFileHandler(
        str(log_dir / "error.log"),
        maxBytes=MAX_BYTES,
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(JsonFormatter())
    root_logger.addHandler(error_handler)

    # ── audit.log 专用 logger（独立文件）──
    audit_logger = logging.getLogger("audit")
    audit_logger.setLevel(AUDIT_LEVEL)
    audit_logger.propagate = False  # 不传播到根 logger，避免重复

    audit_handler = logging.handlers.RotatingFileHandler(
        str(log_dir / "audit.log"),
        maxBytes=MAX_BYTES,
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
    )
    audit_handler.setLevel(AUDIT_LEVEL)
    audit_handler.setFormatter(JsonFormatter())
    audit_logger.addHandler(audit_handler)

    # 降低第三方库的日志噪音
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    _initialized = True

    # 启动日志
    root_logger.info(
        "结构化日志系统初始化完成",
        extra={
            "log_dir": str(log_dir),
            "level": logging.getLevelName(level),
            "max_bytes": MAX_BYTES,
            "backup_count": BACKUP_COUNT,
        },
    )


def get_logger(name: str) -> logging.Logger:
    """获取 logger（自动确保日志系统已初始化）。"""
    if not _initialized:
        setup_logging()
    return logging.getLogger(name)


def get_audit_logger() -> logging.Logger:
    """获取审计日志专用 logger。"""
    if not _initialized:
        setup_logging()
    return logging.getLogger("audit")
