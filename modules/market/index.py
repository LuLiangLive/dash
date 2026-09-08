"""
modules/market/index.py —— 指数日K数据

从 collector.fetcher 迁移（v2.5.5架构重构 - 阶段1）
负责：指数日K线获取、全部指数序列缓存
"""
from __future__ import annotations

import json
import time

from collector.http_utils import _get

# 指数代码映射
INDEX_CODES = {
    "sh000001": "上证指数",
    "sz399006": "创业板指",
    "sh000688": "科创50",
    # 超额收益主题基准(自适应匹配用,备选中证800/沪深300)
    "sh000906": "中证800",
    "sh000300": "沪深300",
    "sh000819": "有色金属",
    "sz399998": "中证煤炭",
    "sh000933": "中证医药",
    "sz399989": "中证医疗",
    "sh000932": "中证消费",
    "sz399997": "中证白酒",
    "sz399967": "中证军工",
    "sz399975": "证券公司",
    "sz399986": "中证银行",
    "sz399808": "中证新能",
    "sh000941": "新能源",
    "sz399995": "基建工程",
    "sh000949": "中证农业",
    "sz399363": "国证算力",
    "sz399811": "CSSW电子",
}


def fetch_index_kline(code: str, days: int = 60) -> list[dict]:
    """返回 [{date, open, close, high, low}],升序。"""
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,,{days},qfq"
    txt = _get(url, referer="https://gu.qq.com/")
    if not txt:
        return []
    try:
        data = json.loads(txt)
    except Exception:
        return []
    d = data.get("data", {}).get(code, {})
    arr = d.get("day") or d.get("qfqday") or []
    items = []
    for row in arr:
        items.append({
            "date": row[0],
            "open": float(row[1]),
            "close": float(row[2]),
            "high": float(row[3]),
            "low": float(row[4]),
        })
    return items


# 指数序列缓存: 成功 6h / 失败 10min
_IDX_CACHE: dict = {"ts": 0.0, "data": None, "ok": False}
_IDX_OK_TTL = 6 * 3600
_IDX_FAIL_TTL = 10 * 60


def _load_indices_from_db(days: int = 60) -> dict:
    """从 nav_history 读指数序列。

    一键更新会把 20 个指数(sh000001/sz399006/...)一并抓进 nav_history(实测各 93 条),
    而下游 fetch_ddown 只需要 date + close 两列, 所以这里完全够用, 无需再打网络。
    """
    try:
        import db as _db
        conn = _db.get_conn()
    except Exception:
        return {}
    out = {}
    for code, name in INDEX_CODES.items():
        try:
            rows = conn.execute(
                "SELECT date, ljjz FROM nav_history WHERE code=? "
                "ORDER BY date DESC LIMIT ?", (code, days)
            ).fetchall()
        except Exception:
            continue
        if not rows:
            continue
        rows = list(reversed(rows))
        items = [{"date": r["date"] if hasattr(r, "keys") else r[0],
                  "close": r["ljjz"] if hasattr(r, "keys") else r[1],
                  "open": None, "high": None, "low": None}
                 for r in rows]
        out[code] = {"name": name, "items": items}
    return out


def fetch_all_indices(days: int = 60) -> dict:
    """全部指数日K,返回 {code: {"name":.., "items":[{date,open,close,high,low}]}}。

    v0.92.0 修复(弹窗 58s → ~0s):
      原实现无条件串行打 20 个指数的腾讯 K 线接口, 而 web.ifzq.gtimg.cn 现已失效
      ——每次请求 2.5s 超时返回空, _get 内部再重试 2 次, 20 个指数累计 ≈ 58s 全部
      耗在 time.sleep 上。这是「有的弹窗特别慢」的真正主因(与净值补拉无关)。

      实测: 这 20 个指数在一键更新时已经抓进 nav_history(各 93 条), 而下游
      fetch_ddown 只用 date + close, 所以改为 DB 优先:
        1. nav_history 有数据 → 直接返回(0 网络请求)
        2. 没有 → 才走网络, 且失败结果也进缓存(10min), 不再每次弹窗都重试死接口
    """
    now = time.time()
    # 1) 进程内缓存
    if _IDX_CACHE["data"] is not None:
        ttl = _IDX_OK_TTL if _IDX_CACHE["ok"] else _IDX_FAIL_TTL
        if now - _IDX_CACHE["ts"] < ttl:
            return _IDX_CACHE["data"] or {}

    # 2) DB 优先(一键更新已落库, 零网络成本)
    db_data = _load_indices_from_db(days)
    if db_data:
        _IDX_CACHE.update(ts=now, data=db_data, ok=True)
        return db_data

    # 3) DB 没有才走网络
    out = {}
    for code, name in INDEX_CODES.items():
        items = fetch_index_kline(code, days)
        if items:
            out[code] = {"name": name, "items": items}
        time.sleep(0.3)
    _IDX_CACHE.update(ts=now, data=out, ok=bool(out))
    return out
