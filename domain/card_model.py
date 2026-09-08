"""
统一基金卡片数据模型 (CardModel) — v0.76.2 组件化重构

消除服务端 rendering/fund_page.py:_fund_card 与前端 watch.js:watchCard
两套独立渲染逻辑的字段别名、信号映射、标签格式差异。

CardModel 是标准化的中间表示:
  原始数据(funds表 / WATCH_DATA / rank_snapshots.meta)
    -> build_card_model() 归一化
    -> 渲染层(_fund_card / watchCard)只消费标准字段
"""
from __future__ import annotations

# 字段别名映射: 旧字段名 -> 标准字段名
FIELD_ALIASES = {
    "d1": ["rzf"], "d2": ["c2d"], "d3": ["c3d"], "d5": ["m5"],
    "d7": ["m7"], "d10": ["m10"], "m1": ["n1"], "m3": ["n3"],
    "m6": ["n6"], "y1": ["n1y"],
    # v2.11.2: dist20h(距20日高) 仅接受真实20日口径 dd20, 不再回退 dd_from_hi(距序列高点),
    # 避免把"距近一年最高点"错标为"距20日高"
    "dist20h": ["dd20"], "mdd": ["mdd"],
    "streak": ["lz"],
}

# 推荐信号7档颜色映射 (dot, bg, text)
SIGNAL_COLORS = {
    "买入": ("#DC2626", "#DC262615", "#DC2626"),
    "布局": ("#DC2626", "#DC262615", "#DC2626"),
    "轻仓": ("#F97316", "#F9731615", "#EA580C"),
    "试探": ("#F59E0B", "#F59E0B15", "#D97706"),
    "观望": ("#64748B", "#E2E8F0", "#334155"),
    "回避": ("#34D399", "#34D39915", "#059669"),
    "清仓": ("#059669", "#05966915", "#059669"),
}
SIGNAL_COLORS_DEFAULT = ("#64748B", "#E2E8F0", "#334155")

# 动能4档 CSS class (v2.11.1: 强劲/维持/衰减/走弱)
MOMENTUM_CLASSES = {
    "强劲": "ms-strong", "维持": "ms-watch",
    "衰减": "ms-decay", "走弱": "ms-fail",
}
MOMENTUM_CLASS_DEFAULT = "ms-fail"

# 阶段涨幅配置: 上行7项 + 下行5项
PERF_UP_FIELDS = [
    ("d1", "1日"), ("d2", "2日"), ("d3", "3日"), ("d5", "5日"), ("d7", "7日"),
    ("d10", "2周"), ("m1", "1月"), ("m3", "3月"),
]
PERF_DOWN_FIELDS = [
    ("m6", "6月"), ("y1", "1年"), ("dist20h", "距20日高"),
    ("streak", "连涨"),
]


def _normalize_fields(p):
    p = dict(p) if p else {}
    for std, aliases in FIELD_ALIASES.items():
        if std not in p:
            for alias in aliases:
                if alias in p:
                    p[std] = p[alias]
                    break
    return p


def _parse_themes(themes):
    """解析主题标签: 支持 list of dict / list of str / str(JSON) / str(逗号分隔)。"""
    if not themes:
        return []
    if isinstance(themes, list):
        result = []
        for t in themes:
            if isinstance(t, dict):
                # {'name': '有色/资源', 'pct': 28.7} -> '有色/资源28.7%'
                name = str(t.get("name") or t.get("theme") or "").strip()
                pct = t.get("pct") or t.get("percent")
                if name:
                    result.append(f"{name}{pct}%" if pct is not None else name)
            elif isinstance(t, (list, tuple)) and len(t) >= 2:
                # ['有色/资源', 28.7] -> '有色/资源28.7%'
                name = str(t[0]).strip()
                pct = t[1]
                if name:
                    result.append(f"{name}{pct}%" if pct is not None else name)
            else:
                s = str(t).strip()
                if s:
                    result.append(s)
        return result
    if isinstance(themes, str):
        s = themes.strip()
        if not s:
            return []
        if s.startswith("["):
            try:
                import json
                arr = json.loads(s)
                if isinstance(arr, list):
                    return _parse_themes(arr)
            except Exception:
                pass
        return [t.strip() for t in s.replace("、", ",").split(",") if t.strip()]
    return []


def _parse_rank_tags(cros=None, rtag=None):
    raw = cros or rtag
    if not raw:
        return []
    s = str(raw).replace("同榜", "").strip()
    return [t.strip() for t in s.split("·") if t.strip()]


def _safe_num(v):
    if v is None or v == "":
        return None
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def _int_if_whole(v):
    if v is not None and v == int(v):
        return int(v)
    return v


def build_card_model(fund, rank=None, board=None):
    """把原始基金数据转换为标准化 CardModel dict。"""
    p = _normalize_fields(fund)
    code = str(p.get("code", "")).strip()
    name = str(p.get("name") or code or "").strip()

    score = _safe_num(p.get("score"))
    ad_score = _safe_num(p.get("ad_score"))
    earn_score = _safe_num(p.get("earn_score"))

    momentum = p.get("ms") or None
    momentum_cls = MOMENTUM_CLASSES.get(momentum, MOMENTUM_CLASS_DEFAULT) if momentum else None

    risk = {
        "mdd": _safe_num(p.get("mdd")),
        "vol": _safe_num(p.get("vol")),
        "calmar": _safe_num(p.get("calmar")),
    }

    signal = {
        "current": p.get("reco") or None,
        "current_days": p.get("reco_days"),
        "previous": p.get("prev_reco") or None,
        "previous_days": p.get("prev_reco_days"),
    }
    signal["colors"] = SIGNAL_COLORS.get(signal["current"] or "", SIGNAL_COLORS_DEFAULT)

    perf_up = []
    for field, label in PERF_UP_FIELDS:
        v = _safe_num(p.get(field))
        perf_up.append({
            "label": label, "value": v,
            "cls": "up" if (v is not None and v >= 0) else ("down" if v is not None else ""),
        })

    perf_down = []
    for field, label in PERF_DOWN_FIELDS:
        v = _safe_num(p.get(field))
        if field == "streak":
            unit = "日"
            cls = "up" if (v is not None and v > 0) else ""
        else:
            unit = "%"
            cls = "up" if (v is not None and v >= 0) else ("down" if v is not None else "")
        perf_down.append({"label": label, "value": v, "cls": cls, "unit": unit})

    themes = _parse_themes(p.get("themes"))
    # v2.11.4 C2/Q8: board_days 全链路下线, 不再解析也不再输出
    rank_tags = _parse_rank_tags(p.get("cros"), p.get("rtag"))

    return {
        "code": code, "name": name, "rank": rank, "board": board,
        "score": _int_if_whole(score),
        "ad_score": _int_if_whole(ad_score),
        "earn_score": _int_if_whole(earn_score),
        "momentum": momentum, "momentum_cls": momentum_cls,
        "risk": risk, "signal": signal,
        "perf_up": perf_up, "perf_down": perf_down,
        "themes": themes, "rank_tags": rank_tags,
    }
