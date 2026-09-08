"""
api/score.py —— 基金评分历史路由模块

从 main.py 拆分出的评分历史相关路由，包括：
- 获取基金分数历史快照

所有路由通过 APIRouter 注册，main.py 中 include_router 引入。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from auth import optional_api_key
from modules.score.score_service import aload_score_snapshots

router = APIRouter(tags=["基金评分历史"])


@router.get("/api/score-history/{code}", dependencies=[Depends(optional_api_key)],
         summary="获取基金评分历史",
         responses={200: {"description": "最近30天的评分快照（综合分/抗跌分/收益分）"}})
async def score_history(code: str):
    """获取基金分数历史快照（最近30天的 score/ad_score/earn_score）。

    - **code**: 6位基金代码
    """
    snaps = await aload_score_snapshots()
    return {"ok": True, "data": snaps.get(code, [])}
