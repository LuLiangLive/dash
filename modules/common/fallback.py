"""
modules/common/fallback.py —— 本地降级数据

从 collector.fetcher 迁移（v2.5.5架构重构 - 阶段1）
负责：本地JSON文件读取、净值数据兜底
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
DATA = ROOT / "data"


def load_json(name: str) -> dict:
    """从 data/ 目录读取 JSON 文件，失败返回空字典。"""
    p = DATA / name
    if not p.exists():
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def local_nav_fallback(code: str) -> list[dict]:
    """从 nav_cache.json 读取本地净值兜底(带 6 位指数码如 000001 映射)。"""
    cache = load_json("nav_cache.json")
    items = cache.get("items", {}).get(code, [])
    return [{"date": x.get("date"), "ljjz": x.get("ljjz"), "dwjz": x.get("dwjz")} for x in items]


# 兼容旧名称（带下划线前缀）
_load_json = load_json
