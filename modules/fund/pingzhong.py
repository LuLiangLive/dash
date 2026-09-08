"""
modules/fund/pingzhong.py —— pingzhongdata统一解析

从 collector.fetcher 迁移（v2.5.5架构重构 - 阶段4）
负责：pingzhongdata单次请求统一解析，覆盖净值序列+区间收益+经理+规模
"""
from __future__ import annotations

import datetime as _dt
from datetime import timezone, timedelta
import json
import os
import re
import threading
import time

from collector.http_utils import _get
from collector.cache_utils import _lru_put

# 统一时区：北京时间 UTC+8（所有日期处理必须使用此时区）
BEIJING_TZ = timezone(timedelta(hours=8))

# --- v0.92.0: pingzhongdata 单次请求统一解析 ---
# 背景: 旧链路「净值全量分页」= 14 页串行/并发, 1.04~3.26s 才拿到 280 条;
#       而 pingzhongdata 单次请求 0.03~0.20s、约 106KB 即返回:
#         - Data_netWorthTrend  全量净值序列(自成立日起, 实测 606 条)
#         - syl_1y/3y/6y/1n     近1月/3月/6月/1年区间收益(无需自己算)
#         - Data_currentFundManager  现任基金经理(姓名/任职天数/星级/能力分)
#         - Data_fluctuationScale    季度规模变动序列
#         - Data_performanceEvaluation / Data_rateInSimilarType 等
#       即「一次请求覆盖净值 + 区间收益 + 经理 + 规模」, 取代全量分页拉取。

_PZ_CACHE: dict[str, tuple[float, dict]] = {}
_PZ_TTL = 6 * 3600          # 6 小时
_PZ_MAX = 600               # 最多缓存 600 只
_PZ_NEG_TTL = 30 * 60       # 失败结果短期记忆, 避免每次重试都打网络

# --- v0.93.0: 净值序列拆出来单独缓存 ---
# 实测(60 只抽样): Data_netWorthTrend 单只 334~2110 条, 解析 49ms/只, 解析结果
# 常驻 565KB/只。若和档案字段混存在同一个 LRU(上限 600 只)里, 满缓存要吃 331MB,
# 而其中 99% 的净值数据只有「基金详情长序列」一个入口用得到。
# 因此: 主缓存 _PZ_CACHE 永不存放 navs;navs 走独立的小缓存(上限 60 只, ~34MB)。
_NAV_CACHE: dict[str, tuple[float, list]] = {}
_NAV_TTL = 6 * 3600
_NAV_MAX = 60

# 原始响应文本短期缓存(48KB/只, 上限 24 只 ≈ 1.2MB):
# 避免「先取档案、再取净值」这种连续调用打第二次网络。
_PZ_RAW: dict[str, tuple[float, str]] = {}
_PZ_RAW_TTL = 15 * 60
_PZ_RAW_MAX = 24


def _pz_cache_get(code: str):
    hit = _PZ_CACHE.get(code)
    if not hit:
        return None
    ts, val = hit
    ttl = _PZ_NEG_TTL if not val else _PZ_TTL
    if time.time() - ts > ttl:
        _PZ_CACHE.pop(code, None)
        return None
    return val


def _pz_cache_set(code: str, val: dict) -> None:
    _lru_put(_PZ_CACHE, code, val, _PZ_MAX)


def _nav_cache_get(code: str):
    hit = _NAV_CACHE.get(code)
    if hit is None:
        return None
    ts, val = hit
    if time.time() - ts > _NAV_TTL:
        _NAV_CACHE.pop(code, None)
        return None
    return val


def _pz_raw_get(code: str):
    hit = _PZ_RAW.get(code)
    if hit is None:
        return None
    ts, txt = hit
    if time.time() - ts > _PZ_RAW_TTL:
        _PZ_RAW.pop(code, None)
        return None
    return txt


def _js_value(txt: str, var: str):
    """从 `var xxx = <json>;` 中稳健取值。

    用 raw_decode 而非贪婪正则: 变量值是合法 JSON, 但贪婪正则会跨到下一个
    变量导致 "Extra data" 报错。
    """
    m = re.search(rf'var\s+{var}\s*=', txt)
    if not m:
        return None
    # v0.93.0: 旧实现是 seg = txt[m.end() : m.end()+400000] 再 raw_decode(seg),
    # 每次调用都要复制一份几十~几百 KB 的字符串(单只响应最大 324KB), 是解析期
    # 最大的内存开销来源。raw_decode 原生支持 idx 参数, 直接在原串上定位即可,
    # 只需自己跳过 = 后面的空白。
    i = m.end()
    n = len(txt)
    while i < n and txt[i] in " \t\r\n":
        i += 1
    if i >= n or txt[i] not in "[{\"":
        return None
    try:
        val, _ = json.JSONDecoder().raw_decode(txt, i)
        return val
    except Exception:
        return None


def _parse_navs(txt: str) -> list:
    """解析 Data_netWorthTrend（自成立日起的全量净值）。

    单独抽出来是因为它最贵: 单只 334~2110 条, 解析 49ms、结果 565KB。
    只有「基金详情长序列」需要, 日常更新不需要, 因此按需调用。
    """
    trend = _js_value(txt, "Data_netWorthTrend") or []
    navs: list[dict] = []
    if not isinstance(trend, list):
        return navs
    for it in trend:
        if not isinstance(it, dict):
            continue
        ts = it.get("x")
        if not ts:
            continue
        try:
            d = _dt.datetime.fromtimestamp(int(ts) / 1000, tz=BEIJING_TZ).strftime("%Y-%m-%d")
        except Exception:
            continue
        try:
            dwjz = float(it.get("y")) if it.get("y") is not None else None
        except (TypeError, ValueError):
            dwjz = None
        if dwjz is None:
            continue
        try:
            jzzzl = float(it.get("equityReturn")) if it.get("equityReturn") not in (None, "") else None
        except (TypeError, ValueError):
            jzzzl = None
        navs.append({"date": d, "dwjz": dwjz, "ljjz": dwjz, "jzzzl": jzzzl})
    navs.sort(key=lambda x: x["date"])
    return navs


def _parse_pz_profile(txt: str, want_navs: bool = True,
                      want_mgr_info: bool = True,
                      want_similar: bool = True) -> dict:
    """从 pingzhongdata 原始文本解析出档案字段（不含网络请求）。"""
    out: dict = {}

    # --- 基金简称 ---
    m = re.search(r'fS_name\s*=\s*"([^"]+)"', txt)
    if m:
        out["name"] = m.group(1)

    # --- 阶段涨跌幅: 直接取接口值, 避免从残缺净值序列推算 ---
    for js_var, field in (("syl_1y", "m1"), ("syl_3y", "m3"),
                          ("syl_6y", "m6"), ("syl_1n", "y1")):
        m = re.search(rf'{js_var}\s*=\s*"([^"]*)"', txt)
        if m:
            try:
                out[field] = float(m.group(1))
            except (ValueError, TypeError):
                pass

    # --- 基金经理 ---
    mgrs = _js_value(txt, "Data_currentFundManager") or []
    if isinstance(mgrs, list) and mgrs:
        m0 = mgrs[0]
        if isinstance(m0, dict):
            nm = (m0.get("name") or "").strip()
            if nm:
                out["manager"] = nm
                if want_mgr_info:
                    out["manager_info"] = {
                        "name": nm,
                        "workTime": m0.get("workTime"),
                        "fundSize": m0.get("fundSize"),
                        "star": m0.get("star"),
                        "power": (m0.get("power") or {}).get("avr") if isinstance(m0.get("power"), dict) else None,
                    }

    # --- 规模: Data_fluctuationScale 最后一期 ---
    fs = _js_value(txt, "Data_fluctuationScale") or {}
    if isinstance(fs, dict):
        cats = fs.get("categories") or []
        series = fs.get("series") or []
        if cats and series:
            try:
                out["scale"] = round(float(series[-1].get("y")), 2)
                out["scale_date"] = cats[-1]
            except (TypeError, ValueError, IndexError, AttributeError):
                pass

    if want_navs:
        navs = _parse_navs(txt)
        if navs:
            out["navs"] = navs

    if want_similar:
        sim = _js_value(txt, "Data_rateInSimilarType")
        if isinstance(sim, list) and sim:
            out["similar_rank"] = sim

    return out


# --- v1.1.4: pingzhongdata 单飞锁 ---
# 按 code 粒度加锁, 只在「走网络」分支持有: 同一只基金的并发请求(榜单更新与
# 弹窗同时触发是典型场景)只放一个线程真正下载, 其余在锁上等待、醒来后先双检
# 缓存, 命中即返回 —— 旧行为会并发打两份 129KB。
# 锁注册表随基金代码数缓慢增长(全市场 ~1.2 万只 ≈ 1MB), 可接受。
_PZ_LOCKS: dict = {}
_PZ_LOCKS_GUARD = threading.Lock()


def _pz_lock(code: str):
    lk = _PZ_LOCKS.get(code)
    if lk is None:
        with _PZ_LOCKS_GUARD:
            lk = _PZ_LOCKS.get(code)
            if lk is None:
                lk = threading.Lock()
                _PZ_LOCKS[code] = lk
    return lk


def fetch_pingzhong(code: str, force: bool = False, need=None) -> dict:
    """一次请求解析 pingzhongdata,返回统一字段字典(失败返回 {} )。

    返回键:
      m1/m3/m6/y1   区间收益 %(直接取 syl_*, 不需要净值序列推算)
      manager       现任基金经理姓名
      manager_info  经理详情 {name, workTime, fundSize, star, power}
      scale         最新规模(亿元, 取自 Data_fluctuationScale 最后一期)
      scale_date    该规模对应的报告期
      navs          全量净值序列 [{date, dwjz, ljjz, jzzzl}] 升序
      name          基金简称
      similar_rank  同类排名信息(若有)

    need —— v0.93.0 新增的惰性解析开关:
      None(默认)        全量解析, 与旧行为完全一致
      字段名的集合/元组  只解析需要的部分, 未点名的昂贵字段直接跳过

      实测(8 只抽样, 响应 60~324KB):
        全量解析   0.049s/只   缓存常驻 565KB/只(满缓存 600 只 = 331MB)
        只要档案   0.0003s/只  缓存常驻  ~2KB/只
      日常更新只点名 m1/m3/m6/y1/scale/manager, 净值序列根本不看,
      跳过它可以省掉 99% 的解析耗时和缓存内存。
    """
    want_full = need is None
    want_navs = want_full or "navs" in need
    want_mgr_info = want_full or "manager_info" in need
    want_similar = want_full or "similar_rank" in need

    # --- 1) 净值走独立缓存(主缓存刻意不存 navs, 否则 600 只 = 331MB) ---
    navs = None
    if want_navs and not force:
        navs = _nav_cache_get(code)
        if navs is None:
            raw = _pz_raw_get(code)          # 15 分钟内的原始文本还能复用, 不重复打网络
            if raw is not None:
                navs = _parse_navs(raw) or None
                if navs:
                    _lru_put(_NAV_CACHE, code, navs, _NAV_MAX)

    # --- 2) 档案字段走主缓存 ---
    if not force:
        cached = _pz_cache_get(code)
        if cached is not None:
            # 命中缓存: 若要净值但缓存已过期, 才需要重新走网络
            if not want_navs or navs is not None:
                out = dict(cached)
                if navs:
                    out["navs"] = navs
                if not want_mgr_info:
                    out.pop("manager_info", None)
                if not want_similar:
                    out.pop("similar_rank", None)
                return out

    # --- 3) 走网络(v1.1.4: 单飞锁保护, 同基金并发只放一个线程真正下载) ---
    with _pz_lock(code):
        navs = None
        txt = None
        if not force:
            # 双检: 等锁期间别的线程可能已完成抓取并写好两级缓存
            if want_navs:
                navs = _nav_cache_get(code)
                if navs is None:
                    raw = _pz_raw_get(code)
                    if raw is not None:
                        navs = _parse_navs(raw) or None
                        if navs:
                            _lru_put(_NAV_CACHE, code, navs, _NAV_MAX)
            cached = _pz_cache_get(code)
            if cached is not None and (not want_navs or navs is not None):
                out = dict(cached)
                if navs:
                    out["navs"] = navs
                if not want_mgr_info:
                    out.pop("manager_info", None)
                if not want_similar:
                    out.pop("similar_rank", None)
                return out
            txt = _pz_raw_get(code)   # 15 分钟内的原始文本还能复用, 不重复打网络
        if txt is None:
            txt = _get(f"https://fund.eastmoney.com/pingzhongdata/{code}.js",
                       referer=f"https://fund.eastmoney.com/{code}.html")
            if txt:
                _lru_put(_PZ_RAW, code, txt, _PZ_RAW_MAX)
        if not txt:
            _pz_cache_set(code, {})
            return {}

        out = _parse_pz_profile(txt, want_navs=want_navs,
                                want_mgr_info=want_mgr_info,
                                want_similar=want_similar)
        # 主缓存剔除 navs, 让 600 个槽位全部留给轻量的档案字段
        _pz_cache_set(code, {k: v for k, v in out.items() if k != "navs"})
        if out.get("navs"):
            _lru_put(_NAV_CACHE, code, out["navs"], _NAV_MAX)
        return out


def fetch_fund_returns(code: str) -> dict:
    """从pingzhongdata直接拉取阶段涨跌幅（避免从净值历史计算）。
    返回: {m1, m3, m6, y1} 单位%，失败返回空dict。
    syl_1y=近1月, syl_3y=近3月, syl_6y=近6月, syl_1n=近1年

    v0.92.0: 改为复用 fetch_pingzhong() 的单次请求缓存, 与净值/经理/规模共用同一份
             响应, 避免同一基金重复下载 106KB。
    """
    pz = fetch_pingzhong(code, need=("m1", "m3", "m6", "y1"))
    return {k: pz[k] for k in ("m1", "m3", "m6", "y1") if pz.get(k) is not None}
