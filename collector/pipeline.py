"""
collector/pipeline.py —— 兼容层

v2.5.5 架构重构：所有函数已迁移到 modules.common.pipeline
本文件保留为兼容层，通过 re-export 方式导出所有函数和常量

注意：以下划线开头的私有函数不会被 `import *` 导出，需要显式导入
"""
from modules.common.pipeline import *  # noqa: F401,F403
# 显式导出被外部调用的私有函数
# v2.9.1 修复: 补充 _fetch_top_holdings 和 _fetch_top_history 导出
#   此前缺失导致 fetch_manager 阶段2.5 ImportError, 持仓抓取完全跳过
# v2.9.44: _fetch_top_returns 已删除(m1/m3/m6/y1由阶段1净值计算), 不再导出
from modules.common.pipeline import (
    _fetch_top_holdings,
    _fetch_top_history,
    _stored_holdings,
    _today,
)  # noqa: F401
