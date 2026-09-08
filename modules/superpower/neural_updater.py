"""
neural_updater.py —— 神经网络自动更新器

自动解析 Python 代码结构，检测函数定义和调用关系，半自动更新 function_graph.json。

核心能力：
- 使用 ast 模块静态解析代码（不执行代码）
- 提取函数/类方法定义、参数、装饰器、docstring
- 提取函数调用关系（直接调用、方法调用、链式调用）
- 与现有 function_graph.json 做差异分析（新增/删除/修改节点和边）
- 生成更新预览，支持人工确认后执行
- 更新前自动备份，失败自动回滚
- 验证更新合理性（节点/边数变化阈值、重复ID、边引用完整性）

工作流程：
    parse_code -> diff_with_graph -> validate_update -> (confirm) -> apply_update

使用方式：
    from modules.superpower.neural_updater import NeuralUpdater

    updater = NeuralUpdater(auto_confirm=False)
    result = updater.run(modified_files=["modules/fund/service.py"])
    if not result.success and result.preview:
        # 人工审核预览后确认执行
        updater.apply_update(result.preview)

版本历史：
    1.0.0 (2026-09-07) - 初始版本，实现代码解析、差异分析、备份回滚、半自动更新
"""
from __future__ import annotations

import ast
import json
import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# 数据结构定义
# ---------------------------------------------------------------------------

@dataclass
class FunctionInfo:
    """函数/方法定义信息。"""

    name: str
    """函数名（如 get_fund_list）"""

    qualified_name: str
    """限定名（模块.类.函数，如 fund.service.FundService.get_fund_list）"""

    file_path: str
    """所在文件相对路径"""

    line_start: int
    """起始行号"""

    line_end: int
    """结束行号"""

    docstring: str
    """文档字符串"""

    decorators: List[str] = field(default_factory=list)
    """装饰器列表（如 ['@staticmethod', '@router.get']）"""

    is_method: bool = False
    """是否为类方法"""

    is_async: bool = False
    """是否为异步函数"""

    params: List[str] = field(default_factory=list)
    """参数名列表（不含 self/cls）"""


@dataclass
class CallRelation:
    """函数调用关系。"""

    caller: str
    """调用者限定名（如 fund.service.FundService.get_fund_list）"""

    callee: str
    """被调用者函数名（如 print、self._query、obj.method）"""

    file_path: str
    """所在文件相对路径"""

    line_no: int
    """调用所在行号"""


@dataclass
class ParseResult:
    """代码解析结果。"""

    functions: List[FunctionInfo] = field(default_factory=list)
    """解析出的函数/方法列表"""

    calls: List[CallRelation] = field(default_factory=list)
    """解析出的调用关系列表"""

    errors: List[str] = field(default_factory=list)
    """解析错误列表"""

    files_scanned: int = 0
    """已扫描文件数"""


@dataclass
class UpdatePreview:
    """更新预览（差异分析结果）。"""

    added_nodes: List[Dict] = field(default_factory=list)
    """新增节点列表"""

    removed_nodes: List[str] = field(default_factory=list)
    """删除节点ID列表"""

    modified_nodes: List[Dict] = field(default_factory=list)
    """修改节点列表（含完整节点信息）"""

    added_edges: List[Dict] = field(default_factory=list)
    """新增边列表"""

    removed_edges: List[Dict] = field(default_factory=list)
    """删除边列表"""

    node_count_before: int = 0
    """更新前节点数"""

    node_count_after: int = 0
    """更新后节点数"""

    edge_count_before: int = 0
    """更新前边数"""

    edge_count_after: int = 0
    """更新后边数"""

    warnings: List[str] = field(default_factory=list)
    """警告信息列表"""


@dataclass
class UpdateResult:
    """更新执行结果。"""

    success: bool
    """是否成功"""

    preview: Optional[UpdatePreview] = None
    """关联的更新预览（auto_confirm=False 时仅返回预览不执行）"""

    backup_path: str = ""
    """备份文件路径"""

    error: Optional[str] = None
    """错误信息（失败时）"""

    rollback_performed: bool = False
    """是否执行了回滚"""


# ---------------------------------------------------------------------------
# 层级推断映射
# ---------------------------------------------------------------------------

# 目录名 -> 层级ID 的映射规则
_LAYER_MAP: Dict[str, str] = {
    # 数据采集层
    "nav": "data",
    "data": "data",
    "datasource": "data",
    "market": "data",
    "collector": "data",
    # 核心业务层
    "fund": "business",
    "rank": "business",
    "score": "business",
    "portfolio": "business",
    "anti": "business",
    "watchlist": "business",
    "alert": "business",
    # 展示层
    "rendering": "display",
    "static": "display",
    # 系统层
    "system": "system",
    "common": "system",
    "logging": "system",
    "security": "system",
    "uploads": "system",
    "monitor": "system",
    # 流程管控层
    "superpower": "process",
    "analysis_pipeline": "process",
    "api": "process",
    "services": "process",
    "repository": "process",
    "domain": "process",
}


# ---------------------------------------------------------------------------
# NeuralUpdater 主类
# ---------------------------------------------------------------------------

class NeuralUpdater:
    """
    神经网络自动更新器。

    解析 Python 代码结构，与 function_graph.json 对比生成更新预览，
    支持半自动（人工确认）或全自动更新。

    Attributes:
        graph_path: function_graph.json 的路径
        code_root: 代码根目录（相对路径）
        backup_dir: 备份目录
        auto_confirm: 是否自动确认更新（False 时仅返回预览）
        graph: 当前加载的 function_graph 数据
    """

    def __init__(
        self,
        graph_path: str = "docs/function_graph.json",
        code_root: str = "modules",
        backup_dir: str = "docs/backups",
        auto_confirm: bool = False,
    ) -> None:
        """
        初始化更新器。

        Args:
            graph_path: function_graph.json 路径（相对工作目录）
            code_root: 代码根目录（相对工作目录）
            backup_dir: 备份目录（相对工作目录）
            auto_confirm: 是否自动确认并执行更新
        """
        self.graph_path = graph_path
        self.code_root = code_root
        self.backup_dir = backup_dir
        self.auto_confirm = auto_confirm
        self.graph: Dict = {}

        # 加载现有图谱
        self._load_graph()

        # 创建备份目录
        os.makedirs(self.backup_dir, exist_ok=True)

    # ------------------------------------------------------------------
    # 代码解析方法
    # ------------------------------------------------------------------

    def parse_file(self, file_path: str) -> ParseResult:
        """
        解析单个 Python 文件，提取函数定义和调用关系。

        使用 ast 模块静态解析，不执行代码。

        Args:
            file_path: Python 文件路径（相对或绝对）

        Returns:
            ParseResult: 解析结果，包含函数列表、调用列表、错误信息
        """
        result = ParseResult(files_scanned=1)

        # 读取文件（使用 utf-8-sig 自动处理 BOM 头）
        try:
            with open(file_path, "r", encoding="utf-8-sig") as f:
                source = f.read()
        except (OSError, UnicodeDecodeError) as e:
            result.errors.append(f"无法读取文件 {file_path}: {e}")
            return result

        # 解析 AST
        try:
            tree = ast.parse(source, filename=file_path)
        except SyntaxError as e:
            result.errors.append(f"语法错误 {file_path}:{e.lineno}: {e.msg}")
            return result

        # 计算相对路径（用于 code_files 和层级推断）
        rel_path = self._relative_path(file_path)

        # 模块名推导（从路径转换，如 modules/fund/service.py -> fund.service）
        module_name = self._path_to_module_name(rel_path)

        # 遍历顶层节点，处理类和函数
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                func_info = self._build_function_info(
                    node, module_name, rel_path, class_name=None
                )
                result.functions.append(func_info)
                # 提取函数体内的调用
                result.calls.extend(
                    self._extract_calls(node, func_info.qualified_name, rel_path)
                )
            elif isinstance(node, ast.ClassDef):
                # 处理类中的方法
                for item in ast.iter_child_nodes(node):
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        method_info = self._build_function_info(
                            item, module_name, rel_path, class_name=node.name
                        )
                        result.functions.append(method_info)
                        result.calls.extend(
                            self._extract_calls(
                                item, method_info.qualified_name, rel_path
                            )
                        )

        return result

    def parse_directory(
        self, dir_path: str, pattern: str = "*.py"
    ) -> ParseResult:
        """
        递归解析目录下所有 Python 文件，合并结果。

        Args:
            dir_path: 目录路径
            pattern: 文件匹配模式（默认 *.py）

        Returns:
            ParseResult: 合并后的解析结果
        """
        merged = ParseResult()

        if not os.path.isdir(dir_path):
            merged.errors.append(f"目录不存在: {dir_path}")
            return merged

        # 递归收集所有匹配文件
        py_files: List[str] = []
        for root, _dirs, files in os.walk(dir_path):
            for fname in files:
                if fname.endswith(".py"):
                    py_files.append(os.path.join(root, fname))

        # 排除 __init__.py 中的空文件和测试文件（可选）
        py_files.sort()

        for py_file in py_files:
            file_result = self.parse_file(py_file)
            merged.functions.extend(file_result.functions)
            merged.calls.extend(file_result.calls)
            merged.errors.extend(file_result.errors)
            merged.files_scanned += file_result.files_scanned

        return merged

    def _extract_calls(
        self, node: ast.AST, caller_name: str, file_path: str
    ) -> List[CallRelation]:
        """
        从 AST 节点中提取所有函数调用。

        处理以下调用形式：
        - 直接调用：func()
        - 方法调用：obj.method()
        - 链式调用：a.b.c()
        - 动态调用标记：getattr、__import__ 等标记为 unresolved

        Args:
            node: AST 节点（通常是函数定义节点）
            caller_name: 调用者的限定名
            file_path: 所在文件路径

        Returns:
            List[CallRelation]: 调用关系列表
        """
        calls: List[CallRelation] = []

        for child in ast.walk(node):
            if not isinstance(child, ast.Call):
                continue

            callee_name = self._resolve_call_name(child.func)

            # 标记动态调用
            if callee_name in ("getattr", "__import__", "eval", "exec"):
                callee_name = f"{callee_name}(unresolved)"

            calls.append(
                CallRelation(
                    caller=caller_name,
                    callee=callee_name,
                    file_path=file_path,
                    line_no=getattr(child, "lineno", 0),
                )
            )

        return calls

    # ------------------------------------------------------------------
    # 差异分析方法
    # ------------------------------------------------------------------

    def diff_with_graph(self, parse_result: ParseResult) -> UpdatePreview:
        """
        将解析结果与现有 function_graph.json 对比，生成更新预览。

        对比规则：
        - 新增节点：解析出的函数在现有 nodes 中不存在（按 id 匹配）
        - 删除节点：现有 nodes 中存在但解析结果中没有的（仅针对涉及被修改文件的节点）
        - 修改节点：函数存在但 docstring/description 变化
        - 新增边：根据调用关系生成 source→target 边
        - 删除边：现有边中涉及被删除节点的边

        Args:
            parse_result: 代码解析结果

        Returns:
            UpdatePreview: 更新预览
        """
        preview = UpdatePreview()
        existing_nodes = self.graph.get("nodes", [])
        existing_edges = self.graph.get("edges", [])

        preview.node_count_before = len(existing_nodes)
        preview.edge_count_before = len(existing_edges)

        # 构建现有节点索引：id -> node
        existing_node_map: Dict[str, Dict] = {
            n["id"]: n for n in existing_nodes if "id" in n
        }

        # 构建解析出的函数节点索引：id -> FunctionInfo
        parsed_node_map: Dict[str, FunctionInfo] = {}
        for func in parse_result.functions:
            nid = self._to_node_id(func.qualified_name)
            parsed_node_map[nid] = func

        # 确定被扫描的文件集合（用于判断哪些节点可能被删除）
        scanned_files = set()
        for func in parse_result.functions:
            scanned_files.add(func.file_path)
        for call in parse_result.calls:
            scanned_files.add(call.file_path)

        # --- 新增节点 ---
        for nid, func in parsed_node_map.items():
            if nid not in existing_node_map:
                node_dict = self._function_to_node(func)
                preview.added_nodes.append(node_dict)

        # --- 删除节点 ---
        # 仅删除 code_files 包含被扫描文件的节点
        removed_ids: List[str] = []
        for nid, node in existing_node_map.items():
            if nid in parsed_node_map:
                continue
            node_files = node.get("code_files", [])
            # 检查节点的 code_files 是否有任何一个在扫描范围内
            affected = False
            for nf in node_files:
                # 标准化路径比较
                nf_norm = nf.replace("\\", "/").lstrip("./")
                for sf in scanned_files:
                    sf_norm = sf.replace("\\", "/").lstrip("./")
                    if nf_norm == sf_norm or nf_norm.endswith(sf_norm) or sf_norm.endswith(nf_norm):
                        affected = True
                        break
                if affected:
                    break
            if affected:
                removed_ids.append(nid)
                preview.removed_nodes.append(nid)

        # --- 修改节点 ---
        for nid, func in parsed_node_map.items():
            if nid not in existing_node_map:
                continue
            existing = existing_node_map[nid]
            new_desc = (func.docstring or "")[:200].strip()
            old_desc = existing.get("description", "")
            # 描述变化视为修改
            if new_desc and new_desc != old_desc:
                modified = dict(existing)
                modified["description"] = new_desc
                modified["last_verified"] = datetime.now().strftime("%Y-%m-%d")
                modified["verify_result"] = "auto_updated"
                # 更新 code_files（追加新文件，去重）
                existing_files = set(existing.get("code_files", []))
                existing_files.add(func.file_path)
                modified["code_files"] = sorted(existing_files)
                preview.modified_nodes.append(modified)

        # --- 新增边 ---
        # 根据调用关系生成边：调用者 -> 被调用者
        # 构建函数名 -> 节点id 的映射（用于解析被调用者）
        func_name_to_id: Dict[str, str] = {}
        for nid, func in parsed_node_map.items():
            func_name_to_id[func.name] = nid
            func_name_to_id[func.qualified_name] = nid

        # 也加入现有节点的名称映射
        for nid, node in existing_node_map.items():
            node_name = node.get("name", "")
            if node_name:
                func_name_to_id[node_name] = nid

        existing_edge_keys = set()
        for edge in existing_edges:
            key = (edge.get("source", ""), edge.get("target", ""), edge.get("type", "calls"))
            existing_edge_keys.add(key)

        for call in parse_result.calls:
            source_id = self._to_node_id(call.caller)
            # 尝试解析被调用者为节点ID
            target_id = self._resolve_callee_to_node_id(
                call.callee, func_name_to_id, parsed_node_map
            )
            if target_id is None:
                continue  # 无法解析为已知节点，跳过

            # 跳过自调用
            if source_id == target_id:
                continue

            edge_key = (source_id, target_id, "calls")
            if edge_key not in existing_edge_keys:
                preview.added_edges.append(
                    {
                        "source": source_id,
                        "target": target_id,
                        "type": "calls",
                        "label": "调用",
                    }
                )
                existing_edge_keys.add(edge_key)  # 去重

        # --- 删除边 ---
        # 涉及被删除节点的边
        for edge in existing_edges:
            src = edge.get("source", "")
            tgt = edge.get("target", "")
            if src in removed_ids or tgt in removed_ids:
                preview.removed_edges.append(dict(edge))

        # --- 计算更新后数量 ---
        preview.node_count_after = (
            preview.node_count_before
            + len(preview.added_nodes)
            - len(preview.removed_nodes)
        )
        preview.edge_count_after = (
            preview.edge_count_before
            + len(preview.added_edges)
            - len(preview.removed_edges)
        )

        # --- 生成警告 ---
        self._generate_warnings(preview, existing_node_map, parsed_node_map)

        return preview

    def _function_to_node(self, func: FunctionInfo) -> Dict:
        """
        将函数信息转换为 function_graph 节点格式。

        Args:
            func: 函数信息

        Returns:
            Dict: 符合 function_graph.json 格式的节点字典
        """
        # 从 docstring 第一行提取中文友好名
        friendly_name = func.name
        if func.docstring:
            first_line = func.docstring.strip().split("\n")[0].strip()
            # 如果第一行是中文或包含中文，用作友好名
            if first_line and len(first_line) <= 50:
                friendly_name = first_line

        # 截取描述前200字
        description = (func.docstring or "")[:200].strip()

        # 获取当前版本号
        version = self.graph.get("meta", {}).get("version", "1.0.0")

        return {
            "id": self._to_node_id(func.qualified_name),
            "name": friendly_name,
            "layer": self._infer_layer(func.file_path),
            "status": "new",
            "version": version,
            "description": description,
            "check_points": [],
            "known_issues": [],
            "code_files": [func.file_path],
            "doc_sections": [],
            "config_keys": [],
            "last_verified": datetime.now().strftime("%Y-%m-%d"),
            "verify_result": "auto_detected",
        }

    # ------------------------------------------------------------------
    # 更新执行方法
    # ------------------------------------------------------------------

    def backup_graph(self) -> str:
        """
        备份当前 function_graph.json 到 backups 目录。

        备份文件名格式：function_graph_YYYYMMDD_HHMMSS.json

        Returns:
            str: 备份文件的绝对路径
        """
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_filename = f"function_graph_{timestamp}.json"
        backup_path = os.path.join(self.backup_dir, backup_filename)

        # 确保源文件存在
        if not os.path.exists(self.graph_path):
            # 如果图谱文件不存在，创建空结构
            self._save_graph(self._empty_graph())

        shutil.copy2(self.graph_path, backup_path)
        return os.path.abspath(backup_path)

    def apply_update(self, preview: UpdatePreview) -> UpdateResult:
        """
        应用更新预览到 function_graph.json。

        执行流程：
        1. 先备份当前图谱
        2. 应用节点增删改
        3. 应用边增删
        4. 更新 meta 中的 node_count、edge_count、last_updated
        5. 写入文件（UTF-8编码，indent=2，ensure_ascii=False）
        6. 验证写入结果
        7. 失败时自动回滚

        Args:
            preview: 更新预览

        Returns:
            UpdateResult: 更新执行结果
        """
        result = UpdateResult(success=False, preview=preview)

        try:
            # 1. 备份
            result.backup_path = self.backup_graph()

            # 2. 构建新图谱
            new_graph = json.loads(json.dumps(self.graph))  # 深拷贝
            nodes = new_graph.get("nodes", [])
            edges = new_graph.get("edges", [])

            # 2a. 删除节点
            removed_set = set(preview.removed_nodes)
            nodes = [n for n in nodes if n.get("id") not in removed_set]

            # 2b. 修改节点
            modified_map = {n["id"]: n for n in preview.modified_nodes if "id" in n}
            for i, node in enumerate(nodes):
                nid = node.get("id")
                if nid in modified_map:
                    nodes[i] = modified_map[nid]

            # 2c. 新增节点
            existing_ids = {n.get("id") for n in nodes}
            for new_node in preview.added_nodes:
                if new_node.get("id") not in existing_ids:
                    nodes.append(new_node)
                    existing_ids.add(new_node["id"])

            new_graph["nodes"] = nodes

            # 3a. 删除边
            removed_edge_keys = set()
            for re in preview.removed_edges:
                removed_edge_keys.add(
                    (re.get("source", ""), re.get("target", ""), re.get("type", ""))
                )
            edges = [
                e
                for e in edges
                if (e.get("source", ""), e.get("target", ""), e.get("type", ""))
                not in removed_edge_keys
            ]

            # 3b. 新增边
            existing_edge_keys = {
                (e.get("source", ""), e.get("target", ""), e.get("type", ""))
                for e in edges
            }
            for new_edge in preview.added_edges:
                key = (
                    new_edge.get("source", ""),
                    new_edge.get("target", ""),
                    new_edge.get("type", ""),
                )
                if key not in existing_edge_keys:
                    edges.append(new_edge)
                    existing_edge_keys.add(key)

            new_graph["edges"] = edges

            # 4. 更新 meta
            meta = new_graph.get("meta", {})
            meta["node_count"] = len(nodes)
            meta["edge_count"] = len(edges)
            meta["last_updated"] = datetime.now().strftime("%Y-%m-%d")
            new_graph["meta"] = meta
            # 同步顶层字段
            new_graph["last_updated"] = meta["last_updated"]

            # 5. 写入文件
            self._save_graph(new_graph)

            # 6. 验证写入结果
            self._load_graph()  # 重新加载
            actual_nodes = len(self.graph.get("nodes", []))
            actual_edges = len(self.graph.get("edges", []))

            if actual_nodes != len(nodes) or actual_edges != len(edges):
                raise ValueError(
                    f"写入验证失败：预期节点={len(nodes)}, 实际={actual_nodes}; "
                    f"预期边={len(edges)}, 实际={actual_edges}"
                )

            result.success = True

        except Exception as e:
            result.error = str(e)
            # 7. 失败自动回滚
            if result.backup_path and os.path.exists(result.backup_path):
                rollback_ok = self.rollback(result.backup_path)
                result.rollback_performed = rollback_ok
                if not rollback_ok:
                    result.error += "（回滚也失败，请手动检查）"

        return result

    def validate_update(self, preview: UpdatePreview) -> Tuple[bool, List[str]]:
        """
        验证更新预览的合理性。

        验证项：
        - 节点数变化不超过50%
        - 边数变化不超过50%
        - 没有重复节点ID
        - 所有边的 source/target 都存在于节点中
        - meta 中 node_count/edge_count 和实际一致

        Args:
            preview: 更新预览

        Returns:
            Tuple[bool, List[str]]: (是否通过, 错误信息列表)
        """
        errors: List[str] = []

        # 1. 节点数变化不超过50%
        if preview.node_count_before > 0:
            node_change = abs(
                preview.node_count_after - preview.node_count_before
            ) / preview.node_count_before
            if node_change > 0.5:
                errors.append(
                    f"节点数变化过大: {preview.node_count_before} -> "
                    f"{preview.node_count_after} ({node_change:.1%} > 50%)"
                )

        # 2. 边数变化不超过50%
        if preview.edge_count_before > 0:
            edge_change = abs(
                preview.edge_count_after - preview.edge_count_before
            ) / preview.edge_count_before
            if edge_change > 0.5:
                errors.append(
                    f"边数变化过大: {preview.edge_count_before} -> "
                    f"{preview.edge_count_after} ({edge_change:.1%} > 50%)"
                )

        # 3. 没有重复节点ID（新增节点之间、新增与现有之间）
        existing_ids = {n["id"] for n in self.graph.get("nodes", []) if "id" in n}
        new_ids = [n["id"] for n in preview.added_nodes if "id" in n]
        # 新增节点内部重复
        if len(new_ids) != len(set(new_ids)):
            dupes = [nid for nid in new_ids if new_ids.count(nid) > 1]
            errors.append(f"新增节点存在重复ID: {set(dupes)}")
        # 新增与现有重复（理论上 diff 阶段已排除，双重检查）
        overlap = set(new_ids) & existing_ids
        if overlap:
            errors.append(f"新增节点ID与现有节点重复: {overlap}")

        # 4. 所有边的 source/target 都存在于最终节点集合中
        final_ids = (existing_ids - set(preview.removed_nodes)) | set(new_ids)
        for edge in preview.added_edges:
            src = edge.get("source", "")
            tgt = edge.get("target", "")
            if src not in final_ids:
                errors.append(f"新增边的 source 不存在于节点中: {src} (边: {src}->{tgt})")
            if tgt not in final_ids:
                errors.append(f"新增边的 target 不存在于节点中: {tgt} (边: {src}->{tgt})")

        # 5. meta 中 node_count/edge_count 和实际一致（基于预览计算）
        expected_nodes = (
            preview.node_count_before
            + len(preview.added_nodes)
            - len(preview.removed_nodes)
        )
        if expected_nodes != preview.node_count_after:
            errors.append(
                f"预览节点数不一致: 计算={expected_nodes}, 记录={preview.node_count_after}"
            )

        return (len(errors) == 0, errors)

    def rollback(self, backup_path: str) -> bool:
        """
        从备份回滚 function_graph.json。

        Args:
            backup_path: 备份文件路径

        Returns:
            bool: 是否回滚成功
        """
        try:
            if not os.path.exists(backup_path):
                return False
            shutil.copy2(backup_path, self.graph_path)
            self._load_graph()  # 重新加载
            return True
        except Exception:
            return False

    def run(
        self, modified_files: Optional[List[str]] = None
    ) -> UpdateResult:
        """
        完整执行更新流程。

        流程：
        1. 如果指定 modified_files，只解析这些文件；否则解析整个 modules 目录
        2. 解析代码
        3. 差异分析生成预览
        4. 验证预览合理性
        5. 如果 auto_confirm=False，返回预览等待确认（不执行更新）
        6. 如果 auto_confirm=True，执行更新
        7. 返回 UpdateResult

        Args:
            modified_files: 指定修改的文件列表（相对路径），为 None 时解析整个 code_root

        Returns:
            UpdateResult: 执行结果（auto_confirm=False 时 success=False 但 preview 有值）
        """
        # 1. 解析代码
        if modified_files:
            parse_result = ParseResult()
            for file_path in modified_files:
                # 支持相对路径和绝对路径
                if not os.path.isabs(file_path):
                    file_path = os.path.join(os.getcwd(), file_path)
                if os.path.exists(file_path):
                    file_result = self.parse_file(file_path)
                    parse_result.functions.extend(file_result.functions)
                    parse_result.calls.extend(file_result.calls)
                    parse_result.errors.extend(file_result.errors)
                    parse_result.files_scanned += file_result.files_scanned
                else:
                    parse_result.errors.append(f"文件不存在: {file_path}")
        else:
            parse_result = self.parse_directory(self.code_root)

        # 2. 差异分析
        preview = self.diff_with_graph(parse_result)

        # 3. 验证预览合理性
        is_valid, validation_errors = self.validate_update(preview)
        if not is_valid:
            preview.warnings.extend(
                [f"[验证失败] {e}" for e in validation_errors]
            )

        # 4. 如果没有变化，直接返回
        if (
            not preview.added_nodes
            and not preview.removed_nodes
            and not preview.modified_nodes
            and not preview.added_edges
            and not preview.removed_edges
        ):
            return UpdateResult(
                success=True,
                preview=preview,
                error=None,
            )

        # 5. auto_confirm=False 时返回预览等待确认
        if not self.auto_confirm:
            return UpdateResult(
                success=False,
                preview=preview,
                error="需要人工确认后执行更新（调用 apply_update）",
            )

        # 6. 验证不通过时不执行
        if not is_valid:
            return UpdateResult(
                success=False,
                preview=preview,
                error="更新验证未通过: " + "; ".join(validation_errors),
            )

        # 7. 执行更新
        return self.apply_update(preview)

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    def _load_graph(self) -> Dict:
        """
        加载 function_graph.json。

        Returns:
            Dict: 图谱数据字典
        """
        if os.path.exists(self.graph_path):
            try:
                with open(self.graph_path, "r", encoding="utf-8") as f:
                    self.graph = json.load(f)
            except (json.JSONDecodeError, OSError):
                self.graph = self._empty_graph()
        else:
            self.graph = self._empty_graph()
        return self.graph

    def _save_graph(self, graph: Dict) -> None:
        """
        保存 function_graph.json（UTF-8 编码）。

        Args:
            graph: 图谱数据字典
        """
        # 确保目录存在
        os.makedirs(os.path.dirname(self.graph_path) or ".", exist_ok=True)
        with open(self.graph_path, "w", encoding="utf-8") as f:
            json.dump(graph, f, indent=2, ensure_ascii=False)

    def _infer_layer(self, file_path: str) -> str:
        """
        根据文件路径推断层级。

        映射规则：
        - modules/nav, modules/data, modules/datasource -> data
        - modules/fund, modules/rank, modules/score -> business
        - modules/system, modules/common -> system
        - modules/superpower -> process
        - 其他 -> business（默认核心业务层）

        Args:
            file_path: 文件相对路径

        Returns:
            str: 层级ID
        """
        normalized = file_path.replace("\\", "/").lower()
        for dir_name, layer in _LAYER_MAP.items():
            if f"/{dir_name}/" in normalized or normalized.startswith(f"{dir_name}/"):
                return layer
        return "business"  # 默认核心业务层

    def _to_node_id(self, name: str) -> str:
        """
        函数名转节点ID（snake_case，去特殊字符）。

        规则：
        - 转小写
        - 点号和空格转下划线
        - 移除非字母数字下划线字符
        - 类方法用 类名_方法名 形式

        Args:
            name: 函数名或限定名

        Returns:
            str: snake_case 节点ID
        """
        # 取限定名的最后两部分（类名.方法名）或最后一部分
        parts = name.split(".")
        if len(parts) >= 2:
            # 取最后两个部分（如 FundService.get_fund_list）
            key = "_".join(parts[-2:])
        else:
            key = parts[-1]

        # 转 snake_case
        # 先处理驼峰：CamelCase -> camel_case
        key = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", key)
        key = re.sub(r"([a-z\d])([A-Z])", r"\1_\2", key)
        key = key.lower()
        # 替换非字母数字下划线为下划线
        key = re.sub(r"[^\w]", "_", key)
        # 合并连续下划线
        key = re.sub(r"_+", "_", key)
        # 去除首尾下划线
        key = key.strip("_")
        return key

    def get_graph_stats(self) -> Dict:
        """
        返回当前图统计信息。

        Returns:
            Dict: 包含节点数、边数、各层节点数的统计字典
        """
        nodes = self.graph.get("nodes", [])
        edges = self.graph.get("edges", [])

        # 各层节点数
        layer_counts: Dict[str, int] = {}
        for node in nodes:
            layer = node.get("layer", "unknown")
            layer_counts[layer] = layer_counts.get(layer, 0) + 1

        # 状态统计
        status_counts: Dict[str, int] = {}
        for node in nodes:
            status = node.get("status", "unknown")
            status_counts[status] = status_counts.get(status, 0) + 1

        return {
            "node_count": len(nodes),
            "edge_count": len(edges),
            "layer_distribution": layer_counts,
            "status_distribution": status_counts,
            "version": self.graph.get("meta", {}).get("version", "unknown"),
            "last_updated": self.graph.get("meta", {}).get("last_updated", "unknown"),
        }

    # ------------------------------------------------------------------
    # 内部辅助方法
    # ------------------------------------------------------------------

    def _build_function_info(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        module_name: str,
        file_path: str,
        class_name: Optional[str],
    ) -> FunctionInfo:
        """
        从 AST 函数定义节点构建 FunctionInfo。

        Args:
            node: AST 函数定义节点
            module_name: 模块名
            file_path: 文件相对路径
            class_name: 类名（顶层函数为 None）

        Returns:
            FunctionInfo: 函数信息
        """
        # 限定名
        if class_name:
            qualified_name = f"{module_name}.{class_name}.{node.name}"
        else:
            qualified_name = f"{module_name}.{node.name}"

        # docstring
        docstring = ast.get_docstring(node) or ""

        # 装饰器
        decorators: List[str] = []
        for dec in node.decorator_list:
            decorators.append(ast.unparse(dec) if hasattr(ast, "unparse") else "@decorator")

        # 参数（排除 self/cls）
        params: List[str] = []
        for arg in node.args.args:
            if arg.arg not in ("self", "cls"):
                params.append(arg.arg)

        return FunctionInfo(
            name=node.name,
            qualified_name=qualified_name,
            file_path=file_path,
            line_start=node.lineno,
            line_end=getattr(node, "end_lineno", node.lineno),
            docstring=docstring,
            decorators=decorators,
            is_method=class_name is not None,
            is_async=isinstance(node, ast.AsyncFunctionDef),
            params=params,
        )

    def _resolve_call_name(self, func_node: ast.AST) -> str:
        """
        解析调用表达式的函数名。

        处理：
        - ast.Name: func -> func
        - ast.Attribute: obj.method -> method
        - ast.Attribute 链式: a.b.c -> c

        Args:
            func_node: 调用的 func 部分 AST 节点

        Returns:
            str: 解析出的函数名
        """
        if isinstance(func_node, ast.Name):
            return func_node.id
        elif isinstance(func_node, ast.Attribute):
            return func_node.attr
        elif isinstance(func_node, ast.Subscript):
            # 如 some_dict["key"]() - 取基础名
            return self._resolve_call_name(func_node.value)
        else:
            return "unresolved"

    def _resolve_callee_to_node_id(
        self,
        callee: str,
        func_name_to_id: Dict[str, str],
        parsed_node_map: Dict[str, FunctionInfo],
    ) -> Optional[str]:
        """
        将被调用者名称解析为节点ID。

        Args:
            callee: 被调用者名称
            func_name_to_id: 函数名 -> 节点ID 映射
            parsed_node_map: 节点ID -> FunctionInfo 映射

        Returns:
            Optional[str]: 节点ID，无法解析时返回 None
        """
        # 直接匹配
        if callee in func_name_to_id:
            return func_name_to_id[callee]

        # 去掉 self. 前缀后匹配
        clean = callee
        if clean.startswith("self."):
            clean = clean[5:]
        if clean in func_name_to_id:
            return func_name_to_id[clean]

        # 尝试在解析出的函数名中模糊匹配
        for nid, func in parsed_node_map.items():
            if func.name == callee or func.name == clean:
                return nid

        return None

    def _generate_warnings(
        self,
        preview: UpdatePreview,
        existing_node_map: Dict[str, Dict],
        parsed_node_map: Dict[str, FunctionInfo],
    ) -> None:
        """
        生成更新预览的警告信息。

        警告类型：
        - 节点数变化超过20%
        - 边数变化超过30%
        - 孤立节点（新增节点没有任何边连接）

        Args:
            preview: 更新预览（会被修改添加警告）
            existing_node_map: 现有节点映射
            parsed_node_map: 解析出的节点映射
        """
        # 节点数变化超过20%
        if preview.node_count_before > 0:
            node_change = abs(
                preview.node_count_after - preview.node_count_before
            ) / preview.node_count_before
            if node_change > 0.2:
                preview.warnings.append(
                    f"节点数变化超过20%: {preview.node_count_before} -> "
                    f"{preview.node_count_after} ({node_change:.1%})"
                )

        # 边数变化超过30%
        if preview.edge_count_before > 0:
            edge_change = abs(
                preview.edge_count_after - preview.edge_count_before
            ) / preview.edge_count_before
            if edge_change > 0.3:
                preview.warnings.append(
                    f"边数变化超过30%: {preview.edge_count_before} -> "
                    f"{preview.edge_count_after} ({edge_change:.1%})"
                )

        # 孤立节点检查（新增节点没有任何边连接）
        added_ids = {n["id"] for n in preview.added_nodes}
        connected_ids = set()
        for edge in preview.added_edges:
            connected_ids.add(edge.get("source", ""))
            connected_ids.add(edge.get("target", ""))
        # 也检查现有边中是否连接到新增节点
        for edge in self.graph.get("edges", []):
            if edge.get("source") in added_ids or edge.get("target") in added_ids:
                connected_ids.add(edge.get("source", ""))
                connected_ids.add(edge.get("target", ""))

        isolated = added_ids - connected_ids
        if isolated:
            preview.warnings.append(
                f"新增孤立节点（无调用关系）: {sorted(isolated)}"
            )

        # 解析错误警告
        if parsed_node_map and not preview.added_nodes and not preview.modified_nodes:
            preview.warnings.append(
                "解析到函数但未产生任何节点变更，可能所有函数已存在于图谱中"
            )

    def _relative_path(self, file_path: str) -> str:
        """
        将文件路径转为相对工作目录的路径。

        Args:
            file_path: 绝对或相对路径

        Returns:
            str: 相对路径（使用正斜杠）
        """
        try:
            rel = os.path.relpath(file_path, os.getcwd())
        except ValueError:
            rel = file_path
        return rel.replace("\\", "/")

    def _path_to_module_name(self, rel_path: str) -> str:
        """
        将文件相对路径转为模块名。

        如 modules/fund/service.py -> fund.service
        如 modules/superpower/neural_updater.py -> superpower.neural_updater

        Args:
            rel_path: 相对路径

        Returns:
            str: 模块点分名
        """
        # 去掉 .py 后缀
        path = rel_path
        if path.endswith(".py"):
            path = path[:-3]
        # 去掉开头的 modules/ 或 backend/modules/
        path = re.sub(r"^(backend/)?modules/", "", path)
        # 路径分隔符转点
        module_name = path.replace("/", ".").replace("\\", ".")
        # 去掉 __init__
        module_name = re.sub(r"\.__init__$", "", module_name)
        return module_name

    @staticmethod
    def _empty_graph() -> Dict:
        """
        返回空的图谱结构。

        Returns:
            Dict: 空图谱字典
        """
        return {
            "meta": {
                "name": "invest-v281 功能神经网络",
                "version": "1.0.0",
                "created": datetime.now().strftime("%Y-%m-%d"),
                "description": "系统级功能依赖图谱，由 NeuralUpdater 自动生成",
                "node_count": 0,
                "edge_count": 0,
                "last_updated": datetime.now().strftime("%Y-%m-%d"),
            },
            "layers": [
                {"id": "data", "name": "数据采集层", "color": "#3B82F6"},
                {"id": "business", "name": "核心业务层", "color": "#8B5CF6"},
                {"id": "display", "name": "展示层", "color": "#10B981"},
                {"id": "system", "name": "系统层", "color": "#F59E0B"},
                {"id": "process", "name": "流程管控层", "color": "#06B6D4"},
            ],
            "nodes": [],
            "edges": [],
            "version": "1.0.0",
            "last_updated": datetime.now().strftime("%Y-%m-%d"),
        }
