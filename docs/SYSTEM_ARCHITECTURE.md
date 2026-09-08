# 投研看板系统架构文档（v2.9.22）

> 本文档是变更准入网关的"架构认知"基础。所有变更执行前必须阅读本文档，确认变更涉及的模块和文件。

## 一、整体架构

```
┌─────────────────────────────────────────────────────────┐
│                    前端 (Vue 3 SPA)                      │
│  Vue 3 + Vite + TypeScript + Pinia + Naive UI + ECharts  │
│  构建: npm run build → dist/ → backend/static/           │
└────────────────────────┬────────────────────────────────┘
                         │ HTTP REST API (JSON)
                         ▼
┌─────────────────────────────────────────────────────────┐
│                  后端 (FastAPI)                          │
│  Python 3 + FastAPI + Uvicorn                            │
│  启动: python main.py 8002 (默认8000，必须指定8002)       │
├─────────────────────────────────────────────────────────┤
│  API层 (modules/*/router.py)                             │
│  业务层 (modules/*/*.py, services/*.py)                  │
│  数据层 (modules/common/db.py, data/*.db)                │
│  分析层 (analysis_pipeline/*.py)                         │
└────────────────────────┬────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────┐
│                  数据存储层                               │
│  SQLite: backend/data/invest.db (主库)                   │
│  JSON: backend/docs/function_graph.json (功能神经网络)   │
│  JSON: backend/docs/change_history.json (变更记录)       │
│  缓存: backend/data/cache/ (净值缓存)                    │
└─────────────────────────────────────────────────────────┘
```

## 二、前端架构

### 2.1 技术栈
- **框架**: Vue 3 (Composition API) + TypeScript
- **构建**: Vite
- **状态管理**: Pinia (`src/stores/`)
- **路由**: Vue Router (`src/router/`)
- **UI组件**: Naive UI
- **图表**: ECharts + lightweight-charts
- **HTTP**: Axios (`src/utils/api.ts`)
- **版本管理**: `src/utils/version.ts`

### 2.2 页面路由
| 路由 | 页面文件 | 功能 |
|------|---------|------|
| `#/fund` | FundView.vue | 基金榜单/首页 |
| `#/watch` | WatchView.vue | 自选基金 |
| `#/portfolio` | PortfolioView.vue | 持仓管理 |
| `#/compare` | CompareView.vue | 基金对比 |
| `#/market` | MarketView.vue | 市场行情 |
| `#/mine` | MineView.vue | 个人中心/一键更新 |
| `#/function-graph` | FunctionGraphView.vue | 功能神经网络 |
| `#/log` | LogView.vue | 日志查看 |

### 2.3 核心组件
| 组件 | 路径 | 用途 |
|------|------|------|
| FundCard | src/components/FundCard.vue | 基金卡片（自选/榜单共用） |
| FundDetailModal | src/components/FundDetailModal.vue | 基金详情弹窗 |
| CompareTable | src/components/CompareTable.vue | 对比表格 |
| ProfitAlertModal | src/components/ProfitAlertModal.vue | 止盈提醒弹窗 |
| SwipeAction | src/components/SwipeAction.vue | 左滑/右滑操作 |

### 2.4 状态管理 (Pinia Stores)
| Store | 路径 | 管理数据 |
|-------|------|---------|
| portfolio | src/stores/portfolio.ts | 持仓、买入卖出记录 |
| watch | src/stores/watch.ts | 自选基金列表 |
| compare | src/stores/compare.ts | 对比基金选择 |
| settings | src/stores/settings.ts | 用户设置 |

### 2.5 构建部署流程
```bash
# 1. 清理旧构建
Remove-Item backend\static\assets -Recurse -Force

# 2. 构建前端
npm run build

# 3. 复制到后端静态目录
Copy-Item dist\* backend\static -Recurse -Force

# 4. 重启后端
Get-Process python | Stop-Process -Force
cd backend && python main.py 8002
```

## 三、后端架构

### 3.1 技术栈
- **框架**: FastAPI
- **服务器**: Uvicorn
- **数据库**: SQLite (通过 `modules/common/db.py` 封装)
- **HTTP客户端**: requests + httpx

### 3.2 模块划分
```
backend/
├── main.py                    # FastAPI入口，组装app、注册路由、挂载静态文件
├── config.py                  # 配置管理（版本号、数据源、API密钥）
├── modules/
│   ├── common/                # 通用模块
│   │   ├── db.py              # 数据库连接和操作
│   │   ├── pipeline.py        # 一键更新主流程
│   │   ├── data_quality.py    # 数据质量校验（v2.9.19新增）
│   │   └── multi_source_fetcher.py  # 多数据源抓取
│   ├── fund/                  # 基金相关
│   │   ├── holdings.py        # 持仓管理
│   │   └── nav/               # 净值抓取
│   │       └── fetcher.py     # 净值抓取器
│   ├── rank/                  # 榜单计算
│   │   └── rank_full.py       # 全量榜单计算
│   ├── analysis/              # 分析模块
│   ├── system/                # 系统管理
│   │   └── router.py          # 功能神经网络+变更网关API（核心）
│   └── nav/                   # 净值管理
├── analysis_pipeline/         # 分析流水线
│   ├── night_fund_monitor.py  # 自选实时获取
│   └── ...
├── services/                  # 业务服务层
│   ├── compare.py             # 对比计算
│   ├── nav_series.py          # 净值序列
│   └── ...
├── data/                      # 数据存储
│   ├── invest.db              # 主数据库
│   └── cache/                 # 缓存目录
├── docs/                      # 文档和配置
│   ├── function_graph.json    # 功能神经网络数据
│   ├── change_history.json    # 变更记录
│   └── SYSTEM_ARCHITECTURE.md # 本文档
└── static/                    # 前端构建产物（由npm build生成）
```

### 3.3 核心API列表
| API | 方法 | 功能 |
|-----|------|------|
| `/api/fund/list` | GET | 基金榜单列表 |
| `/api/fund/detail` | GET | 基金详情 |
| `/api/watch/list` | GET | 自选列表 |
| `/api/portfolio/list` | GET | 持仓列表 |
| `/api/compare` | GET | 基金对比数据 |
| `/api/pipeline/run` | POST | 一键更新 |
| `/api/pipeline/status` | GET | 更新进度 |
| `/api/function-graph` | GET | 功能神经网络 |
| `/api/function-graph/change/gateway` | POST | 变更网关创建 |
| `/api/function-graph/change/gateway/{id}/start` | POST | 开始变更 |
| `/api/function-graph/change/gateway/{id}/complete` | POST | 完成验证门 |
| `/api/function-graph/change/active` | GET | 进行中变更列表 |
| `/api/data-quality/status` | GET | 数据质量状态 |

### 3.4 一键更新流程 (pipeline.py)
```
1. 日期判断 → 确认是交易日
2. 候选池构建 → 从全量基金库筛选（剔除A类、长期持有型等）
3. 净值抓取 → 同花顺API主源 + 天天基金爬虫备源
4. 数据质量校验 → 重复检测、异常检测、新鲜度检测
5. 指标计算 → 涨跌幅、回撤、夏普、卡玛等
6. 榜单计算 → 各榜单排名
7. 数据存储 → 写入SQLite
8. 缓存更新 → 更新前端缓存
```

## 四、数据库结构

### 4.1 核心表
| 表名 | 用途 | 关键字段 |
|------|------|---------|
| funds | 基金基础信息 | code, name, nav, nav_date, d1, data_status |
| fund_nav | 净值历史 | code, date, nav |
| fund_indicators | 基金指标 | code, sharpe, sortino, calmar, max_drawdown |
| holdings | 持仓记录 | id, fund_code, buy_date, buy_price, shares, status |
| watch_list | 自选列表 | fund_code, added_at |
| rankings | 榜单数据 | fund_code, rank_type, rank, score |

### 4.2 数据来源
- **主源**: 同花顺API（v2.9.22切换，需API Key）
- **备源**: 天天基金网爬虫
- **降级**: 多源交叉验证，主源失败时自动切换备源

## 五、功能神经网络

### 5.1 数据结构
- 文件: `backend/docs/function_graph.json`
- 节点: 28个（system层、data层、analysis层、display层）
- 边: 93条（节点间依赖关系）
- 版本: v2.9.22

### 5.2 核心节点
| 节点ID | 名称 | 层级 |
|--------|------|------|
| change_gateway | 变更准入网关 | system |
| ui_design | UI设计规范 | display |
| data_quality | 数据质量校验 | data |
| pipeline | 一键更新 | system |
| rank_calculation | 榜单计算 | analysis |
| fund_card | 基金卡片 | display |
| ... | ... | ... |

## 六、变更执行规范

### 6.1 编码安全（强制）
- **禁止**使用PowerShell的 `Set-Content` 修改包含中文的文件
- **必须**使用Python脚本（`open(..., encoding='utf-8')`）修改中文文件
- 修改后必须验证文件编码和语法

### 6.2 版本号更新（强制）
所有变更必须同步更新三个文件的版本号：
1. `backend/config.py` → `APP_VERSION`
2. `src/utils/version.ts` → `APP_VERSION`
3. `_pack.py` → `VERSION`

### 6.3 构建验证（强制）
- 前端必须 `npm run build` 成功
- 构建产物必须复制到 `backend/static/`
- 后端必须重启并验证API可访问

### 6.4 变更流程（强制）
```
1. 通过网关创建变更记录 → 获得change_id
2. 开始变更 → pending → in_progress
3. 按任务清单逐项执行
4. 标记任务完成
5. 通过验证门 → 七类必做项齐全
6. 神经网络自动同步
7. 打包部署包
```

## 七、常见陷阱

1. **版本号不一致**: config.py、version.ts、_pack.py 三个地方必须同步
2. **旧API绕过**: change/create和change/update已废弃，必须使用网关API
3. **构建缓存**: 每次构建前必须清理 `backend/static/assets`
4. **端口号**: 后端必须用8002端口（`python main.py 8002`），不是默认的8000
5. **数据质量**: d1=0不一定是非交易日，可能是数据源重复，必须检查data_status
6. **编码损坏**: PowerShell修改中文文件会导致UTF-8编码损坏，必须用Python
