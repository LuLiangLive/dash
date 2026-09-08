"""
modules/fund/profile_combined.py —— 统一档案组合

从 collector.fetcher 迁移（v2.5.5架构重构 - 阶段4）
负责：按需组合pingzhongdata + f10 + 移动端三个源，返回统一档案字段
"""
from __future__ import annotations

import time

from modules.fund.pingzhong import fetch_pingzhong
from modules.fund.mobile import fetch_mobile_profile
from modules.fund.profile import fetch_f10_profile, fetch_basic

# 各字段的数据源归属 —— 决定 fetch_profile 该打哪些请求, 避免无谓下载
#
# v0.92.0 实测(30 只抽样):
#   pingzhongdata 并发3~4 = 20ms/只 ; f10 jbgk 并发4 = 51ms/只
#   并发再高反而变慢(并发10 时 220ms/只), 东方财富对高频并发有限流。
# 因此把 scale/manager 也归到 pingzhongdata: 日常刷新 m1/m3/m6/y1/scale/manager
# 只需 1 次请求(20ms), 只有成立日/跟踪标的等静态字段才需要额外打 f10(且只打一次)。
_PZ_ONLY = {"m1", "m3", "m6", "y1", "navs", "manager_info", "similar_rank", "name"}
_F10_ONLY = {"est", "track", "ftype", "company", "mgr_fee", "benchmark", "fullname", "shares"}
# v0.93.0: 移动端接口可覆盖的字段 —— 这些优先走 3.6KB 的移动端, 老源降级为兜底
_MOB_ONLY = {"name", "m1", "m3", "m6", "y1", "ftype", "manager", "est", "scale"}
# 移动端拿不到, 只能走老源: track(跟踪标的) / navs(净值序列) / 僵尸字段
_ONLY_OLD = {"track", "navs", "manager_info", "similar_rank",
             "company", "mgr_fee", "benchmark", "fullname", "shares"}
# 两边都有, 但以 pingzhongdata 为准(省掉一次 f10 请求): manager / scale
#   scale: pingzhongdata 的 Data_fluctuationScale 与 f10「净资产规模」实测一致
#   manager: pingzhongdata 的 Data_currentFundManager 即现任经理, 与 f10 一致


def fetch_profile(code: str, need: tuple = ("m1", "m3", "m6", "y1", "manager",
                                            "scale", "est", "track")) -> dict:
    """按需组合 pingzhongdata + f10 两个源,返回统一档案字段。

    need 里只要 _PZ_ONLY 的键 -> 只打 1 次请求(约 0.04s)
    need 里含 _F10_ONLY 的键 -> 追加 1 次请求(约 0.18s), 两个源并发执行
    返回: {m1,m3,m6,y1,manager,scale,est,track,ftype,company,mgr_fee,benchmark,...}
    """
    need = tuple(need or ())
    _both = {"manager", "scale"}

    # --- 1) 移动端优先: 3.6KB 覆盖 name/m1..y1/ftype/manager/est/scale ---
    mob: dict = {}
    if _MOB_ONLY.intersection(need):
        try:
            mob = fetch_mobile_profile(code, need=tuple(need)) or {}
        except Exception:
            mob = {}

    # --- 2) 老源「只补缺」: 移动端已给出的字段一律不再打老源 ---
    # 关键: 早期版本这里写成 bool(_PZ_ONLY.intersection(need)), 只要 need 里
    # 有 m1 就无条件去打 48KB 的 pingzhongdata —— 结果新链路变成
    # 移动端2次 + pingzhongdata + f10 = 4 次请求, 实测反而慢一倍(5.6s vs 2.7s)。
    # 必须按「还缺什么」来决定, 而不是按「要什么」。
    # 另外这里刻意不嵌套线程池: 外层调用方(如 rank_full._fill_listed_profiles)
    # 已经并发, 若每只基金内部再开线程, 实际并发翻倍会触发东方财富限流
    # ——实测并发10(内层×2)时 220ms/只, 比并发4 的 121ms/只慢近一倍。
    miss = [k for k in need if mob.get(k) is None]

    pz: dict = {}
    f10: dict = {}
    if any(k in _PZ_ONLY or k in _both for k in miss):
        try:
            # need 透传, 让 fetch_pingzhong 惰性解析(跳过用不到的净值序列)
            pz = fetch_pingzhong(code, need=tuple(miss)) or {}
        except Exception:
            pz = {}
    if any(k in _F10_ONLY and pz.get(k) is None for k in miss):
        try:
            f10 = fetch_f10_profile(code) or {}
        except Exception:
            f10 = {}

    out: dict = {}
    for k in need:
        # 优先级: 移动端(3.6KB) > pingzhongdata > f10
        v = mob.get(k)
        if v is None:
            v = pz.get(k)
        if v is None:
            v = f10.get(k)
        if v is not None:
            out[k] = v
    return out


_F10_PROFILE_CACHE: dict[str, tuple[float, dict]] = {}
_F10_TTL = 12 * 3600


def fetch_f10_profile_cached(code: str) -> dict:
    """带 12h 缓存的 f10 概况(成立日/跟踪标的等低频变动字段无需重复抓)。"""
    hit = _F10_PROFILE_CACHE.get(code)
    if hit and time.time() - hit[0] < _F10_TTL:
        return hit[1]
    val = fetch_f10_profile(code)
    if len(_F10_PROFILE_CACHE) > 3000:
        for k in sorted(_F10_PROFILE_CACHE, key=lambda k: _F10_PROFILE_CACHE[k][0])[:600]:
            _F10_PROFILE_CACHE.pop(k, None)
    _F10_PROFILE_CACHE[code] = (time.time(), val)
    return val


def fetch_basic_many(codes: list[str], workers: int = 5) -> dict:
    """并行抓取基金概况,返回 {code: {"scale": .., "est": ..}}。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import threading

    lock = threading.Lock()
    results = {}

    def work(code):
        scale, est = fetch_basic(code)
        with lock:
            results[code] = {"scale": scale, "est": est}
        time.sleep(0.12)
        return code

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(work, c) for c in codes]
        for fut in as_completed(futures):
            try:
                fut.result()
            except Exception:
                pass
    return results
