"""
services/compare.py — 双基金对比服务

从 main.py 拆分，负责自选页双基金对比的核心计算逻辑：
- 双基金净值序列/统计指标/抗跌数据/大涨数据/增强字段计算
- 七段式对比总结文本生成
- 大跌日/大涨日明细双基金整合

调用入口：watch_compare_sync(codes: list[str]) -> {ok, funds: [...], summary: str}
"""
from __future__ import annotations

import re
from typing import Optional

import db
from modules.fund.fund_detail import stats_full as _stats_full
# 延迟导入避免循环导入（anti → common → anti）
# from modules.anti.anti_detail import _resist, _rise, _enhance_detail, _load_nfm, _calc_merge, _name_of
from modules.fund.fund_service import load_idx as _load_idx, get_fund_full_data
from modules.nav.nav_series import series, inject_long_metrics


def _fmt(v, digits=2):
    if v is None:
        return "—"
    return f"{v:.{digits}f}%"


def _build_compare_summary(funds: list) -> str:
    """七段式对比总结：收益表现/波动回撤/结构/上涨阶段/下跌阶段/大跌后修复/结论。"""
    if len(funds) < 2:
        return "请选择两只基金进行对比"
    a, b = funds[0], funds[1]
    lines = []
    lines.append(f"【收益表现】近2月：{a['name']} {_fmt(a['stats'].get('ret2m'))} vs {b['name']} {_fmt(b['stats'].get('ret2m'))}；"
                 f"近1月：{_fmt(a['stats'].get('m1'))} vs {_fmt(b['stats'].get('m1'))}；"
                 f"近2周：{_fmt(a['stats'].get('d10'))} vs {_fmt(b['stats'].get('d10'))}；"
                 f"近5日：{_fmt(a['stats'].get('d5'))} vs {_fmt(b['stats'].get('d5'))}。")
    lines.append(f"【波动与回撤】最大回撤：{a['name']} {_fmt(a['stats'].get('mdd'))} vs {b['name']} {_fmt(b['stats'].get('mdd'))}；"
                 f"单日最大跌：{_fmt(a['stats'].get('max_daily_drop'))} vs {_fmt(b['stats'].get('max_daily_drop'))}；"
                 f"波动率：{_fmt(a['stats'].get('vol'))} vs {_fmt(b['stats'].get('vol'))}；"
                 f"夏普比率：{a['stats'].get('sharpe', '—')} vs {b['stats'].get('sharpe', '—')}。")
    lines.append(f"【结构】上涨天数占比：{a['stats'].get('up_ratio', '—')}% vs {b['stats'].get('up_ratio', '—')}%；"
                 f"盈亏比：{a['stats'].get('pl', '—')} vs {b['stats'].get('pl', '—')}；"
                 f"创新高次数：{a['stats'].get('hi_cnt', '—')} vs {b['stats'].get('hi_cnt', '—')}。")
    a_rup = a.get('rise', {}) or {}
    b_rup = b.get('rise', {}) or {}
    lines.append(f"【上涨阶段表现】大涨日平均涨幅：{a['name']} {_fmt(a_rup.get('fund_avg'))} vs {b['name']} {_fmt(b_rup.get('fund_avg'))}；"
                 f"大盘大涨日均值：{_fmt(a_rup.get('idx_avg'))}。")
    a_rs = a.get('resist', {}) or {}
    b_rs = b.get('resist', {}) or {}
    lines.append(f"【下跌阶段表现】大跌日平均跌幅：{a['name']} {_fmt(a_rs.get('fund_avg'))} vs {b['name']} {_fmt(b_rs.get('fund_avg'))}；"
                 f"大盘大跌日均值：{_fmt(a_rs.get('idx_avg'))}；"
                 f"抗跌得分：{a_rs.get('score', '—')} vs {b_rs.get('score', '—')}。")
    lines.append(f"【大跌后修复】大跌后5日反弹：{a['name']} {_fmt(a_rs.get('repair'))} vs {b['name']} {_fmt(b_rs.get('repair'))}。")
    a_ret = a['stats'].get('ret2m') or 0
    b_ret = b['stats'].get('ret2m') or 0
    a_ad = a_rs.get('score') or 0
    b_ad = b_rs.get('score') or 0
    if a_ret > b_ret and a_ad >= b_ad:
        conclusion = f"综合来看，{a['name']} 在收益和抗跌两方面均优于 {b['name']}，整体表现更强。"
    elif b_ret > a_ret and b_ad >= a_ad:
        conclusion = f"综合来看，{b['name']} 在收益和抗跌两方面均优于 {a['name']}，整体表现更强。"
    elif a_ret > b_ret:
        conclusion = f"{a['name']} 收益更强，但 {b['name']} 抗跌更好；追求收益选前者，追求稳健选后者。"
    elif b_ret > a_ret:
        conclusion = f"{b['name']} 收益更强，但 {a['name']} 抗跌更好；追求收益选前者，追求稳健选后者。"
    else:
        conclusion = "两只基金收益和抗跌表现接近，可根据持仓风格和主题偏好选择。"
    lines.append(f"【结论】{conclusion}")
    return "\n".join(lines)


def _merge_down_days(funds: list) -> list:
    """大跌日明细双基金整合到一张表：日期/大盘/基金A/基金B。"""
    if len(funds) < 2:
        return []
    a_detail = (funds[0].get('resist', {}) or {}).get('detail', []) or []
    b_detail = (funds[1].get('resist', {}) or {}).get('detail', []) or []
    a_map = {d['date']: d for d in a_detail}
    b_map = {d['date']: d for d in b_detail}
    all_dates = sorted(set(list(a_map.keys()) + list(b_map.keys())))
    merged = []
    for d in all_dates:
        a_d = a_map.get(d, {})
        b_d = b_map.get(d, {})
        merged.append({
            "date": d,
            "idx": a_d.get("idx") if a_d.get("idx") is not None else b_d.get("idx"),
            "cyb": a_d.get("cyb") if a_d.get("cyb") is not None else b_d.get("cyb"),
            "kc": a_d.get("kc") if a_d.get("kc") is not None else b_d.get("kc"),
            "fund_a": a_d.get("fund"),
            "fund_b": b_d.get("fund"),
            "abnormal_a": a_d.get("abnormal", False),
            "abnormal_b": b_d.get("abnormal", False),
        })
    return merged


def _merge_up_days(funds: list) -> list:
    """大涨日明细双基金整合到一张表。"""
    if len(funds) < 2:
        return []
    a_detail = (funds[0].get('rise', {}) or {}).get('detail', []) or []
    b_detail = (funds[1].get('rise', {}) or {}).get('detail', []) or []
    a_map = {d['date']: d for d in a_detail}
    b_map = {d['date']: d for d in b_detail}
    all_dates = sorted(set(list(a_map.keys()) + list(b_map.keys())))
    merged = []
    for d in all_dates:
        a_d = a_map.get(d, {})
        b_d = b_map.get(d, {})
        merged.append({
            "date": d,
            "idx": a_d.get("idx") if a_d.get("idx") is not None else b_d.get("idx"),
            "cyb": a_d.get("cyb") if a_d.get("cyb") is not None else b_d.get("cyb"),
            "kc": a_d.get("kc") if a_d.get("kc") is not None else b_d.get("kc"),
            "fund_a": a_d.get("fund"),
            "fund_b": b_d.get("fund"),
        })
    return merged


def watch_compare_sync(codes: list[str]):
    # 延迟导入避免循环导入
    from modules.anti.anti_detail import _resist, _rise, _enhance_detail, _load_nfm, _calc_merge, _name_of
    """双基金对比同步计算（自选页对比面板数据源）。"""
    if not codes or len(codes) < 2:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="请选择至少两只基金进行对比")
    codes = [c for c in codes if re.fullmatch(r"\d{6}", c)]
    if len(codes) < 2:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="基金代码格式不正确")
    nfm = _load_nfm()
    idx_map, idx_rows = _load_idx()
    _items_idx = [(d, m) for d, m in idx_map.items() if isinstance(m, dict)]
    _ddown_local = [(d, m.get("sh", 0.0)) for d, m in sorted(_items_idx)
                    if m.get("sh", 0.0) <= -0.01 or m.get("cyb", 0.0) <= -0.04 or m.get("kc", 0.0) <= -0.04]
    if len(_ddown_local) < 3:
        _ddown_local = [(d, m.get("sh", 0.0)) for d, m in sorted(_items_idx)
                        if m.get("sh", 0.0) < 0 or m.get("cyb", 0.0) < 0 or m.get("kc", 0.0) < 0]
    funds = []
    for code in codes[:2]:
        dates, vals = series(code, days=120)
        if len(vals) < 2:
            continue
        st = _stats_full(vals)
        rs = _resist(dates, vals, idx_map)
        rup = _rise(dates, vals, idx_map)
        cm = _calc_merge(code, dates, vals, idx_map, _ddown_local)
        if st is not None and cm:
            if cm.get("up_cap") is not None:
                cm["up_capture"] = cm["up_cap"]
            if cm.get("dn_cap") is not None:
                cm["dn_capture"] = cm["dn_cap"]
            st.update({k: cm[k] for k in ("ret2m", "max_daily_drop", "mdd", "vol",
                                          "hi_cnt", "dd_from_hi", "pl", "pl_txt", "mdd_days", "mdd_status",
                                          "down_vol", "down_sharpe", "calmar", "up_capture", "dn_capture",
                                          "repair_tag") if k in cm})
            if cm.get("up_ratio") is not None:
                st["up_ratio"] = round(cm["up_ratio"] * 100, 1)
            st["r1m"] = cm.get("m1")
            st["r2w"] = cm.get("d10")
            st["r5d"] = cm.get("d5")
            for _k in ("d3", "d5", "d7", "d10", "m1", "m3", "ret2m"):
                if _k in cm and st.get(_k) is None:
                    st[_k] = cm[_k]
            if rs is not None:
                rs["repair_tag"] = cm.get("repair_tag")
                rs["up_capture"] = cm.get("up_capture")
                rs["dn_capture"] = cm.get("dn_capture")
        f = get_fund_full_data(code)
        inject_long_metrics(code, st)
        # v2026-09-02: 根治数据不一致问题 - 长期指标(m3/m6/y1/ret2m)统一从funds表取
        # 原因: nav_history只存储最近60天数据，不足以计算m6/y1；funds表是一键更新时从天天基金网拉取的完整数据
        # 效果: 弹窗/基金卡片/对比页面的长期指标完全一致
        if f:
            # v2.9.8: 扩展统一数据源范围，把更多指标也统一从funds表取，确保三个页面完全一致
            # 收益指标
            for _k in ("d1", "d2", "m1", "m3", "m6", "y1", "ret2m", "d3", "d5", "d7", "d10"):
                if f.get(_k) is not None:
                    st[_k] = f[_k]
            # 风险指标
            for _k in ("mdd", "mdd_days", "mdd_status", "vol", "down_vol", "down_sharpe", "calmar"):
                if f.get(_k) is not None:
                    st[_k] = f[_k]
            # 其他指标(v2.11.2: 加入 ms, 弹窗动能与卡片同源)
            for _k in ("pl", "hi_cnt", "dd_from_hi", "up_capture", "dn_capture", "up_ratio", "ms"):
                if f.get(_k) is not None:
                    st[_k] = f[_k]
        # v2.9.43: 全局统一使用 compute_scores_v2 算法
        # 一键更新阶段4 recompute_ad 对上榜+自选基金批量调用 compute_scores_v2 计算后写入数据库
        # 弹窗/对比页/卡片都从数据库读取同一套分数，确保100%一致
        ad_db = (f or {}).get("ad_score")
        er_db = (f or {}).get("earn_score")
        du_db = (f or {}).get("score")
        from modules.anti.anti_detail import _ad_tag
        tag = _ad_tag(ad_db)
        if rs is not None:
            rs["score"] = ad_db
            rs["tag"] = tag
        enh = _enhance_detail(code, dates, vals, st or {}, rs, rup, idx_map)
        funds.append({
            "code": code,
            "name": _name_of(code),
            "scale": (f or {}).get("scale"),
            "est": (f or {}).get("est"),
            "dates": dates,
            "navs": [round(v, 4) for v in vals],
            "nav_ret": [round((v / vals[0] - 1) * 100, 2) for v in vals] if vals and vals[0] else [],
            "stats": st,
            "resist": {
                "ad_score": ad_db, "earn_score": er_db, "dual": du_db, "tag": tag,
                "down_days": (rs or {}).get("down_days", 0),
                "detail": (rs or {}).get("detail") or [],
                "fund_avg": (rs or {}).get("fund_avg"),
                "idx_avg": (rs or {}).get("idx_avg"),
                "repair": (rs or {}).get("repair"),
                "repair_tag": (rs or {}).get("repair_tag"),
                "up_capture": (rs or {}).get("up_capture"),
                "dn_capture": (rs or {}).get("dn_capture"),
                "score": (rs or {}).get("score"),
            },
            "rise": rup or {"detail": [], "fund_avg": None, "idx_avg": None, "up_days": 0, "tag": "无大涨日"},
            "enhanced": enh,
        })
    if len(funds) < 2:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="有效基金不足两只")
    summary = _build_compare_summary(funds)
    down_days_merged = _merge_down_days(funds)
    up_days_merged = _merge_up_days(funds)
    return {
        "ok": True,
        "funds": funds,
        "summary": summary,
        "down_days_merged": down_days_merged,
        "up_days_merged": up_days_merged,
        "index_note": "大跌日 = 上证≤-1% 或 创业板指≤-4% 或 科创50≤-4%(任一满足)",
    }




