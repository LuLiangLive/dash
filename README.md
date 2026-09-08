---
title: 投研看板
emoji: 📊
colorFrom: blue
colorTo: green
sdk: docker
app_port: 7860
pinned: false
---

# 投研看板 (Invest Dashboard)

基金投研看板系统，基于 FastAPI + SQLite，支持基金榜单、自选管理、一键更新等功能。

## 功能

- 基金榜单（日榜/推荐榜/预警榜）
- 自选基金管理与分组
- 双基金对比
- 持仓盈亏管理
- 市场行情与资讯
- 板块轮动分析
- 一键更新（自动采集最新数据）

## 首次使用

1. 等待应用启动（首次需从种子库导入数据，约 1-2 分钟）
2. 点击页面上的「一键更新」，等待 5-7 分钟完成全量采集
3. 开始使用

## 注意

- 数据存储在容器内，**重新部署会重置**，请定期使用导出功能备份自选/持仓数据
- 公开访问模式，无需 API Key

## 本地运行

```bash
pip install -r requirements.txt
python run_server.py
```

访问：http://127.0.0.1:7860/
