# -*- coding: utf-8 -*-
"""
recommendation.py —— 推荐信号 / 形态判定 / 动能状态 模块

从 collector/ranker.py 提取的推荐与判定层。

核心函数：
- compute_reco_score: v0.37 推荐打分（位置25+动能35+抗跌25+性价比15）
- compute_reco_v32: V3.2 推荐买入建议（六维评分+抄底/趋势双模式）
- trend_status: 形态分型
- liq_score / reco_filter: 综合优选推荐过滤
- reco_note_txt / reco_risk_tags_txt: 推荐文案与风险标签
- kinetic_state / momentum_status: 动能状态判定

设计要点：
- 本模块仅依赖 collector.metrics 中的纯函数，不依赖 scoring/ranking
- ranking.py 从本模块导入推荐与判定函数
"""
from __future__ import annotations

import math
from typing import Optional

from collector.metrics import (
    _has_unrepaired_big_drop, _max_dd, _daily_rets, _pstdev,
    _ma_n, _big_drop_unrepaired, _ret_pct_navs,
)
from algo_config import get_algo_config

import logging

logger = logging.getLogger(__name__)

# V3.2 推荐信号等级（从低到高）
_RECO_ORDER_V32 = ["清仓", "回避", "观望", "试探", "轻仓", "布局", "买入"]
# 趋势模式信号（只升不降）
_TREND_SIGNALS_V32 = ("买入", "布局", "轻仓")


# ---------------------------------------------------------------------------
# 推荐打分 v0.37
# ---------------------------------------------------------------------------

def compute_reco_score(m, navs, excess=None, momentum="走弱"):
    """推荐是否购买打分(v0.37,满分100,进攻型权重)。
    位置25 + 动能35 + 抗跌风险25 + 性价比15 → 档位 买入/布局/观望/回避。
    动能硬约束(4档): 衰减→最高观望; 走弱→强制回避。
    """
    # 5.1 位置维度 25分(近60日高点回撤)
    dd = m.get("dd_from_hi")
    draw = -(dd if dd is not None else 0)
    if draw <= 0:
        pos = 0        # 创60日新高
    elif draw < 2:
        pos = 2
    elif draw < 8:
        pos = 9
    elif draw < 15:
        pos = 17
    else:
        pos = 25
    # 5.2 多周期动能 35分
    pos_cycles = sum(1 for k in ("d3", "d5", "d7", "d10") if (m.get(k) or 0) > 0)
    kin = {4: 20, 3: 14, 2: 8, 1: 4, 0: 0}.get(pos_cycles, 0)
    d10v = m.get("d10") or 0
    if d10v >= 5:
        kin += 15
    elif d10v >= 2:
        kin += 10
    elif d10v >= 0:
        kin += 5
    ex = excess or {}
    pos_ex = sum(1 for k in (3, 5, 7, 10) if (ex.get("ex%d" % k) or 0) > 0)
    if pos_cycles >= 3 and pos_ex < 2:
        kin = max(0, kin - 5)
    kin = max(0, min(35, kin))
    # 5.3 抗跌风险 25分(近60日最大回撤 + 大跌事件修复)
    maxdd = abs(m.get("mdd") or 0)
    unrep = _has_unrepaired_big_drop(navs)
    if maxdd < 8 and not unrep:
        risk = 25
    elif 8 <= maxdd < 15 and not unrep:
        risk = 18
    elif 15 <= maxdd < 22:
        risk = 10
    else:
        risk = 0
    # 5.4 收益性价比 15分(近60日卡玛比率)
    if len(navs) >= 2 and navs[0]:
        ret60 = (navs[-1] / navs[0] - 1) * 100
    else:
        ret60 = 0
    mdd60 = maxdd if maxdd > 0 else 0
    cal60 = ret60 / mdd60 if mdd60 > 0 else 99
    if cal60 >= 1.5:
        val = 15
    elif cal60 >= 0.8:
        val = 10
    elif cal60 >= 0.3:
        val = 5
    else:
        val = 0
    total = pos + kin + risk + val
    # 5.5 档位映射
    if total >= 80:
        lvl = "买入"
    elif total >= 55:
        lvl = "布局"
    elif total >= 30:
        lvl = "观望"
    else:
        lvl = "回避"
    # 动能硬约束(优先级高于总分, 4档规范)
    if momentum == "走弱":
        lvl = "回避"
    elif momentum == "衰减" and lvl in ("买入", "布局"):
        lvl = "观望"
    return total, lvl


# ---------------------------------------------------------------------------
# V3.2 推荐买入建议信号
# ---------------------------------------------------------------------------

def compute_reco_v32(m, navs, prev_reco=None, prev_reco_days=None,
                     idx60=None):
    """V3.2 推荐买入建议信号。

    参数:
      m         calc_metrics 输出指标字典
      navs      近60日净值序列(升序, 无缺失)
      prev_reco 上一交易日信号(用于趋势模式只升不降 / 连续3天升级)
      prev_reco_days 上一信号已持续交易日数
      idx60     上证指数近60日涨幅(%) 或 None

    返回 (reco_score:int, lvl:str, mode:str, note:str)
    约定: 数据不足时返回观望+备注「净值样本不足」, 由上层保证卡片不删除。
    """
    if not navs or len(navs) < 8:
        return 0, "观望", "抄底", "净值样本不足"
    navs = [v for v in navs if v is not None and v > 0]
    if len(navs) < 8:
        return 0, "观望", "抄底", "净值样本不足"
    w = navs[-60:]          # 近60日窗口(其余点仅用于60日线斜率等计算)

    # 统一从 60 日窗口计算 mdd / 波动率(与弹窗实时回放口径一致, 避免 60 vs 75 日不一致)
    _ddw = _max_dd(w)[0]
    dd60 = abs(_ddw) if _ddw is not None else 0
    _rw = _daily_rets(w)
    _vol60 = (_pstdev(_rw) * math.sqrt(252)) if len(_rw) > 1 else 0
    d10v = m.get("d10") or 0
    d5v = m.get("d5") or 0
    d3v = m.get("d3") or 0
    d7v = m.get("d7") or 0
    pos_cycles = sum(1 for k in ("d3", "d5", "d7", "d10") if (m.get(k) or 0) > 0)

    # ---- 维度1: 位置(15) 近60日最大回撤 + 站上60日线且斜率向上 ----
    pos_score = 0
    if dd60 >= 15:
        pos_score = 10
    elif dd60 >= 10:
        pos_score = 7
    elif dd60 >= 5:
        pos_score = 4
    ma60_now = _ma_n(w, 60) or _ma_n(navs, 60)
    ma60_prev = _ma_n(navs[:-10], 60) if len(navs) > 60 else None
    if ma60_now and ma60_prev and navs[-1] > ma60_now and ma60_now > ma60_prev:
        pos_score += 5

    # ---- 维度2: 动能(30) ----
    kin = min(15.0, pos_cycles * 3.75)
    if d10v > 10:
        kin += 10
    elif d10v > 5:
        kin += 7
    elif d10v > 0:
        kin += 4
    speed3 = d3v / 3.0 if d3v else 0
    speed10 = d10v / 10.0 if d10v else 0
    if speed3 > speed10:
        kin += 5          # 3日均速>10日均速: 动能加速
    if d3v <= d5v and d5v <= d7v:
        kin -= 2          # 3日涨幅递减: 动能衰减
    if pos_cycles >= 3 and d10v > 5:
        kin += 3
    kin = max(0.0, min(30.0, kin))

    # ---- 维度3: 抗跌风险(20) ----
    risk = 0.0
    if _big_drop_unrepaired(w, window=10, thr=-5.0):
        risk = 0.0                       # 近10日大跌未修复: 一票否决
    else:
        if dd60 >= 22:
            risk = 0.0
        elif dd60 >= 15:
            risk = 10.0
        elif dd60 >= 10:
            risk = 15.0
        else:
            risk = 20.0
        # 近10日低点修复进度加分
        if len(w) >= 10:
            seg = w[-10:]
            low10 = min(seg)
            if low10 and w[-1] > low10:
                rebound = (w[-1] / low10 - 1) * 100
                if rebound > 40:
                    risk += 15
                elif rebound > 25:
                    risk += 10
                elif rebound > 15:
                    risk += 5
    risk = max(0.0, min(20.0, risk))

    # ---- 维度4: 反弹弹性(15) 近10日大跌后5日平均涨幅 ----
    big_drop_idx = [i for i in range(max(1, len(w) - 10), len(w))
                    if w[i - 1] and (w[i] / w[i - 1] - 1) * 100 <= -3]
    if big_drop_idx:
        gains = []
        for i in big_drop_idx:
            for j in range(i + 1, min(i + 6, len(w))):
                if w[j - 1]:
                    gains.append((w[j] / w[j - 1] - 1) * 100)
        avg_g = (sum(gains) / len(gains)) if gains else 0.0
        if avg_g > 5:
            el = 15.0
        elif avg_g > 2:
            el = 10.0
        elif avg_g > 0:
            el = 5.0
        else:
            el = 0.0
        if d10v < d5v:
            el -= 3                        # 10日动能弱于5日
    else:
        el = 7.0                           # 近10日无大跌: 中性
    el = max(0.0, min(15.0, el))

    # ---- 维度5: 性价比(10) 近60日卡玛比率 ----
    ret60 = (w[-1] / w[0] - 1) * 100 if w[0] else 0.0
    mdd60 = dd60 if dd60 > 0 else 0.0
    cal60 = (ret60 / mdd60) if mdd60 > 0 else 99.0
    if cal60 > 1:
        val = 10.0
    elif cal60 > 0.5:
        val = 7.0
    elif cal60 > 0.3:
        val = 4.0
    else:
        val = 0.0

    # ---- 维度6: 大盘环境(10) 上证指数近60日涨幅 ----
    if idx60 is None:
        idx_score = 5.0                    # 未知给中性分
    elif idx60 > 10:
        idx_score = 10.0
    elif idx60 > 5:
        idx_score = 7.0
    elif idx60 > 0:
        idx_score = 4.0
    elif idx60 > -5:
        idx_score = 3.0
    elif idx60 > -15:
        idx_score = 3.0
    else:
        idx_score = 5.0                    # ≤-15%: 超跌反弹预期

    total = round(pos_score + kin + risk + el + val + idx_score)

    # ---- 档位门槛(按年化波动率动态调整, 基准阈值从统一配置读取) ----
    cfg = get_algo_config()
    base_lay_th = cfg.reco_layout_th  # 布局基准阈值(默认70)
    base_buy_th = cfg.reco_buy_th     # 买入基准阈值(默认80)
    vol = abs(_vol60) if _vol60 else 0
    if vol < 30:
        # 低波动基金:阈值降低5分(更容易买入)
        lay_th, buy_th = base_lay_th - 5, base_buy_th - 5
    elif vol <= 50:
        # 中波动基金:使用基准阈值
        lay_th, buy_th = base_lay_th, base_buy_th
    else:
        # 高波动基金:阈值提高5分(更谨慎)
        lay_th, buy_th = base_lay_th + 5, base_buy_th + 5

    # ---- 辅助状态 ----
    ma20_now = _ma_n(navs, 20)
    last_ret = (w[-1] / w[-2] - 1) * 100 if len(w) > 1 and w[-2] else 0.0
    new_low = len(w) > 1 and w[-1] <= min(w[:-1])                    # 今日创新低
    low3_no_new = len(w) > 3 and min(w[-3:]) > min(w[:-3])            # 近3日不创新低
    momentum_exhausted = (d10v <= -10 or pos_cycles == 0)
    # chase_high: 从近20日最低点反弹>25% 且 当前价在MA20之上(确认高位), 触发追高熔断
    low20 = min(w[-20:]) if len(w) >= 20 else None
    chase_high = (len(w) >= 20 and low20 is not None
                  and (w[-1] / low20 - 1) * 100 > 25
                  and ma20_now is not None and w[-1] >= ma20_now)
    steady = low3_no_new and (_ret_pct_navs(w, 5) or 0) > 0           # 企稳确认
    kin_quality = pos_cycles

    # ---- 趋势模式判断 ----
    ma60_slope_up = bool(ma60_now and ma60_prev and ma60_now > ma60_prev)
    trend_trigger = ret60 > 12 and ma60_now is not None and navs[-1] > ma60_now and ma60_slope_up
    below20 = 0
    if ma20_now:
        for v in reversed(navs[-3:]):
            if v < ma20_now:
                below20 += 1
            else:
                break
    trend_exit = below20 >= 3                                       # 连续3日跌破20日线
    in_trend_prev = prev_reco in _TREND_SIGNALS_V32

    if in_trend_prev and not trend_exit:
        # 趋势模式延续: 只升不降
        new_lvl = "买入" if ret60 > 30 else ("布局" if ret60 > 15 else "轻仓")
        if _RECO_ORDER_V32.index(new_lvl) > _RECO_ORDER_V32.index(prev_reco):
            lvl = new_lvl
        else:
            lvl = prev_reco
        return total, lvl, "趋势", "趋势延续·只升不降"
    if trend_trigger:
        lvl = "买入" if ret60 > 30 else ("布局" if ret60 > 15 else "轻仓")
        return total, lvl, "趋势", "趋势触发"

    # ---- 抄底模式 ----
    mode = "抄底"
    # 清仓阈值 = 回避阈值 - 10(默认30-10=20)
    clear_th = cfg.reco_avoid_th - 10
    # 回避阈值 = 回避阈值 + 5(默认30+5=35, 保留原硬编码逻辑)
    avoid_th = cfg.reco_avoid_th + 5
    clear_cond = (total < clear_th and new_low and momentum_exhausted) or (last_ret < -5 and new_low)
    avoid_cond = total < avoid_th or new_low or momentum_exhausted or el <= 0
    buy_cond = (prev_reco in ("布局", "买入") and (prev_reco_days or 0) >= 2
                and ma20_now is not None and navs[-1] > ma20_now and kin_quality >= 3
                and total >= buy_th and not chase_high)
    lay_cond = steady and total >= lay_th and not chase_high
    # 试探: 急跌反弹确认 / 连续3日不创新低且当日涨>2% / 反弹弹性≥5且5日涨幅>0 (需企稳)
    trial_cond = steady and (
        ((_ret_pct_navs(w, 5) or 0) <= -15 and last_ret > 2)
        or (low3_no_new and last_ret > 2)
        or (el >= 5 and (_ret_pct_navs(w, 5) or 0) > 0))

    if clear_cond:
        lvl = "清仓"
    elif avoid_cond:
        lvl = "回避"
    elif buy_cond:
        lvl = "买入"
    elif lay_cond:
        lvl = "布局"
    elif trial_cond:
        lvl = "试探"
    else:
        lvl = "观望"
    return total, lvl, mode, "抄底六维评分"


# ---------------------------------------------------------------------------
# 形态分型
# ---------------------------------------------------------------------------


def trend_status(m: dict) -> str:
    """形态分型:⚠短期过热 / 连涨·强趋势 / 贴近高位·抗跌 / 稳涨·有支撑。"""
    d5 = m.get("d5") or 0
    d7 = m.get("d7") or 0
    streak = m.get("streak") or 0
    dd20 = m.get("dd_from_hi")
    if dd20 is None:
        dd20 = m.get("dd20") or 0
    if d5 >= 8 or d7 >= 15:
        return "⚠短期过热"
    if streak >= 4:
        return "连涨·强趋势"
    if dd20 >= -2:
        return "贴近高位·抗跌"
    return "稳涨·有支撑"


# ---------------------------------------------------------------------------
# 综合优选推荐
# ---------------------------------------------------------------------------

def liq_score(scale: Optional[float]) -> float:
    """流动性(规模 亿):10~50亿 满分;过小/过大均降分;缺失容错 6。"""
    if scale is None:
        return 6
    if scale < 1:
        return 0
    if scale < 2:
        return 6
    if scale < 5:
        return 9
    if scale < 10:
        return 11
    if scale < 50:
        return 15
    if scale < 200:
        return 13
    return 10


def reco_filter(m: dict, scale: Optional[float] = None, est: Optional[str] = None) -> bool:
    """综合优选推荐过滤(线上版 reco_filter)。"""
    today = __import__("datetime").date.today()
    if scale is not None and scale < 1.0:
        return False
    if est:
        try:
            if (today - __import__("datetime").date.fromisoformat(est)).days < 180:
                return False
        except Exception:
            logger.debug('推荐过滤: est=%r 成立日期解析失败，按「非次新」处理', est)
    if (m.get("d10") or 0) <= 0 or (m.get("d5") or 0) <= 0:
        return False
    dd20 = m.get("dd_from_hi")
    if dd20 is None:
        dd20 = m.get("dd20") or 0
    if dd20 <= -8:
        return False
    if (m.get("max_daily_drop_7d") or 0) > 5:
        return False
    if (m.get("dn7") or 0) >= 5:
        return False
    if momentum_status(m) == "走弱":
        return False
    return True


def reco_note_txt(m: dict, scale: Optional[float] = None, sec_m5: Optional[float] = None,
                  sec: str = "") -> str:
    """入选逻辑文案(线上版 reco_note)。"""
    pts = [f"10日 {m.get('d10') or 0:+.1f}%"]
    dd20 = m.get("dd_from_hi")
    if dd20 is None:
        dd20 = m.get("dd20")
    if dd20 is not None:
        pts.append(f"距高 {dd20:+.1f}%")
    if sec_m5 is not None:
        pts.append(f"{sec}景气 {sec_m5:+.1f}%")
    if scale is not None:
        pts.append(f"规模 {scale:.0f}亿")
    return "·".join(pts)


def reco_risk_tags_txt(m: dict, scale: Optional[float] = None, est: Optional[str] = None) -> str:
    """风险标签(多标签可并存,线上版 reco_risk_tags)。"""
    tags = []
    if scale is not None and scale < 2.0:
        tags.append("规模偏小")
    if est:
        try:
            today = __import__("datetime").date.today()
            if (today - __import__("datetime").date.fromisoformat(est)).days < 365:
                tags.append("次新基金")
        except Exception:
            logger.debug('风险标签: est=%r 成立日期解析失败，按「非次新」处理', est)
    if (m.get("d5") or 0) >= 8:
        tags.append("短线过热")
    dd20 = m.get("dd_from_hi")
    if dd20 is None:
        dd20 = m.get("dd20")
    if dd20 is not None and dd20 <= -5:
        tags.append("深度回调")
    dd7_val = m.get("max_daily_drop_7d")
    if dd7_val is not None and dd7_val <= -3:
        tags.append("回撤偏大")
    return "、".join(tags) if tags else "风险适中"


# ---------------------------------------------------------------------------
# 动能状态
# ---------------------------------------------------------------------------

def kinetic_state(m: dict) -> str:
    """动能状态(v2.1.9.4优化：多周期加权，提高短期敏感度)。

    动能分数 = d3×30% + d5×30% + d7×20% + d10×20%
    强劲: >5 | 维持: 2~5 | 衰减: 0~2 | 走弱: ≤0

    v2.9.1: 强制类型转换，防止字符串输入导致异常回退到旧5档算法
    """
    def _to_float(v):
        try:
            return float(v) if v is not None else 0.0
        except (ValueError, TypeError):
            return 0.0
    d3 = _to_float(m.get("d3"))
    d5 = _to_float(m.get("d5"))
    d7 = _to_float(m.get("d7"))
    d10 = _to_float(m.get("d10"))
    # 多周期加权动能分数（短期权重60%）
    kin_score = d3 * 0.30 + d5 * 0.30 + d7 * 0.20 + d10 * 0.20
    if kin_score > 5:
        return "强劲"
    if kin_score >= 2:
        return "维持"
    if kin_score > 0:
        return "衰减"
    return "走弱"


def momentum_status(m: dict) -> str:
    """动能状态(线上版,多周期交叉):3/5/7日均速斜率对比,识别高位衰减。
    仅评估前期强(10日动量≥8%)的基金;不足则「平稳」。"""
    d10 = m.get("d10") or 0
    d5 = m.get("d5") or 0
    d3 = m.get("d3") or 0
    d7 = m.get("d7") or 0
    dd = m.get("dd_from_hi")
    if dd is None:
        dd = m.get("dd20") or 0
    if d10 < 8:
        return "平稳"
    a3 = d3 / 3
    a5 = d5 / 5
    a7 = d7 / 7
    if d3 <= 0 or d5 <= 0 or dd <= -5:
        return "走弱"
    base = max(a5, a7)
    if base <= 0:
        return "走弱"
    if d7 <= 0 or d3 <= 0.5 or a3 < base * 0.6 or dd <= -2:
        return "衰减"
    if a3 < base * 0.85:
        return "维持"
    return "强劲"
