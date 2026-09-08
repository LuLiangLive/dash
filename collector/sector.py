"""
collector/sector.py —— 兼容层

v2.5.5 架构重构：所有函数已迁移到 modules.market.sector
本文件保留为兼容层，通过 re-export 方式导出所有函数和常量
"""
from modules.market.sector import (
    SECTOR_RULES, FALLBACK_SECTOR, TYPE_RULES, FALLBACK_TYPE,
    infer_sector, infer_type, THEME_WORDS, INDEX_RULES, THEME_LEADERS,
    infer_themes,
)
