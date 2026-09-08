"""
modules/rank/router.py —— 榜单查询路由模块

从 api/ranks.py 迁移（v2.5.5架构重构 - 第4步按领域模块重组）
包括：
- 榜单查询（panel/sub 结构从 rank_config 读取，禁止硬编码）
- 历史榜单日期列表

所有路由通过 APIRouter 注册，main.py 中 include_router 引入。
"""
from __future__ import annotations

import logging
from fastapi import APIRouter, Depends, Query

import db
from auth import optional_api_key
from rank_config import get_panels, is_deprecated, is_valid_rank

logger = logging.getLogger(__name__)

router = APIRouter(tags=["榜单查询"])


@router.get("/api/ranks", dependencies=[Depends(optional_api_key)],
         summary="获取榜单数据",
         responses={200: {"description": "按面板/子面板组织的榜单数据"}})
async def get_ranks(
    date: str | None = Query(default=None, description="榜单日期 YYYY-MM-DD,默认最新"),
    panel: str | None = Query(default=None, description="按面板筛选,如 day/reco/warn,默认全部"),
    sub: str | None = Query(default=None, description="按子面板筛选,如 当日/抗跌/自选,默认全部"),
):
    """榜单查询：panel/sub 结构从 rank_config 动态读取。

    v1.1.9: 过滤改用 is_valid_rank() 白名单 —— 只返回 rank_config 中当前存在的
    panel/sub。旧的黑名单 is_deprecated() 只能挡住已知的历史遗留
    （稳涨/强趋势/开仓榜单…），未知的残留榜单仍会漏进响应。

    v2026-09: 新增 panel/sub 可选查询参数，支持前端按板块/子板块筛选榜单。
    不传时返回全部面板（与原有行为一致）。
    """
    if date is None:
        date = await db.alatest_rank_date()
    if not date:
        return {"date": None, "panels": {}, "msg": "暂无榜单数据,请先运行采集器"}
    rows = await db.aget_ranks(date, panel=panel, sub=sub)
    # 从 rank_config 读取面板结构；若指定了 panel，则只初始化该面板
    _all_panels = get_panels()
    if panel and panel in _all_panels:
        panels = {panel: {}}
    else:
        panels = {p: {} for p in _all_panels}  # 从 rank_config 读取，禁止硬编码
    skipped = 0
    # 收集所有基金代码，用于批量补充themes字段
    all_codes = set()
    for r in rows:
        panel, sub = r["panel"], r["sub"]
        if panel not in panels:
            skipped += 1
            continue
        # 白名单：不在 rank_config 里的 panel/sub 一律忽略（含历史遗留榜单）
        if not is_valid_rank(panel, sub):
            skipped += 1
            if not is_deprecated(panel, sub):
                logger.warning("未知榜单 panel=%s sub=%s (date=%s) 已忽略", panel, sub, date)
            continue
        meta = r["meta"]
        if isinstance(meta, str):
            import json
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        code = meta.get("code")
        if code:
            all_codes.add(code)
        panels[panel].setdefault(sub, []).append(meta)

    # 批量从funds表补充sec和themes字段（榜单构建时持仓可能还没采集，meta中sec/themes为空）
    # v2.11.2: 同时用 funds 表"当前值"覆盖展示型通用指标(d1..m1/m3/m6/y1/dd20/ms/分数等)——
    # 榜单快照与 funds 表同一天由同一更新写入, 这里保证返回给前端的卡片数字始终读"当前最新",
    # 但保留榜单专属字段(tscore/rank/上榜说明)不被覆盖。
    _LIVE_KEYS = (
        "d1", "d2", "d3", "d5", "d7", "d10", "dn7", "ms", "dd20",
        "mdd", "mdd_days", "mdd_status", "vol", "down_vol", "down_sharpe",
        "calmar", "pl", "hi_cnt", "dd_from_hi", "streak",
        "m1", "m3", "m6", "y1", "ad_score", "earn_score", "score",
        "reco", "reco_score", "reco_days", "nav", "nav_date", "yindie",
    )
    if all_codes:
        try:
            conn = db.get_conn()
            placeholders = ",".join("?" for _ in all_codes)
            _cols_sql = ", ".join(("code", "sec", "themes") + _LIVE_KEYS)
            cursor = conn.execute(
                f"SELECT {_cols_sql} FROM funds WHERE code IN ({placeholders})",
                list(all_codes)
            )
            sec_map = {}
            themes_map = {}
            live_map: dict[str, dict] = {}
            for row in cursor.fetchall():
                code = row["code"]
                # sec: 只有当meta中sec为"其他"或空时才补充
                sec = row["sec"]
                if sec and sec != "其他":
                    sec_map[code] = sec
                # themes: 只有非空时才补充
                themes = row["themes"]
                if themes and themes != '[]' and themes != 'null':
                    themes_map[code] = themes
                live_map[code] = {k: row[k] for k in _LIVE_KEYS}
            # 补充到每个meta中
            import json
            for panel_data in panels.values():
                for items in panel_data.values():
                    for meta in items:
                        code = meta.get("code")
                        if not code:
                            continue
                        # 补充sec: meta中sec为"其他"或空时，用funds表中的值
                        current_sec = meta.get("sec")
                        if (not current_sec or current_sec == "其他") and code in sec_map:
                            meta["sec"] = sec_map[code]
                        # 补充themes: meta中themes为空时，用funds表中的值
                        current_themes = meta.get("themes")
                        # 处理空值：None、空数组、空字符串、字符串"[]"都视为空
                        is_empty = (
                            current_themes is None
                            or current_themes == []
                            or current_themes == ""
                            or (isinstance(current_themes, str) and current_themes.strip() in ("[]", "null", ""))
                        )
                        if is_empty and code in themes_map:
                            try:
                                themes_val = themes_map[code]
                                if isinstance(themes_val, str):
                                    themes_val = json.loads(themes_val)
                                meta["themes"] = themes_val
                            except Exception:
                                pass
                        # v2.11.2: 用 funds 当前值覆盖展示型指标(仅在基金表有值且非空时覆盖,
                        # 避免把当天尚未算好的字段写空; tscore/rank 属榜单专属, 不覆盖)
                        f_row = live_map.get(code)
                        if f_row:
                            for k in _LIVE_KEYS:
                                v = f_row.get(k)
                                if v is not None and not (isinstance(v, str) and v == ""):
                                    meta[k] = v
        except Exception as e:
            logger.warning("补充sec/themes/live字段失败: %s", e)

    if skipped:
        logger.info("忽略 %d 条非当前榜单数据 (date=%s)", skipped, date)

    # v2.9.51: 删除get_pool_score覆盖逻辑，统一使用rank_snapshots.meta中的compute_scores_v2分数
    # 原因：v2.9.43已全局统一使用compute_scores_v2，scoring_v3是历史遗留，覆盖导致卡片与弹窗分数不一致

    # v2.2.2: 推荐榜单按综合分降序重排(日榜仍按涨幅周期语义排序, 不做综合分重排)
    try:
        reco_panels = panels.get("reco")
        if reco_panels:
            for sub, items in reco_panels.items():
                items.sort(
                    key=lambda it: ((it.get("tscore") if it.get("tscore") is not None
                                     else it.get("score"))
                                    if it.get("score") is not None
                                    else it.get("tscore")) or -9999,
                    reverse=True,
                )
                for _i, it in enumerate(items, start=1):
                    it["rank"] = _i
    except Exception as e:
        logger.warning("推荐榜单按综合分重排失败: %s", e)

    return {
        "date": date,
        "panels": panels,
        "updated_at": (await db.ameta_updated()).get("latest_task_at"),
    }


@router.get("/api/rank_dates", dependencies=[Depends(optional_api_key)],
         summary="获取历史榜单日期列表",
         responses={200: {"description": "历史榜单日期列表（降序）"}})
async def rank_dates(limit: int = Query(default=30, description="返回日期数量，默认30")):
    """历史榜单日期列表。"""
    return {"dates": await db.alist_rank_dates(limit)}

