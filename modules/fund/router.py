"""
modules/fund/router.py —— 基金基础信息路由模块

从 api/funds.py 迁移（v2.5.5架构重构 - 第4步按领域模块重组）
包括：
- 基金列表（按板块筛选）
- 基金基础信息
- 净值历史
- 持仓主题+重仓股（按需拉取，24小时缓存）
- 轻量档案（规模/成立日期）

所有路由通过 APIRouter 注册，main.py 中 include_router 引入。
"""
from __future__ import annotations

import asyncio
import json as _json
import time as _time

from fastapi import APIRouter, Depends, HTTPException, Query

import db
from auth import optional_api_key

router = APIRouter(tags=["基金基础信息"])


@router.get("/api/funds", dependencies=[Depends(optional_api_key)],
         summary="获取基金列表",
         responses={200: {"description": "基金列表（按综合分降序）"}})
async def funds(
    sec: str | None = Query(default=None, description="按板块筛选，如 股票型/混合型/债券型，默认全部"),
    limit: int = Query(default=100, description="返回数量上限，默认100"),
):
    """基金列表（按板块筛选，按综合分降序排列）。"""
    return {"funds": await db.alist_funds(sec=sec, limit=limit)}


@router.get("/api/fund_names", dependencies=[Depends(optional_api_key)],
         summary="获取基金名称映射（轻量）")
async def fund_names():
    """轻量接口：仅返回基金代码到名称的映射 {code: name}，约200KB。"""
    try:
        conn = db.get_conn()
        rows = conn.execute("SELECT code, name FROM funds WHERE name IS NOT NULL AND name != ''").fetchall()
        return {row["code"]: row["name"] for row in rows}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取基金名称映射失败: {str(e)}")



@router.get("/api/funds/{code}", dependencies=[Depends(optional_api_key)],
         summary="获取基金基础信息",
         responses={
             200: {"description": "基金基础信息（含净值、涨幅、评分等）"},
             404: {"description": "未找到该基金"},
         })
async def fund_detail(code: str):
    """获取指定基金的基础信息。

    包含基金名称、净值、日涨幅、多周期涨幅、综合评分、抗跌分、收益分等。
    若评分字段为空，会自动从批量评分缓存补全并持久化。

    - **code**: 6位基金代码
    """
    f = await db.aget_fund(code)
    if not f:
        raise HTTPException(status_code=404, detail=f"未找到基金 {code}")
    f = dict(f)

    # v2.1.5: 修复d1(当日涨幅)和dwjz(单位净值)字段
    # funds表中字段名是nav，前端期望的是dwjz，同时返回两个字段兼容
    if f.get("nav") is not None and f.get("dwjz") is None:
        f["dwjz"] = f["nav"]

    # 如果d1为None，从nav_history表计算最新涨幅
    if f.get("d1") is None:
        try:
            navs = await db.aget_nav(code, limit=2)
            if navs and len(navs) >= 2:
                latest = navs[-1]
                prev = navs[-2]
                if prev.get("dwjz") and prev["dwjz"] > 0:
                    f["d1"] = round((latest["dwjz"] / prev["dwjz"] - 1) * 100, 4)
                    f["nav_date"] = latest.get("date")
                    f["dwjz"] = latest.get("dwjz")
                    f["nav"] = latest.get("dwjz")
        except Exception:
            pass

    # v2.11.0: 仅补全score/ad_score/earn_score(tscore/calmar_score为死列已停止写入)
    try:
        from modules.score.score_service import get_pool_score
        if f.get("score") is None or f.get("ad_score") is None or f.get("earn_score") is None:
            pool_score = get_pool_score(code, auto_compute=False)
            if pool_score:
                if f.get("score") is None:
                    f["score"] = pool_score.get("dual_score", f.get("score"))
                if f.get("ad_score") is None:
                    f["ad_score"] = pool_score.get("ad_score")
                if f.get("earn_score") is None:
                    f["earn_score"] = pool_score.get("earn_score")
    except Exception:
        pass
    return {"ok": True, "fund": f}


async def _backfill_nav_history(code: str, days: int = 500):
    """v2.5.3新增: 后台异步补全基金历史净值数据。

    当用户查看某基金时，如果历史数据不足，后台自动补全。
    参考 _do_fund_profile 的按需加载模式，但采用异步方式避免阻塞请求。

    Args:
        code: 基金代码
        days: 需要补全的历史天数（默认500交易日≈2年）
    """
    try:
        import datetime as _dt
        from collector import fetcher as _ft

        # 计算起始日期（自然日约2倍）
        start_date = (_dt.date.today() - _dt.timedelta(days=days * 2)).isoformat()

        # 抓取历史净值
        navs = _ft.fetch_nav_history(
            code,
            page_size=days,
            max_pages=max(1, days // 20),
            start_date=start_date,
        )

        if navs:
            # 批量入库（upsert by code+date）
            db.bulk_upsert_navs(code, navs)
        else:
            pass  # 无历史数据时跳过
    except Exception as e:
        pass  # 后台补全失败不影响主流程


@router.get("/api/funds/{code}/nav", dependencies=[Depends(optional_api_key)],
         summary="获取基金净值历史",
         responses={200: {"description": "净值历史序列（升序，含日涨跌幅）"}})
async def fund_nav(
    code: str,
    days: int = Query(default=120, description="返回的历史天数，默认120，最大1000"),
):
    """基金净值历史（升序，默认120天）。

    v2.5.3新增: 按需加载+自动补全机制。
    当请求的历史数据天数超过DB中已有的数据时，后台异步补全历史数据，
    立即返回现有数据，并标记data_status为"loading"或"complete"。
    解决上榜/自选基金历史数据不足导致的m6/y1等指标缺失问题。
    """
    rows = await db.aget_nav(code, limit=min(days, 1000))
    rows = list(reversed(rows))

    # v2.5.3: 检查数据是否充足，不足时后台异步补全
    data_status = "complete"
    if len(rows) < days and days > 60:  # 只在请求超过60天且数据不足时触发补全
        data_status = "loading"
        # 后台异步补全历史数据（不阻塞当前请求）
        import asyncio
        asyncio.create_task(_backfill_nav_history(code, days))

    # v2.1.5: 计算日涨跌幅(jzzzl)，nav_history表中没有这个字段
    for i in range(len(rows)):
        if i > 0:
            prev = rows[i-1]
            curr = rows[i]
            if prev.get("dwjz") and prev["dwjz"] > 0:
                rows[i]["jzzzl"] = round((curr["dwjz"] / prev["dwjz"] - 1) * 100, 4)
            else:
                rows[i]["jzzzl"] = None
        else:
            rows[i]["jzzzl"] = None
    return {"code": code, "navs": rows, "data_status": data_status, "requested_days": days, "available_days": len(rows)}


def _stocks_v118(v) -> bool:
    """v1.1.8 迁移标记: 老数据的 stocks 是 [name, pct] 两列, 新数据带第 3 列股票代码。

    两列 = v1.1.7 及以前的旧缓存(纯概念白名单口径, 覆盖率约 44%), 需要重算;
    三列 = 已按「概念优先 + 行业兜底」重算过, 可安全复用。
    """
    try:
        arr = v if isinstance(v, list) else _json.loads(v or "[]")
    except Exception:
        return False
    if not isinstance(arr, list) or not arr:
        return False
    return all(isinstance(x, (list, tuple)) and len(x) >= 3 for x in arr)


def _themes_are_inferred(v) -> bool:
    """v1.1.8: 识别「名称推断」的假主题 —— sector.infer_themes 产出的占比恒为 100.0。

    例: 020651(ETF 联接, 十大股票合计 0.2%) 库里存的是
    「农业/牧原股份/温氏股份/海大集团 100.0%」, 这不是持仓数据, 需要重算清掉。
    """
    try:
        arr = v if isinstance(v, list) else _json.loads(v or "[]")
    except Exception:
        return False
    if not isinstance(arr, list) or not arr:
        return False
    for t in arr:
        pct = t.get("pct") if isinstance(t, dict) else (t[1] if isinstance(t, (list, tuple)) and len(t) > 1 else None)
        try:
            if pct is not None and float(pct) >= 99.5:
                return True
        except Exception:
            pass
    return False


def _do_fund_holdings(code: str, force: bool):
    f = db.get_fund(code) or {}
    # v1.3.1: ETF/联接 若库里无真实持仓(stocks 空) → 直接返回空, 不再实时请求
    #   (展示层走 etf_abbr 简称)。注意不能按 is_etf 一刀切 —— 指数 LOF/联接外的
    #   指数基金(is_etf=1)的 jjcc 有真实持仓(如 013275 煤炭LOF), 已有
    #   stocks/themes 的必须走下方正常缓存逻辑。
    _st_ok = f.get("stocks") and f["stocks"] not in ("", "[]")
    if (f.get("is_etf") or f.get("etf_abbr")) and not _st_ok:
        return {"ok": True, "code": code, "cached": True, "themes": [], "stocks": []}
    # v1.3.0 缓存语义修正: stocks 三元组有效即视为已有缓存 —— themes 为空是
    # 合法终态(十大持仓合计 < 5% 阈值, holdings_breakdown 返回空), 旧实现
    # 因 themes 为空永远判为无缓存, 每次打开卡片都重新请求一遍。
    _stocks_ok = (
        f.get("stocks") and f["stocks"] not in ("", "[]")
        # v1.1.8: 旧格式 stocks(两列, 无代码)视为过期 —— 其 themes 是纯概念口径算的
        and _stocks_v118(f["stocks"])
    )
    _has_cache = _stocks_ok and not _themes_are_inferred(f.get("themes"))
    _cache_fresh = False
    if _has_cache and f.get("updated_at"):
        try:
            _updated = _time.mktime(_time.strptime(f["updated_at"], "%Y-%m-%d"))
            _cache_fresh = (_time.time() - _updated) < 86400
        except Exception:
            _cache_fresh = False
    if _has_cache and _cache_fresh and not force:
        return {"ok": True, "code": code, "cached": True, "themes": f["themes"], "stocks": f["stocks"]}
    try:
        from collector import fetcher
        # v1.1.8 修复: name 是 NOT NULL 列, 而旧实现只在「DB 里本来没名字」时才补 name,
        #   对已存在且已有名字的基金, upsert 走 INSERT ... ON CONFLICT 时 excluded.name
        #   是 NULL -> "NOT NULL constraint failed: funds.name", 整条写库失败,
        #   被下面 except 兜住返回 ok=False -> 前端 `if(hd.ok)` 不成立 -> 持仓永远不显示。
        #   这里统一先把 name 解析好, 无条件带进 upsert(name 属 _PRESERVE_ON_UPDATE,
        #   传空串会被 NULLIF+COALESCE 保留原值, 不会抹掉已有名称)。
        _name = f.get("name")
        if not _name:
            try:
                _name = fetcher.fetch_fund_name(code) or ""
            except Exception:
                _name = ""
        # v1.1.8: fetch_holdings_w_full 多返回股票代码列, 供行业兜底归类
        hs = fetcher.fetch_holdings_w_full(code)
        # v1.3.0: 空结果重试一次(限流/瞬时失败兜底); ETF 类 jjcc 实测无披露, 不重试
        if not hs and not (f.get("is_etf") or f.get("etf_abbr")):
            _time.sleep(0.8)
            hs = fetcher.fetch_holdings_w_full(code)
        th = fetcher.holdings_breakdown(hs, limit=8) if hs else []
        # v1.2.0: 名称推断兜底彻底移除 —— sector.infer_themes 产出的「核心资产 /
        #   资源周期」等标签 pct 恒为 100.0, 是编造数据不是真实持仓, 用户确认清除。
        #   拿不到真实持仓归类时 themes 保持为空, 前端显示「—」;
        #   ETF 类(is_etf=1)由 funds.etf_abbr 列提供简称展示(collector/etf_abbr.py)。
        stocks = [[str(s[0]), float(s[1]), (s[2] if len(s) > 2 else "")] for s in hs] if hs else []
        _upsert_data = {"code": code, "name": _name, "themes": th, "stocks": stocks,
                        "updated_at": _time.strftime("%Y-%m-%d")}
        db.upsert_fund(_upsert_data)
        return {"ok": True, "code": code, "cached": False, "themes": th, "stocks": stocks}
    except Exception as e:
        return {"ok": False, "code": code, "error": str(e), "themes": f.get("themes") or [], "stocks": f.get("stocks") or []}

@router.get("/api/funds/{code}/holdings", dependencies=[Depends(optional_api_key)],
         summary="获取基金持仓主题与重仓股",
         responses={200: {"description": "持仓主题分布和前十大重仓股（24小时缓存）"}})
async def fund_holdings(
    code: str,
    force: bool = Query(default=False, description="强制刷新，忽略24小时缓存"),
):
    """持仓主题+重仓股（按需拉取，24小时缓存）。

    v0.53.0: 一键更新不抓全量持仓(6000只太慢),用户查看时实时拉取并缓存24小时。
    策略: funds表有数据且updated_at在24h内 → 直接返回;否则实时抓取后写入缓存。
    v0.88.1: DB中无基金数据时不抛404,直接实时抓取(支持自选页面非榜单基金)
    """
    return await asyncio.to_thread(_do_fund_holdings, code, force)


@router.get("/api/funds/{code}/profile", dependencies=[Depends(optional_api_key)],
         summary="获取基金轻量档案",
         responses={200: {"description": "基金规模和成立日期等轻量档案信息"}})
async def fund_profile(code: str):
    """轻量档案接口: 快速返回基金规模(fund_scale)和成立日期(inception_date)。

    v2026-08-29: 原实现调用 fetch_watch_detail 只为拿 scale/est 两个档案字段,
    但该接口内部会抓取 120 条净值历史(实测 1.9s), 是弹窗"部分基金很慢"的主因。
    本接口直接从 DB 读, 缺失时补拉 fetch_basic(约 0.14s) 并落库 → 后续 0ms。
    """
    return await asyncio.to_thread(_do_fund_profile, code)


def _do_fund_profile(code: str):
    f = db.get_fund(code) or {}
    scale = f.get("scale")
    est = f.get("est")
    # DB 缺失时补拉轻量档案并落库
    if not scale or not est:
        try:
            from collector import fetcher as _ft
            _info = _ft.fetch_basic(code)
            if _info:
                if not scale:
                    scale = _info[0]
                if not est:
                    est = _info[1]
            # 落库: 档案字段变更极慢, 存下后后续请求不再走网络
            if scale or est:
                try:
                    _patch = dict(f) if f else {}
                    if scale and not _patch.get("scale"):
                        _patch["scale"] = scale
                    if est and not _patch.get("est"):
                        _patch["est"] = est
                    _patch["code"] = code
                    db.upsert_fund(_patch)
                except Exception:
                    pass
        except Exception:
            pass
    return {
        "ok": True,
        "code": code,
        "name": f.get("name") or "",
        "scale": scale,
        "inception": est,
        "fund_scale": scale,
        "inception_date": est,
    }

