"""
api/watch.py —— 自选基金管理路由模块

从 main.py 拆分出的自选基金管理相关路由，包括：
- 自选列表查询
- 添加自选（含实时兜底获取）
- 删除自选
- 整体覆盖自选

所有路由通过 APIRouter 注册，main.py 中 include_router 引入。
"""
from __future__ import annotations

import asyncio
import importlib

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import db
from modules.logging.audit import audit_log, AuditAction
from auth import optional_api_key, require_api_key
from modules.common.cache_service import invalidate_index_cache

router = APIRouter(tags=["自选基金管理"])


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------

class WatchAdd(BaseModel):
    code: str


class WatchSync(BaseModel):
    codes: list[str]


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

_NFM = None


def _load_nfm():
    """动态加载 analysis_pipeline/night_fund_monitor.py。"""
    global _NFM
    if _NFM is None:
        _NFM = importlib.import_module("analysis_pipeline.night_fund_monitor")
    return _NFM


def _upsert_fund_from_detail(d: dict) -> None:
    """从实时获取的基金详情构造数据并写入 funds 表。"""
    if not d or not d.get("code"):
        return
    data = {
        "code": d["code"],
        "name": d.get("name"),
        "nav": d.get("nav"),
        "nav_date": d.get("nav_date"),
        "d1": d.get("d1"),
        "d3": d.get("d3"),
        "d5": d.get("d5"),
        "d7": d.get("d7"),
        "d10": d.get("d10"),
        "m1": d.get("m1"),
        "m3": d.get("m3"),
        "score": d.get("score"),
        "ad_score": d.get("ad_score"),
        "earn_score": d.get("earn_score"),
    }
    # 只保留非空字段
    data = {k: v for k, v in data.items() if v is not None}
    if data:
        db.upsert_fund(data)


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------

@router.get("/api/watchlist", dependencies=[Depends(optional_api_key)],
         summary="获取自选基金列表",
         responses={200: {"description": "自选基金列表（含基金详情数据）"}})
async def watchlist():
    """自选列表（含基金详情）。"""
    items = await db.alist_watchlist()
    out = []
    for w in items:
        f = await db.aget_fund(w["code"])
        if f:
            out.append({**w, **f})
        else:
            out.append(w)
    return {"watchlist": out}


def _do_watchlist_add(code: str):
    f = db.get_fund(code)
    if not f:
        try:
            nfm = _load_nfm()
            d = nfm.fetch_watch_detail(code)
            if d and d.get("name"):
                _upsert_fund_from_detail(d)
                f = db.get_fund(code)
        except Exception:
            pass
    if not f:
        return None, "基金库中无该基金,请先运行采集器或检查代码"
    n = db.add_watch(code, f["name"])
    return {"ok": True, "added": n == 1, "name": f["name"]}, None


@router.post("/api/watchlist", dependencies=[Depends(require_api_key)],
         summary="添加自选基金",
         responses={
             200: {"description": "添加成功"},
             400: {"description": "基金代码格式错误（需6位数字）"},
             404: {"description": "基金库中无该基金"},
         })
async def watchlist_add(body: WatchAdd):
    """添加自选基金（基金库中无时实时兜底获取）。

    **请求体示例**：
    ```json
    {"code": "000001"}
    ```
    """
    code = body.code.strip()
    if len(code) != 6 or not code.isdigit():
        # 基金代码标准格式为6位数字（如 000001、510300）
        # 与前端 src/stores/watch.ts 中 /^\d{6}$/ 校验保持一致
        _reason = []
        if not code:
            _reason.append("代码不能为空")
        elif not code.isdigit():
            _reason.append("包含非数字字符")
        if len(code) != 6:
            _reason.append(f"长度为{len(code)}位（应为6位）")
        _detail = "基金代码格式错误：" + "，".join(_reason) + "。请输入6位数字基金代码（如 000001、510300）"
        raise HTTPException(status_code=400, detail=_detail)
    result, err = await asyncio.to_thread(_do_watchlist_add, code)
    if err:
        raise HTTPException(status_code=404, detail=err)
    invalidate_index_cache()  # 自选列表变化，失效首页缓存
    audit_log(AuditAction.WATCHLIST_ADD, f"添加自选基金: {code} ({result.get('name', '')})", extra={"code": code, "name": result.get("name", "")})
    return result


@router.delete("/api/watchlist/{code}", dependencies=[Depends(require_api_key)],
         summary="删除自选基金",
         responses={200: {"description": "删除成功"}})
async def watchlist_remove(code: str):
    """删除自选基金。

    - **code**: 6位基金代码
    """
    n = await asyncio.to_thread(db.remove_watch, code)
    invalidate_index_cache()  # 自选列表变化，失效首页缓存
    audit_log(AuditAction.WATCHLIST_REMOVE, f"删除自选基金: {code}", extra={"code": code})
    return {"ok": True, "removed": n > 0}


def _do_watchlist_sync(codes: list[str]):
    name_of = {}
    for c in codes:
        f = db.get_fund(c)
        name_of[c] = f["name"] if f else c
    db.set_watchlist(codes, name_of)


@router.put("/api/watchlist", dependencies=[Depends(require_api_key)],
         summary="整体覆盖自选列表",
         responses={200: {"description": "覆盖成功"}})
async def watchlist_sync(body: WatchSync):
    """整体覆盖自选列表（用传入的代码列表替换全部自选）。

    **请求体示例**：
    ```json
    {"codes": ["000001", "110011", "161725"]}
    ```
    """
    await asyncio.to_thread(_do_watchlist_sync, body.codes)
    invalidate_index_cache()  # 自选列表变化，失效首页缓存
    audit_log(AuditAction.WATCHLIST_SYNC, f"同步自选列表: {len(body.codes)} 只基金", extra={"count": len(body.codes), "codes": body.codes[:10]})
    return {"ok": True, "n": len(body.codes)}


# ---------------------------------------------------------------------------
# 线上版(SVue SPA)接口别名
# 前端静态包(static/assets/index-*.js)的自选存储调用 /api/watch/list、
# /api/watch/add(POST)、/api/watch/sync(PUT)，此前后端仅注册了 /api/watchlist
# 与 POST /api/watch/sync，导致添加自选时接口 404/405 被前端静默吞掉、
# 自选无法真正写入后端(v2.2.2 修复)。
# ---------------------------------------------------------------------------
@router.get("/api/watch/list", dependencies=[Depends(optional_api_key)],
         summary="自选列表（线上版兼容别名）",
         responses={200: {"description": "自选基金列表"}})
async def watch_list_alias():
    """线上版自选列表接口别名 → /api/watchlist GET。"""
    return await watchlist()


@router.post("/api/watch/add", dependencies=[Depends(require_api_key)],
         summary="添加自选（线上版兼容别名）",
         responses={200: {"description": "添加成功"}, 400: {"description": "代码格式错误"}, 404: {"description": "基金不存在"}})
async def watch_add_alias(body: WatchAdd):
    """线上版添加自选接口别名 → /api/watchlist POST。"""
    return await watchlist_add(body)


@router.put("/api/watch/sync", dependencies=[Depends(require_api_key)],
         summary="自选同步（线上版兼容别名）",
         responses={200: {"description": "同步成功"}})
async def watch_sync_put_alias(body: WatchSync):
    """线上版自选同步接口别名(PUT) → /api/watchlist PUT。"""
    return await watchlist_sync(body)

