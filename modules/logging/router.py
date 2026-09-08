"""
modules/logging/router.py —— 日志查询 + 审计日志 API 路由

所有端点前缀 /api/logs/
- 应用日志查询：/app（支持时间范围、级别、模块、关键词搜索）
- 错误日志查询：/errors
- 日志导出：/export
- 审计日志查询：/audit
- 审计日志统计：/audit/stats
- 日志文件列表：/files
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from auth import optional_api_key, require_api_key
from logging_config import LOG_DIR, get_logger
from modules.logging.audit import list_audit_logs, get_audit_log_stats

logger = get_logger(__name__)

router = APIRouter(prefix="/api/logs", tags=["日志管理"])


# ── 日志文件解析工具 ──────────────────────────────────────

def _parse_log_line(line: str) -> Optional[dict]:
    """解析一行 JSON 日志为字典。"""
    line = line.strip()
    if not line:
        return None
    try:
        return json.loads(line)
    except (json.JSONDecodeError, ValueError):
        # 非 JSON 行（如控制台输出），包装为简单结构
        return {"raw": line, "level": "UNKNOWN", "message": line}


def _read_log_file(filepath: Path, max_lines: int = 5000) -> list[dict]:
    """读取日志文件，返回解析后的日志条目列表（最新的在前）。"""
    if not filepath.exists():
        return []

    entries = []
    try:
        # 从文件末尾读取，优先获取最新日志
        file_size = filepath.stat().st_size
        # 如果文件不大，直接全读
        if file_size < 5 * 1024 * 1024:  # 5MB
            with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        else:
            # 大文件：从末尾读取最后 2MB
            with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                f.seek(max(0, file_size - 2 * 1024 * 1024))
                f.readline()  # 跳过可能不完整的第一行
                lines = f.readlines()

        for line in lines[-max_lines:]:
            entry = _parse_log_line(line)
            if entry:
                entries.append(entry)
    except Exception as e:
        logger.error("读取日志文件失败 %s: %s", filepath, e)

    entries.reverse()  # 最新的在前
    return entries


def _filter_logs(
    entries: list[dict],
    level: Optional[str] = None,
    module: Optional[str] = None,
    keyword: Optional[str] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
) -> list[dict]:
    """按条件过滤日志条目。"""
    result = []
    for entry in entries:
        # 级别过滤
        if level and entry.get("level", "").upper() != level.upper():
            continue
        # 模块过滤
        if module and module.lower() not in entry.get("module", "").lower():
            continue
        # 关键词过滤（在 message 和所有字段值中搜索）
        if keyword:
            kw = keyword.lower()
            found = False
            for value in entry.values():
                if kw in str(value).lower():
                    found = True
                    break
            if not found:
                continue
        # 时间范围过滤
        if start_time and entry.get("timestamp", "") < start_time:
            continue
        if end_time and entry.get("timestamp", "") > end_time:
            continue
        result.append(entry)
    return result


# ══════════════════════════════════════════════════════════
# 1. 应用日志查询
# ══════════════════════════════════════════════════════════

@router.get("/app")
async def query_app_logs(
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=50, ge=1, le=500),
    level: Optional[str] = Query(default=None, description="日志级别：DEBUG/INFO/WARNING/ERROR/CRITICAL"),
    module: Optional[str] = Query(default=None, description="模块名模糊匹配"),
    keyword: Optional[str] = Query(default=None, description="关键词搜索"),
    start_time: Optional[str] = Query(default=None, description="开始时间 ISO8601"),
    end_time: Optional[str] = Query(default=None, description="结束时间 ISO8601"),
):
    """查询应用日志（从 app.log 文件读取，支持多条件筛选和分页）。"""
    # 读取主日志文件（含轮转文件）
    all_entries = []

    # 读取当前 app.log
    all_entries.extend(_read_log_file(LOG_DIR / "app.log", max_lines=10000))

    # 读取轮转文件 app.log.1, app.log.2, ...
    for i in range(1, 11):
        rotated = LOG_DIR / f"app.log.{i}"
        if rotated.exists():
            all_entries.extend(_read_log_file(rotated, max_lines=2000))

    # 过滤
    filtered = _filter_logs(all_entries, level, module, keyword, start_time, end_time)

    # 分页
    total = len(filtered)
    offset = (page - 1) * limit
    page_items = filtered[offset:offset + limit]

    return {
        "ok": True,
        "data": {
            "total": total,
            "page": page,
            "limit": limit,
            "items": page_items,
        },
    }


# ══════════════════════════════════════════════════════════
# 2. 错误日志查询
# ══════════════════════════════════════════════════════════

@router.get("/errors")
async def query_error_logs(
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=50, ge=1, le=200),
    keyword: Optional[str] = Query(default=None),
    start_time: Optional[str] = Query(default=None),
    end_time: Optional[str] = Query(default=None),
):
    """查询错误日志（从 error.log 文件读取，仅包含 ERROR 及以上级别）。"""
    all_entries = []
    all_entries.extend(_read_log_file(LOG_DIR / "error.log", max_lines=5000))

    for i in range(1, 11):
        rotated = LOG_DIR / f"error.log.{i}"
        if rotated.exists():
            all_entries.extend(_read_log_file(rotated, max_lines=1000))

    filtered = _filter_logs(all_entries, keyword=keyword, start_time=start_time, end_time=end_time)

    total = len(filtered)
    offset = (page - 1) * limit
    page_items = filtered[offset:offset + limit]

    return {
        "ok": True,
        "data": {
            "total": total,
            "page": page,
            "limit": limit,
            "items": page_items,
        },
    }


# ══════════════════════════════════════════════════════════
# 3. 审计日志查询
# ══════════════════════════════════════════════════════════

@router.get("/audit")
async def query_audit_logs(
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=20, ge=1, le=200),
    action: Optional[str] = Query(default=None, description="操作类型筛选"),
    operator: Optional[str] = Query(default=None, description="操作人模糊匹配"),
    keyword: Optional[str] = Query(default=None, description="关键词搜索"),
    start_time: Optional[str] = Query(default=None),
    end_time: Optional[str] = Query(default=None),
):
    """查询审计日志（从数据库 audit_logs 表读取，支持多条件筛选）。"""
    result = list_audit_logs(
        page=page,
        limit=limit,
        action=action,
        operator=operator,
        keyword=keyword,
        start_time=start_time,
        end_time=end_time,
    )
    return {"ok": True, "data": result}


@router.get("/audit/stats")
async def audit_log_stats():
    """获取审计日志统计信息（按操作类型分组）。"""
    return {"ok": True, "data": get_audit_log_stats()}


# ══════════════════════════════════════════════════════════
# 4. 日志导出
# ══════════════════════════════════════════════════════════

@router.get("/export")
async def export_logs(
    log_type: str = Query(default="app", description="日志类型：app/error/audit"),
    level: Optional[str] = Query(default=None),
    keyword: Optional[str] = Query(default=None),
    start_time: Optional[str] = Query(default=None),
    end_time: Optional[str] = Query(default=None),
):
    """导出日志为 JSON Lines 文件（流式下载）。"""
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    filename = f"{log_type}_logs_{timestamp}.jsonl"

    def generate():
        if log_type == "audit":
            # 审计日志从数据库导出
            result = list_audit_logs(
                page=1,
                limit=10000,
                keyword=keyword,
                start_time=start_time,
                end_time=end_time,
            )
            for item in result["items"]:
                yield json.dumps(item, ensure_ascii=False) + "\n"
        else:
            # 应用/错误日志从文件导出
            log_file = LOG_DIR / f"{log_type}.log"
            entries = _read_log_file(log_file, max_lines=50000)
            filtered = _filter_logs(entries, level=level, keyword=keyword,
                                    start_time=start_time, end_time=end_time)
            for entry in filtered:
                yield json.dumps(entry, ensure_ascii=False) + "\n"

    return StreamingResponse(
        generate(),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# ══════════════════════════════════════════════════════════
# 5. 日志文件列表
# ══════════════════════════════════════════════════════════

@router.get("/files")
async def list_log_files():
    """列出所有日志文件及其大小和修改时间。"""
    files = []
    if LOG_DIR.exists():
        for f in sorted(LOG_DIR.iterdir()):
            if f.is_file() and (f.name.endswith(".log") or re.match(r".+\.log\.\d+$", f.name)):
                stat = f.stat()
                files.append({
                    "name": f.name,
                    "size_bytes": stat.st_size,
                    "size_mb": round(stat.st_size / (1024 * 1024), 2),
                    "modified_at": time.strftime("%Y-%m-%dT%H:%M:%S+08:00",
                                                  time.localtime(stat.st_mtime)),
                })
    return {"ok": True, "data": {"files": files, "log_dir": str(LOG_DIR)}}


# ══════════════════════════════════════════════════════════
# 6. 日志级别动态调整（运行时）
# ══════════════════════════════════════════════════════════

@router.post("/level", dependencies=[Depends(require_api_key)])
async def set_log_level(level: str = Query(..., description="新的日志级别：DEBUG/INFO/WARNING/ERROR")):
    """动态调整根 logger 的日志级别（运行时生效，不持久化）。"""
    level_upper = level.upper()
    if not hasattr(logging, level_upper):
        return {"ok": False, "msg": f"无效的日志级别: {level}"}

    logging.getLogger().setLevel(getattr(logging, level_upper))
    logger.info("日志级别已动态调整为 %s", level_upper)
    return {"ok": True, "msg": f"日志级别已设置为 {level_upper}"}
