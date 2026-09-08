# -*- coding: utf-8 -*-
"""
market.py —— 市场行情 + 降噪资讯

A. 指数行情: A股/韩股/美股 核心指数实时点位/涨跌幅/涨跌点
   - A股+美股: 东方财富 push2 接口
   - 韩股+SOX: 新浪财经接口
   - 开市/休市状态: 按北京时间与各市场交易时段判断

B. 降噪资讯: 每日 07:00 早报 / 12:00 午报 / 20:00 晚报
   - 抓取(财联社/金十/东财板块+指数) → 清洗 → 存库
   - LLM 结构化暂用规则模板(未接 LLM 时), 存 market_news 表
"""
from __future__ import annotations

import json
import sqlite3
import time
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path

from modules.common.cache_service import market_cache

ROOT = Path(__file__).resolve().parent
import sys
sys.path.insert(0, str(ROOT))

# ---------------- 指数配置 ----------------
# 统一新浪源(格式稳定,无缩放坑)。每项: 名称/市场/新浪代码/类型
#   type: 'idx_full' 完整指数(今开/昨收/当前/最高/最低) → 用昨收算涨跌
#         'znb'       韩股 (当前, 涨跌点, 涨跌%)
#         'gb'        美股 (当前, 涨跌%, 时间, 涨跌点)
INDICES = [
    {"name": "上证指数", "market": "A股", "code": "sh000001", "type": "idx_full"},
    {"name": "深证成指", "market": "A股", "code": "sz399001", "type": "idx_full"},
    {"name": "创业板指", "market": "A股", "code": "sz399006", "type": "idx_full"},
    {"name": "科创50", "market": "A股", "code": "sh000688", "type": "idx_full"},
    {"name": "KOSPI指数", "market": "韩股", "code": "znb_KOSPI", "type": "znb"},
    {"name": "KOSDAQ指数", "market": "韩股", "code": "znb_KOSDAQ", "type": "znb"},
    {"name": "道琼斯工业指数", "market": "美股", "code": "gb_dji", "type": "gb"},
    {"name": "纳斯达克综合指数", "market": "美股", "code": "gb_ixic", "type": "gb"},
    {"name": "标普500指数", "market": "美股", "code": "gb_inx", "type": "gb"},
    {"name": "费城半导体指数(SOX)", "market": "美股", "code": "gb_sox", "type": "gb"},
]

# ---------------- 开市/休市判断 ----------------
def _is_weekend(dt: datetime) -> bool:
    return dt.weekday() >= 5  # 5=周六 6=周日


def market_status(market: str) -> dict:
    """按北京时间判断市场状态。
    返回 {state: 'open'|'closed'|'weekend'|'break', label, trading_hours}"""
    now = datetime.now()  # 本地时间=北京时间
    hm = now.hour * 60 + now.minute
    if market == "A股":
        if _is_weekend(now):
            return {"state": "weekend", "label": "休市（周末）", "hours": "09:30-11:30 / 13:00-15:00"}
        if 9 * 60 + 30 <= hm <= 11 * 60 + 30:
            return {"state": "open", "label": "开市中", "hours": "09:30-11:30 / 13:00-15:00"}
        if 13 * 60 <= hm <= 15 * 60:
            return {"state": "open", "label": "开市中", "hours": "09:30-11:30 / 13:00-15:00"}
        if 11 * 60 + 30 < hm < 13 * 60:
            return {"state": "break", "label": "午间休市", "hours": "09:30-11:30 / 13:00-15:00"}
        return {"state": "closed", "label": "已收盘", "hours": "09:30-11:30 / 13:00-15:00"}
    if market == "韩股":
        if _is_weekend(now):
            return {"state": "weekend", "label": "休市（周末）", "hours": "08:00-14:30"}
        if 8 * 60 <= hm <= 14 * 60 + 30:
            return {"state": "open", "label": "开市中", "hours": "08:00-14:30"}
        return {"state": "closed", "label": "已收盘", "hours": "08:00-14:30"}
    if market == "美股":
        # 北京时间视角(北京=美东+12h 夏令时/+13h 冬令时):
        #   夏令时 09:30-16:00(美东 21:30-次日04:00) / 冬令时 10:30-17:00(美东 22:30-次日05:00)
        #   美股周五晚盘 = 北京周六 09:30-16:00(夏令时), 北京周日全天休市
        dst = _us_dst(now)
        open_hm = 9 * 60 + 30 if dst else 10 * 60 + 30
        close_hm = 16 * 60 if dst else 17 * 60
        hours = "09:30-16:00（夏令时）" if dst else "10:30-17:00（冬令时）"
        if now.weekday() == 6:  # 北京周日 = 美东周六, 休市
            return {"state": "weekend", "label": "休市（周末）", "hours": hours}
        if open_hm <= hm < close_hm:
            return {"state": "open", "label": "开市中", "hours": hours}
        return {"state": "closed", "label": "已收盘", "hours": hours}
    return {"state": "closed", "label": "已收盘", "hours": ""}


def _us_dst(dt: datetime) -> bool:
    """美东夏令时粗略判断: 3月第二个周日至11月第一个周日。简化用月份近似。"""
    m = dt.month
    # 4-10月肯定夏令时; 3/11月看日期(近似取15日前/后)
    if 4 <= m <= 10:
        return True
    if m == 3:
        return dt.day >= 15
    if m == 11:
        return dt.day <= 15
    return False


# ---------------- 行情抓取 ----------------
def _http_json(url: str, timeout: int = 8) -> dict:
    import httpx
    resp = httpx.get(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Referer": "https://finance.sina.com.cn",
    }, timeout=timeout)
    raw = resp.text
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _fetch_sina_batch(codes: list[str]) -> dict:
    """批量拉新浪行情。返回 {code: parts[]}。"""
    if not codes:
        return {}
    url = "https://hq.sinajs.cn/list=" + ",".join(codes)
    try:
        import httpx
        resp = httpx.get(url, headers={
            "User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"}, timeout=10)
        raw = resp.content.decode("gbk", "ignore")
    except Exception:
        return {}
    out = {}
    for line in raw.split("\n"):
        if '="' not in line:
            continue
        var = line.split("=")[0].replace("var hq_str_", "").strip()
        body = line.split('="', 1)[1].rstrip('";').strip()
        out[var] = body.split(",")
    return out


def _parse_quote(idx: dict, parts: list[str]) -> dict | None:
    """按类型解析新浪行情字段, 附行情时间 quote_time(如 '15:30')。
    
    v0.95.1 fix: 安全处理 None 或空 parts，避免 TypeError。
    """
    if not parts or not isinstance(parts, list) or len(parts) < 1:
        return None
    if not idx or not isinstance(idx, dict) or "type" not in idx:
        return None
    def _tm(*idxs):
        for ix in idxs:
            for j in range(ix, min(ix + 4, len(parts))):
                t = parts[j].strip()
                if ":" in t:
                    hh, _, mm = t.partition(":")
                    hh = hh[-2:].zfill(2)
                    return (hh + ":" + mm[:2]) if mm else None
        return None
    try:
        if idx["type"] == "idx_full":
            # 名称,今开,昨收,当前,最高,最低,...,日期,时间
            if len(parts) < 5 or not parts[3]:
                return None
            cur, prev = float(parts[3]), float(parts[2])
            if not prev:
                return None
            pct = (cur / prev - 1) * 100
            return {"point": cur, "pct": pct, "chg": cur - prev,
                    "quote_time": _tm(32, 31, 30)}
        if idx["type"] == "znb":
            # 名称,当前,涨跌点,涨跌%,时间,...
            if len(parts) < 4 or not parts[1]:
                return None
            return {"point": float(parts[1]), "chg": float(parts[2]),
                    "pct": float(parts[3]), "quote_time": _tm(4)}
        if idx["type"] == "gb":
            # 名称,当前,涨跌%,时间,涨跌点,...
            if len(parts) < 5 or not parts[1]:
                return None
            return {"point": float(parts[1]), "pct": float(parts[2]),
                    "chg": float(parts[4]), "quote_time": _tm(3)}
    except (ValueError, IndexError):
        return None
    return None


def get_market_indices() -> dict:
    """拉取全部指数行情, 使用统一缓存服务（默认10秒TTL）。"""
    cached = market_cache.get("indices")
    if cached is not None:
        return cached
    now_t = time.time()
    data = _fetch_sina_batch([x["code"] for x in INDICES])
    markets = {}
    for idx in INDICES:
        parts = data.get(idx["code"])
        if not parts:
            continue
        q = _parse_quote(idx, parts)
        if q is None:
            continue
        st = market_status(idx["market"])
        item = {
            "name": idx["name"], "market": idx["market"],
            "point": round(q["point"], 2),
            "pct": round(q["pct"], 2), "chg": round(q["chg"], 2),
            "quote_time": q.get("quote_time"),
            "status": st,
        }
        markets.setdefault(idx["market"], []).append(item)
    out = {"ts": int(now_t), "markets": markets}
    market_cache.set("indices", out)
    return out


# ---------------- 降噪资讯 ----------------
NEWS_SLOTS = [
    {"key": "morning", "label": "早报", "time": "07:00"},
    {"key": "noon", "label": "午报", "time": "12:00"},
    {"key": "night", "label": "晚报", "time": "20:00"},
]


def _db() -> sqlite3.Connection:
    import db
    return db.get_conn()


def init_news_table():
    """market_news 表: date+slot 唯一索引。"""
    conn = _db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS market_news (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            slot TEXT NOT NULL,
            slot_label TEXT,
            slot_time TEXT,
            market_summary TEXT,
            market_status TEXT,
            focus_sectors TEXT,
            main_news TEXT,
            raw_count INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            UNIQUE(date, slot)
        )
    """)
    # v2.10: 分析文本字段(老库补列)
    _cols = [r[1] for r in conn.execute("PRAGMA table_info(market_news)").fetchall()]
    for _c in ("market_analysis", "focus_analysis"):
        if _c not in _cols:
            conn.execute(f"ALTER TABLE market_news ADD COLUMN {_c} TEXT")
    conn.commit()


def fetch_news_list(date: str | None = None) -> list[dict]:
    """读取指定日期(默认最新有数据的日期)的资讯。返回按时间倒序(晚报最先)。"""
    init_news_table()
    conn = _db()
    if date is None:
        r = conn.execute("SELECT MAX(date) FROM market_news").fetchone()
        date = r[0] if r and r[0] else datetime.now().strftime("%Y-%m-%d")
    rows = conn.execute(
        "SELECT * FROM market_news WHERE date=? ORDER BY slot_time DESC", (date,)
    ).fetchall()
    out = []
    for r in rows:
        out.append({
            "date": r["date"], "slot": r["slot"],
            "slot_label": r["slot_label"], "slot_time": r["slot_time"],
            "market_summary": r["market_summary"],
            "market_status": r["market_status"],
            "market_analysis": r["market_analysis"] if "market_analysis" in r.keys() else "",
            "focus_sectors": json.loads(r["focus_sectors"] or "[]"),
            "focus_analysis": r["focus_analysis"] if "focus_analysis" in r.keys() else "",
            "main_news": json.loads(r["main_news"] or "[]"),
            "created_at": r["created_at"],
        })
    return {"date": date, "items": out}


def upsert_news(date: str, slot_key: str, data: dict):
    """写入/更新一条资讯。"""
    init_news_table()
    conn = _db()
    slot = next((s for s in NEWS_SLOTS if s["key"] == slot_key), NEWS_SLOTS[0])
    conn.execute("""
        INSERT INTO market_news (date, slot, slot_label, slot_time, market_summary,
                                 market_status, market_analysis, focus_sectors,
                                 focus_analysis, main_news, raw_count)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(date, slot) DO UPDATE SET
          slot_label=excluded.slot_label, slot_time=excluded.slot_time,
          market_summary=excluded.market_summary, market_status=excluded.market_status,
          market_analysis=excluded.market_analysis, focus_sectors=excluded.focus_sectors,
          focus_analysis=excluded.focus_analysis, main_news=excluded.main_news,
          raw_count=excluded.raw_count, created_at=datetime('now','localtime')
    """, (
        date, slot_key, slot["label"], slot["time"],
        data.get("market_summary", ""),
        data.get("market_status", ""),
        data.get("market_analysis", ""),
        json.dumps(data.get("focus_sectors", []), ensure_ascii=False),
        data.get("focus_analysis", ""),
        json.dumps(data.get("main_news", []), ensure_ascii=False),
        data.get("raw_count", 0),
    ))
    conn.commit()


# ---------------- 资讯抓取整理 ----------------
def _fetch_cls_news() -> list[dict]:
    """财联社聚合接口: 返回 [{title, brief, ctime, url}]。失败/为空时降级到东财快讯。"""
    out = []
    try:
        d = _http_json("https://api.xcvts.cn/api/hotlist/cls/all", timeout=10)
        items = d.get("data") or []
        for it in items:
            if it.get("is_ad") == 1:
                continue
            ctime = it.get("ctime") or it.get("created_at") or ""
            if isinstance(ctime, (int, float)):
                try:
                    ctime = datetime.fromtimestamp(int(ctime) / 1000).strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    ctime = ""
            out.append({
                "title": it.get("title") or it.get("brief") or "",
                "brief": it.get("brief") or it.get("title") or "",
                "ctime": str(ctime),
                "url": it.get("shareurl") or it.get("url") or "",
            })
    except Exception:
        pass
    if out:
        return out
    # 降级: 东财快讯(7x24)
    try:
        url = ("https://np-listapi.eastmoney.com/comm/web/getNewsByColumns?client=web"
               "&biz=web_news_col&column=345&order=1&needInteractData=0&page_index=1"
               "&page_size=10&req_trace=1&fields=code,showTime,title,summary,url,digest")
        import httpx
        resp = httpx.get(url, headers={
            "User-Agent": "Mozilla/5.0", "Referer": "https://finance.eastmoney.com/"}, timeout=10)
        d2 = json.loads(resp.text)
        items2 = ((d2.get("data") or {}).get("list")) or []
        for it in items2:
            out.append({
                "title": it.get("title") or "",
                "brief": (it.get("digest") or it.get("summary") or "")[:120],
                "ctime": str(it.get("showTime") or ""),
                "url": it.get("url") or "",
            })
    except Exception:
        pass
    return out


def _fetch_sector_ranking(top: int = 8) -> list[dict]:
    """东财行业板块涨幅排行。返回 [{'name','pct'}]。多节点重试, 再失败返回空。"""
    # 1) 东财主源(多节点容错)
    hosts = ("push2.eastmoney.com", "push2delay.eastmoney.com", "82.push2.eastmoney.com")
    for host in hosts:
        try:
            url = ("https://%s/api/qt/clist/get?pn=1&pz=%d&po=1&np=1&fltt=2&invt=2"
                   "&fid=f3&fs=m:90+t:2&fields=f12,f14,f3" % (host, top))
            d = _http_json(url, timeout=6)
            diff = (d.get("data") or {}).get("diff") or []
            out = []
            for x in diff:
                if not x.get("f14"):
                    continue
                try:
                    pct = float(x.get("f3"))
                except (TypeError, ValueError):
                    pct = None
                out.append({"name": x.get("f14"), "pct": pct})
            if out:
                return out
        except Exception:
            continue
    # 2) 新浪行业板块(备选)
    try:
        url2 = "https://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php"
        import httpx
        resp = httpx.get(url2, headers={
            "User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"}, timeout=8)
        raw = resp.content.decode("gbk", "ignore")
        import re as _re
        m = _re.search(r"\{.*\}", raw, _re.S)
        if m:
            data = json.loads(m.group(0))
            rows = []
            for v in data.values():
                # 新浪行业: [code,name,...] 第2位为名称
                if isinstance(v, (list, tuple)) and len(v) >= 2 and v[1]:
                    rows.append({"name": str(v[1]), "pct": None})
            if rows:
                return rows[:top]
    except Exception:
        pass
    return []


def _fetch_finnhub_news() -> list[dict]:
    """Finnhub 美股新闻(免费接口无 token 时返回空)。"""
    return []


def _rule_organize(slot_key: str, indices: dict, sectors: list[dict], news: list[dict]) -> dict:
    """规则化整理(无 LLM 时): 用指数/板块生成结构化字段 + 分析文本。"""
    today = datetime.now().strftime("%Y-%m-%d")
    slot = next((s for s in NEWS_SLOTS if s["key"] == slot_key), NEWS_SLOTS[0])
    # 市场总结
    all_mk = []
    for mk, items in (indices.get("markets") or {}).items():
        for it in items:
            all_mk.append((mk, it["name"], it["pct"]))
    if not all_mk:
        summary = "%s：暂无指数数据。" % slot["label"]
    else:
        up = [x for x in all_mk if x[2] > 0]
        dn = [x for x in all_mk if x[2] < 0]
        up_txt = "、".join(x[1] for x in up[:4]) if up else "无"
        dn_txt = "、".join(x[1] for x in dn[:4]) if dn else "无"
        summary = "%s：指数涨跌互现（上涨：%s；下跌：%s）。" % (slot["label"], up_txt, dn_txt)
    # 市场实际情况
    status_lines = []
    for mk, items in (indices.get("markets") or {}).items():
        for it in items:
            status_lines.append("%s %s %.2f（%+.2f%%）" % (mk, it["name"], it["point"], it["pct"]))
    market_status = "；".join(status_lines) if status_lines else "暂无指数数据"
    # 市场分析文本: 以A股指数涨跌结构为核心
    market_analysis = ""
    a_items = [x for mk, items in (indices.get("markets") or {}).items()
               for x in items if mk == "A股"]
    if a_items:
        ups = [x for x in a_items if (x.get("pct") or 0) > 0]
        dns = [x for x in a_items if (x.get("pct") or 0) < 0]
        if not ups and not dns:
            market_analysis = "A股主要指数基本平盘，多空力量均衡，方向待选择。"
        elif len(ups) >= 3:
            _best = max(ups, key=lambda x: x.get("pct") or 0)
            market_analysis = ("A股主要指数多数收涨，%s领涨（%+.2f%%），市场情绪偏暖；"
                               "关注量能能否持续放大以确认反弹力度。" % (_best["name"], _best["pct"] or 0))
        elif len(dns) >= 3:
            _worst = min(dns, key=lambda x: x.get("pct") or 0)
            market_analysis = ("A股主要指数多数收跌，%s领跌（%+.2f%%），市场情绪偏谨慎；"
                               "短期以防守为主，等待企稳信号。" % (_worst["name"], _worst["pct"] or 0))
        else:
            market_analysis = "A股主要指数涨跌互现，结构分化明显，资金聚焦局部热点，需精选方向。"
    # 重点关注板块(名称列表, 兼容旧字段)
    focus = [x.get("name", "") for x in sectors[:5] if x.get("name")]
    # 板块分析文本: 领涨板块+涨幅
    focus_analysis = ""
    if sectors:
        _pct_parts = []
        for x in sectors[:5]:
            nm = x.get("name", "")
            if not nm:
                continue
            _pct_parts.append(nm + ((" %+.2f%%" % x["pct"]) if x.get("pct") is not None else ""))
        if _pct_parts:
            focus_analysis = "涨幅居前板块：%s。资金偏好集中于上述方向，可结合自身持仓跟踪强弱持续性。" % "、".join(_pct_parts)
    if not focus_analysis and focus:
        focus_analysis = "重点关注板块：%s（今日涨幅居前，可留意其轮动节奏）。" % "、".join(focus)
    # 主线消息
    main_news = []
    for n in news[:4]:
        txt = (n.get("brief") or n.get("title") or "").strip()
        if txt and txt not in main_news:
            main_news.append(txt[:60])
    return {
        "market_summary": summary,
        "market_status": market_status,
        "market_analysis": market_analysis,
        "focus_sectors": focus,
        "focus_analysis": focus_analysis,
        "main_news": main_news,
        "raw_count": len(news),
    }


def run_news_collect(slot_key: str, force: bool = False) -> dict:
    """定时抓取并整理一期资讯, 写入库。slot_key: morning/noon/night。"""
    today = datetime.now().strftime("%Y-%m-%d")
    indices = get_market_indices()
    sectors = _fetch_sector_ranking(8)
    news = _fetch_cls_news()
    if slot_key in ("morning", "night"):
        news += _fetch_finnhub_news()
    data = _rule_organize(slot_key, indices, sectors, news)
    upsert_news(today, slot_key, data)
    return {"ok": True, "date": today, "slot": slot_key, **data}

