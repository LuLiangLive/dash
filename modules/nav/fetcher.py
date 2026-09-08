"""
modules/nav/fetcher.py —— 净值数据抓取

从 collector.fetcher 迁移（v2.5.5架构重构 - 阶段2）
负责：增量净值抓取、净值历史获取、批量净值抓取、净值入库
"""
from __future__ import annotations

import datetime as _dt
import json
import time
from pathlib import Path
from typing import Optional

from collector.http_utils import _get

ROOT = Path(__file__).resolve().parent.parent.parent
DATA = ROOT / "data"

# 近 N 个自然日内, dwjz 非空的净值点数下限。
# 全市场实测中位数 22 点(30 个自然日 ≈ 21 个交易日);
# 而"周五填充"的脏数据只有 4~5 点, 差了一个数量级, 15 是安全的分界线。
MIN_RECENT_NAV_POINTS = 15

# ── v2.11.6: 净值窗口常量收敛（与 cleanup_old_navs(days=365) 清洗窗口对齐）──
# 365 个自然日 ≈ 243 个交易日。历史缺陷：健康判定要求"历史总天数>=250(交易日)"
# 且每日增量恒启用，被清洗的基金永远不达标 → 96%基金每日被判不健康、全量补抓
# （阶段1占一键更新85%时长）。现拆为双模式：
#   每日增量: 只判「最新日期==目标 && 近30天密度>=MIN_RECENT_NAV_POINTS」
#   首跑补全: nav_history 总行数 < NAV_FIRST_RUN_MIN_ROWS 时启用，
#             历史深度门槛用 NAV_MIN_TOTAL_DAYS(可满足)，勿改回 250
NAV_RETENTION_DAYS = 365        # cleanup_old_navs 保留窗口(自然日)
NAV_MIN_TOTAL_DAYS = 200        # 首跑补全: 历史总天数门槛(交易日, 须 < 243)
NAV_MIN_YEARLY_DAYS = 200       # 首跑补全: 最近一年净值天数门槛(交易日)
NAV_FIRST_RUN_MIN_ROWS = 5000   # nav_history 总行数低于此值 → 判定为首跑补全场景

# v2.11.4 Q9: 加载中国A股法定节假日休市日历 (周末由 weekday()>=5 单独判)
_TRADING_CAL_PATH = Path(__file__).parent / "trading_calendar.json"
_HOLIDAYS: set = set()
try:
    _HOLIDAYS = set(json.loads(_TRADING_CAL_PATH.read_text(encoding="utf-8")).get("holidays", []))
except Exception:
    _HOLIDAYS = set()


def is_trading_day(d) -> bool:
    """v2.11.4 Q9: 周末或法定节假日 → False"""
    if d.weekday() >= 5:
        return False
    return d.isoformat() not in _HOLIDAYS


def _now() -> _dt.datetime:
    """当前时间。"""
    return _dt.datetime.now()


def effective_nav_date(now: _dt.datetime = None) -> str:
    """计算"应抓的目标净值日期"。

    规则：
      - 交易日 19:00 前: 当日净值尚未公布, 目标日期 = 缓存最新日期(上一交易日)
      - 交易日 19:00 后: 当日净值陆续公布, 目标日期 = 今日
      - 非交易日(周末/节假日): 取 funds表nav_date最大值(上一交易日), 不强行抓今日

    Returns: ISO 格式日期(YYYY-MM-DD)。
    """
    if now is None:
        now = _now()
    today_iso = now.date().isoformat()
    try:
        import db as _db
        conn = _db.get_conn()
        row = conn.execute(
            "SELECT MAX(nav_date) AS d FROM funds WHERE nav_date IS NOT NULL"
        ).fetchone()
    except Exception:
        row = None
    cached = None
    if row:
        try:
            cached = row["d"] if hasattr(row, "keys") else row[0]
        except Exception:
            cached = row[0] if row else None

    # v2.11.4 Q9: 周末或法定节假日 → 非交易日, 用缓存日期兜底
    if not is_trading_day(now.date()):
        return cached or today_iso

    # 19:00 前: 基金净值通常还没公布, 用缓存最新日期
    if now.hour < 19:
        return cached or today_iso

    # 交易日 19:00 后 → 今日
    return today_iso


def cached_nav_max(code: str) -> Optional[str]:
    """读数据库 nav_history, 返回指定基金已抓最新日期(YYYY-MM-DD), 无则 None。

    只看 **dwjz 非空** 的行: 所有指标(_ret/calc_metrics/composite_score)都用 dwjz 计算,
    而种子数据里存在大量 ljjz 有值、dwjz 为 NULL 的行。此前只判 ljjz, 会把这些
    dwjz 缺失的基金误判为「缓存已最新」而永久跳过, 导致:
      - 净值序列残缺(仅周五有值), _ret(navs,1) 实际算的是跨 5 个交易日的涨幅
      - 016450 的 d1 因此显示为 3.36%(真实 +2.16%), 并错误占据当日榜第一
    改为按 dwjz 判断后, 这类基金会被重新抓取并覆盖为完整序列。
    """
    try:
        import db as _db
        conn = _db.get_conn()
        row = conn.execute(
            "SELECT MAX(date) AS d FROM nav_history WHERE code=? AND dwjz IS NOT NULL",
            (code,),
        ).fetchone()
    except Exception:
        return None
    if not row:
        return None
    try:
        return row["d"] if hasattr(row, "keys") else row[0]
    except Exception:
        return row[0]


def nav_recent_points(code: str, since_date: str) -> int:
    """统计 since_date 之后 dwjz 非空的净值点数。"""
    try:
        import db as _db
        row = _db.get_conn().execute(
            "SELECT COUNT(*) AS n FROM nav_history "
            "WHERE code=? AND date>=? AND dwjz IS NOT NULL",
            (code, since_date),
        ).fetchone()
    except Exception:
        return 0
    try:
        return int(row["n"] if hasattr(row, "keys") else row[0])
    except Exception:
        return 0


def _batch_nav_health(codes: list[str], target_date: str, since_date: str,
                      min_history_days: int = 0, min_yearly_days: int = 200) -> dict:
    """一次查询给出所有基金的「缓存健康」判定 → {code: bool}。

    条件: MAX(date WHERE dwjz NOT NULL) == target_date
          且 近 30 个自然日 dwjz 非空点数 >= MIN_RECENT_NAV_POINTS
          且 (可选) 历史净值总天数 >= min_history_days
          且 (可选) 最近一年(365天)净值天数 >= min_yearly_days

    v2.9.14: 新增 min_history_days 参数，用于历史数据补全场景。
             部署时第一次运行传入 min_history_days=250，
             历史数据不足的基金会被判定为不健康，触发补全。
    v2.9.54: 新增 min_yearly_days 参数，确保最近一年数据完整性。
             默认200天(约1年交易日的80%)，最近一年数据不足的基金
             即使历史总天数达标也会被判定为不健康，触发补全。
    """
    out = {c: False for c in codes}
    if not codes:
        return out
    try:
        import db as _db
        conn = _db.get_conn()
        marks = ",".join("?" for _ in codes)
        if min_history_days > 0 or min_yearly_days > 0:
            # v2.9.54: 同时查询历史总天数和最近一年天数
            import datetime as _dt_y
            try:
                _yearly_since = (_dt_y.date.fromisoformat(target_date)
                                - _dt_y.timedelta(days=365)).isoformat()
            except Exception:
                _yearly_since = since_date
            rows = conn.execute(
                f"SELECT code, MAX(date) AS mx, "
                f"  SUM(CASE WHEN date>=? AND dwjz IS NOT NULL THEN 1 ELSE 0 END) AS recent, "
                f"  COUNT(CASE WHEN dwjz IS NOT NULL THEN 1 END) AS total, "
                f"  SUM(CASE WHEN date>=? AND dwjz IS NOT NULL THEN 1 ELSE 0 END) AS yearly "
                f"FROM nav_history WHERE code IN ({marks}) AND dwjz IS NOT NULL "
                f"GROUP BY code",
                (since_date, _yearly_since, *codes),
            ).fetchall()
            for r in rows:
                c = r["code"] if hasattr(r, "keys") else r[0]
                mx = r["mx"] if hasattr(r, "keys") else r[1]
                recent = r["recent"] if hasattr(r, "keys") else r[2]
                total = r["total"] if hasattr(r, "keys") else r[3]
                yearly = r["yearly"] if hasattr(r, "keys") else r[4]
                # v2.9.54: 增加最近一年完整性检查
                _healthy = (mx == target_date) and (recent or 0) >= MIN_RECENT_NAV_POINTS
                if min_history_days > 0:
                    _healthy = _healthy and (total or 0) >= min_history_days
                if min_yearly_days > 0:
                    _healthy = _healthy and (yearly or 0) >= min_yearly_days
                out[c] = _healthy
        else:
            rows = conn.execute(
                f"SELECT code, MAX(date) AS mx, "
                f"  SUM(CASE WHEN date>=? AND dwjz IS NOT NULL THEN 1 ELSE 0 END) AS recent "
                f"FROM nav_history WHERE code IN ({marks}) AND dwjz IS NOT NULL "
                f"GROUP BY code",
                (since_date, *codes),
            ).fetchall()
            for r in rows:
                c = r["code"] if hasattr(r, "keys") else r[0]
                mx = r["mx"] if hasattr(r, "keys") else r[1]
                recent = r["recent"] if hasattr(r, "keys") else r[2]
                out[c] = (mx == target_date) and (recent or 0) >= MIN_RECENT_NAV_POINTS
    except Exception:
        # 批量查询失败时退回逐只判断(慢但不会误判为「需重抓」导致全量风暴)
        for c in codes:
            out[c] = nav_is_healthy(c, target_date)
    return out


def nav_cached_max_many(codes: list[str]) -> dict[str, str]:
    """批量查每只基金 nav_history 的最新日期 → {code: max_date}(无缓存的不出现)。

    v1.1.4: 增量抓取从「固定 60 条窗口」改为「按缺口区间」抓取的前置查询。
    分块执行以规避 SQLite 变量数上限(老版本默认 999)。
    查询失败时返回 {} —— 所有基金退回「首抓 60 条窗口」的旧行为, 安全但不省。
    """
    out: dict[str, str] = {}
    codes = [c for c in codes if c]
    if not codes:
        return out
    try:
        import db as _db
        conn = _db.get_conn()
    except Exception:
        return out
    try:
        for i in range(0, len(codes), 900):
            part = codes[i:i + 900]
            marks = ",".join("?" for _ in part)
            rows = conn.execute(
                f"SELECT code, MAX(date) AS mx FROM nav_history "
                f"WHERE code IN ({marks}) GROUP BY code",
                part,
            ).fetchall()
            for r in rows:
                c = r["code"] if hasattr(r, "keys") else r[0]
                mx = r["mx"] if hasattr(r, "keys") else r[1]
                if c and mx:
                    out[c] = mx
    except Exception:
        return {}
    return out


def _get_existing_dates_many(codes: list[str], since_date: str) -> dict[str, set]:
    """v2.9.54: 批量查询每只基金在 since_date 之后的已有净值日期集合 → {code: {date, ...}}

    用于精确性补抓：找出缺失的日期区间，只抓缺失的部分。
    分块执行以规避 SQLite 变量数上限。
    """
    out: dict[str, set] = {}
    codes = [c for c in codes if c]
    if not codes:
        return out
    try:
        import db as _db
        conn = _db.get_conn()
    except Exception:
        return out
    try:
        for i in range(0, len(codes), 900):
            part = codes[i:i + 900]
            marks = ",".join("?" for _ in part)
            rows = conn.execute(
                f"SELECT code, date FROM nav_history "
                f"WHERE code IN ({marks}) AND date >= ? AND dwjz IS NOT NULL",
                (*part, since_date),
            ).fetchall()
            for r in rows:
                c = r["code"] if hasattr(r, "keys") else r[0]
                d = r["date"] if hasattr(r, "keys") else r[1]
                if c and d:
                    if c not in out:
                        out[c] = set()
                    out[c].add(d)
    except Exception:
        return {}
    return out


def _find_missing_ranges(existing_dates: set, target_start: str, target_end: str) -> list[tuple[str, str]]:
    """v2.9.54: 找出目标日期范围内缺失的日期，合并为连续区间

    Args:
        existing_dates: 已有的净值日期集合
        target_start: 目标范围起始日期
        target_end: 目标范围结束日期

    Returns:
        [(start_date, end_date), ...] 缺失的连续区间列表（按日期升序）
    """
    from datetime import datetime, timedelta

    try:
        start_dt = datetime.strptime(target_start, '%Y-%m-%d')
        end_dt = datetime.strptime(target_end, '%Y-%m-%d')
    except Exception:
        return [(target_start, target_end)]

    # 生成目标范围内的所有日期（只考虑工作日，因为基金净值只有工作日有）
    target_dates = []
    current = start_dt
    while current <= end_dt:
        # 只考虑周一到周五（0=周一, 4=周五）
        if current.weekday() < 5:
            target_dates.append(current.strftime('%Y-%m-%d'))
        current += timedelta(days=1)

    # 找出缺失的日期
    missing_dates = [d for d in target_dates if d not in existing_dates]

    if not missing_dates:
        return []

    # 合并连续的缺失日期为区间
    ranges = []
    range_start = missing_dates[0]
    range_end = missing_dates[0]

    for i in range(1, len(missing_dates)):
        current_dt = datetime.strptime(missing_dates[i], '%Y-%m-%d')
        prev_dt = datetime.strptime(missing_dates[i - 1], '%Y-%m-%d')
        # 如果当前日期与前一个日期相差<=3天（跨周末），视为连续
        if (current_dt - prev_dt).days <= 3:
            range_end = missing_dates[i]
        else:
            ranges.append((range_start, range_end))
            range_start = missing_dates[i]
            range_end = missing_dates[i]

    ranges.append((range_start, range_end))
    return ranges


def _fetch_backfill_targeted(code: str, existing_dates: set, 
                              target_start: str, target_end: str,
                              max_pages: int = 20) -> list[dict]:
    """v2.9.54: 针对性抓取缺失区间（精确性补抓）

    找出目标范围内缺失的日期区间，逐个区间调用 range API 抓取。

    Args:
        code: 基金代码
        existing_dates: 已有的净值日期集合
        target_start: 目标范围起始日期
        target_end: 目标范围结束日期
        max_pages: 每个区间的最大翻页数

    Returns:
        [{date, ljjz, dwjz, jzzzl}, ...] 抓取到的净值记录
    """
    missing_ranges = _find_missing_ranges(existing_dates, target_start, target_end)

    if not missing_ranges:
        return []

    all_items = []
    for start_date, end_date in missing_ranges:
        try:
            items = fetch_nav_range(code, start_date, end_date, max_pages=max_pages)
            if items:
                all_items.extend(items)
        except Exception:
            # 单个区间抓取失败不影响其他区间
            continue

    return all_items


def nav_is_healthy(code: str, target_date: str) -> bool:
    """净值缓存是否「可用」:最新日期对上 **且** 近期点位密度正常。

    只看 cached_nav_max 是不够的 —— 016450 这类基金每周五仍会写入一个 dwjz,
    于是 MAX(date WHERE dwjz NOT NULL) 恒等于目标日期, 增量逻辑判定「缓存已最新」
    永久跳过它, 而它的序列实际只有 1/5 密度。_ret(navs,1) 在这种序列上算出的
    是跨 5 个交易日的涨幅, 于是 +2.16% 被显示成 +3.36% 并错误登顶当日榜。
    所以缓存判定必须同时看「最新日期」和「密度」两个条件。
    """
    if cached_nav_max(code) != target_date:
        return False
    try:
        import datetime as _dt2
        since = (_dt2.date.fromisoformat(target_date)
                 - _dt2.timedelta(days=30)).isoformat()
    except Exception:
        return True
    return nav_recent_points(code, since) >= MIN_RECENT_NAV_POINTS


def fetch_nav_incremental(codes: list[str], target_date: str,
                          page_size: int = 60, workers: int = 12,
                          cancel_flag=None, on_progress=None,
                          min_history_days: int = 0) -> dict:
    """v0.43.0: 增量净值抓取 — 对每只基金判断缓存最新日期, 仅对缺失日期区间的基金发起抓取。

    v2.9.54: 增加最近一年完整性检查，区分两种抓取类型：
      - 增量型：最新日期 < 目标日期，只抓最新缺口
      - 补全型：最近一年数据不足（<200天），重抓最近一年完整数据（page_size=250）
    v2.11.6: 双模式——每日增量(min_history_days=0)不查历史深度且缓存已最新
      的基金一律跳过（防全量风暴）；首跑补全(nav_history<NAV_FIRST_RUN_MIN_ROWS)
      才启用历史深度检查。

    Args:
        codes: 待处理的基金代码列表
        target_date: 目标日期(YYYY-MM-DD), =effective_nav_date() 输出
        page_size: 抓取窗口(每只基金返回多少条近 N 个交易日数据)
        workers: 并发线程数
        cancel_flag: 取消信号(线程 Event)
        on_progress: fn(done, total, code, skipped) 进度回调
        min_history_days: v2.9.14新增，最低历史净值天数要求。
                          部署时第一次运行传入250，历史数据不足的基金会触发补全。
                          缺口>1000只时自动切换到多数据源并行模式。

    Returns:
        {
          'fetched': [{code, items: [{date, ljjz, dwjz}]}],   # 本次新抓的基金(其缓存最新 < target_date)
          'skipped': [{code, cached_max}],                    # 跳过未抓的基金(缓存最新 == target_date)
          'errors':   [code, ...]                             # 抓取失败的基金
        }
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import threading

    # 1) 先按基金维度筛: 缓存"健康"(最新日期对上 + 近30天点位密度达标 + 历史天数达标) → 跳过
    fetch_list = []
    skip_list = []
    err_codes = []
    try:
        import datetime as _dt3
        _since = (_dt3.date.fromisoformat(target_date)
                  - _dt3.timedelta(days=30)).isoformat()
    except Exception:
        _since = target_date
    # v2.11.6: 双模式——每日增量(min_history_days=0)只看最新日期+密度，
    # 不查历史深度；首跑补全(min_history_days=NAV_MIN_TOTAL_DAYS)才启用
    # 总天数+近一年完整性检查（与 365 自然日清洗窗口自洽）。
    _min_yearly = NAV_MIN_YEARLY_DAYS if min_history_days > 0 else 0
    _health = _batch_nav_health(codes, target_date, _since, 
                                 min_history_days=min_history_days,
                                 min_yearly_days=_min_yearly)
    for c in codes:
        if _health.get(c):
            skip_list.append({"code": c, "cached_max": target_date})
        else:
            fetch_list.append(c)
    
    # v2.9.54: 统计增量型和补全型基金数量
    _incremental_count = 0
    _backfill_count = 0
    _cmax_all = nav_cached_max_many(fetch_list)
    # v2.11.6 防全量风暴闸门：缓存已是最新(cm>=target_date)的基金从待抓列表剔除。
    # 此前它们被归为"补全型"每日全量重抓，但数据已是数据源所能给出的全部，
    # 重抓只会原样写回（货基/周净值/次新基金每日重抓的病根）。
    # 真缺口监控由 data_quality 的日期连续性检查兜底。
    _current_skipped = []
    for c in fetch_list:
        cm = _cmax_all.get(c)
        if cm is not None and cm >= target_date:
            _current_skipped.append({"code": c, "cached_max": cm})
        elif cm is None:
            _backfill_count += 1  # 首抓，需要补全
        else:
            _incremental_count += 1  # 只需要增量抓取最新缺口
    if _current_skipped:
        skip_list.extend(_current_skipped)
        _skipset = {s["code"] for s in _current_skipped}
        fetch_list = [c for c in fetch_list if c not in _skipset]
    
    print(f"[净值抓取] 待抓{len(fetch_list)}只：增量型{_incremental_count}只，补全型{_backfill_count}只，跳过{len(skip_list)}只")
    
    if on_progress and (skip_list or fetch_list):
        try:
            on_progress(0, len(codes), "", skipped=len(skip_list))
        except Exception:
            pass

    if not fetch_list:
        return {"fetched": [], "skipped": skip_list, "errors": []}

    # 2) v2.9.14: 判断是否使用多数据源并行模式
    # 缺口>1000只时，使用三源并行（同花顺+pingzhong+天天基金），加速3倍
    MULTI_SOURCE_THRESHOLD = 1000
    # v2.11.6: 由真缺口规模决定（离线多日/大面积失败场景仍能多源加速），
    # 不再绑定 min_history_days（每日增量恒为0，旧条件使多源模式永不触发）
    use_multi_source = len(fetch_list) > MULTI_SOURCE_THRESHOLD

    if use_multi_source:
        print(f"[净值抓取] 缺口{len(fetch_list)}只 > {MULTI_SOURCE_THRESHOLD}，触发多数据源并行模式")
        return _fetch_multi_source_parallel(
            fetch_list, target_date, page_size, skip_list,
            cancel_flag, on_progress, min_history_days
        )

    # 3) 单数据源模式（原有逻辑）
    # v2.9.10: 使用同花顺作为主源时，自动降低并发数和增加请求间隔，避免限流
    _use_hithink = False
    try:
        from config import settings
        _use_hithink = getattr(settings, "use_hithink_as_primary", True)
    except Exception:
        pass

    if _use_hithink:
        workers = min(workers, 5)  # 同花顺限流，并发不超过5
        _request_interval = 0.2    # 请求间隔0.2秒
        print(f"[净值抓取] 使用同花顺主源，调整并发数为{workers}，请求间隔{_request_interval}秒")
    else:
        _request_interval = 0.08

    lock = threading.Lock()
    fetched = []
    _done = [0]
    _total = len(fetch_list)

    # 2.5) 决定每只基金的抓取策略(v1.1.4: 按缺口区间抓, 不再无脑 60 条):
    # v2.9.10: 优先使用同花顺官方API作为主源，失败自动降级到天天基金网
    _cmax = nav_cached_max_many(fetch_list)
    _GAP_MAX_PAGES = 2
    
    # v2.9.54: 批量查询所有待抓基金的已有日期（用于精确性补抓）
    _verbose_log = True
    try:
        import datetime as _dt_y
        _yearly_start_for_query = (_dt_y.date.fromisoformat(target_date)
                                   - _dt_y.timedelta(days=365)).isoformat()
        _existing_dates_all = _get_existing_dates_many(fetch_list, _yearly_start_for_query)
        print(f"[净值抓取] 已查询{len(_existing_dates_all)}只基金的已有日期（用于精确性补抓）")
    except Exception as _e:
        print(f"[净值抓取] 查询已有日期失败（将使用全量重抓）: {_e}")
        _existing_dates_all = {}

    def _fetch_with_hithink(code, cm):
        """优先使用同花顺API获取净值，失败返回None触发降级
        
        v2.9.54: 补全型使用精确性补抓（针对性抓取缺失区间），混合策略：
          - 缺失超过50% → 直接重抓全部
          - 缺失<=50% → 针对性抓取缺失区间
        """
        try:
            from collector.hithink_integration import get_fund_nav_items, get_fund_nav_range_items
            import datetime as _dtx
            
            if cm is None or cm >= target_date:
                # 补全型：首抓 或 最新日期已达标但最近一年数据不足
                _yearly_start = (_dtx.date.fromisoformat(target_date)
                                - _dtx.timedelta(days=365)).isoformat()
                
                # 查询已有日期，计算缺失比例
                _existing = _existing_dates_all.get(code, set())
                _target_days = 250  # 约1年交易日
                _missing_days = max(0, _target_days - len(_existing))
                _missing_ratio = _missing_days / _target_days if _target_days > 0 else 1.0
                
                if _missing_ratio > 0.5 or len(_existing) == 0:
                    # 缺失超过50% 或 首抓 → 直接重抓全部
                    _effective_page_size = max(page_size, 250)
                    items = get_fund_nav_items(code, days=_effective_page_size)
                    _fetch_type = "backfill_full"
                else:
                    # 缺失<=50% → 针对性抓取缺失区间
                    items = _fetch_backfill_targeted(code, _existing, _yearly_start, target_date)
                    _fetch_type = "backfill_targeted"
                
                if _verbose_log:
                    print(f"[补抓] {code}: 类型={_fetch_type}, 已有{len(_existing)}天, "
                          f"缺失{_missing_days}天({_missing_ratio*100:.0f}%), 抓到{len(items or [])}条")
            else:
                # 增量型: 只抓最新缺口
                sd = (_dtx.date.fromisoformat(cm)
                      + _dtx.timedelta(days=1)).isoformat()
                items = get_fund_nav_range_items(code, sd, target_date)
            
            if items:
                return items
        except Exception as e:
            logger.debug(f"同花顺净值抓取失败 {code}: {e}，降级到天天基金网")
        return None

    def _fetch_with_pingzhong(code, cm):
        """v2.11.4 数据源策略: pingzhongdata 主源(测速最快 203ms + 一次返全史 2314 点)。

        - 补全型(首抓 或 近一年数据不足, cm is None / cm>=target_date): 用 pingzhong 抓全史最优,
          一次调用即覆盖 250 天窗口, 省去天天基金翻页凑数。
        - 增量型(cm < target_date, 仅缺最近几天): 返回 None 交回 work() 走天天基金
          fetch_nav_range 精确补缺口 —— pingzhong 返全史对几天缺口是带宽浪费。
        - pingzhong 失败/空: 返回 None 触发 work() 的天天基金降级分支。
        """
        # 增量型: 交给天天基金 range 精确补缺口
        if cm is not None and cm < target_date:
            return None
        try:
            from modules.fund.pingzhong import fetch_pingzhong
            result = fetch_pingzhong(code, need={"navs"})
            navs = result.get("navs", []) if result else []
            if navs:
                items = [{"date": x["date"], "ljjz": x.get("ljjz"), "dwjz": x.get("dwjz")}
                         for x in navs if x.get("date") and x.get("dwjz")]
                if items:
                    if _verbose_log:
                        print(f"[pingzhong] {code}: 补全型抓到 {len(items)} 条(全史裁剪)")
                    return items
        except Exception as e:
            logger.debug(f"pingzhong净值抓取失败 {code}: {e}，降级到天天基金网")
        return None

    def work(code):
        cm = _cmax.get(code)
        items = None
        try:
            # v2.11.4: 主源改为 pingzhongdata(补全型全史/增量型转天天range), 失败降级天天基金
            items = _fetch_with_pingzhong(code, cm)
            if items is None:
                # 降级到天天基金网
                # v2.9.54: 补全型使用精确性补抓（混合策略）
                import datetime as _dtx2
                if cm is None or cm >= target_date:
                    # 补全型
                    _yearly_start = (_dtx2.date.fromisoformat(target_date)
                                    - _dtx2.timedelta(days=365)).isoformat()
                    
                    # 查询已有日期，计算缺失比例
                    _existing = _existing_dates_all.get(code, set())
                    _target_days = 250
                    _missing_days = max(0, _target_days - len(_existing))
                    _missing_ratio = _missing_days / _target_days if _target_days > 0 else 1.0
                    
                    if _missing_ratio > 0.5 or len(_existing) == 0:
                        # 缺失超过50% 或 首抓 → 直接重抓全部
                        _effective_page_size = max(page_size, 250)
                        items = fetch_nav_history(code, page_size=_effective_page_size)
                    else:
                        # 缺失<=50% → 针对性抓取缺失区间
                        items = _fetch_backfill_targeted(code, _existing, _yearly_start, target_date)
                else:
                    # 增量型: 只抓最新缺口
                    sd = (_dtx2.date.fromisoformat(cm)
                          + _dtx2.timedelta(days=1)).isoformat()
                    items = fetch_nav_range(code, sd, target_date,
                                            max_pages=_GAP_MAX_PAGES)
        except Exception:
            items = []
        with lock:
            if not items and cm is not None:
                # v1.1.4: 缺口抓取空返回 ≠ 失败 —— 目标日净值尚未发布(QDII T+2、
                # 慢发布、停牌)时, 库内缓存就是当前最新可得数据。归入 skipped 让
                # 合并阶段直接读库算指标, 避免「无净值数据」假报错
                skip_list.append({"code": code, "cached_max": cm})
            else:
                fetched.append({"code": code, "items": items})
            _done[0] += 1
            if on_progress:
                try:
                    on_progress(_done[0], _total, code, skipped=len(skip_list))
                except Exception:
                    pass
        time.sleep(_request_interval)

    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(work, c): c for c in fetch_list}
            for fut in as_completed(futs):
                if cancel_flag is not None and getattr(cancel_flag, "is_set", lambda: False)():
                    for f in futs:
                        f.cancel()
                    break
                exc = fut.exception()
                if exc:
                    code = futs[fut]
                    err_codes.append(code)
    except Exception:
        pass

    return {"fetched": fetched, "skipped": skip_list, "errors": err_codes}


def _fetch_multi_source_parallel(fetch_list: list[str], target_date: str,
                                  page_size: int, skip_list: list,
                                  cancel_flag, on_progress,
                                  min_history_days: int) -> dict:
    """v2.9.14: 多数据源并行抓取

    把基金分成3组，每组用不同的数据源同时抓取，加速3倍。
    抓取后做数据质量判断和补全。

    Args:
        fetch_list: 需要抓取的基金代码列表
        target_date: 目标日期
        page_size: 抓取窗口
        skip_list: 已跳过的基金列表
        cancel_flag: 取消信号
        on_progress: 进度回调
        min_history_days: 最低历史天数要求

    Returns:
        同 fetch_nav_incremental
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import threading
    from modules.nav.multi_source_fetcher import fetch_parallel, QUALITY_FULL

    fetched = []
    err_codes = []
    _done = [0]
    _total = len(fetch_list)
    lock = threading.Lock()

    # v2.9.54: 使用多数据源并行抓取，确保days至少250天（覆盖最近一年）
    days = max(page_size, min_history_days, QUALITY_FULL, 250)
    result = fetch_parallel(
        fetch_list,
        days=days,
        use_multi_source=True,
        on_progress=lambda done, total, code, status: (
            on_progress(done, total, code, skipped=len(skip_list)) if on_progress else None
        )
    )

    # 处理结果
    for code, data in result["results"].items():
        items = data.get("items", [])
        status = data.get("status", "")
        if items:
            fetched.append({"code": code, "items": items})
        else:
            err_codes.append(code)
            logger.warning(f"多源抓取失败 {code}: {status}")

    return {"fetched": fetched, "skipped": skip_list, "errors": err_codes}


def merge_navs_to_db(fetched: list, log=None) -> int:
    """v0.43.0: 把增量抓取结果按 (code, date) upsert 到 nav_history。

    与 pipeline.run_collector 阶段5 的 bulk_upsert_navs 同口径, 不覆盖已有数据。
    v2.5.0(perf): 循环内 commit=False, 结束后统一 commit。
    """
    import db as _db
    total = 0
    for it in fetched:
        code = it["code"]
        items = it.get("items") or []
        if not items:
            continue
        rows = []
        for x in items:
            d = x.get("date")
            lj = x.get("ljjz")
            if not d or not lj:
                continue
            rows.append({"date": d, "ljjz": lj, "dwjz": x.get("dwjz")})
        if rows:
            try:
                _db.bulk_upsert_navs(code, rows, commit=False)
                total += len(rows)
                if log:
                    log(f"增量入库 {code} +{len(rows)} 条")
            except Exception as e:
                if log:
                    log(f"增量入库失败 {code}: {e}")
    _db.get_conn().commit()
    return total


def fetch_nav_history(code: str, page_index: int = 1, page_size: int = 60, max_pages: int = 15,
                      start_date: Optional[str] = None, end_date: Optional[str] = None) -> list[dict]:
    """分页拉取历史净值(接口每页实际返回 20 条,自动翻页凑够 page_size)。
    返回 [{date, ljjz, dwjz, jzzzl}],按日期升序。

    start_date / end_date (YYYY-MM-DD):
        传入后只请求该闭区间内的净值。接口原生支持这两个参数,此前这里被硬编码为空串,
        导致哪怕只想补最近 1 天也必须从第 1 页开始一页页翻完全量历史(20 条/页),
        既慢又白白消耗额度。补数场景应当显式传区间。
    """
    real_size = 20  # 天天基金单页固定 20 条
    need_pages = max(1, -(-page_size // real_size))
    need_pages = min(need_pages, max_pages)
    sd = (start_date or "").strip()
    ed = (end_date or "").strip()
    seen = {}
    for pg in range(page_index, page_index + need_pages):
        url = (f"https://api.fund.eastmoney.com/f10/lsjz?fundCode={code}"
               f"&pageIndex={pg}&pageSize={real_size}&startDate={sd}&endDate={ed}")
        txt = _get(url, referer="http://fundf10.eastmoney.com/")
        if not txt:
            break
        try:
            data = json.loads(txt)
        except Exception:
            break
        if data.get("ErrCode") != 0:
            break
        lst = data.get("Data", {}).get("LSJZList", [])
        if not lst:
            break
        for it in lst:
            date = it.get("FSRQ")
            if not date:
                continue
            # 接口按日期倒序返回;一旦早于区间下界即可停止翻页
            if sd and date < sd:
                break
            if ed and date > ed:
                continue
            ljjz = float(it.get("LJJZ") or 0) or float(it.get("DWJZ") or 0)
            dwjz = float(it.get("DWJZ") or 0) if it.get("DWJZ") else None
            seen[date] = {
                "date": date,
                "ljjz": ljjz,
                "dwjz": dwjz,
                "jzzzl": float(it.get("JZZZL") or 0) if it.get("JZZZL") else None,
            }
        # 本页已触及区间下界 -> 后续页只会更早,无需再翻
        if sd and min((it.get("FSRQ") or "") for it in lst) < sd:
            break
        # 全市场模式已被 fetch_manager 用 ThreadPoolExecutor 并发, 页间隔从 0.25s 降到 0.08s
        time.sleep(0.08)
    items = list(seen.values())
    items.sort(key=lambda x: x["date"])
    return items


def fetch_nav_range(code: str, start_date: str, end_date: Optional[str] = None,
                    max_pages: int = 20) -> list[dict]:
    """只拉 [start_date, end_date] 区间内的净值,按日期升序。

    与 fetch_nav_history 的区别:这里以**区间**为准而非以"条数"为准,page_size 给到
    接口上限(max_pages * 20 条)以保证区间被完整覆盖,并在翻出下界时提前收尾。
    补数 / 定点重算涨幅时应优先用它,避免为 1 天的数据翻完几百页历史。
    """
    return fetch_nav_history(code, page_size=max_pages * 20, max_pages=max_pages,
                             start_date=start_date, end_date=end_date)


def nav_pairs(items, min_pts: int = 8) -> list:
    """从净值行列表提取 (date, 单位净值 dwjz) 升序对。

    v1.1.5: 弹窗/详情层统一改按**单位净值**口径计算指标(与榜单 pipeline 一致):
    - 分红/拆分基金(全市场 822 只)的 ljjz(累计净值)会高出 dwjz 数倍, 用它算
      d1/回撤/波动会与东方财富/榜单口径严重偏离(实测 161725: 0.26% vs 真值 1.06%);
    - dwjz 过滤后不足 min_pts 时整体回退 ljjz(历史脏数据兜底), 不混用两种口径
      (混用会在切换点制造假跳变)。
    """
    pairs = [(x.get("date"), x.get("dwjz")) for x in items
             if x.get("date") and x.get("dwjz") is not None]
    if len(pairs) < min_pts:
        pairs = [(x.get("date"), x.get("ljjz")) for x in items
                 if x.get("date") and x.get("ljjz")]
    return pairs


def fetch_nav_many(codes: list[str], page_size: int = 60, workers: int = 16,
                   cancel_flag: object = None, on_progress=None) -> dict:
    """并行抓取多只基金净值,返回 {code: [{date,ljjz,dwjz}]}。
    带全局限速(每请求间隔 ≥ 0.08s)避免触发接口频率限制。
    cancel_flag: 可选 threading.Event, 置位后尽快中止剩余抓取。
    on_progress: 可选 fn(done, total, code) 用于实时进度回报。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import threading

    lock = threading.Lock()
    results = {}
    _done = [0]
    _total = len(codes)

    def work(code):
        items = fetch_nav_history(code, page_size=page_size)
        with lock:
            results[code] = items
            _done[0] += 1
            if on_progress:
                try:
                    on_progress(_done[0], _total, code)
                except Exception:
                    pass
        time.sleep(0.08)
        return code

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(work, c) for c in codes]
        for fut in as_completed(futures):
            if cancel_flag is not None and cancel_flag.is_set():
                # 收到停止信号: 取消未完成的任务, 尽快返回
                for f in futures:
                    f.cancel()
                break
            try:
                fut.result()
            except Exception:
                pass
    return results
