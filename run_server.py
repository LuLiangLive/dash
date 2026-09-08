"""
run_server.py —— 云端部署启动器
读取 PORT 环境变量(发布平台注入),绑定 0.0.0.0,供单端口 HTTP 服务发布。
用法: python run_server.py
"""
import os

# 公开部署模式：默认不强制鉴权，如需关闭公开访问设 REQUIRE_READ_KEY=1
os.environ.setdefault("REQUIRE_READ_KEY", "0")

import uvicorn

import main  # 导入即加载 FastAPI app 及顶层依赖

PORT = int(os.environ.get("PORT", "7860"))
uvicorn.run(main.app, host="0.0.0.0", port=PORT)
