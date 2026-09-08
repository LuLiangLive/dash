"""
modules/rank/fetcher.py —— 榜单数据抓取

从 collector.fetcher 迁移（v2.5.5架构重构 - 阶段3）
负责：按类型拉取排行、全市场模式合并
"""
from __future__ import annotations

import re

from collector.http_utils import _get


def fetch_rank(ft: str = "zs", pn: int = 20000) -> list[dict]:
    """按类型拉取排行(ft=gp/zs/hh/qdii),返回 [{code, name, date, rzf, zzf}]。"""
    url = (f"https://fund.eastmoney.com/data/rankhandler.aspx?op=ph&dt=kf&ft={ft}"
           f"&rs=&gs=0&sc=1nzf&st=desc&qdii=&tabSubtype=,,,,,&pi=1&pn={pn}&dx=1")
    txt = _get(url, referer="https://fund.eastmoney.com/data/fundranking.html")
    if not txt:
        return []
    m = re.search(r"datas:\[(.*?)\]", txt, re.S)
    if not m:
        return []
    out = []
    for item in re.findall(r'"([^"]+)"', m.group(1)):
        f = item.split(",")
        if len(f) < 17:
            continue
        try:
            out.append({"code": f[0], "name": f[1], "date": f[3],
                        "rzf": float(f[6]), "zzf": float(f[7])})
        except Exception:
            pass
    return out


def fetch_all_market() -> list[dict]:
    """分类型全量合并(股票+指数+混合+QDII),按 code 去重。"""
    merged = {}
    for ft in ("gp", "zs", "hh", "qdii"):
        for f in fetch_rank(ft):
            merged[f["code"]] = f
    return list(merged.values())
