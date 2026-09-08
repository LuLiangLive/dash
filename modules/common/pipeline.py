"""
pipeline.py —— 采集主流程:抓净值 → 计算指标/榜单 → 写 SQLite → 记录任务日志

每次执行(手动或 20:30 调度)产出:
- funds 表:全部基金最新指标与评分
- nav_history 表:净值历史(增量 upsert)
- rank_snapshots 表:日榜 + 推荐榜单 (v0.96.1: 排行榜/主题轮动已删除)
- task_logs 表:本次执行记录
"""
from __future__ import annotations

from modules.common.data_quality import check_nav_coverage, check_yearly_coverage

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import logging

logger = logging.getLogger(__name__)

import db
import collector.fetcher as fetcher
import collector.ranker as ranker
from collector.recommendation import kinetic_state
import collector.sector as sector


def _today():
    import datetime as _d
    return _d.date.today().isoformat()


# v2.11.4 C2/Q8: _prev_boards 已删除, board_days 全链路下线 (前端零引用)


def _stored_holdings(code: str):
    """读取 funds 表已抓取的真实持仓 (themes, stocks); 无持仓返回 None。"""
    row = db.get_conn().execute(
        "SELECT themes, stocks FROM funds WHERE code=?", (code,)
    ).fetchone()
    if not row:
        return None
    try:
        th_l = json.loads(row["themes"]) if row["themes"] not in ("", None) else []
        st_l = json.loads(row["stocks"]) if row["stocks"] not in ("", None) else []
    except Exception:
        return None
    if st_l:
        return (th_l, st_l)
    return None


def _fetch_top_holdings(today: str, verbose: bool = True) -> None:
    """榜单排名完成后,抓取上榜基金 + 自选基金的持仓主题与重仓股(仅展示,不参与排名计算)。

    只抓 rank_snapshots 当日上榜的基金(通常几百只) + watchlist 自选基金;
    v2.5.0(perf): 串行→ThreadPoolExecutor(workers=5) 并发抓取, 主线程串行写 DB;
    已有持仓的跳过(断点续传);抓取失败不影响榜单与采集结果。
    v0.54.2: 新增自选基金持仓抓取,解决自选卡片持仓主题缺失问题。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import threading

    conn = db.get_conn()
    # 上榜基金代码
    rank_codes = [r[0] for r in conn.execute(
        "SELECT DISTINCT code FROM rank_snapshots WHERE date=? AND code != ''",
        (today,),
    ).fetchall()]
    # 自选基金代码
    watch_codes = [r[0] for r in conn.execute(
        "SELECT DISTINCT code FROM watchlist WHERE code != ''",
    ).fetchall()]
    # 合并去重
    codes = list(set(rank_codes + watch_codes))
    if not codes:
        return
    name_map = {}
    marks = ",".join("?" * len(codes))
    for r in conn.execute(
        f"SELECT code, name FROM funds WHERE code IN ({marks})", codes
    ).fetchall():
        name_map[r["code"]] = r["name"]
    # 筛选需要抓取的基金(已有持仓跳过)
    todo = []
    skipped = 0
    for c in codes:
        row = conn.execute("SELECT stocks FROM funds WHERE code=?", (c,)).fetchone()
        if row and row["stocks"] not in ("[]", "", None):
            skipped += 1
            continue
        todo.append(c)
    if not todo:
        if verbose:
            pass
        return

    # 并发抓取 (workers=5, 同花顺API并发敏感)
    def _work(code):
        try:
            # v2.9.10: 优先使用同花顺API获取持仓，失败自动降级到天天基金网
            from collector.hithink_integration import get_fund_holdings_simple
            hs = get_fund_holdings_simple(code)
            th = fetcher.holdings_themes(hs) if hs else []  # [(label, weight), ...]
            # v2.9.0 修复: holdings_themes 返回元组, 旧实现用 t["pct"] 索引抛 TypeError
            #   被 except 吞掉→持仓主题被丢弃、降级成名称推断 → 改用位置索引
            th = [t for t in th if t[1] >= 0.5]
            if not th:
                th = sector.infer_themes(name_map.get(code, ""))
            stocks = [[str(s[0]), float(s[1])] for s in hs]
            time.sleep(0.3)  # 限流: 每只请求后间隔
            return code, th, stocks
        except Exception:
            time.sleep(0.3)
            # v0.51.3: 抓取异常时也用名称推断主题, 避免上榜基金 themes 为空
            th = sector.infer_themes(name_map.get(code, ""))
            return code, th, []

    got = done = 0
    write_lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=5) as ex:
        futures = {ex.submit(_work, c): c for c in todo}
        for fut in as_completed(futures):
            code, th, stocks = fut.result()
            # 主线程串行写 DB (SQLite 连接线程局部, 避免写并发)
            with write_lock:
                conn.execute(
                    "UPDATE funds SET themes=?, stocks=? WHERE code=?",
                    (json.dumps(th, ensure_ascii=False),
                     json.dumps(stocks, ensure_ascii=False), code),
                )
            if th:
                got += 1
            done += 1
    conn.commit()
    # v0.51.3: 兜底 — 对所有 themes 仍为空的上榜基金, 用名称推断主题并写入
    # 覆盖: 抓取失败/已有 stocks 但 themes 为空/从未抓取过的基金
    fallback = 0
    for c in codes:
        row = conn.execute("SELECT themes FROM funds WHERE code=?", (c,)).fetchone()
        if row and row["themes"] in ("", None, "[]"):
            th = sector.infer_themes(name_map.get(c, ""))
            if th:
                conn.execute(
                    "UPDATE funds SET themes=? WHERE code=?",
                    (json.dumps(th, ensure_ascii=False), c),
                )
                fallback += 1
    if fallback:
        conn.commit()
    if verbose:
        pass


def _fetch_top_history(today: str, verbose: bool = True, history_days: int = 500) -> None:
    """v2.9.8 优化: 为上榜基金 + 自选基金 + 当日涨幅前100 + 当日跌幅前100抓取更长历史净值(默认2年/500交易日)。

    根因: nav_history 全量基金只存约92天(3个月), 导致上榜/自选基金的
          m6/y1 涨跌幅、抗跌分析、区间最大回撤等指标无法从净值历史计算,
          前端显示数据缺失。

    策略: 参考 _fetch_top_returns 模式, 只对 rank_snapshots 当日上榜基金
          + watchlist 自选基金 + 当日涨幅前100 + 当日跌幅前100抓取历史净值;
          已有足够历史数据(>=history_days)的基金跳过; 并发抓取(workers=8);
          单只失败不影响整体。

    v2.5.4优化: 改用 pingzhongdata 接口获取全量净值, 速度从2~5秒/只
          提升到0.04秒/只(快50倍), 单次请求获取从成立至今的全部净值。

    v2.9.8优化: 动态候选池 - 加入当日涨幅前100和跌幅前100, 让新基金
          有机会获得完整2年历史净值, 解决"榜单自增强"问题。

    Args:
        today: 今日日期(YYYY-MM-DD)
        verbose: 是否打印详细日志
        history_days: 需要的历史数据天数(默认500交易日≈2年)
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import threading

    conn = db.get_conn()

    # 1. 获取上榜基金代码
    rank_codes = [r[0] for r in conn.execute(
        "SELECT DISTINCT code FROM rank_snapshots WHERE date=? AND code != ''",
        (today,),
    ).fetchall()]

    # 2. 获取自选基金代码
    watch_codes = [r[0] for r in conn.execute(
        "SELECT DISTINCT code FROM watchlist WHERE code != ''",
    ).fetchall()]

    # 3. v2.9.8: 动态候选池 - 加入当日涨幅前100和跌幅前100（让新基金有机会获得完整数据）
    dynamic_codes = []
    try:
        from modules.fund.universe_manager import fetch_daily_top100
        # 涨幅前100
        top100 = fetch_daily_top100(verbose=False)
        dynamic_codes.extend([f["code"] for f in top100])

        # 跌幅前100（从全市场排行中按日涨幅升序取前100）
        from collector import fetcher as _fch
        all_funds = _fch.fetch_all_market()
        # 过滤C类且非债券/货币/持有期
        from modules.common.filters import is_holding_period
        c_funds = [f for f in all_funds
                   if _fch._is_c_share(f.get("name", ""))
                   and not is_holding_period(f)]
        # 按日涨幅升序（跌幅最大的排前面）
        c_funds.sort(key=lambda x: x.get("rzf", 0))
        bottom100 = c_funds[:100]
        dynamic_codes.extend([f["code"] for f in bottom100])

        if verbose:
            print(f"[动态候选池] 涨幅前100: {len(top100)}只, 跌幅前100: {len(bottom100)}只")
    except Exception as _e:
        if verbose:
            print(f"[动态候选池] 获取失败(不影响榜单): {_e}")

    # 4. 合并去重（上榜 + 自选 + 涨幅前100 + 跌幅前100）
    codes = list(set(rank_codes + watch_codes + dynamic_codes))
    if not codes:
        if verbose:
            pass
        return

    # 4. 筛选历史数据不足的基金
    todo = []
    skipped = 0
    for code in codes:
        count = conn.execute(
            "SELECT COUNT(*) as cnt FROM nav_history WHERE code=?", (code,)
        ).fetchone()["cnt"]
        if count < history_days:
            todo.append(code)
        else:
            skipped += 1

    if not todo:
        if verbose:
            pass
        return

    if verbose:
        pass

    # 5. 并发抓取历史净值 (v2.5.4: 改用pingzhongdata接口, 单次请求获取全量净值)
    def _work(code):
        try:
            # pingzhongdata接口: 单次请求获取从成立至今的全部净值, 速度0.04秒/只
            pz = fetcher.fetch_pingzhong(code, need={"navs"})
            navs = pz.get("navs", []) if pz else []
            # 只保留最近history_days条数据, 避免写入过多旧数据
            if len(navs) > history_days:
                navs = navs[-history_days:]
            time.sleep(0.1)  # 限流
            return code, navs
        except Exception as e:
            time.sleep(0.1)
            # 失败兜底: 退回原有的分页拉取方式
            try:
                import datetime as _dt
                start_date = (_dt.date.today() - _dt.timedelta(days=history_days * 2)).isoformat()
                navs = fetcher.fetch_nav_history(
                    code, page_size=history_days,
                    max_pages=max(1, history_days // 20),
                    start_date=start_date,
                )
                return code, navs
            except Exception:
                return code, []

    got = done = 0
    write_lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(_work, c): c for c in todo}
        for fut in as_completed(futures):
            code, navs = fut.result()
            if navs:
                with write_lock:
                    db.bulk_upsert_navs(code, navs)
                got += 1
            done += 1
            if verbose and done % 10 == 0:
                pass

    if verbose:
        pass


def _fill_fund_profiles(funds_meta: list[dict], verbose: bool = True,
                        progress_cb=None) -> int:
    """v2.1.2 新增: 批量补齐全量基金档案(name/ftype/scale/manager/est)。

    根因: 全新空库增量采集时 pool 来自空 funds 表, name/ftype/scale 全为空,
          导致: ①质量榜 C 类筛选(name.endswith("C"))0 命中 → quality_pool=[]
                 → _min_max_norm([]) 抛 min() 空列表异常 → 重排榜单阶段崩溃(P0);
               ②前端基金列表/榜单/详情页全部显示空名称(P1)。

    策略: 仅对 name 为空的基金批量抓取; 移动端接口(3.6KB)覆盖 name/ftype/scale/
          manager/est, 约 20ms/只; 并发4(东方财富限流实测最优); 已有 name 跳过;
          单只失败不影响整体; 抓取结果就地更新 funds_meta, 后续 upsert_fund 落库。

    Returns: 成功补齐 name 的基金数量。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import threading as _th

    # 仅补 name 为空的基金(增量更新时大部分已有 name, 直接跳过)
    todo = [f for f in funds_meta if not f.get("name")]
    if not todo:
        return 0

    if verbose:
        pass
    if progress_cb:
        try:
            progress_cb("fetch", 0, len(todo), "", f"批量补齐基金档案 {len(todo)} 只")
        except Exception:
            logger.debug('progress_cb 回调异常（UI 进度回调，不影响采集主流程）')

    _lock = _th.Lock()
    results: dict[str, dict] = {}

    def _work(code):
        try:
            # 移动端接口覆盖 name/ftype/scale/manager/est, 单次请求约 20ms
            prof = fetcher.fetch_profile(
                code, need=("name", "ftype", "scale", "manager", "est"))
            return code, prof
        except Exception:
            return code, {}

    _t0 = time.time()
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = [ex.submit(_work, f["code"]) for f in todo]
        for i, fu in enumerate(as_completed(futs), 1):
            try:
                code, prof = fu.result()
            except Exception:
                continue
            if prof:
                with _lock:
                    results[code] = prof
            if verbose and (i % 1000 == 0 or i == len(todo)):
                _dt = time.time() - _t0
            if progress_cb and (i % 500 == 0 or i == len(todo)):
                try:
                    progress_cb("fetch", i, len(todo), "",
                                f"批量补齐基金档案 {i}/{len(todo)}")
                except Exception:
                    logger.debug('progress_cb 回调异常（UI 进度回调，不影响采集主流程）')

    # 就地更新 funds_meta(后续 upsert_fund 会落库)
    ok = 0
    for f in funds_meta:
        prof = results.get(f["code"])
        if not prof:
            continue
        if prof.get("name"):
            f["name"] = prof["name"]
            ok += 1
        if prof.get("ftype") and not f.get("ftype"):
            f["ftype"] = prof["ftype"]
        if prof.get("scale") is not None:
            f["scale"] = prof["scale"]
        if prof.get("manager"):
            f["manager"] = prof["manager"]
        if prof.get("est"):
            f["est"] = prof["est"]

    _dt = time.time() - _t0
    if verbose:
        pass
    return ok


def run_collector(max_funds: int = 20, verbose: bool = True, progress_cb=None,
                  cancel_flag: object = None, incremental: bool = False,
                  pool_override: list[str] | None = None) -> dict:
    """执行一次完整采集,返回统计。

    incremental=True: 增量模式, 仅对缺失日期区间补抓(默认按 effective_nav_date 判定
                     交易日 19:00 前后窗口), 不重复拉取已缓存历史, 大幅提速。

    progress_cb: 可选回调 fn(stage, done, total, code, msg), 用于前端实时进度展示。
    cancel_flag: 可选 threading.Event, 置位后尽快中止抓取/计算。
    """
    started = time.time()
    log_id = db.log_start("collector")
    # v2.11.4 C4: rank_items 由 rank_full 独立产, pipeline 不再统计
    stats = {"funds": 0, "nav_points": 0, "errors": [],
             "skipped_cached": 0, "fetched": 0}

    # 提前定义,避免 pool_override 分支绕过 else 时 242/362 行 UnboundLocalError
    # v0.51.0 修复:一键更新用 pool_override 走增量模式,跳过 if/else 分支里 full_market 赋值,
    # 但后续 elif full_market / _stored = _stored_holdings(code) if full_market 仍要读这个变量。
    # 现在改为函数顶部统一定义,任何路径都能正确读到合法 bool 值。
    full_market = os.environ.get("FULL_MARKET", "0") == "1"

    def _prog(stage, done, total, code="", msg=""):
        if progress_cb:
            try:
                progress_cb(stage, done, total, code, msg)
            except Exception:
                logger.debug('progress_cb 回调异常（UI 进度回调，不影响采集主流程）')

    try:
        # 1) 基金池 + 指数(FULL_MARKET=1 时走全市场模式:分类型排行 + 净值缓存)
        # 注: 各阶段前主动调 _prog 让前端实时看到进度, 避免长时间静默
        _prog("fetch", 0, max_funds, "", "拉取全市场基金池…")
        if pool_override is not None:
            # 上层注入的合并池(精选 ∪ 上榜 ∪ 自选);每个元素是 code 字符串
            # v0.51.0 修复:补全 name/ftype/sec 字段,避免后续 upsert_fund fm["name"] KeyError
            # v2.1.6 修复:补全 themes 字段,并在 sec 为"其他"或 themes 为空时从名称推断,
            #           避免一键更新后所有基金 sec="其他"、themes=[] 的恶性循环
            from collector import sector as _sector
            _code_to_meta = {}
            try:
                for _fr in db.get_conn().execute(
                    "SELECT code, name, ftype, sec, themes FROM funds"
                ).fetchall():
                    _d = dict(_fr)
                    # 解析 themes JSON 字符串
                    if _d.get("themes"):
                        try:
                            _d["themes"] = json.loads(_d["themes"])
                        except (TypeError, ValueError):
                            _d["themes"] = []
                    else:
                        _d["themes"] = []
                    _code_to_meta[_fr["code"]] = _d
            except Exception:
                logger.warning('采集: 预载基金元数据(代码/名称/主题)失败，本次 pool 将退化为默认字段')
            pool = []
            for c in pool_override:
                _m = _code_to_meta.get(c) or {}
                _name = _m.get("name") or ""
                _sec = _m.get("sec") or "其他"
                _themes = _m.get("themes") or []
                _ftype = _m.get("ftype") or ""
                # v2.1.6: 如果 sec 为"其他"或 themes 为空,从基金名称推断
                if _name and (_sec == "其他" or not _themes):
                    try:
                        _inferred_sec = _sector.infer_sector(_name)
                        _inferred_themes = _sector.infer_themes(_name)
                        if _sec == "其他":
                            _sec = _inferred_sec
                        if not _themes:
                            _themes = _inferred_themes
                        if not _ftype:
                            _ftype = _sector.infer_type(_name)
                    except Exception:
                        logger.debug('基金「%s」行业/主题名称推断失败，按默认值降级', _name)
                pool.append({"code": c, "name": _name,
                             "ftype": _ftype,
                             "sec": _sec,
                             "themes": _themes,
                             "is_etf": 1 if ("ETF" in _name or "联接" in _name) else 0})
            if verbose:
                pass
        else:
            # v0.86.0: 删除精选池,统一用全市场模式
            pool = fetcher.effective_pool(mode="full", max_funds=max_funds)
            if not pool:
                pool = fetcher.effective_pool(mode="local", max_funds=max_funds)
        _prog("fetch", 0, max_funds, "", f"基金池 {len(pool)} 只 · 拉取指数…")
        idx_series = fetcher.fetch_all_indices(days=90)
        # 指数落库(nav_history, code 用指数代码, ljjz=收盘价),供 anti/compare 实时计算
        if idx_series:
            for ick, iv in idx_series.items():
                db.bulk_upsert_navs(ick, [{"date": x["date"], "ljjz": x["close"]} for x in iv.get("items", [])])
        idx_map = ranker._build_idx_map(idx_series, [])  # 先建,后续用基金净值日期对齐
        # v0.37.8: 中800超额基准(供动能5档 calc_excess_rets),与 recompute_ad 同口径
        _bench_items = None
        if idx_series and "sh000906" in idx_series:
            _bench_items = [(x["date"], x["close"]) for x in idx_series["sh000906"].get("items", [])]
        if verbose:
            pass

        # 2) 抓净值
        #     v0.43.0: 增量模式 (按基金维度跳过缓存最新 == 目标日期的基金,
        #                       仅对缺失日期区间的基金发起抓取)
        #     全量模式保持原逻辑 (nav_cache_full.json 复用 + 并发抓净)
        #     精选模式保持原逻辑 (直接并发抓净)
        codes = [f["code"] for f in pool]
        if incremental:
            # 增量模式: 先算目标日期, 再按基金维度筛缓存命中
            target_date = fetcher.effective_nav_date()
            if verbose:
                pass
            _prog("fetch", 0, len(codes), "",
                  f"增量净值 · 目标日期 {target_date}")
            _def = {"fetched": [], "skipped": [], "errors": []}
            try:
                # v2.11.6: 双模式阈值——每日增量不查历史深度(0)，只补真实缺口；
                # 仅 nav_history 总行数过少(空库/种子库/导入失败)时启用首跑补全。
                # 此前每日恒传250，与 cleanup_old_navs(days=365)≈243个交易日的
                # 清洗窗口数学矛盾，96%基金每日全量重抓（阶段1占85%时长）。
                try:
                    _nav_rows = db.get_conn().execute(
                        "SELECT COUNT(*) FROM nav_history").fetchone()[0]
                except Exception:
                    _nav_rows = 0
                _min_hist = (fetcher.NAV_MIN_TOTAL_DAYS
                             if _nav_rows < fetcher.NAV_FIRST_RUN_MIN_ROWS else 0)
                if _min_hist:
                    _prog("fetch", 0, len(codes), "",
                          f"首跑补全模式: nav_history仅{_nav_rows}行, 启用历史深度检查")
                _def = fetcher.fetch_nav_incremental(
                    codes, target_date=target_date, page_size=250,
                    workers=12, cancel_flag=cancel_flag,
                    on_progress=lambda done, total, code, skipped:
                        _prog("fetch", done, total, code,
                              f"增量抓净值(已跳过{skipped}只)"),
                    min_history_days=_min_hist,
                )
            except Exception as e:
                if verbose:
                    pass
                _def = {"fetched": [], "skipped": [], "errors": codes[:]}

            fetched = _def.get("fetched") or []
            skipped = _def.get("skipped") or []
            stats["skipped_cached"] = len(skipped)
            stats["fetched"] = len(fetched)
            if verbose:
                pass

            # 把新抓的入库 (upsert by code+date, 不覆盖已有)
            if fetched:
                _nav_lines = sum(
                    len(x.get("items") or []) for x in fetched if x.get("items"))
                fetcher.merge_navs_to_db(fetched,
                                          log=lambda m: None)
                stats["nav_points"] += _nav_lines
                if verbose:
                    pass

            # 把 命中跳过 + 新抓 合并成 navs_all (统一接口)
            navs_all = {}
            _missing_dates = {}   # 记录无净值数据基金, 防止覆盖错误
            for s in skipped:
                c = s["code"]
                rows = db.get_nav(c, limit=120, asc=True)
                # 按 dwjz 过滤: 后续所有指标(_ret/calc_metrics/composite_score)都用 dwjz 计算,
                # 此前按 ljjz 过滤会放进 dwjz=NULL 的行,导致指标算出 None 或直接抛 TypeError。
                items = [{"date": r["date"], "ljjz": r["ljjz"], "dwjz": r.get("dwjz")}
                         for r in rows if r.get("dwjz") is not None]
                if items:
                    navs_all[c] = items
                else:
                    _missing_dates[c] = "cache empty"
            for fd in fetched:
                c = fd["code"]
                items = fd.get("items") or []
                navs_all[c] = items
                if not items:
                    _missing_dates[c] = "fetch empty"

            basic_all, themes_all = {}, {}
            if verbose:
                pass
        else:
            # v0.86.0: 删除精选模式,非增量统一走全市场逻辑(缓存+并发16)
            cache_date, cache_items = fetcher._load_nav_cache()
            navs_all = {}
            todo = []
            for c in codes:
                ci = cache_items.get(c)
                if ci and ci.get("navs"):
                    navs_all[c] = ci["navs"]
                else:
                    todo.append(c)
            if todo:
                if verbose:
                    pass
                _prog("fetch", 0, len(todo), "", f"并行抓净值 {len(todo)} 只(并发16)…")
                _got = fetcher.fetch_nav_many(todo, page_size=250, workers=16,
                                              cancel_flag=cancel_flag,
                                              on_progress=lambda done, total, code:
                                                  _prog("fetch", done, total, code, "抓取净值中"))
                for c, ns in _got.items():
                    if ns:
                        navs_all[c] = ns
            fetcher._save_nav_cache(cache_date or _today(), navs_all)
            if verbose and len(navs_all) < fetcher.COVER_FLOOR:
                pass
            basic_all, themes_all = {}, {}
        funds_meta = []
        # 净值抓取完成后, 抓取前再查一次停止信号(提升停止响应)
        if cancel_flag is not None and cancel_flag.is_set():
            stats["errors"].append("已手动停止")
            if verbose:
                pass
            db.log_finish(log_id, "stopped", "手动停止", started)
            return {"ok": False, "error": "已手动停止", **stats}
        # v2.4.1: 预载 funds 表 reco 历史字段(pool 来自接口, 无 reco; 防采集重建榜单丢推荐标签)
        _reco_map = {}
        try:
            for _r in db.get_conn().execute(
                "SELECT code,reco,reco_days,prev_reco,prev_reco_days,reco_score FROM funds"
            ).fetchall():
                _reco_map[_r["code"]] = dict(_r)   # Row→dict, 支持 .get()
        except Exception:
            logger.warning('采集: 预载 funds.reco 历史字段失败，榜单重建可能丢失推荐信号标签')
        for f in pool:
            code = f["code"]
            if cancel_flag is not None and cancel_flag.is_set():
                stats["errors"].append("已手动停止")
                if verbose:
                    pass
                break
            _prog("fetch", len(funds_meta), len(pool), code, "抓取净值中")
            try:
                navs_hist = navs_all.get(code) or []
                # v1.1.3: 标记「这次是走的本地降级」。local_nav_fallback 读的是
                # nav_cache.json 里的种子数据, 它是**按周填充**的(每周一条, dwjz 多为 NULL,
                # 且 ljjz 与真实日期错位一天)。这种数据只能拿来兜底算指标, 绝不能回写
                # nav_history —— 实测它会把 017856 这类基金重新污染成 dwjz 全 NULL,
                # 让刚补好的净值又变回错的。下面据此跳过 bulk_upsert_navs。
                _from_fallback = False
                if not navs_hist:
                    navs_hist = fetcher.local_nav_fallback(code)
                    _from_fallback = bool(navs_hist)
                if not navs_hist:
                    stats["errors"].append(f"{code}: 无净值数据")
                    continue
                # v0.86.0: 用单位净值(dwjz)计算涨幅,与东方财富/同花顺一致
                # 过滤 dwjz 为 NULL 的历史行: 历史库里存在大量 dwjz IS NULL 的记录
                # (如 000217 共 3178 条,其中 3118 条为空)。若直接进入 _ret(),
                # metrics._ret 只判断 base==0 不判断 None,取到 navs[-253] 这类空值时
                # 会抛 TypeError: unsupported operand type(s) for -: 'NoneType' and 'NoneType',
                # 导致整只基金被计入失败。navs 与 dates 必须同步过滤以保持对齐。
                _pairs = [(x["date"], x["dwjz"]) for x in navs_hist if x.get("dwjz") is not None]
                if not _pairs:
                    stats["errors"].append(f"{code}: 净值全为空")
                    continue
                dates = [p[0] for p in _pairs]
                navs = [p[1] for p in _pairs]
                # 当日/两日涨幅
                d1 = ranker._ret(navs, 1)
                d2 = ranker._ret(navs, 2)
                m = ranker.calc_metrics(navs, idx_map, [])
                # v2.10.0: down_sharpe/calmar 仅对C类权益基金有意义(质量榜筛选口径),
                # 非C类或固收类(债/货币/固收)置None, 避免全池6000只无意义计算入库
                _fname = f.get("name") or ""
                _ftype_val = f.get("ftype") or ""
                if not (_fname.endswith("C") and not any(kw in _ftype_val for kw in ("债", "货币", "固收"))):
                    m["down_sharpe"] = None
                    m["calmar"] = None
                m["d1"] = d1
                m["d2"] = d2
                m["rzf"] = d1
                m["m3"] = ranker._ret(navs, 63)
                m["m6"] = ranker._ret(navs, 126)
                m["y1"] = ranker._ret(navs, 252)
                # v2.1.9.4: 动能4档(多周期加权，提高短期敏感度)
                try:
                    m["ms"] = ranker.kinetic_state(m)
                except Exception:
                    _ex = ranker.calc_excess_rets(navs, dates, _bench_items) if _bench_items else {}
                    _fl = ranker.big_rise_follow(navs, dates, idx_map)
                    _p7 = ranker._ret_skipna(navs[:-7], 7) if len(navs) > 14 else None
                    m["ms"] = kinetic_state(m)
                # dn7 使用 calc_metrics 真实 7 日下跌天数(不再用 d7 近似)
                m["dn7"] = m.get("dn7", 0)
                # v2.11.2: dd20 已由 calc_metrics 输出为"距最近20交易日高点回撤", 不再用 dd_from_hi 覆盖
                m["dd20"] = m.get("dd20")
                # 近7日单日最大回撤(近7个交易日内单日最大跌幅;navs 最早在前、最新在后)
                _r7 = navs[-8:] if len(navs) >= 8 else navs
                _dd7 = None
                for _i in range(1, len(_r7)):
                    _prev, _cur = _r7[_i - 1], _r7[_i]
                    if _prev:
                        _chg = (_cur - _prev) / _prev * 100
                        if _chg < 0 and (_dd7 is None or _chg < _dd7):
                            _dd7 = _chg
                m["max_daily_drop_7d"] = _dd7
                m["streak"] = ranker.streak_days(navs)
                _b = basic_all.get(code) or {}
                # 真实持仓主题占比优先,缺失时回退配置估算;全量模式保留已抓真实持仓(防覆盖)
                _stored = _stored_holdings(code) if full_market else None
                _th = themes_all.get(code) or (_stored and _stored[0]) or f.get("themes") or []
                # 兜底来源(funds 表/pool)的 themes 可能是 JSON 字符串,统一解析成 list
                if isinstance(_th, str):
                    try:
                        _th = json.loads(_th)
                    except (TypeError, ValueError):
                        _th = []
                    if not isinstance(_th, list):
                        _th = []
                _st = (_stored and _stored[1]) or []
                _rf = _reco_map.get(code) or {}
                funds_meta.append({
                    **f,
                    "nav_date": dates[-1] if dates else None,
                    "scale": _b.get("scale"),
                    "est": _b.get("est"),
                    "themes": _th,
                    "stocks": _st,
                    "metrics": m,
                    "navs": navs,
                    "dates": dates,
                    "ms": m["ms"],
                    # v2.4.1: 补 reco 历史字段(旧值), 保持榜单/卡片推荐标签稳定
                    "reco": _rf.get("reco"),
                    "reco_days": _rf.get("reco_days"),
                    "prev_reco": _rf.get("prev_reco"),
                    "prev_reco_days": _rf.get("prev_reco_days"),
                    "reco_score": _rf.get("reco_score"),
                })
                if not _from_fallback:
                    db.bulk_upsert_navs(code, navs_hist, commit=False)
                    stats["nav_points"] += len(navs_hist)
            except Exception as e:
                stats["errors"].append(f"{code}: {e}")
        # v2.5.0(perf): 批量 nav_history 统一 commit, 避免 6000 次逐基金 fsync
        db.get_conn().commit()

        # 2.8) v2.1.2: 批量补齐全量基金档案(name/ftype/scale/manager/est)
        # 全新空库时 pool 来自空 funds 表, name 全为空; 必须在写库前补齐,
        # 否则质量榜 C 类筛选 0 命中 → 重排榜单崩溃(P0), 前端也显示空名称(P1)。
        if funds_meta:
            _fill_fund_profiles(funds_meta, verbose=verbose, progress_cb=_prog)

        # v2.11.2: 删除原"对齐指数+anti_resist"第3循环 —— 该循环对全池每只算
        # idx_map_f/ddown/rise/anti_resist 后仅写入内存 fm["idx_map"/"ddown"/"rise"/"resist"],
        # 全文件无任何后续消费(不落库、不给榜单/评分/弹窗)，属算完即弃的重复计算。
        # 真正消费处: 榜单统一分(rank_full 用 nfm.anti_metrics)、上榜+自选精确分
        # (recompute_ad.py:374 anti_resist → compute_scores_v2)、弹窗/自选实时(anti_metrics)。
        # 保留: funds 档案补齐(上面 2.8) 与 nav_history 批量 commit(2.5.0)。

        # 5) 写 funds 表
        # v2.9.19: 数据质量统计
        # v2.9.45: 改为批量写入(bulk_upsert_funds), 从6000次commit降到1次, 解决database is locked
        from modules.common.data_quality import QualityStats
        _qa_stats = QualityStats()
        _funds_to_write = []
        for fm in funds_meta:
            m = fm["metrics"]
            _funds_to_write.append({
                "code": fm["code"],
                "name": fm["name"],
                "ftype": fm.get("ftype") or "",
                "sec": fm.get("sec") or "其他",
                "themes": fm.get("themes") or [],  # [{name, pct}] 持仓主题带估算占比
                "stocks": fm.get("stocks") or [],
                # v2.10.0: score/ad_score/earn_score/yindie 不再在采集阶段全池计算,
                # 由阶段4 recompute_ad 对上榜+自选基金精确重算; 这里不写入(保留数据库旧值)
                "nav": fm["navs"][-1] if fm.get("navs") else m.get("ret2m"),   # v0.45.0: 真实单位净值 (旧版先写 ret2m 再 UPDATE, 增加额外 IO)
                "nav_date": fm["dates"][-1] if fm["dates"] else None,
                "d1": m.get("d1"), "d2": m.get("d2"),
                "d3": m.get("d3"), "d5": m.get("d5"), "d7": m.get("d7"), "d10": m.get("d10"),
                "max_daily_drop_7d": m.get("max_daily_drop_7d"), "dn7": m.get("dn7"), "ms": fm["ms"], "dd20": m.get("dd20"),
                "m1": m.get("m1"), "m3": m.get("m3"), "m6": m.get("m6"), "y1": m.get("y1"),
                "mdd": m.get("mdd"), "mdd_days": m.get("mdd_days"), "mdd_status": m.get("mdd_status"),
                "vol": m.get("vol"), "down_vol": m.get("down_vol"),
                "down_sharpe": m.get("down_sharpe"), "calmar": m.get("calmar"),
                "pl": m.get("pl"), "hi_cnt": m.get("hi_cnt"), "dd_from_hi": m.get("dd_from_hi"),
                # v2.11.4 Q11: 前端零引用(grep src/ 全库 0 命中), 后端不再写入;
                # 数据库列保留兼容旧行
                "scale": fm.get("scale"),
                "est": fm.get("est"),
                # v0.92.0: 建池时 is_etf 推断结果没落库, 导致 6000 只全为 0
                # (ftype 明明标了 'ETF联接C'/'指数C')。这里按名称+ftype 就地兜底判定,
                # 口径与 rank_full.is_idx_fund 一致, 避免两套标准。
                "is_etf": (1 if fm.get("is_etf") else
                           (1 if (("ETF" in (fm.get("ftype") or "")) or ("指数" in (fm.get("ftype") or ""))
                                  or ("ETF" in (fm.get("name") or "")) or ("指数" in (fm.get("name") or ""))
                                  or ("联接" in (fm.get("name") or ""))) else 0)),
                "streak": m.get("streak"),
                # v2.9.19: 数据质量状态
                "data_status": m.get("data_status", "ok"),
                "updated_at": _today(),
            })
            stats["funds"] += 1
            # v2.9.19: 数据质量统计
            _qa_stats.add(m.get("data_status", "ok"))
        # v2.9.45: 批量写入, 单次commit
        _written = db.bulk_upsert_funds(_funds_to_write)
        if verbose:
            logger.info("批量写入 funds 表: %d/%d 只", _written, len(_funds_to_write))
        # 5.5) 清理不在当前池内的基金(仅全量且覆盖充分时执行, 防止小规模/失败采集清库)
        #     全量模式仅保留 C 类, 避免历史 A 类/场内 ETF 残留; 但小规模(如测试/调试)
        #     或净值覆盖不足时不得清理, 否则会把 funds 表删空导致数据丢失。
        if funds_meta and len(funds_meta) >= fetcher.COVER_FLOOR:
            _pc = [fm["code"] for fm in funds_meta]
            _marks = ",".join("?" * len(_pc))
            db.get_conn().execute(
                f"DELETE FROM funds WHERE code NOT IN ({_marks})", _pc)
            db.get_conn().commit()

        # 6) 写榜单(仅当采集覆盖充分时重建, 防止小规模/失败采集覆盖已有榜单)
        _complete = len(funds_meta) >= fetcher.COVER_FLOOR
        _prog("rank", 0, 1, "", "重排榜单中")
        today = _today()
        if not _complete and verbose:
            pass
        # v0.54.0: 榜单完全由 rank_full.compute_all_panels 负责 (fetch_manager 阶段2调用),
        # pipeline 不再调用 ranker.build_ranks (避免重复计算 + 两套算法不一致).
        # v0.96.1: 主题轮动已彻底删除.

        # 8) 更新自选表里的名称
        wl = db.list_watchlist()
        for w in wl:
            f = db.get_fund(w["code"])
            if f and f["name"] != w["name"]:
                db.get_conn().execute("UPDATE watchlist SET name=? WHERE code=?", (f["name"], w["code"]))
        db.get_conn().commit()

        # 9) 榜单排名完成后:抓取上榜基金持仓主题(仅展示,不参与排名;失败不影响采集)
        # v0.86.0: 移至 fetch_manager 阶段2.5(榜单构建后执行),确保新上榜基金也被覆盖
        # try:
        #     _fetch_top_holdings(today, verbose=verbose)
        # except Exception as e:  # noqa: BLE001
        #     if verbose:
        #         print(f"[collector] 上榜持仓抓取失败(不影响榜单): {e}")

        # 9.5) v2.9.44: _fetch_top_returns 已删除(m1/m3/m6/y1由阶段1净值历史计算,不再重复抓取)

        # v2.9.19: 数据质量统计结果
        _qa_stats.finalize()
        if verbose:
            logger.info("数据质量: 总计%d, ok=%d, suspect=%d, conflict=%d, stale=%d, 异常率=%.1f%%%s",
                        _qa_stats.total, _qa_stats.ok, _qa_stats.suspect,
                        _qa_stats.conflict, _qa_stats.stale,
                        _qa_stats.suspect_ratio * 100,
                        " ⚠️ 超过5%告警阈值!" if _qa_stats.should_alert else "")
        stats["data_quality"] = _qa_stats.to_dict()

        # v2.9.53: 数据质量检查 - 净值数据覆盖率
        try:
            _nav_qa = check_nav_coverage(days=5)
            stats["nav_coverage"] = {
                "overall": _nav_qa["overall_coverage"],
                "status": _nav_qa["status"],
                "warning_dates": _nav_qa["warning_dates"],
                "latest_date": _nav_qa["dates"][-1] if _nav_qa["dates"] else None,
                "latest_coverage": _nav_qa["coverage_pct"].get(_nav_qa["dates"][-1], 0) if _nav_qa["dates"] else 0,
            }
            if _nav_qa["status"] != "ok":
                print(f"[数据质量] ⚠️ 净值覆盖率警告: 整体{_nav_qa['overall_coverage']}%, "
                      f"警告日期: {_nav_qa['warning_dates']}")
        except Exception as _e:
            print(f"[数据质量] 检查失败: {_e}")
            stats["nav_coverage"] = {"overall": None, "status": "error", "warning_dates": []}

        # v2.9.54: 数据质量检查 - 最近一年数据完整性
        try:
            _yearly_qa = check_yearly_coverage(min_days=200)
            stats["yearly_coverage"] = {
                "total_funds": _yearly_qa["total_funds"],
                "complete_funds": _yearly_qa["complete_funds"],
                "complete_pct": _yearly_qa["complete_pct"],
                "incomplete_funds": _yearly_qa["incomplete_funds"],
                "incomplete_pct": _yearly_qa["incomplete_pct"],
                "avg_days": _yearly_qa["avg_days"],
                "median_days": _yearly_qa["median_days"],
                "status": _yearly_qa["status"],
                "target_date": _yearly_qa["target_date"],
                "min_days": _yearly_qa["min_days"],
            }
            print(f"[数据质量] 最近一年完整性: 完整{_yearly_qa['complete_funds']}/{_yearly_qa['total_funds']}只 "
                  f"({_yearly_qa['complete_pct']}%), 平均{_yearly_qa['avg_days']}天, "
                  f"中位数{_yearly_qa['median_days']}天, 状态{_yearly_qa['status']}")
            if _yearly_qa["status"] != "ok":
                print(f"[数据质量] ⚠️ 最近一年数据不足: {_yearly_qa['incomplete_funds']}只基金 "
                      f"({_yearly_qa['incomplete_pct']}%)最近一年净值少于200天")
                if _yearly_qa["missing_samples"]:
                    print(f"[数据质量] 数据不足样本（前5只）:")
                    for s in _yearly_qa["missing_samples"][:5]:
                        print(f"  - {s['code']} {s['name']}: {s['days']}天")
        except Exception as _e:
            print(f"[数据质量] 最近一年完整性检查失败: {_e}")
            stats["yearly_coverage"] = {"status": "error", "complete_pct": None}

        # v0.54.2: 净值覆盖数 = 缓存命中 + 实际抓取 (有最新净值数据的基金数量)
        _nav_covered = stats.get("skipped_cached", 0) + stats.get("fetched", 0)
        _nav_total = stats["funds"]
        _coverage_warn = ""
        if stats.get("nav_coverage", {}).get("status") != "ok":
            _coverage_warn = f" · ⚠️净值覆盖{stats['nav_coverage']['overall']}%"
        # v2.9.54: 增加最近一年数据完整率
        _yearly_warn = ""
        _yearly_pct = stats.get("yearly_coverage", {}).get("complete_pct")
        if _yearly_pct is not None and _yearly_pct < 95:
            _yearly_warn = f" · ⚠️年度完整{_yearly_pct}%"
        msg = (f"OK 基金 {stats['funds']} 只 · 净值覆盖 {_nav_covered}/{_nav_total} · "
               f"净值 {stats['nav_points']} 条 · 日期 {today}"
               f"{_coverage_warn}{_yearly_warn}")
        db.log_finish(log_id, "ok", msg, started)
        if verbose:
            if stats["errors"]:
                for e in stats["errors"][:8]:
                    pass
        # v0.42.5: 返回 rank_refreshed 标志, 让 fetch_manager._run 知道今日是否重排榜单
        # (当 _complete=False 时, build_ranks 没跑; 此时 fetch_manager 调用
        # recompute_ad 的 refresh_tscores_for_today 兜底刷新 tscore)
        return {"ok": True, **stats, "date": today, "rank_refreshed": bool(_complete)}

    except Exception as e:
        db.log_finish(log_id, "error", f"异常: {e}", started)
        if verbose:
            import traceback
            traceback.print_exc()
        return {"ok": False, "error": str(e), **stats}


if __name__ == "__main__":
    run_collector()
