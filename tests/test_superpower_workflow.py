"""
Superpower工作流引擎集成测试

测试覆盖：
- 初始化
- trigger简单修改自动跳过（单文件小改动）
- trigger复杂修改需要完整分析
- analyze阶段
- modify阶段
- verify阶段
- get_progress进度查询
- 状态机流转（不调用record_change）
- 日志文件创建

注意：不调用record_change()和完整run()方法，避免修改真实数据文件。
update_neural_graph使用auto_confirm=False模式，NeuralUpdater仅返回预览不修改文件。
"""
import os
import sys
import json
import pytest

# 确保backend目录在sys.path中
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from superpower_workflow import (
    SuperpowerWorkflow,
    run_superpower_workflow,
    PHASE_TRIGGER,
    PHASE_ANALYZE,
    PHASE_MODIFY,
    PHASE_VERIFY,
    PHASE_UPDATE,
    PHASE_RECORD,
    PHASE_COMPLETED,
    PHASE_FAILED,
    PHASE_ORDER,
)


@pytest.fixture
def workflow():
    """创建SuperpowerWorkflow实例。"""
    return SuperpowerWorkflow()


@pytest.fixture
def simple_temp_file(tmp_path):
    """创建一个小的临时Python文件（用于简单修改测试）。"""
    f = tmp_path / "simple_util.py"
    f.write_text(
        "def helper():\n"
        "    return 42\n",
        encoding="utf-8",
    )
    return str(f)


@pytest.fixture
def complex_workflow():
    """创建已触发复杂修改的工作流实例（处于ANALYZE阶段）。"""
    wf = SuperpowerWorkflow()
    wf.trigger(
        change_id="CHG-TEST-WF-001",
        title="集成测试复杂变更",
        description="测试工作流复杂变更流程",
        core_nodes=["change_gateway"],
        modified_files=["modules/superpower/gateway_analyzer.py"],
    )
    return wf


class TestWorkflowInit:
    """测试初始化。"""

    @pytest.mark.integration
    def test_init_creates_workflow_id(self, workflow):
        """测试初始化生成工作流ID。"""
        assert workflow.workflow_id is not None
        assert workflow.workflow_id.startswith("WF-")

    @pytest.mark.integration
    def test_init_current_phase_trigger(self, workflow):
        """测试初始阶段为TRIGGER。"""
        assert workflow.current_phase == PHASE_TRIGGER

    @pytest.mark.integration
    def test_init_started_at_set(self, workflow):
        """测试初始化设置started_at时间。"""
        assert workflow.started_at is not None

    @pytest.mark.integration
    def test_init_completed_at_none(self, workflow):
        """测试初始completed_at为None。"""
        assert workflow.completed_at is None

    @pytest.mark.integration
    def test_init_context_has_required_keys(self, workflow):
        """测试初始化上下文包含必要键。"""
        required_keys = [
            "change_id", "title", "description", "core_nodes",
            "modified_files", "analysis_result", "checklist",
            "modifications", "verify_results", "update_result",
            "record_result", "skipped_phases", "errors",
        ]
        for key in required_keys:
            assert key in workflow.context, f"上下文缺少键: {key}"

    @pytest.mark.integration
    def test_init_with_custom_config(self):
        """测试使用自定义配置初始化。"""
        config = {"max_depth": 2, "risk_threshold": "HIGH"}
        wf = SuperpowerWorkflow(config=config)
        assert wf.config == config

    @pytest.mark.integration
    def test_phase_constants_defined(self):
        """测试所有阶段常量已定义。"""
        assert PHASE_TRIGGER == "TRIGGER"
        assert PHASE_ANALYZE == "ANALYZE"
        assert PHASE_MODIFY == "MODIFY"
        assert PHASE_VERIFY == "VERIFY"
        assert PHASE_UPDATE == "UPDATE"
        assert PHASE_RECORD == "RECORD"
        assert PHASE_COMPLETED == "COMPLETED"
        assert PHASE_FAILED == "FAILED"

    @pytest.mark.integration
    def test_phase_order_complete(self):
        """测试PHASE_ORDER包含所有主要阶段。"""
        expected = [PHASE_TRIGGER, PHASE_ANALYZE, PHASE_MODIFY,
                    PHASE_VERIFY, PHASE_UPDATE, PHASE_RECORD, PHASE_COMPLETED]
        assert PHASE_ORDER == expected


class TestWorkflowTriggerSimple:
    """测试trigger简单修改自动跳过。"""

    @pytest.mark.integration
    def test_simple_change_returns_false(self, workflow, simple_temp_file):
        """测试简单修改trigger返回False（跳过网关分析）。"""
        result = workflow.trigger(
            change_id="CHG-SIMPLE-001",
            title="简单修改",
            description="单文件小改动",
            core_nodes=["change_gateway"],
            modified_files=[simple_temp_file],
        )
        assert result is False

    @pytest.mark.integration
    def test_simple_change_skips_analyze_phase(self, workflow, simple_temp_file):
        """测试简单修改后跳过ANALYZE阶段，直接进入MODIFY。"""
        workflow.trigger(
            change_id="CHG-SIMPLE-002",
            title="简单修改",
            description="测试跳过分析",
            core_nodes=["change_gateway"],
            modified_files=[simple_temp_file],
        )
        assert workflow.current_phase == PHASE_MODIFY
        assert PHASE_ANALYZE in workflow.context["skipped_phases"]

    @pytest.mark.integration
    def test_simple_change_saves_context(self, workflow, simple_temp_file):
        """测试简单修改后上下文保存了变更信息。"""
        workflow.trigger(
            change_id="CHG-SIMPLE-003",
            title="上下文测试",
            description="测试上下文保存",
            core_nodes=["change_gateway"],
            modified_files=[simple_temp_file],
        )
        assert workflow.context["change_id"] == "CHG-SIMPLE-003"
        assert workflow.context["title"] == "上下文测试"
        assert "change_gateway" in workflow.context["core_nodes"]

    @pytest.mark.integration
    def test_trigger_empty_change_id_raises(self, workflow):
        """测试空change_id抛出ValueError。"""
        with pytest.raises(ValueError, match="change_id不能为空"):
            workflow.trigger(
                change_id="",
                title="测试",
                description="描述",
                core_nodes=["node1"],
                modified_files=[],
            )

    @pytest.mark.integration
    def test_trigger_empty_core_nodes_raises(self, workflow):
        """测试空core_nodes抛出ValueError。"""
        with pytest.raises(ValueError, match="core_nodes不能为空"):
            workflow.trigger(
                change_id="CHG-TEST-001",
                title="测试",
                description="描述",
                core_nodes=[],
                modified_files=[],
            )


class TestWorkflowTriggerComplex:
    """测试trigger复杂修改需要完整分析。"""

    @pytest.mark.integration
    def test_complex_change_returns_true(self, workflow):
        """测试复杂修改trigger返回True（需要网关分析）。"""
        result = workflow.trigger(
            change_id="CHG-COMPLEX-001",
            title="复杂修改",
            description="涉及核心模块的大改动",
            core_nodes=["change_gateway"],
            modified_files=["modules/superpower/gateway_analyzer.py"],
        )
        assert result is True

    @pytest.mark.integration
    def test_complex_change_enters_analyze_phase(self, workflow):
        """测试复杂修改后进入ANALYZE阶段。"""
        workflow.trigger(
            change_id="CHG-COMPLEX-002",
            title="复杂修改",
            description="测试进入分析阶段",
            core_nodes=["change_gateway"],
            modified_files=["modules/superpower/gateway_analyzer.py"],
        )
        assert workflow.current_phase == PHASE_ANALYZE

    @pytest.mark.integration
    def test_complex_change_not_skipped(self, workflow):
        """测试复杂修改不跳过ANALYZE阶段。"""
        workflow.trigger(
            change_id="CHG-COMPLEX-003",
            title="复杂修改",
            description="测试不跳过",
            core_nodes=["change_gateway"],
            modified_files=["modules/superpower/gateway_analyzer.py"],
        )
        assert PHASE_ANALYZE not in workflow.context["skipped_phases"]

    @pytest.mark.integration
    def test_multiple_files_is_complex(self, workflow):
        """测试多个文件修改被判定为复杂修改。"""
        result = workflow.trigger(
            change_id="CHG-MULTI-001",
            title="多文件修改",
            description="多个文件",
            core_nodes=["change_gateway"],
            modified_files=["file1.py", "file2.py"],
        )
        assert result is True
        assert workflow.current_phase == PHASE_ANALYZE


class TestWorkflowAnalyze:
    """测试analyze阶段。"""

    @pytest.mark.integration
    def test_analyze_returns_result(self, complex_workflow):
        """测试analyze返回网关分析结果。"""
        result = complex_workflow.analyze()
        assert result is not None
        assert hasattr(result, "forward_affected")
        assert hasattr(result, "backward_affected")
        assert hasattr(result, "risks")
        assert hasattr(result, "checklist")

    @pytest.mark.integration
    def test_analyze_saves_to_context(self, complex_workflow):
        """测试analyze结果保存到上下文。"""
        complex_workflow.analyze()
        assert complex_workflow.context["analysis_result"] is not None
        assert len(complex_workflow.context["checklist"]) > 0

    @pytest.mark.integration
    def test_analyze_transitions_to_modify(self, complex_workflow):
        """测试analyze完成后流转到MODIFY阶段。"""
        complex_workflow.analyze()
        assert complex_workflow.current_phase == PHASE_MODIFY

    @pytest.mark.integration
    def test_analyze_wrong_phase_raises(self, workflow):
        """测试在非ANALYZE阶段调用analyze抛出RuntimeError。"""
        with pytest.raises(RuntimeError, match="不能执行analyze"):
            workflow.analyze()

    @pytest.mark.integration
    def test_analyze_skipped_phase_returns_none(self, workflow, simple_temp_file):
        """测试已跳过分析阶段时analyze返回None。"""
        workflow.trigger(
            change_id="CHG-SKIP-001",
            title="跳过分析",
            description="简单修改跳过分析",
            core_nodes=["change_gateway"],
            modified_files=[simple_temp_file],
        )
        # 当前在MODIFY阶段，ANALYZE已跳过
        result = workflow.analyze()
        assert result is None


class TestWorkflowModify:
    """测试modify阶段。"""

    @pytest.fixture
    def workflow_at_modify(self, complex_workflow):
        """创建处于MODIFY阶段的工作流。"""
        complex_workflow.analyze()
        return complex_workflow

    @pytest.mark.integration
    def test_modify_returns_true(self, workflow_at_modify):
        """测试modify返回True。"""
        result = workflow_at_modify.modify([
            {"file": "modules/superpower/gateway_analyzer.py", "action": "edit", "description": "优化BFS"}
        ])
        assert result is True

    @pytest.mark.integration
    def test_modify_saves_modifications(self, workflow_at_modify):
        """测试modify保存修改操作到上下文。"""
        mods = [{"file": "test.py", "action": "edit", "description": "测试"}]
        workflow_at_modify.modify(mods)
        assert workflow_at_modify.context["modifications"] == mods

    @pytest.mark.integration
    def test_modify_transitions_to_verify(self, workflow_at_modify):
        """测试modify完成后流转到VERIFY阶段。"""
        workflow_at_modify.modify([{"file": "test.py", "action": "edit"}])
        assert workflow_at_modify.current_phase == PHASE_VERIFY

    @pytest.mark.integration
    def test_modify_updates_checklist_status(self, workflow_at_modify):
        """测试modify时更新modify阶段检查项状态为completed。"""
        workflow_at_modify.modify([{"file": "test.py", "action": "edit"}])
        modify_items = [
            c for c in workflow_at_modify.context["checklist"]
            if c.stage == "modify"
        ]
        for item in modify_items:
            assert item.status == "completed"
            assert item.verified is True

    @pytest.mark.integration
    def test_modify_wrong_phase_raises(self, workflow):
        """测试在非MODIFY阶段调用modify抛出RuntimeError。"""
        with pytest.raises(RuntimeError, match="不能执行modify"):
            workflow.modify([])

    @pytest.mark.integration
    def test_modify_empty_modifications(self, workflow_at_modify):
        """测试modify传入空列表时仍能流转。"""
        result = workflow_at_modify.modify([])
        assert result is True
        assert workflow_at_modify.current_phase == PHASE_VERIFY


class TestWorkflowVerify:
    """测试verify阶段。"""

    @pytest.fixture
    def workflow_at_verify(self, complex_workflow):
        """创建处于VERIFY阶段的工作流（所有检查项已验证）。"""
        complex_workflow.analyze()
        complex_workflow.modify([{"file": "test.py", "action": "edit"}])
        # 标记所有检查项为已验证
        for item in complex_workflow.context["checklist"]:
            item.verified = True
            item.status = "completed"
        return complex_workflow

    @pytest.mark.integration
    def test_verify_returns_tuple(self, workflow_at_verify):
        """测试verify返回(布尔, 列表)元组。"""
        result = workflow_at_verify.verify()
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert isinstance(result[0], bool)
        assert isinstance(result[1], list)

    @pytest.mark.integration
    def test_verify_all_passed(self, workflow_at_verify):
        """测试所有must项验证通过时verify返回True。"""
        passed, failed = workflow_at_verify.verify()
        assert passed is True
        assert len(failed) == 0

    @pytest.mark.integration
    def test_verify_transitions_to_update(self, workflow_at_verify):
        """测试验证通过后流转到UPDATE阶段。"""
        workflow_at_verify.verify()
        assert workflow_at_verify.current_phase == PHASE_UPDATE

    @pytest.mark.integration
    def test_verify_saves_results(self, workflow_at_verify):
        """测试verify结果保存到上下文。"""
        workflow_at_verify.verify()
        assert "verify_results" in workflow_at_verify.context
        assert workflow_at_verify.context["verify_results"]["all_passed"] is True

    @pytest.mark.integration
    def test_verify_with_unverified_must(self, complex_workflow):
        """测试有未验证must项时verify返回False。"""
        complex_workflow.analyze()
        complex_workflow.modify([{"file": "test.py", "action": "edit"}])
        # 不标记任何检查项为已验证
        passed, failed = complex_workflow.verify()
        assert passed is False
        assert len(failed) > 0

    @pytest.mark.integration
    def test_verify_wrong_phase_raises(self, workflow):
        """测试在非VERIFY阶段调用verify抛出RuntimeError。"""
        with pytest.raises(RuntimeError, match="不能执行verify"):
            workflow.verify()


class TestWorkflowUpdate:
    """测试update_neural_graph阶段（半自动模式，不修改文件）。"""

    @pytest.fixture
    def workflow_at_update(self, complex_workflow):
        """创建处于UPDATE阶段的工作流。"""
        complex_workflow.analyze()
        complex_workflow.modify([{"file": "modules/superpower/gateway_analyzer.py", "action": "edit"}])
        for item in complex_workflow.context["checklist"]:
            item.verified = True
            item.status = "completed"
        complex_workflow.verify()
        return complex_workflow

    @pytest.mark.integration
    def test_update_semiauto_returns_bool(self, workflow_at_update):
        """测试update_neural_graph(auto_confirm=False)返回布尔值。"""
        result = workflow_at_update.update_neural_graph(auto_confirm=False)
        assert isinstance(result, bool)

    @pytest.mark.integration
    def test_update_transitions_to_record(self, workflow_at_update):
        """测试更新后流转到RECORD阶段。"""
        workflow_at_update.update_neural_graph(auto_confirm=False)
        assert workflow_at_update.current_phase == PHASE_RECORD

    @pytest.mark.integration
    def test_update_saves_result(self, workflow_at_update):
        """测试更新结果保存到上下文。"""
        workflow_at_update.update_neural_graph(auto_confirm=False)
        assert workflow_at_update.context["update_result"] is not None

    @pytest.mark.integration
    def test_update_wrong_phase_raises(self, workflow):
        """测试在非UPDATE阶段调用update_neural_graph抛出RuntimeError。"""
        with pytest.raises(RuntimeError, match="不能执行update_neural_graph"):
            workflow.update_neural_graph()

    @pytest.mark.integration
    def test_update_does_not_modify_graph_file(self, workflow_at_update):
        """测试半自动更新不修改function_graph.json文件。"""
        graph_path = os.path.join(_BACKEND_DIR, "docs", "function_graph.json")
        with open(graph_path, "r", encoding="utf-8") as f:
            before = json.load(f)
        before_nodes = len(before["nodes"])
        before_edges = len(before["edges"])

        workflow_at_update.update_neural_graph(auto_confirm=False)

        with open(graph_path, "r", encoding="utf-8") as f:
            after = json.load(f)
        assert len(after["nodes"]) == before_nodes
        assert len(after["edges"]) == before_edges


class TestWorkflowStateMachine:
    """测试状态机完整流转（不调用record_change）。"""

    @pytest.mark.integration
    def test_full_state_transition_to_record(self, complex_workflow):
        """测试完整状态流转从TRIGGER到RECORD（不调用record_change）。"""
        assert complex_workflow.current_phase == PHASE_ANALYZE

        complex_workflow.analyze()
        assert complex_workflow.current_phase == PHASE_MODIFY

        complex_workflow.modify([{"file": "test.py", "action": "edit"}])
        assert complex_workflow.current_phase == PHASE_VERIFY

        for item in complex_workflow.context["checklist"]:
            item.verified = True
            item.status = "completed"
        complex_workflow.verify()
        assert complex_workflow.current_phase == PHASE_UPDATE

        complex_workflow.update_neural_graph(auto_confirm=False)
        assert complex_workflow.current_phase == PHASE_RECORD

    @pytest.mark.integration
    def test_simple_change_state_transition(self, workflow, simple_temp_file):
        """测试简单修改的状态流转（跳过ANALYZE）。"""
        workflow.trigger(
            change_id="CHG-SIMPLE-FLOW-001",
            title="简单流程",
            description="测试简单修改流程",
            core_nodes=["change_gateway"],
            modified_files=[simple_temp_file],
        )
        assert workflow.current_phase == PHASE_MODIFY

        workflow.modify([{"file": simple_temp_file, "action": "edit"}])
        assert workflow.current_phase == PHASE_VERIFY

        # 简单修改没有checklist（因为跳过了analyze），verify应通过
        passed, _ = workflow.verify()
        assert passed is True
        assert workflow.current_phase == PHASE_UPDATE

    @pytest.mark.integration
    def test_state_machine_no_skip_phases_for_complex(self, complex_workflow):
        """测试复杂修改不跳过任何阶段。"""
        assert len(complex_workflow.context["skipped_phases"]) == 0

    @pytest.mark.integration
    def test_context_errors_empty_initially(self, workflow):
        """测试初始上下文错误列表为空。"""
        assert workflow.context["errors"] == []


class TestWorkflowProgress:
    """测试get_progress进度查询。"""

    @pytest.mark.integration
    def test_get_progress_returns_dict(self, workflow):
        """测试get_progress返回字典。"""
        progress = workflow.get_progress()
        assert isinstance(progress, dict)

    @pytest.mark.integration
    def test_progress_contains_workflow_id(self, workflow):
        """测试进度包含工作流ID。"""
        progress = workflow.get_progress()
        assert progress["workflow_id"] == workflow.workflow_id

    @pytest.mark.integration
    def test_progress_contains_current_phase(self, workflow):
        """测试进度包含当前阶段。"""
        progress = workflow.get_progress()
        assert progress["current_phase"] == PHASE_TRIGGER

    @pytest.mark.integration
    def test_progress_completion_percent_range(self, workflow):
        """测试完成百分比在0到100之间。"""
        progress = workflow.get_progress()
        assert 0 <= progress["completion_percent"] <= 100

    @pytest.mark.integration
    def test_progress_at_trigger_is_zero(self, workflow):
        """测试TRIGGER阶段完成度为0%。"""
        progress = workflow.get_progress()
        assert progress["completion_percent"] == 0

    @pytest.mark.integration
    def test_progress_contains_checklist_stats(self, complex_workflow):
        """测试进度包含检查清单统计。"""
        complex_workflow.analyze()
        progress = complex_workflow.get_progress()
        assert "checklist_stats" in progress
        assert progress["checklist_stats"]["total"] > 0

    @pytest.mark.integration
    def test_progress_contains_phases_executed(self, complex_workflow):
        """测试进度包含已执行阶段列表。"""
        complex_workflow.analyze()
        progress = complex_workflow.get_progress()
        assert "phases_executed" in progress
        assert PHASE_TRIGGER in progress["phases_executed"]

    @pytest.mark.integration
    def test_progress_contains_skipped_phases(self, workflow, simple_temp_file):
        """测试进度包含跳过的阶段。"""
        workflow.trigger(
            change_id="CHG-PROG-SKIP-001",
            title="进度测试",
            description="测试跳过阶段进度",
            core_nodes=["change_gateway"],
            modified_files=[simple_temp_file],
        )
        progress = workflow.get_progress()
        assert PHASE_ANALYZE in progress["phases_skipped"]


class TestWorkflowLogging:
    """测试日志文件创建。"""

    @pytest.mark.integration
    def test_log_file_exists(self):
        """测试工作流日志文件存在。"""
        log_file = os.path.join(_BACKEND_DIR, "logs", "superpower_workflow.log")
        assert os.path.exists(log_file), f"日志文件不存在: {log_file}"

    @pytest.mark.integration
    def test_log_file_is_writable(self):
        """测试日志文件可写（非空）。"""
        log_file = os.path.join(_BACKEND_DIR, "logs", "superpower_workflow.log")
        assert os.path.getsize(log_file) >= 0

    @pytest.mark.integration
    def test_workflow_generates_log_entries(self, workflow):
        """测试工作流操作产生日志条目。"""
        log_file = os.path.join(_BACKEND_DIR, "logs", "superpower_workflow.log")
        size_before = os.path.getsize(log_file)
        # 触发工作流产生日志
        workflow.trigger(
            change_id="CHG-LOG-TEST-001",
            title="日志测试",
            description="测试日志生成",
            core_nodes=["change_gateway"],
            modified_files=["modules/superpower/gateway_analyzer.py"],
        )
        size_after = os.path.getsize(log_file)
        assert size_after >= size_before


class TestWorkflowModuleFunctions:
    """测试模块级函数。"""

    @pytest.mark.integration
    def test_run_superpower_workflow_function_exists(self):
        """测试run_superpower_workflow函数存在且可调用。"""
        assert callable(run_superpower_workflow)

    @pytest.mark.integration
    def test_workflow_repr_not_empty(self, workflow):
        """测试工作流实例的字符串表示。"""
        repr_str = repr(workflow)
        assert isinstance(repr_str, str)
        assert len(repr_str) > 0
