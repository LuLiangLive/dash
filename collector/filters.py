"""
collector/filters.py —— 兼容层

v2.5.5 架构重构：所有函数已迁移到 modules.common.filters
本文件保留为兼容层，通过 re-export 方式导出所有函数
"""
from modules.common.filters import is_index_like, is_bond_like, is_equity_excluded
