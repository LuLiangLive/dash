"""
rank_config.py —— 榜单配置中心（Single Source of Truth）

所有榜单相关模块必须从这里读取榜单结构，禁止在各模块中硬编码榜单列表。

修改榜单结构只需改此文件，然后重启服务。

使用方式:
    from rank_config import RANK_CONFIG, get_panels, get_subs, get_top_n, is_deprecated

    panels = get_panels()           # ['day', 'reco']
    subs = get_subs('day')          # ['当日', '三日', '七日', '近2周', '综合']
    top_n = get_top_n('day', '当日') # 40
"""
from __future__ import annotations

from typing import Dict, List, Optional


# ============================================================================
# 榜单配置（唯一数据源）
# ============================================================================

RANK_CONFIG: Dict[str, dict] = {
    "day": {
        "name": "日榜",
        "icon": "📅",
        "top_n": 40,
        "subs": [
            {
                "key": "当日",
                "label": "当日",
                "desc": "按当日涨幅降序排列，取前40名",
            },
            {
                "key": "三日",
                "label": "三日",
                "desc": "按近三日涨幅降序排列，取前40名",
            },
            {
                "key": "七日",
                "label": "七日",
                "desc": "按近七日涨幅降序排列，取前40名",
            },
            {
                "key": "近2周",
                "label": "近2周",
                "desc": "按近2周（10个交易日）涨幅降序排列，取前40名",
            },
            {
                "key": "综合",
                "label": "综合",
                "desc": "取当日/三日/七日/近2周四榜前40并集，按上榜次数降序，同次按综合分降序",
            },
        ],
        "algo_desc": "当日/三日/七日/近2周按对应周期涨幅降序各取前40；综合榜取四榜前40并集，按上榜次数降序，同次按综合分降序",
    },
    "reco": {
        "name": "推荐榜单",
        "icon": "🎯",
        "top_n": 40,
        "subs": [
            {
                "key": "抗跌",
                "label": "抗跌",
                "desc": "综合分=收益分42.5%+抗跌分42.5%+卡玛比率百分位15%（线性评分，池内百分位），取前40名",
                "pool": "上榜基金池",
            },
            {
                "key": "自选",
                "label": "自选",
                "desc": "自选综合分=中长期趋势35%+近期表现25%+回撤控制15%+波动率16%+夏普比率9%，取前40名",
            },
            {
                "key": "质量",
                "label": "质量",
                "desc": "质量分=下行夏普30%+卡玛25%+盈利稳定性15%-下跌捕获15%+上涨捕获15%（全市场C类，min-max归一化），取前40名",
            },
            {
                "key": "超跌筑底",
                "label": "超跌筑底",
                "desc": "急跌+低位+筑底三阶段筛选，按反弹预期综合评分降序，取前40名（v2.9.1重构ETF榜）",
            },
        ],
        "algo_desc": "抗跌榜在上榜基金池内按V3线性评分（收益42.5%+抗跌42.5%+卡玛15%）排序；自选榜六维加权；质量榜全市场C类筛选；超跌筑底榜筛选急跌+低位+筑底基金按反弹预期评分排序",
    },
    "attack": {
        "name": "攻防榜",
        "icon": "⚔️",
        "top_n": 40,
        "subs": [
            {
                "key": "高弹性",
                "label": "高弹性",
                "desc": "β>1.3且上涨捕获率>130%，按上涨捕获率降序——趋势上涨行情中弹性最大的基金",
            },
            {
                "key": "攻守兼备",
                "label": "攻守兼备",
                "desc": "0.5<β<1.1且捕获率差>0且最大回撤<10%，按捕获率差降序——涨时跟涨跌时抗跌的基金",
            },
            {
                "key": "强抗跌",
                "label": "强抗跌",
                "desc": "下跌捕获率<70%且下行波动率<市场均值且最大回撤<5%，按下跌捕获率升序——下跌市中最抗跌的基金",
            },
            {
                "key": "反弹先锋",
                "label": "反弹先锋",
                "desc": "近20日跌幅>5%且近5日涨幅>0，按近5日涨幅降序——超跌后率先反弹的基金",
            },
        ],
        "algo_desc": "高弹性榜筛选β>1.3且上涨捕获率>130%的基金；攻守兼备榜筛选0.5<β<1.1且捕获率差(上涨-下跌)>0且最大回撤<10%的基金；强抗跌榜筛选下跌捕获率<70%且下行波动率低于市场均值且最大回撤<5%的基金；反弹先锋榜筛选近20日跌幅超5%且近5日转正的基金。捕获率基于上证指数，上涨日定义为大盘涨>0.5%，下跌日定义为大盘跌<-0.5%。",
    },
}


# ============================================================================
# 已删除的榜单（用于兼容性检查，遇到这些 panel/sub 应告警或忽略）
#
# v1.1.9: 补全 rank_snapshots 中真实存在、但既不在 RANK_CONFIG 也不在原废弃名单
# 里的 3 个残留榜单（reco/开仓、reco/综合推荐、layer/板块强度代表基金），
# 使「历史遗留榜单」清单与实际库内数据完全对齐，共 10 个。
#
# ⚠️ 新代码的过滤请用 is_valid_rank() 白名单，而不是这里的黑名单：
#    黑名单只能挡住已知的历史遗留，未知 panel/sub 仍会漏进来。
# ============================================================================

DEPRECATED_RANKS: Dict[str, List[str]] = {
    "rank": ["稳涨", "强趋势", "ETF"],
    "reco": ["抗跌榜单", "自选算法榜单", "开仓榜单", "开仓", "综合推荐"],
    "warn": ["动能衰减预警"],
    "layer": ["板块强度代表基金"],
}


# ============================================================================
# 便捷查询函数
# ============================================================================

def get_panels() -> List[str]:
    """获取所有 panel 列表。

    Returns:
        panel key 列表，如 ['day', 'reco']
    """
    return list(RANK_CONFIG.keys())

def get_subs(panel: str) -> List[str]:
    """获取指定 panel 的所有子标签 key 列表。

    Args:
        panel: panel key，如 'day'、'reco'

    Returns:
        子标签 key 列表，如 ['当日', '两日', '三日', '七日', '综合']
        panel 不存在时返回空列表
    """
    if panel not in RANK_CONFIG:
        return []
    return [s["key"] for s in RANK_CONFIG[panel]["subs"]]

def get_sub_configs(panel: str) -> List[dict]:
    """获取指定 panel 的所有子标签完整配置。

    Args:
        panel: panel key

    Returns:
        子标签配置列表，每个元素包含 key/label/desc 等字段
        panel 不存在时返回空列表
    """
    if panel not in RANK_CONFIG:
        return []
    return list(RANK_CONFIG[panel]["subs"])

def get_top_n(panel: str, sub: Optional[str] = None) -> int:
    """获取指定榜单的 top_n。

    Args:
        panel: panel key
        sub: 子标签 key（可选，用于 ETF 等特殊榜单）

    Returns:
        取前 N 名。ETF 榜返回每组前3，其他返回 panel 级别的 top_n（默认30）
    """
    if panel not in RANK_CONFIG:
        return 30
    # ETF 榜每个主题取前3
    if sub == "ETF" and panel == "reco":
        return 3
    return RANK_CONFIG[panel].get("top_n", 30)

def get_panel_display(panel: str) -> str:
    """获取 panel 的显示名称（带图标）。

    Args:
        panel: panel key

    Returns:
        显示名称，如 '📅 日榜'、'🎯 推荐榜单'
        panel 不存在时返回原始 panel 字符串
    """
    if panel not in RANK_CONFIG:
        return panel
    cfg = RANK_CONFIG[panel]
    icon = cfg.get("icon", "")
    name = cfg.get("name", panel)
    return f"{icon} {name}".strip()


def get_panel_name(panel: str) -> str:
    """获取 panel 的纯名称（不带图标）。

    Args:
        panel: panel key

    Returns:
        名称，如 '日榜'、'推荐榜单'
    """
    if panel not in RANK_CONFIG:
        return panel
    return RANK_CONFIG[panel].get("name", panel)


def get_algo_desc(panel: str) -> str:
    """获取 panel 的算法说明文字。

    Args:
        panel: panel key

    Returns:
        算法说明文字，用于页面底部说明区块
    """
    if panel not in RANK_CONFIG:
        return ""
    return RANK_CONFIG[panel].get("algo_desc", "")


def is_deprecated(panel: str, sub: str) -> bool:
    """检查是否是已删除的榜单。

    Args:
        panel: panel key
        sub: 子标签 key

    Returns:
        True 表示该榜单已删除，应忽略或告警
    """
    return sub in DEPRECATED_RANKS.get(panel, [])


def is_valid_rank(panel: str, sub: str) -> bool:
    """检查是否是有效的当前榜单。

    Args:
        panel: panel key
        sub: 子标签 key

    Returns:
        True 表示该榜单在当前配置中存在
    """
    if panel not in RANK_CONFIG:
        return False
    return sub in get_subs(panel)


# ============================================================================
# 自检（直接运行此文件时执行）
# ============================================================================

if __name__ == "__main__":
    for panel in get_panels():
        subs = get_subs(panel)
        for sub in subs:
            top_n = get_top_n(panel, sub)
    for panel, subs in DEPRECATED_RANKS.items():
        for sub in subs:
            pass  # 自检：遍历已弃用榜单
