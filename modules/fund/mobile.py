"""
modules/fund/mobile.py —— 移动端接口

从 collector.fetcher 迁移（v2.5.5架构重构 - 阶段4）
负责：fundmobapi移动端接口，用3.6KB取代59KB的pingzhongdata+f10
"""
from __future__ import annotations

import json
import os
import time

from collector.http_utils import _get

# v0.93.0: 移动端接口 —— 用 3.6 KB 取代 59 KB 的「pingzhongdata + f10 jbgk」
#
# 实测(8~60 只抽样, 并发4):
#   旧链路 pingzhongdata(48KB, 内含 600~6000 条全量净值) + f10 jbgk(11KB)
#         = 216.6 KB/只 / 80 ms/只
#   新链路 FundMNBasicInformation(2.3KB) + FundMNDetailInformation(1.3KB)
#         =   9.7 KB/只 / 65 ms/只        → 流量降 95.5%, 耗时降 19%
# 字段一致性实测 8/8 完全一致: m1/m3/m6/y1/manager/est/ftype/name
#   (scale 取 ENDNAV「期末净资产」 ÷1e8, 与 Data_fluctuationScale 一致;
#    注意不是 NETNAV, 后者是「净资产」口径, 实测 0/10 对不上)
# 移动端不提供: track(跟踪标的) / navs(净值序列) —— 仍走老源, 见 _F10_ONLY / need
#
# ⚠️ 重要: 默认关闭, 不要在生产环境当主链路用。
#   压测记录: 前 ~200 次请求全部成功(120/120), 之后接口开始返回
#   ErrCode=61136403「网络繁忙, 请稍后重试」, 且冷却 30s、并发降到 2 仍 0/20 全败,
#   即东方财富对 fundmobapi 有较硬的频次封禁。一旦被封, 每条基金都要
#   多打 2 次失败请求再回落到老源, 实测端到端反而慢一倍(5.7s vs 2.6s / 40 只)。
#   因此保留实现 + 熔断, 但默认不走;需要时设环境变量 FUND_MOBILE_API=1 开启。

MOBILE_ENABLED = os.environ.get("FUND_MOBILE_API", "0") == "1"
_MOB_FAILS = 0            # 连续失败计数
_MOB_DISABLE_UNTIL = 0.0  # 熔断截止时间戳
_MOB_FAIL_TRIP = 6        # 连续失败多少次后熔断
_MOB_COOLDOWN = 30 * 60   # 熔断 30 分钟后再给一次机会
_MOB_BASIC_URL = ("https://fundmobapi.eastmoney.com/FundMNewApi/"
                  "FundMNBasicInformation?FCODE={code}&deviceid=1&plat=Android"
                  "&product=EFund&version=1")
_MOB_DETAIL_URL = ("https://fundmobapi.eastmoney.com/FundMNewApi/"
                   "FundMNDetailInformation?FCODE={code}&deviceid=1&plat=Android"
                   "&product=EFund&version=1")

# Basic 提供的字段 / Detail 提供的字段 —— 据此决定要不要打第二个请求
_MOB_BASIC_ONLY = {"name", "m1", "m3", "m6", "y1", "ftype"}
_MOB_DETAIL_ONLY = {"manager", "est", "scale"}
_MOB_ALL = _MOB_BASIC_ONLY | _MOB_DETAIL_ONLY

_MOB_CACHE: dict[str, tuple[float, dict]] = {}
_MOB_TTL = 6 * 3600
_MOB_MAX = 800
_MOB_NEG_TTL = 10 * 60


def _mob_num(v):
    """移动端数值: '--'/空 都当缺失, 避免把 '--' 当 0 写库。"""
    if v in (None, "", "--"):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _mob_get(url: str) -> dict:
    txt = _get(url)
    if not txt:
        return {}
    try:
        d = json.loads(txt)
    except Exception:
        return {}
    v = d.get("Datas")
    return v if isinstance(v, dict) else {}


def mobile_available() -> bool:
    """移动端接口当前是否可用(开关打开 + 未被熔断)。"""
    if not MOBILE_ENABLED:
        return False
    return time.time() >= _MOB_DISABLE_UNTIL


def _mob_trip():
    """连续失败达到阈值就熔断, 避免被封后每只基金都白打 2 次请求。"""
    global _MOB_FAILS, _MOB_DISABLE_UNTIL
    _MOB_FAILS += 1
    if _MOB_FAILS >= _MOB_FAIL_TRIP:
        _MOB_DISABLE_UNTIL = time.time() + _MOB_COOLDOWN
        _MOB_FAILS = 0


def fetch_mobile_profile(code: str, need=None) -> dict:
    """移动端接口拉基金档案(3.6KB), 取代 59KB 的 pingzhongdata + f10 jbgk。

    need=None 取全部; 否则按 need 决定只打 Basic 还是 Basic+Detail。
    未开启(MOBILE_ENABLED=False)或已熔断时直接返回 {}, 调用方应回落到老源。
    """
    if not mobile_available():
        return {}
    want = _MOB_ALL if need is None else set(need)
    hit = _MOB_CACHE.get(code)
    if hit and time.time() - hit[0] < (_MOB_TTL if hit[1] else _MOB_NEG_TTL):
        cached = hit[1]
        if all(k in cached or k not in want for k in want):
            return {k: v for k, v in cached.items() if k in want}

    out: dict = {}
    b = _mob_get(_MOB_BASIC_URL.format(code=code))
    if b:
        nm = (b.get("SHORTNAME") or "").strip()
        if nm:
            out["name"] = nm
        for src, dst in (("SYL_Y", "m1"), ("SYL_3Y", "m3"),
                         ("SYL_6Y", "m6"), ("SYL_1N", "y1")):
            v = _mob_num(b.get(src))
            if v is not None:
                out[dst] = v
        ft = (b.get("FTYPE") or "").strip()
        if ft and ft != "--":
            out["ftype"] = ft

    if want & _MOB_DETAIL_ONLY:
        d = _mob_get(_MOB_DETAIL_URL.format(code=code))
        if d:
            jl = (d.get("JJJL") or "").strip()
            if jl and jl != "--":
                # 多位经理逗号分隔, 与旧口径(pingzhongdata 取现任第一位)保持一致
                out["manager"] = [x.strip() for x in jl.split(",") if x.strip()][0]
            est = (d.get("ESTABDATE") or "").strip()
            if est and est != "--":
                out["est"] = est
            # ENDNAV = 期末净资产(元) → 亿元; 与 Data_fluctuationScale 口径一致
            sc = _mob_num(d.get("ENDNAV"))
            if sc:
                out["scale"] = round(sc / 1e8, 2)

    # 熔断计数: 拿到任何字段都算成功(不同基金的有效字段数本就不同),
    # 只有完全空才计入失败, 避免正常基金被误判成接口故障。
    if out:
        global _MOB_FAILS
        _MOB_FAILS = 0
    else:
        _mob_trip()

    if len(_MOB_CACHE) >= _MOB_MAX:
        for k in sorted(_MOB_CACHE, key=lambda k: _MOB_CACHE[k][0])[: _MOB_MAX // 5]:
            _MOB_CACHE.pop(k, None)
    prev = _MOB_CACHE.get(code)
    merged = dict(prev[1]) if prev else {}
    merged.update(out)
    _MOB_CACHE[code] = (time.time(), merged)
    return {k: v for k, v in out.items() if k in want}
