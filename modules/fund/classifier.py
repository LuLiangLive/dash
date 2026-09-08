"""
modules/fund/classifier.py —— 基金类型判断

从 collector.fetcher 迁移（v2.5.5架构重构 - 阶段1）
负责：债券/货币类基金判断、固收类判断、C类份额判断
"""
from __future__ import annotations

# 债券/货币类特征(权益看板剔除;不能含"宝"(宝盈/华宝)、"利"等误伤词)
BOND_FUND_KWS = ("债", "货币", "同业存单", "固收", "偏债", "理财", "现金")

# 固收类基金类型（按ftype字段剔除）
FIXED_INCOME_TYPES = (
    "混合型-偏债",
    "混合型-绝对收益",
    "债券型",
    "债券型-长债",
    "债券型-中短债",
    "债券型-混合债",
    "债券型-可转债",
    "货币型",
    "理财型",
)


def is_bond_fund(name: str) -> bool:
    """债券/货币类基金:中债/国开债/政金债/信用债/货币/同业存单/固收等。"""
    return any(k in (name or "") for k in BOND_FUND_KWS)


def is_fixed_income_type(ftype: str) -> bool:
    """根据基金类型判断是否为固收类。"""
    if not ftype:
        return False
    return any(ft in ftype for ft in FIXED_INCOME_TYPES)


def is_c_share(name: str) -> bool:
    """C 类份额且非债券/货币类: 名称以 C 结尾(覆盖 ETF联接C / 指数C / 混合C / 股票C / QDII C 等)。"""
    n = (name or "").strip()
    return bool(n) and n.endswith("C") and not is_bond_fund(n)


# 兼容旧名称（带下划线前缀）
_is_bond_fund = is_bond_fund
_is_fixed_income_type = is_fixed_income_type
_is_c_share = is_c_share
