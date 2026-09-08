# -*- coding: utf-8 -*-
"""
market_research.py —— 指数技术研判模块（v0.39）

需求: 市场页「指数研判」二级Tab
- 三级Tab: 上证指数(sh000001) / 创业板指(sz399006) / 科创50(sh000688)
- 详情卡片: K线(分时/日/周/月) + 四段分析(技术形态/量能/当日盘面/短线预判) + 情绪标签
- 更新: 交易日 12:00(午盘) / 15:10(收盘) lazy 运算, 缓存 key = code_date_slot
- 情绪判定: ⚠情绪驱动 / ✓资金驱动 / —震荡整理(量化标准见需求文档第五节)

数据源: 腾讯财经K线接口(web.ifzq.gtimg.cn), 后端拉取统一缓存
"""
from __future__ import annotations

import json
import math
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# 三个研判指数: 名称 -> 腾讯代码
RESEARCH_INDICES = {
    "sh000001": "上证指数",
    "sz399006": "创业板指",
    "sh000688": "科创50",
}

# 午盘/收盘更新时间节点(时,分)
SLOT_NOON = (12, 0)
SLOT_CLOSE = (15, 10)


def _http(url: str, timeout: int = 10) -> str:
    import httpx
    resp = httpx.get(url, headers={
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X)",
        "Referer": "https://gu.qq.com/",
    }, timeout=timeout)
    return resp.text


def _get_kline(tx_code: str, period: str, count: int = 130) -> list[list]:
    """腾讯 fqkline: period in (day/week/month)。返回 [[date,open,close,high,low,vol], ...]"""
    url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param="
           f"{tx_code},{period},,,{count},qfq")
    d = json.loads(_http(url))
    node = d.get("data", {}).get(tx_code, {})
    rows = node.get(period, []) or node.get("qfq" + period, [])
    return rows


def _get_minute(tx_code: str) -> tuple[list, dict]:
    """腾讯分时。返回 (trends, quote)。trends 每条: 'HHMM 价 量 额'。"""
    url = f"https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={tx_code}"
    d = json.loads(_http(url))
    node = d.get("data", {}).get(tx_code, {})
    raw = node.get("data", {})
    trends = raw.get("data", []) if isinstance(raw, dict) else []
    qt = raw.get("qt", {}) if isinstance(raw, dict) else node.get("qt", {})
    return trends, qt


# ---------------- 技术指标 ----------------
def _ma(vals: list[float], n: int) -> list[float | None]:
    out = []
    s = 0.0
    for i, v in enumerate(vals):
        s += v
        if i >= n:
            s -= vals[i - n]
        out.append(s / n if i >= n - 1 else None)
    return out


def _ema(vals: list[float], n: int) -> list[float]:
    k = 2.0 / (n + 1)
    out = []
    e = None
    for v in vals:
        e = v if e is None else v * k + e * (1 - k)
        out.append(e)
    return out


def _macd(closes: list[float], fast=12, slow=26, sig=9) -> dict:
    """返回 {'dif':[], 'dea':[], 'hist':[]} 与最新状态。"""
    ef = _ema(closes, fast)
    es = _ema(closes, slow)
    dif = [a - b for a, b in zip(ef, es)]
    dea = _ema(dif, sig)
    hist = [2 * (a - b) for a, b in zip(dif, dea)]
    return {"dif": dif, "dea": dea, "hist": hist}


def _kdj(closes: list[float], highs: list[float], lows: list[float], n=9) -> dict:
    k, d = 50.0, 50.0
    ks, ds, js = [], [], []
    for i in range(len(closes)):
        lo = min(lows[max(0, i - n + 1): i + 1])
        hi = max(highs[max(0, i - n + 1): i + 1])
        rsv = 50.0 if hi == lo else (closes[i] - lo) / (hi - lo) * 100
        k = 2 / 3 * k + 1 / 3 * rsv
        d = 2 / 3 * d + 1 / 3 * k
        j = 3 * k - 2 * d
        ks.append(k); ds.append(d); js.append(j)
    return {"k": ks, "d": ds, "j": js}


def _kline_shape(o, c, h, l) -> str | None:
    """K线组合形态识别: 十字星/锤头/吞没需前一日数据, 这里识别单日形态。"""
    body = abs(c - o)
    rng = max(h - l, 1e-9)
    up_shadow = h - max(o, c)
    dn_shadow = min(o, c) - l
    if body / max(c, 1e-9) < 0.0015:
        return "十字星"
    if dn_shadow > 2.2 * max(body, 1e-9) and up_shadow < 0.6 * body:
        return "锤头线"
    if up_shadow > 2.2 * max(body, 1e-9) and dn_shadow < 0.6 * body:
        return "上吊线/射击之星"
    return None


# ---------------- 情绪判定 ----------------
def _judge_emotion(day_rows: list, minute: list, close_pct: float) -> dict:
    """按需求文档第五节量化判定。
    day_rows: 日K列表(旧->新) [[date,o,c,h,l,vol],...]; minute: 分时 trends。
    返回 {emotion:'fund'/'mood'/'flat', label:'资金驱动'/'情绪驱动'/'震荡整理', icon:'✓'/'⚠'/'—', rules:[命中描述], strong:bool}
    """
    n = len(day_rows)
    cur = day_rows[-1]
    close, vol = float(cur[2]), float(cur[5])
    prev = day_rows[-2] if n >= 2 else cur
    prev_close = float(prev[2])
    # 近5日均量(不含当日)
    vols5 = [float(r[5]) for r in day_rows[-6:-1]]
    avg5 = sum(vols5) / len(vols5) if vols5 else vol
    vol_ratio = vol / avg5 if avg5 else 1.0
    # 尾盘30分钟涨幅占比(分时最后约30分钟)
    tail_ratio = None
    if len(minute) >= 4:
        head = float(minute[-4].split()[1])
        tail = float(minute[-1].split()[1])
        if head:
            tail_ratio = (tail - head) / head * 100
    # 条件
    conds = []
    # 1) 短期急拉: 单日涨幅>3% 且 尾盘集中
    c1 = close_pct > 3 and (tail_ratio is not None and tail_ratio > close_pct * 0.4)
    # 2) 量价背离: 上涨但量未放大(低于近5日均量)
    c2 = close_pct > 0.5 and vol_ratio < 1.0
    # 3) 板块广度差: 近似——涨幅大但量能不足/波动率大(无全市场家数数据, 用价量特征近似)
    c3 = close_pct > 1.5 and vol_ratio < 0.8
    # 4) 缺乏持续性: 前一交易日下跌
    c4 = n >= 3 and (float(day_rows[-2][2]) - float(day_rows[-3][2])) / float(day_rows[-3][2]) * 100 < 0
    mood_hits = [x for i, x in enumerate([c1, c2, c3, c4]) if x]
    # 资金驱动条件(全部)
    f1 = 1 <= close_pct <= 3
    f2 = vol_ratio >= 1.05
    f3 = close_pct > 0 and vol_ratio >= 1.0 and n >= 3 and (float(day_rows[-2][2]) - float(day_rows[-3][2])) / float(day_rows[-3][2]) * 100 >= -0.3
    f4 = n >= 4 and sum(1 for r in day_rows[-4:] if float(r[2]) > float(r[1])) >= 2  # 近4日多数收阳(温和持续)
    if len(mood_hits) >= 2:
        return {"emotion": "mood", "label": "情绪驱动", "icon": "⚠",
                "rules": mood_hits, "strong": True,
                "desc": "该行情由短期情绪驱动，持续性较弱，后续回调风险较高，需警惕追高风险"}
    if f1 and f2 and f3 and f4:
        return {"emotion": "fund", "label": "资金驱动", "icon": "✓",
                "rules": [], "strong": False,
                "desc": "资金驱动行情，价量配合良好，板块联动性强，持续性相对较好"}
    if abs(close_pct) < 0.5 and vol_ratio < 1.0:
        return {"emotion": "flat", "label": "震荡整理", "icon": "—",
                "rules": [], "strong": False,
                "desc": "震荡整理，无明显驱动因素，方向未明，观望为主"}
    return {"emotion": "flat", "label": "震荡整理", "icon": "—",
            "rules": [], "strong": False,
            "desc": "震荡整理，无明显驱动因素，方向未明，观望为主"}


# ---------------- 四段分析 ----------------
def _minute_avg(minute: list) -> float | None:
    """分时均价 = 累计成交额 / 累计成交量。trends 每条: 'HHMM 价 量 额'。"""
    tot_amt, tot_vol = 0.0, 0.0
    for line in minute:
        parts = line.split()
        if len(parts) < 4:
            continue
        try:
            tot_amt += float(parts[3])
            tot_vol += float(parts[2])
        except (ValueError, IndexError):
            continue
    return tot_amt / tot_vol if tot_vol > 0 else None


def _gen_analysis(name: str, day_rows: list, minute: list, emotion: dict) -> dict:
    closes = [float(r[2]) for r in day_rows]
    highs = [float(r[3]) for r in day_rows]
    lows = [float(r[4]) for r in day_rows]
    vols = [float(r[5]) for r in day_rows]
    ma5 = _ma(closes, 5); ma10 = _ma(closes, 10); ma20 = _ma(closes, 20)
    macd = _macd(closes); kd = _kdj(closes, highs, lows)
    i = len(closes) - 1
    c, o, h, l = closes[i], float(day_rows[i][1]), highs[i], lows[i]
    prev_c = closes[i - 1] if i >= 1 else c
    pct = (c - prev_c) / prev_c * 100 if prev_c else 0.0
    # 支撑压力
    sup5 = ma5[i]; sup_lo10 = min(lows[-10:]) if len(lows) >= 10 else min(lows)
    prs20 = ma20[i]; prs_hi20 = max(highs[-20:]) if len(highs) >= 20 else max(highs)
    support = min(sup5 or sup_lo10, sup_lo10)
    pressure = max(prs20 or prs_hi20, prs_hi20)
    shape = _kline_shape(o, c, h, l)
    vol_now = vols[i]; vol5 = sum(vols[-6:-1]) / 5 if len(vols) >= 6 else vol_now
    vol_ratio = vol_now / vol5 if vol5 else 1.0

    # ---- 第一段 技术形态 ----
    parts1 = []
    if ma5[i] and ma10[i] and ma20[i]:
        if ma5[i] > ma10[i] > ma20[i]:
            parts1.append("均线呈多头排列（5日>10日>20日），短期趋势偏强")
        elif ma5[i] < ma10[i] < ma20[i]:
            parts1.append("均线呈空头排列（5日<10日<20日），短期趋势偏弱")
        else:
            parts1.append("均线交叉缠绕，方向未明")
        # 均线斜率(5日均线近5日变化) → 趋势强度
        if ma5[i - 5] is not None and ma5[i] is not None:
            slope = (ma5[i] - ma5[i - 5]) / ma5[i - 5] * 100
            if slope > 1:
                parts1.append("5日均线快速上翘（近5日+%.1f%%），短线动能充沛" % slope)
            elif slope < -1:
                parts1.append("5日均线快速下行（近5日%.1f%%），短线仍在寻底" % slope)
        # 价格相对20日均线位置
        gap20 = (c - ma20[i]) / ma20[i] * 100 if ma20[i] else 0
        if gap20 > 5:
            parts1.append("现价高于20日均线%.1f%%，偏离度偏高，存在均线回归牵引" % gap20)
        elif gap20 < -5:
            parts1.append("现价低于20日均线%.1f%%，超跌后存在技术性修复需求" % abs(gap20))
    dif, dea, hist = macd["dif"][i], macd["dea"][i], macd["hist"][i]
    if dif is not None and dea is not None:
        if dif > dea:
            parts1.append("MACD位于零轴上方金叉运行" if dif > 0 else "MACD水下金叉，反弹性质待确认")
        else:
            parts1.append("MACD死叉运行" + ("，绿柱仍在" if hist and hist < 0 else ""))
        # 柱体变化: 红柱扩大/缩小
        if i >= 2 and hist is not None:
            h_prev = macd["hist"][i - 1]
            if hist > 0 and h_prev is not None:
                parts1.append("红柱%s，多头动能%s" % ("持续放大" if hist > h_prev else "开始收窄", "增强" if hist > h_prev else "衰减"))
            elif hist < 0 and h_prev is not None:
                parts1.append("绿柱%s，空头动能%s" % ("持续放大" if hist < h_prev else "开始收窄", "增强" if hist < h_prev else "衰减"))
    kk, dd = kd["k"][i], kd["d"][i]
    if kk is not None and dd is not None:
        if kk > 80:
            parts1.append("KDJ处于超买区（K>80），短线追高风险需警惕")
        elif kk < 20:
            parts1.append("KDJ处于超卖区（K<20），存在技术性修复需求")
        elif kk > dd:
            parts1.append("KDJ金叉向上")
        else:
            parts1.append("KDJ死叉向下")
    # 近5/10/20日区间表现与位置
    if len(closes) >= 21:
        hi20, lo20 = max(highs[-20:]), min(lows[-20:])
        pos20 = (c - lo20) / (hi20 - lo20) * 100 if hi20 > lo20 else 50
        r20 = (c - closes[-21]) / closes[-21] * 100
        parts1.append("近20日区间涨幅%+.1f%%，现价处于区间%s位（%d%%分位）" % (
            r20, "偏高" if pos20 >= 70 else ("偏低" if pos20 <= 30 else "中部"), round(pos20)))
    parts1.append(f"短期支撑参考 {support:.2f}（5日均线/近10日低点），短期压力参考 {pressure:.2f}（20日均线/近20日高点）")
    if shape:
        parts1.append(f"K线呈{shape}形态，多空分歧或加大")
    # 吞没判断
    if i >= 1:
        po, pc = float(day_rows[i - 1][1]), float(day_rows[i - 1][2])
        if pc > po and c < o and c < pc and o > po:
            parts1.append("当日阴线实体吞没前日阳线，短线走弱信号需关注")
        elif pc < po and c > o and c > pc and o < po:
            parts1.append("当日阳线实体吞没前日阴线，短线走强信号需关注")

    # ---- 第二段 量能 ----
    parts2 = []
    if vol_ratio >= 1.2:
        parts2.append(f"当日成交量约为近5日均量的 {vol_ratio:.1f} 倍，呈放量状态")
    elif vol_ratio <= 0.8:
        parts2.append(f"当日成交量约为近5日均量的 {vol_ratio:.1f} 倍，呈缩量状态")
    else:
        parts2.append(f"当日成交量与近5日均量基本持平（{vol_ratio:.1f} 倍）")
    if pct > 0.5 and vol_ratio >= 1.05:
        parts2.append("价量配合良好（上涨放量），动能较为良性")
    elif pct > 0.5 and vol_ratio < 1.0:
        parts2.append("上涨伴随缩量，价量存在一定背离，持续性存疑")
    elif pct < -0.5 and vol_ratio >= 1.2:
        parts2.append("下跌伴随放量，抛压较重，短线或仍承压")
    # 量能趋势: 近3日量能连续放大/萎缩
    if len(vols) >= 5:
        v3 = vols[-3:]
        up_cnt = sum(1 for a, b in zip(v3, v3[1:]) if b > a)
        dn_cnt = sum(1 for a, b in zip(v3, v3[1:]) if b < a)
        if up_cnt >= 2:
            parts2.append("近3日量能阶梯式放大，资金活跃度持续升温")
        elif dn_cnt >= 2:
            parts2.append("近3日量能持续萎缩，交投趋于清淡")
    # 量价背离(新高缩量/新低放量)
    if len(closes) >= 5 and c >= max(closes[-5:-1]) and vol_ratio < 0.9:
        parts2.append("价格创近几日新高但量能未同步放大，需警惕冲高回落")
    if len(closes) >= 5 and c <= min(closes[-5:-1]) and vol_ratio > 1.3:
        parts2.append("价格创近几日新低且量能放大，短线情绪偏弱")

    # ---- 第三段 当日盘面 ----
    emo = emotion
    parts3 = [emo["desc"]]
    # 情绪规则命中项具体化
    _rule_txt = {
        0: "盘中出现短期急拉（涨幅>3%且尾盘集中放量），情绪脉冲特征明显",
        1: "上涨但量能未同步放大（低于近5日均量），量价配合欠佳",
        2: "涨幅较大但量能明显不足（不足均量8成），上攻基础薄弱",
        3: "前一交易日下跌，缺乏连续上攻的持续性",
    }
    for _idx, _hit in enumerate(emo.get("rules") or []):
        if _hit:
            parts3.append(_rule_txt.get(_idx, ""))
    # 分时特征: 均价线上/下 + 尾盘强弱
    _avg = _minute_avg(minute)
    if minute:
        _last_px = float(minute[-1].split()[1])
        if _avg:
            if _last_px > _avg:
                parts3.append("分时价格全天运行于均价线上方，买盘相对占优")
            else:
                parts3.append("分时价格多数时间位于均价线下方，卖压相对占优")
        # 尾盘30分钟 vs 早盘30分钟
        if len(minute) >= 60:
            _head = float(minute[30].split()[1])
            _tail = float(minute[-1].split()[1])
            if _head:
                _tr = (_tail - _head) / _head * 100
                if _tr >= 0.3:
                    parts3.append("尾盘走势强于早盘（尾段%+.2f%%），承接力量偏强" % _tr)
                elif _tr <= -0.3:
                    parts3.append("尾盘走弱（尾段%+.2f%%），冲高乏力迹象明显" % _tr)
    if emo["emotion"] == "mood":
        parts3.append("⚠ 该行情由短期情绪驱动，持续性较弱，后续回调风险较高，需警惕追高风险")
    elif emo["emotion"] == "fund":
        parts3.append("关注价量配合的延续性与板块轮动节奏，若量能维持则走势有望延续")

    # ---- 第四段 短线预判 ----
    parts4 = []
    score = 0.0
    if ma5[i] and ma20[i]:
        score += 1 if ma5[i] > ma20[i] else -1
    if dif is not None and dea is not None:
        score += 1 if dif > dea else -1
    score += 0.5 if pct > 0 else -0.5
    # 支撑/压力距离
    _dist_s = (c - support) / support * 100 if support else 0
    _dist_p = (pressure - c) / c * 100 if c else 0
    if emo["emotion"] == "mood":
        parts4.append("短期情绪脉冲后，冲高回落概率偏大，需警惕追高风险，宜观望为主")
    elif emo["emotion"] == "fund":
        parts4.append("价量配合与板块联动延续下，震荡上行可能性较高，但需防范高位分歧")
    elif score >= 1:
        parts4.append("技术面偏强，短线震荡上行可能性较高，压力位附近需留意反复")
    elif score <= -1:
        parts4.append("技术面偏弱，短线承压调整概率偏大，关注支撑位有效性")
    else:
        parts4.append("多空力量相对均衡，短线大概率维持区间震荡，方向选择仍需等待")
    parts4.append("情景参考：有效站稳并放量突破压力位 %.2f（现价上方 %.1f%%）则打开上行空间；"
                 "若跌破支撑 %.2f（现价下方 %.1f%%）则调整压力加大" % (pressure, _dist_p, support, _dist_s))

    return {
        "tech": "；".join(parts1),
        "volume": "；".join(parts2),
        "market": "；".join(parts3),
        "trend": "；".join(parts4),
        "support": round(support, 2),
        "pressure": round(pressure, 2),
        "emotion": emo["emotion"],
        "emotion_label": emo["label"],
        "emotion_icon": emo["icon"],
        "emotion_strong": emo["strong"],
    }


# ---------------- 缓存与主入口 ----------------
def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(ROOT / "fund.db"))
    conn.row_factory = sqlite3.Row
    return conn


def init_table():
    conn = _db()
    conn.execute("""CREATE TABLE IF NOT EXISTS market_research (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT, date TEXT, slot TEXT, data TEXT, ts INTEGER,
        UNIQUE(code, date, slot))""")
    conn.commit()
    conn.close()


def _is_trading_day(dt: datetime, kline_last_date: str) -> bool:
    """以K线最新日期近似交易日: 若K线最后日期==今天则今天是交易日(已出数据)。"""
    return kline_last_date == dt.strftime("%Y-%m-%d")


def _current_slot(dt: datetime) -> str:
    """当前应归属时段: 收盘(>=15:10) / 午盘(>=12:00) / 早盘(其他=沿用昨日收盘)。"""
    hm = dt.hour * 60 + dt.minute
    if hm >= SLOT_CLOSE[0] * 60 + SLOT_CLOSE[1]:
        return "close"
    if hm >= SLOT_NOON[0] * 60 + SLOT_NOON[1]:
        return "noon"
    return "early"


def get_research(tx_code: str, force: bool = False) -> dict:
    """指数研判主入口: 拉K线→指标→四段分析→情绪→缓存(lazy)。"""
    init_table()
    name = RESEARCH_INDICES.get(tx_code, tx_code)
    now = datetime.now()
    # 1) 拉日K判断交易日
    day = _get_kline(tx_code, "day", 130)
    if len(day) < 20:
        return {"ok": False, "msg": "K线数据不足"}
    last_date = day[-1][0]
    slot = "close" if now.hour >= 15 and now.minute >= 10 else ("noon" if now.hour >= 12 else "early")
    today = now.strftime("%Y-%m-%d")
    # 非交易日(今天K线未更新) → 沿用最近交易日收盘缓存
    if not _is_trading_day(now, last_date):
        slot = "close"
    conn = _db()
    row = conn.execute("SELECT data FROM market_research WHERE code=? AND date=? AND slot=?",
                       (tx_code, last_date, slot)).fetchone()
    if row and not force:
        conn.close()
        out = json.loads(row["data"])
        out["cached"] = True
        return out
    # 2) 全周期K线 + 分时
    week = _get_kline(tx_code, "week", 60)
    month = _get_kline(tx_code, "month", 36)
    minute, qt = _get_minute(tx_code)
    if len(week) < 8 or len(month) < 6:
        week = _get_kline(tx_code, "week", 60)
        month = _get_kline(tx_code, "month", 36)
    # 3) 当日涨幅与情绪
    prev_close = float(day[-2][2]) if len(day) >= 2 else float(day[-1][1])
    close_now = float(day[-1][2])
    pct = (close_now - prev_close) / prev_close * 100 if prev_close else 0.0
    emotion = _judge_emotion(day, minute, pct)
    # 4) 四段分析
    ana = _gen_analysis(name, day, minute, emotion)
    # 5) 组装返回
    kline = {
        "day": _kline_for_chart(day, "day"),
        "week": _kline_for_chart(week, "week"),
        "month": _kline_for_chart(month, "month"),
        "minute": _minute_for_chart(minute, close_now),
    }
    result = {
        "ok": True,
        "code": tx_code,
        "name": name,
        "point": round(close_now, 2),
        "pct": round(pct, 2),
        "last_date": last_date,
        "slot": slot,
        "generated": now.strftime("%Y-%m-%d %H:%M") + ("（午盘）" if slot == "noon" else "（收盘）"),
        "emotion": ana["emotion"], "emotion_label": ana["emotion_label"],
        "emotion_icon": ana["emotion_icon"], "emotion_strong": ana["emotion_strong"],
        "analysis": {
            "tech": ana["tech"], "volume": ana["volume"],
            "market": ana["market"], "trend": ana["trend"],
        },
        "support": ana["support"], "pressure": ana["pressure"],
        "kline": kline,
    }
    conn.execute("INSERT OR REPLACE INTO market_research(code,date,slot,data,ts) VALUES(?,?,?,?,?)",
                 (tx_code, last_date, slot, json.dumps(result, ensure_ascii=False), int(time.time())))
    conn.commit()
    conn.close()
    result["cached"] = False
    return result


def _kline_for_chart(rows: list, period: str) -> list[dict]:
    """转为图表友好结构: [{time:'2026-08-21', open, high, low, close, volume}]"""
    out = []
    for r in rows:
        try:
            out.append({
                "time": r[0], "open": float(r[1]), "close": float(r[2]),
                "high": float(r[3]), "low": float(r[4]), "volume": float(r[5]),
            })
        except Exception:
            continue
    return out


def _minute_for_chart(minute: list, last_close: float) -> list[dict]:
    """分时: [{time:'09:30', price, avg, volume}]"""
    out = []
    prev = last_close
    for i, line in enumerate(minute):
        parts = line.split()
        if len(parts) < 3:
            continue
        hm = parts[0]
        hm = f"{hm[:2]}:{hm[2:]}"
        price = float(parts[1])
        vol = float(parts[2])
        out.append({"time": hm, "price": price, "volume": vol,
                    "pct": round((price - prev) / prev * 100, 2) if prev else 0})
        prev = price
    return out


def get_tabs() -> list[dict]:
    """三级Tab概览: 三个指数的 点位/涨跌幅/情绪标签(实时行情 + 研判缓存情绪)。"""
    out = []
    try:
        from modules.market import market_service as market  # v2.8.1 修复: 原 market.py 已在模块化重构中移除
        d = market.get_market_indices()
        markets = d.get("markets", {})
        a_share = markets.get("A股", [])
        m = {x.get("name"): x for x in a_share}
    except Exception:
        m = {}
    conn = _db()
    for code, name in RESEARCH_INDICES.items():
        it = m.get(name, {})
        emo, emo_label, emo_icon = None, "—", ""
        row = conn.execute(
            "SELECT data FROM market_research WHERE code=? ORDER BY ts DESC LIMIT 1",
            (code,)).fetchone()
        if row:
            try:
                rd = json.loads(row["data"])
                emo = rd.get("emotion")
                emo_label = rd.get("emotion_label") or "—"
                emo_icon = rd.get("emotion_icon") or ""
            except Exception:
                pass
        out.append({
            "code": code, "name": name,
            "point": it.get("point"), "pct": it.get("pct"),
            "emotion": emo, "emotion_label": emo_label, "emotion_icon": emo_icon,
        })
    conn.close()
    return out


def init():
    init_table()
