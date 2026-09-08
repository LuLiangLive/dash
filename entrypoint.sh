#!/bin/sh
# entrypoint.sh —— 投研看板统一入口
# 运行模式：启动 FastAPI 服务（ROLE 环境变量保留兼容，固定使用 api）。
# 注：APScheduler 采集调度已于 v2.8.1 移除（云端为单 web 进程部署，
#     采集统一由页面「一键更新」/api/fetch/start 手动触发）。
#
# 数据初始化：确保 /data 目录存在，若 fund.db 不存在则从镜像内 seed 一份。

set -e

# 1. 数据目录初始化
mkdir -p /data
if [ ! -f /data/fund.db ]; then
    if [ -f /app/fund.db ]; then
        echo "[entrypoint] 初始化数据库: /app/fund.db -> /data/fund.db"
        cp /app/fund.db /data/fund.db
    else
        echo "[entrypoint] 警告: 镜像内未找到 fund.db，将由应用自动创建空库"
    fi
fi

# 2. 确定运行角色（历史兼容：collector 参数已废弃，一律按 api 处理）
ROLE="api"

echo "[entrypoint] 运行模式: $ROLE"
echo "[entrypoint] 数据目录: /data"
echo "[entrypoint] 时区: $TZ"

# 3. 启动服务
echo "[entrypoint] 启动 FastAPI 服务 (端口 ${PORT:-8002})..."
exec python main.py "${PORT:-8002}"
