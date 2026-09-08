"""
collector/scoring.py —— 兼容层

v2.5.5 架构重构：所有函数已迁移到 modules.score.scoring
本文件保留为兼容层，通过 re-export 方式导出所有函数和常量
"""
from modules.score.scoring import (
    pool_earn_scores, _up_capture,
    _align_bench_vals, calc_excess_rets, big_rise_follow,
    _fund_bench_series, detect_yindie_deep,
    compute_scores_v2,
)
