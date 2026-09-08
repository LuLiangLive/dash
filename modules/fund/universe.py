"""
modules/fund/universe.py —— 基金池构建

从 collector.fetcher 迁移（v2.5.5架构重构 - 阶段3）
负责：基金池构建、元数据推断、全市场代码池
"""
from __future__ import annotations

from modules.common.fallback import load_json as _load_json
from modules.rank.fetcher import fetch_all_market
from modules.fund.classifier import is_c_share as _is_c_share, is_fixed_income_type as _is_fixed_income_type


def _load_local_pool() -> list[dict]:
    """从 watchlist_data.json 读取本地基金池兜底（私有,fetcher 内部使用)."""
    wd = _load_json("watchlist_data.json")
    funds = wd.get("funds", {})
    out = []
    for code, f in funds.items():
        if f.get("name"):
            out.append({
                "code": code,
                "name": f.get("name"),
                "ftype": f.get("ftype") or "",
                "sec": f.get("sec") or "其他",
                "is_etf": bool(f.get("is_etf")),
            })
    return out


def _infer_meta(name: str) -> dict:
    """按名称推断 sec / ftype / themes / is_etf。"""
    from collector import sector
    sec = sector.infer_sector(name)
    ftype = sector.infer_type(name)
    themes = sector.infer_themes(name)
    is_etf = 1 if ("ETF" in name and ("联接" in name or "联接" not in name and "ETF" == name[-3:])) else 0
    if "ETF联接" in name:
        is_etf = 1
    elif "ETF" in name and "指数" not in name and "联接" not in name:
        is_etf = 1
    return {"sec": sec, "ftype": ftype, "themes": themes, "is_etf": is_etf}


def _build_full_market_pool(max_funds: int = 6000) -> list[dict]:
    """全市场代码池构建(按 ftype 扫东方财富). 私有,经 effective_pool 调用.

    v2.1.5: 推断 ftype 后额外剔除固收类(偏债混合/绝对收益/债券型/货币型),
            避免名称不含"债"字的偏债基金混入权益看板。
    """
    from collector import sector
    raw = [f for f in fetch_all_market() if _is_c_share(f["name"])]
    pool = []
    for f in raw:
        name = f["name"] or ""
        ftype = sector.infer_type(name)
        # 双重过滤: 名称关键词 + 类型判断,确保固收类不混入
        if _is_fixed_income_type(ftype):
            continue
        pool.append({
            "code": f["code"],
            "name": name,
            "ftype": ftype,
            "sec": sector.infer_sector(name),
            "themes": sector.infer_themes(name),
            "is_etf": 1 if ("ETF" in name or "联接" in name) else 0,
        })
        if len(pool) >= max_funds:
            break
    return pool


def effective_pool(mode: str = "full", max_funds: int = 6000,
                   fetch_name: bool = True) -> list[dict]:
    """抓净值用的统一代码池(v0.86.0: 删除精选池,统一用全市场模式).

    Args:
        mode:
          "full"    — 全市场在线抓(按 ftype 扫东方财富 gp/zs/hh/qdii),默认
          "local"   — 本地缓存 watchlist_data.json(网络不通兜底)
        max_funds: 各 mode 的上限
        fetch_name: 保留兼容参数,full 模式永远在线抓 name

    Returns:
        list[{code, name, ftype, sec, themes, is_etf}, ...]
    """
    if mode == "local":
        return _load_local_pool()[:max_funds]
    # 默认 full 模式
    return _build_full_market_pool(max_funds)
