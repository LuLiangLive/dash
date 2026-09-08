"""
modules/anti/router.py —— 抗跌详情 API 路由
提供 /api/fund/anti_detail 接口，供前端基金详情弹窗使用。
注意：使用延迟导入避免循环导入（anti → common → anti）
"""
from fastapi import APIRouter, Depends, Query, HTTPException
from auth import optional_api_key

router = APIRouter(tags=["抗跌详情"])


@router.get("/api/fund/anti_detail", dependencies=[Depends(optional_api_key)])
async def get_anti_detail(
    code: str = Query(..., description="6位基金代码"),
):
    """
    抗跌详情（基金详情弹窗主数据）。
    返回 {ok, fund: {code, name, stats, resist, rise, enhance, ...}}
    """
    try:
        # 延迟导入避免循环导入
        from modules.anti.anti_detail import anti_detail_sync
        result = anti_detail_sync(code)
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"抗跌详情计算失败: {str(e)}")
