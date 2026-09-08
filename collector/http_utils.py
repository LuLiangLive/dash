"""
collector/http_utils.py —— HTTP请求工具函数

从 collector/fetcher.py 拆分（v2.5.5架构重构 - 阶段1）
负责：统一的GET请求、UA、超时配置
"""
import time
from typing import Optional

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"
TIMEOUT = 10


def _get(url: str, referer: Optional[str] = None, retries: int = 2) -> Optional[str]:
    """GET 文本,失败重试。

    v2026-08-29: urllib -> httpx
    """
    import httpx
    for i in range(retries + 1):
        try:
            resp = httpx.get(url, headers={
                "User-Agent": UA,
                "Accept": "*/*",
                "Referer": referer or "https://fund.eastmoney.com/",
            }, timeout=TIMEOUT)
            return resp.text
        except Exception:
            time.sleep(0.4 + i * 0.4)
            continue
    return None
