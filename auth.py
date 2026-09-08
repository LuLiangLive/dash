"""
auth.py —— API Key 鉴权
策略:
- 读写操作统一遵循 REQUIRE_READ_KEY 开关
- 本机/内网默认 (REQUIRE_READ_KEY=0) 完全开放，开箱即用
- 外网部署设 REQUIRE_READ_KEY=1 时，读写操作均强制 X-API-Key
  （修正 v2.7.1 遗留：写操作此前始终强制鉴权，与本机默认开放的设计意图矛盾，
    导致前端未配置 API Key 时自选同步等写接口 401 静默失败）
Key 从统一配置 settings.api_keys 读取(环境变量 API_KEYS, 逗号分隔);
未配置时使用开发默认值 dev-key-123。
"""
from __future__ import annotations

from fastapi import Header, HTTPException

from config import settings

API_KEYS = settings.api_keys
REQUIRE_READ_KEY = settings.require_read_key


def _check(x_api_key: str):
    if x_api_key not in API_KEYS:
        raise HTTPException(status_code=401, detail="无效或缺失 API Key")


def require_api_key(x_api_key: str = Header(default=None, alias="X-Touyan-Key")):
    """写操作鉴权。本机模式(REQUIRE_READ_KEY=0)放行，外网模式强制校验。

    注意：header 用 X-Touyan-Key 而非 X-API-Key——发布平台网关会剥离
    X-API-Key 头（防伪造），导致应用永远收不到 key 而一律 401（实测定位）。
    """
    if REQUIRE_READ_KEY:
        _check(x_api_key)
    return x_api_key


def optional_api_key(x_api_key: str = Header(default=None, alias="X-Touyan-Key")):
    if REQUIRE_READ_KEY:
        _check(x_api_key)
    return x_api_key
