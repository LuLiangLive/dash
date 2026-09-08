"""
modules/fund/fetcher.py —— 基金基础信息抓取

从 collector.fetcher 迁移（v2.5.5架构重构 - 阶段1）
负责：基金名称获取、前十大持仓股票获取
"""
from __future__ import annotations

import json
import re

from collector.http_utils import _get


def fetch_fund_name(code: str) -> str | None:
    """从pingzhongdata获取基金名称。"""
    txt = _get(f"https://fund.eastmoney.com/pingzhongdata/{code}.js", referer=f"https://fund.eastmoney.com/{code}.html")
    if not txt:
        return None
    m = re.search(r'fS_name\s*=\s*"([^"]+)"', txt)
    return m.group(1) if m else None


def fetch_fund_stocks(code: str) -> list[dict]:
    """抓前十大持仓股票(名称占位,真实场景应接持仓接口)。"""
    txt = _get(f"https://fund.eastmoney.com/pingzhongdata/{code}.js", referer=f"https://fund.eastmoney.com/{code}.html")
    if not txt:
        return []
    m = re.search(r'var stockCodes=(\[[^\]]*\])', txt)
    if not m:
        return []
    try:
        codes = json.loads(m.group(1))
    except Exception:
        return []
    return [{"name": c, "pct": 0.0} for c in codes[:10]]
