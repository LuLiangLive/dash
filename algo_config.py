# -*- coding: utf-8 -*-
"""
algo_config.py —— 算法参数统一配置中心（Single Source of Truth）

所有算法模块必须从这里读取参数，禁止在各模块中硬编码。
修改参数只需在设置页面保存，或修改此文件的默认值，然后重启服务/重新计算。

使用方式:
    from algo_config import get_algo_config, AlgoConfig

    cfg = get_algo_config()
    earn_weights = cfg.earn_weights  # {'d3': 0.15, 'd5': 0.20, ...}
    top_n = cfg.rank_topn            # 40
"""
from __future__ import annotations

import time
from typing import Dict, Optional

import db
from security.crypto import encrypt, decrypt


# ============================================================================
# 默认参数（所有可配置算法参数的唯一默认值来源）
# ============================================================================

DEFAULT_CONFIG: Dict[str, float] = {
    # --- 池子与样本 ---
    "pool_size": 6000.0,        # 池子大小上限
    "min_days": 300.0,          # 最小样本天数

    # --- 榜单 ---
    "rank_topn": 40.0,          # 各榜单取前N名

    # --- 大跌日阈值 ---
    "ddown_th": -1.0,           # 大跌日阈值(%)，大盘跌幅低于此值视为大跌日

    # --- 持仓 ---
    "stk_pct": 3.0,             # 持仓占比阈值(%)

    # --- 收益分约束 ---
    "earn_cap": 4.0,            # 收益分最大权重(大涨跑输大盘时的上限倍数)

    # --- 收益分权重（多周期涨幅加权）---
    # ⚠️ V3 线性收益分实际使用 modules/score/scoring_v3.py 的 EARN_CYCLE_WEIGHTS 常量,
    #    以下 earn_w_* 仅被旧百分位算法 pool_earn_scores 读取。默认值已与常量对齐,
    #    后续调整需两处同步, 避免漂移。
    "earn_w_d3": 0.15,          # 3日涨幅权重
    "earn_w_d5": 0.20,          # 5日涨幅权重
    "earn_w_d7": 0.15,          # 7日涨幅权重
    "earn_w_d10": 0.15,         # 10日涨幅权重(v2.11.2: 与 scoring_v3 常量对齐, 原0.20)
    "earn_w_m1": 0.20,          # 近1月涨幅权重(v2.11.2: 与 scoring_v3 常量对齐, 原0.15)
    "earn_w_ret2m": 0.15,       # 近2月涨幅权重

    # --- 收益分合成(旧百分位方案遗留, V3线性不读取) ---
    "earn_pct_weight": 0.60,    # 池内百分位权重
    "earn_abs_weight": 0.40,    # 绝对涨幅映射权重
    "earn_abs_scale": 10.0,     # 绝对涨幅分缩放系数(加权值10%→100分)

    # --- 抗跌分权重(旧百分位4指标方案遗留, V3线性使用 scoring_v3.AD_METRIC_WEIGHTS / AD_*_COEFF) ---
    "ad_w_max_daily_drop": 0.25,        # 单日最大跌幅权重
    "ad_w_max_dd": 0.25,        # 最大回撤权重
    "ad_w_down_vol": 0.25,      # 下行波动率权重
    "ad_w_dd_avg": 0.25,        # 大跌日均跌幅权重

    # --- 综合分权重 ---
    # ⚠️ V3 综合分固定为 收益42.5% + 抗跌42.5% + 卡玛百分位15%(scoring_v3.DUAL_*),
    #    下列 dual_* 为旧两分法(抗跌/收益)遗留, 已不被任何运行代码读取, 保留仅供历史设置兼容。
    "dual_ad_weight": 0.425,    # [遗留] 旧综合分抗跌权重(未使用, V3为42.5%+卡玛15%)
    "dual_earn_weight": 0.425,  # [遗留] 旧综合分收益权重(未使用, V3为42.5%+卡玛15%)

    # --- 推荐信号阈值 ---
    "reco_buy_th": 80.0,        # 买入信号阈值(综合分≥)
    "reco_layout_th": 70.0,     # 布局信号阈值
    "reco_light_th": 60.0,      # 轻仓信号阈值
    "reco_test_th": 50.0,       # 试探信号阈值
    "reco_watch_th": 40.0,      # 观望信号阈值
    "reco_avoid_th": 30.0,      # 回避信号阈值
    # 低于回避阈值 → 清仓

    # --- 攻防榜：捕获率计算口径 ---
    "atk_up_day_th": 0.5,       # 上涨日阈值(%)，大盘日收益>此值视为上涨日
    "atk_dn_day_th": -0.5,      # 下跌日阈值(%)，大盘日收益<此值视为下跌日

    # --- 攻防榜：高弹性榜 ---
    "atk_elastic_beta": 1.3,    # 高弹性Beta下限
    "atk_elastic_upcap": 1.3,   # 高弹性上涨捕获率下限

    # --- 攻防榜：攻守兼备榜 ---
    "atk_balanced_beta_low": 0.5,   # 攻守兼备Beta下限
    "atk_balanced_beta_high": 1.1,  # 攻守兼备Beta上限
    "atk_balanced_capdiff": 0.0,    # 攻守兼备捕获率差下限
    "atk_balanced_maxdd": -10.0,    # 攻守兼备最大回撤下限(跌幅小于此值)

    # --- 攻防榜：强抗跌榜 ---
    "atk_def_dncap": 0.7,       # 强抗跌下跌捕获率上限
    "atk_def_maxdd": -5.0,      # 强抗跌最大回撤下限(跌幅小于此值)

    # --- 攻防榜：反弹先锋榜 ---
    "atk_rebound_dd20": -5.0,   # 反弹先锋20日跌幅上限(跌幅大于此值)
    "atk_rebound_d5": 0.0,      # 反弹先锋5日涨幅下限

    # --- 超跌筑底榜（v2.9.1 重构ETF榜，v2.9.11加入RPS相对强度） ---
    "cd_crash_k": 2.0,          # 急跌强度系数K（波动率倍数，默认2.0）
    "cd_accel_ratio": 1.5,      # 加速下跌倍数（近5日跌幅/平时5日跌幅，默认1.5）
    "cd_low_dd": -20.0,         # 低位回撤阈值（距60日高点，默认-20%）
    "cd_low_pct": 20.0,         # 低位分位阈值（60日净值分位，默认20%）
    "cd_vol_narrow": 0.8,       # 筑底波动率收窄系数（10日波动<30日波动×此值，默认0.8）
    "cd_topn": 40.0,            # 超跌筑底榜取前N名（默认40）
    "cd_rps_threshold": 30.0,   # RPS入榜阈值（60日相对强度低于此值才入榜，默认30%）
    "cd_rps_rebound": 10.0,     # RPS回升加分阈值（20日RPS比60日RPS高此值，加筑底分，默认10百分位）
}

# ============================================================================
# 参数范围（用于前端校验和后端保存校验）
# ============================================================================

CONFIG_RANGES: Dict[str, tuple] = {
    "pool_size": (1000.0, 10000.0),
    "min_days": (60.0, 800.0),
    "rank_topn": (10.0, 100.0),
    "ddown_th": (-5.0, -0.1),
    "stk_pct": (0.5, 20.0),
    "earn_cap": (0.0, 10.0),
    "earn_w_d3": (0.0, 1.0),
    "earn_w_d5": (0.0, 1.0),
    "earn_w_d7": (0.0, 1.0),
    "earn_w_d10": (0.0, 1.0),
    "earn_w_m1": (0.0, 1.0),
    "earn_w_ret2m": (0.0, 1.0),
    "earn_pct_weight": (0.0, 1.0),
    "earn_abs_weight": (0.0, 1.0),
    "earn_abs_scale": (1.0, 100.0),
    "ad_w_max_daily_drop": (0.0, 1.0),
    "ad_w_max_dd": (0.0, 1.0),
    "ad_w_down_vol": (0.0, 1.0),
    "ad_w_dd_avg": (0.0, 1.0),
    "dual_ad_weight": (0.0, 1.0),
    "dual_earn_weight": (0.0, 1.0),
    "reco_buy_th": (0.0, 100.0),
    "reco_layout_th": (0.0, 100.0),
    "reco_light_th": (0.0, 100.0),
    "reco_test_th": (0.0, 100.0),
    "reco_watch_th": (0.0, 100.0),
    "reco_avoid_th": (0.0, 100.0),
    # 攻防榜：捕获率计算口径
    "atk_up_day_th": (0.1, 3.0),
    "atk_dn_day_th": (-3.0, -0.1),
    # 攻防榜：高弹性榜
    "atk_elastic_beta": (0.5, 3.0),
    "atk_elastic_upcap": (0.5, 3.0),
    # 攻防榜：攻守兼备榜
    "atk_balanced_beta_low": (0.0, 1.5),
    "atk_balanced_beta_high": (0.5, 2.0),
    "atk_balanced_capdiff": (-1.0, 1.0),
    "atk_balanced_maxdd": (-30.0, -1.0),
    # 攻防榜：强抗跌榜
    "atk_def_dncap": (0.1, 1.5),
    "atk_def_maxdd": (-20.0, -1.0),
    # 攻防榜：反弹先锋榜
    "atk_rebound_dd20": (-30.0, -1.0),
    "atk_rebound_d5": (-5.0, 10.0),
    # 超跌筑底榜
    "cd_crash_k": (1.0, 4.0),
    "cd_accel_ratio": (1.0, 3.0),
    "cd_low_dd": (-40.0, -5.0),
    "cd_low_pct": (5.0, 50.0),
    "cd_vol_narrow": (0.5, 1.0),
    "cd_topn": (10.0, 100.0),
    "cd_rps_threshold": (10.0, 60.0),
    "cd_rps_rebound": (5.0, 30.0),
}

# ============================================================================
# 参数分组（用于前端设置页面分组展示）
# ============================================================================

CONFIG_GROUPS: Dict[str, list] = {
    "池子与样本": ["pool_size", "min_days"],
    "榜单": ["rank_topn"],
    "大跌日与持仓": ["ddown_th", "stk_pct"],
    "收益分权重": ["earn_w_d3", "earn_w_d5", "earn_w_d7", "earn_w_d10", "earn_w_m1", "earn_w_ret2m"],
    "收益分合成": ["earn_pct_weight", "earn_abs_weight", "earn_abs_scale", "earn_cap"],
    "抗跌分权重": ["ad_w_max_daily_drop", "ad_w_max_dd", "ad_w_down_vol", "ad_w_dd_avg"],
    "综合分权重": ["dual_ad_weight", "dual_earn_weight"],
    "推荐信号阈值": ["reco_buy_th", "reco_layout_th", "reco_light_th", "reco_test_th", "reco_watch_th", "reco_avoid_th"],
    "攻防榜-捕获率口径": ["atk_up_day_th", "atk_dn_day_th"],
    "攻防榜-高弹性": ["atk_elastic_beta", "atk_elastic_upcap"],
    "攻防榜-攻守兼备": ["atk_balanced_beta_low", "atk_balanced_beta_high", "atk_balanced_capdiff", "atk_balanced_maxdd"],
    "攻防榜-强抗跌": ["atk_def_dncap", "atk_def_maxdd"],
    "攻防榜-反弹先锋": ["atk_rebound_dd20", "atk_rebound_d5"],
    "超跌筑底榜": ["cd_crash_k", "cd_accel_ratio", "cd_low_dd", "cd_low_pct", "cd_vol_narrow", "cd_topn", "cd_rps_threshold", "cd_rps_rebound"],
}

# 参数中文标签
CONFIG_LABELS: Dict[str, str] = {
    "pool_size": "池子大小上限",
    "min_days": "最小样本天数",
    "rank_topn": "榜单取前N名",
    "ddown_th": "大跌日阈值(%)",
    "stk_pct": "持仓占比阈值(%)",
    "earn_cap": "收益分上限倍数",
    "earn_w_d3": "3日涨幅权重",
    "earn_w_d5": "5日涨幅权重",
    "earn_w_d7": "7日涨幅权重",
    "earn_w_d10": "10日涨幅权重",
    "earn_w_m1": "近1月涨幅权重",
    "earn_w_ret2m": "近2月涨幅权重",
    "earn_pct_weight": "池内百分位权重",
    "earn_abs_weight": "绝对涨幅权重",
    "earn_abs_scale": "绝对涨幅缩放系数",
    "ad_w_max_daily_drop": "单日最大跌幅权重",
    "ad_w_max_dd": "最大回撤权重",
    "ad_w_down_vol": "下行波动率权重",
    "ad_w_dd_avg": "大跌日均跌幅权重",
    "dual_ad_weight": "抗跌分权重",
    "dual_earn_weight": "收益分权重",
    "reco_buy_th": "买入阈值",
    "reco_layout_th": "布局阈值",
    "reco_light_th": "轻仓阈值",
    "reco_test_th": "试探阈值",
    "reco_watch_th": "观望阈值",
    "reco_avoid_th": "回避阈值",
    # 攻防榜：捕获率计算口径
    "atk_up_day_th": "上涨日阈值(%)",
    "atk_dn_day_th": "下跌日阈值(%)",
    # 攻防榜：高弹性榜
    "atk_elastic_beta": "Beta下限",
    "atk_elastic_upcap": "上涨捕获率下限",
    # 攻防榜：攻守兼备榜
    "atk_balanced_beta_low": "Beta下限",
    "atk_balanced_beta_high": "Beta上限",
    "atk_balanced_capdiff": "捕获率差下限",
    "atk_balanced_maxdd": "最大回撤下限(%)",
    # 攻防榜：强抗跌榜
    "atk_def_dncap": "下跌捕获率上限",
    "atk_def_maxdd": "最大回撤下限(%)",
    # 攻防榜：反弹先锋榜
    "atk_rebound_dd20": "20日跌幅上限(%)",
    "atk_rebound_d5": "5日涨幅下限(%)",
    # 超跌筑底榜
    "cd_crash_k": "急跌强度系数K",
    "cd_accel_ratio": "加速下跌倍数",
    "cd_low_dd": "低位回撤阈值(%)",
    "cd_low_pct": "低位分位阈值(%)",
    "cd_vol_narrow": "筑底波动率收窄系数",
    "cd_topn": "榜单取前N名",
    "cd_rps_threshold": "RPS入榜阈值(%)",
    "cd_rps_rebound": "RPS回升加分阈值(百分位)",
}


class AlgoConfig:
    """算法配置对象，提供类型化的属性访问。"""

    def __init__(self, data: Dict[str, float]):
        self._data = data

    def __getattr__(self, name: str) -> float:
        if name.startswith("_"):
            return super().__getattribute__(name)
        data = super().__getattribute__("_data")
        if name in data:
            return data[name]
        raise AttributeError(f"AlgoConfig has no attribute '{name}'")

    def get(self, key: str, default: Optional[float] = None) -> Optional[float]:
        return self._data.get(key, default)

    def to_dict(self) -> Dict[str, float]:
        return dict(self._data)

    @property
    def earn_weights(self) -> Dict[str, float]:
        """收益分多周期权重字典。"""
        return {
            "d3": self._data["earn_w_d3"],
            "d5": self._data["earn_w_d5"],
            "d7": self._data["earn_w_d7"],
            "d10": self._data["earn_w_d10"],
            "m1": self._data["earn_w_m1"],
            "ret2m": self._data["earn_w_ret2m"],
        }

    @property
    def ad_weights(self) -> Dict[str, float]:
        """抗跌分4指标权重字典。"""
        return {
            "max_dn": self._data["ad_w_max_daily_drop"],
            "max_dd": self._data["ad_w_max_dd"],
            "down_vol": self._data["ad_w_down_vol"],
            "dd_avg": self._data["ad_w_dd_avg"],
        }


# 缓存：避免每次都查数据库
_cache: Optional[AlgoConfig] = None
_cache_time: float = 0.0
_CACHE_TTL: float = 60.0  # 缓存60秒


def get_algo_config(force_refresh: bool = False) -> AlgoConfig:
    """获取算法配置（带缓存）。

    优先级：数据库 settings 表 > 默认值。
    设置页面保存后会调用 invalidate_cache() 清除缓存。
    """
    global _cache, _cache_time
    now = time.time()
    if not force_refresh and _cache is not None and (now - _cache_time) < _CACHE_TTL:
        return _cache

    data = dict(DEFAULT_CONFIG)
    try:
        conn = db.get_conn()
        rows = conn.execute("SELECT key, value FROM settings WHERE key LIKE 'algo.%'").fetchall()
        for r in rows:
            k = (r["key"] or "").split(".", 1)[-1]
            if k in DEFAULT_CONFIG:
                try:
                    # v2.7.1: 解密敏感配置（向后兼容：明文自动识别）
                    raw_value = decrypt(r["value"]) if r["value"] else r["value"]
                    data[k] = float(raw_value)
                except (TypeError, ValueError):
                    pass
    except Exception:
        pass

    _cache = AlgoConfig(data)
    _cache_time = now
    return _cache


def invalidate_cache() -> None:
    """清除配置缓存（设置页面保存后调用）。"""
    global _cache, _cache_time
    _cache = None
    _cache_time = 0.0


def save_algo_config(cfg: Dict[str, float]) -> tuple[bool, str]:
    """保存算法配置到数据库（带范围校验）。

    返回 (success, message)。
    """
    try:
        conn = db.get_conn()
        for k, v in cfg.items():
            if k not in DEFAULT_CONFIG:
                continue
            lo, hi = CONFIG_RANGES.get(k, (-1e9, 1e9))
            try:
                vf = float(v)
            except (TypeError, ValueError):
                return False, f"{k} 不是合法数字"
            if vf < lo or vf > hi:
                return False, f"{CONFIG_LABELS.get(k, k)} 必须在 [{lo}, {hi}] 区间"
            conn.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                # v2.7.1: 加密存储敏感配置
                (f"algo.{k}", encrypt(str(vf)), time.strftime("%Y-%m-%dT%H:%M:%S+08:00")),
            )
        conn.commit()
        invalidate_cache()
        return True, ""
    except Exception as e:
        return False, str(e)[:100]


def get_config_meta() -> dict:
    """获取配置元信息（供前端设置页面使用）。"""
    return {
        "defaults": DEFAULT_CONFIG,
        "ranges": {k: {"min": v[0], "max": v[1]} for k, v in CONFIG_RANGES.items()},
        "groups": CONFIG_GROUPS,
        "labels": CONFIG_LABELS,
    }
