# 投研看板 — 部署指南

## 项目概览
- **技术栈**: FastAPI + SQLite + APScheduler + 原生前端(HTML/CSS/JS)
- **入口**: `python main.py 8000` (API) / `python collector/schedule.py` (采集器)
- **依赖**: fastapi, uvicorn, apscheduler, httpx, markdown (见 requirements.txt)
- **数据**: SQLite (`fund.db`, 预种子 6001 只基金 / 358k 条净值)
- **端口**: 8000

## 架构说明：API 与采集器分离

v0.94.0 起，系统支持 **API 服务**与**采集调度器**分离部署：

| 角色 | 职责 | 启动命令 |
|---|---|---|
| **api** | FastAPI 网页和 API 服务 | `python main.py 8000` |
| **collector** | APScheduler 定时采集（20:30 全量采集 + 07/12/20 资讯） | `python collector/schedule.py` |

两者共享同一个 SQLite 数据库（`/data/fund.db`），SQLite WAL 模式支持一写多读。采集完成后通过 `task_logs` 表通知 API 侧缓存自动失效。

---

## 部署方式一：docker-compose（推荐，双容器）

同时启动 API + 采集器两个容器，采集时不影响网页访问。

```bash
# 1. 构建并启动
docker compose up -d

# 2. 查看状态
docker compose ps

# 3. 查看日志
docker compose logs -f api        # API 日志
docker compose logs -f collector  # 采集器日志

# 4. 停止
docker compose down
```

访问：http://localhost:8000

### docker-compose 环境变量
在项目根目录创建 `.env` 文件（可选）：
```env
API_KEYS=你的密钥           # 默认 dev-key-123，生产环境务必修改
COLLECT_MAX=6000            # 全量采集基金数上限
```

---

## 部署方式二：Docker 单容器

适合资源有限的场景，API 和采集器在同一个容器内运行（采集时可能影响响应速度）。

```bash
# 构建
docker build -t invest-dashboard .

# 运行（默认 API 模式，同时后台启动采集器）
docker run -p 8000:8000 \
  -e API_KEYS=你的密钥 \
  -v ./data:/data \
  invest-dashboard
```

### 单容器双进程模式（兼容旧版）
如果需要在单容器内同时运行 API + 采集器，可以使用旧的启动命令：
```bash
docker run -p 8000:8000 -v ./data:/data invest-dashboard \
  sh -c "mkdir -p /data && [ ! -f /data/fund.db ] && cp /app/fund.db /data/fund.db; \
  nohup python collector/schedule.py > /tmp/schedule.log 2>&1 & \
  exec python main.py 8000"
```

---

## 部署方式三：腾讯云 CloudBase 云托管

1. 登录 [CloudBase 控制台](https://console.cloud.tencent.com/tcb)
2. 新建环境 → 云托管 → 新建服务
3. 上传代码(本项目根目录 zip)或关联 Git 仓库
4. 配置:
   - 监听端口: 8000
   - 启动命令: (留空, 使用 Dockerfile 默认 API 模式)
   - 容量: 1核 2G 起步
5. 挂载 CFS 持久化存储到 `/data`
6. 如需独立采集器，再建一个服务，启动命令设为 `collector`，同样挂载 CFS

---

## 部署方式四：Render（最简单，免运维）

1. 登录 [render.com](https://render.com)
2. New → Web Service
3. 关联 Git 仓库或上传代码
4. 配置:
   - Runtime: Docker
   - Instance Type: Starter ($7/月) 或 Standard ($25/月)
   - 环境变量: `API_KEYS=你的密钥`, `ROLE=api`
5. 注意: Render 免费层磁盘不持久化, fund.db 重启会重置; 需付费层或外接数据库
6. 采集器可另建一个 Background Worker，环境变量设 `ROLE=collector`

---

## 部署方式五：自有服务器 / VPS

```bash
# 1. 安装 Python 3.11+ 和 pip
# 2. 上传项目到服务器
scp -r invest-dashboard user@server:/opt/
cd /opt/invest-dashboard

# 3. 安装依赖
pip install -r requirements.txt

# 4. 启动 API 服务（前台或 systemd）
python main.py 8000

# 5. 另开一个终端启动采集器
python collector/schedule.py

# 6. 配置 nginx 反向代理（参考 nginx.conf）
```

### systemd 服务配置示例

**API 服务** (`/etc/systemd/system/invest-api.service`):
```ini
[Unit]
Description=Invest Dashboard API
After=network.target

[Service]
Type=simple
User=www-data
WorkingDirectory=/opt/invest-dashboard
Environment="DB_PATH=/opt/invest-dashboard/data/fund.db"
Environment="API_KEYS=你的密钥"
ExecStart=/usr/bin/python main.py 8000
Restart=always

[Install]
WantedBy=multi-user.target
```

**采集器** (`/etc/systemd/system/invest-collector.service`):
```ini
[Unit]
Description=Invest Dashboard Collector
After=network.target

[Service]
Type=simple
User=www-data
WorkingDirectory=/opt/invest-dashboard
Environment="DB_PATH=/opt/invest-dashboard/data/fund.db"
Environment="FULL_MARKET=1"
Environment="COLLECT_MAX=6000"
ExecStart=/usr/bin/python collector/schedule.py
Restart=always

[Install]
WantedBy=multi-user.target
```

---

## 部署后验证

1. 访问首页 `http://<域名>/` → 应看到投研看板
2. 检查榜单数据 → 应显示最新榜单（预种子或采集后）
3. 首次全量采集约 5-7 分钟（采集器容器自动运行），期间数据逐步更新
4. API 文档: `http://<域名>/docs`
5. 健康检查: `http://<域名>/api/health`

---

## 环境变量清单

| 变量 | 默认值 | 说明 |
|---|---|---|
| `ROLE` | `api` | 运行角色：`api` / `collector` |
| `PORT` | `8000` | API 服务端口 |
| `DB_PATH` | `./fund.db` | SQLite 数据库路径 |
| `API_KEYS` | `dev-key-123` | API 密钥（逗号分隔多个），生产环境务必修改 |
| `FULL_MARKET` | `0` | 是否全市场采集模式（1=开启） |
| `COLLECT_MAX` | `6000` | 全量采集基金数上限 |
| `TZ` | `Asia/Shanghai` | 时区 |
| `REQUIRE_READ_KEY` | `0` | 读操作是否也需要 API Key（1=需要） |

---

## 注意事项

- **首次启动**: 采集器容器会立即执行一次全量采集（约 5-7 分钟），CPU/网络占用较高；API 容器不受影响
- **数据持久化**: 务必挂载持久化存储到 `/data`，否则容器重启后采集数据丢失
- **API 密钥**: 修改 `API_KEYS` 环境变量，不要用默认值
- **fund.db 大小**: 预种子 46MB，全量采集后可能增长到 100-200MB
- **时区**: 已设置 `TZ=Asia/Shanghai`，采集时间按北京时间
- **SQLite 并发**: API 容器（读）和采集器容器（写）共享数据库，WAL 模式支持一写多读；如遇 `database is locked` 错误，可增大 `busy_timeout`（已设 5000ms）
