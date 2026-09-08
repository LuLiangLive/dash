"""
collector/metrics.py —— 兼容层

v2.5.5 架构重构：所有函数已迁移到 modules.common.metrics
本文件保留为兼容层，通过 re-export 方式导出所有函数和常量
"""
from modules.common.metrics import (
    METRIC_FIELD_MAP, apply_metric_field_map,
    _ret, _daily_rets, _pstdev, streak_days, risk_flags, _max_dd,
    percentile_scores, inverse_percentile_scores, _norm_weights, _cdf_phi,
    _pct_val, _ret_skipna, _reverse_pct, _forward_pct, _ma_n, _ret_pct_navs,
    _big_drop_unrepaired, _has_unrepaired_big_drop, _ret_series,
)
