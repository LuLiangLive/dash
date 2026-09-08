# 抗跌榜旧算法删除影响范围分析报告 (CHG-050)

> 生成时间: 2026-09-07
> 项目版本: v2.9.56
> 分析范围: backend/ + src/ 全量
> 神经网络: function_graph.json (56节点 / 143边)

---

## 一、执行摘要

| 旧函数 | 定义位置 | 运行时调用方数 | 删除风险等级 | 可否直接删除 |
|---|---|---|---|---|
| `nfm.anti_score(ma)` | night_fund_monitor.py:249 | 3处外部 + 1处内部 | **高** | 否，需先迁移 |
| `nfm.earn_score(xa, ma)` | night_fund_monitor.py:271 | 3处外部 + 1处内部 | **高** | 否，需先迁移 |
| `nfm.dual_score(xa, ma)` | night_fund_monitor.py:290 | 3处外部 | **高** | 否，需先迁移 |
| `scoring.compute_ad_scores_v2(pool)` | scoring.py:320 | 1处内部(compute_scores_v2) | **中** | 否，compute_scores_v2依赖它 |
| `scoring.pool_ad_scores` | scoring.py:69 | **0处** | **低** | 是，删除import后可安全删除 |

**核心结论**:
1. **`pool_ad_scores` 是纯死代码**，全项目无任何运行时调用，删除import后可直接移除。
2. **`compute_ad_scores_v2` 被 `compute_scores_v2` 内部调用**（scoring.py:493），而 `compute_scores_v2` 是一键更新阶段4的主力评分入口，不能直接删。
3. **`nfm.anti_score/earn_score/dual_score` 有3个活跃调用方**（anti_detail、rank_full、score_service），必须先将这些调用方迁移到V3，再删除函数。
4. 前端**不直接调用**任何旧评分API，仅消费API响应中的 `ad_score/earn_score/dual` 字段，删除旧算法后只要V3仍产出同名字段，前端零改动。
5. `dual_ad_weight/dual_earn_weight` 配置项被 `nfm.dual_score` 和 `rank_full._anti_score_simple` 引用，删除旧算法后需评估是否保留配置项。

---

## 二、调用点清单

### 2.1 `night_fund_monitor.anti_score(ma)` — 线性抗跌分

**定义**: `backend/analysis_pipeline/night_fund_monitor.py:249`

| # | 文件:行号 | 调用方式 | 上下文摘要 |
|---|---|---|---|
| 1 | night_fund_monitor.py:295 | `a = anti_score(ma)` | `dual_score()` 内部调用，计算综合分的抗跌分量 |
| 2 | anti_detail.py:250 | `ad = nfm.anti_score(_ma)` | 抗跌详情页分数历史序列计算，逐净值点调用 |
| 3 | rank_full.py:138 | `ad = nfm.anti_score(_ma)` | `_get_unified_score()` 计算单基金统一分数（榜单排名用） |
| 4 | score_service.py:231 | `ad_score = nfm.anti_score(_ma)` | `_compute_single_score()` 降级路径（pool_score缓存未命中时） |

**注释/文档引用**（非运行时）:
- night_fund_monitor.py:12（模块docstring）
- anti_detail.py:212（变更日志注释）

---

### 2.2 `night_fund_monitor.earn_score(xa, ma)` — 线性收益分

**定义**: `backend/analysis_pipeline/night_fund_monitor.py:271`

| # | 文件:行号 | 调用方式 | 上下文摘要 |
|---|---|---|---|
| 1 | night_fund_monitor.py:296 | `e = earn_score(xa, ma)` | `dual_score()` 内部调用，计算综合分的收益分量 |
| 2 | anti_detail.py:251 | `earn = nfm.earn_score(_xa, _ma)` | 抗跌详情页分数历史序列计算 |
| 3 | rank_full.py:139 | `earn = nfm.earn_score(_xa, _ma)` | `_get_unified_score()` 统一分数计算 |
| 4 | score_service.py:232 | `earn_score = nfm.earn_score(_xa, _ma)` | `_compute_single_score()` 降级路径 |

**注意**: `earn_score` 作为字段名（dict key / DB列名）在全项目有大量引用（50+处），但这些是**数据字段**而非函数调用，不受函数删除影响。

---

### 2.3 `night_fund_monitor.dual_score(xa, ma)` — 综合分（抗跌50%+收益50%）

**定义**: `backend/analysis_pipeline/night_fund_monitor.py:290`

| # | 文件:行号 | 调用方式 | 上下文摘要 |
|---|---|---|---|
| 1 | anti_detail.py:252 | `comp = nfm.dual_score(_xa, _ma)` | 抗跌详情页分数历史序列，计算综合分曲线 |
| 2 | rank_full.py:140 | `dual = nfm.dual_score(_xa, _ma)` | `_get_unified_score()` 统一分数，结果存入 `_UNIFIED_SCORE_CACHE` |
| 3 | score_service.py:233 | `dual = nfm.dual_score(_xa, _ma)` | `_compute_single_score()` 降级路径 |

**内部依赖**: `dual_score` 内部调用 `anti_score` + `earn_score`，并读取 `algo_config.dual_ad_weight / dual_earn_weight`。

---

### 2.4 `scoring.compute_ad_scores_v2(pool)` — 百分位版抗跌分

**定义**: `backend/modules/score/scoring.py:320`

| # | 文件:行号 | 调用方式 | 上下文摘要 |
|---|---|---|---|
| 1 | scoring.py:493 | `ad_map = compute_ad_scores_v2(pool, yindie_codes)` | `compute_scores_v2()` 内部调用，是一键更新阶段4抗跌分的唯一计算路径 |

**Import但未调用**（删除函数后这些import会报ImportError）:
| 文件:行号 | 说明 |
|---|---|
| collector/scoring.py:10 | 兼容层re-export，`from modules.score.scoring import (... compute_ad_scores_v2 ...)` |
| ranker.py:32 | `from collector.scoring import (... compute_ad_scores_v2 ...)`，导入后从未调用 |

---

### 2.5 `scoring.pool_ad_scores` — 百分位版批量入口

**定义**: `backend/modules/score/scoring.py:69`

| # | 文件:行号 | 调用方式 | 上下文摘要 |
|---|---|---|---|
| — | **无任何运行时调用** | — | 全项目grep `pool_ad_scores(` 仅匹配到函数定义本身 |

**Import但未调用**:
| 文件:行号 | 说明 |
|---|---|
| collector/scoring.py:8 | 兼容层re-export |
| ranker.py:30 | 导入后从未调用 |

**结论**: `pool_ad_scores` 是**纯死代码**，可在移除上述两个import后直接删除函数体。

---

### 2.6 scoring.py 中其他以 score/Score 结尾的函数

| 函数名 | 行号 | 运行时调用方 | 是否旧算法 | 备注 |
|---|---|---|---|---|
| `pool_earn_scores` | 39 | recompute_earn.py:37 | 是（旧百分位） | 独立脚本使用，非主链路 |
| `compute_earn_scores` | 122 | scoring.py:491 (compute_scores_v2内部) | 否（V2现行） | compute_scores_v2依赖 |
| `compute_ad_scores_v2` | 320 | scoring.py:493 (compute_scores_v2内部) | 是（待删除） | 见2.4 |
| `compute_scores_v2` | 479 | 一键更新阶段4 / recompute_ad | 否（V2现行主力） | **不可删除**，是当前主链路 |
| `composite_score` | 501 | ranking.py（稳涨/强趋势榜） | 否（独立算法） | 用于其他榜单，非抗跌榜 |

---

## 三、神经网络影响分析

### 3.1 受影响节点

| 节点ID | 节点名称 | Layer | code_files | 影响类型 |
|---|---|---|---|---|
| `score_calc` | 分数计算服务 | business | score_service.py, scoring_v3.py, scoring.py | **直接受影响** — 旧算法所在节点 |
| `rank_calc` | 榜单计算 | business | rank_full.py, ranker.py, algo_config.py | **直接受影响** — _get_unified_score调用nfm旧函数 |
| `detail_api` | 详情API路由 | business | anti/router.py, anti_detail.py | **直接受影响** — 分数历史序列使用nfm旧函数 |
| `one_click_update` | 一键更新 | business | fetch_manager.py, pipeline.py, main.py | **间接受影响** — 阶段4调用compute_scores_v2(内部用compute_ad_scores_v2) |
| `rank_api` | 榜单API路由 | business | rank/router.py | **间接受影响** — 依赖score_calc节点 |
| `metrics_calc` | 指标计算 | data | ranker.py, metrics.py, rank_full.py | **间接受影响** — rank_full中_unified_score依赖 |

### 3.2 关键上下游边

```
one_click_update --call--> score_calc
    (一键更新阶段4批量调用compute_scores_v2)

rank_api --dependency--> score_calc
    (v2.9.51起榜单API使用rank_snapshots.meta中的compute_scores_v2分数)

detail_api --dependency--> score_calc
    (详情API从funds表读取compute_scores_v2分数)

rank_calc --data_flow--> rank_api
    (榜单计算结果存储到rank_snapshots表)

algo_config --dependency--> rank_calc
    (dual_ad_weight/dual_earn_weight被rank_full._anti_score_simple读取)
```

### 3.3 神经网络层面的删除传导路径

```
删除 nfm.anti_score/earn_score/dual_score
  → rank_calc节点 (_get_unified_score) 断裂
    → rank_full._anti_score_simple 降级到DB值（静默行为变化）
  → detail_api节点 (anti_detail分数历史) 断裂
    → 降级到简单算法（anti_detail.py:254-280已有降级逻辑）
  → score_calc节点 (_compute_single_score降级路径) 断裂
    → pool_score未命中时返回None（静默行为变化）

删除 compute_ad_scores_v2
  → score_calc节点 (compute_scores_v2) 内部断裂
    → 一键更新阶段4抗跌分计算失败 → 运行时NameError
  → collector/scoring.py re-export断裂 → ImportError
  → ranker.py import断裂 → ImportError

删除 pool_ad_scores
  → collector/scoring.py re-export断裂 → ImportError
  → ranker.py import断裂 → ImportError
  → 无运行时传导（死代码）
```

---

## 四、前端依赖检查

### 4.1 前端是否直接调用旧评分API

**结论: 否。** 前端没有任何直接调用 `anti-score` / `ad-score` / `earn-score` 等评分计算API的代码。

前端所有评分数据均通过以下API间接获取:
- `/api/ranks` — 榜单数据（含ad_score/earn_score/score字段）
- `/api/fund/anti_detail` — 基金详情弹窗（含resist.ad_score/earn_score/dual）
- `/api/watch/fetch` — 自选实时获取（含ad_score/earn_score/score）
- `/api/compare` — 对比页面（含resist.ad_score/earn_score/dual）

### 4.2 消费评分字段的前端组件

| 组件 | 文件:行号 | 消费字段 | 数据来源 |
|---|---|---|---|
| CompareTable.vue | :80, :94, :116, :147, :152 | `resist.ad_score`, `resist.earn_score`, `fund.ad_score` | /api/compare |
| FundBasicInfo.vue | :35-36 | `resist.ad_score`, `resist.earn_score` | 详情弹窗 |
| FundScoreDetail.vue | :103, :188 | `resist.ad_score` | 详情弹窗 |
| normalize.ts | :189-190 | `ad_score`, `earn_score` | 通用卡片归一化 |
| watch.ts (store) | :54 | `tscore/score/ad_score` 兜底排序 | 自选列表 |
| types/index.ts | :32-33, :157-158, :225-226, :241-242 | `ad_score`, `earn_score` 类型定义 | — |
| shared/types.ts | :158-159 | `ad_score`, `earn_score` 类型定义 | — |

### 4.3 前端风险评估

- **删除旧算法函数本身**: 前端零影响（不直接调用Python函数）。
- **删除后V3产出的字段名变化**: 如果V3不再产出 `ad_score`/`earn_score`/`dual` 同名字段，前端所有上述组件会显示 `—`。**必须确保V3输出字段名与旧算法一致**。
- **测试文件**: `CompareTable.render.test.ts`、`ComparePanel.test.ts`、`FundDetailModal.test.ts` 中有硬编码的 `ad_score/earn_score/dual` 测试数据，不受算法删除影响。

---

## 五、弹窗/基金卡片/对比页面检查

### 5.1 抗跌详情弹窗 (anti_detail.py)

**调用旧函数**: 是，anti_detail.py:250-252
```python
ad = nfm.anti_score(_ma)
earn = nfm.earn_score(_xa, _ma)
comp = nfm.dual_score(_xa, _ma)
```
- 用途: 构建分数历史序列（earn_s/ad_s/comp_s），用于弹窗中的分数演化走势图。
- **已有降级逻辑**: anti_detail.py:253-280，当nfm调用失败时，使用简单算法（ret10映射+最大回撤映射）兜底。
- 删除旧函数后: 若不迁移，每次都走降级路径，分数历史精度下降但不报错。

### 5.2 基金卡片 (FundCard.vue / card_model.py)

**调用旧函数**: 否。
- card_model.py:135,183 仅从dict中读取 `earn_score` 字段并格式化输出。
- 卡片数据来源: 榜单API（rank_snapshots表）或自选API（funds表），分数已在一键更新阶段预计算落库。

### 5.3 对比页面 (compare.py)

**调用旧函数**: 否。
- compare.py:198-200 从 `funds` 表读取 `ad_score`/`earn_score`/`score`（`compute_scores_v2` 的落库结果）。
- compare.py:217 组装 `resist` 对象返回给前端。
- **结论**: 对比页面完全依赖DB落库分数，不直接调用任何评分函数。只要一键更新仍正常写入分数，对比页不受影响。

---

## 六、algo_config 配置项分析

### 6.1 dual_ad_weight / dual_earn_weight 定义

| 配置项 | 文件:行号 | 默认值 | 说明 |
|---|---|---|---|
| `dual_ad_weight` | algo_config.py:65 | 0.50 | 抗跌分权重 |
| `dual_earn_weight` | algo_config.py:66 | 0.50 | 收益分权重 |

**配置元数据**:
- 校验范围: algo_config.py:134-135 (`(0.0, 1.0)`)
- UI分组: algo_config.py:181 (`"综合分权重": ["dual_ad_weight", "dual_earn_weight"]`)
- UI标签: algo_config.py:212-213 (`"抗跌分权重"` / `"收益分权重"`)
- 设置面板展示: src/components/SettingsPanel.vue

### 6.2 引用位置

| # | 文件:行号 | 引用方式 | 上下文 |
|---|---|---|---|
| 1 | night_fund_monitor.py:297 | `cfg.dual_ad_weight`, `cfg.dual_earn_weight` | `dual_score()` 内部，计算加权综合分 |
| 2 | rank_full.py:163-164 | `_cfg.dual_ad_weight`, `_cfg.dual_earn_weight` | `_anti_score_simple()` 降级路径，当统一分数不可用时计算近似综合分 |

### 6.3 删除旧算法后的配置项命运

| 场景 | dual_ad_weight/dual_earn_weight 是否保留 |
|---|---|
| 删除 nfm.dual_score 但保留 rank_full._anti_score_simple | **需保留** — rank_full降级路径仍读取 |
| 同时迁移 rank_full._anti_score_simple 到V3权重体系 | 可删除，但需同步移除algo_config定义、校验、UI标签 |
| V3 (scoring_v3.py) 使用独立常量 | V3已硬编码 `DUAL_EARN_WEIGHT=0.425, DUAL_AD_WEIGHT=0.425, DUAL_CALMAR_WEIGHT=0.15`（scoring_v3.py:415-416），不读取algo_config |

**注意**: V3的权重体系与旧版不同（旧版各50%，V3抗跌42.5%+收益42.5%+卡玛15%），删除旧算法后设置面板中的"综合分权重"滑块将不再生效，需要更新UI说明或移除该配置组。

---

## 七、风险点识别

### 7.1 删除后直接报错（ImportError / AttributeError / NameError）

| 被删函数 | 报错位置 | 错误类型 | 触发条件 |
|---|---|---|---|
| `pool_ad_scores` | collector/scoring.py:8 | ImportError | 模块导入时（服务启动即报错） |
| `pool_ad_scores` | ranker.py:30 | ImportError | ranker模块导入时 |
| `compute_ad_scores_v2` | collector/scoring.py:10 | ImportError | 模块导入时 |
| `compute_ad_scores_v2` | ranker.py:32 | ImportError | ranker模块导入时 |
| `compute_ad_scores_v2` | scoring.py:493 | NameError | 一键更新阶段4调用compute_scores_v2时 |
| `nfm.anti_score` | anti_detail.py:250 | AttributeError | 访问详情弹窗分数历史时 |
| `nfm.anti_score` | rank_full.py:138 | AttributeError | 榜单计算_get_unified_score时 |
| `nfm.anti_score` | score_service.py:231 | AttributeError | 单基金评分降级路径（pool_score未命中时） |
| `nfm.earn_score` | anti_detail.py:251 / rank_full.py:139 / score_service.py:232 | AttributeError | 同上 |
| `nfm.dual_score` | anti_detail.py:252 / rank_full.py:140 / score_service.py:233 | AttributeError | 同上 |

### 7.2 删除后静默行为变化（不报错但结果不同）

| 场景 | 行为变化 | 影响范围 |
|---|---|---|
| nfm旧函数删除后，anti_detail走降级路径 | 分数历史序列从"线性抗跌+收益分"变为"简单ret10映射+最大回撤映射"，精度下降 | 详情弹窗分数演化走势图 |
| nfm旧函数删除后，rank_full._get_unified_score返回None | _anti_score_simple降级到funds表已落库的ad_score/earn_score，若DB中为空则用近5日跌幅近似 | 抗跌榜排名顺序可能变化 |
| nfm旧函数删除后，score_service降级路径返回None | 单基金查询时ad_score/earn_score/dual均为None，前端显示"—" | 自选新加基金（DB无分数时）的卡片展示 |
| compute_ad_scores_v2删除后compute_scores_v2改用V3 | 抗跌分从"6项不等权百分位"变为V3算法，全池分数重分布 | 所有榜单排名、卡片分数、详情弹窗分数 |
| dual_ad_weight/dual_earn_weight配置失效 | 设置面板滑块调整不再影响综合分计算 | 用户可调参数减少 |

### 7.3 前端展示逻辑依赖

- 前端**不依赖**具体算法实现，仅依赖字段名 `ad_score` / `earn_score` / `dual` / `score` / `tscore`。
- **风险**: 如果V3输出的字段名或数值范围（0-100）与旧版不一致，前端展示会异常。
- **要求**: V3必须保持 `ad_score`/`earn_score` ∈ [0,100] 整数，`dual`/`score` ∈ [0,100] 整数。

---

## 八、删除安全结论与迁移建议

### 8.1 各函数删除安全性判定

| 函数 | 可否安全删除 | 前置条件 | 优先级 |
|---|---|---|---|
| `pool_ad_scores` | **是** | 移除 collector/scoring.py:8 和 ranker.py:30 的import | P0 — 可立即删除 |
| `nfm.anti_score` | **否** | 迁移 anti_detail.py:250, rank_full.py:138, score_service.py:231 到V3 | P2 — 需先迁移 |
| `nfm.earn_score` | **否** | 迁移 anti_detail.py:251, rank_full.py:139, score_service.py:232 到V3 | P2 — 需先迁移 |
| `nfm.dual_score` | **否** | 迁移 anti_detail.py:252, rank_full.py:140, score_service.py:233 到V3 | P2 — 需先迁移 |
| `compute_ad_scores_v2` | **否** | compute_scores_v2内部依赖；需先决定compute_scores_v2是否迁移到V3 | P1 — 需架构决策 |

### 8.2 推荐迁移顺序

```
阶段1 (无风险清理):
  1. 删除 pool_ad_scores 函数体 (scoring.py:69-94)
  2. 从 collector/scoring.py import列表移除 pool_ad_scores
  3. 从 ranker.py import列表移除 pool_ad_scores
  → 验证: 服务启动正常，所有榜单API返回正常

阶段2 (compute_scores_v2 架构决策):
  4. 决策: compute_scores_v2 是否整体迁移到V3?
     - 方案A: 保留compute_scores_v2但将内部compute_ad_scores_v2替换为V3的compute_ad_score
     - 方案B: 彻底废弃compute_scores_v2，一键更新阶段4改用scoring_v3批量入口
  5. 按决策迁移，确保一键更新后 funds表 ad_score/earn_score/score 字段正常写入

阶段3 (nfm旧函数迁移):
  6. 迁移 rank_full._get_unified_score → 使用V3 compute_ad_score/compute_earn_score/compute_dual_score
  7. 迁移 anti_detail 分数历史序列 → 使用V3逐点计算
  8. 迁移 score_service._compute_single_score 降级路径 → 使用V3
  9. 删除 nfm.anti_score/earn_score/dual_score 函数体
  10. 评估 dual_ad_weight/dual_earn_weight 配置项是否保留或移除

阶段4 (验证):
  11. 全量回归: 一键更新 → 榜单API → 详情弹窗 → 对比页 → 自选卡片
  12. 验证前端所有组件分数显示正常
```

### 8.3 不可删除的关联函数

| 函数 | 原因 |
|---|---|
| `nfm.anti_metrics` | V3可能仍需要它计算抗跌指标（fund_avg/idx_avg/repair等），agent-hint明确标记不可删除 |
| `scoring.compute_scores_v2` | 当前一键更新阶段4主力入口，被one_click_update和recompute_ad调用，不属于"旧算法"范畴 |
| `scoring.compute_earn_scores` | compute_scores_v2内部依赖，V2现行算法 |
| `scoring_v3.compute_ad_score` (单数) | V3目标版函数，是迁移目标而非删除对象 |
| `scoring_v3.compute_earn_score` (单数) | V3目标版函数 |
| `scoring_v3.compute_dual_score` | V3目标版函数 |

---

## 九、附录: 关键文件清单

| 文件 | 角色 | 需修改 |
|---|---|---|
| backend/analysis_pipeline/night_fund_monitor.py | 旧算法定义 (anti_score:249, earn_score:271, dual_score:290) | 是（阶段3） |
| backend/modules/score/scoring.py | 旧算法定义 (pool_ad_scores:69, compute_ad_scores_v2:320) + compute_scores_v2:479 | 是（阶段1+2） |
| backend/collector/scoring.py | 兼容层re-export | 是（阶段1移除import） |
| backend/modules/rank/ranker.py | 导入但未调用旧函数 | 是（阶段1移除import） |
| backend/modules/rank/rank_full.py | _get_unified_score (138-140) + _anti_score_simple (163-164) | 是（阶段3迁移） |
| backend/modules/anti/anti_detail.py | 分数历史序列 (250-252) | 是（阶段3迁移） |
| backend/modules/score/score_service.py | _compute_single_score降级 (231-233) | 是（阶段3迁移） |
| backend/algo_config.py | dual_ad_weight:65, dual_earn_weight:66 | 评估（阶段3） |
| backend/modules/score/scoring_v3.py | V3目标版 (compute_ad_score, compute_earn_score, compute_dual_score) | 否（迁移目标） |
| backend/modules/fund/fund_metrics.py | 已使用V3 (174-191) | 否（已迁移范例） |
| src/components/SettingsPanel.vue | 综合分权重配置UI | 评估（阶段3） |

---

*报告结束。本报告由自动化影响分析生成，所有行号基于 v2.9.56 代码快照。*
