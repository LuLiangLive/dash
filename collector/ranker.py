"""
collector/ranker.py —— 兼容层

v2.5.5 架构重构：所有函数已迁移到 modules.rank.ranker
本文件保留为兼容层，通过 re-export 方式导出所有函数和常量

注意：以下划线开头的私有函数不会被 `import *` 导出，需要显式导入
"""
from modules.rank.ranker import *  # noqa: F401,F403
# 显式导出被外部调用的私有函数
from modules.rank.ranker import (
    _build_idx_map,
    _build_ddays,
    _ret,
    _ret_skipna,
)  # noqa: F401
