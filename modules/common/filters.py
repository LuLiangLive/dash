"""
modules/common/filters.py —— 基金类型过滤器(各 sub 榜单用)

从 collector.filters 迁移（v2.5.5架构重构）
集中管理各 sub 面板用到的基金类型过滤器
"""


def is_index_like(f: dict) -> bool:
    """ETF/联接/含'指数'字样 → 被动管理产品.

    用于:
      - rank/ETF 面板"选中"
      - rank/{强趋势,稳涨} 面板"排除"
    """
    n = (f.get("name") if isinstance(f, dict) else "") or ""
    return bool(f.get("is_etf")) or "指数" in n or "联接" in n or "ETF" in n


def is_bond_like(f: dict) -> bool:
    """债/货币/固收+/纯债/中短债 类 → 主动权益榜单排除."""
    n = (f.get("name") if isinstance(f, dict) else "") or ""
    return any(k in n for k in ("债", "货币", "同业存单", "固收", "偏债",
                                 "理财", "现金", "纯债", "中短债"))


def is_holding_period(f: dict) -> bool:
    """持有期/定开/封闭基金 → 有持有期限制,不灵活,全榜单排除.

    名称包含: 持有(3个月/6个月/1年等)、定开、封闭
    v2.9.8新增: 用户要求剔除长期持有类基金
    """
    n = (f.get("name") if isinstance(f, dict) else "") or ""
    return any(k in n for k in ("持有", "定开", "封闭"))


def is_equity_excluded(f: dict) -> bool:
    """被排除在主动权益榜单外: 被动产品 + 债券类 + 持有期基金.

    等价于: is_index_like(f) OR is_bond_like(f) OR is_holding_period(f)
    用于: reco/抗跌榜单, reco/开仓榜单, rank/{强趋势,稳涨}
    v2.9.8: 新增持有期基金排除
    """
    return is_index_like(f) or is_bond_like(f) or is_holding_period(f)
