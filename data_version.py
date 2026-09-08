"""
data_version.py —— 数据版本戳管理

用于解决前端缓存污染问题：
- 每次一键更新完成后，更新 data_version 为当前时间戳
- 前端 localStorage 缓存数据时，同时保存 data_version
- 加载缓存时，对比版本戳，不一致则强制重新抓取

使用方式：
    from data_version import get_data_version, bump_data_version
    
    # 获取当前数据版本
    version = get_data_version()
    
    # 一键更新完成后，更新版本戳
    bump_data_version()
"""
from __future__ import annotations

import time
from typing import Optional

import db

# settings 表中的 key
DATA_VERSION_KEY = "data_version"

# 内存缓存，避免每次都查数据库
_cached_version: Optional[str] = None
_cache_time: float = 0

# 缓存有效期（秒），避免频繁查数据库
_CACHE_TTL = 60


def get_data_version() -> str:
    """获取当前数据版本戳。
    
    Returns:
        数据版本字符串（时间戳），如果不存在则返回 "0"
    """
    global _cached_version, _cache_time
    
    # 检查内存缓存
    now = time.time()
    if _cached_version is not None and (now - _cache_time) < _CACHE_TTL:
        return _cached_version
    
    # 从数据库读取
    try:
        conn = db.get_conn()
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?",
            (DATA_VERSION_KEY,)
        ).fetchone()
        if row and row["value"]:
            _cached_version = row["value"]
        else:
            _cached_version = "0"
    except Exception:
        _cached_version = "0"
    
    _cache_time = now
    return _cached_version


def bump_data_version() -> str:
    """更新数据版本戳为当前时间戳。
    
    一键更新完成后调用此函数，通知前端所有缓存失效。
    
    Returns:
        新的数据版本字符串
    """
    global _cached_version, _cache_time
    
    new_version = str(int(time.time()))
    
    try:
        conn = db.get_conn()
        conn.execute(
            """INSERT INTO settings (key, value, updated_at)
               VALUES (?, ?, datetime('now'))
               ON CONFLICT(key) DO UPDATE SET
                   value = excluded.value,
                   updated_at = excluded.updated_at""",
            (DATA_VERSION_KEY, new_version)
        )
        conn.commit()
    except Exception as e:
        # 即使数据库写入失败，也更新内存缓存
        print(f"[data_version] 写入数据库失败: {e}")
    
    _cached_version = new_version
    _cache_time = time.time()
    
    return new_version


def invalidate_cache() -> None:
    """使内存缓存失效，下次调用 get_data_version 时重新从数据库读取。"""
    global _cached_version, _cache_time
    _cached_version = None
    _cache_time = 0
