"""
神经网络更新器（NeuralUpdater）单元测试

测试覆盖：
- 初始化和图加载
- parse_file解析已知文件
- 解析结果包含函数和调用关系
- parse_directory目录解析
- diff_with_graph差异分析
- backup_graph备份（使用tmp_path）
- validate_update验证
- rollback回滚（使用临时文件）
- run半自动模式不修改真实文件
- get_graph_stats统计

注意：所有可能修改真实文件的操作（apply_update、run的auto_confirm=True模式）
均使用临时路径或mock，不直接调用真实文件修改。
"""
import os
import sys
import json
import shutil
import pytest

# 确保backend目录在sys.path中
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from modules.superpower.neural_updater import (
    NeuralUpdater,
    FunctionInfo,
    CallRelation,
    ParseResult,
    UpdatePreview,
    UpdateResult,
)

# 真实图谱路径
GRAPH_PATH = os.path.join(_BACKEND_DIR, "docs", "function_graph.json")
# 已知的可解析文件
KNOWN_PY_FILE = os.path.join(_BACKEND_DIR, "modules", "superpower", "gateway_analyzer.py")


@pytest.fixture
def temp_graph(tmp_path):
    """创建临时图谱文件副本，返回路径。"""
    temp_graph_path = tmp_path / "function_graph.json"
    shutil.copy2(GRAPH_PATH, temp_graph_path)
    return str(temp_graph_path)


@pytest.fixture
def updater(temp_graph, tmp_path):
    """创建NeuralUpdater实例，使用临时图谱和备份目录。"""
    backup_dir = str(tmp_path / "backups")
    return NeuralUpdater(
        graph_path=temp_graph,
        code_root=os.path.join(_BACKEND_DIR, "modules"),
        backup_dir=backup_dir,
        auto_confirm=False,
    )


@pytest.fixture
def parse_result(updater):
    """解析已知文件，返回ParseResult。"""
    return updater.parse_file(KNOWN_PY_FILE)


class TestNeuralUpdaterInit:
    """测试初始化和图加载。"""

    @pytest.mark.unit
    def test_init_loads_graph(self, temp_graph, tmp_path):
        """测试初始化成功加载图谱。"""
        updater = NeuralUpdater(
            graph_path=temp_graph,
            backup_dir=str(tmp_path / "bk"),
        )
        assert updater.graph is not None
        assert "nodes" in updater.graph
        assert "edges" in updater.graph

    @pytest.mark.unit
    def test_init_creates_backup_dir(self, temp_graph, tmp_path):
        """测试初始化自动创建备份目录。"""
        backup_dir = str(tmp_path / "new_backups")
        NeuralUpdater(graph_path=temp_graph, backup_dir=backup_dir)
        assert os.path.isdir(backup_dir)

    @pytest.mark.unit
    def test_init_default_auto_confirm_false(self, temp_graph, tmp_path):
        """测试默认auto_confirm为False。"""
        updater = NeuralUpdater(
            graph_path=temp_graph,
            backup_dir=str(tmp_path / "bk"),
        )
        assert updater.auto_confirm is False

    @pytest.mark.unit
    def test_init_nonexistent_graph_creates_empty(self, tmp_path):
        """测试图谱文件不存在时初始化为空图谱。"""
        fake_graph = str(tmp_path / "nonexistent.json")
        backup_dir = str(tmp_path / "bk")
        updater = NeuralUpdater(graph_path=fake_graph, backup_dir=backup_dir)
        assert updater.graph.get("nodes") == []
        assert updater.graph.get("edges") == []


class TestNeuralUpdaterParseFile:
    """测试parse_file文件解析。"""

    @pytest.mark.unit
    def test_parse_file_returns_parse_result(self, updater):
        """测试parse_file返回ParseResult实例。"""
        result = updater.parse_file(KNOWN_PY_FILE)
        assert isinstance(result, ParseResult)

    @pytest.mark.unit
    def test_parse_file_contains_functions(self, parse_result):
        """测试解析结果包含函数定义。"""
        assert len(parse_result.functions) > 0
        for func in parse_result.functions:
            assert isinstance(func, FunctionInfo)
            assert func.name is not None
            assert func.qualified_name is not None

    @pytest.mark.unit
    def test_parse_file_contains_calls(self, parse_result):
        """测试解析结果包含调用关系。"""
        assert len(parse_result.calls) > 0
        for call in parse_result.calls:
            assert isinstance(call, CallRelation)
            assert call.caller is not None
            assert call.callee is not None

    @pytest.mark.unit
    def test_parse_file_files_scanned_count(self, parse_result):
        """测试解析结果files_scanned为1。"""
        assert parse_result.files_scanned == 1

    @pytest.mark.unit
    def test_parse_file_known_class_detected(self, parse_result):
        """测试解析结果中检测到GatewayAnalyzer类。"""
        class_methods = [f for f in parse_result.functions if f.is_method]
        assert len(class_methods) > 0
        gateway_methods = [
            f for f in class_methods
            if "GatewayAnalyzer" in f.qualified_name
        ]
        assert len(gateway_methods) > 0

    @pytest.mark.unit
    def test_parse_file_function_has_params(self, parse_result):
        """测试解析出的函数包含参数信息。"""
        funcs_with_params = [f for f in parse_result.functions if f.params]
        assert len(funcs_with_params) > 0

    @pytest.mark.unit
    def test_parse_file_handles_syntax_error(self, updater, tmp_path):
        """测试解析语法错误的文件时记录错误不崩溃。"""
        bad_file = tmp_path / "bad_syntax.py"
        bad_file.write_text("def foo(:\n    pass\n", encoding="utf-8")
        result = updater.parse_file(str(bad_file))
        assert len(result.errors) > 0

    @pytest.mark.unit
    def test_parse_file_nonexistent_returns_error(self, updater):
        """测试解析不存在的文件时返回错误。"""
        result = updater.parse_file("/nonexistent/file.py")
        assert len(result.errors) > 0
        assert result.files_scanned == 1


class TestNeuralUpdaterParseDirectory:
    """测试parse_directory目录解析。"""

    @pytest.mark.unit
    def test_parse_directory_returns_result(self, updater, tmp_path):
        """测试parse_directory返回ParseResult。"""
        # 创建临时目录包含一个Python文件
        test_dir = tmp_path / "test_module"
        test_dir.mkdir()
        (test_dir / "sample.py").write_text(
            "def hello():\n    return 'world'\n",
            encoding="utf-8",
        )
        result = updater.parse_directory(str(test_dir))
        assert isinstance(result, ParseResult)
        assert result.files_scanned >= 1

    @pytest.mark.unit
    def test_parse_directory_nonexistent_returns_error(self, updater):
        """测试解析不存在的目录时返回错误。"""
        result = updater.parse_directory("/nonexistent/dir")
        assert len(result.errors) > 0

    @pytest.mark.unit
    def test_parse_directory_merges_multiple_files(self, updater, tmp_path):
        """测试parse_directory合并多个文件的解析结果。"""
        test_dir = tmp_path / "multi_module"
        test_dir.mkdir()
        (test_dir / "file_a.py").write_text(
            "def func_a():\n    pass\n", encoding="utf-8"
        )
        (test_dir / "file_b.py").write_text(
            "def func_b():\n    pass\n", encoding="utf-8"
        )
        result = updater.parse_directory(str(test_dir))
        func_names = [f.name for f in result.functions]
        assert "func_a" in func_names
        assert "func_b" in func_names
        assert result.files_scanned == 2


class TestNeuralUpdaterDiff:
    """测试diff_with_graph差异分析。"""

    @pytest.mark.unit
    def test_diff_returns_update_preview(self, updater, parse_result):
        """测试diff_with_graph返回UpdatePreview实例。"""
        preview = updater.diff_with_graph(parse_result)
        assert isinstance(preview, UpdatePreview)

    @pytest.mark.unit
    def test_diff_has_node_counts(self, updater, parse_result):
        """测试差异预览包含节点数前后对比。"""
        preview = updater.diff_with_graph(parse_result)
        assert preview.node_count_before > 0
        assert preview.node_count_after > 0

    @pytest.mark.unit
    def test_diff_has_edge_counts(self, updater, parse_result):
        """测试差异预览包含边数前后对比。"""
        preview = updater.diff_with_graph(parse_result)
        assert preview.edge_count_before > 0
        assert preview.edge_count_after > 0

    @pytest.mark.unit
    def test_diff_node_count_after_matches_calculation(self, updater, parse_result):
        """测试更新后节点数 = 前 + 新增 - 删除。"""
        preview = updater.diff_with_graph(parse_result)
        expected = (
            preview.node_count_before
            + len(preview.added_nodes)
            - len(preview.removed_nodes)
        )
        assert preview.node_count_after == expected

    @pytest.mark.unit
    def test_diff_edge_count_after_matches_calculation(self, updater, parse_result):
        """测试更新后边数 = 前 + 新增 - 删除。"""
        preview = updater.diff_with_graph(parse_result)
        expected = (
            preview.edge_count_before
            + len(preview.added_edges)
            - len(preview.removed_edges)
        )
        assert preview.edge_count_after == expected

    @pytest.mark.unit
    def test_diff_with_empty_parse_result(self, updater):
        """测试空解析结果的差异分析。"""
        empty_result = ParseResult()
        preview = updater.diff_with_graph(empty_result)
        assert isinstance(preview, UpdatePreview)
        assert preview.node_count_after == preview.node_count_before


class TestNeuralUpdaterBackup:
    """测试backup_graph备份功能。"""

    @pytest.mark.unit
    def test_backup_creates_file(self, updater):
        """测试backup_graph创建备份文件。"""
        backup_path = updater.backup_graph()
        assert os.path.exists(backup_path)
        assert backup_path.endswith(".json")

    @pytest.mark.unit
    def test_backup_file_is_valid_json(self, updater):
        """测试备份文件是有效的JSON。"""
        backup_path = updater.backup_graph()
        with open(backup_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert "nodes" in data
        assert "edges" in data

    @pytest.mark.unit
    def test_backup_preserves_content(self, updater, temp_graph):
        """测试备份内容与原文件一致。"""
        backup_path = updater.backup_graph()
        with open(temp_graph, "r", encoding="utf-8") as f:
            original = json.load(f)
        with open(backup_path, "r", encoding="utf-8") as f:
            backed = json.load(f)
        assert len(original["nodes"]) == len(backed["nodes"])
        assert len(original["edges"]) == len(backed["edges"])


class TestNeuralUpdaterValidate:
    """测试validate_update验证功能。"""

    @pytest.mark.unit
    def test_validate_returns_tuple(self, updater, parse_result):
        """测试validate_update返回(布尔, 列表)元组。"""
        preview = updater.diff_with_graph(parse_result)
        result = updater.validate_update(preview)
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert isinstance(result[0], bool)
        assert isinstance(result[1], list)

    @pytest.mark.unit
    def test_validate_empty_preview_passes(self, updater):
        """测试空预览（无变更）通过验证。"""
        preview = UpdatePreview(
            node_count_before=53,
            node_count_after=53,
            edge_count_before=135,
            edge_count_after=135,
        )
        passed, errors = updater.validate_update(preview)
        assert passed is True
        assert len(errors) == 0

    @pytest.mark.unit
    def test_validate_detects_duplicate_new_ids(self, updater):
        """测试验证检测到新增节点重复ID。"""
        preview = UpdatePreview(
            node_count_before=10,
            node_count_after=12,
            edge_count_before=20,
            edge_count_after=20,
            added_nodes=[{"id": "dup_node"}, {"id": "dup_node"}],
        )
        passed, errors = updater.validate_update(preview)
        assert passed is False
        assert any("重复ID" in e for e in errors)


class TestNeuralUpdaterRollback:
    """测试rollback回滚功能。"""

    @pytest.mark.unit
    def test_rollback_from_valid_backup(self, updater, temp_graph):
        """测试从有效备份回滚成功。"""
        # 先备份
        backup_path = updater.backup_graph()
        # 修改图谱内容
        with open(temp_graph, "w", encoding="utf-8") as f:
            json.dump({"nodes": [], "edges": [], "meta": {}}, f)
        # 回滚
        result = updater.rollback(backup_path)
        assert result is True
        # 验证内容恢复
        with open(temp_graph, "r", encoding="utf-8") as f:
            restored = json.load(f)
        assert len(restored.get("nodes", [])) > 0

    @pytest.mark.unit
    def test_rollback_nonexistent_backup_returns_false(self, updater):
        """测试从不存在的备份回滚返回False。"""
        result = updater.rollback("/nonexistent/backup.json")
        assert result is False


class TestNeuralUpdaterRun:
    """测试run半自动模式。"""

    @pytest.mark.unit
    def test_run_semiauto_returns_update_result(self, updater):
        """测试run(auto_confirm=False)返回UpdateResult。"""
        result = updater.run(modified_files=[KNOWN_PY_FILE])
        assert isinstance(result, UpdateResult)

    @pytest.mark.unit
    def test_run_semiauto_does_not_modify_graph(self, updater, temp_graph):
        """测试run(auto_confirm=False)不修改图谱文件。"""
        # 记录修改前的内容
        with open(temp_graph, "r", encoding="utf-8") as f:
            before = json.load(f)
        before_nodes = len(before["nodes"])
        before_edges = len(before["edges"])

        # 执行半自动run
        updater.run(modified_files=[KNOWN_PY_FILE])

        # 验证图谱未被修改
        with open(temp_graph, "r", encoding="utf-8") as f:
            after = json.load(f)
        assert len(after["nodes"]) == before_nodes
        assert len(after["edges"]) == before_edges

    @pytest.mark.unit
    def test_run_semiauto_success_false_with_preview(self, updater):
        """测试半自动模式下有变更时success=False但preview有值。"""
        result = updater.run(modified_files=[KNOWN_PY_FILE])
        # 因为auto_confirm=False，有变更时success应为False
        if result.preview and (
            result.preview.added_nodes
            or result.preview.removed_nodes
            or result.preview.modified_nodes
            or result.preview.added_edges
        ):
            assert result.success is False
            assert result.preview is not None

    @pytest.mark.unit
    def test_run_with_nonexistent_files(self, updater):
        """测试run传入不存在的文件时记录错误不崩溃。"""
        result = updater.run(modified_files=["/nonexistent/file.py"])
        assert isinstance(result, UpdateResult)

    @pytest.mark.unit
    def test_run_no_modified_files_parses_directory(self, updater):
        """测试run不指定modified_files时解析整个code_root目录。"""
        # 使用临时code_root避免解析大量文件
        import tempfile
        with tempfile.TemporaryDirectory() as tmp_code_root:
            # 创建一个简单的Python文件
            sample = os.path.join(tmp_code_root, "sample.py")
            with open(sample, "w", encoding="utf-8") as f:
                f.write("def test_func():\n    pass\n")
            updater.code_root = tmp_code_root
            result = updater.run()
            assert isinstance(result, UpdateResult)


class TestNeuralUpdaterStats:
    """测试get_graph_stats统计功能。"""

    @pytest.mark.unit
    def test_get_graph_stats_returns_dict(self, updater):
        """测试get_graph_stats返回字典。"""
        stats = updater.get_graph_stats()
        assert isinstance(stats, dict)

    @pytest.mark.unit
    def test_stats_contains_node_count(self, updater):
        """测试统计包含节点数。"""
        stats = updater.get_graph_stats()
        assert "node_count" in stats
        assert stats["node_count"] > 0

    @pytest.mark.unit
    def test_stats_contains_edge_count(self, updater):
        """测试统计包含边数。"""
        stats = updater.get_graph_stats()
        assert "edge_count" in stats
        assert stats["edge_count"] > 0

    @pytest.mark.unit
    def test_stats_contains_layer_distribution(self, updater):
        """测试统计包含层级分布。"""
        stats = updater.get_graph_stats()
        assert "layer_distribution" in stats
        assert isinstance(stats["layer_distribution"], dict)
        assert len(stats["layer_distribution"]) > 0

    @pytest.mark.unit
    def test_stats_contains_version(self, updater):
        """测试统计包含图谱版本。"""
        stats = updater.get_graph_stats()
        assert "version" in stats
        assert stats["version"] != "unknown"
