"""
变更记录器（ChangeRecorder）单元测试

测试覆盖：
- 初始化
- get_current_version读取版本号
- bump_version版本升级（patch/minor/major）
- generate_change_id生成变更ID
- create_record创建变更记录
- validate_record验证记录
- get_recent_changes获取最近变更
- get_change_by_id按ID查询
- get_stats统计信息
- _parse_version/_format_version静态方法

注意：append_record、update_config_version、record方法会修改真实文件，
测试中使用临时路径（tmp_path）避免修改真实数据。
"""
import os
import sys
import json
import re
import pytest
from datetime import datetime

# 确保backend目录在sys.path中
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from modules.superpower.change_recorder import (
    ChangeRecorder,
    ChangeRecord,
    RecordResult,
)

# 真实文件路径（只读操作使用）
REAL_HISTORY_PATH = os.path.join(_BACKEND_DIR, "docs", "change_history.json")
REAL_CONFIG_PATH = os.path.join(_BACKEND_DIR, "config.py")


@pytest.fixture
def temp_recorder(tmp_path):
    """创建使用临时文件的ChangeRecorder实例。"""
    history_path = str(tmp_path / "change_history.json")
    config_path = str(tmp_path / "config.py")
    # 创建临时config.py，包含版本号
    with open(config_path, "w", encoding="utf-8") as f:
        f.write('APP_VERSION = _env_str("APP_VERSION", "2.9.54")\n')
    return ChangeRecorder(history_path=history_path, config_path=config_path)


@pytest.fixture
def real_recorder():
    """创建使用真实文件的ChangeRecorder实例（仅用于只读查询）。"""
    return ChangeRecorder(
        history_path=REAL_HISTORY_PATH,
        config_path=REAL_CONFIG_PATH,
    )


class TestChangeRecorderInit:
    """测试初始化。"""

    @pytest.mark.unit
    def test_init_with_temp_paths(self, temp_recorder):
        """测试使用临时路径初始化成功。"""
        assert temp_recorder.history_path is not None
        assert temp_recorder.config_path is not None

    @pytest.mark.unit
    def test_init_resolves_absolute_paths(self, tmp_path):
        """测试传入绝对路径时直接使用。"""
        history = str(tmp_path / "h.json")
        config = str(tmp_path / "c.py")
        recorder = ChangeRecorder(history_path=history, config_path=config)
        assert str(recorder.history_path) == history
        assert str(recorder.config_path) == config

    @pytest.mark.unit
    def test_init_with_real_paths(self, real_recorder):
        """测试使用真实路径初始化。"""
        assert real_recorder.history_path.exists()
        assert real_recorder.config_path.exists()


class TestChangeRecorderVersion:
    """测试版本管理。"""

    @pytest.mark.unit
    def test_get_current_version_returns_valid_version(self, real_recorder):
        """测试从真实config.py读取版本号，格式为x.y.z且非0.0.0。"""
        version = real_recorder.get_current_version()
        assert version != "0.0.0"
        # 验证语义化版本格式
        parts = version.split(".")
        assert len(parts) == 3
        assert all(p.isdigit() for p in parts)

    @pytest.mark.unit
    def test_get_current_version_temp(self, temp_recorder):
        """测试从临时config.py读取版本号。"""
        version = temp_recorder.get_current_version()
        assert version == "2.9.54"

    @pytest.mark.unit
    def test_get_current_version_missing_config(self, tmp_path):
        """测试config.py不存在时返回0.0.0。"""
        recorder = ChangeRecorder(
            history_path=str(tmp_path / "h.json"),
            config_path=str(tmp_path / "nonexistent.py"),
        )
        assert recorder.get_current_version() == "0.0.0"

    @pytest.mark.unit
    def test_bump_version_patch(self, temp_recorder):
        """测试patch版本升级：2.9.54 -> 2.9.55。"""
        new_version = temp_recorder.bump_version("patch")
        assert new_version == "2.9.55"

    @pytest.mark.unit
    def test_bump_version_minor(self, temp_recorder):
        """测试minor版本升级：2.9.54 -> 2.10.0。"""
        new_version = temp_recorder.bump_version("minor")
        assert new_version == "2.10.0"

    @pytest.mark.unit
    def test_bump_version_major(self, temp_recorder):
        """测试major版本升级：2.9.54 -> 3.0.0。"""
        new_version = temp_recorder.bump_version("major")
        assert new_version == "3.0.0"

    @pytest.mark.unit
    def test_bump_version_default_is_patch(self, temp_recorder):
        """测试默认升级级别为patch。"""
        new_version = temp_recorder.bump_version()
        assert new_version == "2.9.55"

    @pytest.mark.unit
    def test_parse_version(self):
        """测试_parse_version静态方法解析版本号。"""
        major, minor, patch = ChangeRecorder._parse_version("2.9.54")
        assert major == 2
        assert minor == 9
        assert patch == 54

    @pytest.mark.unit
    def test_parse_version_invalid_returns_zeros(self):
        """测试解析无效版本号返回0。"""
        major, minor, patch = ChangeRecorder._parse_version("invalid")
        assert major == 0
        assert minor == 0
        assert patch == 0

    @pytest.mark.unit
    def test_parse_version_partial(self):
        """测试解析不完整版本号。"""
        major, minor, patch = ChangeRecorder._parse_version("2")
        assert major == 2
        assert minor == 0
        assert patch == 0

    @pytest.mark.unit
    def test_format_version(self):
        """测试_format_version静态方法格式化版本号。"""
        result = ChangeRecorder._format_version(2, 9, 55)
        assert result == "2.9.55"

    @pytest.mark.unit
    def test_parse_format_roundtrip(self):
        """测试解析-格式化往返一致。"""
        original = "3.10.5"
        major, minor, patch = ChangeRecorder._parse_version(original)
        formatted = ChangeRecorder._format_version(major, minor, patch)
        assert formatted == original


class TestChangeRecorderChangeId:
    """测试generate_change_id。"""

    @pytest.mark.unit
    def test_generate_change_id_format(self, temp_recorder):
        """测试生成的变更ID符合CHG-YYYYMMDD-NNN格式。"""
        change_id = temp_recorder.generate_change_id()
        pattern = r"^CHG-\d{8}-\d{3}$"
        assert re.match(pattern, change_id), f"ID格式不正确: {change_id}"

    @pytest.mark.unit
    def test_generate_change_id_contains_today(self, temp_recorder):
        """测试生成的变更ID包含今天的日期。"""
        change_id = temp_recorder.generate_change_id()
        today = datetime.now().strftime("%Y%m%d")
        assert today in change_id

    @pytest.mark.unit
    def test_generate_change_id_starts_at_001_for_empty_history(self, temp_recorder):
        """测试空历史时生成的ID序号为001。"""
        change_id = temp_recorder.generate_change_id()
        assert change_id.endswith("-001")

    @pytest.mark.unit
    def test_generate_change_id_increments(self, temp_recorder, tmp_path):
        """测试已有记录时ID序号递增。"""
        # 先创建一条历史记录
        today = datetime.now().strftime("%Y%m%d")
        history = {
            "meta": {"version": "2.9.54", "total_changes": 1},
            "changes": [
                {"id": f"CHG-{today}-001", "title": "已有记录"},
            ],
        }
        with open(str(temp_recorder.history_path), "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False)
        change_id = temp_recorder.generate_change_id()
        assert change_id.endswith("-002")


class TestChangeRecorderCreateRecord:
    """测试create_record。"""

    @pytest.mark.unit
    def test_create_record_returns_change_record(self, temp_recorder):
        """测试create_record返回ChangeRecord实例。"""
        record = temp_recorder.create_record(
            title="测试变更",
            description="这是一个测试变更记录",
        )
        assert isinstance(record, ChangeRecord)

    @pytest.mark.unit
    def test_create_record_has_id(self, temp_recorder):
        """测试创建的记录包含自动生成的ID。"""
        record = temp_recorder.create_record(title="测试", description="描述")
        assert record.id is not None
        assert record.id.startswith("CHG-")

    @pytest.mark.unit
    def test_create_record_has_version(self, temp_recorder):
        """测试创建的记录包含当前版本号。"""
        record = temp_recorder.create_record(title="测试", description="描述")
        assert record.version == "2.9.54"

    @pytest.mark.unit
    def test_create_record_has_date(self, temp_recorder):
        """测试创建的记录包含今天的日期。"""
        record = temp_recorder.create_record(title="测试", description="描述")
        today = datetime.now().strftime("%Y-%m-%d")
        assert record.date == today

    @pytest.mark.unit
    def test_create_record_default_category(self, temp_recorder):
        """测试创建记录默认category为feature。"""
        record = temp_recorder.create_record(title="测试", description="描述")
        assert record.category == "feature"

    @pytest.mark.unit
    def test_create_record_custom_category(self, temp_recorder):
        """测试创建记录时指定category。"""
        record = temp_recorder.create_record(
            title="测试", description="描述", category="bugfix"
        )
        assert record.category == "bugfix"

    @pytest.mark.unit
    def test_create_record_with_affected_files(self, temp_recorder):
        """测试创建记录时传入受影响文件。"""
        files = ["modules/fund/service.py", "api/fund.py"]
        record = temp_recorder.create_record(
            title="测试", description="描述", affected_files=files
        )
        assert record.affected_files == files

    @pytest.mark.unit
    def test_create_record_with_affected_nodes(self, temp_recorder):
        """测试创建记录时传入受影响节点。"""
        nodes = ["change_gateway", "gateway_analyzer"]
        record = temp_recorder.create_record(
            title="测试", description="描述", affected_nodes=nodes
        )
        assert record.affected_nodes == nodes

    @pytest.mark.unit
    def test_create_record_with_extra_fields(self, temp_recorder):
        """测试创建记录时通过extra覆盖字段。"""
        record = temp_recorder.create_record(
            title="测试",
            description="描述",
            extra={"risk_level": "high", "gateway_passed": True},
        )
        assert record.risk_level == "high"
        assert record.gateway_passed is True

    @pytest.mark.unit
    def test_create_record_status_default_completed(self, temp_recorder):
        """测试创建记录默认status为completed。"""
        record = temp_recorder.create_record(title="测试", description="描述")
        assert record.status == "completed"


class TestChangeRecorderValidate:
    """测试validate_record。"""

    @pytest.mark.unit
    def test_validate_valid_record(self, temp_recorder):
        """测试验证有效记录返回True。"""
        record = temp_recorder.create_record(
            title="有效变更", description="完整描述"
        )
        valid, errors = temp_recorder.validate_record(record)
        assert valid is True
        assert len(errors) == 0

    @pytest.mark.unit
    def test_validate_empty_title(self, temp_recorder):
        """测试验证空标题记录返回False。"""
        record = temp_recorder.create_record(title="", description="描述")
        valid, errors = temp_recorder.validate_record(record)
        assert valid is False
        assert any("title" in e for e in errors)

    @pytest.mark.unit
    def test_validate_empty_description(self, temp_recorder):
        """测试验证空描述记录返回False。"""
        record = temp_recorder.create_record(title="标题", description="")
        valid, errors = temp_recorder.validate_record(record)
        assert valid is False
        assert any("description" in e for e in errors)

    @pytest.mark.unit
    def test_validate_invalid_id_format(self):
        """测试验证ID格式不正确的记录。"""
        record = ChangeRecord(
            id="INVALID-ID",
            version="2.9.54",
            date="2026-09-07",
            title="测试",
            description="描述",
        )
        recorder = ChangeRecorder.__new__(ChangeRecorder)
        valid, errors = recorder.validate_record(record)
        assert valid is False
        assert any("id 格式不正确" in e for e in errors)

    @pytest.mark.unit
    def test_validate_invalid_date_format(self):
        """测试验证日期格式不正确的记录。"""
        record = ChangeRecord(
            id="CHG-20260907-001",
            version="2.9.54",
            date="2026/09/07",
            title="测试",
            description="描述",
        )
        recorder = ChangeRecorder.__new__(ChangeRecorder)
        valid, errors = recorder.validate_record(record)
        assert valid is False
        assert any("date 格式不正确" in e for e in errors)

    @pytest.mark.unit
    def test_validate_returns_tuple(self, temp_recorder):
        """测试validate_record返回(布尔, 列表)元组。"""
        record = temp_recorder.create_record(title="t", description="d")
        result = temp_recorder.validate_record(record)
        assert isinstance(result, tuple)
        assert len(result) == 2


class TestChangeRecorderHistoryQuery:
    """测试变更历史查询（只读）。"""

    @pytest.mark.unit
    def test_get_recent_changes_returns_list(self, real_recorder):
        """测试get_recent_changes返回列表。"""
        changes = real_recorder.get_recent_changes(count=5)
        assert isinstance(changes, list)

    @pytest.mark.unit
    def test_get_recent_changes_count(self, real_recorder):
        """测试get_recent_changes返回指定数量。"""
        changes = real_recorder.get_recent_changes(count=3)
        assert len(changes) <= 3

    @pytest.mark.unit
    def test_get_recent_changes_sorted_descending(self, real_recorder):
        """测试最近变更按时间倒序排列（最新的在前）。"""
        changes = real_recorder.get_recent_changes(count=5)
        if len(changes) >= 2:
            # ID中的日期和序号应该递减
            assert changes[0]["id"] >= changes[1]["id"]

    @pytest.mark.unit
    def test_get_change_by_id_known(self, real_recorder):
        """测试按已知ID查询变更记录。"""
        change = real_recorder.get_change_by_id("CHG-20260907-045")
        assert change is not None
        assert change["id"] == "CHG-20260907-045"

    @pytest.mark.unit
    def test_get_change_by_id_unknown(self, real_recorder):
        """测试按不存在的ID查询返回None。"""
        change = real_recorder.get_change_by_id("CHG-19990101-999")
        assert change is None

    @pytest.mark.unit
    def test_get_change_by_id_returns_dict(self, real_recorder):
        """测试查询到的变更记录是字典。"""
        change = real_recorder.get_change_by_id("CHG-20260907-045")
        assert isinstance(change, dict)
        assert "title" in change
        assert "version" in change

    @pytest.mark.unit
    def test_get_stats_returns_dict(self, real_recorder):
        """测试get_stats返回字典。"""
        stats = real_recorder.get_stats()
        assert isinstance(stats, dict)

    @pytest.mark.unit
    def test_get_stats_contains_total(self, real_recorder):
        """测试统计包含总变更数。"""
        stats = real_recorder.get_stats()
        assert "total" in stats
        assert stats["total"] > 0

    @pytest.mark.unit
    def test_get_stats_contains_by_category(self, real_recorder):
        """测试统计包含按category分布。"""
        stats = real_recorder.get_stats()
        assert "by_category" in stats
        assert isinstance(stats["by_category"], dict)

    @pytest.mark.unit
    def test_get_stats_contains_by_version(self, real_recorder):
        """测试统计包含按version分布。"""
        stats = real_recorder.get_stats()
        assert "by_version" in stats
        assert isinstance(stats["by_version"], dict)

    @pytest.mark.unit
    def test_load_history_empty_file(self, temp_recorder):
        """测试加载不存在的历史文件返回空结构。"""
        history = temp_recorder.load_history()
        assert "changes" in history
        assert history["changes"] == []
        assert "meta" in history


class TestChangeRecorderAppendWithTemp:
    """测试append_record使用临时文件（不修改真实数据）。"""

    @pytest.mark.unit
    def test_append_record_to_temp_history(self, temp_recorder):
        """测试追加记录到临时历史文件。"""
        record = temp_recorder.create_record(
            title="临时测试变更",
            description="这是追加到临时文件的测试记录",
        )
        result = temp_recorder.append_record(record)
        assert isinstance(result, RecordResult)
        assert result.success is True
        assert result.change_id == record.id

    @pytest.mark.unit
    def test_append_record_increments_count(self, temp_recorder):
        """测试追加记录后历史记录数增加。"""
        record = temp_recorder.create_record(title="t", description="d")
        temp_recorder.append_record(record)
        history = temp_recorder.load_history()
        assert len(history["changes"]) == 1

    @pytest.mark.unit
    def test_append_record_invalid_returns_failure(self, temp_recorder):
        """测试追加无效记录返回失败。"""
        record = ChangeRecord(
            id="", version="", date="", title="", description="", category=""
        )
        result = temp_recorder.append_record(record)
        assert result.success is False
        assert result.error is not None

    @pytest.mark.unit
    def test_save_history_creates_file(self, temp_recorder):
        """测试save_history创建历史文件。"""
        history = {"meta": {}, "changes": []}
        temp_recorder.save_history(history)
        assert temp_recorder.history_path.exists()

    @pytest.mark.unit
    def test_update_config_version_temp(self, temp_recorder):
        """测试更新临时config.py的版本号。"""
        result = temp_recorder.update_config_version("2.9.55")
        assert result is True
        # 验证版本已更新
        new_version = temp_recorder.get_current_version()
        assert new_version == "2.9.55"
