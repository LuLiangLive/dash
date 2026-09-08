"""
modules/logging/audit.py —— 关键操作审计日志服务

功能：
- 记录关键操作（添加/删除自选基金、添加/删除持仓、修改设置等）
- 审计日志包含：操作时间、操作类型、操作人、操作内容、IP地址、请求ID
- 同时写入 audit.log 文件和数据库 audit_logs 表（双重持久化，不可篡改）
- 数据库表使用 INSERT ONLY 模式，不提供 UPDATE/DELETE 接口

审计操作类型：
- WATCHLIST_ADD: 添加自选基金
- WATCHLIST_REMOVE: 删除自选基金
- WATCHLIST_SYNC: 同步自选基金
- PORTFOLIO_ADD: 添加持仓
- PORTFOLIO_REMOVE: 删除持仓
- PORTFOLIO_UPDATE: 更新持仓
- SETTINGS_CHANGE: 修改设置
- ALGO_CONFIG_CHANGE: 修改算法配置
- ADMIN_ACTION: 管理员操作（清缓存、重算等）
- USER_LOGIN: 用户登录
- USER_LOGOUT: 用户登出
- DATA_EXPORT: 数据导出
"""
from __future__ import annotations

import json
import time
from enum import Enum
from typing import Any, Optional

from fastapi import Request

from logging_config import get_audit_logger, get_request_context
import db

logger = get_audit_logger()


class AuditAction(str, Enum):
    """审计操作类型枚举。"""
    WATCHLIST_ADD = "watchlist_add"
    WATCHLIST_REMOVE = "watchlist_remove"
    WATCHLIST_SYNC = "watchlist_sync"
    PORTFOLIO_ADD = "portfolio_add"
    PORTFOLIO_REMOVE = "portfolio_remove"
    PORTFOLIO_UPDATE = "portfolio_update"
    SETTINGS_CHANGE = "settings_change"
    ALGO_CONFIG_CHANGE = "algo_config_change"
    ADMIN_ACTION = "admin_action"
    USER_LOGIN = "user_login"
    USER_LOGOUT = "user_logout"
    DATA_EXPORT = "data_export"
    OTHER = "other"


def _now_iso() -> str:
    """当前时间 ISO 8601 格式。"""
    return time.strftime("%Y-%m-%dT%H:%M:%S+08:00")


def audit_log(
    action: AuditAction | str,
    detail: str,
    *,
    request: Optional[Request] = None,
    operator: Optional[str] = None,
    extra: Optional[dict[str, Any]] = None,
) -> None:
    """记录一条审计日志。

    同时写入：
    1. audit.log 文件（JSON 格式，按大小轮转）
    2. 数据库 audit_logs 表（INSERT ONLY，不可篡改）

    Args:
        action: 操作类型（AuditAction 枚举或字符串）
        detail: 操作描述（人类可读）
        request: FastAPI Request 对象（用于提取 IP、user_id、request_id）
        operator: 操作人标识（如未提供则从请求上下文提取）
        extra: 额外的结构化数据（如基金代码、持仓ID等）
    """
    # 从请求上下文提取信息
    ctx = get_request_context()
    request_id = ctx.get("request_id")
    ip = ctx.get("ip")
    user_id = ctx.get("user_id")

    # 如果传入了 request 对象，优先从 request 提取
    if request is not None:
        if not request_id:
            request_id = request.headers.get("X-Request-ID")
        if not ip:
            forwarded = request.headers.get("X-Forwarded-For")
            if forwarded:
                ip = forwarded.split(",")[0].strip()
            else:
                ip = request.client.host if request.client else "unknown"
        if not user_id:
            user_id = request.headers.get("X-User-ID")
            if not user_id:
                api_key = request.headers.get("X-API-Key")
                if api_key:
                    user_id = f"key:{api_key[:8]}..."

    # 操作人
    if not operator:
        operator = user_id or "system"

    # 操作类型标准化
    if isinstance(action, AuditAction):
        action_str = action.value
    else:
        action_str = str(action)

    # 额外数据
    extra_json = json.dumps(extra or {}, ensure_ascii=False, default=str)

    timestamp = _now_iso()

    # ── 1. 写入 audit.log 文件 ──
    logger.audit(  # type: ignore[attr-defined]
        detail,
        extra={
            "action": action_str,
            "operator": operator,
            "ip": ip,
            "request_id": request_id,
            "extra": extra or {},
        },
    )

    # ── 2. 写入数据库 audit_logs 表 ──
    try:
        conn = db.get_conn()
        conn.execute(
            """
            INSERT INTO audit_logs
                (timestamp, action, operator, detail, ip, request_id, extra)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (timestamp, action_str, operator, detail, ip, request_id, extra_json),
        )
        conn.commit()
    except Exception as e:
        # 数据库写入失败不影响主流程，但记录到 app log
        from logging_config import get_logger
        get_logger("audit").warning("审计日志写入数据库失败: %s", e)


def list_audit_logs(
    page: int = 1,
    limit: int = 20,
    action: Optional[str] = None,
    operator: Optional[str] = None,
    keyword: Optional[str] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
) -> dict:
    """查询审计日志列表（支持分页和多条件筛选）。

    Returns:
        {"total": int, "page": int, "limit": int, "items": list[dict]}
    """
    conn = db.get_conn()

    # 构建 WHERE 子句
    conditions = []
    params: list[Any] = []

    if action:
        conditions.append("action = ?")
        params.append(action)
    if operator:
        conditions.append("operator LIKE ?")
        params.append(f"%{operator}%")
    if keyword:
        conditions.append("(detail LIKE ? OR extra LIKE ?)")
        params.extend([f"%{keyword}%", f"%{keyword}%"])
    if start_time:
        conditions.append("timestamp >= ?")
        params.append(start_time)
    if end_time:
        conditions.append("timestamp <= ?")
        params.append(end_time)

    where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""

    # 总数
    count_row = conn.execute(
        f"SELECT COUNT(*) as cnt FROM audit_logs{where_clause}",
        params,
    ).fetchone()
    total = count_row["cnt"] if count_row else 0

    # 分页查询
    offset = (page - 1) * limit
    rows = conn.execute(
        f"""
        SELECT id, timestamp, action, operator, detail, ip, request_id, extra
        FROM audit_logs{where_clause}
        ORDER BY id DESC
        LIMIT ? OFFSET ?
        """,
        params + [limit, offset],
    ).fetchall()

    items = []
    for row in rows:
        item = dict(row)
        # 解析 extra JSON
        try:
            item["extra"] = json.loads(item["extra"]) if item["extra"] else {}
        except (json.JSONDecodeError, TypeError):
            item["extra"] = {}
        items.append(item)

    return {
        "total": total,
        "page": page,
        "limit": limit,
        "items": items,
    }


def get_audit_log_stats() -> dict:
    """获取审计日志统计信息（按操作类型分组计数）。"""
    conn = db.get_conn()
    rows = conn.execute(
        """
        SELECT action, COUNT(*) as cnt, MAX(timestamp) as last_time
        FROM audit_logs
        GROUP BY action
        ORDER BY cnt DESC
        """
    ).fetchall()

    total = conn.execute("SELECT COUNT(*) as cnt FROM audit_logs").fetchone()
    total_count = total["cnt"] if total else 0

    return {
        "total": total_count,
        "by_action": [dict(row) for row in rows],
    }
