# 投研看板 (Invest Dashboard)

基金投研看板系统，基于 FastAPI + SQLite，支持基金榜单、自选管理、一键更新等功能。

## 快速启动

### Windows
双击 `start.bat` 或命令行执行：
```bash
python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

### 通用
```bash
pip install -r requirements.txt
python -m uvicorn main:app --host 0.0.0.0 --port 8000
```

访问：http://127.0.0.1:8000/

## 项目结构

```
├── main.py              # FastAPI 主入口（唯一入口）
├── db.py                # 数据库操作
├── auth.py              # 认证
├── auto_update.py       # 自动更新
├── market.py            # 市场数据
├── market_research.py   # 市场研究
├── main_handler.py      # 主处理器
├── collector/           # 数据采集
│   ├── ranker.py        # 排名算法
│   ├── rank_full.py     # 完整榜单
│   ├── pipeline.py      # 采集流水线
│   ├── fetcher.py       # 数据抓取
│   ├── fetch_manager.py # 抓取管理
│   ├── sector.py        # 行业分类
│   ├── schedule.py      # 定时任务
│   └── run.py           # 采集运行
├── rendering/           # 渲染
│   ├── fund_page.py     # 基金页面渲染
│   └── md_builder.py    # Markdown 构建
├── analysis_pipeline/   # 分析流水线
├── scripts/             # 脚本
├── static/              # 静态资源
│   ├── index.html       # 前端页面
│   ├── css/             # 样式
│   └── js/              # JavaScript
├── data/                # 数据目录
│   ├── fund.db          # SQLite 数据库
│   └── *.json           # 配置和缓存
├── tests/               # 单元测试
├── requirements.txt     # 依赖
├── Dockerfile           # Docker 配置
└── start.bat            # Windows 启动脚本
```

## 版本记录

- v0.51.3: 榜单渲染与数据修复
  - 抗跌榜单综合分计算修复
  - 基金卡片标签整理（删除重仓股/verdict，添加连涨天数）
  - 重仓行业兜底推断
  - 日排同类型ETF去重
  - 净值数据顺序修复
  - 调度状态持久化
  - 日期判断修复
