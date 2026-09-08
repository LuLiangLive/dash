"""
services/fund_service.py —— 基金相关辅助函数

从 main.py 迁移的相对独立的基金相关辅助函数，包括：
- 动态加载采集模块
- 基金名称查询（含实时兜底）
- 基准指数名称和选择
- 从详情构造数据并写入

设计原则：
- 只包含相对独立、无复杂依赖的函数
- 复杂的计算函数（_series、_resist、_rise等）暂保留在 main.py
- 未来可逐步迁移更多函数
"""
from __future__ import annotations

import importlib
import re

import db

# ---------------------------------------------------------------------------
# 常量（从 main.py 迁移）
# ---------------------------------------------------------------------------

BENCH_NAMES = {
    "sh000300": "沪深300",
    "sh000905": "中证500",
    "sh000852": "中证1000",
    "sh000906": "中证800",
    "sh000001": "上证指数",
    "sz399001": "深证成指",
    "sz399006": "创业板指",
    "sh000688": "科创50",
}

DEFAULT_BENCH = "sh000906"  # 中证800


# ---------------------------------------------------------------------------
# 动态加载
# ---------------------------------------------------------------------------

_NFM = None


def load_nfm():
    """动态加载 analysis_pipeline/night_fund_monitor.py（线上版采集脚本）。"""
    global _NFM
    if _NFM is None:
        _NFM = importlib.import_module("analysis_pipeline.night_fund_monitor")
    return _NFM


# ---------------------------------------------------------------------------
# 基金名称
# ---------------------------------------------------------------------------

def name_of(code: str) -> str:
    """获取基金名称，数据库中无时实时从天天基金抓取并更新。"""
    f = db.get_fund(code)
    # 如果name是6位数字(代码),说明没有正确获取名称,需要重新获取
    if f and f.get("name") and not re.fullmatch(r"\d{6}", f.get("name")):
        return f["name"]
    # 数据库中无名称或name是代码时,实时从天天基金pingzhongdata接口抓取
    try:
        import httpx
        url = f"http://fund.eastmoney.com/pingzhongdata/{code}.js"
        resp = httpx.get(url, headers={"Referer": "http://fund.eastmoney.com/"}, timeout=10)
        text = resp.text
        m = re.search(r'fS_name\s*=\s*"([^"]+)"', text)
        if m:
            name = m.group(1)
            # 更新数据库
            try:
                conn = db.get_conn()
                conn.execute("UPDATE funds SET name = ? WHERE code = ?", (name, code))
                conn.commit()
            except Exception:
                pass
            return name
    except Exception:
        pass
    return code


# ---------------------------------------------------------------------------
# 基准指数
# ---------------------------------------------------------------------------

def bench_name(ick: str) -> str:
    """指数代码 → 简称（超额收益备注用）。"""
    return BENCH_NAMES.get(ick, ick)


def pick_bench(code: str) -> str:
    """v0.36: 超额收益全部采用中证800（不再按主题匹配自适应）。"""
    return DEFAULT_BENCH


# ---------------------------------------------------------------------------
# 数据写入
# ---------------------------------------------------------------------------

def upsert_fund_from_detail(d: dict) -> None:
    """从实时获取的基金详情构造数据并写入 funds 表。"""
    if not d or not d.get("code"):
        return
    data = {
        "code": d["code"],
        "name": d.get("name"),
        "nav": d.get("nav"),
        "nav_date": d.get("nav_date"),
        "d1": d.get("d1"),
        "d3": d.get("d3"),
        "d5": d.get("d5"),
        "d7": d.get("d7"),
        "d10": d.get("d10"),
        "m1": d.get("m1"),
        "m3": d.get("m3"),
        "score": d.get("score"),
        "ad_score": d.get("ad_score"),
        "earn_score": d.get("earn_score"),
    }
    # 只保留非空字段
    data = {k: v for k, v in data.items() if v is not None}
    if data:
        db.upsert_fund(data)


# ---------------------------------------------------------------------------
# 净值序列
# ---------------------------------------------------------------------------

def series(code: str, days: int = 42) -> tuple[list[str], list[float]]:
    """基金净值序列（升序）：优先 DB nav_history，不足时 nfm.fetch_nav 实时补拉。

    返回 (dates, vals)。
    v2026-08-28: 实时补拉后自动保存到DB，避免非Fund库基金重复抓取导致加载慢。
    """
    rows = db.get_nav(code, limit=120)
    rows = list(reversed(rows))  # 升序
    if len(rows) < 40:
        nfm = load_nfm()
        got = []
        for pg in (1, 2, 3):
            pn = nfm.fetch_nav(code, page=pg, size=25)
            if not pn:
                break
            got.extend(pn)
            if len(pn) < 20:
                break
        if len(got) > len(rows):
            rows = [{"date": x.get("date"), "ljjz": x.get("ljjz"), "dwjz": x.get("dwjz")} for x in reversed(got)]
            # v2026-08-28: 保存到DB,避免重复抓取
            try:
                db.bulk_upsert_navs(code, rows)
            except Exception:
                pass
    seen = {}
    for v in rows:
        d, x = v.get("date"), v.get("ljjz")
        if d and x and d not in seen:
            seen[d] = float(x)
    seq = sorted(seen.items())[-days:]
    return [d for d, _ in seq], [x for _, x in seq]


# ---------------------------------------------------------------------------
# 指数序列
# ---------------------------------------------------------------------------

def load_idx(days: int = 90) -> tuple[dict, list]:
    """从 nav_history 读三指数，构建 idx_map(date -> {sh,cyb,kc}) + idx_rows(上证序列)。

    v0.50.x 修复: 同时在 idx_map 里嵌入 `daily_ret` 字段(日期→ {sh,cyb,kc} 百分比)
    让 nfm.calc_metrics 可以计算 up_cap/dn_cap, 弹窗「上涨/下跌捕获率」字段不再 None。
    """
    series_data = {}
    for ick, key in (("sh000001", "sh"), ("sz399006", "cyb"), ("sh000688", "kc")):
        rows = db.get_nav(ick, limit=days)
        rows = list(reversed(rows))
        series_data[key] = {r["date"]: r["ljjz"] for r in rows}
    idx_map = {}
    for date in sorted(series_data.get("sh", {})):
        row = {}
        for key in ("sh", "cyb", "kc"):
            m = series_data.get(key, {})
            dts = sorted(m.keys())
            if date in m and date in dts and dts.index(date) > 0:
                prev = dts[dts.index(date) - 1]
                if m.get(prev):
                    row[key] = m[date] / m[prev] - 1
        if row.get("sh") is not None:
            idx_map[date] = row
    idx_rows = sorted(series_data.get("sh", {}).items())
    # 配套 daily_ret: [(date, scalar_pct)] — ranker.calc_metrics 期望的格式 (% 单位)
    # 这里用上证日收益作为捕获率基准, 也可换 sh000906(中证800), 但弹窗不强调超额, 用上证更稳定
    daily_ret = [(date, idx_map[date]["sh"] * 100) for date in sorted(idx_map.keys())]
    idx_map["daily_ret"] = daily_ret
    return idx_map, idx_rows


# ---------------------------------------------------------------------------
# 缓存（从 main.py 迁移）
# ---------------------------------------------------------------------------

# 长净值序列缓存 (v2026-08-29 性能优化: 弹窗加载慢治理)
LONG_SERIES_CACHE: dict = {}  # "code:days" -> (timestamp, [ljjz...])
LONG_SERIES_CACHE_TTL = 21600   # 6 小时(历史净值当日不变, 可长缓存)
LONG_SERIES_CACHE_MAX = 800

# 补拉冷却表: 新基金(成立不足 6 个月)客观上不存在 260 条净值, 补拉必然空手而归。
LONG_FETCH_COOLDOWN: dict = {}  # code -> timestamp
LONG_FETCH_COOLDOWN_TTL = 86400  # 24 小时

# 极端历史事件警示缓存 (v0.52.9: 避免重复查询数据库)
EXTREME_CACHE: dict = {}  # code -> result dict or None


# ---------------------------------------------------------------------------
# v2.9.44: 统一读取接口（根治字段漏读问题）
# ---------------------------------------------------------------------------

_FUNDS_COLUMNS = None


def _get_columns():
    """获取 funds 表字段列表（缓存）"""
    global _FUNDS_COLUMNS
    if _FUNDS_COLUMNS is None:
        try:
            conn = db.get_conn()
            _FUNDS_COLUMNS = [r[1] for r in conn.execute("PRAGMA table_info(funds)").fetchall()]
        except Exception:
            _FUNDS_COLUMNS = []
    return _FUNDS_COLUMNS


def get_fund_full_data(code):
    """获取单只基金的完整数据（SELECT *，保证字段完整性，杜绝漏读 d1/d2 等字段）。"""
    if not code:
        return None
    try:
        conn = db.get_conn()
        row = conn.execute("SELECT * FROM funds WHERE code=?", (code,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["themes"] = db._uj(d.get("themes")) if hasattr(db, '_uj') else d.get("themes")
        d["stocks"] = db._uj(d.get("stocks")) if hasattr(db, '_uj') else d.get("stocks")
        d["suggest"] = db._uj(d.get("suggest")) if hasattr(db, '_uj') else d.get("suggest")
        return d
    except Exception:
        return None


def get_fund_batch(codes):
    """批量获取基金完整数据（避免 N+1 查询），返回 {code: fund_dict}。"""
    if not codes:
        return {}
    result = {}
    try:
        conn = db.get_conn()
        batch_size = 500
        for i in range(0, len(codes), batch_size):
            batch = codes[i:i + batch_size]
            placeholders = ",".join("?" * len(batch))
            rows = conn.execute(
                f"SELECT * FROM funds WHERE code IN ({placeholders})",
                batch
            ).fetchall()
            for row in rows:
                d = dict(row)
                result[d["code"]] = d
    except Exception:
        pass
    return result


def get_fund_stats(code):
    """获取基金的统计指标字段（供弹窗/对比页使用），返回完整 funds 行字典。"""
    return get_fund_full_data(code)


def enrich_with_fund_details(items):
    """v2.9.44: 为自选分组/列表项丰富基金详情字段（统一从 funds 表读取，保证字段完整）。

    Args:
        items: [{"code": "001174", "group_id": 1, ...}, ...]

    Returns:
        丰富后的 items 列表，每个 item 合并了 funds 表的 name/nav/m1/ad_score 等字段
    """
    if not items:
        return items
    codes = [item.get("code") for item in items if item.get("code")]
    batch = get_fund_batch(codes)
    enriched = []
    for item in items:
        code = item.get("code")
        d = batch.get(code, {})
        merged = dict(item)
        # 合并基金详情字段（不覆盖 item 已有的字段）
        for k, v in d.items():
            if k not in merged or merged.get(k) is None:
                merged[k] = v
        enriched.append(merged)
    return enriched


def verify_field_completeness(code=None):
    """字段完整性校验：检查 funds 表所有字段是否都能通过统一接口读取。"""
    columns = _get_columns()
    if code is None:
        try:
            conn = db.get_conn()
            row = conn.execute("SELECT code FROM funds LIMIT 1").fetchone()
            code = row[0] if row else None
        except Exception:
            code = None
    if code is None:
        return {"total_fields": len(columns), "returned_fields": 0,
                "missing_fields": columns, "ok": False, "error": "无基金数据"}
    fund = get_fund_full_data(code)
    if fund is None:
        return {"total_fields": len(columns), "returned_fields": 0,
                "missing_fields": columns, "ok": False, "error": f"基金 {code} 不存在"}
    returned = set(fund.keys())
    expected = set(columns)
    missing = expected - returned
    return {
        "total_fields": len(columns),
        "returned_fields": len(returned),
        "missing_fields": sorted(missing),
        "ok": len(missing) == 0,
        "code": code,
    }
