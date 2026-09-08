# FIXES v2.9.2 — 功能链 P0 修复（2026-09-05）

> 依据：《投研看板 v2.9.1 · 14 条功能链完整矩阵》（backend/docs/CHAIN_TEST_MANUAL.md §3.5/§3.8/§3.11 断链核查）
> 范围：P0 第一步（危险区契约 + 监控链接线）。N2 服务端事件中枢单独排期。
> 验证：uv 沙箱冒烟 5 步全过（干净测试库 + 包内 investment.db 副本），未触碰任何真实库。

## 一、改动清单（4 文件）

| 文件 | 改动 |
|---|---|
| `modules/system/router.py` | ①新增 `POST /api/admin/clear-all-data`（清 11 张业务表）与 `POST /api/admin/reset-settings`（清 settings + 自选分组），危险操作前自动整库快照 `data_backup/pre_*.sql` 保留 5 份，成功后 `_invalidate_all_caches()`（统一缓存/HTML/指标进程缓存）+ 审计；②修复 `ROOT` 少算一层（v2.5.5 迁入 modules/system 后指向 backend/modules/），顺带救活 clear-cache 的 fund_cache.json 静默失败清理 |
| `modules/common/fetch_manager.py` | `start()` 线程入口 `_run` → `_run_monitored` 包装：finally 统一调 `monitor.service.log_data_update`，done→success / error→failed / stopped→cancelled，含 nav_points 计数与 errors 摘要（≤500 字符）；接线失败静默、绝不影响更新任务 |
| `migrate_db.py` | migrate() 第 14 步调用 `monitor/db_init.init_monitor_tables()`（幂等），保证 monitor_errors/data_update_logs/system_metrics/monitor_alerts 随启动创建 |
| `CHANGES.md` | v2.9.2 条目 + 两条待决策已知问题 |

前端零改动：DangerZone.vue 既有调用路径（`POST /api/admin/{kind}`，CONFIRM 二次确认）与响应契约（`{ok,msg}`）完全匹配；更新 msg 由后端返回展示。

## 二、断链修复对照

| 链路 | 修复前 | 修复后 |
|---|---|---|
| §3.8 危险区 | clear-all-data / reset-settings 按钮必 404（后端无端点） | 端点补齐 + 快照逃生门（矩阵 N7 的最小组合） |
| §3.11 监控 | `log_data_update` 零调用者 → 数据更新状态/超时/失败告警永不产生；监控表本身也可能不存在 | 一键更新每次终态落 1 行；migrate 建表。超时>30min→warning、failed→critical 告警实测生成 |
| 隐性 | clear-cache 删缓存文件路径自 v2.5.5 起一直错位静默失败 | ROOT 修复后真正删除 |

## 三、实测记录（沙箱，Python 3.12 + 项目 requirements.txt）

```
[1] migrate() 创建监控四表 OK
[2a] 鉴权 OK: 云端无key/错key→401, 云端正确key→200+已清配置, 本机→放行
[2b] reset-settings OK（快照 pre_reset_settings_*.sql）
[2c] clear-all-data OK（funds/nav/watchlist 全清, 快照 pre_clear_all_data_*.sql, 含审计记录）
[3]  log_data_update 落表 OK；失败+超时告警均生成: ['data_update_failed','data_update_timeout']
[4]  _run_monitored 终态接线 OK：error→failed 且 errors 落表
[5]  done→success + record_count=777 OK
SMOKE ALL PASS ✅
```

## 四、遗留与待决策

1. **鉴权开关疑似失效（重要，需决策）**：`config.py` 的 `require_read_key` 无条件 `return False`（不吃环境变量）、`api_keys` 硬编码随源码分发——若云端未另行改码，`REQUIRE_READ_KEY=1` 的"云端强制鉴权"实际未生效，所有读写接口匿名可调（含本次新增的两个 admin 端点，其沿用全局开关语义保持一致）。根因涉及发布平台注入行为（v2.8.1 api_keys 注释），不宜在本 PATCH 顺手改，建议单独定夺：云端 config 覆盖机制 / admin 端点独立强校验 / 前端 localStorage 已带 `wb_api_key` 时影响面评估。
2. 手册 §6.1「e2e 文件随包分发」与 `_pack.py` 排除 `tests` 矛盾：二选一（打包加回 tests/，或改手册表述）。
3. 提醒链服务端调度、事件中枢（矩阵 N2）、更新完成后榜单自动刷新（N4）按矩阵 P0 后续推进。
