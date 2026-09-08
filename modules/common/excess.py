"""
services/excess.py — 超额收益计算服务

从 main.py 拆分，负责基金相对基准指数的超额收益计算：
- pick_bench: 根据基金类型选择基准指数（中证800/沪深300/科创50）
- bench_series: 获取基准指数净值序列
- excess_rets: 计算多周期超额收益（d3/d7/d10/m1）

调用入口：excess_rets(dates, vals, bench_pts, window=42) -> {d3, d7, d10, m1}
"""
from __future__ import annotations

from typing import Optional

import db


# 基准指数代码映射
BENCH_MAP = {
    "sh000906": "中证800",
    "sh000300": "沪深300",
    "sh000688": "科创50",
    "sz399006": "创业板指",
    "sh000001": "上证指数",
}


def pick_bench(code: str) -> str:
    """
    根据基金代码/类型选择基准指数。
    默认中证800，科创主题选科创50，创业板主题选创业板指。
    """
    f = db.get_fund(code)
    if f:
        name = (f.get("name") or "").upper()
        sec = (f.get("sec") or "")
        # 科创主题
        if "科创" in name or "科创" in sec or code.startswith("588"):
            return "sh000688"
        # 创业板主题
        if "创业" in name or "创业" in sec or code.startswith("159"):
            return "sz399006"
        # 沪深300主题
        if "300" in name or "沪深300" in sec:
            return "sh000300"
    return "sh000906"  # 默认中证800


def bench_name(bench_code: str) -> str:
    """获取基准指数名称。"""
    return BENCH_MAP.get(bench_code, bench_code)


def bench_series(bench_code: str, days: int = 120):
    """
    获取基准指数净值序列。
    从nav_history表读取（指数数据也存在nav_history表中）。
    返回 [(date, close), ...] 列表，按日期升序。
    """
    rows = db.get_nav(bench_code, limit=days)
    rows = list(reversed(rows))
    if not rows:
        return []
    return [(r["date"], float(r["ljjz"])) for r in rows]


def _ret(pts: list, n: int) -> Optional[float]:
    """从价格序列计算n日收益率（百分比）。"""
    if len(pts) <= n or not pts[0] or not pts[-n - 1]:
        return None
    return (pts[-1] / pts[-n - 1] - 1) * 100


def excess_rets(fund_dates: list, fund_vals: list, bench_pts: list, window: int = 42) -> dict:
    """
    计算基金相对基准的多周期超额收益。

    Args:
        fund_dates: 基金日期列表
        fund_vals: 基金净值列表
        bench_pts: 基准指数 [(date, close), ...]
        window: 计算窗口（默认42天，约2个月）

    Returns:
        {d3, d7, d10, m1} 各周期超额收益（百分比）
    """
    if not fund_vals or not bench_pts or len(fund_vals) < 2:
        return {"d3": None, "d7": None, "d10": None, "m1": None}
    # 对齐日期：取基金和基准都有的日期
    bench_map = dict(bench_pts)
    aligned_fund = []
    aligned_bench = []
    for i, d in enumerate(fund_dates):
        if d in bench_map:
            aligned_fund.append(fund_vals[i])
            aligned_bench.append(bench_map[d])
    if len(aligned_fund) < 5:
        return {"d3": None, "d7": None, "d10": None, "m1": None}
    # 截取窗口
    if len(aligned_fund) > window:
        aligned_fund = aligned_fund[-window:]
        aligned_bench = aligned_bench[-window:]
    # 计算各周期超额收益
    result = {}
    for key, n in [("d3", 3), ("d7", 7), ("d10", 10), ("m1", 21)]:
        f_ret = _ret(aligned_fund, n)
        b_ret = _ret(aligned_bench, n)
        if f_ret is not None and b_ret is not None:
            result[key] = round(f_ret - b_ret, 2)
        else:
            result[key] = None
    return result
