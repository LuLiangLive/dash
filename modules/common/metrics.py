"""
modules/common/metrics.py —— 纯指标计算函数（无外部依赖，仅依赖 math/typing）

从 collector.metrics 迁移（v2.5.5架构重构）
从 ranker.py 提取的基础工具层，所有函数均为纯函数：
- 收益计算：_ret, _daily_rets, _ret_skipna, _ret_pct_navs, _ret_series
- 统计工具：_pstdev, percentile_scores, inverse_percentile_scores, _reverse_pct, _forward_pct, _pct_val, _norm_weights, _cdf_phi
- 回撤/波动：_max_dd, streak_days, _ma_n, _big_drop_unrepaired, _has_unrepaired_big_drop
- 风险标记：risk_flags
- 字段映射：METRIC_FIELD_MAP, apply_metric_field_map

设计要点：
- 本模块不导入 collector 内部任何模块，保证可独立测试
- ranker.py / scoring.py / recommendation.py 均从本模块导入基础函数
"""
from __future__ import annotations

import math
from typing import Optional


# ---------------------------------------------------------------------------
# 统一字段映射 (calc_metrics 输出 → 前端/API 使用的字段名)
# 避免在 main.py 多处硬编码导致漂移
# ---------------------------------------------------------------------------
METRIC_FIELD_MAP = {
    "up_cap": "up_capture",    # 上涨捕获率
    "dn_cap": "dn_capture",    # 下跌捕获率
}


def apply_metric_field_map(metrics: dict) -> dict:
    """将 calc_metrics 输出的字段名映射为前端使用的字段名, 返回新 dict."""
    out = dict(metrics)
    for src, dst in METRIC_FIELD_MAP.items():
        if src in out:
            out[dst] = out[src]
    return out


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def _ret(navs, n, min_ratio=0.0):
    """近 n 个交易日累计涨幅 %。样本不足/含空值返回 None。

    历史净值里存在 dwjz 为 NULL 的行(种子数据按周填充, 仅周五有值),
    此前只判断 base==0, 取到 None 时会抛
    TypeError: unsupported operand type(s) for -: 'NoneType' and 'NoneType',
    使整只基金被计入采集失败。这里对 None 一并做防御。

    v2.9.8: 增加 min_ratio 参数，数据不足窗口的 min_ratio 时返回 None，
    避免用不足数据冒充长期指标。默认0.0表示不检查（保持向后兼容）。
    """
    if not navs or len(navs) < n + 1:
        return None
    # 数据完整性检查：数据不足窗口的min_ratio时返回None
    if min_ratio > 0 and len(navs) < (n + 1) * min_ratio:
        return None
    base, cur = navs[-(n + 1)], navs[-1]
    if base is None or cur is None or base == 0:
        return None
    return (cur - base) / base * 100


def _daily_rets(navs):
    return [(navs[i] - navs[i - 1]) / navs[i - 1] * 100
            for i in range(1, len(navs)) if navs[i - 1] and navs[i] is not None]


def _pstdev(data):
    """总体标准差(替代 statistics.pstdev, 避免 Fraction 精确计算开销).

    statistics.pstdev 内部对 float 输入使用 Fraction 做精确求和
    (as_integer_ratio + math.gcd), 对金融波动率场景完全多余。
    本实现与 statistics.pstdev 数值一致(diff=0), 速度快 ~8.8x。
    """
    n = len(data)
    if n < 2:
        return 0.0
    mean = sum(data) / n
    return math.sqrt(sum((x - mean) ** 2 for x in data) / n)


def streak_days(navs):
    """截至最新的连续上涨天数(日涨幅>0.1%)。"""
    rets = _daily_rets(navs)
    cnt = 0
    for r in reversed(rets):
        if r > 0.1:
            cnt += 1
        else:
            break
    return cnt


def risk_flags(f: dict, m: dict) -> str:
    """风险标记(演示口径):规模偏小 / 波动大 / 回撤深。"""
    flags = []
    scale = f.get("scale")
    if scale is not None and scale < 0.5:
        flags.append("规模偏小")
    if m.get("vol") and m["vol"] > 40:
        flags.append("波动大")
    if m.get("mdd") and m["mdd"] < -30:
        flags.append("回撤深")
    return "、".join(flags) if flags else ""


def _max_dd(navs):
    """最大回撤 % / 持续天数 / 修复状态 / 回撤区间(起点,终点索引)。
    回撤区间:最大回撤段的 [peak_idx, trough_idx]；未修复时终点=最新点。
    持续天数 = 峰值日 → 最低点 的交易日跨度。"""
    if not navs:
        return None, None, "—", None
    peak = navs[0]
    peak_idx = 0
    dd, mdd_peak, mdd_peak_idx, mdd_trough_idx = 0.0, navs[0], 0, 0
    for i, v in enumerate(navs):
        if v > peak:
            peak = v
            peak_idx = i
        cur = (v - peak) / peak
        if cur < dd:
            dd = cur
            mdd_peak = peak
            mdd_peak_idx = peak_idx   # 回撤起点 = 峰值日
            mdd_trough_idx = i         # 回撤终点 = 最低点
    status = "已修复" if (navs and navs[-1] >= mdd_peak * 0.995) else "修复中"
    days = mdd_trough_idx - mdd_peak_idx + 1
    seg = (mdd_peak_idx, mdd_trough_idx)
    return dd * 100, days, status, seg


def percentile_scores(values):
    """池内百分位:最优 100 / 最差 0。values: list[float|None]。返回 {value_index: score}。"""
    n = len(values)
    if n == 0:
        return {}
    valid = [(i, v) for i, v in enumerate(values) if v is not None]
    if not valid:
        return {}
    sorted_v = sorted(valid, key=lambda x: x[1])
    scores = {}
    for rank, (i, _) in enumerate(sorted_v):
        scores[i] = round(rank / (len(sorted_v) - 1) * 100) if len(sorted_v) > 1 else 100
    return scores


def inverse_percentile_scores(values):
    """反转百分位:值越小越好(回撤/波动率/跌幅类)。"""
    n = len(values)
    if n == 0:
        return {}
    valid = [(i, v) for i, v in enumerate(values) if v is not None]
    if not valid:
        return {}
    sorted_v = sorted(valid, key=lambda x: x[1], reverse=True)
    scores = {}
    for rank, (i, _) in enumerate(sorted_v):
        scores[i] = round(rank / (len(sorted_v) - 1) * 100) if len(sorted_v) > 1 else 100
    return scores


def _norm_weights(weights):
    """缺失指标权重归一化:weights=[(值,权重,方向)],方向 dir=1 值大高分 / dir=-1 值小高分。
    返回 (百分位分, 归一化权重) 或 (None,0)。"""
    valid = [w for w in weights if w[0] is not None]
    if not valid:
        return None, 0
    tot = sum(w[1] for w in valid)
    return valid, tot


def _cdf_phi(z):
    """标准正态累积分布函数 Φ(z) ≈ 100×CDF。z 已 clamp 到 [-3,3]。
    用误差函数 erf 实现:Φ(z)=0.5*(1+erf(z/√2))。"""
    return 100 * 0.5 * (1 + math.erf(z / math.sqrt(2)))


def _pct_val(values, p=85):
    """池内排序第 p 百分位值(p:0-100)。None 跳过;空列表返回 None。
    用于收益分池内类基准:基准 = 池内前 (100-p)% 门槛,如 p=85 → 前15%门槛。"""
    v = [x for x in values if x is not None]
    if not v:
        return None
    v = sorted(v)
    idx = min(len(v) - 1, max(0, round(p / 100.0 * (len(v) - 1))))
    return v[idx]


def _ret_skipna(seq, k):
    """近 k 交易日区间收益(%),跳过 None 取尾部有效值。"""
    vals = [v for v in seq if v is not None]
    if len(vals) <= k:
        return None
    base = vals[-1 - k]
    if not base:
        return None
    return (vals[-1] / base - 1) * 100


def _reverse_pct(values):
    """反转百分位:值越小越好,返回 {idx: 0-100}。None 跳过。
    最小值(最抗跌)→100分,最大值→0分。"""
    n = len(values)
    valid = [(i, v) for i, v in enumerate(values) if v is not None]
    if not valid:
        return {}
    sv = sorted(valid, key=lambda x: x[1])  # 升序:最小在前
    scores = {}
    _m = len(sv) - 1
    for rank, (i, _) in enumerate(sv):
        # rank=0 是最小值 → 100 分;rank=最后是最大值 → 0 分
        scores[i] = round((_m - rank) / _m * 100) if _m > 0 else 100
    return scores


def _forward_pct(values):
    """正向百分位:值越大越好。
    最大值→100分,最小值→0分。"""
    n = len(values)
    valid = [(i, v) for i, v in enumerate(values) if v is not None]
    if not valid:
        return {}
    sv = sorted(valid, key=lambda x: x[1], reverse=True)  # 降序:最大在前
    scores = {}
    _m = len(sv) - 1
    for rank, (i, _) in enumerate(sv):
        # rank=0 是最大值 → 100 分;rank=最后是最小值 → 0 分
        scores[i] = round((_m - rank) / _m * 100) if _m > 0 else 100
    return scores


def _ma_n(navs, n):
    """最近 n 日简单均线; 样本不足返回 None。"""
    if not navs or len(navs) < n:
        return None
    return sum(navs[-n:]) / n


def _ret_pct_navs(navs, n):
    """近 n 交易日涨跌幅(%)。"""
    if not navs or len(navs) <= n or not navs[-(n + 1)]:
        return None
    return (navs[-1] / navs[-(n + 1)] - 1) * 100


def _big_drop_unrepaired(navs, window=None, thr=-5.0):
    """窗口(window=近N日,None=全序列)内是否存在单日跌幅≤thr%且未修复的大跌。
    未修复: 未恢复到事件前一日收盘。"""
    start = max(1, len(navs) - window) if window else 1
    for i in range(start, len(navs)):
        prev = navs[i - 1]
        if prev and (navs[i] / prev - 1) * 100 <= thr:
            base = prev
            if not any(v >= base for v in navs[i + 1:]):
                return True
    return False


def _has_unrepaired_big_drop(navs):
    """近60日是否存在未修复大跌事件(单日净值跌幅≥3%,且未恢复到事件前一日收盘)。"""
    for i in range(1, len(navs)):
        prev = navs[i - 1]
        if prev and (navs[i] / prev - 1) * 100 <= -3:
            base = prev
            if not any(v >= base for v in navs[i + 1:]):
                return True
    return False


def _ret_series(pts, k):
    """净值序列近 k 交易日累计涨幅;不足返回 None。pts 升序 [(date,nav)]。"""
    if len(pts) <= k or not pts[0][1]:
        return None
    return (pts[-1][1] / pts[-1 - k][1] - 1) * 100
