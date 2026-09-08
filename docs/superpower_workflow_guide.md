# Superpower工作流引擎使用指南

## 目录

1. [概述](#1-概述)
2. [架构设计](#2-架构设计)
3. [快速开始](#3-快速开始)
4. [模块详解](#4-模块详解)
5. [配置说明](#5-配置说明)
6. [使用示例](#6-使用示例)
7. [扩展指南](#7-扩展指南)
8. [常见问题](#8-常见问题)

---

## 1. 概述

Superpower工作流引擎是一个面向代码变更管理的自动化闭环系统，旨在解决大型项目中代码修改影响范围不可控、测试遗漏、版本管理混乱等问题。

### 核心能力

- **神经网络影响分析**：基于`function_graph.json`功能依赖图谱，对变更核心节点执行BFS正向/反向传播分析，精确识别受影响的上下游节点
- **智能风险识别**：自动识别影响范围、核心业务层、数据库操作、API接口等7类风险点，按HIGH/MEDIUM/LOW分级
- **分级检查清单**：根据风险分析自动生成must/should/optional三级检查清单，覆盖修改、验证、更新、记录四个阶段
- **半自动神经网络更新**：静态解析Python代码结构，与现有图谱做差异分析，生成更新预览，人工确认后执行，支持自动备份和回滚
- **变更记录与版本管理**：自动生成变更ID（CHG-YYYYMMDD-NNN），记录完整变更信息到`change_history.json`，语义化升级版本号

### 适用场景

- 核心模块代码修改前的影响范围评估
- 多人协作项目的变更审查与测试路径规划
- 版本发布前的变更记录与版本号管理
- 功能依赖图谱的自动化维护

---

## 2. 架构设计

### 2.1 六阶段状态机

Superpower工作流引擎采用严格的六阶段状态机驱动，每个阶段完成后才能进入下一阶段：

```
┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐
│ TRIGGER  │───▶│ ANALYZE  │───▶│  MODIFY  │───▶│  VERIFY  │───▶|  UPDATE  |───▶|  RECORD  |
│  触发阶段  │    │  分析阶段  │    │  修改阶段  │    │  验证阶段  │    │ 更新阶段  │    │ 记录阶段  │
└──────────┘    └──────────┘    └──────────┘    └──────────┘    └──────────┘    └──────────┘
     │                                                                │                    │
     │ 简单修改自动跳过                                                │ 失败可回滚          ▼
     ▼                                                                ▼              ┌──────────┐
┌──────────┐                                                   ┌──────────┐        │COMPLETED │
│  跳过分析  │                                                   │  FAILED  │        │  完成     │
└──────────┘                                                   └──────────┘        └──────────┘
```

### 2.2 各阶段职责

| 阶段 | 职责 | 核心模块 |
|------|------|----------|
| **TRIGGER** | 接收变更请求，智能判断修改复杂度（简单修改自动跳过分析） | SuperpowerWorkflow |
| **ANALYZE** | 调用GatewayAnalyzer执行神经网络影响传播分析，生成风险点和检查清单 | GatewayAnalyzer |
| **MODIFY** | 记录代码修改操作，更新modify阶段检查项状态 | SuperpowerWorkflow |
| **VERIFY** | 逐项验证检查清单，must项必须全部通过才能进入下一阶段 | SuperpowerWorkflow |
| **UPDATE** | 调用NeuralUpdater更新function_graph.json神经网络（半自动，需确认） | NeuralUpdater |
| **RECORD** | 调用ChangeRecorder记录变更到change_history.json，升级版本号 | ChangeRecorder |

### 2.3 模块关系图

```
                    ┌─────────────────────┐
                    │  SuperpowerWorkflow  │
                    │   (工作流引擎主类)    │
                    └──────────┬──────────┘
                               │
           ┌───────────────────┼───────────────────┐
           │                   │                   │
           ▼                   ▼                   ▼
  ┌────────────────┐  ┌────────────────┐  ┌────────────────┐
  │ GatewayAnalyzer │  │ NeuralUpdater  │  │ ChangeRecorder │
  │  (网关分析器)    │  │ (神经网络更新器) │  │  (变更记录器)   │
  └────────────────┘  └────────────────┘  └────────────────┘
           │                                       │
           ▼                                       ▼
  ┌────────────────┐                      ┌────────────────┐
  │ChecklistManager│                      │ change_history │
  │ (检查清单管理器) │                      │    .json       │
  └────────────────┘                      └────────────────┘
           │
           ▼
  ┌────────────────┐
  │ function_graph │
  │    .json       │
  └────────────────┘
```

### 2.4 数据文件

| 文件 | 路径 | 用途 |
|------|------|------|
| 功能神经网络 | `docs/function_graph.json` | 存储节点（函数/模块）和边（调用/依赖关系） |
| 变更历史 | `docs/change_history.json` | 存储所有变更记录，按时间倒序 |
| 配置文件 | `config.py` | 存储应用版本号（APP_VERSION） |
| 图谱备份 | `docs/backups/` | NeuralUpdater更新前的自动备份 |
| 工作流日志 | `logs/superpower_workflow.log` | 工作流执行日志 |

---

## 3. 快速开始

### 3.1 环境要求

- Python 3.8+
- 项目目录结构中存在`backend/docs/function_graph.json`
- 项目目录结构中存在`backend/config.py`（包含`APP_VERSION`配置）

### 3.2 最简使用方式

```python
from superpower_workflow import run_superpower_workflow

# 执行完整工作流（一行代码）
result = run_superpower_workflow(
    change_id="CHG-20260907-001",
    title="优化基金计算性能",
    description="重构基金净值计算逻辑，提升计算速度",
    core_nodes=["fund_calc"],
    modified_files=["modules/fund/service.py"],
    auto_confirm=False,  # 神经网络更新需要人工确认
)

print(f"状态: {result['status']}")
print(f"执行阶段: {result['phases_executed']}")
print(f"跳过阶段: {result['phases_skipped']}")
```

### 3.3 分阶段使用方式

```python
from superpower_workflow import SuperpowerWorkflow

# 1. 创建工作流实例
wf = SuperpowerWorkflow(config={"max_depth": 3, "risk_threshold": "MEDIUM"})

# 2. 触发（智能判断是否需要完整分析）
need_full = wf.trigger(
    change_id="CHG-20260907-002",
    title="修改网关分析器",
    description="优化BFS算法",
    core_nodes=["change_gateway"],
    modified_files=["modules/superpower/gateway_analyzer.py"],
)

if need_full:
    # 3. 分析（复杂修改执行网关分析）
    analysis = wf.analyze()
    print(f"正向影响: {len(analysis.forward_affected)}节点")
    print(f"风险点: {len(analysis.risks)}个")

# 4. 修改（记录代码修改操作）
wf.modify([
    {"file": "modules/superpower/gateway_analyzer.py", "action": "edit", "description": "优化BFS"}
])

# 5. 验证（外部执行测试后标记检查项）
for item in wf.context["checklist"]:
    item.verified = True
    item.status = "completed"
passed, failed = wf.verify()

# 6. 更新神经网络（半自动模式）
if passed:
    wf.update_neural_graph(auto_confirm=False)

# 7. 记录变更（升级版本号）
# wf.record_change()  # 注意：此方法会修改真实文件
```

### 3.4 运行测试

```bash
cd backend
python -m pytest tests/test_gateway_analyzer.py tests/test_neural_updater.py \
    tests/test_checklist_manager.py tests/test_change_recorder.py \
    tests/test_superpower_workflow.py -v
```

---

## 4. 模块详解

### 4.1 GatewayAnalyzer（网关分析器）

**文件位置**：`backend/modules/superpower/gateway_analyzer.py`

**功能**：基于function_graph.json神经网络的变更影响传播分析。

#### 类定义

```python
class GatewayAnalyzer:
    def __init__(
        self,
        graph_path: str = "docs/function_graph.json",
        max_depth: int = 3,
        risk_threshold: str = "MEDIUM",
    ) -> None
```

#### 核心方法

| 方法 | 签名 | 说明 |
|------|------|------|
| `analyze` | `analyze(change_id, core_nodes, change_title="") -> GatewayAnalysisResult` | 执行完整网关分析 |
| `generate_report` | `generate_report(result) -> str` | 生成结构化文本报告 |
| `to_dict` | `to_dict(result) -> Dict` | 序列化为可JSON序列化的字典 |

#### 数据结构

**RiskItem（风险点）**

```python
@dataclass
class RiskItem:
    id: str           # 风险点ID，格式 RISK-{序号}
    level: str        # HIGH / MEDIUM / LOW
    description: str  # 风险描述
    impact: str       # 风险影响说明
    mitigation: str   # 缓解措施建议
```

**ChecklistItem（检查项）**

```python
@dataclass
class ChecklistItem:
    id: str           # 检查项ID，格式 CHK-{change_id}-{序号}
    stage: str        # modify / verify / update / record
    description: str  # 检查项描述
    priority: str     # must / should / optional
    status: str       # pending / in_progress / completed / skipped
    verified: bool    # 是否已验证通过
```

**GatewayAnalysisResult（分析结果）**

```python
@dataclass
class GatewayAnalysisResult:
    change_id: str              # 变更编号
    core_nodes: List[str]       # 核心节点列表
    forward_affected: Set[str]  # 正向影响节点（下游）
    backward_affected: Set[str] # 反向影响节点（上游）
    forward_depth: Dict[str, int]    # 正向传播深度映射
    backward_depth: Dict[str, int]   # 反向传播深度映射
    risks: List[RiskItem]       # 风险点列表
    checklist: List[ChecklistItem]  # 检查清单列表
    stats: Dict[str, Any]       # 统计数据
    generated_at: str           # 生成时间（ISO格式）
```

#### 风险识别规则

| 规则 | 触发条件 | 风险等级 |
|------|----------|----------|
| 影响范围过大 | 影响节点 > 20 | HIGH |
| 影响范围中等 | 影响节点 > 10 | MEDIUM |
| 核心业务层 | business层节点 > 3 | HIGH |
| 神经网络本身 | 核心节点包含function_graph/change_gateway | HIGH |
| 数据库操作 | 影响范围内包含数据库相关节点 | MEDIUM |
| API接口 | 影响范围内包含API相关节点 | MEDIUM |
| 深度传播过远 | 最大深度 >= 3 且影响节点 > 10 | MEDIUM |
| 双向影响 | 正向和反向都 > 5节点 | MEDIUM |

#### 使用示例

```python
from modules.superpower.gateway_analyzer import GatewayAnalyzer

# 初始化
analyzer = GatewayAnalyzer(
    graph_path="docs/function_graph.json",
    max_depth=3,
    risk_threshold="MEDIUM",
)

# 执行分析
result = analyzer.analyze(
    change_id="CHG-20260907-001",
    core_nodes=["change_gateway"],
    change_title="优化网关分析器",
)

# 查看结果
print(f"正向影响: {len(result.forward_affected)}节点")
print(f"反向影响: {len(result.backward_affected)}节点")
print(f"风险点: {len(result.risks)}个")
print(f"检查项: {len(result.checklist)}个")

# 生成报告
report = analyzer.generate_report(result)
print(report)

# 序列化
data = analyzer.to_dict(result)
import json
json.dumps(data, ensure_ascii=False, indent=2)
```

---

### 4.2 NeuralUpdater（神经网络更新器）

**文件位置**：`backend/modules/superpower/neural_updater.py`

**功能**：自动解析Python代码结构，检测函数定义和调用关系，半自动更新function_graph.json。

#### 类定义

```python
class NeuralUpdater:
    def __init__(
        self,
        graph_path: str = "docs/function_graph.json",
        code_root: str = "modules",
        backup_dir: str = "docs/backups",
        auto_confirm: bool = False,
    ) -> None
```

#### 核心方法

| 方法 | 签名 | 说明 |
|------|------|------|
| `parse_file` | `parse_file(file_path) -> ParseResult` | 解析单个Python文件 |
| `parse_directory` | `parse_directory(dir_path, pattern="*.py") -> ParseResult` | 递归解析目录 |
| `diff_with_graph` | `diff_with_graph(parse_result) -> UpdatePreview` | 与现有图谱做差异分析 |
| `backup_graph` | `backup_graph() -> str` | 备份当前图谱，返回备份路径 |
| `apply_update` | `apply_update(preview) -> UpdateResult` | 应用更新预览（会修改文件） |
| `validate_update` | `validate_update(preview) -> Tuple[bool, List[str]]` | 验证更新合理性 |
| `rollback` | `rollback(backup_path) -> bool` | 从备份回滚 |
| `run` | `run(modified_files=None) -> UpdateResult` | 完整更新流程 |
| `get_graph_stats` | `get_graph_stats() -> Dict` | 返回图谱统计信息 |

#### 数据结构

**FunctionInfo（函数信息）**

```python
@dataclass
class FunctionInfo:
    name: str              # 函数名
    qualified_name: str    # 限定名（模块.类.函数）
    file_path: str         # 所在文件路径
    line_start: int        # 起始行号
    line_end: int          # 结束行号
    docstring: str         # 文档字符串
    decorators: List[str]  # 装饰器列表
    is_method: bool        # 是否为类方法
    is_async: bool         # 是否为异步函数
    params: List[str]      # 参数名列表
```

**UpdatePreview（更新预览）**

```python
@dataclass
class UpdatePreview:
    added_nodes: List[Dict]      # 新增节点
    removed_nodes: List[str]     # 删除节点ID
    modified_nodes: List[Dict]   # 修改节点
    added_edges: List[Dict]      # 新增边
    removed_edges: List[Dict]    # 删除边
    node_count_before: int       # 更新前节点数
    node_count_after: int        # 更新后节点数
    edge_count_before: int       # 更新前边数
    edge_count_after: int        # 更新后边数
    warnings: List[str]          # 警告信息
```

**UpdateResult（更新结果）**

```python
@dataclass
class UpdateResult:
    success: bool               # 是否成功
    preview: Optional[UpdatePreview]  # 关联的更新预览
    backup_path: str            # 备份文件路径
    error: Optional[str]        # 错误信息
    rollback_performed: bool    # 是否执行了回滚
```

#### 验证规则

- 节点数变化不超过50%
- 边数变化不超过50%
- 没有重复节点ID
- 所有边的source/target都存在于节点中
- 节点数/边数计算一致

#### 使用示例

```python
from modules.superpower.neural_updater import NeuralUpdater

# 初始化（auto_confirm=False为半自动模式）
updater = NeuralUpdater(
    graph_path="docs/function_graph.json",
    code_root="modules",
    backup_dir="docs/backups",
    auto_confirm=False,
)

# 方式1：解析单个文件
parse_result = updater.parse_file("modules/superpower/gateway_analyzer.py")
print(f"解析到 {len(parse_result.functions)} 个函数")
print(f"解析到 {len(parse_result.calls)} 个调用关系")

# 方式2：解析整个目录
parse_result = updater.parse_directory("modules/superpower")

# 差异分析
preview = updater.diff_with_graph(parse_result)
print(f"新增节点: {len(preview.added_nodes)}")
print(f"删除节点: {len(preview.removed_nodes)}")
print(f"新增边: {len(preview.added_edges)}")

# 验证更新合理性
is_valid, errors = updater.validate_update(preview)
if not is_valid:
    print(f"验证失败: {errors}")

# 半自动模式：run返回预览，不执行更新
result = updater.run(modified_files=["modules/fund/service.py"])
if not result.success and result.preview:
    # 人工审核预览后确认执行
    # updater.apply_update(result.preview)  # 注意：此方法会修改真实文件
    pass

# 查看图谱统计
stats = updater.get_graph_stats()
print(f"节点数: {stats['node_count']}")
print(f"边数: {stats['edge_count']}")
```

---

### 4.3 ChecklistManager（检查清单管理器）

**文件位置**：`backend/modules/superpower/checklist_manager.py`

**功能**：生成、跟踪、验证变更检查清单，支持从网关分析结果自动生成。

#### 类定义

```python
class ChecklistManager:
    def __init__(self, items: Optional[List[ChecklistItem]] = None) -> None
```

#### 核心方法

| 方法 | 签名 | 说明 |
|------|------|------|
| `generate_from_analysis` | `generate_from_analysis(analysis_result: Dict) -> List[ChecklistItem]` | 从分析结果生成检查清单 |
| `add_item` | `add_item(stage, description, priority="should") -> ChecklistItem` | 添加检查项 |
| `remove_item` | `remove_item(item_id) -> bool` | 删除检查项 |
| `update_item` | `update_item(item_id, **kwargs) -> Optional[ChecklistItem]` | 修改检查项 |
| `start_item` | `start_item(item_id) -> bool` | 标记为进行中 |
| `complete_item` | `complete_item(item_id, notes=None) -> bool` | 标记为已完成 |
| `skip_item` | `skip_item(item_id, reason) -> bool` | 标记为已跳过（must项返回False） |
| `verify_item` | `verify_item(item_id, passed, notes=None) -> bool` | 验证检查项 |
| `verify_all` | `verify_all() -> Tuple[bool, List[str]]` | 验证所有must项 |
| `get_incomplete_must` | `get_incomplete_must() -> List[ChecklistItem]` | 获取未完成的must项 |
| `is_ready_for_verification` | `is_ready_for_verification() -> bool` | 判断是否可进入验证阶段 |
| `generate_report` | `generate_report() -> ChecklistReport` | 生成统计报告 |
| `generate_text_report` | `generate_text_report() -> str` | 生成文本报告 |
| `to_dict` | `to_dict() -> Dict` | 序列化 |
| `from_dict` | `from_dict(data) -> ChecklistManager` | 反序列化（classmethod） |

#### 数据结构

**ChecklistItem（检查项）**

```python
@dataclass
class ChecklistItem:
    id: str              # 唯一标识，格式 CL-{阶段缩写}-{序号}
    stage: str           # 所属阶段（分析阶段/修改阶段/验证阶段/更新阶段/记录阶段）
    description: str     # 检查项描述
    priority: str        # must / should / optional
    status: str          # pending / in_progress / completed / skipped
    verified: bool       # 是否已验证通过
    verified_at: Optional[str]  # 验证时间
    notes: Optional[str]        # 备注
    created_at: str      # 创建时间
    updated_at: str      # 最后更新时间
```

**ChecklistReport（统计报告）**

```python
@dataclass
class ChecklistReport:
    total: int                    # 检查项总数
    completed: int                # 已完成数
    pending: int                  # 待处理数
    in_progress: int              # 进行中数
    skipped: int                  # 已跳过数
    verified: int                 # 已验证通过数
    must_completed: int           # must优先级已完成数
    must_total: int               # must优先级总数
    coverage_rate: float          # 整体完成率（0.0~1.0）
    by_stage: Dict[str, Dict]     # 按阶段统计
    incomplete_must: List[str]    # 未完成的must项id列表
```

#### 使用示例

```python
from modules.superpower.checklist_manager import ChecklistManager

# 初始化
manager = ChecklistManager()

# 从网关分析结果生成检查清单
analysis_result = {
    "forward_affected": ["node_a", "node_b"],
    "backward_affected": ["node_c"],
    "risks": [
        {"level": "HIGH", "description": "影响核心业务"},
        {"level": "MEDIUM", "description": "影响API接口"},
    ],
}
items = manager.generate_from_analysis(analysis_result)
print(f"生成 {len(items)} 个检查项")

# 手动添加检查项
item = manager.add_item("验证阶段", "执行端到端测试", "must")

# 状态流转
manager.start_item(item.id)           # pending -> in_progress
manager.complete_item(item.id, "测试通过")  # in_progress -> completed

# 跳过（must项不允许跳过）
result = manager.skip_item(item.id, "不需要")  # 返回False（must项）

# 验证
manager.verify_item(item.id, passed=True, notes="所有测试通过")

# 批量验证
all_pass, failed = manager.verify_all()
if not all_pass:
    print(f"未通过项: {failed}")

# 就绪判断
if manager.is_ready_for_verification():
    print("所有must项已完成，可以进入验证阶段")

# 生成报告
report = manager.generate_report()
print(f"完成率: {report.coverage_rate:.1%}")
print(f"Must项: {report.must_completed}/{report.must_total}")

# 文本报告
text = manager.generate_text_report()
print(text)

# 序列化/反序列化
data = manager.to_dict()
restored = ChecklistManager.from_dict(data)
```

---

### 4.4 ChangeRecorder（变更记录器）

**文件位置**：`backend/modules/superpower/change_recorder.py`

**功能**：管理应用版本号和变更历史记录，支持语义化版本升级、变更ID生成、记录验证和持久化。

#### 类定义

```python
class ChangeRecorder:
    def __init__(
        self,
        history_path: str = "docs/change_history.json",
        config_path: str = "config.py",
    ) -> None
```

#### 核心方法

| 方法 | 签名 | 说明 |
|------|------|------|
| `get_current_version` | `get_current_version() -> str` | 读取当前版本号 |
| `bump_version` | `bump_version(level="patch") -> str` | 升级版本号 |
| `generate_change_id` | `generate_change_id() -> str` | 生成变更ID |
| `create_record` | `create_record(title, description, ...) -> ChangeRecord` | 创建变更记录 |
| `validate_record` | `validate_record(record) -> Tuple[bool, List[str]]` | 验证记录完整性 |
| `load_history` | `load_history() -> Dict` | 加载变更历史 |
| `save_history` | `save_history(history) -> None` | 保存变更历史 |
| `append_record` | `append_record(record) -> RecordResult` | 追加记录到历史（会修改文件） |
| `update_config_version` | `update_config_version(new_version) -> bool` | 更新config.py版本号（会修改文件） |
| `record` | `record(title, description, ...) -> RecordResult` | 完整记录流程（会修改文件） |
| `get_recent_changes` | `get_recent_changes(count=5) -> List[Dict]` | 获取最近N条变更 |
| `get_change_by_id` | `get_change_by_id(change_id) -> Optional[Dict]` | 按ID查询变更 |
| `get_stats` | `get_stats() -> Dict` | 获取变更统计 |

#### 数据结构

**ChangeRecord（变更记录）**

```python
@dataclass
class ChangeRecord:
    id: str                    # 变更ID，格式 CHG-YYYYMMDD-NNN
    version: str               # 关联版本号
    date: str                  # 变更日期（YYYY-MM-DD）
    title: str                 # 变更标题
    description: str           # 变更详细描述
    category: str              # 分类（feature/bugfix/refactor等）
    priority: str              # 优先级（high/medium/low）
    status: str                # 状态（completed/in_progress/planned）
    affected_modules: List[str]     # 受影响模块
    affected_files: List[str]       # 受影响文件
    affected_nodes: List[str]       # 受影响神经网络节点
    changes: List[Dict]             # 具体变更列表
    testing: Dict                   # 测试信息
    impact: Dict                    # 影响分析
    verification: List[str]         # 验证步骤
    risk_assessment: str            # 风险评估
    risk_level: str                 # 风险等级
    gateway_passed: bool            # 是否通过网关检查
    tested: bool                    # 是否经过测试
```

**RecordResult（操作结果）**

```python
@dataclass
class RecordResult:
    success: bool          # 是否成功
    change_id: str         # 变更ID
    new_version: str       # 新版本号
    record_path: str       # 记录文件路径
    error: Optional[str]   # 错误信息
```

#### 版本号规则

- **patch**：2.9.54 -> 2.9.55（小修复、小功能）
- **minor**：2.9.54 -> 2.10.0（新功能、接口变更）
- **major**：2.9.54 -> 3.0.0（重大重构、不兼容变更）

#### 使用示例

```python
from modules.superpower.change_recorder import ChangeRecorder

# 初始化
recorder = ChangeRecorder(
    history_path="docs/change_history.json",
    config_path="config.py",
)

# 读取当前版本
version = recorder.get_current_version()
print(f"当前版本: {version}")  # 如 "2.9.54"

# 升级版本号
new_version = recorder.bump_version("patch")
print(f"新版本: {new_version}")  # 如 "2.9.55"

# 生成变更ID
change_id = recorder.generate_change_id()
print(f"变更ID: {change_id}")  # 如 "CHG-20260907-046"

# 创建变更记录（不写入文件）
record = recorder.create_record(
    title="优化基金计算性能",
    description="重构基金净值计算逻辑，使用向量化运算提升速度",
    category="feature",
    priority="high",
    affected_files=["modules/fund/service.py"],
    affected_nodes=["fund_calc", "nav_fetch"],
    changes=[
        {"file": "modules/fund/service.py", "type": "modify", "description": "重构calc_nav方法"},
    ],
)

# 验证记录
valid, errors = recorder.validate_record(record)
if valid:
    print("记录验证通过")
else:
    print(f"验证失败: {errors}")

# 查询变更历史
recent = recorder.get_recent_changes(count=5)
for change in recent:
    print(f"{change['id']}: {change['title']}")

# 按ID查询
change = recorder.get_change_by_id("CHG-20260907-045")
if change:
    print(f"找到变更: {change['title']}")

# 统计信息
stats = recorder.get_stats()
print(f"总变更数: {stats['total']}")
print(f"按分类: {stats['by_category']}")

# 注意：以下方法会修改真实文件，使用时需谨慎
# recorder.append_record(record)           # 追加记录到change_history.json
# recorder.update_config_version("2.9.55") # 更新config.py版本号
# recorder.record(title="...", description="...")  # 完整流程（升级版本+追加记录）
```

---

### 4.5 SuperpowerWorkflow（工作流引擎主类）

**文件位置**：`backend/superpower_workflow.py`

**功能**：整合变更工作流的六个阶段，提供状态机驱动的自动化闭环。

#### 类定义

```python
class SuperpowerWorkflow:
    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None
```

#### 阶段常量

```python
PHASE_TRIGGER = "TRIGGER"      # 触发阶段
PHASE_ANALYZE = "ANALYZE"      # 分析阶段
PHASE_MODIFY = "MODIFY"        # 修改阶段
PHASE_VERIFY = "VERIFY"        # 验证阶段
PHASE_UPDATE = "UPDATE"        # 更新阶段
PHASE_RECORD = "RECORD"        # 记录阶段
PHASE_COMPLETED = "COMPLETED"  # 完成
PHASE_FAILED = "FAILED"        # 失败
```

#### 核心方法

| 方法 | 签名 | 说明 |
|------|------|------|
| `trigger` | `trigger(change_id, title, description, core_nodes, modified_files) -> bool` | 触发工作流，返回是否需要完整分析 |
| `analyze` | `analyze() -> Optional[GatewayAnalysisResult]` | 执行网关分析 |
| `modify` | `modify(modifications) -> bool` | 记录修改操作 |
| `verify` | `verify() -> Tuple[bool, List[str]]` | 验证检查清单 |
| `update_neural_graph` | `update_neural_graph(auto_confirm=False) -> bool` | 更新神经网络 |
| `record_change` | `record_change(extra_info=None) -> bool` | 记录变更（会修改文件） |
| `run` | `run(change_id, ..., auto_confirm=False) -> Dict` | 完整工作流（会修改文件） |
| `get_progress` | `get_progress() -> Dict` | 获取进度信息 |

#### 智能触发规则

简单修改自动跳过网关分析的判断条件（**全部满足**）：
1. 修改文件数 <= `simple_change_max_files`（默认1）
2. 修改代码行数 <= `simple_change_max_lines`（默认20）
3. 不涉及核心模块（nav/anti/rank/score/portfolio/fund/market/data/api/services/repository等）

#### 配置参数

```python
config = {
    "graph_path": "docs/function_graph.json",     # 图谱路径
    "max_depth": 3,                                 # BFS最大深度
    "risk_threshold": "MEDIUM",                     # 风险报告阈值
    "simple_change_max_files": 1,                   # 简单修改最大文件数
    "simple_change_max_lines": 20,                  # 简单修改最大代码行数
    "auto_confirm_update": False,                    # 是否自动确认神经网络更新
}
```

#### 使用示例

```python
from superpower_workflow import SuperpowerWorkflow, run_superpower_workflow

# ===== 方式1：完整工作流（一行代码）=====
result = run_superpower_workflow(
    change_id="CHG-20260907-001",
    title="优化基金计算",
    description="重构净值计算逻辑",
    core_nodes=["fund_calc"],
    modified_files=["modules/fund/service.py"],
    auto_confirm=False,
)
print(f"状态: {result['status']}")

# ===== 方式2：分阶段控制 =====
wf = SuperpowerWorkflow(config={"max_depth": 2})

# 触发
need_full = wf.trigger(
    change_id="CHG-20260907-002",
    title="修改网关",
    description="优化BFS",
    core_nodes=["change_gateway"],
    modified_files=["modules/superpower/gateway_analyzer.py"],
)

if need_full:
    analysis = wf.analyze()
    print(f"风险: {len(analysis.risks)}个")

# 修改
wf.modify([{"file": "test.py", "action": "edit", "description": "优化"}])

# 验证（外部执行测试后标记）
for item in wf.context["checklist"]:
    item.verified = True
    item.status = "completed"
passed, failed = wf.verify()

# 更新神经网络
if passed:
    wf.update_neural_graph(auto_confirm=False)

# 查看进度
progress = wf.get_progress()
print(f"完成度: {progress['completion_percent']}%")
print(f"当前阶段: {progress['current_phase']}")

# 注意：record_change()和完整run()会修改真实文件
# wf.record_change()
```

---

## 5. 配置说明

### 5.1 pytest配置

项目根目录（backend/）下的`pytest.ini`配置：

```ini
[pytest]
testpaths = tests
python_files = test_*.py
python_classes = Test*
python_functions = test_*

markers =
    unit: 单元测试 - 单个函数/模块验证
    integration: 集成测试 - 模块间交互验证
    smoke: 冒烟测试 - 核心功能快速验证
    slow: 慢速测试 - 需要网络/较长运行时间

filterwarnings =
    ignore::DeprecationWarning
    ignore::PendingDeprecationWarning

addopts =
    -v
    --tb=short
    --strict-markers
    -m "not slow"
```

### 5.2 运行测试

```bash
# 运行所有Superpower相关测试
cd backend
python -m pytest tests/test_gateway_analyzer.py tests/test_neural_updater.py \
    tests/test_checklist_manager.py tests/test_change_recorder.py \
    tests/test_superpower_workflow.py -v

# 只运行单元测试
python -m pytest tests/ -m unit -v

# 只运行集成测试
python -m pytest tests/ -m integration -v

# 运行单个测试文件
python -m pytest tests/test_gateway_analyzer.py -v

# 运行单个测试类
python -m pytest tests/test_gateway_analyzer.py::TestGatewayAnalyzerInit -v

# 运行单个测试方法
python -m pytest tests/test_gateway_analyzer.py::TestGatewayAnalyzerInit::test_init_loads_graph_successfully -v
```

### 5.3 数据文件配置

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `graph_path` | `docs/function_graph.json` | 功能神经网络文件路径 |
| `history_path` | `docs/change_history.json` | 变更历史文件路径 |
| `config_path` | `config.py` | 应用配置文件路径（含版本号） |
| `backup_dir` | `docs/backups` | 图谱备份目录 |
| `code_root` | `modules` | 代码根目录（NeuralUpdater解析范围） |

---

## 6. 使用示例

### 6.1 场景一：核心模块修改前的影响评估

```python
from modules.superpower.gateway_analyzer import GatewayAnalyzer

# 场景：准备修改基金计算模块，先评估影响范围
analyzer = GatewayAnalyzer(graph_path="docs/function_graph.json", max_depth=3)

result = analyzer.analyze(
    change_id="CHG-20260907-100",
    core_nodes=["fund_calc", "nav_fetch"],
    change_title="重构基金计算模块",
)

# 输出影响范围
print(f"正向影响（下游）: {len(result.forward_affected)} 个节点")
print(f"反向影响（上游）: {len(result.backward_affected)} 个节点")
print(f"总影响节点: {result.stats['total_affected_count']} 个")
print(f"涉及代码文件: {result.stats['affected_file_count']} 个")

# 输出高风险点
high_risks = [r for r in result.risks if r.level == "HIGH"]
print(f"\n高风险点 ({len(high_risks)}个):")
for risk in high_risks:
    print(f"  - {risk.description}")
    print(f"    缓解: {risk.mitigation[:80]}...")

# 输出must检查项
must_items = [c for c in result.checklist if c.priority == "must"]
print(f"\n必须完成的检查项 ({len(must_items)}个):")
for item in must_items:
    print(f"  [{item.stage}] {item.description}")

# 生成完整报告
report = analyzer.generate_report(result)
with open("analysis_report.txt", "w", encoding="utf-8") as f:
    f.write(report)
```

### 6.2 场景二：代码修改后的神经网络更新

```python
from modules.superpower.neural_updater import NeuralUpdater

# 场景：修改了多个文件后，更新function_graph.json
updater = NeuralUpdater(
    graph_path="docs/function_graph.json",
    code_root="modules",
    backup_dir="docs/backups",
    auto_confirm=False,  # 半自动模式
)

# 解析修改的文件
modified_files = [
    "modules/fund/service.py",
    "modules/fund/models.py",
]

# 执行更新流程（返回预览，不修改文件）
result = updater.run(modified_files=modified_files)

if result.preview:
    preview = result.preview
    print(f"新增节点: {len(preview.added_nodes)}")
    for node in preview.added_nodes[:5]:
        print(f"  + {node['id']}: {node.get('name', '')}")

    print(f"\n删除节点: {len(preview.removed_nodes)}")
    for nid in preview.removed_nodes[:5]:
        print(f"  - {nid}")

    print(f"\n修改节点: {len(preview.modified_nodes)}")
    print(f"新增边: {len(preview.added_edges)}")
    print(f"删除边: {len(preview.removed_edges)}")

    # 验证更新合理性
    is_valid, errors = updater.validate_update(preview)
    if is_valid:
        print("\n验证通过，可以执行更新")
        # 人工确认后执行（注意：会修改真实文件）
        # apply_result = updater.apply_update(preview)
        # if apply_result.success:
        #     print(f"更新成功，备份在: {apply_result.backup_path}")
        # else:
        #     print(f"更新失败: {apply_result.error}")
    else:
        print(f"\n验证失败: {errors}")

# 查看图谱统计
stats = updater.get_graph_stats()
print(f"\n图谱统计: {stats['node_count']}节点, {stats['edge_count']}边")
```

### 6.3 场景三：检查清单跟踪与验证

```python
from modules.superpower.checklist_manager import ChecklistManager

# 场景：变更执行过程中跟踪检查清单
manager = ChecklistManager()

# 从分析结果生成
analysis = {
    "forward_affected": ["node_a", "node_b", "node_c"],
    "backward_affected": ["node_d"],
    "risks": [
        {"level": "HIGH", "description": "影响核心计算"},
        {"level": "MEDIUM", "description": "影响API接口"},
    ],
}
items = manager.generate_from_analysis(analysis)

# 模拟执行过程
print("=== 开始执行检查清单 ===\n")

for item in items:
    # 开始执行
    manager.start_item(item.id)
    print(f"[进行中] {item.id}: {item.description}")

    # 模拟执行结果
    if item.priority == "must":
        # must项必须完成并验证
        manager.complete_item(item.id, notes="执行完成")
        manager.verify_item(item.id, passed=True, notes="验证通过")
        print(f"  [完成+验证通过]")
    elif item.priority == "should":
        # should项完成
        manager.complete_item(item.id, notes="执行完成")
        print(f"  [完成]")
    else:
        # optional项可以跳过
        manager.skip_item(item.id, "本次不执行")
        print(f"  [跳过]")

# 检查就绪状态
if manager.is_ready_for_verification():
    print("\n=== 所有must项已完成 ===")
else:
    incomplete = manager.get_incomplete_must()
    print(f"\n=== 还有 {len(incomplete)} 个must项未完成 ===")
    for item in incomplete:
        print(f"  - [{item.status}] {item.id}: {item.description}")

# 生成报告
report = manager.generate_report()
print(f"\n=== 完成率: {report.coverage_rate:.1%} ===")
print(f"Must项: {report.must_completed}/{report.must_total}")

# 文本报告
print(manager.generate_text_report())
```

### 6.4 场景四：完整工作流执行

```python
from superpower_workflow import SuperpowerWorkflow

# 场景：执行一次完整的变更工作流
wf = SuperpowerWorkflow(config={
    "max_depth": 3,
    "risk_threshold": "MEDIUM",
})

# 1. 触发
need_full = wf.trigger(
    change_id="CHG-20260907-200",
    title="新增基金对比功能",
    description="新增多基金对比分析页面和API",
    core_nodes=["compare", "rank_api"],
    modified_files=[
        "modules/rank/service.py",
        "api/compare.py",
    ],
)
print(f"需要完整分析: {need_full}")

# 2. 分析
if need_full:
    analysis = wf.analyze()
    print(f"分析完成: 正向{len(analysis.forward_affected)}节点, "
          f"风险{len(analysis.risks)}个, 检查项{len(analysis.checklist)}个")

# 3. 修改
wf.modify([
    {"file": "modules/rank/service.py", "action": "edit", "description": "新增对比计算"},
    {"file": "api/compare.py", "action": "create", "description": "新增对比API"},
])

# 4. 验证（实际项目中由CI/CD执行测试后标记）
for item in wf.context["checklist"]:
    item.verified = True
    item.status = "completed"
passed, failed = wf.verify()
print(f"验证通过: {passed}, 未通过项: {failed}")

# 5. 更新神经网络（半自动）
if passed:
    wf.update_neural_graph(auto_confirm=False)
    print("神经网络更新预览已生成，等待人工确认")

# 6. 查看进度
progress = wf.get_progress()
print(f"\n工作流进度: {progress['completion_percent']}%")
print(f"当前阶段: {progress['current_phase']}")
print(f"已执行阶段: {progress['phases_executed']}")
print(f"跳过阶段: {progress['phases_skipped']}")

# 注意：最后一步record_change()会修改真实文件
# wf.record_change()
```

---

## 7. 扩展指南

### 7.1 扩展风险识别规则

在`gateway_analyzer.py`的`_identify_risks`方法中添加新规则：

```python
def _identify_risks(self, core_nodes, forward, backward, stats):
    risks = []
    # ... 现有规则 ...

    # 新增规则：涉及安全模块
    security_nodes = [n for n in forward | backward if "security" in n.lower()]
    if security_nodes:
        risks.append(RiskItem(
            id=f"RISK-{len(risks)+1:03d}",
            level="HIGH",
            description=f"变更影响安全模块: {security_nodes[:3]}",
            impact="安全模块变更可能导致权限绕过或数据泄露",
            mitigation="1. 执行安全审计; 2. 验证权限控制; 3. 渗透测试",
        ))

    # 按等级排序并过滤
    risks.sort(key=lambda r: self._RISK_LEVEL_ORDER.get(r.level, 0), reverse=True)
    return risks
```

### 7.2 扩展检查清单生成规则

在`gateway_analyzer.py`的`_generate_checklist`方法中添加新检查项：

```python
def _generate_checklist(self, change_id, core_nodes, risks, stats):
    checklist = []
    # ... 现有检查项 ...

    # 新增：安全检查
    if any("security" in n.lower() for n in core_nodes):
        checklist.append(ChecklistItem(
            id=f"CHK-{change_id}-{len(checklist)+1:03d}",
            stage="verify",
            description="执行安全审计和权限验证",
            priority="must",
            status="pending",
            verified=False,
        ))

    return checklist
```

### 7.3 扩展NeuralUpdater层级映射

在`neural_updater.py`的`_LAYER_MAP`字典中添加新目录映射：

```python
_LAYER_MAP = {
    # 现有映射...
    "ai": "business",        # 新增AI模块映射到业务层
    "integration": "system",  # 新增集成模块映射到系统层
}
```

### 7.4 自定义工作流阶段

继承`SuperpowerWorkflow`类，重写阶段方法：

```python
class CustomWorkflow(SuperpowerWorkflow):
    def __init__(self, config=None):
        super().__init__(config)
        self.custom_phase_result = None

    def modify(self, modifications):
        """重写modify阶段，添加自定义逻辑"""
        # 自定义前置检查
        for mod in modifications:
            if not mod.get("file"):
                raise ValueError("修改操作必须包含file字段")

        # 调用父类方法
        result = super().modify(modifications)

        # 自定义后置处理
        self.custom_phase_result = {"modified_count": len(modifications)}
        return result
```

### 7.5 添加新的测试

在`backend/tests/`目录下创建新的测试文件：

```python
"""
自定义模块单元测试模板
"""
import os
import sys
import pytest

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

# 导入待测试模块
# from modules.superpower.your_module import YourClass


class TestYourClass:
    """测试类命名规则：Test*"""

    @pytest.mark.unit
    def test_your_functionality(self):
        """测试函数命名规则：test_*，必须有docstring"""
        # 测试逻辑
        assert True
```

---

## 8. 常见问题

### Q1: 运行测试时提示`ModuleNotFoundError: No module named 'modules'`

**A**: 确保在`backend/`目录下运行测试：
```bash
cd backend
python -m pytest tests/ -v
```

测试文件中已自动将backend目录添加到`sys.path`，但工作目录必须正确。

### Q2: GatewayAnalyzer初始化时提示`FileNotFoundError: 神经网络文件不存在`

**A**: 确认`docs/function_graph.json`文件存在。如果使用自定义路径，确保路径相对于工作目录（backend/）正确：
```python
analyzer = GatewayAnalyzer(graph_path="docs/function_graph.json")  # 相对backend/
# 或使用绝对路径
analyzer = GatewayAnalyzer(graph_path="/absolute/path/to/function_graph.json")
```

### Q3: ChangeRecorder的`get_current_version()`返回`"0.0.0"`

**A**: 确认`config.py`中包含正确格式的版本号配置：
```python
APP_VERSION = _env_str("APP_VERSION", "2.9.54")
```
ChangeRecorder通过正则匹配`_env_str("APP_VERSION", "x.x.x")`格式读取版本号。

### Q4: NeuralUpdater的`run()`方法返回`success=False`但没有报错

**A**: 这是正常行为。当`auto_confirm=False`（默认）时，`run()`方法执行解析和差异分析后返回预览，不执行实际更新。`success=False`表示"需要人工确认"，`result.preview`中包含更新预览。人工审核后调用`apply_update(preview)`执行更新。

### Q5: 工作流的`trigger()`对简单修改的判断标准是什么？

**A**: 简单修改需要**同时满足**以下三个条件：
1. 修改文件数 <= 1（可通过`simple_change_max_files`配置）
2. 修改代码行数 <= 20（可通过`simple_change_max_lines`配置）
3. 不涉及核心模块（nav/anti/rank/score/portfolio/fund/market/data/api/services/repository/db.py/config.py/main.py等）

### Q6: 如何安全地测试会修改文件的方法？

**A**: 使用`tmp_path` fixture创建临时文件，避免修改真实数据：
```python
def test_append_record(self, tmp_path):
    history_path = str(tmp_path / "change_history.json")
    config_path = str(tmp_path / "config.py")
    with open(config_path, "w") as f:
        f.write('APP_VERSION = _env_str("APP_VERSION", "2.9.54")')
    recorder = ChangeRecorder(history_path=history_path, config_path=config_path)
    record = recorder.create_record(title="test", description="test")
    result = recorder.append_record(record)  # 只修改临时文件
    assert result.success
```

### Q7: 工作流执行到UPDATE阶段后，神经网络没有被更新？

**A**: 检查`auto_confirm`参数。默认`auto_confirm=False`，NeuralUpdater只生成更新预览不执行更新。需要人工确认后调用`updater.apply_update(preview)`执行，或在工作流中设置`auto_confirm=True`：
```python
wf.update_neural_graph(auto_confirm=True)  # 自动确认并执行更新
```

### Q8: 如何回滚神经网络更新？

**A**: NeuralUpdater在每次`apply_update`前自动备份原图谱到`docs/backups/`目录。如果更新失败，会自动回滚。手动回滚：
```python
updater = NeuralUpdater()
backup_path = "docs/backups/function_graph_20260907_120000.json"
success = updater.rollback(backup_path)
```

### Q9: 测试中使用真实的function_graph.json安全吗？

**A**: GatewayAnalyzer的所有操作（analyze、generate_report、to_dict）都是只读的，不会修改图谱文件。NeuralUpdater的`parse_file`、`parse_directory`、`diff_with_graph`、`validate_update`、`get_graph_stats`也是只读的。只有`apply_update`、`backup_graph`、`rollback`会修改文件。测试中使用`auto_confirm=False`的`run()`方法不会修改文件。

### Q10: 如何查看工作流的详细日志？

**A**: 工作流日志保存在`backend/logs/superpower_workflow.log`，包含所有阶段的执行记录：
```bash
tail -f backend/logs/superpower_workflow.log
```
日志同时输出到控制台（INFO级别）和文件（DEBUG级别）。

---

## 附录

### A. 测试文件清单

| 文件 | 测试类数量 | 测试用例数量 | 标记 |
|------|-----------|-------------|------|
| `test_gateway_analyzer.py` | 8 | 40+ | unit |
| `test_neural_updater.py` | 10 | 40+ | unit |
| `test_checklist_manager.py` | 10 | 60+ | unit |
| `test_change_recorder.py` | 9 | 50+ | unit |
| `test_superpower_workflow.py` | 10 | 50+ | integration |

### B. 模块版本信息

- Superpower工作流引擎版本：1.0.0
- 发布日期：2026-09-07
- Python版本要求：3.8+
- 依赖：标准库（json, ast, re, os, logging, dataclasses, datetime, pathlib, typing, collections）

### C. 相关文件路径

```
backend/
├── superpower_workflow.py              # 工作流引擎主类
├── modules/superpower/
│   ├── __init__.py                     # 模块包初始化
│   ├── gateway_analyzer.py             # 网关分析器
│   ├── neural_updater.py               # 神经网络更新器
│   ├── checklist_manager.py            # 检查清单管理器
│   └── change_recorder.py              # 变更记录器
├── tests/
│   ├── __init__.py                     # 测试包初始化
│   ├── test_gateway_analyzer.py        # 网关分析器测试
│   ├── test_neural_updater.py          # 神经网络更新器测试
│   ├── test_checklist_manager.py       # 检查清单管理器测试
│   ├── test_change_recorder.py         # 变更记录器测试
│   └── test_superpower_workflow.py     # 工作流集成测试
├── docs/
│   ├── function_graph.json             # 功能神经网络
│   ├── change_history.json             # 变更历史
│   ├── backups/                        # 图谱备份目录
│   └── superpower_workflow_guide.md    # 本文档
├── logs/
│   └── superpower_workflow.log         # 工作流日志
└── config.py                            # 应用配置（含版本号）
```
