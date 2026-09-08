# ============================================================================
# Dockerfile —— 投研看板后端（FastAPI + SQLite），适配 Hugging Face Spaces
# 端口: 7860（HF Spaces Docker 默认端口）
# 数据: 存在容器内 /app/data/fund.db（重新部署会重置，需自行备份）
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
    TZ=Asia/Shanghai \
    PORT=7860 \
    DB_PATH=/app/data/fund.db

COPY --from=builder /install /usr/local

# 复制业务代码（.dockerignore 已排除 fund.db、tests、缓存等）
COPY . .

# 运行时数据目录（容器内，重部署会重置）
RUN mkdir -p /app/data

EXPOSE 7860

# 启动：run_server.py 读取 PORT 环境变量（HF Spaces 固定 7860）
CMD ["python", "run_server.py"]
