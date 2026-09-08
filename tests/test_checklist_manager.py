"""
检查清单管理器（ChecklistManager）单元测试

测试覆盖：
- 初始化
- generate_from_analysis从分析结果生成
- add_item/remove_item/update_item增删改
- start_item/complete_item状态流转
- skip_item对must项返回False
- verify_item/verify_all验证
- get_incomplete_must/is_ready_for_verification就绪判断
- generate_report/generate_text_report报告生成
- to_dict/from_dict序列化

所有测试均在内存中操作，不修改任何文件。
"""
import os
import sys
import json
import pytest

# 确保backend目录在sys.path中
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from modules.superpower.checklist_manager import (
    ChecklistManager,
    ChecklistItem,
    ChecklistReport,
)


@pytest.fixture
def manager():
    """创建空的ChecklistManager实例。"""
    return ChecklistManager()


@pytest.fixture
def manager_with_items():
    """创建包含预设检查项的ChecklistManager。"""
    mgr = ChecklistManager()
    mgr.add_item("修改阶段", "确认代码修改完成", "must")
    mgr.add_item("验证阶段", "执行单元测试", "must")
    mgr.add_item("验证阶段", "执行集成测试", "should")
    mgr.add_item("记录阶段", "更新变更日志", "optional")
    return mgr


@pytest.fixture
def sample_analysis_result():
    """构造模拟的网关分析结果字典。"""
    return {
        "forward_affected": ["node_a", "node_b", "node_c"],
        "backward_affected": ["node_d", "node_e"],
        "risks": [
            {"level": "HIGH", "description": "高风险：影响核心业务"},
            {"level": "MEDIUM", "description": "中风险：影响API接口"},
            {"level": "LOW", "description": "低风险：文档更新"},
        ],
    }


class TestChecklistManagerInit:
    """测试初始化。"""

    @pytest.mark.unit
    def test_init_empty(self):
        """测试空初始化。"""
        mgr = ChecklistManager()
        assert len(mgr.items) == 0

    @pytest.mark.unit
    def test_init_with_items(self):
        """测试带初始检查项的初始化。"""
        items = [
            ChecklistItem(id="CL-TEST-001", stage="修改阶段", description="测试", priority="must"),
        ]
        mgr = ChecklistManager(items=items)
        assert len(mgr.items) == 1
        assert mgr.items[0].id == "CL-TEST-001"

    @pytest.mark.unit
    def test_items_property_returns_copy(self, manager_with_items):
        """测试items属性返回副本，修改不影响内部状态。"""
        items = manager_with_items.items
        items.append(ChecklistItem(id="fake", stage="x", description="y"))
        assert len(manager_with_items.items) == 4  # 原始数量不变

    @pytest.mark.unit
    def test_len_returns_item_count(self, manager_with_items):
        """测试__len__返回检查项数量。"""
        assert len(manager_with_items) == 4


class TestChecklistManagerGenerate:
    """测试generate_from_analysis。"""

    @pytest.mark.unit
    def test_generate_returns_list(self, manager, sample_analysis_result):
        """测试generate_from_analysis返回检查项列表。"""
        items = manager.generate_from_analysis(sample_analysis_result)
        assert isinstance(items, list)
        assert len(items) > 0

    @pytest.mark.unit
    def test_generate_items_are_checklistitem(self, manager, sample_analysis_result):
        """测试生成的项是ChecklistItem实例。"""
        items = manager.generate_from_analysis(sample_analysis_result)
        for item in items:
            assert isinstance(item, ChecklistItem)

    @pytest.mark.unit
    def test_generate_contains_must_items(self, manager, sample_analysis_result):
        """测试生成的清单包含must优先级项。"""
        items = manager.generate_from_analysis(sample_analysis_result)
        must_items = [i for i in items if i.priority == "must"]
        assert len(must_items) > 0

    @pytest.mark.unit
    def test_generate_high_risk_creates_must(self, manager, sample_analysis_result):
        """测试高风险点生成must优先级检查项。"""
        items = manager.generate_from_analysis(sample_analysis_result)
        high_risk_items = [
            i for i in items
            if "高风险" in i.description and i.priority == "must"
        ]
        assert len(high_risk_items) > 0

    @pytest.mark.unit
    def test_generate_medium_risk_creates_should(self, manager, sample_analysis_result):
        """测试中风险点生成should优先级检查项。"""
        items = manager.generate_from_analysis(sample_analysis_result)
        medium_items = [
            i for i in items
            if "中风险" in i.description and i.priority == "should"
        ]
        assert len(medium_items) > 0

    @pytest.mark.unit
    def test_generate_covers_workflow_stages(self, manager, sample_analysis_result):
        """测试生成的清单覆盖所有工作流阶段。"""
        items = manager.generate_from_analysis(sample_analysis_result)
        stages = {i.stage for i in items}
        expected_stages = {"分析阶段", "修改阶段", "验证阶段", "更新阶段", "记录阶段"}
        assert expected_stages.issubset(stages)

    @pytest.mark.unit
    def test_generate_with_no_risks(self, manager):
        """测试无风险点时仍生成基本检查项。"""
        result = {"forward_affected": [], "backward_affected": [], "risks": []}
        items = manager.generate_from_analysis(result)
        assert len(items) > 0
        # 无风险时修改阶段应有默认项
        modify_items = [i for i in items if i.stage == "修改阶段"]
        assert len(modify_items) > 0

    @pytest.mark.unit
    def test_generate_with_integer_affected_counts(self, manager):
        """测试forward_affected为整数时也能正确处理。"""
        result = {"forward_affected": 5, "backward_affected": 3, "risks": []}
        items = manager.generate_from_analysis(result)
        assert len(items) > 0
        # 验证阶段应包含受影响节点验证项
        verify_items = [i for i in items if i.stage == "验证阶段"]
        assert len(verify_items) > 0


class TestChecklistManagerCRUD:
    """测试add_item/remove_item/update_item。"""

    @pytest.mark.unit
    def test_add_item(self, manager):
        """测试添加检查项。"""
        item = manager.add_item("修改阶段", "测试添加", "must")
        assert isinstance(item, ChecklistItem)
        assert item.stage == "修改阶段"
        assert item.description == "测试添加"
        assert item.priority == "must"
        assert len(manager.items) == 1

    @pytest.mark.unit
    def test_add_item_default_priority_should(self, manager):
        """测试添加检查项默认优先级为should。"""
        item = manager.add_item("验证阶段", "默认优先级")
        assert item.priority == "should"

    @pytest.mark.unit
    def test_add_item_generates_unique_id(self, manager):
        """测试添加的检查项ID唯一。"""
        item1 = manager.add_item("修改阶段", "第一项")
        item2 = manager.add_item("修改阶段", "第二项")
        assert item1.id != item2.id

    @pytest.mark.unit
    def test_remove_item_existing(self, manager_with_items):
        """测试删除存在的检查项。"""
        item_id = manager_with_items.items[0].id
        result = manager_with_items.remove_item(item_id)
        assert result is True
        assert len(manager_with_items.items) == 3

    @pytest.mark.unit
    def test_remove_item_nonexistent(self, manager_with_items):
        """测试删除不存在的检查项返回False。"""
        result = manager_with_items.remove_item("CL-NONEXISTENT-999")
        assert result is False
        assert len(manager_with_items.items) == 4

    @pytest.mark.unit
    def test_update_item_description(self, manager_with_items):
        """测试更新检查项描述。"""
        item_id = manager_with_items.items[0].id
        updated = manager_with_items.update_item(item_id, description="新描述")
        assert updated is not None
        assert updated.description == "新描述"

    @pytest.mark.unit
    def test_update_item_nonexistent_returns_none(self, manager):
        """测试更新不存在的检查项返回None。"""
        result = manager.update_item("CL-FAKE-001", description="x")
        assert result is None

    @pytest.mark.unit
    def test_update_item_ignores_invalid_fields(self, manager_with_items):
        """测试update_item忽略不允许修改的字段。"""
        item_id = manager_with_items.items[0].id
        original_id = manager_with_items.items[0].id
        manager_with_items.update_item(item_id, id="HACKED", invalid_field="x")
        assert manager_with_items.items[0].id == original_id


class TestChecklistManagerStatusFlow:
    """测试start_item/complete_item状态流转。"""

    @pytest.mark.unit
    def test_start_item_changes_status(self, manager_with_items):
        """测试start_item将状态改为in_progress。"""
        item_id = manager_with_items.items[0].id
        result = manager_with_items.start_item(item_id)
        assert result is True
        item = manager_with_items.items[0]
        assert item.status == "in_progress"

    @pytest.mark.unit
    def test_start_item_nonexistent_returns_false(self, manager):
        """测试start_item不存在的项返回False。"""
        result = manager.start_item("CL-FAKE-001")
        assert result is False

    @pytest.mark.unit
    def test_complete_item_changes_status(self, manager_with_items):
        """测试complete_item将状态改为completed。"""
        item_id = manager_with_items.items[0].id
        result = manager_with_items.complete_item(item_id, notes="已完成")
        assert result is True
        item = manager_with_items.items[0]
        assert item.status == "completed"
        assert item.notes == "已完成"

    @pytest.mark.unit
    def test_complete_item_without_notes(self, manager_with_items):
        """测试complete_item不传入notes时不修改notes。"""
        item_id = manager_with_items.items[0].id
        manager_with_items.complete_item(item_id)
        item = manager_with_items.items[0]
        assert item.status == "completed"
        assert item.notes is None

    @pytest.mark.unit
    def test_full_status_flow_pending_to_completed(self, manager):
        """测试完整状态流转：pending -> in_progress -> completed。"""
        item = manager.add_item("验证阶段", "流程测试", "must")
        assert item.status == "pending"
        manager.start_item(item.id)
        assert manager.items[0].status == "in_progress"
        manager.complete_item(item.id)
        assert manager.items[0].status == "completed"


class TestChecklistManagerSkip:
    """测试skip_item。"""

    @pytest.mark.unit
    def test_skip_should_item_succeeds(self, manager_with_items):
        """测试跳过should优先级项成功。"""
        should_items = [i for i in manager_with_items.items if i.priority == "should"]
        item_id = should_items[0].id
        result = manager_with_items.skip_item(item_id, "不需要执行")
        assert result is True
        item = [i for i in manager_with_items.items if i.id == item_id][0]
        assert item.status == "skipped"
        assert item.notes == "不需要执行"

    @pytest.mark.unit
    def test_skip_must_item_returns_false(self, manager_with_items):
        """测试跳过must优先级项返回False。"""
        must_items = [i for i in manager_with_items.items if i.priority == "must"]
        item_id = must_items[0].id
        result = manager_with_items.skip_item(item_id, "尝试跳过must")
        assert result is False
        item = [i for i in manager_with_items.items if i.id == item_id][0]
        assert item.status != "skipped"

    @pytest.mark.unit
    def test_skip_optional_item_succeeds(self, manager_with_items):
        """测试跳过optional优先级项成功。"""
        optional_items = [i for i in manager_with_items.items if i.priority == "optional"]
        item_id = optional_items[0].id
        result = manager_with_items.skip_item(item_id, "可选跳过")
        assert result is True

    @pytest.mark.unit
    def test_skip_nonexistent_returns_false(self, manager):
        """测试跳过不存在的项返回False。"""
        result = manager.skip_item("CL-FAKE-001", "原因")
        assert result is False


class TestChecklistManagerVerify:
    """测试verify_item/verify_all。"""

    @pytest.mark.unit
    def test_verify_item_passed(self, manager_with_items):
        """测试验证检查项通过。"""
        item_id = manager_with_items.items[0].id
        result = manager_with_items.verify_item(item_id, passed=True, notes="验证通过")
        assert result is True
        item = manager_with_items.items[0]
        assert item.verified is True
        assert item.verified_at is not None

    @pytest.mark.unit
    def test_verify_item_failed(self, manager_with_items):
        """测试验证检查项不通过。"""
        item_id = manager_with_items.items[0].id
        manager_with_items.verify_item(item_id, passed=False)
        assert manager_with_items.items[0].verified is False

    @pytest.mark.unit
    def test_verify_item_nonexistent_returns_false(self, manager):
        """测试验证不存在的项返回False。"""
        result = manager.verify_item("CL-FAKE-001", passed=True)
        assert result is False

    @pytest.mark.unit
    def test_verify_all_all_passed(self, manager_with_items):
        """测试所有must项已完成且验证通过时verify_all返回True。"""
        # 先完成所有must项并验证
        for item in manager_with_items.items:
            if item.priority == "must":
                manager_with_items.complete_item(item.id)
                manager_with_items.verify_item(item.id, passed=True)
        all_pass, failed = manager_with_items.verify_all()
        assert all_pass is True
        assert len(failed) == 0

    @pytest.mark.unit
    def test_verify_all_some_failed(self, manager_with_items):
        """测试部分must项未验证时verify_all返回False。"""
        # 只完成第一个must项并验证
        must_items = [i for i in manager_with_items.items if i.priority == "must"]
        manager_with_items.complete_item(must_items[0].id)
        manager_with_items.verify_item(must_items[0].id, passed=True)
        # 第二个must项完成但未验证
        manager_with_items.complete_item(must_items[1].id)
        all_pass, failed = manager_with_items.verify_all()
        assert all_pass is False
        assert must_items[1].id in failed

    @pytest.mark.unit
    def test_verify_all_ignores_incomplete_must(self, manager_with_items):
        """测试verify_all只检查已completed的must项。"""
        # 不完成任何must项
        all_pass, failed = manager_with_items.verify_all()
        # 没有completed的must项，所以全部通过（空集）
        assert all_pass is True
        assert len(failed) == 0


class TestChecklistManagerReadiness:
    """测试get_incomplete_must/is_ready_for_verification。"""

    @pytest.mark.unit
    def test_get_incomplete_must_returns_list(self, manager_with_items):
        """测试get_incomplete_must返回未完成的must项列表。"""
        incomplete = manager_with_items.get_incomplete_must()
        assert isinstance(incomplete, list)
        assert len(incomplete) > 0  # 初始状态所有must项都未完成

    @pytest.mark.unit
    def test_get_incomplete_must_only_must(self, manager_with_items):
        """测试get_incomplete_must只包含must优先级项。"""
        incomplete = manager_with_items.get_incomplete_must()
        for item in incomplete:
            assert item.priority == "must"

    @pytest.mark.unit
    def test_get_incomplete_must_excludes_completed(self, manager_with_items):
        """测试已完成的must项不在incomplete列表中。"""
        must_items = [i for i in manager_with_items.items if i.priority == "must"]
        manager_with_items.complete_item(must_items[0].id)
        incomplete = manager_with_items.get_incomplete_must()
        incomplete_ids = [i.id for i in incomplete]
        assert must_items[0].id not in incomplete_ids

    @pytest.mark.unit
    def test_is_ready_for_verification_false_when_incomplete(self, manager_with_items):
        """测试有未完成must项时is_ready_for_verification返回False。"""
        assert manager_with_items.is_ready_for_verification() is False

    @pytest.mark.unit
    def test_is_ready_for_verification_true_when_all_done(self, manager_with_items):
        """测试所有must项完成时is_ready_for_verification返回True。"""
        for item in manager_with_items.items:
            if item.priority == "must":
                manager_with_items.complete_item(item.id)
        assert manager_with_items.is_ready_for_verification() is True

    @pytest.mark.unit
    def test_is_ready_for_verification_true_for_empty(self, manager):
        """测试空清单时is_ready_for_verification返回True。"""
        assert manager.is_ready_for_verification() is True


class TestChecklistManagerReport:
    """测试generate_report/generate_text_report。"""

    @pytest.mark.unit
    def test_generate_report_returns_checklistreport(self, manager_with_items):
        """测试generate_report返回ChecklistReport实例。"""
        report = manager_with_items.generate_report()
        assert isinstance(report, ChecklistReport)

    @pytest.mark.unit
    def test_report_total_matches_item_count(self, manager_with_items):
        """测试报告total等于检查项总数。"""
        report = manager_with_items.generate_report()
        assert report.total == len(manager_with_items.items)

    @pytest.mark.unit
    def test_report_status_counts(self, manager_with_items):
        """测试报告中的状态计数正确。"""
        # 完成一个项
        manager_with_items.complete_item(manager_with_items.items[0].id)
        report = manager_with_items.generate_report()
        assert report.completed == 1
        assert report.pending == 3  # 其余仍为pending

    @pytest.mark.unit
    def test_report_must_counts(self, manager_with_items):
        """测试报告中的must计数正确。"""
        report = manager_with_items.generate_report()
        must_items = [i for i in manager_with_items.items if i.priority == "must"]
        assert report.must_total == len(must_items)

    @pytest.mark.unit
    def test_report_coverage_rate(self, manager_with_items):
        """测试报告完成率在0到1之间。"""
        report = manager_with_items.generate_report()
        assert 0.0 <= report.coverage_rate <= 1.0

    @pytest.mark.unit
    def test_report_by_stage(self, manager_with_items):
        """测试报告包含按阶段统计。"""
        report = manager_with_items.generate_report()
        assert isinstance(report.by_stage, dict)
        assert "修改阶段" in report.by_stage
        assert "验证阶段" in report.by_stage

    @pytest.mark.unit
    def test_generate_text_report_nonempty(self, manager_with_items):
        """测试generate_text_report返回非空字符串。"""
        text = manager_with_items.generate_text_report()
        assert isinstance(text, str)
        assert len(text) > 0

    @pytest.mark.unit
    def test_text_report_contains_title(self, manager_with_items):
        """测试文本报告包含标题。"""
        text = manager_with_items.generate_text_report()
        assert "检查清单报告" in text

    @pytest.mark.unit
    def test_text_report_contains_progress(self, manager_with_items):
        """测试文本报告包含进度信息。"""
        text = manager_with_items.generate_text_report()
        assert "总进度" in text

    @pytest.mark.unit
    def test_text_report_empty_manager(self, manager):
        """测试空管理器的文本报告仍可生成。"""
        text = manager.generate_text_report()
        assert isinstance(text, str)
        assert len(text) > 0


class TestChecklistManagerSerialization:
    """测试to_dict/from_dict序列化。"""

    @pytest.mark.unit
    def test_to_dict_returns_dict(self, manager_with_items):
        """测试to_dict返回字典。"""
        data = manager_with_items.to_dict()
        assert isinstance(data, dict)
        assert "items" in data
        assert "counters" in data

    @pytest.mark.unit
    def test_to_dict_items_count(self, manager_with_items):
        """测试to_dict中items数量正确。"""
        data = manager_with_items.to_dict()
        assert len(data["items"]) == len(manager_with_items.items)

    @pytest.mark.unit
    def test_from_dict_restores_items(self, manager_with_items):
        """测试from_dict恢复检查项。"""
        data = manager_with_items.to_dict()
        restored = ChecklistManager.from_dict(data)
        assert len(restored.items) == len(manager_with_items.items)
        assert restored.items[0].id == manager_with_items.items[0].id
        assert restored.items[0].description == manager_with_items.items[0].description

    @pytest.mark.unit
    def test_from_dict_restores_counters(self, manager_with_items):
        """测试from_dict恢复计数器。"""
        data = manager_with_items.to_dict()
        restored = ChecklistManager.from_dict(data)
        assert restored._counters == manager_with_items._counters

    @pytest.mark.unit
    def test_roundtrip_serialization(self, manager_with_items):
        """测试序列化-反序列化往返后数据一致。"""
        data = manager_with_items.to_dict()
        restored = ChecklistManager.from_dict(data)
        data2 = restored.to_dict()
        assert json.dumps(data, ensure_ascii=False, sort_keys=True) == \
               json.dumps(data2, ensure_ascii=False, sort_keys=True)

    @pytest.mark.unit
    def test_from_dict_empty(self):
        """测试from_dict处理空数据。"""
        restored = ChecklistManager.from_dict({"items": [], "counters": {}})
        assert len(restored.items) == 0

    @pytest.mark.unit
    def test_to_dict_json_serializable(self, manager_with_items):
        """测试to_dict结果可被JSON序列化。"""
        data = manager_with_items.to_dict()
        json_str = json.dumps(data, ensure_ascii=False)
        assert isinstance(json_str, str)
