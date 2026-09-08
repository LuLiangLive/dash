# 基金投研看板 — 数据字典与字段血缘

> 版本: v0.93.0 | 更新: 2026-08-29（上一版 v0.92.0 / 2026-08-29）
> 用途: 快速定位每个显示字段的数据来源、计算函数、存储位置和更新频率，解决"多处显示不一致"问题。
>
> **v0.93.0 主要变更（僵尸字段治理 + 抓取瘦身）**
> 1. **净值序列惰性解析** —— `fetch_pingzhong(need=...)` 按需解析，`Data_netWorthTrend`
>    只在详情页要长序列时才解析。档案字段缓存 **565 KB/只 → 0.14 KB/只**，
>    满缓存内存 **331 MB → 36 MB（-89%）**
> 2. **净值独立缓存** `_NAV_CACHE`（上限 60）与主缓存 `_PZ_CACHE`（上限 600）分离，
>    主缓存永不存放 navs
> 3. **`_js_value` 去掉 400KB 字符串切片** —— 改用 `raw_decode(txt, idx)` 原地定位
> 4. **下线 9 个僵尸 meta 键**：`up7 / max_dn7 / dn_ratio / is_etf / rtag / status /
>    sections / periods / reco_note`（全部"只写不读"，`rtag`/`status` 恒为 None）
>    榜单 meta 字段数 **66 → 54**，存储 -9.6%
> 5. **修复 f10 解析出的僵尸字段**：`company/benchmark/mgr_fee/fullname/shares/
>    manager_info/similar_rank/scale_date` 抓了但从不落库也从不展示，已不再解析
> 6. 新增 `scripts/audit_fields.py` 字段僵尸审计（区分消费方/生产方，可当回归门禁）
>
> **v0.92.0 主要变更**
> 1. 字段字典 `FIELD_MAP` 由 19 项补全到 **47 项**（前端实际使用约 45 个字段）
> 2. 修复 `m3/m6/y1` 覆盖率 0% 的问题 —— 改从 pingzhongdata `syl_*` 直取，不再从残缺净值序列推算
> 3. 修复 `scale/est/manager/track` 长期为空 —— 新增 f10 概况页解析
> 4. 修复 `is_etf` 全表为 0 的 bug
> 5. 净值补拉由 14 页分页改为 **pingzhongdata 单次请求**（1.04~3.26s → 0.28s）
> 6. 新增 `scripts/check_field_alignment.py` 字段对齐自检工具

---

## 一、核心原则

1. **单一数据源**: 每个字段只有一个权威计算函数，其他地方引用而非重算
2. **全量 vs 按需**: 核心指标一键更新全量；持仓/重仓股等大数据按需拉取
3. **可追溯**: 修改任何字段前，先查此字典确认影响范围

---

## 二、字段血缘表

### 2.1 基础信息

| 字段 | 显示位置 | 数据来源 | 计算函数 | 存储 | 更新频率 |
|------|----------|----------|----------|------|----------|
| code | 所有页面 | 基金代码 | — | funds.code | 不变 |
| name | 所有页面 | 天天基金网 | fetcher.fetch_pool | funds.name |  rarely |
| ftype | 卡片/展开 | 名称推断 | sector.infer_ftype | funds.ftype |  rarely |
| sec | 卡片/榜单 | 名称推断 | sector.infer_sector | funds.sec |  rarely |
| scale | 展开详情 | 天天基金网 | fetcher.fetch_basic_many | funds.scale | 按需/全量 |
| est | 展开详情 | 天天基金网 | fetcher.fetch_basic_many | funds.est | 按需/全量 |
| is_etf | 内部筛选 | 名称判断 | fetcher._build_pool | funds.is_etf |  rarely |

### 2.2 净值与涨跌

| 字段 | 显示位置 | 数据来源 | 计算函数 | 存储 | 更新频率 |
|------|----------|----------|----------|------|----------|
| nav | 卡片/展开 | 天天基金网 | fetcher.fetch_nav_many | funds.nav | 一键更新 |
| nav_date | 卡片/展开 | 净值日期 | fetcher.target_nav_date | funds.nav_date | 一键更新 |
| d3/d5/d7/d10 | 卡片/榜单 | 净值序列 | ranker._ret_skipna | funds.d3..d10 | 一键更新 |
| m1/m3/m6/y1 | 卡片/对比 | **pingzhongdata syl_*** | fetcher.fetch_pingzhong | funds.m1..y1 | 一键更新 |

> **v0.92.0 重要修复（m3/m6/y1）**
> 原口径是从 `nav_history` 净值序列用 `ranker._ret(navs, 63/126/252)` 推算，
> 但实测 nav_history **5810 只仅 63~125 条、126~251 条为 0 只**，
> 而 `_ret` 分别需要 64/127/253 个净值点 → **m3/m6/y1 恒为 None**，榜单 meta 覆盖率 0%。
> 现改为直接取 pingzhongdata 的 `syl_1y/syl_3y/syl_6y/syl_1n`（单次请求 0.04s），
> 覆盖率升至 **m3 94% / m6 92% / y1 86%**。
| dd7 | 卡片 | 近7日净值 | ranker.calc_metrics | funds.dd7 | 一键更新 |
| dn7 | 卡片 | 近7日涨跌 | ranker.calc_metrics | funds.dn7 | 一键更新 |
| streak | 卡片 | 净值序列 | ranker.streak_days | funds.streak | 一键更新 |

### 2.3 风险指标

| 字段 | 显示位置 | 数据来源 | 计算函数 | 存储 | 更新频率 |
|------|----------|----------|----------|------|----------|
| max_dd | 卡片/对比/弹窗 | 净值序列 | ranker._max_dd | funds.max_dd | 一键更新 |
| mdd_days | 展开/弹窗 | 净值序列 | ranker._max_dd | funds.mdd_days | 一键更新 |
| mdd_status | 展开/弹窗 | 净值序列 | ranker._max_dd | funds.mdd_status | 一键更新 |
| vol | 对比/弹窗 | 日收益率 | ranker._pstdev*sqrt(252) | funds.vol | 一键更新 |
| down_vol | 对比/弹窗 | 下跌日收益 | ranker.calc_metrics | funds.down_vol | 一键更新 |
| down_sharpe | 对比/弹窗 | 下跌日收益 | ranker.calc_metrics | funds.down_sharpe | 一键更新 |
| calmar | 对比/弹窗 | 收益/回撤 | ranker.calc_metrics | funds.calmar | 一键更新 |
| pl | 展开/弹窗 | 涨跌比 | ranker.calc_metrics | funds.pl | 一键更新 |
| hi_cnt | 展开 | 净值序列 | ranker.calc_metrics | funds.hi_cnt | 一键更新 |
| dd_from_hi | 卡片 | 20日高点 | ranker.calc_metrics | funds.dd_from_hi | 一键更新 |

### 2.4 评分与信号

| 字段 | 显示位置 | 数据来源 | 计算函数 | 存储 | 更新频率 |
|------|----------|----------|----------|------|----------|
| score | 卡片/榜单 | 收益50%+抗跌50% | pipeline: score=earn*0.5+ad*0.5 | funds.score | 一键更新 |
| ad_score | 卡片/榜单 | 抗跌6项百分位 | ranker.compute_ad_scores_v2 | funds.ad_score | 一键更新 |
| earn_score | 卡片/榜单 | 收益类基准锚定 | ranker.compute_earn_scores | funds.earn_score | 一键更新 |
| ms | 卡片/榜单 | 动能状态 | ranker.momentum_status | funds.ms | 一键更新 |
| yindie | 卡片标签 | 阴跌识别 | ranker.compute_scores_v2 | funds.yindie | 一键更新 |
| verdict | 展开 | 综合判断 | ranker.verdict_of | funds.verdict | 一键更新 |
| suggest | 展开 | 建议详情 | ranker.verdict_of | funds.suggest | 一键更新 |

### 2.5 买入信号（reco）

| 字段 | 显示位置 | 数据来源 | 计算函数 | 存储 | 更新频率 |
|------|----------|----------|----------|------|----------|
| reco | 卡片/榜单/弹窗 | 六维推荐 | ranker.compute_reco_v32 | funds.reco | 一键更新(v0.53.0修复) |
| reco_score | 内部/开仓榜 | 六维打分 | ranker.compute_reco_v32 | funds.reco_score | 一键更新 |
| reco_days | 卡片/弹窗 | 信号持续天数 | recompute_ad 回放 | funds.reco_days | recompute_ad |
| prev_reco | 卡片/弹窗 | 上一信号 | recompute_ad 回放 | funds.prev_reco | recompute_ad |
| prev_reco_days | 卡片/弹窗 | 上一持续天数 | recompute_ad 回放 | funds.prev_reco_days | recompute_ad |

> **注意**: reco_days/prev_reco 需要精确回放历史，由 recompute_ad.py 计算；pipeline 只更新 reco/reco_score。

### 2.6 持仓数据（按需拉取）

| 字段 | 显示位置 | 数据来源 | 计算函数 | 存储 | 更新频率 |
|------|----------|----------|----------|------|----------|
| themes | 卡片🧭标签 | 前十大持仓聚合 | fetcher.holdings_themes | funds.themes | 按需拉取+上榜抓取 |
| stocks | 展开详情 | 前十大持仓 | fetcher.fetch_holdings_w | funds.stocks | 按需拉取+上榜抓取 |

> **策略**: 一键更新不抓全量持仓（6000只太慢）；用户展开卡片时按需拉取，缓存24小时。

### 2.7 基金档案（v0.92.0 新增）

| 字段 | 显示位置 | 数据来源 | 计算函数 | 存储 | 更新频率 |
|------|----------|----------|----------|------|----------|
| scale | 弹窗/详情 | pingzhongdata `Data_fluctuationScale`（f10 兜底） | fetcher.fetch_profile | funds.scale | 一键更新 |
| est | 弹窗/详情 | f10 jbgk「成立日期/规模」 | fetcher.fetch_f10_profile | funds.est | **仅补缺失**（不变值） |
| manager | 弹窗「基础档案」 | pingzhongdata `Data_currentFundManager`（f10 兜底） | fetcher.fetch_profile | funds.manager | 缺才补 |
| track | 弹窗「跟踪指数」 | f10 jbgk「跟踪标的」 | fetcher.fetch_f10_profile | funds.track | **仅补缺失**（不变值） |
| ftype | 卡片/弹窗 | f10 jbgk「基金类型」 | fetcher.fetch_f10_profile | funds.ftype | 缺才补 |

### 2.8 已下线的僵尸字段（v0.93.0 · 不要再重新引入）

审计口径：**消费方** = `static/` + `rendering/` + `main.py`；**生产方** = `collector/` +
`analysis_pipeline/` + `domain/` + `scripts/`。消费方 0 引用即判定为僵尸。

| 字段 | 原位置 | 判定依据 | 处理 |
|------|--------|----------|------|
| up7 | rank_snapshots.meta | 消费方 0 引用（有值 1884 行） | 停止写入 |
| max_dn7 | rank_snapshots.meta | 消费方 0 引用（`dd7` 是它在使用中的别名） | 停止写入，保留 dd7 |
| dn_ratio | rank_snapshots.meta | 消费方 0 引用（`up_ratio` 在用） | 停止写入 |
| is_etf | rank_snapshots.meta | 消费方 0 引用 | 停止写入；**funds.is_etf 列保留** |
| rtag / status | rank_snapshots.meta | 恒为 None（1884 行全空），纯占位 | 停止写入 |
| sections / periods | rank_snapshots.meta(rot) | 消费方 0 引用 | 停止写入，保留 cells |
| reco_note | rank_snapshots.meta | 消费方 0 引用（92 行有值） | 停止写入 |
| company / benchmark / mgr_fee / fullname / shares | f10 jbgk 解析 | 抓了但**无对应列、前端不展示** | 停止解析 |
| manager_info / similar_rank / scale_date | pingzhongdata 解析 | 抓了但**无对应列、前端不展示** | 改为按需解析 |

> **funds.is_etf 为什么保留？** 前端不展示，但 `rank_full._fill_listed_profiles`
> 用它判断"是否值得为 `track` 打一次 f10 请求"（非 ETF 没有跟踪标的，打了也是白打）。
> 已在 `scripts/audit_fields.py` 的 `KEEP_ALIVE` 白名单中标注。

### 2.9 移动端接口（实验性 · 默认关闭）

`fundmobapi.eastmoney.com/FundMNewApi/FundMNBasicInformation`（2.3KB）+
`FundMNDetailInformation`（1.3KB）可覆盖 `name/m1/m3/m6/y1/ftype/manager/est/scale`，
**流量仅为老链路的 4.5%（216.6 KB/只 → 9.7 KB/只）**，字段一致性实测 8/8 完全吻合。

但**不要在生产环境当主链路**，压测结论：
- 前 ~200 次请求 100% 成功（120/120 抽样）
- 之后开始返回 `ErrCode=61136403「网络繁忙，请稍后重试」`
- 冷却 30s、并发降到 2 仍然 **0/20 全败** —— 东方财富对 fundmobapi 有较硬的频次封禁
- 被封后每只基金要多打 2 次失败请求再回落老源，端到端**反而慢一倍**（5.7s vs 2.6s / 40 只）

因此：`MOBILE_ENABLED` 默认 `False`（可用环境变量 `FUND_MOBILE_API=1` 开启），
并内置熔断器 `_mob_trip()`（连续 6 次失败熔断 30 分钟），把被封时的损失限制在前几只基金。

> 字段映射备忘：`SYL_Y→m1`、`SYL_3Y→m3`、`SYL_6Y→m6`、`SYL_1N→y1`、
> `SHORTNAME→name`、`FTYPE→ftype`、`JJJL→manager`（多经理取首位）、`ESTABDATE→est`、
> **`ENDNAV→scale`（÷1e8）**。注意是 `ENDNAV`「期末净资产」而非 `NETNAV`「净资产」，
> 后者口径不同，实测 0/10 与 `Data_fluctuationScale` 对不上。

> **两个数据源的分工**（一次请求 0.04~0.20s）
> - **pingzhongdata**（`fund.eastmoney.com/pingzhongdata/{code}.js`，约 106KB）：
>   `syl_1y/3y/6y/1n` 区间收益、`Data_netWorthTrend` 全量净值（自成立日起，实测 606~5995 条）、
>   `Data_currentFundManager` 基金经理（姓名/任职天数/管理规模/星级/能力分）、
>   `Data_fluctuationScale` 季度规模序列
> - **f10 jbgk**（`fundf10.eastmoney.com/jbgk_{code}.html`）：成立日、跟踪标的、基金类型、
>   基金管理人、业绩比较基准、管理费率、份额规模
>
> `fetcher.fetch_profile(code, need=...)` 按缺失字段决定打哪个源：
> 只要区间收益/经理/规模 → 只打 pingzhongdata（20ms/只）；需要成立日/跟踪标的才追加 f10。

---

## 三·补 字段覆盖率自检

```bash
python3.11 scripts/check_field_alignment.py           # 全量报告
python3.11 scripts/check_field_alignment.py --top 20  # 只看问题最大的 20 项
python3.11 scripts/check_field_alignment.py --json    # 机器可读
```

输出每个展示字段在 `funds` 表 / `rank_snapshots.meta` 的覆盖率，并标注是否已在
`FIELD_MAP` 中定义。以下字段**按设计就是部分覆盖**，不算缺陷：

| 字段 | 预期覆盖率 | 原因 |
|------|-----------|------|
| rtag | 0% | 运行时注入（main.py:103 / db.py:491），不落 meta |
| track | ~30% | 仅指数/ETF 类基金有跟踪标的，主动型本就为空 |
| cros | ~37% | 同榜提醒，只有同时登上多个日榜的基金才有值 |
| tscore | ~67% | 仅稳涨/强趋势面板计算 |
| yindie | ~32% | 阴跌识别标签，只有符合阴跌特征的基金才打标 |

---

## 三、榜单算法血缘

### 3.1 唯一权威入口

**`rank_full.compute_all_panels()`** — 所有榜单的唯一计算入口

输出 10 个 sub 面板:
- **日排(day)×4**: 日涨幅榜 / 3日涨幅榜 / 5日涨幅榜 / 10日涨幅榜
- **排名(rank)×3**: 稳涨榜单 / 强趋势榜单 / ETF榜单
- **推荐(reco)×3**: 自选算法榜单 / 抗跌榜单 / 开仓榜单

### 3.2 已废弃（待删除）

- `ranker.build_ranks()` — 旧版榜单，pipeline 内调用后被 rank_full 覆盖
- 计划: v0.54.0 删除此函数，pipeline 不再调用

### 3.3 各面板评分函数

| 面板 | 评分函数 | 数据依赖 |
|------|----------|----------|
| 日涨幅榜 | 按 d1/d3/d5/d10 降序 | d1/d3/d5/d10 |
| 稳涨榜单 | ranker.panel_score("稳涨") | 趋势40+支撑30+稳健20+平衡10 |
| 强趋势榜单 | ranker.panel_score("强趋势") | 同稳涨，阈值不同 |
| ETF榜单 | ranker.panel_score("ETF") | 同稳涨，仅ETF |
| 自选算法榜单 | ranker.watch_score_light | 中长期35+近期25+回撤15+波动16+夏普9 |
| 抗跌榜单 | ad_score 降序 | compute_ad_scores_v2 |
| 开仓榜单 | reco_score 降序 | compute_reco_v32 |

---

## 四、API 端点与数据来源

| 端点 | 方法 | 返回数据 | 数据源 |
|------|------|----------|--------|
| / | GET | 首页HTML(含WATCH_DATA) | funds表 + rank_snapshots |
| /api/ranks | GET | 榜单数据 | rank_snapshots表 |
| /api/funds/{code} | GET | 基金详情 | funds表 |
| /api/funds/{code}/nav | GET | 净值序列 | nav_history表 |
| /api/funds/{code}/holdings | GET | 持仓主题+重仓股 | 实时抓取+缓存(v0.53.0新增) |
| /api/anti/{code} | GET | 抗跌专项详情 | 实时计算 |
| /api/watch/fetch | GET | 添加自选时实时获取 | night_fund_monitor |
| /api/watch/compare | POST | 对比页数据 | funds表 + 实时计算 |
| /api/market/indices | GET | 市场指数 | 实时抓取 |
| /api/market/news | GET | 资讯 | market_news表 |
| /api/fetch/start | POST | 触发一键更新 | pipeline + rank_full |
| /api/fetch/status | GET | 更新进度 | fetch_manager |

---

## 五、数据更新流程

```
一键更新(/api/fetch/start)  —— collector/fetch_manager.py 5 阶段状态机
  ├─ 阶段0: 日期判断      effective_nav_date() + cached_nav_max() 逐只校验缓存
  ├─ 阶段1: 抓净值        fetch_nav_incremental(并发) → nav_history
  ├─ 阶段2: 重排榜单      rank_full.compute_all_panels() ← 唯一权威入口
  │                        ├─ 算 metrics (6001 只, 约 1s)
  │                        ├─ 算 10 个 sub 面板
  │                        └─ _fill_listed_profiles() ← v0.92.0 新增
  │                             在写库前补齐上榜基金的 m1/m3/m6/y1/scale/est/manager/track
  ├─ 阶段2.5: 上榜数据     _fetch_top_holdings + _fetch_top_returns
  ├─ 阶段3: 算推荐信号     recompute_ad / compute_reco_v32
  └─ 阶段4: 同步榜单

按需拉取(用户展开卡片时)
  └─ /api/funds/{code}/holdings
      ├─ 查funds.themes/stocks (有则直接返回)
      ├─ 无则实时抓取 (fetcher.fetch_holdings_w)
      ├─ 聚合主题 (fetcher.holdings_themes)
      └─ 写入funds表缓存24小时
```

> **v0.92.0 关键修复：档案补齐的时点**
> 原流程中 `m3/m6/y1` 由「阶段2.5」的 `_fetch_top_returns` 写入 funds 表，
> 但榜单 meta 在「阶段2」的 `_build_item` 就已经构建完成并落库了 ——
> **数据抓到了，却写不进已经定稿的 meta**，所以 meta 里这三个字段常年 0%。
> 现在改为在 `compute_all_panels` 内部、**`_save_to_db` 之前**调用
> `_fill_listed_profiles()`，就地补齐后回写 panels item，数据才能回流。
>
> **时变 vs 静态字段分开处理**（这是提速的关键）
> - 时变（每交易日刷新）：`m1/m3/m6/y1/scale` —— 全部来自 pingzhongdata，只打 1 次请求
> - 静态（缺才补，入库后永久跳过）：`est/track/manager` —— 成立日/跟踪标的不可能变
>
> 实测：首轮全量补齐 101ms/只，第二轮增量仅 **6ms/只**。

> **并发数不是越大越好**
> 东方财富对高频并发有限流，实测：并发 4 = 20~51ms/只，并发 10 反而退化到 220ms/只
> （响应变慢还会触发 `_get` 重试 sleep）。因此 `_fill_listed_profiles` 固定用并发 4，
> 且 `fetch_profile` 内部**刻意不嵌套线程池** —— 外层已并发，内层再开会翻倍触发限流。

> **⚠️ 档案字段必须用 COALESCE 写入（v0.92.0 血泪教训）**
> 净值/指标链路（pipeline / recompute_ad）与档案链路（f10 / pingzhongdata）是
> **两套数据源**。前者的 dict 里没有 `scale/est/manager`，`f.get(k)` 为 None，
> 若用 `ON CONFLICT DO UPDATE SET k=excluded.k` 无条件覆盖，一键更新就会把
> 已补齐的档案清空 —— 实测 est/manager 从 100% 掉回 4.3%、m1 从 99.8% 掉到 3.7%。
>
> 已改为 COALESCE 的位置：
> - `db.upsert_fund`：`_PRESERVE_ON_UPDATE = {scale, est, manager, track, m1, m3, m6, y1}`
> - `scripts/recompute_ad.py`：`m1=COALESCE(?, m1)`, `nav=COALESCE(?, nav)`
>
> **今后新增写库逻辑时，凡涉及这两类字段，务必沿用 COALESCE。**

---

## 六、修改检查清单

修改任何字段前，确认:
1. [ ] 此字段在字典中的权威计算函数是哪个？
2. [ ] 此字段在哪些页面显示？
3. [ ] 修改是否影响榜单排序？
4. [ ] 是否需要同步更新 recompute_ad.py？
5. [ ] 单元测试是否覆盖？
