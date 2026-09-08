# invest-v281 项目管理准则

> 本文档记录项目开发过程中积累的硬性规范和踩坑经验。所有代码变更必须遵守。
> 最后更新：v2.11.0

---

## 一、字段生命周期追踪规范

### 1.1 每个数据字段必须可追踪完整链路

每个字段从产生到消亡必须经过四个阶段，缺一不可：

```
计算 → 存储 → 同步 → 展示
```

| 阶段 | 要求 | 检查方法 |
|------|------|----------|
| 计算 | 明确在哪个函数/哪一行计算，输入来源是什么 | Grep 字段名 + Read 计算函数 |
| 存储 | 明确写入哪张表的哪一列，或仅存在于内存 | Grep INSERT/UPDATE/bulk_upsert |
| 同步 | 明确是否从DB读入meta、是否存入rank_snapshots | 检查 _build_item / _load_funds_meta |
| 展示 | 明确前端哪个组件在什么场景下展示 | Grep 前端 .vue/.ts 文件 |

### 1.2 引用检查必须覆盖间接引用

**禁止只查直接引用。** 以下间接引用方式必须检查：

- **dict.get() 动态读取**：`m.get('field')` 或 `item.get('field', default)`
- **变量传递**：字段值赋给变量后在别处使用
- **\*\*kwargs / 解包**：`func(**meta)` 导致字段隐式传递
- **别名/适配层**：normalize.ts 的 FIELD_ALIASES、后端的字段重命名
- **SQL 列名映射**：`SELECT old_name AS new_name`
- **JSON 序列化**：字段存入 rank_snapshots.meta JSON 后被前端读取
- **collector/ 兼容层**：collector/*.py 是 modules/ 的 re-export，查引用要两边都查

### 1.3 字段删除前的强制检查清单

删除任何字段前必须确认：

- [ ] 后端生产代码（排除 _*.py 临时脚本）无引用
- [ ] 前端 .vue/.ts 无引用（包括类型定义接口）
- [ ] 数据库写入方已移除（不会再写入该列）
- [ ] rank_snapshots 历史数据中的该字段不会导致前端报错（前端有 undefined 保护）
- [ ] 测试文件中的 mock 数据已同步更新
- [ ] 注释和文档中的旧字段名已清理

---

## 二、变更准入网关强制流程

### 2.1 每次代码修改必须先走 SuperPower 网关分析

**禁止跳过网关直接改代码。** 流程：

```
1. 确定变更组（一组相关的修改作为一个变更单元）
2. 调用 GatewayAnalyzer 分析影响范围
3. 审查分析报告：受影响节点、上下游边、风险点、检查清单
4. 按检查清单执行修改
5. 修改后逐组验证
6. NeuralUpdater 更新 function_graph.json
7. ChangeRecorder 记录变更 + 升级版本号
```

### 2.2 GatewayAnalyzer 正确用法

```python
from modules.superpower.gateway_analyzer import GatewayAnalyzer

analyzer = GatewayAnalyzer('docs/function_graph.json')
result = analyzer.analyze(
    change_id='CHG-064-A',
    core_nodes=['rank_build', 'scoring'],  # 必须是 function_graph.json 中存在的节点id
    change_title='删除旧版榜单链路'
)
```

- `core_nodes` 必须是 function_graph.json 中实际存在的节点 id，先查节点列表再传
- 分析报告保存到 `docs/gateway_analysis_CHG-*.json`

### 2.3 变更组划分原则

- 高内聚的修改放在一组（如"旧版榜单链路删除"是一组）
- 低耦合的修改分开（前端修复和后端删除分开）
- 每组修改后立即验证，不要攒到最后一起测

---

## 三、新旧代码不允许并存

### 3.1 删除要彻底

**该删的全删，连以下内容都要清理：**

- 旧版函数体（不只是不调用，函数定义也要删）
- 旧版字段（计算、存储、读取三处都要删）
- 兼容代码和 fallback 分支（`if old_field else new_field`）
- 注释中的旧字段名和旧逻辑说明
- 别名映射（normalize.ts FIELD_ALIASES 中已无运行时引用的条目）
- 临时脚本（_*.py，部署前清理）
- 废弃的 API 路由（不只是注释掉，要删除）

### 3.2 算法只留一套

- 评分算法：只用 scoring_v3.py 的 dual_score，不允许 scoring.py 的 composite_score 并存
- 榜单构建：只用 rank_full.compute_all_panels()，不允许 ranking.build_ranks() 并存
- 如果需要重算，调用主流程（fetch_manager 一键更新），不要用旧版脚本

### 3.3 collector/ 兼容层处理

- collector/ 目录是 v2.5.5 架构重构后的 re-export 兼容层
- 实际实现在 modules/ 对应文件中
- 修改函数时两边都要检查，但只改 modules/ 中的实现

---

## 四、命名规范

### 4.1 四条命名规则

| 规则 | 示例 | 反例 |
|------|------|------|
| 行业标准缩写用缩写 | mdd、vol、calmar、sharpe | max_drawdown、volatility |
| 无标准缩写用全称（下划线分隔） | up_capture、dn_capture、down_vol、max_daily_drop | upcap、dncap、maxdn |
| 周期指标缩写+数字 | d1、d3、d7、d10、m1、m3、m6、y1、dn7 | day1、month3、up_7 |
| 重复字段合并成一个，删掉重复的 | 标准名 dist20h（dd20/dd_from_hi 仅作读取历史数据的兼容别名） | dist20h、dd20、dd_from_hi 三个同时当输出字段 |

### 4.2 易混淆字段必须区分

- `dist20h`：距历史高点回撤（标准字段；dd20 / dd_from_hi 仅为读取旧数据时的兼容别名，见 card_model.FIELD_ALIASES）
- `dd_from_high` / `dd_from_60high`：超跌筑底榜专用，距60日高点回撤
- 含义不同的字段不能用近似命名，必须加限定词

### 4.3 前端类型定义必须与实现一致

- types/index.ts 中的接口字段必须与实际数据结构一致
- 发现接口定义与实现不一致时立即修复，不要留"死字段"
- 动态 Record 类型（如 `AntiStats = Record<string, Num>`）要列出实际访问的 key 清单

---

## 五、版本号同步规范

### 5.1 版本号必须在 4 处同步

| 文件 | 位置 |
|------|------|
| backend/config.py | `APP_VERSION` 默认值 |
| src/utils/version.ts | `APP_VERSION` 常量 |
| _pack.py（项目根目录） | `VERSION` 常量 |
| backend/docs/function_graph.json | 顶层 `version` 字段 |

**任何一处修改版本号，其他三处必须同步。** 不一致会导致前端缓存清除失效、部署包版本错误。

### 5.2 版本号升级规则

- patch（x.y.Z）：bug修复、小调整
- minor（x.Y.0）：功能变更、字段删除、接口变更
- major（X.0.0）：架构重构、不兼容变更

### 5.3 ChangeRecorder 自动升级

```python
from modules.superpower.change_recorder import ChangeRecorder
recorder = ChangeRecorder('docs/change_history.json')
recorder.record(
    title='...', description='...', category='...', priority='...',
    affected_files=[...], affected_nodes=[...], changes=[...],
    bump='minor', level='minor'  # 自动升级版本号
)
```

- change_history.json 的 `changes` 是**列表**，用 append，不是字典
- 记录后手动确认 4 处版本号一致

---

## 六、验证清单

### 6.1 每次修改后的必做验证

| 验证项 | 命令/方法 | 通过标准 |
|--------|-----------|----------|
| 后端导入 | `cd backend; python -c "import main"` | 无报错 |
| 服务启动 | `python -m uvicorn main:app --host 0.0.0.0 --port 8000` | 正常启动无异常 |
| 榜单API | `curl -H "X-API-Key: ..." /api/rank/day` | 返回正常，字段无旧名 |
| 对比API | `/api/watch/compare` | 字段正确，无拼写错误 |
| 自选API | `/api/watch/list` | 正常返回 |
| 前端构建 | `npm run build` | 无 TypeScript 错误 |
| 前端部署 | `Copy-Item dist/* backend/static -Recurse -Force` | static 目录更新 |

### 6.2 一键更新全流程验证

修改涉及榜单/评分/采集逻辑时，必须验证一键更新全流程：
1. 触发 `/api/update/start`
2. 等待完成（监控 `/api/update/status`）
3. 检查各榜单数据是否正常生成
4. 检查 rank_snapshots 表 meta 字段是否符合预期

### 6.3 神经网络图验证

- NeuralUpdater 后检查 function_graph.json 节点数
- 节点数不应暴增（传 scripts/ 文件会导致 58→243）
- 只传实际修改的生产代码文件，不传 _*.py 和 scripts/

---

## 七、常见坑与规避

### 7.1 Edit 工具对含中文文件可能失败

- 含中文的 Python/Vue 文件，Edit 工具可能因不可见字符匹配失败
- **规避**：用 Python 脚本读取→修改→写入，指定 `encoding='utf-8'`
- 示例：
  ```python
  with open('file.py', 'r', encoding='utf-8') as f:
      content = f.read()
  content = content.replace('old', 'new')
  with open('file.py', 'w', encoding='utf-8') as f:
      f.write(content)
  ```

### 7.2 PowerShell 引号转义

- PowerShell 中含中文的 `python -c "..."` 命令引号转义经常失败
- **规避**：写成 .py 文件执行，不要用 -c 内联

### 7.3 change_history.json 结构

- `changes` 是列表，不是字典
- 用 `changes.append(record)`，不要用 `changes[key] = record`
- 顶层有 meta、changes、last_updated、current_version、total_changes

### 7.4 NeuralUpdater 不要传 scripts 文件

- 传 scripts/ 下的文件会导致节点数从 58 暴增到 243
- 只传 backend/modules/、backend/*.py（生产代码）、src/ 下的文件
- 不传 _*.py 临时脚本

### 7.5 SQLite DROP COLUMN 限制

- SQLite 3.35+ 支持 DROP COLUMN，但生产环境版本不确定
- **规避**：停止写入该列即可，不强制 DROP；如需 DROP 先确认版本
- funds 表的死列（tscore/calmar_score/verdict）停止写入后保留列不影响功能

### 7.6 前端静态文件注入

- main.py 的 index 路由向 index.html 注入 `__FUND_HTML__`、`__WATCH_DATA_JSON__` 等占位符
- 删除注入内容时，占位符替换为空字符串即可，不要删除 index.html 中的占位符标记
- 前端 Vue 应用不依赖 `__FUND_HTML__`（静态HTML产物），但依赖 `__WATCH_DATA_JSON__`

### 7.7 rank_snapshots.meta 是单基金 dict

- rank_snapshots 表的 meta 列是单个基金的 dict，不是榜单列表
- 前端从 /api/rank/* 接口拿到的 items[].meta 就是这个 dict
- 修改 _build_item 返回字段后，前端类型定义要同步更新

---

## 八、临时脚本管理

### 8.1 _*.py 脚本规范

- `_` 开头的 Python 文件是临时调试/迁移脚本
- 完成使命后必须删除，不要留在代码库中
- 部署包中不包含任何 _*.py 文件
- 临时脚本不要 import 生产代码中的内部函数（会产生虚假依赖）

### 8.2 部署前清理

```powershell
# 清理 backend 下所有 _*.py
Get-ChildItem backend -Filter "_*.py" -Recurse | Remove-Item
# 清理项目根目录下的 _*.py（保留 _pack.py 部署脚本）
Get-ChildItem . -Filter "_*.py" | Where-Object { $_.Name -ne '_pack.py' } | Remove-Item
```

---

## 九、API 接口规范

### 9.1 旧接口别名处理

- 线上版兼容路由（如 /api/fund/anti_detail → /api/anti/{code}）在确认无外部调用后删除
- 删除前检查前端是否调用该路由
- 不要保留"以防万一"的兼容路由

### 9.2 API Key

- 所有 /api/* 路由需要 `X-API-Key: 35a92aeb9e0acef9bfbdcbe733f74c4d` 请求头
- 测试时不要忘记带

---

## 十、文档维护

### 10.1 必须维护的文档

| 文档 | 位置 | 更新时机 |
|------|------|----------|
| 字段审计表 | docs/field_audit_backend.csv / field_audit_frontend.csv | 字段变更后 |
| 神经网络图 | docs/function_graph.json | 每次代码变更后（NeuralUpdater） |
| 变更历史 | docs/change_history.json | 每次变更后（ChangeRecorder） |
| 网关分析报告 | docs/gateway_analysis_CHG-*.json | 每次变更前 |
| 项目准则 | docs/PROJECT_GUIDELINES.md | 发现新坑后 |
| 架构文档 | docs/SYSTEM_ARCHITECTURE.md | 架构变更后 |

### 10.2 本文档更新规则

- 发现新的踩坑经验立即追加到对应章节
- 不要删除旧条目（即使已修复，作为历史警示保留）
- 每条经验必须有：问题描述 + 规避方法 + 示例
