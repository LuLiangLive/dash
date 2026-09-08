"""
网关分析器 - 基于神经网络function_graph.json的变更影响传播分析

本模块实现GatewayAnalyzer类，用于：
1. 加载function_graph.json神经网络，构建正向/反向邻接表
2. 对变更核心节点执行BFS正向/反向影响传播分析
3. 自动识别风险点（影响范围、核心业务层、神经网络本身、数据库操作、API接口）
4. 自动生成分级检查清单（must/should/optional）
5. 生成结构化文本报告和可序列化字典

参考实现：backend/_gateway_047_superpower_fusion.py中的BFS逻辑

数据结构：
- RiskItem: 风险点（id, level, description, impact, mitigation）
- ChecklistItem: 检查项（id, stage, description, priority, status, verified）
- GatewayAnalysisResult: 网关分析完整结果

版本：1.0.0
"""

import json
import os
import logging
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Set, Tuple, Optional, Literal, Any
from collections import defaultdict, deque
from datetime import datetime

# 配置日志
logger = logging.getLogger(__name__)


# ============================================================
# 数据结构定义
# ============================================================

@dataclass
class RiskItem:
    """
    风险点数据结构

    Attributes:
        id: 风险点唯一标识，格式 RISK-{change_id}-{序号}
        level: 风险等级，HIGH / MEDIUM / LOW
        description: 风险描述
        impact: 风险影响说明
        mitigation: 缓解措施建议
    """
    id: str
    level: Literal["HIGH", "MEDIUM", "LOW"]
    description: str
    impact: str
    mitigation: str


@dataclass
class ChecklistItem:
    """
    检查项数据结构

    Attributes:
        id: 检查项唯一标识，格式 CHK-{change_id}-{序号}
        stage: 所属阶段（modify/verify/update/record）
        description: 检查项描述
        priority: 优先级，must（必须）/ should（应该）/ optional（可选）
        status: 状态，pending / in_progress / completed / skipped
        verified: 是否已验证通过
    """
    id: str
    stage: str
    description: str
    priority: Literal["must", "should", "optional"]
    status: Literal["pending", "in_progress", "completed", "skipped"] = "pending"
    verified: bool = False


@dataclass
class GatewayAnalysisResult:
    """
    网关分析完整结果数据结构

    Attributes:
        change_id: 变更编号
        core_nodes: 本次变更的核心节点列表
        forward_affected: 正向影响节点集合（下游受影响）
        backward_affected: 反向影响节点集合（上游依赖方）
        forward_depth: 正向各节点的传播深度映射
        backward_depth: 反向各节点的传播深度映射
        risks: 识别出的风险点列表
        checklist: 生成的检查清单列表
        stats: 统计数据字典
        generated_at: 分析生成时间（ISO格式）
    """
    change_id: str
    core_nodes: List[str]
    forward_affected: Set[str]
    backward_affected: Set[str]
    forward_depth: Dict[str, int]
    backward_depth: Dict[str, int]
    risks: List[RiskItem]
    checklist: List[ChecklistItem]
    stats: Dict[str, Any]
    generated_at: str


# ============================================================
# 网关分析器主类
# ============================================================

class GatewayAnalyzer:
    """
    网关分析器 - 基于神经网络的变更影响传播分析

    功能：
    1. 加载function_graph.json，构建正向/反向邻接表
    2. BFS正向/反向影响传播分析（支持自定义最大深度）
    3. 影响范围统计（节点数、深度分布、涉及文件数、上下游边数）
    4. 自动风险识别（影响范围、核心业务层、神经网络本身、数据库、API）
    5. 自动生成分级检查清单
    6. 生成结构化文本报告和可序列化字典

    使用示例：
        analyzer = GatewayAnalyzer(graph_path="docs/function_graph.json", max_depth=3)
        result = analyzer.analyze("CHG-TEST-001", ["change_gateway"], "测试变更")
        report = analyzer.generate_report(result)
        print(report)
    """

    # 风险等级排序权重
    _RISK_LEVEL_ORDER = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}

    def __init__(
        self,
        graph_path: str = "docs/function_graph.json",
        max_depth: int = 3,
        risk_threshold: str = "MEDIUM",
    ) -> None:
        """
        初始化网关分析器

        Args:
            graph_path: function_graph.json的路径（相对于backend/目录）
            max_depth: BFS最大传播深度，默认3
            risk_threshold: 风险报告阈值，低于此等级的风险不显示（HIGH/MEDIUM/LOW）

        Raises:
            FileNotFoundError: 当graph_path指定的文件不存在时
            json.JSONDecodeError: 当JSON文件格式错误时
        """
        self.graph_path = graph_path
        self.max_depth = max_depth
        self.risk_threshold = risk_threshold

        # 加载神经网络图
        self._load_graph()

        # 构建邻接表
        self._build_adjacency()

        logger.info(
            "GatewayAnalyzer initialized: graph=%s, nodes=%d, edges=%d, max_depth=%d",
            graph_path,
            len(self.nodes),
            len(self.edges),
            max_depth,
        )

    def _load_graph(self) -> None:
        """
        加载function_graph.json神经网络文件

        内部方法，解析JSON并提取nodes、edges、meta信息。
        同时构建node_info字典，便于按id快速查找节点详情。
        """
        if not os.path.exists(self.graph_path):
            raise FileNotFoundError(f"神经网络文件不存在: {self.graph_path}")

        with open(self.graph_path, "r", encoding="utf-8") as f:
            self.graph = json.load(f)

        self.nodes = self.graph.get("nodes", [])
        self.edges = self.graph.get("edges", [])
        self.meta = self.graph.get("meta", {})
        self.layers = self.graph.get("layers", [])

        # 构建节点信息查找表
        self.node_info: Dict[str, Dict[str, Any]] = {}
        for node in self.nodes:
            nid = node.get("id")
            if nid:
                self.node_info[nid] = node

        logger.debug("Graph loaded: %d nodes, %d edges", len(self.nodes), len(self.edges))

    def _build_adjacency(self) -> None:
        """
        构建正向邻接表和反向邻接表

        兼容边的source/target字段可能是source/target或from/to的情况。
        - adj: 正向邻接表，adj[src] = [dst1, dst2, ...] 表示src依赖/影响dst
        - rev_adj: 反向邻接表，rev_adj[dst] = [src1, src2, ...] 表示依赖dst的节点
        """
        self.adj: Dict[str, List[str]] = defaultdict(list)
        self.rev_adj: Dict[str, List[str]] = defaultdict(list)

        for edge in self.edges:
            # 兼容 source/target 和 from/to 两种字段命名
            src = edge.get("source") or edge.get("from")
            dst = edge.get("target") or edge.get("to")
            if src and dst:
                self.adj[src].append(dst)
                self.rev_adj[dst].append(src)

        logger.debug(
            "Adjacency built: forward keys=%d, reverse keys=%d",
            len(self.adj),
            len(self.rev_adj),
        )

    # ========================================================
    # 核心分析方法
    # ========================================================

    def analyze(
        self,
        change_id: str,
        core_nodes: List[str],
        change_title: str = "",
    ) -> GatewayAnalysisResult:
        """
        执行完整网关分析

        分析流程：
        1. 验证核心节点是否存在于神经网络中
        2. BFS正向影响传播（下游受影响节点）
        3. BFS反向影响传播（上游依赖方节点）
        4. 计算影响范围统计数据
        5. 自动识别风险点
        6. 自动生成检查清单
        7. 组装GatewayAnalysisResult

        Args:
            change_id: 变更编号，如 "CHG-TEST-001"
            core_nodes: 本次变更的核心节点id列表
            change_title: 变更标题（可选，用于报告展示）

        Returns:
            GatewayAnalysisResult: 完整的网关分析结果

        Raises:
            ValueError: 当core_nodes为空或所有节点都不存在于图中时
        """
        if not core_nodes:
            raise ValueError("core_nodes不能为空")

        # 过滤出实际存在于图中的核心节点
        valid_core_nodes = [n for n in core_nodes if n in self.node_info]
        missing_nodes = [n for n in core_nodes if n not in self.node_info]

        if not valid_core_nodes:
            raise ValueError(
                f"所有核心节点都不存在于神经网络中: {core_nodes}"
            )

        if missing_nodes:
            logger.warning(
                "部分核心节点不存在于神经网络中，已跳过: %s",
                missing_nodes,
            )

        logger.info(
            "Starting analysis: change_id=%s, core_nodes=%s, title=%s",
            change_id,
            valid_core_nodes,
            change_title,
        )

        # 1. BFS正向影响传播
        forward_affected, forward_depth = self._bfs(
            valid_core_nodes, self.adj, self.max_depth
        )

        # 2. BFS反向影响传播
        backward_affected, backward_depth = self._bfs(
            valid_core_nodes, self.rev_adj, self.max_depth
        )

        # 3. 计算统计数据
        stats = self._compute_stats(
            forward_affected, backward_affected, forward_depth, backward_depth
        )
        stats["change_title"] = change_title
        stats["valid_core_nodes"] = valid_core_nodes
        stats["missing_nodes"] = missing_nodes
        stats["graph_version"] = self.meta.get("version", "unknown")

        # 4. 识别风险点
        risks = self._identify_risks(valid_core_nodes, forward_affected, backward_affected, stats)

        # 5. 生成检查清单
        checklist = self._generate_checklist(change_id, valid_core_nodes, risks, stats)

        # 6. 组装结果
        result = GatewayAnalysisResult(
            change_id=change_id,
            core_nodes=valid_core_nodes,
            forward_affected=forward_affected,
            backward_affected=backward_affected,
            forward_depth=forward_depth,
            backward_depth=backward_depth,
            risks=risks,
            checklist=checklist,
            stats=stats,
            generated_at=datetime.now().isoformat(),
        )

        logger.info(
            "Analysis complete: change_id=%s, forward=%d, backward=%d, risks=%d, checklist=%d",
            change_id,
            len(forward_affected),
            len(backward_affected),
            len(risks),
            len(checklist),
        )

        return result

    def _bfs(
        self,
        start_nodes: List[str],
        adjacency: Dict[str, List[str]],
        max_depth: int,
    ) -> Tuple[Set[str], Dict[str, int]]:
        """
        BFS广度优先遍历

        从起始节点出发，沿邻接表进行广度优先搜索，返回所有可达节点
        及其传播深度。起始节点的深度为0。

        参考实现：backend/_gateway_047_superpower_fusion.py中的bfs函数

        Args:
            start_nodes: 起始节点列表
            adjacency: 邻接表（正向或反向）
            max_depth: 最大搜索深度

        Returns:
            Tuple[Set[str], Dict[str, int]]:
                - 可达节点集合（包含起始节点）
                - 节点到传播深度的映射字典
        """
        visited: Set[str] = set()
        depth_map: Dict[str, int] = {}
        queue: deque = deque()

        # 初始化队列：所有起始节点深度为0
        for node in start_nodes:
            if node not in visited:
                queue.append((node, 0))

        while queue:
            node, depth = queue.popleft()

            # 跳过已访问或超过最大深度的节点
            if node in visited or depth > max_depth:
                continue

            visited.add(node)
            depth_map[node] = depth

            # 将邻居加入队列（深度+1）
            for neighbor in adjacency.get(node, []):
                if neighbor not in visited:
                    queue.append((neighbor, depth + 1))

        return visited, depth_map

    # ========================================================
    # 统计计算
    # ========================================================

    def _compute_stats(
        self,
        forward: Set[str],
        backward: Set[str],
        f_depth: Dict[str, int],
        b_depth: Dict[str, int],
    ) -> Dict[str, Any]:
        """
        计算影响范围统计数据

        统计内容：
        - 正向/反向影响节点总数
        - 按深度分布的节点数
        - 涉及的代码文件数（从节点的code_files字段收集）
        - 上下游边数
        - 涉及的层级分布
        - 核心业务层节点数

        Args:
            forward: 正向影响节点集合
            backward: 反向影响节点集合
            f_depth: 正向深度映射
            b_depth: 反向深度映射

        Returns:
            Dict[str, Any]: 统计数据字典
        """
        stats: Dict[str, Any] = {}

        # 节点总数
        stats["forward_node_count"] = len(forward)
        stats["backward_node_count"] = len(backward)
        stats["total_affected_count"] = len(forward | backward)
        stats["overlap_count"] = len(forward & backward)

        # 按深度分布
        forward_depth_dist: Dict[int, int] = defaultdict(int)
        for _, depth in f_depth.items():
            forward_depth_dist[depth] += 1
        stats["forward_depth_distribution"] = dict(sorted(forward_depth_dist.items()))

        backward_depth_dist: Dict[int, int] = defaultdict(int)
        for _, depth in b_depth.items():
            backward_depth_dist[depth] += 1
        stats["backward_depth_distribution"] = dict(sorted(backward_depth_dist.items()))

        # 收集涉及的代码文件
        all_affected = forward | backward
        affected_code_files: Set[str] = set()
        for node_id in all_affected:
            node = self.node_info.get(node_id, {})
            code_files = node.get("code_files", [])
            affected_code_files.update(code_files)
        stats["affected_code_files"] = sorted(affected_code_files)
        stats["affected_file_count"] = len(affected_code_files)

        # 上下游边数（影响范围内的边）
        forward_edges = 0
        for src in forward:
            for dst in self.adj.get(src, []):
                if dst in forward:
                    forward_edges += 1
        stats["forward_internal_edges"] = forward_edges

        backward_edges = 0
        for dst in backward:
            for src in self.rev_adj.get(dst, []):
                if src in backward:
                    backward_edges += 1
        stats["backward_internal_edges"] = backward_edges

        # 层级分布
        layer_dist: Dict[str, int] = defaultdict(int)
        business_layer_nodes: List[str] = []
        for node_id in all_affected:
            node = self.node_info.get(node_id, {})
            layer = node.get("layer", "unknown")
            layer_dist[layer] += 1
            if layer == "business":
                business_layer_nodes.append(node_id)
        stats["layer_distribution"] = dict(layer_dist)
        stats["business_layer_nodes"] = sorted(business_layer_nodes)
        stats["business_layer_count"] = len(business_layer_nodes)

        # 涉及数据库操作的节点（code_files包含db或database）
        db_related_nodes: List[str] = []
        for node_id in all_affected:
            node = self.node_info.get(node_id, {})
            code_files = node.get("code_files", [])
            if any("db" in cf.lower() or "database" in cf.lower() or "repository" in cf.lower() for cf in code_files):
                db_related_nodes.append(node_id)
        stats["db_related_nodes"] = sorted(db_related_nodes)
        stats["db_related_count"] = len(db_related_nodes)

        # 涉及API接口的节点（code_files包含api或route）
        api_related_nodes: List[str] = []
        for node_id in all_affected:
            node = self.node_info.get(node_id, {})
            code_files = node.get("code_files", [])
            if any("api" in cf.lower() or "route" in cf.lower() for cf in code_files):
                api_related_nodes.append(node_id)
        stats["api_related_nodes"] = sorted(api_related_nodes)
        stats["api_related_count"] = len(api_related_nodes)

        return stats

    # ========================================================
    # 风险识别
    # ========================================================

    def _identify_risks(
        self,
        core_nodes: List[str],
        forward: Set[str],
        backward: Set[str],
        stats: Dict[str, Any],
    ) -> List[RiskItem]:
        """
        根据影响范围自动识别风险点

        风险识别规则（可扩展）：
        1. 影响范围大小：影响节点>20为HIGH，>10为MEDIUM，否则LOW
        2. 涉及核心业务层节点：business层节点>3为HIGH，>0为MEDIUM
        3. 修改神经网络本身：核心节点包含function_graph或change_gateway为HIGH
        4. 涉及数据库操作：影响范围内包含数据库相关节点为MEDIUM
        5. 涉及API接口：影响范围内包含API相关节点为MEDIUM
        6. 深度传播过远：最大深度>=3且影响节点>10为MEDIUM
        7. 上下游双向影响：正向和反向都有较大影响为MEDIUM

        Args:
            core_nodes: 核心节点列表
            forward: 正向影响节点集合
            backward: 反向影响节点集合
            stats: 统计数据字典

        Returns:
            List[RiskItem]: 识别出的风险点列表（按风险等级降序排列）
        """
        risks: List[RiskItem] = []
        risk_counter = 0
        change_id_prefix = "RISK"

        total_affected = stats.get("total_affected_count", 0)
        business_count = stats.get("business_layer_count", 0)
        db_count = stats.get("db_related_count", 0)
        api_count = stats.get("api_related_count", 0)
        max_f_depth = max(stats.get("forward_depth_distribution", {}).keys(), default=0)
        max_b_depth = max(stats.get("backward_depth_distribution", {}).keys(), default=0)

        # 规则1：影响范围大小
        if total_affected > 20:
            risk_counter += 1
            risks.append(RiskItem(
                id=f"{change_id_prefix}-{risk_counter:03d}",
                level="HIGH",
                description=f"变更影响范围过大，共影响 {total_affected} 个节点（正向{len(forward)}+反向{len(backward)}）",
                impact="大范围影响可能导致多处功能异常，回归测试成本高，遗漏风险大",
                mitigation="1. 拆分变更为多个小变更；2. 优先执行全量回归测试；3. 重点验证深度传播路径上的节点；4. 考虑灰度发布",
            ))
        elif total_affected > 10:
            risk_counter += 1
            risks.append(RiskItem(
                id=f"{change_id_prefix}-{risk_counter:03d}",
                level="MEDIUM",
                description=f"变更影响范围中等，共影响 {total_affected} 个节点",
                impact="中等范围影响需要关注核心路径的功能正确性",
                mitigation="1. 针对影响节点执行定向测试；2. 重点验证核心业务流程；3. 检查清单逐项确认",
            ))
        else:
            risk_counter += 1
            risks.append(RiskItem(
                id=f"{change_id_prefix}-{risk_counter:03d}",
                level="LOW",
                description=f"变更影响范围较小，共影响 {total_affected} 个节点",
                impact="小范围影响风险较低，常规测试即可覆盖",
                mitigation="1. 执行基本功能验证；2. 确认检查清单中must项全部通过",
            ))

        # 规则2：涉及核心业务层
        if business_count > 3:
            risk_counter += 1
            risks.append(RiskItem(
                id=f"{change_id_prefix}-{risk_counter:03d}",
                level="HIGH",
                description=f"变更涉及 {business_count} 个核心业务层节点: {stats.get('business_layer_nodes', [])[:5]}",
                impact="核心业务层直接影响基金计算、榜单、自选等核心功能，异常会导致用户可见的功能故障",
                mitigation="1. 对所有涉及的业务层节点执行专项测试；2. 验证核心计算结果的准确性；3. 对比变更前后的数据一致性；4. 准备数据回滚方案",
            ))
        elif business_count > 0:
            risk_counter += 1
            risks.append(RiskItem(
                id=f"{change_id_prefix}-{risk_counter:03d}",
                level="MEDIUM",
                description=f"变更涉及 {business_count} 个核心业务层节点",
                impact="业务层节点变更可能影响核心功能逻辑",
                mitigation="1. 针对涉及的业务节点执行功能测试；2. 验证业务流程端到端正确性",
            ))

        # 规则3：修改神经网络本身
        neural_nodes = {"function_graph", "change_gateway", "function_graph_page"}
        core_neural = [n for n in core_nodes if n in neural_nodes]
        if core_neural:
            risk_counter += 1
            risks.append(RiskItem(
                id=f"{change_id_prefix}-{risk_counter:03d}",
                level="HIGH",
                description=f"变更直接修改神经网络核心节点: {core_neural}",
                impact="神经网络本身的变更会影响所有后续网关分析的准确性，可能导致影响范围分析错误",
                mitigation="1. 更新后验证神经网络的节点数和边数合理性；2. 备份原function_graph.json；3. 执行分析结果一致性校验；4. 提供手动回滚功能",
            ))

        # 规则4：涉及数据库操作
        if db_count > 0:
            risk_counter += 1
            risks.append(RiskItem(
                id=f"{change_id_prefix}-{risk_counter:03d}",
                level="MEDIUM",
                description=f"变更影响 {db_count} 个数据库相关节点: {stats.get('db_related_nodes', [])[:5]}",
                impact="数据库操作变更可能导致数据不一致、性能下降或数据丢失",
                mitigation="1. 验证数据库操作的事务完整性；2. 检查SQL性能和索引使用；3. 确认数据迁移和回滚方案；4. 备份数据库",
            ))

        # 规则5：涉及API接口
        if api_count > 0:
            risk_counter += 1
            risks.append(RiskItem(
                id=f"{change_id_prefix}-{risk_counter:03d}",
                level="MEDIUM",
                description=f"变更影响 {api_count} 个API接口相关节点: {stats.get('api_related_nodes', [])[:5]}",
                impact="API接口变更可能导致前端调用失败、接口兼容性问题或性能下降",
                mitigation="1. 验证API接口的向后兼容性；2. 检查接口返回格式和状态码；3. 执行接口性能测试；4. 更新API文档",
            ))

        # 规则6：深度传播过远
        if (max_f_depth >= 3 or max_b_depth >= 3) and total_affected > 10:
            risk_counter += 1
            risks.append(RiskItem(
                id=f"{change_id_prefix}-{risk_counter:03d}",
                level="MEDIUM",
                description=f"变更影响传播深度较远（正向最大深度{max_f_depth}，反向最大深度{max_b_depth}）",
                impact="深层传播路径上的间接影响容易被忽略，可能导致隐蔽的功能异常",
                mitigation="1. 追踪完整的传播路径并验证；2. 重点关注深度>=2的间接影响节点；3. 执行端到端链路测试",
            ))

        # 规则7：上下游双向影响
        if len(forward) > 5 and len(backward) > 5:
            risk_counter += 1
            risks.append(RiskItem(
                id=f"{change_id_prefix}-{risk_counter:03d}",
                level="MEDIUM",
                description=f"变更同时产生较大的正向（{len(forward)}节点）和反向（{len(backward)}节点）影响",
                impact="双向影响意味着变更处于核心位置，上下游都可能受影响，测试复杂度高",
                mitigation="1. 分别验证正向和反向影响路径；2. 确认变更节点的接口契约；3. 执行完整的依赖链路测试",
            ))

        # 按风险等级降序排列
        risks.sort(
            key=lambda r: self._RISK_LEVEL_ORDER.get(r.level, 0),
            reverse=True,
        )

        # 根据风险阈值过滤
        threshold_order = self._RISK_LEVEL_ORDER.get(self.risk_threshold, 0)
        filtered_risks = [
            r for r in risks
            if self._RISK_LEVEL_ORDER.get(r.level, 0) >= threshold_order
        ]

        logger.info(
            "Risk identification: total=%d, after threshold=%d (threshold=%s)",
            len(risks),
            len(filtered_risks),
            self.risk_threshold,
        )

        return filtered_risks

    # ========================================================
    # 检查清单生成
    # ========================================================

    def _generate_checklist(
        self,
        change_id: str,
        core_nodes: List[str],
        risks: List[RiskItem],
        stats: Dict[str, Any],
    ) -> List[ChecklistItem]:
        """
        根据影响范围和风险点自动生成检查清单

        检查清单分级：
        - must（必须）：高风险项对应的检查，核心功能验证
        - should（应该）：中风险项对应的检查，重要功能验证
        - optional（可选）：低风险项对应的检查，优化建议

        检查阶段：
        - modify：修改阶段检查
        - verify：验证阶段检查
        - update：神经网络更新阶段检查
        - record：记录阶段检查

        Args:
            change_id: 变更编号
            core_nodes: 核心节点列表
            risks: 识别出的风险点列表
            stats: 统计数据字典

        Returns:
            List[ChecklistItem]: 生成的检查清单列表
        """
        checklist: List[ChecklistItem] = []
        chk_counter = 0

        def _add_check(
            stage: str,
            description: str,
            priority: Literal["must", "should", "optional"],
        ) -> None:
            nonlocal chk_counter
            chk_counter += 1
            checklist.append(ChecklistItem(
                id=f"CHK-{change_id}-{chk_counter:03d}",
                stage=stage,
                description=description,
                priority=priority,
                status="pending",
                verified=False,
            ))

        has_high_risk = any(r.level == "HIGH" for r in risks)
        has_medium_risk = any(r.level == "MEDIUM" for r in risks)
        business_count = stats.get("business_layer_count", 0)
        db_count = stats.get("db_related_count", 0)
        api_count = stats.get("api_related_count", 0)
        total_affected = stats.get("total_affected_count", 0)

        # ===== 修改阶段检查 =====
        _add_check(
            "modify",
            f"确认核心节点 {core_nodes} 的代码修改已完成，且修改范围与变更描述一致",
            "must",
        )

        if total_affected > 10:
            _add_check(
                "modify",
                "评估是否需要拆分变更为多个小变更以降低风险",
                "should" if not has_high_risk else "must",
            )

        # ===== 验证阶段检查 =====
        _add_check(
            "verify",
            "执行核心功能冒烟测试，确认基本功能正常",
            "must",
        )

        if business_count > 0:
            _add_check(
                "verify",
                f"验证 {business_count} 个核心业务层节点的功能正确性，对比变更前后数据一致性",
                "must",
            )

        if db_count > 0:
            _add_check(
                "verify",
                "验证数据库操作的事务完整性和数据一致性，检查SQL性能",
                "must" if has_high_risk else "should",
            )

        if api_count > 0:
            _add_check(
                "verify",
                "验证API接口的向后兼容性，检查返回格式和状态码",
                "must" if has_high_risk else "should",
            )

        if total_affected > 10:
            _add_check(
                "verify",
                "执行影响范围内所有节点的定向回归测试",
                "must" if has_high_risk else "should",
            )
        else:
            _add_check(
                "verify",
                "执行影响范围内节点的基本功能验证",
                "should",
            )

        if has_high_risk:
            _add_check(
                "verify",
                "执行端到端链路测试，覆盖完整的正向和反向传播路径",
                "must",
            )

        _add_check(
            "verify",
            "确认无新增的已知问题（known_issues），或已有问题已记录",
            "should",
        )

        # ===== 神经网络更新阶段检查 =====
        neural_nodes = {"function_graph", "change_gateway", "function_graph_page"}
        if any(n in neural_nodes for n in core_nodes):
            _add_check(
                "update",
                "备份原function_graph.json，确认回滚方案可用",
                "must",
            )
            _add_check(
                "update",
                "更新后验证神经网络的节点数和边数合理性，执行一致性校验",
                "must",
            )
        else:
            _add_check(
                "update",
                "确认是否需要更新function_graph.json（新增/修改节点或边）",
                "should",
            )

        _add_check(
            "update",
            "更新function_graph.json中相关节点的last_verified和verify_result字段",
            "should",
        )

        # ===== 记录阶段检查 =====
        _add_check(
            "record",
            "记录变更到change_history.json，包含完整的变更信息和影响范围",
            "must",
        )

        _add_check(
            "record",
            "升级版本号（config.py中的APP_VERSION），确认版本号格式正确",
            "must",
        )

        if has_medium_risk or has_high_risk:
            _add_check(
                "record",
                "在变更记录中详细描述风险点和缓解措施的执行情况",
                "should",
            )

        _add_check(
            "record",
            "确认检查清单中所有must项已完成验证",
            "must",
        )

        logger.info(
            "Checklist generated: total=%d (must=%d, should=%d, optional=%d)",
            len(checklist),
            sum(1 for c in checklist if c.priority == "must"),
            sum(1 for c in checklist if c.priority == "should"),
            sum(1 for c in checklist if c.priority == "optional"),
        )

        return checklist

    # ========================================================
    # 报告生成
    # ========================================================

    def generate_report(self, result: GatewayAnalysisResult) -> str:
        """
        生成结构化文本报告

        报告包含：
        1. 变更信息（编号、标题、核心节点、分析时间）
        2. 影响范围（正向/反向节点数、深度分布、涉及文件）
        3. 风险点（按等级排列的风险详情）
        4. 检查清单（按阶段和优先级排列）
        5. 统计数据（层级分布、数据库/API影响等）

        Args:
            result: 网关分析结果

        Returns:
            str: 结构化文本报告
        """
        lines: List[str] = []
        sep = "=" * 80
        sub_sep = "-" * 60

        # 标题
        lines.append(sep)
        lines.append("Superpower 网关分析报告")
        lines.append(sep)

        # 1. 变更信息
        lines.append("")
        lines.append("【变更信息】")
        lines.append(sub_sep)
        lines.append(f"  变更编号: {result.change_id}")
        change_title = result.stats.get("change_title", "")
        if change_title:
            lines.append(f"  变更标题: {change_title}")
        lines.append(f"  核心节点: {', '.join(result.core_nodes)}")
        missing = result.stats.get("missing_nodes", [])
        if missing:
            lines.append(f"  缺失节点: {', '.join(missing)}（不在神经网络中，已跳过）")
        lines.append(f"  分析时间: {result.generated_at}")
        lines.append(f"  图谱版本: {result.stats.get('graph_version', 'unknown')}")

        # 2. 影响范围
        lines.append("")
        lines.append("【影响范围】")
        lines.append(sub_sep)
        lines.append(f"  正向影响（下游）: {result.stats.get('forward_node_count', 0)} 个节点")
        f_dist = result.stats.get("forward_depth_distribution", {})
        for depth, count in sorted(f_dist.items()):
            depth_nodes = [n for n, d in result.forward_depth.items() if d == depth]
            preview = ", ".join(sorted(depth_nodes)[:5])
            if len(depth_nodes) > 5:
                preview += f" ...等{len(depth_nodes)}个"
            lines.append(f"    深度{depth}: {count}个 - {preview}")

        lines.append(f"  反向影响（上游）: {result.stats.get('backward_node_count', 0)} 个节点")
        b_dist = result.stats.get("backward_depth_distribution", {})
        for depth, count in sorted(b_dist.items()):
            depth_nodes = [n for n, d in result.backward_depth.items() if d == depth]
            preview = ", ".join(sorted(depth_nodes)[:5])
            if len(depth_nodes) > 5:
                preview += f" ...等{len(depth_nodes)}个"
            lines.append(f"    深度{depth}: {count}个 - {preview}")

        lines.append(f"  总影响节点数: {result.stats.get('total_affected_count', 0)}")
        lines.append(f"  重叠节点数: {result.stats.get('overlap_count', 0)}")
        lines.append(f"  涉及代码文件: {result.stats.get('affected_file_count', 0)} 个")
        affected_files = result.stats.get("affected_code_files", [])
        if affected_files:
            for cf in affected_files[:10]:
                lines.append(f"    - {cf}")
            if len(affected_files) > 10:
                lines.append(f"    ...等{len(affected_files)}个文件")

        # 3. 风险点
        lines.append("")
        lines.append("【风险点】")
        lines.append(sub_sep)
        if not result.risks:
            lines.append("  未识别到风险点")
        else:
            for i, risk in enumerate(result.risks, 1):
                level_icon = {"HIGH": "!!!", "MEDIUM": "!!", "LOW": "!"}.get(risk.level, "?")
                lines.append(f"  {i}. [{risk.level}] {level_icon} {risk.id}")
                lines.append(f"     描述: {risk.description}")
                lines.append(f"     影响: {risk.impact}")
                lines.append(f"     缓解: {risk.mitigation}")
                lines.append("")

        # 4. 检查清单
        lines.append("【检查清单】")
        lines.append(sub_sep)
        stages = ["modify", "verify", "update", "record"]
        stage_names = {
            "modify": "修改阶段",
            "verify": "验证阶段",
            "update": "更新阶段",
            "record": "记录阶段",
        }
        for stage in stages:
            stage_items = [c for c in result.checklist if c.stage == stage]
            if not stage_items:
                continue
            lines.append(f"  ▶ {stage_names.get(stage, stage)}（{len(stage_items)}项）")
            for item in stage_items:
                priority_mark = {"must": "[必]", "should": "[应]", "optional": "[选]"}.get(item.priority, "[?]")
                status_mark = {"pending": "○", "in_progress": "◐", "completed": "●", "skipped": "⊘"}.get(item.status, "?")
                lines.append(f"    {status_mark} {priority_mark} {item.id}: {item.description}")
            lines.append("")

        # 5. 统计数据
        lines.append("【统计数据】")
        lines.append(sub_sep)
        layer_dist = result.stats.get("layer_distribution", {})
        lines.append(f"  层级分布:")
        for layer, count in sorted(layer_dist.items()):
            layer_name = layer
            for l in self.layers:
                if l.get("id") == layer:
                    layer_name = l.get("name", layer)
                    break
            lines.append(f"    - {layer_name} ({layer}): {count}个")

        lines.append(f"  核心业务层节点: {result.stats.get('business_layer_count', 0)}个")
        if result.stats.get("business_layer_nodes"):
            lines.append(f"    {', '.join(result.stats['business_layer_nodes'][:10])}")

        lines.append(f"  数据库相关节点: {result.stats.get('db_related_count', 0)}个")
        lines.append(f"  API接口相关节点: {result.stats.get('api_related_count', 0)}个")
        lines.append(f"  正向内部边数: {result.stats.get('forward_internal_edges', 0)}")
        lines.append(f"  反向内部边数: {result.stats.get('backward_internal_edges', 0)}")

        # 检查清单统计
        must_count = sum(1 for c in result.checklist if c.priority == "must")
        should_count = sum(1 for c in result.checklist if c.priority == "should")
        optional_count = sum(1 for c in result.checklist if c.priority == "optional")
        lines.append(f"  检查清单统计: 共{len(result.checklist)}项（必须{must_count}，应该{should_count}，可选{optional_count}）")

        lines.append("")
        lines.append(sep)
        lines.append("报告结束")
        lines.append(sep)

        return "\n".join(lines)

    # ========================================================
    # 序列化
    # ========================================================

    def to_dict(self, result: GatewayAnalysisResult) -> Dict[str, Any]:
        """
        将GatewayAnalysisResult序列化为字典

        注意：Set类型会转换为排序后的列表，便于JSON序列化。

        Args:
            result: 网关分析结果

        Returns:
            Dict[str, Any]: 可JSON序列化的字典
        """
        return {
            "change_id": result.change_id,
            "core_nodes": result.core_nodes,
            "forward_affected": sorted(result.forward_affected),
            "backward_affected": sorted(result.backward_affected),
            "forward_depth": result.forward_depth,
            "backward_depth": result.backward_depth,
            "risks": [asdict(r) for r in result.risks],
            "checklist": [asdict(c) for c in result.checklist],
            "stats": result.stats,
            "generated_at": result.generated_at,
        }
