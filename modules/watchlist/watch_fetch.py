"""
api/watch_fetch.py —— 自选实时获取路由模块

从 main.py 拆分出的自选实时获取相关路由，包括：
- 单只基金实时获取
- 批量基金实时获取（线程池并行）

所有路由通过 APIRouter 注册，main.py 中 include_router 引入。
"""
from __future__ import annotations

import asyncio
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

from fastapi import APIRouter, Depends, Query

import db
from auth import optional_api_key
from modules.fund.fund_service import load_nfm, series
from modules.fund.fund_detail import stats_full
from modules.score.score_service import save_score_snapshot

router = APIRouter(tags=["自选实时获取"])


def _fetch_fund_detail(code: str) -> dict | None:
    """获取单只基金的实时详情数据，统一评分口径，补算 vol/sharpe，返回近21日净值。

    被单只获取和批量获取共用，保证两者口径完全一致。
    """
    nfm = load_nfm()
    d = nfm.fetch_watch_detail(code)
    if not d or (d.get("name") is None and d.get("nav") is None):
        return None

    # 统一评分口径：优先使用 nfm.fetch_watch_detail 中统一服务计算的分数，
    # 只有当统一服务结果为 None 时才用 DB 旧值兜底
    f = db.get_fund(code)
    if f:
        for _k in ("score", "ad_score", "earn_score", "suggest",
                   "ms", "reco", "reco_score",
                   "reco_days", "prev_reco", "prev_reco_days"):
            if d.get(_k) is None and f.get(_k) is not None:
                d[_k] = f[_k]

    # 兜底展示字段：vol/sharpe 若缺失，用本地净值实时补算
    if d.get("vol") is None or d.get("sharpe") is None:
        try:
            _dv, _vv = series(code, 42)
            _s2 = stats_full(_vv) if len(_vv) >= 2 else None
            if _s2:
                if d.get("vol") is None:
                    d["vol"] = _s2.get("vol")
                if d.get("sharpe") is None:
                    d["sharpe"] = _s2.get("sharpe")
        except Exception:
            pass

    # 返回近21日历史净值用于走势图
    try:
        _dv21, _vv21 = series(code, 21)
        if _dv21 and _vv21:
            d["navs"] = _vv21
            d["dates"] = _dv21
    except Exception:
        pass

    d["_v"] = 2  # 版本标记：旧缓存在前端会被重新 fetch 刷新为最新口径

    # v2.1.5: 优先使用批量评分缓存（方案C+），确保所有地方分数一致
    # v2.1.6: 同时覆盖 tscore 字段，因为前端 normalizeCard 优先使用 tscore
    try:
        from modules.score.score_service import get_pool_score
        pool_score = get_pool_score(code, auto_compute=False)
        if pool_score:
            new_score = pool_score.get("dual_score", d.get("score"))
            d["score"] = new_score
            d["tscore"] = new_score  # 同时覆盖 tscore，前端优先使用此字段
            d["ad_score"] = pool_score.get("ad_score", d.get("ad_score"))
            d["earn_score"] = pool_score.get("earn_score", d.get("earn_score"))
            d["calmar_score"] = pool_score.get("calmar_score")
    except Exception:
        pass

    # 保存分数快照（每日一次）
    try:
        save_score_snapshot(code, d)
    except Exception:
        pass

    return d


@router.get("/api/watch/fetch", dependencies=[Depends(optional_api_key)],
         summary="单只基金实时获取",
         responses={200: {"description": "基金实时详情数据（含评分、净值、近21日走势）"}})
async def watch_fetch(code: str):
    """自选添加实时获取：调用 night_fund_monitor.fetch_watch_detail 立即返回整套数据。

    - **code**: 6位基金代码
    """
    if not re.fullmatch(r"\d{6}", code):
        return {"ok": False, "msg": "基金代码需为 6 位数字"}
    try:
        d = await asyncio.to_thread(_fetch_fund_detail, code)
        if not d:
            return {"ok": False, "msg": "实时获取失败,请确认代码或稍后重试"}
        return {"ok": True, "data": d}
    except Exception as e:
        return {"ok": False, "msg": "实时获取异常: " + str(e)[:60]}


def _do_watch_fetch_batch(code_list: list[str]):
    result = {}
    failed = []
    max_workers = min(5, len(code_list))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_fetch_fund_detail, code): code for code in code_list}
        for future in as_completed(futures):
            code = futures[future]
            try:
                d = future.result()
                if d is None:
                    failed.append(code)
                else:
                    result[code] = d
            except Exception:
                failed.append(code)
    return {"ok": True, "data": result, "failed": failed}

@router.get("/api/watch/fetch-batch", dependencies=[Depends(optional_api_key)],
         summary="批量基金实时获取",
         responses={200: {"description": "多只基金实时详情数据（线程池并行获取）"}})
async def watch_fetch_batch(codes: str = Query(..., description="逗号分隔的6位基金代码，最多20只，如 000001,110011")):
    """批量自选实时获取：一次请求拿回多只基金数据，替代前端逐个 fetch 导致的多次重渲染。

    codes: 逗号分隔的6位基金代码，最多20只。
    返回 {ok: true, data: {code: fund_dict, ...}, failed: [code, ...]}

    v0.76.2 perf: 消除首屏 N 次 /api/watch/fetch 串行请求，12只自选从12次HTTP降到1次。
    v2026-08-28 perf: 线程池并行获取，8只基金从47.9秒降到~6秒（提升8倍）。
    """
    code_list = [c.strip() for c in codes.split(",") if c.strip()]
    code_list = [c for c in code_list if re.fullmatch(r"\d{6}", c)][:20]
    if not code_list:
        return {"ok": False, "msg": "无有效基金代码", "data": {}, "failed": []}
    return await asyncio.to_thread(_do_watch_fetch_batch, code_list)


