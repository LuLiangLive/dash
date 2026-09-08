# -*- coding: utf-8 -*-
"""
main_handler.py —— CloudBase(腾讯云开发)云函数入口

把 TCB 云函数 HTTP 触发事件(event)适配成 FastAPI(ASGI)请求,并支持定时触发跑采集。

- HTTP 访问(如 /、/api/ranks):转发给 FastAPI app,返回标准集成响应
- 定时触发器(无 httpMethod):执行一次采集(每天 20:30 由平台定时触发器调用)

部署:在 CloudBase 云函数新建「invest-api」,运行环境 Python,入口 main_handler,
把本项目代码打包上传;另建「invest-collector」或同一函数配置定时触发器。

数据持久化:设置环境变量 DB_PATH 指向持久化挂载(如 CFS /mnt/cfs/fund.db);
若平台不支持挂载,需改用云数据库/COS(见 deploy/CLOUDBASE_FUNCTION.md)。
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
from urllib.parse import urlencode


def _ensure_db() -> None:
    """云函数无状态:在可写位置准备 SQLite。

    - 若 DB_PATH 未设置:云函数(POSIX)默认 /tmp/fund.db(挂载 CFS 可设 DB_PATH=/mnt/cfs/fund.db)
    - 若目标不存在,从函数包内 data/fund.db 或 fund.db seed 一份(带初始榜单数据)
    """
    if os.name != "posix":
        return  # 本地 Windows 保持默认路径
    dbp = os.environ.get("DB_PATH", "")
    if not dbp:
        dbp = "/tmp/fund.db"
        os.environ["DB_PATH"] = dbp
    try:
        os.makedirs(os.path.dirname(dbp), exist_ok=True)
    except Exception:
        pass
    if not os.path.exists(dbp):
        here = os.path.dirname(os.path.abspath(__file__))
        for src in (os.path.join(here, "fund.db"), os.path.join(here, "data", "fund.db")):
            if os.path.exists(src):
                try:
                    import shutil

                    shutil.copy(src, dbp)
                except Exception as e:  # noqa: BLE001
                    pass
                break


_ensure_db()

from main import app  # FastAPI 实例(复用全部路由/静态托管/服务端渲染)  # noqa: E402


def _to_scope(event: dict):
    """TCB HTTP 事件 → (ASGI scope, body)。"""
    method = event.get("httpMethod") or "GET"
    path = event.get("path") or "/"
    headers = event.get("headers") or {}
    qs = event.get("queryStringParameters") or event.get("queryString") or {}
    qs_str = urlencode(qs) if isinstance(qs, dict) else str(qs or "")

    body = event.get("body") or b""
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body) if isinstance(body, str) else body
    elif isinstance(body, str):
        body = body.encode("utf-8")

    raw_headers = []
    for k, v in (headers or {}).items():
        raw_headers.append((str(k).lower().encode("latin1"), str(v).encode("latin1")))

    scheme = (headers or {}).get("x-forwarded-proto") or "https"
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": scheme,
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": qs_str.encode("utf-8"),
        "root_path": "",
        "headers": raw_headers,
        "client": ("cloudbase", 0),
        "server": ("tcb", 443),
        "state": {},
    }
    return scope, body


async def _dispatch(scope, body):
    """调用 FastAPI app,收集响应。"""
    response = {}
    messages = []

    async def receive():
        if messages:
            return messages.pop(0)
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            response["status"] = message["status"]
            response["headers"] = {
                k.decode("latin1"): v.decode("latin1") for k, v in message.get("headers", [])
            }
        elif message["type"] == "http.response.body":
            response["body"] = (response.get("body") or b"") + (message.get("body") or b"")

    await app(scope, receive, send)
    return response


def main_handler(event, context=None):
    """TCB 云函数入口。event 无 httpMethod 时视为定时触发(采集)。"""
    # 定时触发 / 控制台测试
    if not event or not event.get("httpMethod"):
        from collector.pipeline import run_collector

        result = run_collector(max_funds=int(os.environ.get("COLLECT_MAX", "63")))
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json; charset=utf-8"},
            "body": json.dumps(result, ensure_ascii=False),
        }

    scope, body = _to_scope(event)
    try:
        response = asyncio.run(_dispatch(scope, body))
    except Exception as e:  # 兜底,避免云函数 502
        return {
            "statusCode": 500,
            "headers": {"Content-Type": "text/plain; charset=utf-8"},
            "body": f"internal error: {e}",
        }
    return {
        "statusCode": response.get("status", 500),
        "headers": response.get("headers", {"Content-Type": "text/plain; charset=utf-8"}),
        "body": (response.get("body") or b"").decode("utf-8", errors="replace"),
        "isBase64Encoded": False,
    }


if __name__ == "__main__":
    # 本地自测:模拟 TCB HTTP 事件
    import sys

    test = sys.argv[1] if len(sys.argv) > 1 else "/api/health"
    ev = {"path": test, "httpMethod": "GET", "headers": {}, "queryStringParameters": {}}
    out = main_handler(ev)
