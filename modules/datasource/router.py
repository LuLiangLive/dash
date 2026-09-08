"""
modules/datasource/router.py —— 数据源管理 API 路由

提供数据源配置、切换、监控、对比等 API 端点，供前端设置页面使用。

端点列表：
- GET  /api/datasource/list          —— 获取所有数据源列表及状态
- GET  /api/datasource/status        —— 获取数据源管理器状态
- POST /api/datasource/priority      —— 设置数据源优先级
- POST /api/datasource/switch        —— 手动切换活跃数据源
- POST /api/datasource/health-check  —— 执行健康检查
- GET  /api/datasource/monitor       —— 获取监控状态
- POST /api/datasource/monitor/start —— 启动监控
- POST /api/datasource/monitor/stop  —— 停止监控
- POST /api/datasource/compare       —— 对比基金数据
- POST /api/datasource/compare/batch —— 批量对比
- GET  /api/datasource/cache/stats   —— 获取缓存统计
- POST /api/datasource/cache/clear   —— 清除缓存
- GET  /api/datasource/fund/{code}   —— 从指定数据源获取基金信息
"""
from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import APIRouter, Body, Depends, Query
from pydantic import BaseModel

from auth import optional_api_key, require_api_key
from .manager import get_manager
from .monitor import get_monitor
from .compare import get_comparator
from .cache import get_cache

router = APIRouter(tags=["数据源管理"])


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------
class PriorityRequest(BaseModel):
    """设置数据源优先级请求。"""
    priorities: list[str]


class SwitchRequest(BaseModel):
    """切换数据源请求。"""
    source: Optional[str] = None  # None 表示自动模式


class CompareRequest(BaseModel):
    """单只基金对比请求。"""
    code: str
    source_a: str = "eastmoney"
    source_b: str = "danjuan"


class BatchCompareRequest(BaseModel):
    """批量对比请求。"""
    codes: list[str]
    source_a: str = "eastmoney"
    source_b: str = "danjuan"


class MonitorConfigRequest(BaseModel):
    """监控配置请求。"""
    check_interval: int = 300
    auto_switch: bool = True


# ---------------------------------------------------------------------------
# 数据源列表与状态
# ---------------------------------------------------------------------------
@router.get("/api/datasource/list", dependencies=[Depends(optional_api_key)])
async def datasource_list():
    """获取所有已注册数据源的列表及状态。"""
    mgr = get_manager()
    return {
        "ok": True,
        "sources": mgr.get_sources(),
        "active_source": mgr.get_active_source_name(),
    }


@router.get("/api/datasource/status", dependencies=[Depends(optional_api_key)])
async def datasource_status():
    """获取数据源管理器完整状态。"""
    mgr = get_manager()
    return {
        "ok": True,
        "status": mgr.get_monitor_stats(),
    }


# ---------------------------------------------------------------------------
# 优先级与切换
# ---------------------------------------------------------------------------
@router.post("/api/datasource/priority", dependencies=[Depends(require_api_key)])
async def set_priority(body: PriorityRequest):
    """设置数据源优先级顺序。"""
    mgr = get_manager()
    ok = mgr.set_priority(body.priorities)
    if not ok:
        return {"ok": False, "msg": "设置失败：包含未注册的数据源名称"}
    return {"ok": True, "msg": "优先级已更新", "priorities": body.priorities}


@router.post("/api/datasource/switch", dependencies=[Depends(require_api_key)])
async def switch_source(body: SwitchRequest):
    """手动切换活跃数据源（source 为 null 表示自动模式）。"""
    mgr = get_manager()
    ok = mgr.set_active_source(body.source)
    if not ok:
        return {"ok": False, "msg": "切换失败：数据源不存在"}
    mode = "自动模式" if body.source is None else f"手动指定: {body.source}"
    return {"ok": True, "msg": f"已切换到 {mode}", "active_source": mgr.get_active_source_name()}


# ---------------------------------------------------------------------------
# 健康检查
# ---------------------------------------------------------------------------
@router.post("/api/datasource/health-check", dependencies=[Depends(optional_api_key)])
async def health_check():
    """对所有数据源执行健康检查。"""
    mgr = get_manager()
    results = await asyncio.to_thread(mgr.health_check_all)
    return {"ok": True, "results": results}


# ---------------------------------------------------------------------------
# 监控
# ---------------------------------------------------------------------------
@router.get("/api/datasource/monitor", dependencies=[Depends(optional_api_key)])
async def monitor_status():
    """获取监控状态。"""
    mon = get_monitor()
    return {"ok": True, "status": mon.get_status()}


@router.post("/api/datasource/monitor/start", dependencies=[Depends(require_api_key)])
async def monitor_start(body: MonitorConfigRequest):
    """启动数据源监控。"""
    mon = get_monitor()
    mon.set_check_interval(body.check_interval)
    mon.set_auto_switch(body.auto_switch)
    ok = mon.start(check_interval=body.check_interval)
    if not ok:
        return {"ok": False, "msg": "监控已在运行中"}
    return {"ok": True, "msg": f"监控已启动，检查间隔 {body.check_interval} 秒"}


@router.post("/api/datasource/monitor/stop", dependencies=[Depends(require_api_key)])
async def monitor_stop():
    """停止数据源监控。"""
    mon = get_monitor()
    mon.stop()
    return {"ok": True, "msg": "监控已停止"}


@router.get("/api/datasource/monitor/history", dependencies=[Depends(optional_api_key)])
async def monitor_history(limit: int = Query(default=20, ge=1, le=100)):
    """获取监控检查历史。"""
    mon = get_monitor()
    return {"ok": True, "history": mon.get_history(limit)}


# ---------------------------------------------------------------------------
# 数据对比
# ---------------------------------------------------------------------------
@router.post("/api/datasource/compare", dependencies=[Depends(optional_api_key)])
async def compare_fund(body: CompareRequest):
    """对比单只基金在两个数据源的数据。"""
    comparator = get_comparator()
    result = await asyncio.to_thread(
        comparator.compare_fund_nav, body.code, body.source_a, body.source_b
    )
    return {
        "ok": True,
        "comparison": {
            "code": result.code,
            "source_a": result.source_a,
            "source_b": result.source_b,
            "date": result.date,
            "nav_a": result.nav_a,
            "nav_b": result.nav_b,
            "abs_diff": result.abs_diff,
            "rel_diff": result.rel_diff,
            "consistent": result.consistent,
            "detail": result.detail,
        },
    }


@router.post("/api/datasource/compare/batch", dependencies=[Depends(optional_api_key)])
async def compare_batch(body: BatchCompareRequest):
    """批量对比多只基金的净值数据。"""
    comparator = get_comparator()
    report = await asyncio.to_thread(
        comparator.compare_nav_batch, body.codes, body.source_a, body.source_b
    )
    return {
        "ok": True,
        "report": {
            "report_id": report.report_id,
            "generated_at": report.generated_at,
            "source_a": report.source_a,
            "source_b": report.source_b,
            "total_funds": report.total_funds,
            "consistent_count": report.consistent_count,
            "inconsistent_count": report.inconsistent_count,
            "error_count": report.error_count,
            "avg_abs_diff": report.avg_abs_diff,
            "max_abs_diff": report.max_abs_diff,
            "inconsistent_funds": report.inconsistent_funds,
        },
    }


@router.post("/api/datasource/compare/info", dependencies=[Depends(optional_api_key)])
async def compare_info(body: CompareRequest):
    """对比单只基金的基本信息。"""
    comparator = get_comparator()
    result = await asyncio.to_thread(
        comparator.compare_fund_info, body.code, body.source_a, body.source_b
    )
    return {"ok": True, "comparison": result}


# ---------------------------------------------------------------------------
# 缓存管理
# ---------------------------------------------------------------------------
@router.get("/api/datasource/cache/stats", dependencies=[Depends(optional_api_key)])
async def cache_stats():
    """获取数据源缓存统计。"""
    cache = get_cache()
    return {"ok": True, "stats": cache.get_stats()}


@router.post("/api/datasource/cache/clear", dependencies=[Depends(require_api_key)])
async def cache_clear(source: Optional[str] = Query(default=None)):
    """清除数据源缓存（指定 source 则只清该数据源，否则全部清除）。"""
    cache = get_cache()
    if source:
        count = cache.invalidate_source(source)
        return {"ok": True, "msg": f"已清除数据源 {source} 的 {count} 条缓存"}
    cache.clear()
    return {"ok": True, "msg": "已清除所有数据源缓存"}


# ---------------------------------------------------------------------------
# 从指定数据源获取基金信息
# ---------------------------------------------------------------------------
@router.get("/api/datasource/fund/{code}", dependencies=[Depends(optional_api_key)])
async def get_fund_from_source(
    code: str,
    source: Optional[str] = Query(default=None, description="数据源名称，不指定则使用当前活跃数据源")
):
    """从指定数据源获取基金基本信息和最新净值。"""
    mgr = get_manager()
    if source:
        src = mgr.get_source(source)
        if not src:
            return {"ok": False, "msg": f"数据源 {source} 不存在"}
    else:
        src = mgr._get_effective_source()
        if not src:
            return {"ok": False, "msg": "没有可用的数据源"}

    info = await asyncio.to_thread(src.get_fund_info, code)
    nav = await asyncio.to_thread(src.get_latest_nav, code)

    return {
        "ok": True,
        "source": src.name,
        "fund_info": {
            "code": info.code if info else code,
            "name": info.name if info else "",
            "ftype": info.ftype if info else "",
            "scale": info.scale if info else None,
            "manager": info.manager if info else "",
            "nav": info.nav if info else None,
            "nav_date": info.nav_date if info else "",
        } if info else None,
        "latest_nav": {
            "date": nav.date,
            "ljjz": nav.ljjz,
            "dwjz": nav.dwjz,
            "jzzzl": nav.jzzzl,
        } if nav else None,
    }
