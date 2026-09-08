# ============================================================================
# Dockerfile —— 投研看板后端（FastAPI + SQLite），适配 Fly.io 部署
# 构建: docker build -t touyan-board .
# 运行: docker run -p 8080:8080 -e PORT=8080 -e DB_PATH=/data/fund.db -v ./data:/data touyan-board
# ============================================================================

# Stage 1：构建阶段（安装编译依赖 + pip 安装）
FROM python:3.11-slim AS builder

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# Stage 2：运行阶段（仅依赖 + 业务代码 + 种子数据库）
FROM python:3.11-slim AS runtime

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai

COPY --from=builder /install /usr/local

# 复制业务代码（.dockerignore 已排除 fund.db、tests、缓存等）
COPY . .

# 持久化数据目录（Fly.io volume 挂载到 /data）
RUN mkdir -p /data
ENV DB_PATH=/data/fund.db

EXPOSE 8080

# 启动：run_server.py 自动读取 PORT 环境变量（Fly.io 注入）
CMD ["python", "run_server.py"]
