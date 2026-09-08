"""
modules/nav/multi_source_fetcher.py —— 多数据源并行净值抓取

v2.9.14新增: 部署时全量补全场景下，多数据源并行抓取，加速3倍。
v2.11.4 数据源策略调整(基于15只样本测速):
  优先级: pingzhongdata(主, 203ms+一次返全史2314点) > 天天基金网(兜底, 430ms/60点)
  同花顺API 已禁用 —— hithink_client 依赖 requests 但 requirements.txt 未声明,
  运行时静默降级到天天基金(假源); 且即便修复 432ms 仍慢于 pingzhong, 无保留价值。

设计要点:
- 先判断缺口大小，缺口>1000只时触发多源并行
- pingzhong 主源抓全量，失败的按 SOURCE_PRIORITY 降级到天天基金兜底
- 抓取后做数据质量判断，不足的用其他数据源补全
- 统一输出格式: [{date, ljjz, dwjz}]，按日期升序
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

logger = logging.getLogger(__name__)

# 数据源配置
SOURCE_CONFIG = {
    "hithink": {
        "name": "同花顺API",
        "max_workers": 5,
        "interval": 0.2,
        # v2.11.4: 禁用 —— hithink_client 依赖 requests(未声明), 静默降级天天基金;
        # 测速 432ms 慢于 pingzhong 203ms, 无保留价值
        "enabled": False,
    },
    "pingzhong": {
        "name": "pingzhongdata",
        # v2.11.4: 升为主源, 并发 8→12 (pingzhong 单次 203ms 最快, 可承载更高并发)
        "max_workers": 12,
        "interval": 0.1,
        "enabled": True,
    },
    "eastmoney": {
        "name": "天天基金网",
        "max_workers": 8,
        "interval": 0.08,
        "enabled": True,
    },
}

# 数据质量阈值
QUALITY_FULL = 250      # 完整数据天数
QUALITY_MIN = 200       # 最低可接受天数
MULTI_SOURCE_THRESHOLD = 1000  # 触发多源并行的缺口阈值

# v2.11.4: 数据源优先级链 —— 主源 pingzhong, 失败降级天天基金兜底; 同花顺已禁用不入链
SOURCE_PRIORITY = ["pingzhong", "eastmoney"]


# ---------------------------------------------------------------------------
# 单数据源抓取函数
# ---------------------------------------------------------------------------

def _fetch_hithink(code: str, days: int = 250) -> list[dict]:
    """同花顺API抓取净值"""
    try:
        from collector.hithink_integration import get_fund_nav_items
        items = get_fund_nav_items(code, days=days)
        if items:
            return [{"date": x["date"], "ljjz": x.get("ljjz"), "dwjz": x.get("dwjz")}
                    for x in items if x.get("date") and x.get("dwjz")]
    except Exception as e:
        logger.debug(f"同花顺抓取失败 {code}: {e}")
    return []


def _fetch_pingzhong(code: str, days: int = 250) -> list[dict]:
    """pingzhongdata抓取净值"""
    try:
        from modules.fund.pingzhong import fetch_pingzhong
        result = fetch_pingzhong(code, need={"navs"})
        navs = result.get("navs", []) if result else []
        if navs:
            # 只取最近days天
            if len(navs) > days:
                navs = navs[-days:]
            return [{"date": x["date"], "ljjz": x.get("ljjz"), "dwjz": x.get("dwjz")}
                    for x in navs if x.get("date") and x.get("dwjz")]
    except Exception as e:
        logger.debug(f"pingzhong抓取失败 {code}: {e}")
    return []


def _fetch_eastmoney(code: str, days: int = 250) -> list[dict]:
    """天天基金网抓取净值"""
    try:
        from modules.nav.fetcher import fetch_nav_history
        max_pages = max(1, -(-days // 20))  # 每页20条
        items = fetch_nav_history(code, page_size=days, max_pages=max_pages)
        if items:
            return [{"date": x["date"], "ljjz": x.get("ljjz"), "dwjz": x.get("dwjz")}
                    for x in items if x.get("date") and x.get("dwjz")]
    except Exception as e:
        logger.debug(f"天天基金网抓取失败 {code}: {e}")
    return []


# 数据源映射
FETCHERS = {
    "hithink": _fetch_hithink,
    "pingzhong": _fetch_pingzhong,
    "eastmoney": _fetch_eastmoney,
}


# ---------------------------------------------------------------------------
# 数据准确性验证
# ---------------------------------------------------------------------------

def detect_anomalies(items: list[dict]) -> list[str]:
    """异常值检测

    Returns:
        异常信息列表
    """
    anomalies = []
    if not items or len(items) < 2:
        return anomalies

    # 1. 检查净值是否为0或负数
    for i, item in enumerate(items):
        dwjz = item.get("dwjz")
        if dwjz is None or dwjz <= 0:
            anomalies.append(f"第{i}天净值异常: {dwjz}")

    # 2. 检查单日涨跌幅是否超过±10%
    for i in range(1, len(items)):
        prev = items[i-1].get("dwjz")
        curr = items[i].get("dwjz")
        if prev and curr and prev > 0:
            change = (curr / prev - 1) * 100
            if abs(change) > 10:
                anomalies.append(f"{items[i]['date']} 单日涨跌幅异常: {change:.2f}%")

    # 3. 检查连续多日净值不变（超过5天）
    if len(items) >= 6:
        same_count = 1
        for i in range(1, len(items)):
            if items[i].get("dwjz") == items[i-1].get("dwjz"):
                same_count += 1
                if same_count > 5:
                    anomalies.append(f"{items[i]['date']} 连续{same_count}天净值不变")
                    break
            else:
                same_count = 1

    return anomalies


def check_date_continuity(items: list[dict]) -> list[str]:
    """检查日期连续性

    Returns:
        缺失日期信息列表
    """
    import datetime as _dt
    issues = []
    if not items or len(items) < 2:
        return issues

    dates = [_dt.date.fromisoformat(item["date"]) for item in items if item.get("date")]
    if len(dates) < 2:
        return issues

    # 检查日期间隔是否合理（应该是1-4天，周末和节假日）
    for i in range(1, len(dates)):
        gap = (dates[i] - dates[i-1]).days
        if gap > 7:  # 超过7天可能有缺失
            issues.append(f"{dates[i-1]} 到 {dates[i]} 间隔{gap}天，可能有缺失")

    return issues


def cross_validate(code: str, items: list[dict], sources: list[str] = None) -> dict:
    """多数据源交叉验证

    Args:
        code: 基金代码
        items: 首选数据源的数据
        sources: 用于验证的数据源列表

    Returns:
        {
            "valid": bool,           # 是否通过验证
            "match_rate": float,     # 数据匹配率
            "differences": list,     # 差异点
            "best_source": str,      # 最佳数据源
            "best_items": list       # 最佳数据
        }
    """
    if sources is None:
        sources = ["pingzhong", "eastmoney"]

    if not items:
        return {"valid": False, "match_rate": 0, "differences": ["首选数据源为空"],
                "best_source": "unknown", "best_items": []}

    # 用其他数据源验证
    source_data = {}
    for source in sources:
        try:
            fetcher = FETCHERS.get(source)
            if fetcher:
                other_items = fetcher(code, days=len(items))
                if other_items:
                    source_data[source] = other_items
        except Exception:
            continue

    if not source_data:
        return {"valid": True, "match_rate": 1.0, "differences": [],
                "best_source": "primary", "best_items": items}

    # 对比数据
    best_items = items
    best_source = "primary"
    max_match = 0

    for source, other_items in source_data.items():
        # 计算匹配率（相同日期的净值差异在0.5%以内）
        match_count = 0
        total_count = 0
        item_map = {x["date"]: x.get("dwjz") for x in items}
        for other in other_items:
            date = other.get("date")
            if date in item_map and item_map[date] and other.get("dwjz"):
                total_count += 1
                diff = abs(other["dwjz"] - item_map[date]) / item_map[date] * 100
                if diff < 0.5:  # 差异在0.5%以内认为匹配
                    match_count += 1

        match_rate = match_count / total_count if total_count > 0 else 0
        if match_rate > max_match:
            max_match = match_rate
            # 如果其他数据源匹配率更高且数据更多，采用其他数据源
            if len(other_items) > len(items):
                best_items = other_items
                best_source = source

    valid = max_match >= 0.9  # 匹配率>=90%认为通过

    return {
        "valid": valid,
        "match_rate": max_match,
        "differences": [] if valid else [f"匹配率仅{max_match:.1%}"],
        "best_source": best_source,
        "best_items": best_items,
    }


# ---------------------------------------------------------------------------
# 数据质量判断
# ---------------------------------------------------------------------------

def validate_data_quality(code: str, items: list[dict], min_days: int = QUALITY_MIN,
                          cross_check: bool = False) -> tuple[list[dict], str]:
    """数据质量判断，不足的用其他数据源补全，可选交叉验证

    Returns:
        (items, status)
        status: "合格" / "补全后合格" / "基本合格" / "数据不足" / "异常"
    """
    if not items:
        return [], "数据为空"

    # 1. 异常值检测
    anomalies = detect_anomalies(items)
    if anomalies:
        logger.warning(f"数据异常 {code}: {anomalies[:3]}")

    # 2. 日期连续性检查
    date_issues = check_date_continuity(items)
    if date_issues:
        logger.debug(f"日期不连续 {code}: {date_issues[:3]}")

    # 3. 天数检查
    if len(items) >= QUALITY_FULL:
        # 4. 可选交叉验证
        if cross_check:
            result = cross_validate(code, items)
            if result["valid"]:
                return result["best_items"], f"合格（交叉验证通过，匹配率{result['match_rate']:.0%}）"
            else:
                return result["best_items"], f"合格（交叉验证匹配率{result['match_rate']:.0%}，已采用最佳源）"
        return items, "合格"

    # 数据不足，尝试用其他数据源补全
    original_count = len(items)
    for source_name, fetcher in FETCHERS.items():
        if len(items) >= QUALITY_FULL:
            break
        try:
            new_items = fetcher(code, days=QUALITY_FULL)
            if new_items and len(new_items) > len(items):
                items = new_items
        except Exception:
            continue

    if len(items) >= QUALITY_FULL:
        return items, f"补全后合格（原{original_count}天）"
    elif len(items) >= min_days:
        return items, f"基本合格（{len(items)}天）"
    else:
        return items, f"数据不足（仅{len(items)}天）"


# ---------------------------------------------------------------------------
# 单只基金抓取（带质量判断）
# ---------------------------------------------------------------------------

def fetch_one(code: str, days: int = 250, source: str = "pingzhong") -> tuple[list[dict], str, str]:
    """抓取单只基金净值，带数据质量判断、补全与多源降级。

    v2.11.4: 默认主源改为 pingzhong(最快+全史); 主源返回空时按 SOURCE_PRIORITY
    依次降级(天天基金兜底), 直到拿到非空数据。同花顺已禁用不入链。

    Args:
        code: 基金代码
        days: 需要的净值天数
        source: 首选数据源(默认 pingzhong)

    Returns:
        (items, status, used_source) —— used_source 为实际命中数据的源
    """
    # 构造降级链: 首选源在前, 其余按 SOURCE_PRIORITY 补全(去重, 跳过禁用源)
    chain = [source]
    for s in SOURCE_PRIORITY:
        if s not in chain and SOURCE_CONFIG.get(s, {}).get("enabled", False):
            chain.append(s)

    items = []
    used_source = source
    for src in chain:
        fetcher = FETCHERS.get(src)
        if not fetcher:
            continue
        try:
            items = fetcher(code, days=days)
        except Exception as e:
            logger.debug(f"[fetch_one] {src} 抓取异常 {code}: {e}")
            items = []
        if items:
            used_source = src
            break

    # 数据质量判断和补全
    items, status = validate_data_quality(code, items)
    return items, status, used_source


# ---------------------------------------------------------------------------
# 多数据源并行抓取
# ---------------------------------------------------------------------------

def fetch_parallel(codes: list[str], days: int = 250,
                   use_multi_source: bool = True,
                   on_progress=None) -> dict:
    """多数据源并行抓取

    Args:
        codes: 基金代码列表
        days: 需要的净值天数
        use_multi_source: 是否使用多数据源并行
        on_progress: 进度回调 fn(done, total, code, status)

    Returns:
        {
            "results": {code: {"items": [...], "status": "...", "source": "..."}},
            "stats": {"total": N, "success": N, "failed": N, "full": N, "partial": N}
        }
    """
    if not codes:
        return {"results": {}, "stats": {"total": 0, "success": 0, "failed": 0, "full": 0, "partial": 0}}

    results = {}
    stats = {"total": len(codes), "success": 0, "failed": 0, "full": 0, "partial": 0}
    done = [0]
    lock = __import__("threading").Lock()

    # v2.11.4: 统一 pingzhong 主源(fetch_one 内含 eastmoney 兜底), 废弃三源分组
    _primary = SOURCE_PRIORITY[0]
    _cfg = SOURCE_CONFIG[_primary]
    _interval = _cfg["interval"]
    _workers = _cfg["max_workers"]

    def _work(code: str):
        nonlocal results, stats
        try:
            items, status, used_source = fetch_one(code, days=days, source=_primary)
            with lock:
                results[code] = {"items": items, "status": status, "source": used_source}
                if items:
                    stats["success"] += 1
                    if "合格" in status:
                        stats["full"] += 1
                    else:
                        stats["partial"] += 1
                else:
                    stats["failed"] += 1
                done[0] += 1
                if on_progress:
                    try:
                        on_progress(done[0], len(codes), code, status)
                    except Exception:
                        pass
        except Exception as e:
            with lock:
                results[code] = {"items": [], "status": f"异常: {e}", "source": _primary}
                stats["failed"] += 1
                done[0] += 1
        # v2.11.4: interval 移入 worker(抓取后限速), 不再阻塞 as_completed 收集循环
        time.sleep(_interval)

    if use_multi_source and len(codes) > MULTI_SOURCE_THRESHOLD:
        logger.info(f"[多源抓取] 缺口{len(codes)}只 > {MULTI_SOURCE_THRESHOLD}，"
                    f"pingzhong主源+天天兜底，并发{_workers}")
    else:
        logger.info(f"[净值抓取] pingzhong主源+天天兜底，并发{_workers}，共{len(codes)}只")

    with ThreadPoolExecutor(max_workers=_workers) as ex:
        futures = [ex.submit(_work, c) for c in codes]
        for f in as_completed(futures):
            f.result()

    return {"results": results, "stats": stats}


# ---------------------------------------------------------------------------
# 缺口判断
# ---------------------------------------------------------------------------

def get_history_gap(codes: list[str], min_days: int = QUALITY_FULL) -> list[str]:
    """判断历史数据缺口：返回历史天数不足min_days的基金列表

    Args:
        codes: 基金代码列表
        min_days: 最低要求天数

    Returns:
        需要补全的基金代码列表
    """
    import db
    need_backfill = []
    for code in codes:
        try:
            count = db.get_conn().execute(
                "SELECT COUNT(*) as cnt FROM nav_history WHERE code=? AND dwjz IS NOT NULL",
                (code,)
            ).fetchone()["cnt"]
            if count < min_days:
                need_backfill.append(code)
        except Exception:
            need_backfill.append(code)
    return need_backfill


def should_use_multi_source(gap_count: int) -> bool:
    """判断是否应该使用多数据源并行"""
    return gap_count > MULTI_SOURCE_THRESHOLD
