"""collector/rank_full.py

v0.47.0+: 全量重排所有榜单。

实现榜单计算（panel/sub 结构从 rank_config 读取，禁止硬编码）:
  day    × 5: 当日 / 两日 / 三日 / 七日 / 综合  — 数据源 funds 表全量 ∩ NAV 当日有效
  reco   × 4: 抗跌 / 自选 / 质量 / 超跌筑底      — 抗跌在12榜上榜池内(简化ad_score)，自选/质量/超跌筑底用全量
  warn   × 1: 动能衰减预警（特殊面板，不在 rank_config 中）

关键规则:
  - 抗跌榜的数据池 = 「上榜基金池」(day×4 ∪ reco/自选 ∪ reco/质量 ∪ reco/ETF 的并集)
  - 自选榜/质量榜用全量池
  - ETF榜按主题分组，每组前3
  - 综合榜 = day×4 前30并集，按上榜次数降序

调用入口:
  panels = rank_full.compute_all_panels(progress_cb=fn)

输出写入 rank_snapshots 表（覆盖当日 10 个 sub）.
"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Callable, Optional

import logging

logger = logging.getLogger(__name__)

import re
import db
from collector import ranker
from collector.filters import is_index_like, is_equity_excluded
# v1.1.0: 榜单结构从 rank_config 读取，禁止硬编码
from rank_config import get_subs, get_top_n

# 统一算法分数缓存（模块级，日榜和推荐榜单共用）
_UNIFIED_SCORE_CACHE = {}
_UNIFIED_DDOWN_CACHE = None
_V3_SCORES_PRECOMPUTED = False


def _precompute_v3_scores(funds_meta: list[dict], conn=None):
    """批量计算所有基金的scoring_v3评分，缓存到_UNIFIED_SCORE_CACHE。
    v2.1.6.1: 统一使用scoring_v3.py的评分算法，确保榜单排名和显示分数一致。
    v2.11.4 Q6: 支持 conn 参数, 加载 bench_items 并计算 excess_metrics 供 earn_score 使用.
    """
    global _UNIFIED_SCORE_CACHE, _UNIFIED_DDOWN_CACHE, _V3_SCORES_PRECOMPUTED

    # 如果已经计算过，直接返回
    if _V3_SCORES_PRECOMPUTED:
        return

    try:
        import importlib
        nfm = importlib.import_module("analysis_pipeline.night_fund_monitor")
        if _UNIFIED_DDOWN_CACHE is None:
            _UNIFIED_DDOWN_CACHE = nfm.fetch_ddown()
        ddown_full = _UNIFIED_DDOWN_CACHE

        # v2.11.4 Q6: 加载 bench_items(中证800 指数行), 供 calc_excess_rets 使用
        bench_items = []
        try:
            from scripts.recompute_ad import _load_bench_items
            _conn = conn or db.get_conn()
            if _conn is not None:
                bench_items = _load_bench_items(_conn) or []
        except Exception:
            bench_items = []

        # 构建池子
        pool = []
        for f in funds_meta:
            code = f.get("code")
            if not code:
                continue
            metrics = f.get("metrics") or {}
            navs = f.get("navs") or []
            dates = f.get("dates") or []

            # v2.11.4 A3/Q7: anti_metrics 要求 [{date, dwjz|ljjz}, ...] 格式,
            # 裸 float 列表会让 night_fund_monitor.py 分流走 else 分支恒返回 {}
            _navs_dict = [
                {"date": d, "dwjz": v}
                for d, v in zip(dates, navs)
                if d and v is not None
            ]
            # 计算抗跌指标
            try:
                _ma = nfm.anti_metrics(code, ddown_full, navs=_navs_dict) or {}
            except Exception:
                _ma = {}

            # v2.11.4 Q6: 计算超额收益(相对 bench_items), 供 earn_score 40% 权重使用
            _excess = None
            if bench_items and navs and dates:
                try:
                    from collector.ranker import calc_excess_rets
                    _excess = calc_excess_rets(navs, dates, bench_items)
                except Exception:
                    _excess = None

            pool.append({
                "code": code,
                "metrics": metrics,
                "ad_metrics": {
                    "dd_avg": _ma.get("fund_avg"),
                    "repair_5d": _ma.get("repair"),
                    "repair_10d": _ma.get("repair_10d"),
                },
                "excess_metrics": _excess,
            })

        # 批量计算评分
        from collector.scoring_v3 import compute_pool_scores
        results = compute_pool_scores(pool)

        # 缓存结果
        for code, scores in results.items():
            _UNIFIED_SCORE_CACHE[code] = {
                "ad_score": scores.get("ad_score"),
                "earn_score": scores.get("earn_score"),
                "dual": scores.get("dual_score"),
                "calmar_score": scores.get("calmar_score"),
            }

        _V3_SCORES_PRECOMPUTED = True
    except Exception as e:
        _V3_SCORES_PRECOMPUTED = True  # 标记为已计算，避免重复尝试


def _get_unified_score(f: dict) -> Optional[dict]:
    """使用统一算法计算抗跌分、收益分、综合分（与自选/弹窗一致）。
    模块级缓存，避免重复计算。
    """
    global _UNIFIED_DDOWN_CACHE
    code = f.get("code")
    if not code:
        return None
    if code in _UNIFIED_SCORE_CACHE:
        return _UNIFIED_SCORE_CACHE[code]
    try:
        import importlib
        nfm = importlib.import_module("analysis_pipeline.night_fund_monitor")
        if _UNIFIED_DDOWN_CACHE is None:
            _UNIFIED_DDOWN_CACHE = nfm.fetch_ddown()
        ddown_full = _UNIFIED_DDOWN_CACHE
        # 从DB加载带真实日期的净值数据（与统一分数服务一致）
        import db as _db
        rows = _db.get_nav(code, limit=120) or []
        _navs_list = [{"date": r.get("date"), "dwjz": r.get("dwjz")}
                      for r in rows if r.get("date") and r.get("dwjz")]
        _navs_list.sort(key=lambda x: x["date"])
        if len(_navs_list) < 30:
            _UNIFIED_SCORE_CACHE[code] = None
            return None
        _ma = nfm.anti_metrics(code, ddown_full, navs=_navs_list)
        vals = [n["dwjz"] for n in _navs_list]
        def _ret(n):
            if len(vals) < n + 1 or not vals[-n-1]:
                return None
            return (vals[-1] / vals[-n-1] - 1) * 100
        # v2.9.57: 统一使用scoring_v3线性评分（收益42.5%+抗跌42.5%+卡玛15%）
        _metrics = dict(f.get("metrics") or {})
        _metrics.update({"d3": _ret(3), "d5": _ret(5), "d7": _ret(7), "d10": _ret(10)})
        _ad_metrics = {
            "dd_avg": _ma.get("fund_avg"),
            "repair_5d": _ma.get("repair"),
            "repair_10d": _ma.get("repair_10d"),
            "mdd": _metrics.get("mdd"),
            "down_vol": _metrics.get("down_vol"),
            "max_daily_drop": _metrics.get("max_daily_drop"),
        }
        from collector.scoring_v3 import compute_ad_score, compute_earn_score, compute_dual_score
        ad = compute_ad_score(_ad_metrics)
        earn = compute_earn_score(_metrics, repair_5d=_ma.get("repair"))
        calmar = 50  # 单只计算无池内卡玛数据，用默认中位分
        dual = compute_dual_score(earn, ad, calmar)
        result = {"ad_score": ad, "earn_score": earn, "dual": dual, "calmar_score": calmar}
        _UNIFIED_SCORE_CACHE[code] = result
        f["_unified_score"] = result
        return result
    except Exception:
        _UNIFIED_SCORE_CACHE[code] = None
        return None


def _anti_score_simple(f: dict) -> float:
    """v2.10.0: 简化抗跌综合分(上榜池排名用, 无需2年历史净值).

    优先用统一算法(_get_unified_score)的 dual 分; 降级时用 funds 表已落库的
    ad_score/earn_score 或近5日跌幅/短期收益近似. 与原 _compute_reco_panels
    内联 _anti_score 逻辑完全一致, 提取为模块级供 compute_all_panels 调用.
    """
    unified = _get_unified_score(f)
    if unified and unified.get("dual") is not None:
        return unified["dual"]
    # v2.9.57: V3综合分权重 = 收益42.5% + 抗跌42.5% + 卡玛15%（卡玛缺失按50计）
    _ad_w, _earn_w, _calmar_w = 0.425, 0.425, 0.15
    _calmar_default = 50
    m = f.get("metrics") or {}
    ad = f.get("ad_score")
    earn = f.get("earn_score")
    if ad is None:
        navs = f.get("navs") or []
        if len(navs) >= 6:
            down_days = []
            for i in range(-5, 0):
                if navs[i] and navs[i - 1] and navs[i] < navs[i - 1]:
                    down_days.append(abs((navs[i] / navs[i - 1] - 1) * 100))
            ad = sum(down_days) / len(down_days) if down_days else 0
        else:
            ad = m.get("max_daily_drop_7d") or 0
    if earn is None:
        earn = m.get("d20") or m.get("m1") or 0
    return (ad or 0) * _ad_w + (earn or 0) * _earn_w + _calmar_default * _calmar_w


# ---------------------------------------------------------------------------
# 数据加载: 从 funds 表全量 + nav_history 算 metrics
# ---------------------------------------------------------------------------

def _load_idx_series(conn) -> dict:
    """读 idx 指数(供 metrics / reco / earn / ad 用). 与 recompute_ad._load_idx_series 等价."""
    try:
        from scripts.recompute_ad import _load_idx_series as _f
        return _f(conn)
    except Exception:
        return {}


# v2.11.4 C2/Q8: _prev_boards 已删除, board_days 全链路下线 (前端零引用)


def _load_funds_meta_full(conn) -> list[dict]:
    """加载 funds 表全部 6000+ 只,每只 dict 含 sec/ftype/themes/is_etf/nav_date/scale/score 等。

    metrics 字段若已有则复用;没有时按需从 nav_history 现算（内存级 cache）.

    v0.48.0: 多读 prev_reco/prev_reco_days/reco_score/manager/track。
    v2.11.4 C2/Q8: board_days 全链路下线, 不再从 rank_snapshots 推算。
    """
    rows = conn.execute("""
        SELECT code, name, sec, ftype, themes, stocks, is_etf, nav_date, scale, est,
               score, ad_score, earn_score, reco, reco_days,
               prev_reco, prev_reco_days, reco_score, manager, track,
               ms, m1, m3, m6, y1
        FROM funds
        ORDER BY code
    """).fetchall()
    items = [dict(r) for r in rows]
    return items


def _fetch_navs(conn, code: str, limit: int = 260) -> tuple[list, list]:
    """读某只基金的最近 N 天净值 -> (dates, navs)。默认 260 个交易日以覆盖 y1 (近 1 年) 计算。
    v0.51.3: 返回 ASC 顺序(从旧到新), 保证 navs[-1] 是最新净值, 与 d1=navs[-1]/navs[-2]-1 计算逻辑一致。
    之前 ORDER BY date DESC 直接返回, 导致 navs[0]最新但计算用 navs[-1](最旧), 所有 metrics 错误。
    """
    rows = conn.execute(
        "SELECT date, dwjz FROM nav_history WHERE code=? AND dwjz IS NOT NULL "
        "ORDER BY date DESC LIMIT ?", (code, limit)
    ).fetchall()
    if not rows:
        return [], []
    # 反转: DESC -> ASC (从旧到新), navs[-1] 为最新
    rows = list(reversed(rows))
    dates = [r["date"] for r in rows]
    navs = [r["dwjz"] for r in rows]  # v0.86.0: 用单位净值(dwjz),与东方财富/同花顺一致
    return dates, navs


_MEMO_METRICS: dict[str, dict] = {}


def clear_metrics_cache() -> int:
    """清空 _metrics_for 的进程级指标缓存,返回被清除的条数。

    必须在一轮「重排榜单」前调用。这个缓存是**模块级、永不失效**的:只要进程不重启,
    第一次算出来的 metrics 就会被一直复用。而一键更新发生在同一次进程生命周期内 ——
    它重新抓净值、重写 nav_history, 但缓存里的 d1/d2/d3 仍是上一轮(可能是脏数据)
    算出来的值, 于是出现:
        nav_history 已修对、funds 表已刷新, 重排出的榜单数字却还是旧的。
    典型表现:016450 的 d1 在库里已是 +2.16%, 当日榜 meta 里仍写 3.36% 并稳居第一。
    v2.11.4 A2: 补齐清理 _UNIFIED_SCORE_CACHE / _UNIFIED_DDOWN_CACHE / _V3_SCORES_PRECOMPUTED,
    防止跨天锁死首轮值。
    """
    global _UNIFIED_DDOWN_CACHE, _V3_SCORES_PRECOMPUTED
    n1 = len(_MEMO_METRICS)
    _MEMO_METRICS.clear()
    n2 = len(_UNIFIED_SCORE_CACHE)
    _UNIFIED_SCORE_CACHE.clear()
    _UNIFIED_DDOWN_CACHE = None
    _V3_SCORES_PRECOMPUTED = False
    return n1 + n2


def _metrics_for(f: dict, conn: sqlite3.Connection, idx_map: dict) -> dict:
    """fund dict 注入 metrics,若已算过或 fund 上带 metrics 则复用。

    v0.48.0: 补 m3/m6/y1/streak —— calc_metrics 只算 m1+ret2m+ret2w+ret5d,
             md_builder 需要 m3(63日)/m6(126日)/y1(252日) 渲染「近3月/近6月/近1年」标签,
             也需要 streak(连涨天数) 渲染「连涨 N 日」标签。
    """
    if f.get("metrics"):
        return f["metrics"]
    code = f["code"]
    if code in _MEMO_METRICS:
        m = _MEMO_METRICS[code]
        f["metrics"] = m
        return m
    # v0.51.0: 日榜 4 期 (当日/两日/三日/七日) 排序键跌点修复
    # 原代码 len(navs)<8 一律 m={} → d1/d2/d3 全 None → 4 期 panel 看起来"完全一样"
    # 修复: 哪怕仅有 2 个净值, 也按可用长度算 d1 (1日收益) / d2 (2日累计),
    #       保证 4 期 panel 各自有不同排序结果(切换有效)。
    nav_dates, navs = _fetch_navs(conn, code, limit=260)
    m = {}
    # v2.9.19: 数据质量校验（延迟导入避免循环依赖）
    from modules.common.data_quality import full_quality_check
    # v2.9.8: 数据完整度 - nav_history实际数据天数 / 260(1年)
    # 用于复杂指标榜排名时加权，避免"数据越完整排名越靠前"的自增强问题
    m["_data_days"] = len(navs)
    if len(navs) >= 208:  # 260 * 0.8 = 208，数据完整度>=80%
        m["_completeness"] = 1.0
    elif len(navs) >= 156:  # 60%
        m["_completeness"] = 0.9
    elif len(navs) >= 104:  # 40%
        m["_completeness"] = 0.7
    else:
        m["_completeness"] = 0.5
    if len(navs) >= 2 and navs[-1] and navs[-2]:
        _qa = full_quality_check(navs, nav_dates)
        m["d1"] = _qa.d1  # suspect状态时为None
        m["rzf"] = _qa.d1
        m["data_status"] = _qa.status
    else:
        m["data_status"] = "stale"
    if len(navs) >= 3 and navs[-1] and navs[-3]:
        m["d2"] = round((navs[-1] / navs[-3] - 1) * 100, 4)
    if len(navs) >= 4 and navs[-1] and navs[-4]:
        m["d3"] = round((navs[-1] / navs[-4] - 1) * 100, 4)  # 3个交易日 = 4个净值点
    if len(navs) >= 8:
        # 长周期字段(metrics 大块): 需 8+ 日才用 calc_metrics 高阶规则
        m.update(ranker.calc_metrics(navs, idx_map=idx_map))
        # d1 = 最新一日涨幅 (calc_metrics 不输出 d1)
        # v2.9.19: 调用数据质量校验，连续两天净值相同则d1=None
        if len(navs) >= 2 and navs[-1] and navs[-2]:
            _qa = full_quality_check(navs, nav_dates)
            m["d1"] = _qa.d1
            m["rzf"] = _qa.d1
            m["data_status"] = _qa.status
        else:
            m["d1"] = None
            m["rzf"] = None
            m["data_status"] = "stale"
        # v0.92.0: 近月段优先取 funds 表已落库的值(来自 pingzhongdata syl_*, 权威)。
        # 原实现一律用 nav_history 的净值序列推算, 但 nav_history 实测 5810 只仅 63~125 条
        # (126~251 条为 0 只), 而 _ret 分别需要 64/127/253 个点 → m3/m6/y1 恒为 None,
        # 这正是榜单 meta 里这三个字段覆盖率 0% 的根因。接口有现成值, 没必要自己算。
        m["m3"] = f.get("m3") if f.get("m3") is not None else ranker._ret(navs, 63)
        m["m6"] = f.get("m6") if f.get("m6") is not None else ranker._ret(navs, 126)
        m["y1"] = f.get("y1") if f.get("y1") is not None else ranker._ret(navs, 252)
        if f.get("m1") is not None and m.get("m1") is None:
            m["m1"] = f.get("m1")
        # v0.48.0: 连涨天数 (与 pipeline.py:357 同口径)
        try:
            m["streak"] = ranker.streak_days(navs)
        except Exception:
            m["streak"] = None
        # v0.48.0: max_daily_drop_7d (7日内最大单日跌幅) — 与 md_builder 用名保持一致
        m["max_daily_drop_7d"] = m.get("max_daily_drop_7d")
    _MEMO_METRICS[code] = m
    f["metrics"] = m
    f["navs"] = navs
    f["dates"] = nav_dates
    return m


# ---------------------------------------------------------------------------
# 各 sub panel 实现
# ---------------------------------------------------------------------------

def _compute_day_panels(funds_meta: list[dict], today: str) -> dict:
    """day × 5: 当日/三日/七日/近2周/综合
    数据源 = funds ∩ {nav_date == today}。

    每个 sub: top 40 (ETF 按板块去重). 各 sub 排序键分别为 d1/d3/d7/d10.
    """
    latest = max((f.get("nav_date") or "") for f in funds_meta) if funds_meta else ""
    if not latest:
        latest = today
    pool = [f for f in funds_meta if f.get("nav_date") == latest]
    if not pool:
        pool = funds_meta  # fallback

    period_keys = [("当日", "d1"), ("三日", "d3"), ("七日", "d7"), ("近2周", "d10")]

    def _dedup_etf_by_sec(items):
        """v0.51.3: 同板块指数型基金(ETF/联接/指数)只保留一个(按涨幅排序后的第一个).
        判断: is_etf=1 或名称含 'ETF'/'指数'/'联接'.
        去重 key: sec(板块). 同板块的指数型基金视为同类型, 只保留涨幅最高的一个.
        """
        seen_sec, out = set(), []
        for f in items:
            name = f.get("name") or ""
            is_idx_fund = bool(f.get("is_etf")) or "ETF" in name or "指数" in name or "联接" in name
            if is_idx_fund:
                sec = f.get("sec") or "其他"
                if sec in seen_sec:
                    continue
                seen_sec.add(sec)
            out.append(f)
            if len(out) >= 40:
                break
        return out

    panels = {}
    for sub_name, key in period_keys:
        srt = sorted(pool,
                     key=lambda f: (f.get("metrics") or {}).get(key) or -999,
                     reverse=True)
        items = _dedup_etf_by_sec(srt)
        panels[sub_name] = [
            _build_item(f, i + 1) for i, f in enumerate(items)
        ]

    # v2.2.0: 综合榜 — 四个日榜前40的并集，按上榜次数降序，同次数按综合分降序（方案A）
    rank_count = {}  # code -> 上榜次数
    rank_score = {}   # code -> 综合分（score或tscore）
    for sub_name, _ in period_keys:
        for it in panels.get(sub_name, []):
            code = it["code"]
            rank_count[code] = rank_count.get(code, 0) + 1
            if code not in rank_score:
                # 从 pool 中找对应的基金，获取综合分
                for f in pool:
                    if f["code"] == code:
                        # 综合分优先取tscore，其次取score
                        metrics = f.get("metrics") or {}
                        score = metrics.get("tscore") or metrics.get("score") or f.get("score") or -999
                        rank_score[code] = score
                        break
    # 按上榜次数降序，同次数按综合分降序
    sorted_codes = sorted(rank_count.keys(),
                          key=lambda c: (-rank_count[c], -(rank_score.get(c) or -999)))
    # 构建综合榜 items（取前40）
    comprehensive_items = []
    for i, code in enumerate(sorted_codes[:40]):
        # 从 pool 中找对应的基金
        for f in pool:
            if f["code"] == code:
                it = _build_item(f, i + 1)
                it["rank_count"] = rank_count[code]  # 上榜次数
                comprehensive_items.append(it)
                break
    panels["综合"] = comprehensive_items

    return panels
def _compute_reco_panels(funds_meta: list[dict],
                         day_panels: dict,
                         sec_m5_map: dict,
                         conn: sqlite3.Connection = None) -> dict:
    """v2.9.12: reco × 4 (重构版)
      - 抗跌 (上榜基金池内, 抗跌综合分=抗跌分50%+收益分50%, 取前40)
      - 自选 (全量 funds, 自选综合分多因子加权, 取前40)
      - 质量 (全市场C类基金, 质量分=下行夏普30%+卡玛25%+盈利稳定性15%-下跌捕获15%+上涨捕获15%, 取前40)
      - 超跌筑底 (全量基金, 急跌+低位+RPS弱势+筑底四阶段筛选, 按反弹预期综合评分降序, 取前40)

    上榜基金池 = 日榜四子榜各前40 ∪ 自选榜前40 ∪ 质量榜前40 ∪ 超跌筑底榜前40

    v2.1.6.1: 统一使用scoring_v3.py的评分算法，确保榜单排名和显示分数一致。
    v2.9.1: ETF榜重构为超跌筑底榜，基于急跌+低位+筑底三段式筛选。
    """
    # 批量计算所有基金的scoring_v3评分（统一算法）
    # v2.11.4 Q6: 传入 conn, 供内部加载 bench_items 并计算 excess_metrics
    _precompute_v3_scores(funds_meta, conn=conn)

    # ============================================================
    # 1. 自选榜 (全量 funds, 自选综合分多因子加权)
    # ============================================================
    _wsl = [(f, ranker.panel_score("自选算法榜单", f, sec_m5_map))
            for f in funds_meta]
    _wsl = [(f, w) for f, w in _wsl if w is not None]
    # v2.9.8: 数据完整性加权 - 复杂指标榜排名时乘以完整性系数
    _wsl = [(f, w * ((f.get("metrics") or {}).get("_completeness", 1.0))) for f, w in _wsl]
    _wsl.sort(key=lambda x: -(x[1] or 0))
    seen_wsl, wsl_dedup = set(), []
    for f, w in _wsl:
        if f.get("is_etf"):
            sec = f.get("sec") or "其他"
            if sec in seen_wsl:
                continue
            seen_wsl.add(sec)
        wsl_dedup.append((f, w))
        # v2.2.2: 自选榜取数从30改为40，与其他榜单一致
        if len(wsl_dedup) >= 40:
            break
    panel_zixuan = [_build_item(f, i + 1, tscore=w)
                    for i, (f, w) in enumerate(wsl_dedup)]

    # ============================================================
    # 2. 质量榜 (全市场C类基金, 多因子加权)
    # ============================================================
    # 筛选C类基金（名称以C结尾，非债券/货币/偏债/绝对收益）
    quality_pool = []
    for f in funds_meta:
        name = f.get("name") or ""
        # C类份额：名称以C结尾，非债券/货币/ETF联接
        if not name.endswith("C"):
            continue
        ftype = f.get("ftype") or ""
        # 剔除所有固收类：债券/货币/偏债/绝对收益/理财
        if any(kw in ftype for kw in ("债券", "货币", "偏债", "绝对收益", "理财")):
            continue
        quality_pool.append(f)


    # v2.1.2 修复: quality_pool 为空时直接跳过质量榜, 不进入因子归一化。
    # 根因: 全新空库采集时 funds.name 全为空, name.endswith("C") 筛选 0 命中。
    # P1 修复(采集阶段补齐档案)后此分支通常不会触发, 此处作为防御性兜底。
    if not quality_pool:
        panel_quality = []
    else:
        # v2.9.12: 批量读取净值，计算盈利稳定性（R²）
        from modules.rank.tech_indicators import calc_profit_stability
        quality_codes = [f["code"] for f in quality_pool]
        quality_nav_map = {}
        _batch_size = 200
        for _bi in range(0, len(quality_codes), _batch_size):
            _batch = quality_codes[_bi:_bi+_batch_size]
            _placeholders = ','.join(['?'] * len(_batch))
            _rows = conn.execute(
                f"SELECT code, date, dwjz FROM nav_history WHERE code IN ({_placeholders}) AND dwjz IS NOT NULL ORDER BY code, date",
                _batch
            ).fetchall()
            for _code, _date, _dwjz in _rows:
                if _code not in quality_nav_map:
                    quality_nav_map[_code] = []
                quality_nav_map[_code].append((_date, float(_dwjz)))

        # 计算质量分：下行夏普30% + 卡玛25% + 盈利稳定性15% - 下跌捕获15% + 上涨捕获15%
        # v2.9.12: 加入盈利稳定性因子，过滤"赌对一次大行情"的运气型基金
        # 各因子先做min-max归一化
        def _safe_get(f, key, default=0):
            v = f.get(key)
            if v is None:
                v = (f.get("metrics") or {}).get(key)
            return v if v is not None else default

        # 收集各因子值用于归一化
        down_sharpes = [_safe_get(f, "down_sharpe") for f in quality_pool]
        calmars = [_safe_get(f, "calmar") for f in quality_pool]
        mdds = [_safe_get(f, "mdd", -10) for f in quality_pool]
        y1s = [_safe_get(f, "y1") for f in quality_pool]
        # v2.9.12: 盈利稳定性（R²）
        stability_vals = [calc_profit_stability(quality_nav_map.get(f["code"], []), 60) or 0.5 for f in quality_pool]

        def _min_max_norm(values):
            """min-max归一化，处理全相同值和空列表的情况。"""
            if not values:
                return []
            vmin, vmax = min(values), max(values)
            if vmax == vmin:
                return [0.5] * len(values)
            return [(v - vmin) / (vmax - vmin) for v in values]

        ds_norm = _min_max_norm(down_sharpes)
        ca_norm = _min_max_norm(calmars)
        dd_norm = _min_max_norm([-d for d in mdds])
        y1_norm = _min_max_norm(y1s)
        # v2.9.12: 盈利稳定性归一化
        st_norm = _min_max_norm(stability_vals)

        # 计算质量分（v2.9.12调整权重，加入盈利稳定性）
        quality_scores = []
        for i, f in enumerate(quality_pool):
            qscore = (ds_norm[i] * 0.30 + ca_norm[i] * 0.25 + st_norm[i] * 0.15 +
                      dd_norm[i] * 0.15 + y1_norm[i] * 0.15)
            quality_scores.append((f, qscore))

        quality_scores.sort(key=lambda x: -x[1])
        # v2.2.2: 质量榜取数从30改为40，与其他榜单一致
        panel_quality = [_build_item(f, i + 1, tscore=round(s * 100, 2))
                         for i, (f, s) in enumerate(quality_scores[:40])]

    # ============================================================
    # 3. 超跌筑底榜 (v2.9.1 重构ETF榜: 急跌+低位+筑底三阶段筛选)
    # ============================================================
    panel_crash_bottom = _compute_crash_bottom_panel(funds_meta, conn)


    # v2.10.0: 抗跌榜已移至 compute_all_panels (在12榜上榜池内用简化ad_score排名),
    # 此处只返回 {自选, 质量, 超跌筑底} 3个榜; 上榜池收集由 compute_all_panels 统一处理.
    return {
        "自选": panel_zixuan,
        "质量": panel_quality,
        "超跌筑底": panel_crash_bottom,
    }

def _compute_crash_bottom_panel(funds_meta: list[dict], conn: sqlite3.Connection = None) -> list:
    """v2.9.12: 超跌筑底反弹候选榜（加入RPS相对强度+均线粘合）
    核心形态四段式：急跌 + 低位 + RPS弱势 + 筑底
    - 急跌：波动率倍数法 / 短期加速下跌法 / 历史分位法（三信号任一）
    - 低位：距60日高点回撤≤-20% 或 60日净值分位≤20%
    - RPS弱势：60日相对强度 < 阈值（默认30%），确保是真超跌而非正常回调
    - 筑底阶段：已反弹/反弹后回调/低位震荡/二次探底/创新低/下跌中
    - 均线粘合：MA5/MA10/MA20差距<2%，低位震荡时额外加分
    - 综合评分：急跌程度20 + RPS弱势10 + 低位程度25 + 筑底信号35 + 赛道景气10
    数据周期：因现有数据仅约100交易日，用60日代替方案中的120日/250日
    """
    import math
    from algo_config import get_algo_config
    _cfg = get_algo_config()
    _K = _cfg.cd_crash_k
    _accel = _cfg.cd_accel_ratio
    _low_dd = _cfg.cd_low_dd
    _low_pct = _cfg.cd_low_pct
    _vol_narrow = _cfg.cd_vol_narrow
    _topn = int(_cfg.cd_topn)
    _rps_th = _cfg.cd_rps_threshold       # RPS入榜阈值，默认30%
    _rps_rebound = _cfg.cd_rps_rebound   # RPS回升加分阈值，默认10百分位

    if conn is None:
        return []

    # 1. 批量读取所有基金的近60日净值
    all_codes = [f["code"] for f in funds_meta]
    nav_map = {}  # code -> [(date, nav), ...]
    batch_size = 300
    for bi in range(0, len(all_codes), batch_size):
        batch = all_codes[bi:bi+batch_size]
        placeholders = ','.join(['?'] * len(batch))
        rows = conn.execute(
            f"SELECT code, date, dwjz FROM nav_history WHERE code IN ({placeholders}) AND dwjz IS NOT NULL ORDER BY code, date",
            batch
        ).fetchall()
        for code, date, dwjz in rows:
            if code not in nav_map:
                nav_map[code] = []
            nav_map[code].append((date, float(dwjz)))

    # 2. 计算所有基金的20日/60日涨跌幅，用于RPS相对强度和历史分位法
    all_ret20 = []  # (code, ret20)
    all_ret60 = []  # (code, ret60)
    for code, navs in nav_map.items():
        if len(navs) >= 21:
            ret20 = (navs[-1][1] / navs[-21][1] - 1) * 100
            all_ret20.append((code, ret20))
        if len(navs) >= 61:
            ret60 = (navs[-1][1] / navs[-61][1] - 1) * 100
            all_ret60.append((code, ret60))
        elif len(navs) >= 21:
            # 数据不足60日，用全部数据计算
            ret60 = (navs[-1][1] / navs[0][1] - 1) * 100
            all_ret60.append((code, ret60))

    # 计算RPS百分位（0%=最弱，100%=最强）
    def _calc_rps(ret_list, target_code):
        """计算target_code在ret_list中的RPS百分位"""
        if not ret_list:
            return 50.0
        sorted_rets = sorted(ret_list, key=lambda x: x[1])  # 按涨幅从小到大排序
        total = len(sorted_rets)
        for i, (c, ret) in enumerate(sorted_rets):
            if c == target_code:
                # RPS = 比它弱的数量 / 总数 * 100
                return (i / total) * 100
        return 50.0

    # 5%分位数（历史极端阈值，用于急跌信号3）
    ret20_values = [r for _, r in all_ret20]
    ret20_values.sort()
    ret20_5pct = ret20_values[int(len(ret20_values) * 0.05)] if ret20_values else -20.0

    # 3. 逐基金计算指标和评分
    results = []
    for f in funds_meta:
        code = f["code"]
        navs = nav_map.get(code, [])
        if len(navs) < 30:  # 至少30天数据
            continue

        nav_values = [n[1] for n in navs]
        nav_dates = [n[0] for n in navs]
        n = len(nav_values)

        # 日收益率
        rets = []
        for i in range(1, n):
            if nav_values[i-1] > 0:
                rets.append((nav_values[i] / nav_values[i-1] - 1) * 100)

        if len(rets) < 20:
            continue

        # 基础指标
        current_nav = nav_values[-1]
        ret_5d = (current_nav / nav_values[-6] - 1) * 100 if n >= 6 else None
        ret_20d = (current_nav / nav_values[-21] - 1) * 100 if n >= 21 else None
        ret_60d = (current_nav / nav_values[-min(61, n)] - 1) * 100 if n >= 21 else None

        # 波动率（用60日作为基准，代替250日）
        vol_60d = (sum(r**2 for r in rets[-60:]) / len(rets[-60:])) ** 0.5 if len(rets) >= 20 else None
        expected_move_20d = vol_60d * math.sqrt(20) if vol_60d else None

        # 近60日最大回撤
        nav_60 = nav_values[-60:] if n >= 60 else nav_values
        peak = nav_60[0]
        mdd_60d = 0
        for v in nav_60:
            if v > peak:
                peak = v
            dd = (v / peak - 1) * 100
            if dd < mdd_60d:
                mdd_60d = dd

        # 距60日高点回撤（代替120日）
        high_60 = max(nav_60)
        dd_from_60_high = (current_nav / high_60 - 1) * 100

        # 60日净值分位（0%=最低）
        sorted_60 = sorted(nav_60)
        percentile_60 = (sorted_60.index(current_nav) + 1) / len(sorted_60) * 100 if current_nav in sorted_60 else 50

        # 近60日滚动5日涨跌幅绝对值的均值（加速下跌对比基准）
        abs_ret5_list = []
        for i in range(5, len(nav_values)):
            r5 = abs((nav_values[i] / nav_values[i-5] - 1) * 100)
            abs_ret5_list.append(r5)
        avg_abs_ret5_60d = sum(abs_ret5_list[-60:]) / len(abs_ret5_list[-60:]) if abs_ret5_list else 5.0

        # 10日/30日波动率（筑底波动率收窄判断）
        vol_10d = (sum(r**2 for r in rets[-10:]) / len(rets[-10:])) ** 0.5 if len(rets) >= 10 else None
        vol_30d = (sum(r**2 for r in rets[-30:]) / len(rets[-30:])) ** 0.5 if len(rets) >= 30 else None

        # 近60日最低净值和日期
        low_idx = nav_60.index(min(nav_60))
        recent_low_nav = nav_60[low_idx]
        recent_low_date = nav_dates[-len(nav_60)+low_idx] if len(nav_dates) >= len(nav_60) else nav_dates[low_idx]

        # MA5/MA20
        ma5 = sum(nav_values[-5:]) / 5
        ma20 = sum(nav_values[-20:]) / 20 if n >= 20 else current_nav

        # 近5日是否创60日新低
        recent_5_low = min(nav_values[-5:]) if n >= 5 else current_nav
        made_new_low_5d = recent_5_low <= recent_low_nav * 1.001

        # 近10日是否创60日新低
        recent_10_low = min(nav_values[-10:]) if n >= 10 else current_nav
        made_new_low_10d = recent_10_low <= recent_low_nav * 1.001

        # === 急跌判定（三信号） ===
        crash_signals = 0

        # 信号1：波动率倍数法
        if ret_20d is not None and expected_move_20d and expected_move_20d > 0:
            if abs(ret_20d) >= expected_move_20d * _K and ret_20d < 0:
                crash_signals += 1

        # 信号2：短期加速下跌法
        if (ret_5d is not None and ret_20d is not None
                and avg_abs_ret5_60d > 0
                and abs(ret_5d) >= avg_abs_ret5_60d * _accel
                and ret_5d < ret_20d / 4
                and ret_5d < 0):
            crash_signals += 1

        # 信号3：历史分位法
        if ret_20d is not None and ret_20d <= ret20_5pct:
            crash_signals += 1

        # 急跌强度分级（v2.9.11: 从30分调整为20分，拆出10分给RPS）
        if crash_signals >= 3:
            crash_level = "重度急跌"
            crash_score = 20
        elif crash_signals >= 2:
            crash_level = "中度急跌"
            crash_score = 15
        elif crash_signals >= 1:
            crash_level = "轻度急跌"
            crash_score = 10
        else:
            continue  # 无急跌信号，不入榜

        # === RPS相对强度判定（v2.9.11新增） ===
        rps_20d = _calc_rps(all_ret20, code)
        rps_60d = _calc_rps(all_ret60, code)
        if rps_60d >= _rps_th:
            continue  # RPS不够低，不是真超跌，不入榜

        # RPS弱势程度评分（10分）：RPS越低分越高
        if rps_60d <= 0:
            rps_score = 10.0
        elif rps_60d >= _rps_th:
            rps_score = 0.0
        else:
            rps_score = 10.0 - (rps_60d / _rps_th) * 10.0

        # === 低位判定 ===
        is_low = (dd_from_60_high <= _low_dd) or (percentile_60 <= _low_pct)
        if not is_low:
            continue  # 不在低位，不入榜

        # 低位程度评分（25分）
        # dd_from_60_high 越深分越高（-40%→25分，-20%→12分，线性插值）
        if dd_from_60_high <= -40:
            dd_score = 12.5
        elif dd_from_60_high >= _low_dd:
            dd_score = 6
        else:
            dd_score = 12.5 - (dd_from_60_high + 40) / (-_low_dd + 40) * (12.5 - 6)
        # percentile_60 越低分越高（0%→12.5分，20%→6分）
        if percentile_60 <= 0:
            pct_score = 12.5
        elif percentile_60 >= _low_pct:
            pct_score = 6
        else:
            pct_score = 12.5 - percentile_60 / _low_pct * (12.5 - 6)
        low_score = dd_score + pct_score

        # === 筑底阶段判定 ===
        rebound_from_low = (current_nav / recent_low_nav - 1) * 100 if recent_low_nav > 0 else 0

        # v2.9.12: 计算均线粘合（MA5/MA10/MA20相对差距<2%）
        from modules.rank.tech_indicators import is_ma_converged
        ma_converged = is_ma_converged(navs, threshold=0.02) if len(navs) >= 20 else False

        if ret_20d is not None and ret_20d > 3 and not made_new_low_5d:
            bottom_stage = "已反弹"
            bottom_score = 35
        elif (ret_20d is not None and -2 <= ret_20d <= 3
                and rebound_from_low > 5
                and current_nav > recent_low_nav * 1.03):
            bottom_stage = "反弹后回调"
            bottom_score = 30
        elif (ret_20d is not None and -3 <= ret_20d <= 3
                and not made_new_low_10d
                and vol_10d is not None and vol_30d is not None and vol_30d > 0
                and vol_10d < vol_30d * _vol_narrow):
            bottom_stage = "低位震荡"
            bottom_score = 28
            # v2.9.12: 均线粘合额外加分（筑底信号更强）
            if ma_converged:
                bottom_score = min(bottom_score + 3, 35)
        elif (ret_20d is not None and -8 <= ret_20d <= -3
                and abs(current_nav - recent_low_nav) / recent_low_nav * 100 < 3
                and current_nav >= recent_low_nav):
            bottom_stage = "二次探底"
            bottom_score = 20
        elif made_new_low_5d:
            bottom_stage = "创新低"
            bottom_score = 10
        elif ma5 < ma20:
            bottom_stage = "下跌中"
            bottom_score = 5
        else:
            bottom_stage = "低位震荡"
            bottom_score = 28

        # RPS回升加分（v2.9.11新增）：近期相对强度回升，说明有资金关注
        rps_rebound_bonus = 0
        if rps_20d > rps_60d + _rps_rebound:
            rps_rebound_bonus = 3
            bottom_score = min(bottom_score + 3, 35)  # 筑底分上限35

        # === 综合评分 ===
        sector_score = 5  # 赛道景气默认5分（后续可接行业资金流）
        total_score = crash_score + rps_score + low_score + bottom_score + sector_score

        results.append((f, total_score, crash_level, bottom_stage, ret_20d, ret_60d, dd_from_60_high, percentile_60, rps_20d, rps_60d, ma_converged))

    # 排序：综合评分降序，同分按abs(ret_60d)大的排前
    results.sort(key=lambda x: (-x[1], -(abs(x[5]) if x[5] is not None else 0)))
    # 构建输出
    panel = []
    for i, (f, score, crash_level, bottom_stage, ret_20d, ret_60d, dd_high, pct, rps20, rps60, ma_conv) in enumerate(results[:_topn]):
        it = _build_item(f, i + 1, tscore=round(score, 2))
        it["tscore"] = round(score, 2)  # 覆盖_build_item中的统一算法分数
        it["score"] = round(score, 2)   # score字段也显示超跌筑底综合分，与排序一致
        it["crash_level"] = crash_level
        it["bottom_stage"] = bottom_stage
        it["ret_20d"] = round(ret_20d, 2) if ret_20d is not None else None
        it["ret_60d"] = round(ret_60d, 2) if ret_60d is not None else None
        it["dd_from_high"] = round(dd_high, 2)
        it["percentile"] = round(pct, 1)
        it["rps_20d"] = round(rps20, 1)
        it["rps_60d"] = round(rps60, 1)
        it["ma_converged"] = ma_conv
        panel.append(it)

    logger.info(f"超跌筑底榜(v2.9.12含RPS+均线粘合): 候选{len(results)}只, 取前{len(panel)}只")
    return panel


def _compute_attack_panels(funds_meta: list[dict], conn: sqlite3.Connection = None) -> dict:
    """v2.9.12: attack × 4 (攻防榜 - 特征分类，加入技术形态)
      - 高弹性: β>阈值且上涨捕获率>阈值，且趋势强度>0（过滤下跌趋势）
      - 攻守兼备: Beta区间内且捕获率差>阈值且最大回撤<阈值，按捕获率差降序
      - 强抗跌: 下跌捕获率<阈值且下行波动率<市场均值且最大回撤<阈值，按下跌捕获率升序
      - 反弹先锋: 近20日跌幅>阈值且近5日涨幅>阈值；ETF额外要求突破20日高点+均线多头
    所有阈值从 algo_config 读取，可在「我的-算法」面板调整。
    """
    import statistics as _stats
    from algo_config import get_algo_config
    _cfg = get_algo_config()

    def _safe_metric(f, key):
        m = f.get("metrics") or {}
        v = m.get(key)
        if v is None:
            v = f.get(key)
        return float(v) if v is not None else None

    def _safe_val(f, key):
        v = f.get(key)
        if v is None:
            v = (f.get("metrics") or {}).get(key)
        return float(v) if v is not None else None

    # 1. 批量计算Beta（从nav_history读取日收益）
    # 先读上证指数日收益
    idx_rows = conn.execute(
        "SELECT date, ljjz FROM nav_history WHERE code='sh000001' AND ljjz IS NOT NULL ORDER BY date"
    ).fetchall()
    idx_rets = {}
    for i in range(1, len(idx_rows)):
        if idx_rows[i-1][1] and idx_rows[i][1]:
            ret = (idx_rows[i][1] / idx_rows[i-1][1] - 1) * 100
            idx_rets[idx_rows[i][0]] = ret

    # 批量读所有基金的日净值
    all_codes = [f["code"] for f in funds_meta]
    beta_map = {}
    batch_size = 200
    for bi in range(0, len(all_codes), batch_size):
        batch = all_codes[bi:bi+batch_size]
        placeholders = ','.join(['?'] * len(batch))
        rows = conn.execute(
            f"SELECT code, date, dwjz FROM nav_history WHERE code IN ({placeholders}) AND dwjz IS NOT NULL ORDER BY code, date",
            batch
        ).fetchall()
        # 按基金分组
        fund_navs = {}
        for code, date, dwjz in rows:
            if code not in fund_navs:
                fund_navs[code] = []
            fund_navs[code].append((date, dwjz))
        # 计算每只基金的beta和技术指标
        from modules.rank.tech_indicators import calc_trend_strength, is_breakout_high, is_ma_bullish
        tech_trend_map = {}   # code -> 趋势强度
        tech_breakout_map = {} # code -> 是否突破20日高点
        tech_ma_bullish_map = {} # code -> 是否均线多头
        for code, navs in fund_navs.items():
            if len(navs) < 10:
                continue
            f_rets = {}
            for j in range(1, len(navs)):
                if navs[j-1][1] and navs[j][1]:
                    ret = (navs[j][1] / navs[j-1][1] - 1) * 100
                    f_rets[navs[j][0]] = ret
            common = set(f_rets.keys()) & set(idx_rets.keys())
            if len(common) < 10:
                continue
            sorted_dates = sorted(common)
            f_aligned = [f_rets[d] for d in sorted_dates]
            i_aligned = [idx_rets[d] for d in sorted_dates]
            # 计算beta = cov(f, i) / var(i)
            f_mean = sum(f_aligned) / len(f_aligned)
            i_mean = sum(i_aligned) / len(i_aligned)
            cov = sum((f_aligned[k] - f_mean) * (i_aligned[k] - i_mean) for k in range(len(f_aligned))) / len(f_aligned)
            i_var = sum((x - i_mean) ** 2 for x in i_aligned) / len(i_aligned)
            if i_var > 0:
                beta_map[code] = cov / i_var
            # v2.9.12: 计算技术指标
            tech_trend_map[code] = calc_trend_strength(navs, 20)
            tech_breakout_map[code] = is_breakout_high(navs, 20)
            tech_ma_bullish_map[code] = is_ma_bullish(navs)

    # 1.5 从funds表查询dd20和d5（funds_meta中没有这两个字段）
    dd20_map = {}
    d5_map = {}
    _rows = conn.execute("SELECT code, dd20, d5 FROM funds WHERE dd20 IS NOT NULL OR d5 IS NOT NULL").fetchall()
    for _r in _rows:
        if _r["dd20"] is not None:
            dd20_map[_r["code"]] = float(_r["dd20"])
        if _r["d5"] is not None:
            d5_map[_r["code"]] = float(_r["d5"])

    # 2. 计算下行波动率均值（用于强抗跌榜筛选）
    dv_vals = []
    for f in funds_meta:
        dv = _safe_metric(f, "down_vol")
        if dv is not None and dv > 0:
            dv_vals.append(dv)
    dv_mean = sum(dv_vals) / len(dv_vals) if dv_vals else 20.0

    # 3. 四个特征榜筛选和排序
    high_elastic = []   # 高弹性
    balanced = []       # 攻守兼备
    strong_def = []     # 强抗跌
    rebound = []        # 反弹先锋

    for f in funds_meta:
        code = f["code"]
        beta = beta_map.get(code)
        up_capture = _safe_metric(f, "up_capture")
        dn_capture = _safe_metric(f, "dn_capture")
        cap_diff = (up_capture - dn_capture) if (up_capture is not None and dn_capture is not None) else None
        mdd = _safe_val(f, "mdd")
        down_vol = _safe_metric(f, "down_vol")
        d5 = _safe_val(f, "d5")
        dd20 = _safe_val(f, "dd20")

        # 高弹性: beta>阈值 且 up_capture>阈值 且 趋势强度>0（v2.9.12过滤下跌趋势）
        _trend = tech_trend_map.get(code)
        if (beta is not None and beta > _cfg.atk_elastic_beta
                and up_capture is not None and up_capture > _cfg.atk_elastic_upcap
                and (_trend is None or _trend > 0)):
            high_elastic.append((f, up_capture))

        # 攻守兼备: Beta区间内 且 cap_diff>阈值 且 mdd>阈值(跌幅小于阈值)
        if (beta is not None
                and _cfg.atk_balanced_beta_low < beta < _cfg.atk_balanced_beta_high
                and cap_diff is not None and cap_diff > _cfg.atk_balanced_capdiff
                and mdd is not None and mdd > _cfg.atk_balanced_maxdd):
            balanced.append((f, cap_diff))

        # 强抗跌: dn_capture<阈值 且 down_vol<dv_mean 且 mdd>阈值
        if (dn_capture is not None and dn_capture < _cfg.atk_def_dncap
                and down_vol is not None and down_vol < dv_mean
                and mdd is not None and mdd > _cfg.atk_def_maxdd):
            strong_def.append((f, dn_capture))

        # 反弹先锋: dd20<阈值(跌幅大于阈值) 且 d5>阈值
        # v2.9.12: ETF/指数基金额外要求突破20日高点 + 均线多头（确认真反弹）
        _dd20 = dd20_map.get(code)
        _d5 = d5_map.get(code)
        _is_etf = bool(f.get("is_etf")) or "ETF" in (f.get("name") or "") or "指数" in (f.get("name") or "")
        _breakout = tech_breakout_map.get(code, False)
        _ma_bullish = tech_ma_bullish_map.get(code, False)
        if (_dd20 is not None and _dd20 < _cfg.atk_rebound_dd20
                and _d5 is not None and _d5 > _cfg.atk_rebound_d5):
            # ETF/指数基金需要技术形态确认，主动基金保持原逻辑
            if _is_etf and not (_breakout and _ma_bullish):
                pass  # ETF但未满足技术形态，跳过
            else:
                rebound.append((f, _d5))

    # 排序
    high_elastic.sort(key=lambda x: -x[1])
    balanced.sort(key=lambda x: -x[1])
    strong_def.sort(key=lambda x: x[1])  # 下跌捕获率升序（越小越抗跌）
    rebound.sort(key=lambda x: -x[1])

    # 构建输出
    # v2.11.4 Q3: 攻防榜 tscore 统一为 V3 dual 分（_build_item 自动填),
    # 排序键保持原样(捕获率/涨幅/dn_capture)
    panel_high = [_build_item(f, i + 1, tscore=None)
                  for i, (f, s) in enumerate(high_elastic[:40])]
    panel_balanced = [_build_item(f, i + 1, tscore=None)
                      for i, (f, s) in enumerate(balanced[:40])]
    panel_def = [_build_item(f, i + 1, tscore=None)
                 for i, (f, s) in enumerate(strong_def[:40])]
    panel_rebound = [_build_item(f, i + 1, tscore=None)
                     for i, (f, s) in enumerate(rebound[:40])]

    logger.info(f"攻防榜特征分类(v2.9.12含技术形态): 高弹性{len(high_elastic)}只, 攻守兼备{len(balanced)}只, "
                f"强抗跌{len(strong_def)}只, 反弹先锋{len(rebound)}只")

    return {
        "高弹性": panel_high,
        "攻守兼备": panel_balanced,
        "强抗跌": panel_def,
        "反弹先锋": panel_rebound,
    }


def _compute_warn_panels(funds_meta: list[dict]) -> dict:
    """warn × 1: 动能衰减预警 (前期强 10日≥8% + 动能走弱/衰减).
    v0.54.0: 迁移自 ranker.build_ranks 的 warn 面板.
    """
    from collector.ranker import momentum_status
    _m_of = lambda f: f.get("metrics") or {}
    fail_f = [f for f in funds_meta
              if (_m_of(f).get("d10") or 0) >= 8 and momentum_status(_m_of(f)) == "走弱"]
    decay_f = [f for f in funds_meta
               if (_m_of(f).get("d10") or 0) >= 8 and momentum_status(_m_of(f)) == "衰减"]
    fail_f.sort(key=lambda f: ((_m_of(f).get("d5") or 0) / (_m_of(f).get("d10") or 1),
                               _m_of(f).get("d5") or 0))
    decay_f.sort(key=lambda f: ((_m_of(f).get("d5") or 0) / (_m_of(f).get("d10") or 1),
                                _m_of(f).get("d5") or 0))
    warn_f = _dedup_etf_by_sec(fail_f[:8] + decay_f[:7])
    panel_warn = [_build_item(f, i + 1) for i, f in enumerate(warn_f)]
    return {"动能衰减预警": panel_warn}


def _dedup_etf_by_sec(items: list[dict]) -> list[dict]:
    """非 ETF 榜单中,同板块(sec)的 ETF 仅保留一只(排序首只=涨幅/评分最高).
    v0.54.0: 从 ranker.build_ranks 内部嵌套函数迁移到 rank_full 模块级.
    """
    seen, out = set(), []
    for f in items:
        if f.get("is_etf"):
            _sec = f.get("sec") or "其他"
            if _sec in seen:
                continue
            seen.add(_sec)
        out.append(f)
    return out


def _etf_abbr_of(f: dict) -> str:
    """从基金名称抽取 ETF 简称; 非 ETF 或抽取失败返回空串。

    v2.1.0: funds 表无 etf_abbr 列, 从名称实时抽取(collector/etf_abbr.py)。
    仅对 is_etf=1 的基金尝试抽取, 主动基金直接返回空串。
    """
    if not f.get("is_etf"):
        return ""
    name = f.get("name") or ""
    if not name:
        return ""
    try:
        from collector.etf_abbr import etf_short_label
        return etf_short_label(name) or ""
    except Exception:
        return ""


def _build_item(f: dict, rank: int, tscore=None, tstatus=None) -> dict:
    """构造 rank_snapshots.meta 入表 dict."""
    m = f.get("metrics") or {}
    # v0.92.0: tstatus 兜底。原实现只取入参 tstatus, 而 _compute_rank_panels 是把它
    # 算在 f["_tstatus"] 上的(带下划线), 其余面板既不传参也没有 _tstatus
    # → meta 里 tstatus 覆盖率仅 15%。这里统一回退到 _tstatus, 缺失时现算。
    if not tstatus:
        tstatus = f.get("_tstatus")
    if not tstatus:
        try:
            tstatus = ranker.trend_status(m)
        except Exception:
            tstatus = None
    # v0.92.0: risk 兜底。原实现只有 reco 面板会给 f["risk"] 赋值(见 _compute_reco_panels),
    # day/rank 面板读 f.get("risk") 恒为 None → 覆盖率仅 5%。
    # reco_risk_tags_txt 依赖 scale/est(规模偏小/次新基金), 这两个字段补齐后内容也更准。
    _risk = f.get("risk")
    if not _risk:
        try:
            from collector.recommendation import reco_risk_tags_txt
            _risk = reco_risk_tags_txt(m, f.get("scale"), f.get("est")) or None
        except Exception:
            _risk = None
    # 统一算法的分数（与自选/弹窗一致）
    _unified = _get_unified_score(f)
    item = {
        "rank": rank,
        "code": f["code"],
        "name": f.get("name") or "",
        # 统一算法的综合分（与自选/弹窗一致），覆盖传入的tscore
        "tscore": (_unified or {}).get("dual") if _unified and _unified.get("dual") is not None else (tscore or f.get("score")),
        "tstatus": tstatus,
        # === 区间涨幅(组 ②) ===
        "d1": m.get("d1"),
        "d2": m.get("d2"),
        "d3": m.get("d3"),
        "d5": m.get("d5"),
        "d7": m.get("d7"),
        "d10": m.get("d10"),
        # === 近月段(组 ④ → 「近1月/近3月/近6月/近1年」)===
        "m1": m.get("m1"),
        "m3": m.get("m3"),
        "m6": m.get("m6"),
        "y1": m.get("y1"),
        "ret2m": m.get("ret2m"),
        # === 7日内形态(组 ② → 「7日跌N天 / 7日最大跌」)===
        "dn7": m.get("dn7"),
        "max_daily_drop_7d": m.get("max_daily_drop_7d"),
        # === 回撤与距高(组 ②/④ → 「回撤 / 历史回撤 / 距20日高」)===
        "max_daily_drop": m.get("max_daily_drop"),
        "mdd": m.get("mdd"),
        "dd_from_hi": m.get("dd_from_hi"),       # 标准字段：距历史高点回撤
        "dd20": m.get("dd20") if m.get("dd20") is not None else f.get("dd20"),  # v2.11.2: 真实"距最近20交易日高点回撤"(卡片"距20日高"), 独立于dd_from_hi
        "hi_cnt": m.get("hi_cnt"),
        # === 风险/波动(组 ② → 「风险/波动/回撤深」)===
        "vol": m.get("vol"),
        "down_vol": m.get("down_vol"),
        "down_sharpe": m.get("down_sharpe"),
        "calmar": m.get("calmar"),
        "pl": m.get("pl"),
        "up_ratio": m.get("up_ratio"),
        "up_capture": m.get("up_capture"),
        "dn_capture": m.get("dn_capture"),
        # === 动能与连涨(组 ② → 「动能X/连涨N日」)===
        "ms": f.get("ms"),
        "streak": m.get("streak"),
        # === 评分/建议/七日信号 ===
        # 统一算法的综合分（与自选/弹窗一致）
        "score": (_unified or {}).get("dual") if _unified and _unified.get("dual") is not None else f.get("score"),
        # 统一算法的抗跌分/收益分（与自选/弹窗一致）
        "ad_score": (_unified or {}).get("ad_score") if _unified and _unified.get("ad_score") is not None else f.get("ad_score"),
        "earn_score": (_unified or {}).get("earn_score") if _unified and _unified.get("earn_score") is not None else f.get("earn_score"),
        "reco": f.get("reco"),
        "reco_days": f.get("reco_days"),
        "prev_reco": f.get("prev_reco"),
        "prev_reco_days": f.get("prev_reco_days"),
        # === 持仓/规模/成立(组 ①/③ → 「规模X亿/成立YYYY-MM-DD/行业」)===
        "themes": f.get("themes") or [],
        # v1.2.0: ETF 简称进 meta —— 渲染层 _themes() 对 ETF 基金用简称替代
        #   持仓主题(ETF 无真实持仓主题数据)。v0.93.0 删掉的 is_etf 在此恢复,
        #   因为渲染层现在确实需要它判定「是否 ETF 基金」。
        #   v2.1.0: etf_abbr 无 DB 列, 从基金名称实时抽取(collector/etf_abbr.py)。
        "is_etf": f.get("is_etf"),
        "etf_abbr": _etf_abbr_of(f),
        "nav_date": f.get("nav_date"),
        "nav": (f.get("navs") or [None])[-1],
        "sec": f.get("sec"),
        "ftype": f.get("ftype"),
        # v0.93.0: 删掉 is_etf —— meta 里零引用。funds.is_etf 列保留,
        #   它被 _fill_listed_profiles 用来判断「是否值得为 track 打一次 f10」。
        "scale": f.get("scale"),         # 基金档案
        "est": f.get("est"),             # 基金档案
        "manager": f.get("manager"),
        "track": f.get("track"),
        # === 榜单信息(组 ⑥ → 「🏅同榜 / 风险」)===
        # v2.11.4 C2/Q8: board_days 全链路下线, 不再写入 meta
        "cros": f.get("cros"),
        "risk": _risk,
        # v0.93.0: 删掉 "rtag": None / "status": None ——
        #   这两个键自上线起恒为 None(库里 1884 行全部空值), 纯占位。
        #   唯一读取点 card_renderer.py:323 的 d["rtag"] 被 elif d.get("rtag") 保护,
        #   删键后行为不变(取到 None 而非缺失)。
    }
    return item


# ---------------------------------------------------------------------------
# v0.92.0: 上榜基金档案补齐
# ---------------------------------------------------------------------------

# 需要在写 meta 前补齐的字段 -> (funds 表列名, 所属数据源)
_PROFILE_FIELDS = ("m1", "m3", "m6", "y1", "manager", "scale", "est", "track")
_PROFILE_LIMIT = 600          # 单次更新最多补齐多少只(防御性上限)


def _fill_listed_profiles(panels: dict, funds_meta: list, conn,
                          progress_cb=None) -> dict:
    """对本次上榜的基金就地补齐缺失档案字段,并回写 funds 表 + panels item。

    为什么必须在这里做:
      _build_item 读的是 funds 表的 m3/m6/y1/scale/est/manager/track。
      这些字段原由 pipeline._fetch_top_returns 在「阶段2.5」写入(v2.9.44已删除该函数,
      m1/m3/m6/y1改由阶段1净值历史计算), 但本函数是「阶段2」, 执行时它们还是 NULL
      → meta 里恒为空(实测 383 行全部 0% 覆盖)。
      现在改为: 榜单算出后立刻补齐, 再写库, 数据即可回流。

    数据源(单次 HTTP 约 0.04~0.20s):
      pingzhongdata -> m1/m3/m6/y1(直接取 syl_*, 无需净值序列推算)/manager/scale
      f10 jbgk      -> est/track/ftype/company/manager/scale
     两源按缺失字段按需请求, 只在必要时才打第二个请求。
    """
    # --- 1. 收集本次上榜 code ---
    codes = set()
    for subs in panels.values():
        for items in subs.values():
            for it in items:
                c = it.get("code")
                if c:
                    codes.add(c)
    # 自选基金也一并补齐(自选页同样展示这些字段)
    try:
        for r in conn.execute("SELECT code FROM watchlist WHERE code != ''").fetchall():
            codes.add(r["code"] if isinstance(r, sqlite3.Row) else r[0])
    except Exception:
        logger.warning('榜单: 读取自选代码列表失败，本次榜单将不补拉自选基金档案')
    if not codes:
        return {"listed": 0, "todo": 0, "ok": 0}

    codes = list(codes)[:_PROFILE_LIMIT]

    # --- 2. 按「时变 / 静态」分别判定需要抓什么 ---
    #   时变(每个交易日都变, 必须刷新): m1/m3/m6/y1/scale —— 全部来自 pingzhongdata,
    #       只要 1 次请求(实测 20ms/只), 这是日常更新的常态路径。
    #   静态(几月才变一次, 缺了才补): manager/est/track —— est/track 需额外打 f10,
    #       入库存后就永久跳过, 避免每天为 400 只基金重复抓不变数据。
    _VOLATILE = ("m1", "m3", "m6", "y1", "scale")
    _fmap = {f["code"]: f for f in funds_meta}
    todo, need_map = [], {}
    for c in codes:
        f = _fmap.get(c) or {}
        missing = list(_VOLATILE)          # 时变字段每次都刷
        if not f.get("manager"):
            missing.append("manager")
        for k in ("est", "track"):
            if not f.get(k):
                missing.append(k)
        # track 仅指数/ETF 才有, 主动基金不必强求(省掉一次 f10 请求)
        if "track" in missing and not f.get("is_etf"):
            missing.remove("track")
        if missing:
            todo.append(c)
            need_map[c] = tuple(dict.fromkeys(missing))
    if not todo:
        if progress_cb:
            progress_cb("rank_full", 6, 6, "", f"档案齐全, 跳过补齐 ({len(codes)} 只)")
        return {"listed": len(codes), "todo": 0, "ok": 0}

    # --- 3. 并发抓取 ---
    from collector import fetcher as _fetcher
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import threading as _threading

    _lock = _threading.Lock()
    results: dict[str, dict] = {}

    def _work(code):
        try:
            return code, _fetcher.fetch_profile(code, need=need_map[code])
        except Exception:
            return code, {}

    _t0 = time.time()
    # 并发数取 4 而非越大越好: 东方财富对高频并发有限流, 实测并发4 = 20~51ms/只,
    # 并发10 反而退化到 220ms/只(响应变慢 + 触发 _get 重试 sleep)。
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = [ex.submit(_work, c) for c in todo]
        for fu in as_completed(futs):
            try:
                code, prof = fu.result()
            except Exception:
                continue
            if prof:
                with _lock:
                    results[code] = prof
    _dt = time.time() - _t0

    if not results:
        return {"listed": len(codes), "todo": len(todo), "ok": 0}

    # --- 4. 写回 funds 表 ---
    ok = 0
    for code, prof in results.items():
        cols, vals = [], []
        for k in ("m1", "m3", "m6", "y1", "manager", "scale", "est", "track"):
            if k in prof and prof[k] is not None and k in need_map.get(code, ()):
                cols.append(f"{k}=?")
                vals.append(prof[k])
        if not cols:
            continue
        vals.append(code)
        try:
            conn.execute(f"UPDATE funds SET {','.join(cols)} WHERE code=?", vals)
            ok += 1
        except Exception:
            continue
    try:
        conn.commit()
    except Exception as _e:
        logger.warning('榜单: 基金档案批量写库 commit 失败: %s', _e)

    # --- 5. 回写内存 funds_meta + panels item(否则本次 meta 仍是旧值) ---
    for code, prof in results.items():
        f = _fmap.get(code)
        if f is not None:
            for k, v in prof.items():
                if k != "navs":
                    f[k] = v
    for subs in panels.values():
        for items in subs.values():
            for it in items:
                code = it.get("code")
                prof = results.get(code)
                if prof:
                    for k, v in prof.items():
                        if k == "navs":
                            continue
                        if it.get(k) is None or it.get(k) == "":
                            it[k] = v
                # v2.2.0: 补齐 reco_days 字段（从 funds_meta 读取，确保信号标签显示"信号+天数"）
                f = _fmap.get(code)
                if f is not None:
                    reco_days = f.get("reco_days")
                    if reco_days is not None and (it.get("reco_days") is None or it.get("reco_days") == ""):
                        it["reco_days"] = reco_days
                    reco = f.get("reco")
                    if reco is not None and (it.get("reco") is None or it.get("reco") == ""):
                        it["reco"] = reco

    if progress_cb:
        progress_cb("rank_full", 6, 6, "",
                    f"档案补齐 {ok}/{len(todo)} 只 ({_dt:.1f}s)")
    return {"listed": len(codes), "todo": len(todo), "ok": ok}


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def compute_all_panels(progress_cb: Optional[Callable] = None) -> dict:
    """一次性算 13 个 sub,写入 rank_snapshots 表。返回 {panels: {...}, secs: ...}.

    各 sub 排名面板计算量 ~6000+ 只,实测耗时 5-15s,带 progress_cb 进度回调。
    """
    t0 = time.time()
    conn = db.get_conn()
    # 关键:先丢掉上一轮的指标缓存,否则本轮重排会用旧净值算出的 d1/d2/d3 排名。
    # (详见 clear_metrics_cache 的 docstring)
    _n_clear = clear_metrics_cache()
    funds_meta = _load_funds_meta_full(conn)

    if progress_cb:
        progress_cb("rank_full", 0, 6, "", "加载 funds 表完成")

    idx_series = _load_idx_series(conn)
    idx_map = ranker._build_idx_map(idx_series, [])

    # 算 metrics (memoization)
    _t = time.time()
    miss = 0
    for f in funds_meta:
        if not f.get("metrics"):
            _metrics_for(f, conn, idx_map)
            miss += 1

    # 算 sec_m5_map (板块 d5 聚合)
    sec_d5 = {}
    for f in funds_meta:
        if not is_index_like(f):
            continue
        sec = f.get("sec") or "其他"
        if sec == "其他":
            continue
        v = (f.get("metrics") or {}).get("d5")
        if v is not None:
            sec_d5.setdefault(sec, []).append(v)
    sec_m5_map = {s: round(sum(v) / len(v), 2) for s, v in sec_d5.items() if v}

    if progress_cb:
        progress_cb("rank_full", 1, 6, "", "metrics 与 sec_m5_map 完成")

    # v0.51.1 修复: today 用当前日期而非 max(nav_date),
    # 与 pipeline._today() 保持一致, 避免 rank_full 写入前一日(date=nav_date)
    # 导致无法覆盖 pipeline 当日榜单(date=today), 页面显示旧的 pipeline build_ranks 结果.
    today = time.strftime("%Y-%m-%d")

    # 1. day × 5 (当日/两日/三日/七日/综合)
    # v2.1.2 修复: 每个 panel 组独立 try-except, 一个失败不影响其他 panel。
    try:
        panels_day = _compute_day_panels(funds_meta, today)
    except Exception as _e:
        panels_day = {}
    if progress_cb:
        progress_cb("rank_full", 2, 6, "", f"日榜 day × 4 完成（{today}）")

    # 2. v2.10.0: reco × 3 (自选/质量/超跌筑底) — 抗跌榜移至步骤4(上榜池内排名)
    try:
        panels_reco = _compute_reco_panels(funds_meta, panels_day, sec_m5_map, conn)
    except Exception as _e:
        import traceback
        traceback.print_exc()
        panels_reco = {}
    if progress_cb:
        progress_cb("rank_full", 3, 6, "", "推荐榜单 {自选, 质量, 超跌筑底} 完成")

    # 3. v2.9.1: attack × 4 (高弹性/攻守兼备/强抗跌/反弹先锋)
    try:
        panels_attack = _compute_attack_panels(funds_meta, conn)
    except Exception as _e:
        import traceback
        traceback.print_exc()
        panels_attack = {}
    if progress_cb:
        progress_cb("rank_full", 4, 6, "", "攻防榜 {高弹性, 攻守兼备, 强抗跌, 反弹先锋} 完成")

    # v2.1.3: warn 动能衰减预警已移除（前端未展示，白算浪费性能）
    # 历史 warn 数据仍在 _save_to_db 中清理

    # v1.1.0: day/reco 从 rank_config 读取
    panels = {
        "day": panels_day,
        "reco": panels_reco,
        "attack": panels_attack,
    }

    # v2.9.57: 预计算V3评分缓存（抗跌榜及统一分数服务共用，确保池内百分位一致）
    # v2.11.4 Q6: 传入 conn, 供内部加载 bench_items 并计算 excess_metrics
    _precompute_v3_scores(funds_meta, conn=conn)

    # 4. v2.10.0: 抗跌榜 — 先收集12个榜(day×5 + reco×3 + attack×4)的上榜基金池,
    #    仅对上榜基金(~300只)算简化ad_score, 在上榜池内排名取前40.
    #    全池compute_scores_v2已从pipeline移除, 此处用_anti_score_simple近似排名.
    try:
        listed_codes_12 = set()
        for _panel_data in panels.values():
            for _items in _panel_data.values():
                for _it in _items:
                    _c = _it.get("code")
                    if _c:
                        listed_codes_12.add(_c)
        anti_pool = [f for f in funds_meta
                     if f["code"] in listed_codes_12 and not is_equity_excluded(f)]
        anti_pool.sort(key=lambda f: -_anti_score_simple(f))
        panel_antidi = [_build_item(f, i + 1, tscore=round(_anti_score_simple(f), 2))
                        for i, f in enumerate(anti_pool[:40])]
        panels["reco"]["抗跌"] = panel_antidi
        if progress_cb:
            progress_cb("rank_full", 5, 6, "",
                        f"抗跌榜完成 (上榜池{len(listed_codes_12)}只 → 抗跌{len(panel_antidi)}只)")
    except Exception as _e:
        import traceback
        traceback.print_exc()
        logger.warning("rank_full: 抗跌榜计算失败, 跳过: %s", _e)

    # v0.51.3: 同榜提醒 — 用日榜五期(当日/两日/三日/七日/综合)交叉排名,生成 cros 字段
    # 形式: "🏅同榜 当日#3·两日#5·三日#7·七日#2" (排除自身所在子榜)
    rank_map = {}
    for sub, items in panels.get("day", {}).items():
        for it in items:
            rank_map.setdefault(it["code"], {})[sub] = it["rank"]
    for panel, subs in panels.items():
        for sub, items in subs.items():
            for it in items:
                mine = rank_map.get(it["code"], {})
                other = [f"{k}#{v}" for k, v in mine.items() if k != sub]
                if other:
                    it["cros"] = "🏅同榜 " + "·".join(other)
                else:
                    it["cros"] = None

    # v0.92.0: 上榜基金档案补齐 —— 必须在 _save_to_db 之前执行
    # 原因: _build_item 直接读 funds 表的 m3/m6/y1/scale/est/manager/track,
    #       而这些字段此前由 pipeline._fetch_top_returns 在「阶段2.5」才写入,
    #       时点晚于本函数(阶段2), 导致榜单 meta 里这些字段恒为空(实测覆盖率 0%)。
    # 这里在榜单算出后、写库前, 对上榜基金就地补齐, 数据才能回流进 meta。
    _fill_listed_profiles(panels, funds_meta, conn, progress_cb)

    # 写入 rank_snapshots (UPSERT 当日 13 个 sub)
    _save_to_db(panels, today, conn)

    if progress_cb:
        progress_cb("rank_full", 6, 6, "",
                    f"全部 13 sub 写库 ({time.time()-t0:.1f}s)")

    # v0.96.0: 从 panels 计算上榜基金数量（listed_codes 已在 _compute_reco_panels 内部计算）
    listed_codes = set()
    for panel, subs in panels.items():
        for sub, items in subs.items():
            for it in items:
                listed_codes.add(it.get("code"))
    return {"panels": panels, "today": today,
            "funds_size": len(funds_meta),
            "listed_size": len(listed_codes)}


def _save_to_db(panels: dict, today: str, conn: sqlite3.Connection):
    """UPSERT 写入 rank_snapshots 当日所有 sub (覆盖式)."""
    # v0.54.0: rank_full 接管所有面板 (day/rank/reco/warn), 删除 pipeline 生成的旧数据
    # v0.96.1: rot (主题轮动) 已彻底删除
    # v2.9.1: 新增 attack (攻防榜) 面板，必须一并删除否则 UNIQUE constraint 失败
    conn.execute(
        "DELETE FROM rank_snapshots WHERE date=? AND ("
        "panel='day' OR panel='rank' OR panel='reco' OR panel='warn' OR panel='attack'"
        ")", (today,)
    )
    conn.commit()
    n = 0
    for panel, subs in panels.items():
        for sub, items in subs.items():
            # v2.9.4 部署实抓防御：it['rank'] 缺失(归 0)或批内重复会直接撞
            # UNIQUE(date,panel,sub,rank)，一个坏点崩掉整轮重排。按批次位置补号。
            used: set[int] = set()
            auto = 0
            for it in items:
                rank = it.get("rank") or 0
                if rank <= 0 or rank in used:
                    auto += 1
                    while auto in used:
                        auto += 1
                    rank = auto
                    it["rank"] = rank
                used.add(rank)
                code = it.get("code") or ""
                name = it.get("name") or ""
                conn.execute(
                    "INSERT INTO rank_snapshots (date, panel, sub, rank, code, name, meta, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (today, panel, sub, rank, code, name,
                     json.dumps(it, ensure_ascii=False, default=str),
                     time.time())
                )
                n += 1
    conn.commit()
