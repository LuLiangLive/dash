"""
modules/fund/universe_manager.py —— 基金代码库管理 (v0.86.0)

从 collector.fund_universe 迁移（v2.5.5架构重构）
- 全市场C类权益基金代码库(持久化,每周更新)
- 当日日涨幅前100 C类基金(每天更新)
- 合并去重后作为有效抓取池
"""

import json
import time
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
UNIVERSE_PATH = ROOT / "data" / "fund_universe.json"
DAILY_TOP_PATH = ROOT / "data" / "daily_top100.json"

# 代码库有效期: 7天(每周更新)
UNIVERSE_TTL_SECONDS = 7 * 24 * 3600
# 当日前100有效期: 1天
DAILY_TOP_TTL_SECONDS = 24 * 3600


def _load_json(path: Path) -> Optional[dict]:
    """安全加载JSON文件。"""
    try:
        if path.exists():
            return json.load(open(path, encoding="utf-8"))
    except Exception:
        pass
    return None


def _save_json(path: Path, data: dict) -> None:
    """原子写入JSON文件。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(path) + ".tmp"
        json.dump(data, open(tmp, "w", encoding="utf-8"), ensure_ascii=False)
        import os
        os.replace(tmp, path)
    except Exception:
        pass


def _is_expired(updated_at: float, ttl: float) -> bool:
    """判断是否过期。"""
    return (time.time() - updated_at) > ttl


# ---------------------------------------------------------------------------
# 基金代码库(全市场C类,每周更新)
# ---------------------------------------------------------------------------

def load_fund_universe() -> list[dict]:
    """加载基金代码库。返回 [{code, name, ftype, sec, themes, is_etf}, ...]"""
    data = _load_json(UNIVERSE_PATH)
    if data and data.get("funds"):
        return data["funds"]
    return []


def fund_universe_codes() -> set[str]:
    """获取基金代码库中的代码集合。"""
    return {f["code"] for f in load_fund_universe()}


def is_universe_expired() -> bool:
    """判断基金代码库是否过期(>7天)。"""
    data = _load_json(UNIVERSE_PATH)
    if not data:
        return True
    updated_at = data.get("updated_at", 0)
    return _is_expired(updated_at, UNIVERSE_TTL_SECONDS)


def rebuild_fund_universe(max_funds: int = 6000, verbose: bool = True) -> list[dict]:
    """重建基金代码库(全市场C类权益基金,统一剔除债券/货币/持有期/定开/封闭基金)。

    从东方财富拉取全市场C类基金,去重后保存。
    建议每周调用一次。
    v2.9.8: 统一在构建阶段剔除所有不需要的类型:
      - 债券/货币/固收/偏债/理财/现金/纯债/中短债/同业存单
      - 持有期/定开/封闭基金
    这样代码库中只保留C类权益基金(含ETF/指数),后续榜单无需重复筛选。
    """
    from collector import fetcher
    from modules.common.filters import is_holding_period, is_bond_like
    if verbose:
        pass
    pool = fetcher.effective_pool(mode="full", max_funds=max_funds)

    # v2.9.8: 统一剔除不需要的类型
    before_count = len(pool)

    # 1. 剔除债券/货币/固收/偏债等
    pool = [f for f in pool if not is_bond_like(f)]
    after_bond = len(pool)

    # 2. 剔除持有期/定开/封闭基金
    pool = [f for f in pool if not is_holding_period(f)]
    after_holding = len(pool)

    if verbose:
        print(f"[基金代码库] 统一剔除: {before_count} -> 剔除债券{before_count-after_bond}只 -> 剔除持有期{after_bond-after_holding}只 -> 最终{after_holding}只")

    data = {
        "updated_at": time.time(),
        "updated_at_str": time.strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(pool),
        "funds": pool,
    }
    _save_json(UNIVERSE_PATH, data)
    if verbose:
        pass
    return pool


def ensure_fund_universe(max_funds: int = 6000, verbose: bool = True) -> list[dict]:
    """确保基金代码库可用(过期则自动重建)。"""
    if is_universe_expired() or not load_fund_universe():
        return rebuild_fund_universe(max_funds, verbose)
    funds = load_fund_universe()
    if verbose:
        data = _load_json(UNIVERSE_PATH)
    return funds


# ---------------------------------------------------------------------------
# 当日日涨幅前100 C类基金(每天更新)
# ---------------------------------------------------------------------------

def fetch_daily_top100(verbose: bool = True) -> list[dict]:
    """拉取当日日涨幅前100的C类基金。

    从东方财富排行接口按日涨幅降序拉取,过滤C类,取前100。
    """
    from collector import fetcher
    from collector import sector

    if verbose:
        pass

    # 按日涨幅降序拉取(四类合并)
    all_funds = []
    for ft in ("gp", "zs", "hh", "qdii"):
        # sc=rzf 按日涨幅排序, st=desc 降序, pn=500 多拉一些用于过滤C类
        funds = fetcher.fetch_rank(ft, pn=500)
        all_funds.extend(funds)

    # 按code去重
    merged = {}
    for f in all_funds:
        merged[f["code"]] = f

    # 过滤C类且非债券/货币
    c_funds = [f for f in merged.values() if fetcher._is_c_share(f.get("name", ""))]

    # v2.9.8: 剔除持有期/定开/封闭基金
    from modules.common.filters import is_holding_period
    before_count = len(c_funds)
    c_funds = [f for f in c_funds if not is_holding_period(f)]
    after_count = len(c_funds)
    if verbose:
        print(f"[当日涨幅前100] 剔除持有期基金: {before_count} -> {after_count} (剔除{before_count-after_count}只)")

    # 按日涨幅降序排序(接口已经排序,但合并后可能乱序)
    c_funds.sort(key=lambda x: x.get("rzf", 0), reverse=True)

    # 补充元数据并剔除固收类型
    result = []
    for f in c_funds:
        name = f.get("name", "")
        ftype = sector.infer_type(name)
        # 剔除固收类(偏债混合/绝对收益/债券型/货币型)
        if fetcher._is_fixed_income_type(ftype):
            continue
        result.append({
            "code": f["code"],
            "name": name,
            "ftype": ftype,
            "sec": sector.infer_sector(name),
            "themes": sector.infer_themes(name),
            "is_etf": 1 if ("ETF" in name or "联接" in name) else 0,
            "rzf": f.get("rzf", 0),  # 日涨幅
        })
        if len(result) >= 100:
            break

    if verbose:
        if result:
            pass

    return result


def load_daily_top100() -> list[dict]:
    """加载当日前100缓存。"""
    data = _load_json(DAILY_TOP_PATH)
    if data and data.get("funds"):
        return data["funds"]
    return []
def is_daily_top_expired() -> bool:
    """判断当日前100是否过期(>1天)。"""
    data = _load_json(DAILY_TOP_PATH)
    if not data:
        return True
    updated_at = data.get("updated_at", 0)
    return _is_expired(updated_at, DAILY_TOP_TTL_SECONDS)


def ensure_daily_top100(verbose: bool = True) -> list[dict]:
    """确保当日前100可用(过期则自动拉取)。"""
    if is_daily_top_expired() or not load_daily_top100():
        funds = fetch_daily_top100(verbose)
        data = {
            "updated_at": time.time(),
            "updated_at_str": time.strftime("%Y-%m-%d %H:%M:%S"),
            "date": time.strftime("%Y-%m-%d"),
            "count": len(funds),
            "funds": funds,
        }
        _save_json(DAILY_TOP_PATH, data)
        return funds
    funds = load_daily_top100()
    if verbose:
        data = _load_json(DAILY_TOP_PATH)
    return funds


# ---------------------------------------------------------------------------
# 有效抓取池(代码库 + 当日前100,去重)
# ---------------------------------------------------------------------------

def get_effective_pool(max_funds: int = 6000, verbose: bool = True) -> tuple[list[str], dict]:
    """获取有效抓取池 = 基金代码库 ∪ 当日前100(去重)。

    Returns:
        (codes, stats)
        codes: 去重后的代码列表
        stats: {"代码库": N1, "当日前100": N2, "去重后": N3}
    """
    # 确保代码库可用(过期自动重建)
    universe = ensure_fund_universe(max_funds, verbose)
    universe_codes = {f["code"] for f in universe}

    # 确保当日前100可用(过期自动拉取)
    daily_top = ensure_daily_top100(verbose)
    daily_codes = {f["code"] for f in daily_top}

    # 合并去重
    merged = universe_codes | daily_codes

    stats = {
        "代码库": len(universe_codes),
        "当日前100": len(daily_codes),
        "去重后": len(merged),
        "新增热门": len(daily_codes - universe_codes),
    }

    if verbose:
        pass

    return sorted(merged), stats


def get_effective_pool_with_meta(max_funds: int = 6000, verbose: bool = True) -> tuple[list[dict], dict]:
    """获取有效抓取池(含元数据) = 基金代码库 ∪ 当日前100(去重)。

    Returns:
        (pool, stats)
        pool: [{code, name, ftype, sec, themes, is_etf}, ...]
        stats: 统计信息
    """
    universe = ensure_fund_universe(max_funds, verbose)
    daily_top = ensure_daily_top100(verbose)

    # 合并(当日前100覆盖代码库中的同名基金,保留日涨幅信息)
    merged = {}
    for f in universe:
        merged[f["code"]] = f
    for f in daily_top:
        merged[f["code"]] = f  # 覆盖,保留日涨幅

    pool = list(merged.values())

    # v2.9.8: 双重保险 - 剔除持有期/定开/封闭基金(即使缓存中有旧数据也能过滤)
    from modules.common.filters import is_holding_period
    before_count = len(pool)
    pool = [f for f in pool if not is_holding_period(f)]
    after_count = len(pool)

    stats = {
        "代码库": len(universe),
        "当日前100": len(daily_top),
        "去重后": len(pool),
        "剔除持有期": before_count - after_count,
    }

    return pool, stats
