"""
modules/fund/profile.py —— 基金概况数据

从 collector.fetcher 迁移（v2.5.5架构重构 - 阶段3）
负责：基金概况获取、f10概况全字段解析
"""
from __future__ import annotations

import re
import time

from collector.http_utils import _get
from collector.cache_utils import _lru_put

# 规模/成立日期是月度级变化的字段, 而概况页单次 45.7KB/0.32s, 旧实现每次
# 弹窗/档案刷新都重新下载。命中即 0 网络; 只缓存成功结果, 失败下次重试。
_BASIC_CACHE: dict[str, tuple[float, tuple]] = {}
_BASIC_TTL = 24 * 3600
_BASIC_MAX = 600


def fetch_basic(code: str) -> tuple:
    """基金概况: 返回 (净资产规模·亿元 or None, 成立日期 'YYYY-MM-DD' or None)。

    v1.1.4: 结果走 24h LRU 缓存。
    v2.9.10: 优先使用同花顺API，失败降级到天天基金网。
    """
    hit = _BASIC_CACHE.get(code)
    if hit is not None:
        ts, val = hit
        if time.time() - ts <= _BASIC_TTL:
            return val
        _BASIC_CACHE.pop(code, None)

    # v2.9.10: 优先使用同花顺API
    try:
        from collector.hithink_integration import get_fund_profile
        profile = get_fund_profile(code)
        if profile:
            scale = None
            est = None
            # 转换规模：元 → 亿元
            if profile.get("fund_scale"):
                scale = round(float(profile["fund_scale"]) / 100000000, 2)
            # 转换成立日期：毫秒时间戳 → YYYY-MM-DD
            if profile.get("estab_date"):
                from datetime import datetime
                est = datetime.fromtimestamp(int(profile["estab_date"]) / 1000).strftime("%Y-%m-%d")
            if scale is not None or est is not None:
                _lru_put(_BASIC_CACHE, code, (scale, est), _BASIC_MAX)
                return scale, est
    except Exception:
        pass  # 同花顺失败，降级到天天基金网

    for _attempt in range(2):
        txt = _get(f"https://fundf10.eastmoney.com/jbgk_{code}.html",
                   referer=f"https://fund.eastmoney.com/{code}.html")
        if not txt:
            continue
        scale, est = None, None
        m_s = re.search(r'净资产规模：\s*<span>\s*([\d.]+)\s*(亿|万)元', txt)
        if m_s:
            v = float(m_s.group(1))
            scale = round(v / 10000.0, 2) if m_s.group(2) == "万" else round(v, 2)
        m_e = re.search(r'成立日期：\s*<span>(\d{4}-\d{2}-\d{2})</span>', txt)
        if m_e:
            est = m_e.group(1)
        if scale is not None or est is not None:
            _lru_put(_BASIC_CACHE, code, (scale, est), _BASIC_MAX)
            return scale, est
        time.sleep(0.3)
    return None, None


def fetch_f10_profile(code: str) -> dict:
    """基金概况(f10 jbgk)全字段解析,一次请求约 0.18s。

    v0.92.0: 原 fetch_basic 只取 (scale, est) 两个字段, 浪费了同一份响应里的
             其余信息。概况页是 <th>标签</th><td>值</td> 结构, 可一次拿到:
               scale(净资产规模·亿元) / est(成立日) / manager(基金经理人)
               track(跟踪标的, ETF 才有) / ftype(基金类型) / company(基金管理人)
               benchmark(业绩比较基准) / mgr_fee(管理费率) / fullname(基金全称)
             其中 manager 与 track 在 funds 表此前 100% 为空。
    """
    txt = _get(f"https://fundf10.eastmoney.com/jbgk_{code}.html",
               referer=f"https://fund.eastmoney.com/{code}.html")
    if not txt:
        return {}

    kv: dict[str, str] = {}
    for m in re.finditer(r'<th[^>]*>([^<]{2,12})</th>\s*<td[^>]*>(.*?)</td>', txt, re.S):
        k = m.group(1).strip()
        v = re.sub(r'<[^>]+>', '', m.group(2))
        v = re.sub(r'\s+', ' ', v).strip()
        if k and v and k not in kv:
            kv[k] = v
    if not kv:
        return {}

    out: dict = {}

    def _take(*labels):
        for lb in labels:
            if lb in kv:
                return kv[lb]
        return None

    # 基金全称 / 类型 / 管理人
    if _take("基金全称"):
        out["fullname"] = _take("基金全称")
    if _take("基金管理人"):
        out["company"] = _take("基金管理人")
    if _take("基金经理人"):
        _mg = _take("基金经理人")
        if _mg and _mg != "---":
            out["manager"] = _mg
    if _take("跟踪标的"):
        _tk = _take("跟踪标的")
        # 非指数基金该单元格是占位文案而非真实标的, 必须过滤否则前端显示"该基金无跟踪标的"
        if _tk and _tk != "---" and "无跟踪标的" not in _tk:
            out["track"] = _tk
    if _take("业绩比较基准"):
        out["benchmark"] = _take("业绩比较基准")

    # 基金类型: 概况页"基金代码"单元格里附带了类型, 如 "159941（主代码）基金类型指数型-海外股票"
    _code_cell = _take("基金代码") or ""
    m = re.search(r'基金类型\s*([^\s（(]+)', _code_cell)
    if m:
        out["ftype"] = m.group(1)

    # 管理费率
    _fee = _take("管理费率")
    if _fee:
        m = re.search(r'([\d.]+)\s*%', _fee)
        if m:
            try:
                out["mgr_fee"] = float(m.group(1))
            except ValueError:
                pass

    # 净资产规模: "346.82亿元（截止至：2026年06月30日）"
    _s = _take("净资产规模") or ""
    m = re.search(r'([\d.]+)\s*(亿|万)元', _s)
    if m:
        try:
            v = float(m.group(1))
            out["scale"] = round(v / 10000.0, 2) if m.group(2) == "万" else round(v, 2)
        except ValueError:
            pass
    m = re.search(r'([\d.]+)\s*亿份', _s)
    if m:
        try:
            out["shares"] = float(m.group(1))
        except ValueError:
            pass

    # 成立日期: "2015年06月10日 / 2.631亿份"
    _e = _take("成立日期/规模") or ""
    m = re.search(r'(\d{4})年(\d{2})月(\d{2})日', _e)
    if m:
        out["est"] = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"

    return out
