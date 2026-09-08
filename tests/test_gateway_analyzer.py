"""
网关分析器（GatewayAnalyzer）单元测试

测试覆盖：
- 初始化和图加载
- BFS正向/反向传播分析
- 影响范围统计
- 风险点识别
- 检查清单生成
- 报告生成
- 序列化to_dict
- 自定义参数（max_depth）
- 边界情况（缺失节点、空核心节点）

所有测试使用真实的function_graph.json（只读操作），不修改任何数据文件。
"""
import os
import sys
import json
import pytest

# 确保backend目录在sys.path中
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from modules.superpower.gateway_analyzer import (
    GatewayAnalyzer,
    GatewayAnalysisResult,
    RiskItem,
    ChecklistItem,
)

# 真实图谱路径（相对于backend目录）
GRAPH_PATH = os.path.join(_BACKEND_DIR, "docs", "function_graph.json")


@pytest.fixture
def analyzer():
    """创建GatewayAnalyzer实例，使用真实图谱（只读）。"""
    return GatewayAnalyzer(graph_path=GRAPH_PATH, max_depth=3, risk_threshold="MEDIUM")


@pytest.fixture
def analysis_result(analyzer):
    """对change_gateway节点执行分析，返回结果。"""
    return analyzer.analyze(
        change_id="CHG-TEST-GATEWAY-001",
        core_nodes=["change_gateway"],
        change_title="测试网关分析",
    )


class TestGatewayAnalyzerInit:
    """测试初始化和图加载。"""

    @pytest.mark.unit
    def test_init_loads_graph_successfully(self):
        """测试初始化成功加载function_graph.json。"""
        analyzer = GatewayAnalyzer(graph_path=GRAPH_PATH)
        assert analyzer.graph is not None
        assert len(analyzer.nodes) > 0
        assert len(analyzer.edges) > 0

    @pytest.mark.unit
    def test_init_builds_adjacency_tables(self):
        """测试初始化构建正向和反向邻接表。"""
        analyzer = GatewayAnalyzer(graph_path=GRAPH_PATH)
        assert hasattr(analyzer, "adj")
        assert hasattr(analyzer, "rev_adj")
        assert len(analyzer.adj) > 0
        assert len(analyzer.rev_adj) > 0

    @pytest.mark.unit
    def test_init_node_info_lookup(self):
        """测试node_info字典可按id快速查找节点。"""
        analyzer = GatewayAnalyzer(graph_path=GRAPH_PATH)
        assert "change_gateway" in analyzer.node_info
        node = analyzer.node_info["change_gateway"]
        assert node.get("id") == "change_gateway"

    @pytest.mark.unit
    def test_init_invalid_path_raises(self):
        """测试不存在的图谱路径抛出FileNotFoundError。"""
        with pytest.raises(FileNotFoundError):
            GatewayAnalyzer(graph_path="/nonexistent/path/graph.json")

    @pytest.mark.unit
    def test_init_custom_parameters(self):
        """测试自定义max_depth和risk_threshold参数。"""
        analyzer = GatewayAnalyzer(
            graph_path=GRAPH_PATH, max_depth=1, risk_threshold="HIGH"
        )
        assert analyzer.max_depth == 1
        assert analyzer.risk_threshold == "HIGH"


class TestGatewayAnalyzerBFS:
    """测试BFS正向/反向传播。"""

    @pytest.mark.unit
    def test_forward_affected_contains_core_node(self, analysis_result):
        """测试正向影响集合包含核心节点本身。"""
        assert "change_gateway" in analysis_result.forward_affected

    @pytest.mark.unit
    def test_backward_affected_contains_core_node(self, analysis_result):
        """测试反向影响集合包含核心节点本身。"""
        assert "change_gateway" in analysis_result.backward_affected

    @pytest.mark.unit
    def test_forward_depth_maps_core_to_zero(self, analysis_result):
        """测试核心节点的正向传播深度为0。"""
        assert analysis_result.forward_depth["change_gateway"] == 0

    @pytest.mark.unit
    def test_backward_depth_maps_core_to_zero(self, analysis_result):
        """测试核心节点的反向传播深度为0。"""
        assert analysis_result.backward_depth["change_gateway"] == 0

    @pytest.mark.unit
    def test_max_depth_limits_propagation(self):
        """测试max_depth=1限制传播深度不超过1。"""
        analyzer = GatewayAnalyzer(graph_path=GRAPH_PATH, max_depth=1)
        result = analyzer.analyze("CHG-DEPTH-TEST", ["change_gateway"])
        max_fwd = max(result.forward_depth.values()) if result.forward_depth else 0
        max_bwd = max(result.backward_depth.values()) if result.backward_depth else 0
        assert max_fwd <= 1
        assert max_bwd <= 1

    @pytest.mark.unit
    def test_bfs_with_multiple_core_nodes(self, analyzer):
        """测试多个核心节点的BFS传播。"""
        result = analyzer.analyze(
            "CHG-MULTI-001", ["change_gateway", "gateway_analyzer"]
        )
        assert "change_gateway" in result.forward_affected
        assert "gateway_analyzer" in result.forward_affected

    @pytest.mark.unit
    def test_analyze_with_partially_missing_nodes(self, analyzer):
        """测试部分核心节点不存在于图中时仍能分析。"""
        result = analyzer.analyze(
            "CHG-MISSING-001",
            ["change_gateway", "nonexistent_node_xyz"],
        )
        assert "change_gateway" in result.core_nodes
        assert "nonexistent_node_xyz" not in result.core_nodes
        assert "nonexistent_node_xyz" in result.stats.get("missing_nodes", [])

    @pytest.mark.unit
    def test_analyze_empty_core_nodes_raises(self, analyzer):
        """测试空核心节点列表抛出ValueError。"""
        with pytest.raises(ValueError, match="core_nodes不能为空"):
            analyzer.analyze("CHG-EMPTY-001", [])

    @pytest.mark.unit
    def test_analyze_all_missing_nodes_raises(self, analyzer):
        """测试所有核心节点都不存在时抛出ValueError。"""
        with pytest.raises(ValueError, match="所有核心节点都不存在"):
            analyzer.analyze("CHG-ALLMISS-001", ["node_a", "node_b"])


class TestGatewayAnalyzerStats:
    """测试影响范围统计。"""

    @pytest.mark.unit
    def test_stats_contains_forward_count(self, analysis_result):
        """测试统计包含正向影响节点数。"""
        assert "forward_node_count" in analysis_result.stats
        assert analysis_result.stats["forward_node_count"] > 0

    @pytest.mark.unit
    def test_stats_contains_backward_count(self, analysis_result):
        """测试统计包含反向影响节点数。"""
        assert "backward_node_count" in analysis_result.stats
        assert analysis_result.stats["backward_node_count"] > 0

    @pytest.mark.unit
    def test_stats_total_affected(self, analysis_result):
        """测试统计包含总影响节点数。"""
        assert "total_affected_count" in analysis_result.stats
        total = analysis_result.stats["total_affected_count"]
        fwd = analysis_result.stats["forward_node_count"]
        bwd = analysis_result.stats["backward_node_count"]
        assert total <= fwd + bwd  # 有重叠时total < fwd + bwd

    @pytest.mark.unit
    def test_stats_depth_distribution(self, analysis_result):
        """测试统计包含深度分布。"""
        assert "forward_depth_distribution" in analysis_result.stats
        assert "backward_depth_distribution" in analysis_result.stats
        fwd_dist = analysis_result.stats["forward_depth_distribution"]
        assert 0 in fwd_dist  # 核心节点深度为0

    @pytest.mark.unit
    def test_stats_layer_distribution(self, analysis_result):
        """测试统计包含层级分布。"""
        assert "layer_distribution" in analysis_result.stats
        assert isinstance(analysis_result.stats["layer_distribution"], dict)

    @pytest.mark.unit
    def test_stats_affected_code_files(self, analysis_result):
        """测试统计包含受影响代码文件列表。"""
        assert "affected_code_files" in analysis_result.stats
        assert "affected_file_count" in analysis_result.stats
        assert isinstance(analysis_result.stats["affected_code_files"], list)

    @pytest.mark.unit
    def test_stats_graph_version(self, analysis_result):
        """测试统计包含图谱版本信息。"""
        assert "graph_version" in analysis_result.stats
        assert analysis_result.stats["graph_version"] != "unknown"


class TestGatewayAnalyzerRisks:
    """测试风险点识别。"""

    @pytest.mark.unit
    def test_risks_is_list(self, analysis_result):
        """测试风险点返回列表。"""
        assert isinstance(analysis_result.risks, list)

    @pytest.mark.unit
    def test_risks_contain_riskitem_instances(self, analysis_result):
        """测试风险点列表中的元素是RiskItem实例。"""
        assert len(analysis_result.risks) > 0
        for risk in analysis_result.risks:
            assert isinstance(risk, RiskItem)

    @pytest.mark.unit
    def test_risk_item_has_required_fields(self, analysis_result):
        """测试每个RiskItem包含必填字段。"""
        for risk in analysis_result.risks:
            assert risk.id is not None
            assert risk.level in ("HIGH", "MEDIUM", "LOW")
            assert risk.description is not None
            assert risk.impact is not None
            assert risk.mitigation is not None

    @pytest.mark.unit
    def test_risks_sorted_by_level_descending(self, analysis_result):
        """测试风险点按等级降序排列。"""
        level_order = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}
        levels = [level_order[r.level] for r in analysis_result.risks]
        assert levels == sorted(levels, reverse=True)

    @pytest.mark.unit
    def test_neural_node_modification_triggers_high_risk(self, analyzer):
        """测试修改神经网络核心节点时识别出HIGH风险。"""
        result = analyzer.analyze("CHG-NEURAL-001", ["change_gateway"])
        high_risks = [r for r in result.risks if r.level == "HIGH"]
        # change_gateway属于神经网络核心节点集合，应触发HIGH风险
        neural_descriptions = [
            r for r in high_risks
            if "神经网络" in r.description or "change_gateway" in r.description
        ]
        assert len(neural_descriptions) > 0

    @pytest.mark.unit
    def test_risk_threshold_filters_low_risks(self):
        """测试risk_threshold=HIGH时过滤掉LOW和MEDIUM风险。"""
        analyzer = GatewayAnalyzer(graph_path=GRAPH_PATH, risk_threshold="HIGH")
        result = analyzer.analyze("CHG-THRESH-001", ["change_gateway"])
        for risk in result.risks:
            assert risk.level == "HIGH"


class TestGatewayAnalyzerChecklist:
    """测试检查清单生成。"""

    @pytest.mark.unit
    def test_checklist_is_list(self, analysis_result):
        """测试检查清单返回列表。"""
        assert isinstance(analysis_result.checklist, list)
        assert len(analysis_result.checklist) > 0

    @pytest.mark.unit
    def test_checklist_items_are_checklistitem(self, analysis_result):
        """测试检查清单项是ChecklistItem实例。"""
        for item in analysis_result.checklist:
            assert isinstance(item, ChecklistItem)

    @pytest.mark.unit
    def test_checklist_has_must_items(self, analysis_result):
        """测试检查清单包含must优先级项。"""
        must_items = [c for c in analysis_result.checklist if c.priority == "must"]
        assert len(must_items) > 0

    @pytest.mark.unit
    def test_checklist_has_should_items(self, analysis_result):
        """测试检查清单包含should优先级项。"""
        should_items = [c for c in analysis_result.checklist if c.priority == "should"]
        assert len(should_items) > 0

    @pytest.mark.unit
    def test_checklist_covers_all_stages(self, analysis_result):
        """测试检查清单覆盖modify/verify/update/record四个阶段。"""
        stages = {c.stage for c in analysis_result.checklist}
        assert "modify" in stages
        assert "verify" in stages
        assert "update" in stages
        assert "record" in stages

    @pytest.mark.unit
    def test_checklist_items_default_status_pending(self, analysis_result):
        """测试新生成的检查项默认状态为pending。"""
        for item in analysis_result.checklist:
            assert item.status == "pending"
            assert item.verified is False

    @pytest.mark.unit
    def test_checklist_item_ids_unique(self, analysis_result):
        """测试检查项ID唯一。"""
        ids = [c.id for c in analysis_result.checklist]
        assert len(ids) == len(set(ids))


class TestGatewayAnalyzerReport:
    """测试报告生成。"""

    @pytest.mark.unit
    def test_report_is_nonempty_string(self, analyzer, analysis_result):
        """测试生成的报告是非空字符串。"""
        report = analyzer.generate_report(analysis_result)
        assert isinstance(report, str)
        assert len(report) > 0

    @pytest.mark.unit
    def test_report_contains_change_info(self, analyzer, analysis_result):
        """测试报告包含变更信息。"""
        report = analyzer.generate_report(analysis_result)
        assert "CHG-TEST-GATEWAY-001" in report
        assert "change_gateway" in report

    @pytest.mark.unit
    def test_report_contains_risk_section(self, analyzer, analysis_result):
        """测试报告包含风险点章节。"""
        report = analyzer.generate_report(analysis_result)
        assert "风险点" in report

    @pytest.mark.unit
    def test_report_contains_checklist_section(self, analyzer, analysis_result):
        """测试报告包含检查清单章节。"""
        report = analyzer.generate_report(analysis_result)
        assert "检查清单" in report

    @pytest.mark.unit
    def test_report_contains_stats_section(self, analyzer, analysis_result):
        """测试报告包含统计数据章节。"""
        report = analyzer.generate_report(analysis_result)
        assert "统计数据" in report


class TestGatewayAnalyzerSerialization:
    """测试to_dict序列化。"""

    @pytest.mark.unit
    def test_to_dict_returns_dict(self, analyzer, analysis_result):
        """测试to_dict返回字典。"""
        data = analyzer.to_dict(analysis_result)
        assert isinstance(data, dict)

    @pytest.mark.unit
    def test_to_dict_contains_all_fields(self, analyzer, analysis_result):
        """测试to_dict包含所有必要字段。"""
        data = analyzer.to_dict(analysis_result)
        required_fields = [
            "change_id", "core_nodes", "forward_affected",
            "backward_affected", "forward_depth", "backward_depth",
            "risks", "checklist", "stats", "generated_at",
        ]
        for field in required_fields:
            assert field in data, f"缺少字段: {field}"

    @pytest.mark.unit
    def test_to_dict_sets_are_sorted_lists(self, analyzer, analysis_result):
        """测试to_dict中Set类型转换为排序后的列表。"""
        data = analyzer.to_dict(analysis_result)
        assert isinstance(data["forward_affected"], list)
        assert isinstance(data["backward_affected"], list)
        assert data["forward_affected"] == sorted(data["forward_affected"])

    @pytest.mark.unit
    def test_to_dict_json_serializable(self, analyzer, analysis_result):
        """测试to_dict结果可被JSON序列化。"""
        data = analyzer.to_dict(analysis_result)
        json_str = json.dumps(data, ensure_ascii=False)
        assert isinstance(json_str, str)
        assert len(json_str) > 0

    @pytest.mark.unit
    def test_to_dict_risks_are_dicts(self, analyzer, analysis_result):
        """测试to_dict中risks转换为字典列表。"""
        data = analyzer.to_dict(analysis_result)
        for risk in data["risks"]:
            assert isinstance(risk, dict)
            assert "id" in risk
            assert "level" in risk
