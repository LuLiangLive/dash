"""实时数据访问薄封装（v0.48.0 增量实现）.

策略:**实时接口优先 + DB 缓存兜底**.所有方法都对异常做兜底,
返回 None / {} / 0 等合理默认值,不抛异常冒泡到调用方。

调用入口:
- main.py:_load_nfm() → importlib.import_module('analysis_pipeline.night_fund_monitor')

被调用方(若缺失函数会让多处端点 500):
- _series() / _long_series() → nfm.fetch_nav
- _calc_merge() → nfm.calc_metrics
- anti_detail() → nfm.fetch_ddown / anti_metrics / anti_score / earn_score / dual_score
- /api/watch/fetch → nfm.fetch_watch_detail

数据来源链路:
- 净值       → fetcher.fetch_nav_history(天天基金 lsjz)
- 持仓       → fetcher.fetch_holdings_w(fundf10 eastmoney)
- 基本信息   → fetcher.fetch_basic(fundf10 eastmoney jbgk)
- 主题聚合   → fetcher.holdings_themes(本地映射)
- 指数       → fetcher.fetch_index_kline(腾讯财经)
- 下跌日     → 从指数 series 派生(上证 ≤-1% / 创业板 ≤-4% / 科创50 ≤-4%)
- 评分/推荐  → collector.ranker.compute_scores_v2 / compute_reco_v32
- DB 缓存    → db.get_fund / db.get_nav(从 nav_history 表)
"""
from __future__ import annotations

import statistics
import sys
from typing import Optional

# v2.9.19: 数据质量校验模块
from modules.common.data_quality import full_quality_check, QualityStats

from pathlib import Path as _P

# 让 import collector.* 能找到
_ROOT = _P(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _safe(fn, default=None):
    """统一异常兜底:函数任一环节异常返回 default."""
    try:
        return fn()
    except Exception:
        return default


# ---------------------------------------------------------------------------
# 1. 净值实时抓取(天天基金 lsjz)
# ---------------------------------------------------------------------------

def fetch_nav(code: str, page: int = 1, size: int = 25) -> list[dict]:
    """天天基金实时拉取某只基金的某页净值。

    Args:
        code: 6 位基金代码
        page: 页索引(1 开始)
        size: 每页条数

    Returns:
        list of {date, ljjz, dwjz} —— 升序;失败返回 []
    """
    def _do():
        from collector import fetcher
        # fetch_nav_history 返回的是降序 (DESC),按 page/page_size 分页
        items = fetcher.fetch_nav_history(code, page_index=page, page_size=size, max_pages=1)
        # 升序返回
        return sorted(items, key=lambda x: x.get("date") or "")
    return _safe(_do, default=[])


def fetch_nav_many(codes: list[str], page_size: int = 60, workers: int = 16) -> dict:
    """并发抓多只基金的全部净值历史。"""
    def _do():
        from collector import fetcher
        return fetcher.fetch_nav_many(codes, page_size=page_size, workers=workers)
    return _safe(_do, default={})


# ---------------------------------------------------------------------------
# 2. 指数 / 大盘下跌日 / idx_map
# ---------------------------------------------------------------------------

def fetch_ddown() -> tuple[list, list, dict]:
    """拉取全部指数序列 + 派生大盘下跌日。

    Returns:
        (ddown_dates, ddown_pcts, idx_map) 三个并行列表+字典

        - idx_map[date] = {"sh": ..., "cyb": ..., "kc": ..., "close": ...}
          其中 sh/cyb/kc 是小数表示的涨跌幅(0.01 表示 +1%, -0.04 表示 -4%)
        - ddown_dates / ddown_pcts 是其中符合"上证 ≤ -1% 或 创业板 ≤ -4%
          或 科创50 ≤ -4%"的日期序列
    """
    def _do():
        from collector import fetcher
        idx_dict = fetcher.fetch_all_indices(days=90)
        if not idx_dict:
            return [], [], {}
        idx_map = {}
        # 把 K 线 items 展平为 date→日涨跌
        for code, info in idx_dict.items():
            items = info.get("items", [])
            for i, it in enumerate(items):
                d = it.get("date")
                if not d:
                    continue
                if d not in idx_map:
                    idx_map[d] = {"sh": 0.0, "cyb": 0.0, "kc": 0.0, "close": None}
                # 以"当日 close / 上一日 close - 1"算收益
                if i > 0 and items[i - 1].get("close"):
                    prev_c = items[i - 1]["close"]
                    cur_c = it.get("close") or 0
                    if prev_c > 0:
                        ret = (cur_c / prev_c - 1)
                        # 命名约定:用代码前缀标识
                        if code == "sh000001":
                            idx_map[d]["sh"] = ret
                        elif code == "sz399006":
                            idx_map[d]["cyb"] = ret
                        elif code == "sh000688":
                            idx_map[d]["kc"] = ret
                # close 也存(供最近)
                idx_map[d]["close"] = it.get("close")
        # 派生下跌日
        ddown = sorted([
            (d, m.get("sh", 0.0))
            for d, m in idx_map.items()
            if m.get("sh", 0.0) <= -0.01
            or m.get("cyb", 0.0) <= -0.04
            or m.get("kc", 0.0) <= -0.04
        ])
        ddown_dates = [d for d, _ in ddown]
        ddown_pcts = [p for _, p in ddown]
        return ddown_dates, ddown_pcts, idx_map
    return _safe(_do, default=([], [], {}))


# ---------------------------------------------------------------------------
# 3. 指标计算(封装 ranker.calc_metrics)
# ---------------------------------------------------------------------------

def calc_metrics(navs, idx_map: Optional[dict] = None, ddown=None) -> dict:
    """对一组净值序列跑完整常规指标(区间涨幅/上下天数/年化波动/最大回撤等).

    Args:
        navs: float list 升序;也接受 [{date, ljjz}, ...] dict list
        idx_map: 可选指数映射,用于捕捉超额

    Returns:
        dict(可能含 None)。失败返回 {}。
    """
    def _do():
        from collector import ranker
        # 适配 dict list 输入
        if navs and isinstance(navs[0], dict):
            # v1.1.5: 单位净值(dwjz)优先, 与榜单口径一致; 老数据无 dwjz 时回退 ljjz
            vs = [float(x.get("dwjz") or x.get("ljjz") or 0) for x in navs]
        else:
            vs = list(navs)
        if len(vs) < 2:
            return {}
        return ranker.calc_metrics(vs, idx_map=idx_map or {}, ddown=ddown or [])
    return _safe(_do, default={})


# ---------------------------------------------------------------------------
# 4. 抗跌专项 metrics(scores 0-100)
# ---------------------------------------------------------------------------

def anti_metrics(code: str, ddown, navs) -> dict:
    """抗跌指标(单只基金相对大盘下跌日的表现)。

    Args:
        code: 6 位基金代码
        ddown: (ddown_dates, ddown_pcts, idx_map) 三元组(来自 fetch_ddown)。
        navs: 升序 [{date, ljjz}, ...] 净值序列

    Returns:
        dict: {"down_days": int, "fund_avg": float|None,
               "idx_avg": float|None, "repair": float|None,
               "detail": list[{date, idx, fund}...]}
        失败返回 {}。
    """
    def _do():
        ddown_dates, ddown_pcts, idx_map = (ddown or ([], [], {}))
        if not isinstance(navs, list):
            return {}
        if navs and isinstance(navs[0], dict):
            fdates = [x.get("date") for x in navs]
            # v1.1.5: 单位净值(dwjz)优先, 与榜单口径一致; 老数据无 dwjz 时回退 ljjz
            fvals = [float(x.get("dwjz") or x.get("ljjz") or 0) for x in navs]
        else:
            fdates = []; fvals = list(navs)
        # 区间内下跌日
        if not fdates or not idx_map:
            return {}
        lo, hi = fdates[0], fdates[-1]
        in_range = [(d, m.get("sh", 0.0))
                    for d, m in sorted(idx_map.items())
                    if (m.get("sh", 0.0) <= -0.01 or m.get("cyb", 0.0) <= -0.04 or m.get("kc", 0.0) <= -0.04)
                    and lo <= d <= hi]
        # 派生基金当日涨跌幅(fund_ret)
        fret = {}
        for i in range(1, len(fdates)):
            if fvals[i - 1]:
                fret[fdates[i]] = (fvals[i] / fvals[i - 1] - 1) * 100
        detail = []
        f_avgs = []
        for d, ir in in_range:
            cand = [x for x in fdates[1:] if x <= d]
            dd = max(cand) if cand else None
            fv = fret.get(dd)
            detail.append({"date": d, "idx": round(ir * 100, 2), "fund": round(fv, 2) if fv is not None else None})
            if fv is not None:
                f_avgs.append(fv)
        fund_avg = sum(f_avgs) / len(f_avgs) if f_avgs else None
        idx_avg = sum(r for _, r in in_range) / len(in_range) * 100 if in_range else None
        # 修复:大跌后 5 日
        repairs = []
        for d, _ in in_range:
            if d in fdates:
                i0 = fdates.index(d)
                i1 = min(i0 + 5, len(fvals) - 1)
                if i1 > i0:
                    repairs.append((fvals[i1] / fvals[i0] - 1) * 100)
        repair = sum(repairs) / len(repairs) if repairs else None

        # v2.1.5: 大跌后 10 日反弹（方案C+抗跌分指标）
        repairs_10d = []
        for d, _ in in_range:
            if d in fdates:
                i0 = fdates.index(d)
                i1 = min(i0 + 10, len(fvals) - 1)
                if i1 > i0:
                    repairs_10d.append((fvals[i1] / fvals[i0] - 1) * 100)
        repair_10d = sum(repairs_10d) / len(repairs_10d) if repairs_10d else None

        return {"down_days": len(in_range), "detail": detail,
                "fund_avg": round(fund_avg, 2) if fund_avg is not None else None,
                "idx_avg": round(idx_avg, 2) if idx_avg is not None else None,
                "repair": round(repair, 2) if repair is not None else None,
                "repair_10d": round(repair_10d, 2) if repair_10d is not None else None}
    return _safe(_do, default={})


# ---------------------------------------------------------------------------
# 5. 自选实时获取(name + 概况 + 净值 + 持仓主题)
# ---------------------------------------------------------------------------

# v2026-08-28: 自选详情短期缓存(10分钟),避免批量获取和页面刷新时重复请求
_WATCH_DETAIL_CACHE = {}
_WATCH_DETAIL_CACHE_TTL = 600  # 10分钟

# v2026-08-29: 持仓缓存 —— 基金持仓按季度披露, 变化极慢, 但实测单次抓取可达 1.57s
#   (DB 持仓为空的基金每次添加自选都要付这笔网络开销)。缓存 6 小时并落库 funds 表。
_HOLDINGS_CACHE = {}  # code -> (timestamp, [[name, pct], ...])
_HOLDINGS_CACHE_TTL = 21600  # 6 小时


def _holdings_cached(code: str) -> list:
    """带缓存 + 落库的持仓获取(DB 已有持仓时由调用方跳过, 这里只管网络部分)。"""
    import time as _t
    _item = _HOLDINGS_CACHE.get(code)
    if _item and (_t.time() - _item[0]) < _HOLDINGS_CACHE_TTL:
        return _item[1]
    stocks: list = []
    try:
        from collector import fetcher as _ft
        raw = _ft.fetch_holdings_w(code)  # [(name, pct), ...] 元组列表
        # v2.9.0 修复: 旧实现用 s.get("name") 按 dict 访问, 对元组抛 AttributeError 被吞,
        #   导致实时获取持仓/主题永远为空 → 改用位置索引
        stocks = [[s[0], s[1]] for s in (raw or [])[:10] if s and s[0]]
    except Exception:
        stocks = []
    _HOLDINGS_CACHE[code] = (_t.time(), stocks)
    if stocks:
        try:
            import db as _dbw
            _f = _dbw.get_fund(code) or {}
            _patch = dict(_f)
            _patch["code"] = code
            _patch["stocks"] = stocks
            _dbw.upsert_fund(_patch)
        except Exception:
            pass
    return stocks

def fetch_watch_detail(code: str) -> dict:
    """自选实时获取全套数据(用于 ↗ 自选添加时即时显示)。

    Returns:
        dict: {ok, code, name, scale, est, ftype, sec, is_etf, manager, track,
               themes, stocks, nav, nav_date, m1, m3, m6, y1,
               d3, d5, d7, d10, dd7, dn7, ms, reco, score, ad_score, earn_score, ...}
        任意字段缺失允许 None;失败时仍返回 {ok: False, msg: ...}

    v0.50.x: 修复 #1 「自选卡 标签缺失」——
    - 旧版 d 字典缺失 score / ad_score / earn_score / mdd_status / dd20 / streak / mdd
    - 这些字段从 fund 表读不到时,自选新加的卡片永远少「综合分」「抗跌·收益」「回撤持续」标签
    - 修复策略: ① 优先从 funds 表读(由 rank_full 写入的池内分); ② 兜底走 ranker 单基金计算

    v2026-08-28: 添加10分钟短期缓存,避免批量获取和页面刷新时重复网络请求
    """
    # 检查缓存
    import time as _time
    _cached = _WATCH_DETAIL_CACHE.get(code)
    if _cached and (_time.time() - _cached['time']) < _WATCH_DETAIL_CACHE_TTL:
        return _cached['data'].copy()

    def _do():
        from collector import fetcher, ranker
        # 0) 优先从 funds 表读(已由 rank_full / fetch_manager 写入的池内分与计算结果)
        #    这是与「基金页抗跌榜 / 卡片」同口径的数据, 用户看到一致
        from db import get_fund
        _db_row = get_fund(code) or {}
        # v2026-08-29 性能优化:
        #   ① 净值优先走 DB(定时任务已落库, 0ms); 仅当 DB 缺失或数据不新鲜时才实时抓取
        #      —— 实测单次实时净值抓取 1.16s, 占自选添加总耗时 80%
        #   ② 其余互不依赖的网络请求由「串行」改为「并发」, 总耗时取各请求最大值
        _nav_from_db = None
        try:
            import db as _dbn
            _rows = _dbn.get_nav(code, limit=120) or []
            if len(_rows) >= 40:
                _latest_all = _dbn.latest_nav_date() or ""
                # 允许滞后 3 个自然日: ETF / 联接基金 / 新基金的净值天然晚于主流基金 0-2 天,
                # 判据过严(必须 == 全库最新日)会让这类基金永远走实时抓取(1.16s)
                _thresh = _latest_all
                if _latest_all:
                    try:
                        from datetime import date as _date, timedelta as _td
                        _thresh = (_date.fromisoformat(_latest_all) - _td(days=3)).isoformat()
                    except Exception:
                        pass
                if (_rows[0].get("date") or "") >= _thresh:
                    _nav_from_db = _rows
        except Exception:
            _nav_from_db = None
        from concurrent.futures import ThreadPoolExecutor as _TPE
        with _TPE(max_workers=4) as _ex:
            _f_info = _ex.submit(fetcher.fetch_basic, code)               # 档案 scale/est
            _f_name = _ex.submit(_fetch_name, code)                       # 名称
            _f_nav = (None if _nav_from_db is not None
                      else _ex.submit(fetcher.fetch_nav_history, code, 1, 120))  # 净值历史
            _f_ret = _ex.submit(fetcher.fetch_fund_returns, code)         # 阶段涨幅
            try:
                info = _f_info.result()
            except Exception:
                info = None
            try:
                _nm = _f_name.result()
            except Exception:
                _nm = None
            items = _nav_from_db
            if items is None and _f_nav is not None:
                try:
                    items = _f_nav.result()
                except Exception:
                    items = None
            try:
                _pre_returns = _f_ret.result()
            except Exception:
                _pre_returns = None
        # 1) 名称 + 概况(优先实时 → DB → code 兜底)
        scale, est = (None, None)
        if info:
            scale, est = info[0], info[1]
        # DB 已有 scale/est 用 DB 的
        if not scale and _db_row.get("scale") is not None:
            scale = _db_row.get("scale")
        if not est and _db_row.get("est"):
            est = _db_row.get("est")
        # v2026-08-29: 档案字段落库 funds 表。此前 scale/est/is_etf 长期为 NULL
        #   (导致 ETF 卡片缺「规模/成立」、is_etf 全 0), 落库后后续请求直接命中 DB, 无需网络。
        if (scale or est) and (not _db_row.get("scale") or not _db_row.get("est")):
            try:
                import db as _dbw
                _patch = dict(_db_row) if _db_row else {}
                if scale and not _patch.get("scale"):
                    _patch["scale"] = scale
                if est and not _patch.get("est"):
                    _patch["est"] = est
                _patch["code"] = code
                _dbw.upsert_fund(_patch)
            except Exception:
                pass
        # 名称用 fundf10 jbgk 页面提取, 失败回 DB
        name = _nm or _db_row.get("name") or code
        # 2) 净值(实时,补不齐时 DB 拼)
        if not items:
            return {"ok": False, "msg": "实时净值抓取失败"}
        items.sort(key=lambda x: x.get("date") or "")
        # v1.1.5: 指标统一按单位净值(dwjz)口径计算, 与榜单/东财一致 ——
        # 旧实现用 ljjz(累计净值), 分红/拆分基金(全市场 822 只)会严重偏高,
        # 实测 161725: d1 显示 0.26% 而真值 1.06%、净值显示 2.27(真值 0.55)。
        _pairs = fetcher.nav_pairs(items)
        dates = [d for d, _ in _pairs]
        navs = [float(v) for _, v in _pairs]
        nav = navs[-1] if navs else None
        nav_date = _pairs[-1][0] if _pairs else None
        # v2026-08-28: 保存净值到DB,避免非Fund库基金重复抓取
        try:
            import db as _db
            _nav_items = [{"date": x.get("date"), "ljjz": x.get("ljjz"), "dwjz": x.get("dwjz")} for x in items if x.get("date") and x.get("ljjz")]
            if _nav_items:
                _db.bulk_upsert_navs(code, _nav_items)
        except Exception:
            pass
        # 3) 持仓 + 主题(优先 DB → 实时)
        stocks = []
        themes = []
        # DB 已有 themes/stocks
        try:
            _db_themes = _db_row.get("themes")
            if isinstance(_db_themes, str):
                import json as _j
                _db_themes = _j.loads(_db_themes) if _db_themes else []
            if _db_themes and isinstance(_db_themes, list) and len(_db_themes) > 0:
                themes = [[t.get("name") if isinstance(t, dict) else (t[0] if isinstance(t, list) else t),
                          t.get("pct") if isinstance(t, dict) else (t[1] if isinstance(t, list) and len(t) > 1 else None)]
                         for t in _db_themes[:6]]
            _db_stocks = _db_row.get("stocks")
            if isinstance(_db_stocks, str):
                import json as _j
                _db_stocks = _j.loads(_db_stocks) if _db_stocks else []
            if _db_stocks and isinstance(_db_stocks, list) and len(_db_stocks) > 0:
                stocks = [[s.get("name") if isinstance(s, dict) else (s[0] if isinstance(s, list) else s),
                          s.get("pct") if isinstance(s, dict) else (s[1] if isinstance(s, list) and len(s) > 1 else None)]
                         for s in _db_stocks[:10]]
        except Exception:
            pass
        # 实时抓取补全
        # v2026-08-29: 改用带缓存 + 落库的 _holdings_cached(单次抓取实测可达 1.57s,
        #   持仓按季度披露, 缓存 6 小时不影响数据准确性)
        try:
            if not stocks:
                stocks = _holdings_cached(code)
            if not themes:
                themes_raw = fetcher.holdings_themes(stocks) if stocks else []  # [(label, weight), ...] 元组
                # v2.9.0 修复: holdings_themes 返回元组列表, 旧实现用 t.get() 抛 AttributeError 被吞,
                #   themes 永远为空 → 改用位置索引
                themes = [[t[0], t[1]] for t in themes_raw[:6] if t and t[0]]
        except Exception:
            pass
        # 4) 区间涨幅 / metrics
        m = ranker.calc_metrics(navs) if len(navs) >= 8 else {}
        # v0.88.1: 优先从 pingzhongdata 直接拉取阶段涨跌幅(避免新基金净值不足导致 m1/m3/m6/y1 为空)
        # v2026-08-29: 复用开头并发预取的 _pre_returns, 不再重复发起网络请求
        try:
            _direct_returns = _pre_returns
            if _direct_returns:
                for _k in ("m1", "m3", "m6", "y1"):
                    if _direct_returns.get(_k) is not None:
                        m[_k] = _direct_returns[_k]
        except Exception:
            pass
        # 补 m1 / m3 / m6 / y1 / d1 / d2 —— 数据不足时回退 DB 池内已算好的数据
        # v1.1.5: m1/m3/m6/y1 改为 setdefault 语义 —— 上面 syl_*(官方)已覆盖过的不再
        # 重算覆盖(旧实现无条件重算, 把官方值又盖回净值计算值, 实测 161725 近1月
        # 显示 -5.49% 而东财官方 -0.93%); d1/d2 无官方源, 保持计算值(dwjz 口径)。
        # v2.9.19: d1计算前调用数据质量校验，连续两天净值相同则d1设为None（不兜底）
        if len(navs) >= 2 and navs[-2]:
            _qa = full_quality_check(navs, dates)
            m["d1"] = _qa.d1  # suspect状态时为None
            m["data_status"] = _qa.status
            if _qa.reason:
                m["data_status_reason"] = _qa.reason
        else:
            m["data_status"] = "stale"
            m["d1"] = None
        if len(navs) >= 21:
            m.setdefault("m1", round((navs[-1] / navs[-21] - 1) * 100, 4))
        else:
            m.setdefault("m1", _db_row.get("m1"))  # DB 兜底(仅在直接拉取和净值计算都缺失时)
        if len(navs) >= 63:
            m.setdefault("m3", round((navs[-1] / navs[-63] - 1) * 100, 4))
        else:
            m.setdefault("m3", _db_row.get("m3"))
        if len(navs) >= 126:
            m.setdefault("m6", round((navs[-1] / navs[-126] - 1) * 100, 4))
        else:
            # v0.50.x: 修复 m6 缺失 —— 即便只有 ~30 日净值,DB 池内已有的 m6 也复用
            m.setdefault("m6", _db_row.get("m6"))
        if len(navs) >= 252:
            m.setdefault("y1", round((navs[-1] / navs[-252] - 1) * 100, 4))
        else:
            m.setdefault("y1", _db_row.get("y1"))
        # 补 dd20(距 20 日高) —— 30 日内也可算, 但 ranker 用的区间口径更准
        m.setdefault("dd20", _db_row.get("dd20"))
        # 补 mdd / mdd_days / mdd_status(若是 navs 太短, 用 DB)
        m.setdefault("mdd", _db_row.get("mdd"))
        m.setdefault("mdd_days", _db_row.get("mdd_days"))
        m.setdefault("mdd_status", _db_row.get("mdd_status"))
        # 补 streak(DB 优先, 否则走 ranker)
        m.setdefault("streak", _db_row.get("streak") or m.get("streak"))
        # 补 max_daily_drop_7d(DB 优先, 否则用 max_daily_drop_7d 兜底)
        m.setdefault("max_daily_drop_7d", _db_row.get("max_daily_drop_7d") or m.get("max_daily_drop_7d") or m.get("max_daily_drop_7d"))
        # 5) 推断 ftype / sec / is_etf(用名字猜)
        ftype = _db_row.get("ftype") or _guess_ftype(name)
        sec = _db_row.get("sec") or _guess_sec(name)
        is_etf = _db_row.get("is_etf") or (1 if "ETF" in (name or "") or "联接" in (name or "") else 0)
        # 6) 七档推荐 + 综合分(用 ranker 的 reco 路径,失败则空)
        reco = None
        try:
            if len(navs) >= 30:
                _, _, reco_score, reco_level, _, _ = ranker.compute_reco_v32(m, navs)
                reco = reco_level
        except Exception:
            pass
        # 7) v0.50.x: 评分 / 抗跌·收益 / 动能 / 连涨 / 历史回撤 / 距20日高
        #    v2.9.43: 全局统一使用 compute_scores_v2 算法（数据库存储）
        #    一键更新阶段4 recompute_ad 批量计算后写入数据库，所有页面/脚本统一读取
        _score = _db_row.get("score")
        _ad_score = _db_row.get("ad_score")
        _earn_score = _db_row.get("earn_score")
        _ms = _db_row.get("ms") or (ranker.kinetic_state(m) if (m and len(navs) >= 8) else None)
        _streak = _db_row.get("streak") or m.get("streak")
        _mdd_val = _db_row.get("mdd") or m.get("mdd")
        _mdd_days = _db_row.get("mdd_days") or m.get("mdd_days")
        _mdd_status = _db_row.get("mdd_status") or m.get("mdd_status")
        _dd20 = _db_row.get("dd20") or m.get("dd20")
        # 8) reco 后续参数(尽量从 DB 补)
        _reco_days = _db_row.get("reco_days")
        _prev_reco = _db_row.get("prev_reco")
        _prev_reco_days = _db_row.get("prev_reco_days")
        # v0.71.0: 从榜单快照获取排名标签
        # v2.11.4 C2/Q8: board_days 全链路下线, 删除近30天在榜天数查询
        _rtag = None
        try:
            import db as _db2
            _conn = _db2.get_conn()
            # 获取最新日期在各个榜单的排名
            _latest_date = _conn.execute(
                "SELECT MAX(date) as d FROM rank_snapshots WHERE code=?", (code,)
            ).fetchone()
            if _latest_date:
                _ld = _latest_date["d"] if hasattr(_latest_date, "keys") else _latest_date[0]
                if _ld:
                    _rows = _conn.execute(
                        "SELECT sub, rank FROM rank_snapshots WHERE code=? AND date=? ORDER BY panel, sub",
                        (code, _ld)
                    ).fetchall()
                    if _rows:
                        _tags = []
                        for _r in _rows:
                            _sub = _r["sub"] if hasattr(_r, "keys") else _r[0]
                            _rank = _r["rank"] if hasattr(_r, "keys") else _r[1]
                            if _sub and _rank:
                                _tags.append(f"{_sub}#{_rank}")
                        if _tags:
                            _rtag = " ".join(_tags[:5])  # 最多显示5个
        except Exception as _e:
            pass
        d = {
            "ok": True,
            "code": code,
            "name": name,
            "scale": scale,
            "est": est,
            "ftype": ftype,
            "sec": sec,
            "is_etf": is_etf,
            "manager": _db_row.get("manager"),
            "track": _db_row.get("track"),
            "themes": themes,
            "stocks": stocks,
            "nav": nav,
            "nav_date": nav_date,
            "d1": m.get("d1"),
            "d2": m.get("d2"),
            "d3": m.get("d3"), "d5": m.get("d5"), "d7": m.get("d7"), "d10": m.get("d10"),
            "m1": m.get("m1"), "m3": m.get("m3"), "m6": m.get("m6"), "y1": m.get("y1"),
            "dn7": m.get("dn7"), "max_daily_drop_7d": m.get("max_daily_drop_7d"),
            "mdd": _mdd_val,
            "mdd_days": _mdd_days,
            "mdd_status": _mdd_status,
            "max_daily_drop": m.get("max_daily_drop"),
            "dd20": _dd20,
            "dd_from_hi": m.get("dd_from_hi"),
            "vol": m.get("vol"), "down_vol": m.get("down_vol"),
            "down_sharpe": m.get("down_sharpe"), "calmar": m.get("calmar"),
            "pl": m.get("pl"),
            "hi_cnt": m.get("hi_cnt"),
            # v1.1.6: up_ratio 对外统一为百分比(×100) —— 旧值 0~1 被前端
            # 直接 toFixed+'%' 渲染成 "0%"/"0.4%"(对比表里与捕获率 41.2% 并排穿帮)
            "up_ratio": round((m.get("up_ratio") or 0) * 100, 1),
            "streak": _streak,
            # v0.50.x: 评分 / 抗跌·收益 —— 修复 #1「综合分后动能 5 档标签不渲染」
            "score": _score,
            "ad_score": _ad_score,
            "earn_score": _earn_score,
            "ms": _ms,
            "reco": reco,
            "reco_days": _reco_days,
            "prev_reco": _prev_reco,
            "prev_reco_days": _prev_reco_days,
            # v0.71.0: 榜单排名标签
            # v2.11.4 C2/Q8: board_days 全链路下线, 从返回结构中移除
            "rtag": _rtag,
        }
        return d
    _result = _safe(_do, default={"ok": False, "msg": "night_fund_monitor.fetch_watch_detail 异常"})
    # v2026-08-28: 成功结果存入10分钟缓存
    if _result and _result.get("ok"):
        # 缓存容量控制:最多200条
        if len(_WATCH_DETAIL_CACHE) >= 200:
            _oldest = min(_WATCH_DETAIL_CACHE.items(), key=lambda x: x[1]['time'])
            if _oldest: del _WATCH_DETAIL_CACHE[_oldest[0]]
        _WATCH_DETAIL_CACHE[code] = {'time': _time.time(), 'data': _result.copy()}
    return _result


def _fetch_name(code: str) -> Optional[str]:
    """从天天基金 pingzhongdata 提取基金名称(轻量级)。

    v1.1.4: 改走 fetcher.fetch_pingzhong 统一入口 —— 复用 6h 档案缓存与
    15min 原始文本缓存, 并受单飞锁保护。旧实现裸打 pingzhongdata(129KB),
    每次弹窗都要重新下载一份完整 js, 属纯冗余流量。
    """
    def _do():
        from collector import fetcher
        return (fetcher.fetch_pingzhong(code, need=("name",)) or {}).get("name")
    return _safe(_do, default=None)


def _guess_ftype(name: str) -> str:
    """根据基金名推断类型(ETF联接/主动C/被动型QDII)."""
    n = name or ""
    if "ETF" in n and ("联接" in n or "连接" in n):
        return "ETF联接C"
    if "ETF" in n:
        return "ETF"
    if "QDII" in n:
        return "QDII"
    if n.endswith("C"):
        return "主动C"
    return "主动"


def _guess_sec(name: str) -> str:
    """根据基金名粗略归类(实际板块会在 recompute_ad 里细分)。"""
    n = name or ""
    table = {
        "银行": "金融", "证券": "非银金融", "保险": "非银金融",
        "煤炭": "煤炭", "钢铁": "钢铁", "有色": "有色金属",
        "新能源": "新能源", "光伏": "光伏", "锂电": "锂电池",
        "半导体": "半导体", "芯片": "半导体", "电子": "电子",
        "计算机": "科技", "软件": "软件", "互联网": "科技",
        "消费": "消费", "食品": "食品饮料", "白酒": "食品饮料",
        "医药": "医药", "医疗": "医药", "创新药": "医药",
        "军工": "国防军工", "地产": "房地产", "基建": "基建",
        "红利": "红利", "红利低波": "红利", "高股息": "红利",
        "农业": "农业", "传媒": "传媒", "游戏": "传媒", "影视": "传媒",
        "恒生": "港股", "港股通": "港股", "港股": "港股",
        "美股": "美股", "纳斯达克": "美股", "标普": "美股",
        "黄金": "黄金", "黄金ETF": "黄金",
        "石油": "石油石化", "天然气": "石油石化", "油气": "石油石化",
    }
    for kw, sec in table.items():
        if kw in n:
            return sec
    return "其他"

