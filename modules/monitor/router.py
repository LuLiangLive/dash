"""
modules/monitor/router.py —— 监控 API 路由

所有端点前缀 /api/monitor/
- 性能监控：/performance, /slow-queries, /error-rate
- 错误日志：/errors, /errors/{id}, DELETE /errors
- 数据更新：/data-updates, /data-updates/latest, /data-updates/stats
- 系统资源：/system, /system/history
- 告警管理：/alerts, /alerts/{id}/acknowledge
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query

from auth import optional_api_key, require_api_key
from modules.monitor import middleware as apm
from modules.monitor import service as svc

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/monitor", tags=["监控告警"])


# ══════════════════════════════════════════════════════════
# 1. 性能监控 APM
# ══════════════════════════════════════════════════════════

@router.get("/performance", summary="获取性能数据概览", responses={200: {"description": "平均响应时间/P50/P95/P99/请求量/错误率（按端点分组）"}})
async def get_performance():
    """获取性能数据概览（平均响应时间、P50/P95/P99、请求量、错误率，按端点分组）。"""
    return {"ok": True, "data": apm.get_performance_overview()}


@router.get("/slow-queries", summary="获取慢查询列表", responses={200: {"description": "响应时间>1秒的慢请求列表"}})
async def get_slow_queries(limit: int = Query(default=20, ge=1, le=500)):
    """获取慢查询列表（响应时间 > 1秒）。"""
    return {"ok": True, "data": apm.get_slow_queries(limit=limit)}


@router.get("/error-rate", summary="获取错误率统计", responses={200: {"description": "按端点分组的错误率统计"}})
async def get_error_rate():
    """获取错误率统计（按端点分组）。"""
    return {"ok": True, "data": apm.get_error_rate_stats()}


# ══════════════════════════════════════════════════════════
# 2. 错误日志
# ══════════════════════════════════════════════════════════

@router.get("/errors", summary="获取错误日志列表", responses={200: {"description": "错误日志列表（支持分页和状态筛选）"}})
async def list_errors(
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=20, ge=1, le=200),
    resolved: Optional[int] = Query(default=None, ge=0, le=1),
):
    """获取错误日志列表（支持分页和按状态筛选）。"""
    return {"ok": True, "data": svc.list_errors(page=page, limit=limit, resolved=resolved)}


@router.get("/errors/{error_id}", summary="获取单条错误详情", responses={200: {"description": "错误详情"}, 404: {"description": "错误记录不存在"}})
async def get_error(error_id: int):
    """获取单条错误详情。"""
    error = svc.get_error(error_id)
    if not error:
        return {"ok": False, "msg": "错误记录不存在"}
    return {"ok": True, "data": error}


@router.delete("/errors", dependencies=[Depends(require_api_key)], summary="清空错误日志", responses={200: {"description": "错误日志已清空"}})
async def clear_errors():
    """清空错误日志（需要管理员权限/API Key）。"""
    deleted = svc.clear_errors()
    return {"ok": True, "msg": f"已清空 {deleted} 条错误日志"}


# ══════════════════════════════════════════════════════════
# 3. 数据更新状态
# ══════════════════════════════════════════════════════════

@router.get("/data-updates", summary="获取数据更新历史", responses={200: {"description": "数据更新历史列表（支持分页和任务筛选）"}})
async def list_data_updates(
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=20, ge=1, le=200),
    task_name: Optional[str] = Query(default=None),
):
    """获取数据更新历史（支持分页和 task_name 筛选）。"""
    return {"ok": True, "data": svc.list_data_updates(page=page, limit=limit, task_name=task_name)}


@router.get("/data-updates/latest", summary="获取各任务最新更新状态", responses={200: {"description": "各任务最新更新状态"}})
async def get_latest_data_updates():
    """获取各任务最新更新状态。"""
    return {"ok": True, "data": svc.get_latest_data_updates()}


@router.get("/data-updates/stats", summary="获取更新统计", responses={200: {"description": "总次数/成功率/平均耗时/最近更新时间"}})
async def get_data_update_stats():
    """获取更新统计（总次数、成功率、平均耗时、最近更新时间）。"""
    return {"ok": True, "data": svc.get_data_update_stats()}


# ══════════════════════════════════════════════════════════
# 4. 系统资源监控
# ══════════════════════════════════════════════════════════

@router.get("/system", summary="获取系统资源使用情况", responses={200: {"description": "CPU/内存/磁盘/数据库大小"}})
async def get_system_metrics():
    """获取最新系统资源使用情况（CPU/内存/磁盘/数据库大小）。"""
    metrics = svc.get_latest_system_metrics()
    if not metrics:
        # 如果没有历史数据，立即采集一次
        metrics = svc.collect_system_metrics()
    return {"ok": True, "data": metrics}


@router.get("/system/history", summary="获取历史资源使用数据", responses={200: {"description": "历史资源使用序列"}})
async def get_system_metrics_history(
    hours: int = Query(default=24, ge=1, le=720),
):
    """获取历史资源使用数据（支持时间范围，默认最近24小时）。"""
    return {"ok": True, "data": svc.get_system_metrics_history(hours=hours)}


# ══════════════════════════════════════════════════════════
# 5. 告警管理
# ══════════════════════════════════════════════════════════

@router.get("/alerts", summary="获取告警列表", responses={200: {"description": "告警列表（支持按确认状态筛选）"}})
async def list_alerts(
    acknowledged: Optional[int] = Query(default=None, ge=0, le=1),
    limit: int = Query(default=50, ge=1, le=200),
):
    """获取告警列表（支持按 acknowledged 筛选）。"""
    return {"ok": True, "data": svc.list_alerts(acknowledged=acknowledged, limit=limit)}


@router.post("/alerts/{alert_id}/acknowledge", summary="确认告警", responses={200: {"description": "告警已确认"}, 404: {"description": "告警不存在"}})
async def acknowledge_alert(alert_id: int):
    """确认告警。"""
    ok = svc.acknowledge_alert(alert_id)
    if not ok:
        return {"ok": False, "msg": "确认告警失败"}
    return {"ok": True, "msg": "告警已确认"}
