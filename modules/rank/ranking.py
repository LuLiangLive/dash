# -*- coding: utf-8 -*-
"""
ranking.py —— 自选评分 / 面板综合分 模块

核心函数：
- watch_score_light: 自选标准评分（中长期35+近期25+回撤15+波动16+夏普9）
- panel_score: 面板特定综合分（仅保留自选算法榜单分支）

设计要点：
- 本模块依赖 collector.metrics 和 collector.recommendation
- v2.11.0: 删除旧版build_ranks/composite_score/is_candidate及panel_score旧分支
"""
from __future__ import annotations

from typing import Optional

from collector.metrics import _daily_rets, _pstdev
from collector.recommendation import momentum_status

def watch_score_light(m: dict, navs: list = None, scale: Optional[float] = None) -> dict:
    """自选标准评分(线上版 watch_score_light 适配本地升序净值):
    无基础分:中长期35 + 近期25 + 回撤15 + 波动16 + 夏普9,封顶100,无规模分。
    m: calc_metrics dict; navs: 升序净值序列(用于回退夏普)。"""
    m1, m3, m6, y1 = m.get("m1"), m.get("m3"), m.get("m6"), m.get("y1")
    d3, d5, d7, d10 = m.get("d3"), m.get("d5"), m.get("d7"), m.get("d10")
    mdd_val, vol = m.get("mdd"), m.get("vol")
    sharpe = m.get("sharpe")
    if sharpe is None and navs and len(navs) > 20:
        rets = _daily_rets(navs)
        if len(rets) >= 20:
            mean = sum(rets) / len(rets)
            sd = _pstdev(rets)
            if sd > 0:
                sharpe = (mean * 252 - 0.02) / (sd * (252 ** 0.5))
    score = 0.0
    if m1 is not None:
        score += min(max((m1 + 3) / 12, 0), 1) * 12
    if m3 is not None:
        score += min(max(m3 / 15, 0), 1) * 13
    if m6 is not None:
        score += min(max(m6 / 25, 0), 1) * 10

    def _short(k, cap, w):
        return min(max((k or 0) / cap, 0), 1) * w if k is not None else 0.0

    score += _short(d3, 5, 4) + _short(d5, 8, 6) + _short(d7, 10, 7) + _short(d10, 15, 8)
    if mdd_val is not None:
        score += max(1 + mdd_val / 12, 0) * 15
    if vol is not None:
        score += max(1 - vol / 28, 0) * 16
    if sharpe is not None and sharpe > 0:
        score += min(sharpe, 2) * 4.5
    score = max(0, min(100, round(score)))
    return {"score": score, "m1": m1, "m3": m3, "m6": m6, "y1": y1,
            "mdd": mdd_val, "vol": vol, "sharpe": sharpe,
            "d3": d3, "d5": d5, "d7": d7, "d10": d10,
            "dd20": m.get("dd20"), "dn7": m.get("dn7"), "max_daily_drop_7d": m.get("max_daily_drop_7d"),
            "ms": momentum_status(m)}


# ---------------------------------------------------------------------------
# 面板特定综合分
# ---------------------------------------------------------------------------

def panel_score(sub: str, f: dict, sec_m5_map: dict) -> Optional[int]:
    """面板特定综合分。v2.11.0: 仅保留自选算法榜单分支，其余走默认score。

    自选算法榜单: watch_score_light(中长期35+近期25+回撤15+波动16+夏普9)
    """
    m = f.get("metrics") or {}
    # v2.11.4 B1: rank_config 正式名是 "自选", 兼容旧名 "自选算法榜单"
    # (已废弃但历史 snapshot 可能存在)
    if sub in ("自选", "自选算法榜单"):
        wsl = watch_score_light(m, f.get("navs"))
        return wsl.get("score") if isinstance(wsl, dict) else None
    return f.get("score")
