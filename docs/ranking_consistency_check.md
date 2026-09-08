# 全榜单算法统一性检查报告

> 检查日期：2026-09-07
> 检查范围：13个子榜（日榜×5 + 推荐榜单×4 + 攻防榜×4）
> 生产榜单入口：`modules/rank/rank_full.py::compute_all_panels`（由 `fetch_manager.py` 阶段3调用）

---

## 一、榜单算法矩阵

| # | 榜单名 | Panel/Sub | 使用的算法文件 | 核心函数 | 多套并存 | 死代码/废弃函数 |
|---|--------|-----------|---------------|---------|---------|----------------|
| 1 | 当日涨幅榜 | day/当日 | `rank_full.py` | `_compute_day_panels`（按 `d1` 排序） | 否 | `ranking.py::build_ranks` 中有重复实现（仅 `recompute_ranks.py` 调用） |
| 2 | 三日涨幅榜 | day/三日 | `rank_full.py` | `_compute_day_panels`（按 `d3` 排序） | 否 | 同上 |
| 3 | 七日涨幅榜 | day/七日 | `rank_full.py` | `_compute_day_panels`（按 `d7` 排序） | 否 | 同上 |
| 4 | 近2周涨幅榜 | day/近2周 | `rank_full.py` | `_compute_day_panels`（按 `d10` 排序） | 否 | 同上 |
| 5 | 综合榜 | day/综合 | `rank_full.py` | `_compute_day_panels`（四榜并集+上榜次数+综合分） | 否 | 同上（`build_ranks` 无综合榜实现） |
| 6 | **抗跌榜** | reco/抗跌 | `rank_full.py` + `night_fund_monitor.py` + `scoring_v3.py` + `scoring.py` | `_anti_score_simple` → `_get_unified_score` → `nfm.anti_score/earn_score/dual_score`；`_precompute_v3_scores` → `scoring_v3.compute_pool_scores` | **是（3套）** | `scoring.py::compute_scores_v2`（百分位版，仅 `recompute_ad.py` 写库用，未接入榜单排序） |
| 7 | 自选榜 | reco/自选 | `rank_full.py` → `ranking.py` | `ranker.panel_score("自选算法榜单")` → `watch_score_light` | 否 | `ranking.py::build_ranks` 中有重复实现（仅 `recompute_ranks.py` 调用） |
| 8 | 质量榜 | reco/质量 | `rank_full.py`（内联） | `_compute_reco_panels` 内联 min-max 归一化（5因子） | 否 | **文档与代码不一致**（见问题清单#2） |
| 9 | 超跌筑底榜 | reco/超跌筑底 | `rank_full.py` | `_compute_crash_bottom_panel`（急跌+低位+RPS+筑底四阶段） | 否 | 无 |
| 10 | 高弹性 | attack/高弹性 | `rank_full.py` | `_compute_attack_panels`（β>阈值 且 上涨捕获率>阈值） | 否 | 无 |
| 11 | 攻守兼备 | attack/攻守兼备 | `rank_full.py` | `_compute_attack_panels`（β区间 + 捕获率差 + 最大回撤） | 否 | 无 |
| 12 | 强抗跌 | attack/强抗跌 | `rank_full.py` | `_compute_attack_panels`（下跌捕获率 + 下行波动率 + 最大回撤） | 否 | 无 |
| 13 | 反弹先锋 | attack/反弹先锋 | `rank_full.py` | `_compute_attack_panels`（20日跌幅 + 5日涨幅，ETF额外技术形态） | 否 | 无 |

---

## 二、评分模块全函数清单与调用状态

### 2.1 `modules/score/scoring.py`（百分位评分体系，v2.4旧版）

| 函数 | 状态 | 调用方 | 说明 |
|------|------|--------|------|
| `pool_earn_scores` | **死代码** | 仅 `scripts/recompute_earn.py`（一次性脚本） | 旧版池内百分位收益分，生产无调用 |
| `pool_ad_scores` | **死代码** | 无生产调用方 | 旧版池内百分位抗跌分 |
| `compute_earn_scores` | 在用（间接） | `compute_scores_v2` → `recompute_ad.py` | v0.36收益分（前15%基准锚定） |
| `compute_ad_scores_v2` | 在用（间接） | `compute_scores_v2` → `recompute_ad.py` | 6项不等权抗跌分 |
| `compute_scores_v2` | 在用（写库） | `recompute_ad.py`、`recompute_ranks.py` | 统一评分入口，**仅用于写 funds 表，不用于榜单排序** |
| `composite_score` | 在用 | `panel_score`（稳涨/强趋势/ETF已废弃）、`pipeline.py`（cs字段） | 综合分（趋势40+支撑30+稳健20+平衡10） |
| `compute_momentum5` | 在用（降级） | `ranker.py` 动能5档降级路径 | 动能5档判定 |
| `calc_excess_rets` | 在用（降级） | `ranker.py` 动能5档降级路径 | 超额收益计算 |
| `big_rise_follow` | 在用（降级） | `ranker.py` 动能5档降级路径 | 大涨跟随弹性 |
| `detect_yindie_deep` | 在用（间接） | `compute_scores_v2` | 阴跌/深调识别 |
| `_up_capture` | 在用（间接） | `compute_scores_v2` | 上涨捕获率 |
| `_fund_bench_series` | 在用（间接） | `detect_yindie_deep` | 池内类基准序列 |
| `_align_bench_vals` | 在用（间接） | `calc_excess_rets` | 基准日期对齐 |

### 2.2 `modules/score/scoring_v3.py`（方案C+评分体系，v2.1.6现行）

| 函数 | 状态 | 调用方 | 说明 |
|------|------|--------|------|
| `compute_pool_scores` | **在用（榜单）** | `rank_full._precompute_v3_scores`、`score_service.compute_pool_scores` | 批量评分入口，抗跌榜预计算使用 |
| `compute_earn_score` | 在用（间接） | `compute_pool_scores` | 线性收益分（50+加权收益×3+修复奖励-暴跌惩罚） |
| `compute_ad_score` | 在用（间接） | `compute_pool_scores` | 线性抗跌分（50-风险指标+反弹奖励） |
| `compute_dual_score` | 在用（间接） | `compute_pool_scores` | 综合分（收益42.5%+抗跌42.5%+卡玛15%） |
| `compute_calmar_percentile` | 在用（间接） | `compute_pool_scores` | 卡玛比率百分位 |
| `compute_calmar_6m` | 在用（间接） | `compute_calmar_percentile` | 近6月卡玛计算 |
| `compute_weighted_return` | 在用（间接） | `compute_earn_score`、`compute_pool_scores` | 加权收益计算 |
| `percentile_rank` | 工具函数 | `compute_calmar_percentile` | 百分位排名 |
| `inverse_percentile_rank` | 工具函数 | 无直接调用 | 反向百分位（定义但未被使用） |
| `percentile_to_score` | 在用（间接） | `compute_calmar_percentile` | 百分位转分段分数 |

### 2.3 `modules/score/score_service.py`（评分服务层）

| 函数 | 状态 | 调用方 | 说明 |
|------|------|--------|------|
| `compute_pool_scores` | 在用（API） | API层、`calc_scores` 降级 | 调用 `collector.scoring_v3.compute_pool_scores`，带1小时缓存 |
| `get_pool_score` | 在用 | `calc_scores` | 从缓存取单只基金评分 |
| `calc_scores` | **已废弃** | 无生产调用方（标记 v2.9.43 DEPRECATED） | 单基金实时计算，降级到 `nfm.anti_score/earn_score/dual_score` |
| `save_score_snapshot` | 在用 | 基金详情页 | 保存评分快照 |
| `load_score_snapshots` | 在用（API） | `/api/score-history/{code}` | 加载评分历史 |

### 2.4 `analysis_pipeline/night_fund_monitor.py`（夜间监控评分）

| 函数 | 状态 | 调用方 | 说明 |
|------|------|--------|------|
| `anti_score` | **在用（榜单降级）** | `rank_full._get_unified_score`、`score_service.calc_scores`（废弃） | 简单线性抗跌分（50+(1-ratio)×40+修复×2） |
| `earn_score` | **在用（榜单降级）** | 同上 | 简单线性收益分（50+平均涨幅×4+修复奖励） |
| `dual_score` | **在用（榜单降级）** | 同上 | 综合分（抗跌×权重+收益×权重） |
| `anti_metrics` | 在用 | `rank_full._precompute_v3_scores`、`_get_unified_score`、`score_service` | 大跌日抗跌指标计算 |
| `calc_metrics` | 在用 | `score_service.compute_pool_scores` | 封装 `ranker.calc_metrics` |
| `fetch_ddown` | 在用 | `rank_full`、`score_service` | 拉取指数下跌日 |

### 2.5 `modules/rank/ranking.py`（旧榜单构建层）

| 函数 | 状态 | 调用方 | 说明 |
|------|------|--------|------|
| `watch_score_light` | **在用（自选榜）** | `panel_score("自选算法榜单")` → `rank_full` | 自选综合分（中长期35+近期25+回撤15+波动16+夏普9） |
| `panel_score` | **在用** | `rank_full`（自选榜）、`recompute_ad.py`（tscore刷新） | 面板特定综合分分发器 |
| `build_ranks` | **遗留/死代码** | 仅 `scripts/recompute_ranks.py` | 完整旧榜单实现（日榜/稳涨/强趋势/ETF/自选算法榜单/抗跌榜单/开仓榜单/综合推荐/动能衰减预警），与生产 `compute_all_panels` 算法不一致 |

### 2.6 `modules/rank/ranker.py`（指标计算层）

| 函数 | 状态 | 调用方 | 说明 |
|------|------|--------|------|
| `calc_metrics` | 在用 | 全系统 | 常规指标计算（区间涨幅/波动/回撤/捕获率等） |
| `_build_idx_map` | 在用 | 全系统 | 指数日期对齐 |
| `_build_ddays` | 在用 | `pipeline.py`、`recompute_ad.py` | 大跌/大涨日构造 |
| `anti_resist` | 在用（计算但不用于榜单排序） | `pipeline.py`（存 `resist` 字段）、`recompute_ad.py`、`recompute_ranks.py` | 抗跌专项指标，与 `nfm.anti_metrics` 功能重叠 |

---

## 三、问题清单（除抗跌榜外）

### 问题1：`build_ranks` 完整旧榜单实现与生产算法并存

- **位置**：`modules/rank/ranking.py::build_ranks`（第111-370行）
- **现状**：包含日榜四期、稳涨、强趋势、ETF、自选算法榜单、抗跌榜单、开仓榜单、综合推荐、动能衰减预警共9个旧子榜的完整排序逻辑。仅被 `scripts/recompute_ranks.py` 调用。
- **风险**：`recompute_ranks.py` 运行后会用旧算法覆盖 `rank_snapshots`，导致榜单结果与生产 `compute_all_panels` 不一致（如旧版无攻防榜、无超跌筑底榜、自选榜取前30而非40）。
- **严重度**：中（脚本非自动运行，但手动执行会导致数据不一致）

### 问题2：质量榜文档与实际代码公式不一致

- **位置**：`rank_config.py`（第77行）、`rendering/fund_page.py`（第310行）
- **文档描述**：`质量分 = 下行夏普35% + 卡玛30% - 下跌捕获20% + 上涨捕获15%`（4因子）
- **实际代码**（`rank_full.py` 第563行，v2.9.12）：`质量分 = 下行夏普30% + 卡玛25% + 盈利稳定性15% + 回撤15% + 近1年收益15%`（5因子，min-max归一化）
- **影响**：用户看到的算法说明与实际排名逻辑不符，可能导致对榜单结果的误判。
- **严重度**：高（用户可见的描述错误）

### 问题3：`pool_earn_scores` / `pool_ad_scores` 死代码残留

- **位置**：`modules/score/scoring.py` 第39-107行
- **现状**：旧版池内百分位评分函数，`pool_earn_scores` 仅被一次性脚本 `recompute_earn.py` 调用，`pool_ad_scores` 无任何生产调用方。
- **严重度**：低（不影响运行，但增加代码维护成本）

### 问题4：`score_service.calc_scores` 已废弃但保留完整降级逻辑

- **位置**：`modules/score/score_service.py` 第148-240行
- **现状**：标记为 `[DEPRECATED v2.9.43]`，但仍保留约90行代码，包含从批量缓存降级到 `nfm` 单基金计算的完整路径。无生产调用方。
- **严重度**：低

### 问题5：`anti_resist` 与 `nfm.anti_metrics` 功能重叠

- **位置**：`modules/rank/ranker.py::anti_resist`（第234行） vs `analysis_pipeline/night_fund_monitor.py::anti_metrics`（第173行）
- **现状**：两个函数都计算"大跌日基金平均跌幅/反弹幅度"，但实现细节不同：
  - `anti_resist`：返回 `ad_score/earn_score/dual/avg_dd_fund/rep5_avg/rep10_avg`，用 `_build_ddays` 的大跌日定义
  - `anti_metrics`：返回 `fund_avg/repair/repair_10d/detail`，用 `fetch_ddown` 的下跌日定义
  - `pipeline.py` 用 `anti_resist`（存 `resist` 字段供 `compute_scores_v2` 使用）
  - `rank_full.py` 用 `anti_metrics`（供 `scoring_v3` 和 `nfm` 评分使用）
- **风险**：两套大跌日定义和指标计算可能导致 `funds.ad_score`（由 `recompute_ad` 用 `anti_resist`+`compute_scores_v2` 算出）与榜单显示分数（由 `rank_full` 用 `anti_metrics`+`scoring_v3` 算出）不一致。
- **严重度**：中

### 问题6：`fund_page.py` 仍描述已废弃的ETF榜规则

- **位置**：`rendering/fund_page.py` 第312-314行
- **现状**：v2.9.1已将ETF榜重构为超跌筑底榜，但该页面仍展示"④ ETF榜 · 按主题分组，每组取前3名"及ETF动量分公式。
- **严重度**：中（用户可见的过时文档）

### 问题7：`recompute_ranks.py` 使用旧版 `build_ranks` 算法

- **位置**：`scripts/recompute_ranks.py` 第139行
- **现状**：调用 `ranker.build_ranks`，该函数不包含攻防榜（attack×4）和超跌筑底榜，且自选榜/抗跌榜取前30而非40，与生产 `compute_all_panels` 结果不一致。
- **严重度**：中（手动运行会覆盖生产榜单数据）

---

## 四、抗跌榜三套算法现状记录（本次统一目标）

| 套数 | 算法文件 | 核心函数 | 评分方式 | 当前状态 |
|------|---------|---------|---------|---------|
| 第1套 | `analysis_pipeline/night_fund_monitor.py` | `anti_score` / `earn_score` / `dual_score` | 简单线性公式（抗跌=50+(1-ratio)×40+修复×2；收益=50+均涨幅×4+修复） | **在用**：`rank_full._get_unified_score` 降级路径、`score_service.calc_scores`（已废弃）降级路径 |
| 第2套 | `modules/score/scoring.py` | `compute_ad_scores_v2` / `compute_earn_scores` / `compute_scores_v2` | 池内百分位归一（抗跌6项不等权+阴跌惩罚；收益前15%基准锚定+动能衰减惩罚+捕获率约束） | **未接入榜单**：仅 `recompute_ad.py` 用于写 `funds.ad_score/earn_score/score` 字段 |
| 第3套 | `modules/score/scoring_v3.py` | `compute_ad_score` / `compute_earn_score` / `compute_dual_score` / `compute_pool_scores` | 线性评分（抗跌=50-风险×系数+反弹奖励；收益=50+加权收益×3+修复-暴跌惩罚；综合=收益42.5%+抗跌42.5%+卡玛15%） | **在用（预计算）**：`rank_full._precompute_v3_scores` 批量计算后缓存，`_get_unified_score` 优先读缓存 |

**实际运行路径**：`rank_full.compute_all_panels` → `_precompute_v3_scores`（批量算 scoring_v3，缓存）→ `_anti_score_simple` → `_get_unified_score`（优先读 v3 缓存，失败时降级到 nfm 简单公式）。

---

## 五、清理建议

> 本次任务仅清理抗跌榜，以下为其他问题的后续清理建议。

### 优先级 P0（用户可见错误）
1. **修正质量榜文档**：更新 `rank_config.py` 第77行和 `fund_page.py` 第310行的质量分公式描述，与 v2.9.12 实际代码（5因子）一致。
2. **修正 fund_page.py 超跌筑底榜描述**：将第312-314行的旧ETF榜描述替换为超跌筑底榜的四阶段筛选说明。

### 优先级 P1（数据一致性风险）
3. **废弃或重写 `recompute_ranks.py`**：该脚本调用旧版 `build_ranks`，运行后会覆盖生产榜单。建议改为调用 `compute_all_panels`，或明确标注为"仅用于历史数据恢复，勿用于日常重算"。
4. **统一抗跌指标计算**：将 `pipeline.py` 中的 `anti_resist` 替换为 `nfm.anti_metrics`，确保 `funds.ad_score` 与榜单显示分数使用同一套大跌日定义和指标。

### 优先级 P2（代码整洁）
5. **删除 `build_ranks` 中的废弃榜单实现**：稳涨/强趋势/ETF/抗跌榜单/开仓榜单/综合推荐已在 `rank_config.DEPRECATED_RANKS` 中标记删除，可从 `build_ranks` 中移除对应代码块。保留 `watch_score_light` 和 `panel_score`（仍被自选榜使用）。
6. **删除 `pool_earn_scores` / `pool_ad_scores`**：确认无调用方后移除，或迁移到 `scripts/` 目录。
7. **删除 `score_service.calc_scores`**：已标记废弃且无调用方，可安全移除。
8. **移除 `scoring_v3.inverse_percentile_rank`**：定义后无任何调用方。

### 优先级 P3（架构优化）
9. **评分函数统一出口**：当前抗跌分有3个计算路径（nfm简单版、scoring.py百分位版、scoring_v3线性版），建议统一为单一出口函数，内部根据配置选择算法，避免 `_get_unified_score` 中的多级降级逻辑。
10. **`collector/` 兼容层清理**：`collector/ranker.py`、`collector/scoring.py`、`collector/scoring_v3.py`、`collector/rank_full.py` 均为 re-export 兼容层，待所有调用方迁移到 `modules/` 后可移除。

---

## 六、总结

- **13个子榜中，12个榜单使用单一算法实现**，无多套并存问题。
- **抗跌榜**存在3套算法并存（已知问题，本次统一）。
- **主要风险不在多套算法，而在文档与代码不一致**（质量榜公式、ETF榜过时描述）和**遗留脚本使用旧算法**（`recompute_ranks.py` 调用 `build_ranks`）。
- **评分模块存在约4个死代码函数**（`pool_earn_scores`、`pool_ad_scores`、`calc_scores`、`inverse_percentile_rank`），不影响运行但增加维护成本。
- **`anti_resist` 与 `anti_metrics` 功能重叠**可能导致 `funds` 表分数与榜单显示分数不一致，建议后续统一。
