# 部署清单 v2.11.0 — CHG-064 死代码清理与字段规范统一

**版本号**: 2.11.0
**变更ID**: CHG-20260908-065
**日期**: 2026-09-08
**分类**: refactor (高优先级)

---

## 一、修改文件清单（25个生产文件）

### 后端（16个）

| 文件 | 修改内容 |
|------|----------|
| `modules/rank/ranking.py` | 删除 `build_ranks()`；`panel_score()` 仅保留自选算法榜单分支；清理 imports |
| `modules/score/scoring.py` | 删除 `composite_score()` 函数 |
| `modules/rank/recommendation.py` | 删除 `is_candidate()`、`verdict_of()` 死函数 |
| `collector/scoring.py` | 移除 `composite_score` 导出 |
| `modules/rank/ranker.py` | 清理 imports；删除 `dn_days`/`dn_ratio` 计算及返回字段 |
| `modules/common/pipeline.py` | 移除 `composite_score()` 调用和 `cs` 字段 |
| `main.py` | 移除 `fund_html_content` 导入、`__FUND_HTML__` 注入、`/api/fund/html` 路由 |
| `modules/common/compare.py` | 移除 `dn_ratio` 死读取（2处） |
| `modules/rank/rank_full.py` | `_build_item` 删除 `up7`/`reco_score` meta 字段；删除 dn_ratio 注释 |
| `modules/fund/router.py` | 移除 `tscore`/`calmar_score` 写入逻辑 |
| `modules/fund/fund_detail.py` | 修复 `max_dn` 未定义 bug→`max_daily_drop`；移除 `dn_ratio` |
| `modules/anti/anti_detail.py` | 移除 `dn_ratio` 写入和元组引用（2处） |
| `modules/score/scoring_v3.py` | 常量统一：`AD_MAX_DD_COEFF`→`AD_MDD_COEFF`、`AD_MAX_DN_COEFF`→`AD_MAX_DAILY_DROP_COEFF`，与字段名 mdd/max_daily_drop 对齐 |
| `analysis_pipeline/night_fund_monitor.py` | 删除 `dn_ratio` 死输出（前端只用 up_ratio，metrics 不产出 dn_ratio 恒为0）；删除 `max_daily_drop_7d` 重复字典键；删除 up7 写入 |
| `modules/rank/recommendation.py` | 删除六维死函数 `reco_score()`（主流程实际用 `compute_reco_v32`，不引用 up7） |
| `config.py` | 版本号 2.10.0 → 2.11.0 |
| `docs/function_graph.json` | 版本号 2.10.2 → 2.11.0，last_updated=2026-09-08 |

### 前端（8个）

| 文件 | 修改内容 |
|------|----------|
| `src/components/compare/CompareTable.vue` | `up_captureture`→`up_capture`、`dn_captureture`→`dn_capture`、`max_dn`→`max_daily_drop`、删除 `dn_ratio` 行 |
| `src/components/fund/FundScoreDetail.vue` | 同上拼写修复 + 删除 `dn_ratio` |
| `src/types/index.ts` | `AntiResist` 接口拼写修复；`RecoHistoryItem` 添加 `chg` 字段 |
| `src/utils/normalize.ts` | `NormalizedCard.risk.dd7`→`max_daily_drop_7d`；`FIELD_ALIASES` 方向修正（标准名为 key：mdd:['max_dd']、dist20h:['dd20','dd_from_hi']，与后端 card_model 对齐） |
| `src/components/ComparePanel.test.ts` | 旧字段名同步更新 |
| `src/components/FundDetailModal.test.ts` | 旧字段名同步更新 |
| `src/components/settings/AlgoConfigPanel.vue` | 只读面板 key 对齐：`ad_max_dd_coeff`→`ad_mdd_coeff`、`ad_max_dn_coeff`→`ad_max_daily_drop_coeff` |
| `src/components/FundCard.test.ts` | 测试 mock 字段对齐组件：`dd7`→`max_daily_drop_7d`、`maxDd`→`mdd` |


### 部署脚本（1个）

| 文件 | 修改内容 |
|------|----------|
| `_pack.py` | 版本号 2.10.0 → 2.11.0 |
| `src/utils/version.ts` | 版本号 2.10.0 → 2.11.0 |

---

## 二、删除文件清单

### 生产代码删除（3项）

| 文件/目录 | 原因 |
|-----------|------|
| `scripts/recompute_ranks.py` | 完全依赖旧版 `build_ranks`，整体废弃 |
| `modules/common/html_service.py` | 静态HTML链路，仅 `fund_html_content` 函数 |
| `rendering/` 目录（6个文件） | 静态HTML渲染链路（md_builder.py 等） |

### 临时脚本删除（21个，项目根目录）

`_add_change_history.py`, `_add_fusion_dashboard.py`, `_analyze_graph.py`, `_check_graph1.py`, `_check_key_nodes.py`, `_create_checklist.py`, `_fix_prebuild.py`, `_fix_remaining_issues.py`, `_inspect_db.py`, `_pack_local.py`, `_summarize_issues.py`, `_sync_import.py`, `_update_change_history.py`, `_update_docs.py`, `_update_graph.py`, `_update_router.py`, `_update_spec.py`, `_update_version_md.py`, `_update_vue.py`, `_upgrade_to_2938.py`, `_verify_all.py`

> 保留 `_pack.py`（部署脚本）

### 备份文件删除（13个，backend/ 下）

- `fund.db.bak_before_naming_unify`
- `modules/anti/anti_detail.py.bak_035`
- `modules/common/compare.py.bak_035`
- `modules/common/fetch_manager.py.bak`
- `modules/common/fetch_manager.py.bak_035`
- `modules/common/pipeline.py.bak_035`
- `scripts/recompute_ad.py.bak_035`
- `scripts/recompute_ad.py.bak_reco_fix`
- `docs/change_history.json.bak.20260907_171815`
- `docs/change_history.json.bak.20260907_171851`
- `docs/function_graph.json.bak.20260907_171751`
- `docs/function_graph.json.bak.20260907_171815`
- `docs/function_graph.json.bak.20260907_171851`

---

## 三、网关分析报告（5份）

| 文件 | 变更组 | 核心节点 |
|------|--------|----------|
| `docs/gateway_analysis_CHG-064-A.json` | 删除旧版榜单链路 | rank_calc, score_calc, rank_api |
| `docs/gateway_analysis_CHG-064-B.json` | 删除静态HTML链路 | rank_page, fund_card, global_ui |
| `docs/gateway_analysis_CHG-064-C.json` | 前端拼写错误和旧字段名修复 | compare_page, fund_modal, compare |
| `docs/gateway_analysis_CHG-064-D.json` | 后端compare清理和meta死字段删除 | compare, rank_calc, metrics_calc |
| `docs/gateway_analysis_CHG-064-E.json` | funds表死列停止写入 | data_source, fund_universe, score_calc |

---

## 四、验证结果

| 验证项 | 结果 |
|--------|------|
| 后端 `import main` | 通过 |
| 服务启动（端口8000） | 正常 |
| 榜单API `/api/ranks?panel=day` | 正常（160 items） |
| 对比API `/api/watch/compare` | 正常（dn_ratio 已清除） |
| 抗跌详情API `/api/anti/{code}` | 正常 |
| 自选API `/api/watch/list` | 正常 |
| 生产代码 up7/dn_ratio/captureture/旧常量 残留 | 全部 0 处（脚本扫描确认） |
| 前端源码旧字段名 残留 | 0 处 |
| backend/static 历史 hash 清理 | 已清空后重新同步，清除36个旧 JS |
| 前端构建 `npm run build` | 通过（21/21冒烟测试，0问题） |
| 前端静态资源复制 | 已复制到 `backend/static` |
| `function_graph.json` | 58节点/147边，版本2.11.0 |
| `change_history.json` | 最新记录 CHG-20260908-065 v2.11.0 |
| 版本号4处同步 | config.py / version.ts / _pack.py / function_graph.json 均为2.11.0 |

### 附带修复

- **预存bug修复**: `modules/fund/fund_detail.py` L56 引用未定义变量 `max_dn`（应为 `max_daily_drop`），导致对比API 500错误。已修复。

---

## 五、部署步骤

```bash
# 1. 前端构建
cd invest-v281
npm run build

# 2. 清空旧静态资源后再复制（避免叠加累积旧 hash 文件）
Remove-Item backend/static -Recurse -Force -ErrorAction SilentlyContinue
Copy-Item dist backend/static -Recurse

# 3. 启动后端
cd backend
python -m uvicorn main:app --host 0.0.0.0 --port 8000

# 4. 验证
curl -H "X-API-Key: <key>" http://127.0.0.1:8000/api/ranks?panel=day
```

---

## 六、注意事项

1. **历史数据**: `rank_snapshots` 表中历史记录仍含 `up7`/`dn_ratio`/`cs`/`reco_score` 等旧字段（旧代码写入），新代码不再写入这些字段，后续新榜单将不含旧字段。无需数据迁移。
2. **数据库列**: `funds` 表的 `tscore`/`calmar_score`/`verdict` 列保留（SQLite不轻易删列），但已停止写入，恒为NULL。
3. **NeuralUpdater**: 未应用自动更新（会将58节点膨胀至132节点），仅升级版本号字段。图结构保持58节点147边。
4. **up7 已彻底删除**: 六维 `reco_score()` 是死函数（ranker 只 import 未调用，主流程实际用 `compute_reco_v32`，后者不依赖 up7），故 up7 的计算、返回、写入、meta、注释已全部删除，生产代码零残留。
   - 注意区分：被删的是六维 `reco_score` **函数**；funds 表的 `reco_score` **列保留**（由 `compute_reco_v32` 经 recompute_ad 写入、reco 榜排序用，前端不展示）。
   - `dn7` 保留（基金卡片"7日跌N天"在用）；`up_ratio` 保留（"上涨天数占比"在用），只删了与之配对但无人消费的 `dn_ratio`。
5. **常量与字段命名**: 字段（数据）与模块内常量已统一为 mdd / max_daily_drop / up_capture / dn_capture / max_daily_drop_7d，旧名 max_dd/max_dn/dd7/maxDd/captureture 全部清除。
