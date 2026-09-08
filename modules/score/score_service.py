"""
services/score_service.py — 评分服务

包含两部分功能：
1. 评分快照服务：保存和加载基金评分历史快照（用于分数演化图表）
2. 统一分数计算服务：所有地方（自选卡片、弹窗、对比、榜单）都调用这个服务计算分数，
   确保综合分、抗跌分、收益分在所有地方一致。

计算逻辑（v2.1.5 方案C+）：
- 收益分：6周期加权线性（EARN_CYCLE_WEIGHTS），50基准 + 加权收益×3 + 修复奖励(≤5) - 短期暴跌惩罚(≤15)（见 scoring_v3）
- 抗跌分：6指标线性（风险指标扣分 + 反弹加分，AD_*_COEFF）
- 卡玛比率百分位：近6月卡玛，回撤<1%封顶
- 综合分：收益42.5% + 抗跌42.5% + 卡玛15%（V3统一口径，卡玛百分位缺失按中位50）
"""
from __future__ import annotations

import importlib
import time
from typing import Optional

_NFM = None

# 评分快照缓存（内存）
_score_snapshots: dict[str, list] = {}
_score_snapshot_last_save: float = 0

# v2.1.5: 批量评分缓存（池内百分位归一化需要所有基金数据）
_pool_scores_cache: dict[str, dict] = {}
_pool_scores_last_update: float = 0
_POOL_SCORES_TTL = 3600  # 1小时缓存


def _load_nfm():
    """动态加载 analysis_pipeline/night_fund_monitor.py"""
    global _NFM
    if _NFM is None:
        _NFM = importlib.import_module("analysis_pipeline.night_fund_monitor")
    return _NFM


# ============================================================
# v2.1.5: 批量评分（池内百分位归一化）
# ============================================================

def compute_pool_scores(force: bool = False) -> dict:
    """批量计算池内所有基金的评分（方案C+）。

    从数据库加载所有基金的指标和抗跌指标，批量计算评分并缓存。
    池内百分位归一化需要所有基金数据，所以必须批量计算。

    Args:
        force: 是否强制重新计算（忽略缓存）

    Returns:
        {code: {earn_score, ad_score, calmar_score, dual_score}}
    """
    global _pool_scores_cache, _pool_scores_last_update

    # 检查缓存
    if not force and _pool_scores_cache and (time.time() - _pool_scores_last_update) < _POOL_SCORES_TTL:
        return _pool_scores_cache

    try:
        import db as _db
        from collector.scoring_v3 import compute_pool_scores as _compute_v3

        # 从数据库加载所有基金
        funds = _db.list_funds(limit=10000) or []
        if not funds:
            return {}

        # 获取大跌日数据（所有基金共用）
        nfm = _load_nfm()
        ddown_full = nfm.fetch_ddown()

        # 构建评分池
        pool = []
        for f in funds:
            code = f.get("code")
            if not code:
                continue
            # 从数据库加载净值
            rows = _db.get_nav(code, limit=126) or []  # 近6月约126个交易日
            navs = [{"date": r.get("date"), "dwjz": r.get("dwjz")}
                    for r in rows if r.get("date") and r.get("dwjz")]
            navs.sort(key=lambda x: x["date"])
            if len(navs) < 30:
                continue

            # 计算指标
            try:
                cm = nfm.calc_metrics(navs) or {}
            except Exception:
                cm = {}

            # 计算抗跌指标
            try:
                ma = nfm.anti_metrics(code, ddown_full, navs=navs) or {}
            except Exception:
                ma = {}

            pool.append({
                "code": code,
                "metrics": cm,
                "ad_metrics": {
                    "dd_avg": ma.get("fund_avg"),
                    "repair_5d": ma.get("repair"),
                    "repair_10d": ma.get("repair_10d"),
                },
                "excess_metrics": None,  # 超额收益暂未计算，后续可添加
            })

        # 批量计算评分
        results = _compute_v3(pool)

        # 更新缓存
        _pool_scores_cache = results
        _pool_scores_last_update = time.time()

        return results
    except Exception as e:
        return {}


def get_pool_score(code: str, auto_compute: bool = False) -> Optional[dict]:
    """从批量评分缓存中获取单只基金的评分。

    Args:
        code: 基金代码
        auto_compute: 是否自动触发批量评分计算（默认False，避免单基金查询时触发耗时计算）
    """
    if auto_compute:
        cache = compute_pool_scores()
    else:
        # 只使用已有缓存，不触发计算
        global _pool_scores_cache, _pool_scores_last_update
        if _pool_scores_cache and (time.time() - _pool_scores_last_update) < _POOL_SCORES_TTL:
            cache = _pool_scores_cache
        else:
            cache = {}
    return cache.get(code)


# ============================================================
# 统一分数计算服务
# ============================================================

def calc_scores(
    code: str,
    navs: Optional[list] = None,
    stats: Optional[dict] = None,
) -> dict:
    """[DEPRECATED v2.9.43] 单基金实时分数计算（旧算法）。

    ⚠️ 已废弃：全局统一使用 compute_scores_v2 算法。
    唯一分数来源：一键更新阶段4 recompute_ad 对上榜+自选基金批量调用
    modules.score.scoring.compute_scores_v2() 计算后写入数据库 funds 表。
    所有消费方（卡片/弹窗/对比页/夜间监控）都应直接从数据库读取，
    不再调用本函数实时计算。

    保留本函数仅为向后兼容，避免潜在的未发现调用方报错。
    非上榜/非自选基金数据库中可能没有分数，显示"暂无评分"即可（用户看不到这些基金）。

    v2.1.5: 优先使用批量评分缓存（池内百分位归一化），确保所有地方分数一致。
    如果批量缓存中没有该基金，则降级使用单基金计算（旧算法）。

    注意：分数计算需要足够长的净值序列（至少60条，用于计算最大回撤、下跌波动率等）。
    如果传入的navs不足60条，会从数据库加载120条净值序列用于计算。

    Args:
        code: 基金代码
        navs: 净值序列 [{"date": "2026-01-01", "dwjz": 1.23}, ...]，可选
        stats: 已计算的指标 {d3, d5, d7, d10, ...}，可选

    Returns:
        {ad_score, earn_score, dual}，计算失败返回 None
    """
    # v2.1.5: 优先使用批量评分缓存（不触发自动计算，避免单基金查询时耗时过长）
    pool_score = get_pool_score(code, auto_compute=False)
    if pool_score:
        return {
            "ad_score": pool_score.get("ad_score"),
            "earn_score": pool_score.get("earn_score"),
            "dual": pool_score.get("dual_score"),
        }

    # 降级：单基金计算（旧算法）
    nfm = _load_nfm()

    # 分数计算需要足够长的净值序列（至少60条）
    # 如果传入的navs不足60条，从数据库加载120条
    if navs is None or len(navs) < 60:
        try:
            import db
            rows = db.get_nav(code, limit=120) or []
            navs = [{"date": r.get("date"), "dwjz": r.get("dwjz")} for r in rows if r.get("date") and r.get("dwjz")]
            # 按日期升序排序（最旧的在前），确保周期涨幅计算正确
            navs.sort(key=lambda x: x["date"])
        except Exception:
            navs = navs or []

    if not navs:
        return {"ad_score": None, "earn_score": None, "dual": None}

    # 计算抗跌指标
    try:
        _ddown_full = nfm.fetch_ddown()  # 完整三元组 (dates, pcts, idx_map)
        _ma = nfm.anti_metrics(code, _ddown_full, navs=navs)
    except Exception:
        _ma = None

    if _ma is None:
        return {"ad_score": None, "earn_score": None, "dual": None}

    # 如果没有传入stats，从navs计算
    if stats is None:
        try:
            cm = nfm.calc_metrics(navs) or {}
            stats = cm
        except Exception:
            stats = {}

    _xa = {
        "code": code,
        "d3": stats.get("d3"),
        "d5": stats.get("d5"),
        "d7": stats.get("d7"),
        "d10": stats.get("d10"),
    }

    # v2.9.57: 统一使用scoring_v3线性评分（单只计算，卡玛用默认50）
    _ad_metrics = {
        "dd_avg": _ma.get("fund_avg"),
        "repair_5d": _ma.get("repair"),
        "repair_10d": _ma.get("repair_10d"),
        "mdd": (stats or {}).get("mdd"),
        "down_vol": (stats or {}).get("down_vol"),
        "max_daily_drop": (stats or {}).get("max_daily_drop"),
    }
    from collector.scoring_v3 import compute_ad_score, compute_earn_score, compute_dual_score
    ad_score = compute_ad_score(_ad_metrics)
    earn_score = compute_earn_score(stats or {}, repair_5d=_ma.get("repair"))
    _calmar = 50  # 单只计算无池内卡玛数据
    dual = compute_dual_score(earn_score, ad_score, _calmar)

    result = {
        "ad_score": ad_score,
        "earn_score": earn_score,
        "dual": dual,
    }
    return result


# ============================================================
# 评分快照服务
# ============================================================

def save_score_snapshot(code: str, fund_detail: dict):
    """保存单个基金的评分快照（每日一次）。

    从基金详情数据中提取 score/ad_score/earn_score，保存到内存缓存。
    """
    global _score_snapshots, _score_snapshot_last_save
    try:
        today = time.strftime("%Y-%m-%d")
        score = fund_detail.get("score")
        ad_score = fund_detail.get("ad_score")
        earn_score = fund_detail.get("earn_score")
        if score is None and ad_score is None and earn_score is None:
            return
        if code not in _score_snapshots:
            _score_snapshots[code] = []
        # 检查今天是否已经保存过
        today_exists = any(s.get("date") == today for s in _score_snapshots[code])
        if not today_exists:
            _score_snapshots[code].append({
                "date": today,
                "score": score,
                "ad_score": ad_score,
                "earn_score": earn_score,
            })
            # 只保留最近30天
            _score_snapshots[code] = _score_snapshots[code][-30:]
    except Exception:
        pass


def save_score_snapshots():
    """保存所有评分快照（空实现，兼容旧接口）。"""
    global _score_snapshot_last_save
    _score_snapshot_last_save = time.time()


def load_score_snapshots() -> dict:
    """加载所有基金的评分快照。"""
    return _score_snapshots


async def aload_score_snapshots() -> dict:
    """异步加载所有基金的评分快照。"""
    return _score_snapshots
