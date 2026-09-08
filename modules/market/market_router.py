"""
api/market.py —— 市场与资讯路由模块

从 main.py 拆分出的市场相关路由，包括：
- 指数行情
- 降噪资讯
- 主题轮动
- 指数技术研判
- 行情仪表盘（涨跌统计/行业热力/资金流向/风格轮动/题材/北向/分级研判）

所有路由通过 APIRouter 注册，main.py 中 include_router 引入。
"""
from __future__ import annotations

import asyncio
from fastapi import APIRouter, Depends, HTTPException, Query

from modules.market import market_service as market
from auth import optional_api_key, require_api_key
from security.sanitizer import sanitize_html

router = APIRouter(prefix="/api/market", tags=["市场与资讯"])


def _sanitize_news_items(data):
    if isinstance(data, dict) and "items" in data:
        for item in data["items"]:
            if isinstance(item, dict) and item.get("content"):
                item["content"] = sanitize_html(item["content"])
    return data


@router.get("/indices", dependencies=[Depends(optional_api_key)], summary="获取市场指数行情", responses={200: {"description": "A股/韩股/美股核心指数实时点位、涨跌幅及开市状态"}})
async def market_indices():
    """市场指数行情: A股/韩股/美股 核心指数实时点位/涨跌幅/涨跌点 + 开市状态。"""
    return await asyncio.to_thread(market.get_market_indices)


@router.get("/news", dependencies=[Depends(optional_api_key)], summary="获取降噪资讯列表", responses={200: {"description": "降噪后的资讯列表（已做XSS过滤）"}})
async def market_news(date: str | None = Query(default=None, description="资讯日期 YYYY-MM-DD,默认最新")):
    result = await asyncio.to_thread(market.fetch_news_list, date)
    return _sanitize_news_items(result)
@router.post("/news/collect", dependencies=[Depends(require_api_key)], summary="手动触发资讯抓取", responses={200: {"description": "抓取任务已触发"}, 400: {"description": "slot参数无效"}})
async def market_news_collect(slot: str = Query(default="night", description="morning/noon/night")):
    """手动触发一期资讯抓取整理(测试/运维用)。"""
    if slot not in ("morning", "noon", "night"):
        raise HTTPException(400, "slot 须为 morning/noon/night")
    return await asyncio.to_thread(market.run_news_collect, slot)


@router.get("/research", dependencies=[Depends(optional_api_key)], summary="获取指数技术研判", responses={200: {"description": "K线+四段分析+情绪标签（带缓存）"}, 400: {"description": "code参数无效"}})
async def market_research_api(code: str = Query(default="sh000001", description="sh000001/sz399006/sh000688")):
    """指数技术研判: K线 + 四段分析 + 情绪标签(带缓存 lazy 更新)。"""
    import market_research
    if code not in ("sh000001", "sz399006", "sh000688"):
        raise HTTPException(400, "code 须为 sh000001/sz399006/sh000688")
    try:
        result = await asyncio.to_thread(market_research.get_research, code)
        if isinstance(result, dict) and result.get("research"):
            result["research"] = sanitize_html(result["research"])
        return result
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}


@router.get("/research/tabs", dependencies=[Depends(optional_api_key)], summary="获取指数研判Tab概览", responses={200: {"description": "三指数点位/涨跌幅/情绪标签概览"}})
async def market_research_tabs():
    """指数研判三级Tab概览(三指数点位/涨跌幅/情绪标签)。"""
    import market_research
    try:
        return {"ok": True, "items": await asyncio.to_thread(market_research.get_tabs)}
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}


@router.get("/dashboard", dependencies=[Depends(optional_api_key)], summary="获取市场行情仪表盘", responses={200: {"description": "所有市场模块数据汇总"}})
async def market_dashboard():
    """市场行情仪表盘：所有模块数据汇总。"""
    import market_data
    try:
        return await asyncio.to_thread(market_data.get_market_dashboard)
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}


@router.get("/stats", dependencies=[Depends(optional_api_key)], summary="获取全市场涨跌统计", responses={200: {"description": "全市场涨跌家数统计"}})
async def market_stats():
    """全市场涨跌统计。"""
    import market_data
    try:
        return await asyncio.to_thread(market_data.get_market_stats)
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}


@router.get("/industry", dependencies=[Depends(optional_api_key)], summary="获取行业热力数据", responses={200: {"description": "行业涨跌幅热力数据"}})
async def market_industry():
    """行业热力展示。"""
    import market_data
    try:
        return {"items": await asyncio.to_thread(market_data.get_industry_heat)}
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}


@router.get("/fundflow", dependencies=[Depends(optional_api_key)], summary="获取主力资金流向", responses={200: {"description": "主力资金流向对比数据"}})
async def market_fundflow():
    """主力资金流向对比。"""
    import market_data
    try:
        return await asyncio.to_thread(market_data.get_fund_flow)
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}


@router.get("/style", dependencies=[Depends(optional_api_key)], summary="获取风格轮动数据", responses={200: {"description": "多周期风格轮动表现"}})
async def market_style():
    """风格轮动多周期表现。"""
    import market_data
    try:
        return {"items": await asyncio.to_thread(market_data.get_style_rotation)}
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}


@router.get("/topics", dependencies=[Depends(optional_api_key)], summary="获取强势股题材统计", responses={200: {"description": "强势股题材分布统计"}})
async def market_topics():
    """强势股题材统计。"""
    import market_data
    try:
        return {"items": await asyncio.to_thread(market_data.get_hot_topics)}
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}


@router.get("/northbound", dependencies=[Depends(optional_api_key)], summary="获取北向资金数据", responses={200: {"description": "北向资金流入流出数据"}})
async def market_northbound():
    """北向资金数据。"""
    import market_data
    try:
        return await asyncio.to_thread(market_data.get_northbound)
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}


@router.get("/analysis", dependencies=[Depends(optional_api_key)], summary="获取行情分级研判", responses={200: {"description": "行情分级研判结果"}})
async def market_analysis():
    """行情分级研判。"""
    import market_data
    try:
        return {"items": await asyncio.to_thread(market_data.get_market_research)}
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}
