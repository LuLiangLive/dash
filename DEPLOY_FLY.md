# 投研看板 — Fly.io 部署指南

> 公开访问模式 + SQLite 持久化 + 一键更新自动采集

## 一、架构概览

```
GitHub (代码仓库, 含 Git LFS 种子库)
    │  git push main
    ▼
GitHub Actions (自动 CI/CD)
    │  flyctl deploy --remote-only
    ▼
Fly.io (单实例 Docker 容器)
    ├── 端口 8080，自动 HTTPS
    ├── /data 持久化 Volume (fund.db)
    └── 镜像内含 fund_core_data.db (种子数据)
```

## 二、前置准备

### 1. 安装工具

```bash
# 安装 Fly.io CLI
curl -L https://fly.io/install.sh | sh

# 安装 Git LFS（管理 124MB 种子数据库）
git lfs install
```

### 2. 注册账号

- Fly.io: https://fly.io （需绑定信用卡，免费额度约够 1 个小实例）
- GitHub: 已有即可

## 三、首次部署（约 10 分钟）

### Step 1: 准备代码仓库

```bash
cd backend

# 初始化 Git
git init
git lfs install
git lfs track "fund_core_data.db"

# 确认 .gitignore 已排除 fund.db（运行时数据）
cat .gitignore | grep fund.db

# 提交代码
git add .
git commit -m "init: 投研看板 v2.11.6 Fly.io 部署版"
git branch -M main

# 创建 GitHub 仓库并推送
# 在 GitHub 网页新建仓库，然后：
git remote add origin https://github.com/<你的用户名>/<仓库名>.git
git push -u origin main
```

> **注意**：`fund_core_data.db`（124MB）通过 Git LFS 提交，GitHub 免费版 LFS 额度 1GB。

### Step 2: 修改 fly.toml 应用名

```bash
# 编辑 fly.toml，把 app 名改成你自己的（全局唯一）
app = "你的应用名"  # 例如 touyan-board-zhangsan
```

### Step 3: 登录 Fly.io 并首次部署

```bash
fly auth login

# 首次部署会自动创建 app + volume
fly launch
# 或直接：
fly deploy
```

部署过程中 Fly.io 会：
1. 远程构建 Docker 镜像（包含 124MB 种子库）
2. 创建 `touyan_data` Volume 挂载到 `/data`
3. 启动容器，首次启动自动：
   - 创建 `/data/fund.db` 并执行数据库迁移
   - 从 `fund_core_data.db` 导入净值和基金基础信息（约 1-2 分钟）
   - 预计算分数

### Step 4: 验证部署

```bash
# 查看应用状态
fly status

# 查看日志（确认种子导入完成）
fly logs

# 打开网站
fly open
```

访问 `https://<你的应用名>.fly.dev`，应看到投研看板首页。

### Step 5: 执行一键更新

首次部署后，种子数据只有基础净值，需要在页面上点「一键更新」：
1. 打开网站 → 找到「一键更新」按钮
2. 点击启动全量采集（约 5-7 分钟）
3. 完成后榜单、行情、资讯等数据全部就绪

## 四、配置 GitHub Actions 自动部署

以后每次 `git push` 到 main 分支自动部署，无需手动 `fly deploy`。

### Step 1: 获取 Fly.io API Token

```bash
fly auth token
# 复制输出的 token
```

### Step 2: 在 GitHub 仓库配置 Secret

1. 仓库 → Settings → Secrets and variables → Actions
2. New repository secret:
   - Name: `FLY_API_TOKEN`
   - Value: 上一步复制的 token

### Step 3: 触发自动部署

```bash
# 随便改点什么，push 上去就会自动部署
git commit --allow-empty -m "trigger auto deploy"
git push
```

在仓库的 Actions 标签页可以查看部署进度。

## 五、日常运维

### 查看日志
```bash
fly logs
```

### SSH 进入容器
```bash
fly ssh console
```

### 查看数据库
```bash
fly ssh console
sqlite3 /data/fund.db ".tables"
```

### 备份数据库
```bash
fly sftp get /data/fund.db ./fund_backup.db
```

### 重新部署
```bash
fly deploy
```

### 扩容/缩容
```bash
# 查看当前规格
fly scale show

# 升级到 2GB 内存（一键更新时需要更多内存）
fly scale memory 2048
```

## 六、成本估算

| 项目 | 规格 | 月费（约） |
|---|---|---|
| 应用实例 | 1 核 1GB 内存 | ~$5-10 |
| Volume | 1GB | ~$0.15 |
| 流量 | 100GB/月内 | 免费 |
| **合计** | | **约 $5-12/月** |

> 一键更新时 CPU/内存占用较高，建议至少 1GB 内存。如果采集时 OOM，执行 `fly scale memory 2048`。

## 七、已修改的文件清单

| 文件 | 修改内容 |
|---|---|
| `main.py` | 移除硬编码 `REQUIRE_READ_KEY=1` 和 `API_KEYS`，改为公开访问 |
| `run_server.py` | 移除硬编码鉴权，默认端口改为 8080，读取 `PORT` 环境变量 |
| `config.py` | `api_keys` 改为从环境变量 `API_KEYS` 读取；默认端口 8080 |
| `modules/common/core_data_importer.py` | 修复 `MAIN_DB` 路径，支持 `DB_PATH` 环境变量 |
| `Dockerfile` | 统一端口 8080，用 `run_server.py` 启动，设置 `DB_PATH=/data/fund.db` |
| `.dockerignore` | 排除 `fund.db`（运行时数据），保留 `fund_core_data.db`（种子） |
| `fly.toml` | **新增**，Fly.io 部署配置（端口、Volume、健康检查、环境变量） |
| `.gitattributes` | **新增**，指定 `fund_core_data.db` 用 Git LFS |
| `.github/workflows/deploy.yml` | **新增**，GitHub Actions 自动部署 |

## 八、常见问题

### Q: 部署后网站 502 / 503？
A: 首次启动需要导入种子数据（1-2 分钟），等健康检查通过即可。用 `fly logs` 查看进度。

### Q: 一键更新卡住或失败？
A: 可能是内存不足。执行 `fly scale memory 2048` 升级到 2GB 后重试。

### Q: 想关闭公开访问，加 API Key？
A: 在 fly.toml 的 `[env]` 中设置 `REQUIRE_READ_KEY = "1"` 和 `API_KEYS = "你的密钥"`，然后 `fly deploy`。

### Q: 数据会丢吗？
A: 不会。`fund.db` 存在 Fly.io 的持久化 Volume 上，容器重启/重新部署都不会丢。镜像里的 `fund_core_data.db` 只是首次导入用的种子。

### Q: 可以不用 Git LFS 吗？
A: 可以。如果不想用 LFS，把 `fund_core_data.db` 加入 `.gitignore`，部署后完全靠「一键更新」从零采集（首次采集约 10-15 分钟）。但建议保留种子库，部署更快更稳定。

### Q: 国内访问速度怎么样？
A: Fly.io 香港节点（hkg）国内访问延迟约 50-100ms，尚可。如果需要更快，可以考虑国内云服务商。
