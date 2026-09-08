"""
collector/etf_abbr.py —— 兼容层

v2.5.5 架构重构：所有函数已迁移到 modules.fund.etf_abbr
本文件保留为兼容层，通过 re-export 方式导出所有函数
"""
from modules.fund.etf_abbr import etf_short_label, is_index_like

__all__ = ["etf_short_label", "is_index_like"]
