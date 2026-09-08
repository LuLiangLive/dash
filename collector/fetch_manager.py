"""
collector/fetch_manager.py —— 兼容层

v2.5.5 架构重构：所有函数已迁移到 modules.common.fetch_manager
本文件保留为兼容层，通过 re-export 方式导出所有函数和常量
"""
from modules.common.fetch_manager import *  # noqa: F401,F403
