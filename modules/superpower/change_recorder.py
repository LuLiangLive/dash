"""
变更记录器（ChangeRecorder）

Superpower工作流引擎的核心模块之一，负责：
- 读取和升级应用版本号（config.py）
- 生成变更记录（ChangeRecord）
- 追加变更记录到 change_history.json
- 提供变更历史查询和统计

版本历史：
  1.0.0 (2026-09-07) - 初始版本，实现版本管理、变更记录生成与持久化
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# 后端根目录（本模块位于 backend/modules/superpower/，向上三级为 backend/）
_BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent

# config.py 中版本号的正则模式
_VERSION_PATTERN = re.compile(r'(_env_str\("APP_VERSION",\s*)"([^"]+)"(\))')


def _now_iso() -> str:
    """返回当前时间的ISO格式字符串。"""
    return datetime.now().isoformat()


def _today_str() -> str:
    """返回今天的日期字符串 YYYY-MM-DD。"""
    return datetime.now().strftime("%Y-%m-%d")


def _today_compact() -> str:
    """返回今天的紧凑日期字符串 YYYYMMDD。"""
    return datetime.now().strftime("%Y%m%d")


@dataclass
class ChangeRecord:
    """变更记录数据结构。

    Attributes:
        id: 变更ID，格式 CHG-YYYYMMDD-NNN
        version: 关联的应用版本号
        date: 变更日期（YYYY-MM-DD）
        title: 变更标题
        description: 变更详细描述
        category: 变更分类（功能/修复/优化/重构/数据质量/基础设施/UI/UX等）
        priority: 优先级（high/medium/low）
        status: 状态（completed/in_progress/planned）
        affected_modules: 受影响模块列表
        affected_files: 受影响文件列表
        affected_nodes: 受影响神经网络节点列表
        changes: 具体变更列表，每项 {file, type, description}
        testing: 测试信息 {unit_tests, integration_tests, results}
        impact: 影响分析 {positive, negative, risks}
        verification: 验证步骤列表
        risk_assessment: 风险评估文字描述
        risk_level: 风险等级（high/medium/low）
        gateway_passed: 是否通过网关检查
        tested: 是否经过测试
    """
    id: str
    version: str
    date: str
    title: str
    description: str
    category: str = "feature"
    priority: str = "medium"
    status: str = "completed"
    affected_modules: List[str] = field(default_factory=list)
    affected_files: List[str] = field(default_factory=list)
    affected_nodes: List[str] = field(default_factory=list)
    changes: List[Dict] = field(default_factory=list)
    testing: Dict = field(default_factory=lambda: {
        "unit_tests": "",
        "integration_tests": "",
        "results": "",
    })
    impact: Dict = field(default_factory=lambda: {
        "positive": [],
        "negative": [],
        "risks": [],
    })
    verification: List[str] = field(default_factory=list)
    risk_assessment: str = ""
    risk_level: str = "medium"
    gateway_passed: bool = False
    tested: bool = False


@dataclass
class RecordResult:
    """变更记录操作结果。

    Attributes:
        success: 是否成功
        change_id: 生成的变更ID
        new_version: 升级后的新版本号
        record_path: 记录文件路径
        error: 错误信息（失败时）
    """
    success: bool
    change_id: str = ""
    new_version: str = ""
    record_path: str = ""
    error: Optional[str] = None


class ChangeRecorder:
    """变更记录器。

    负责管理应用版本号和变更历史记录。支持从config.py读取版本号、
    语义化版本升级、生成变更ID、创建和验证变更记录、追加到
    change_history.json等完整功能。

    Usage:
        recorder = ChangeRecorder()
        version = recorder.get_current_version()  # "2.9.54"
        new_ver = recorder.bump_version("patch")  # "2.9.55"
        result = recorder.record(
            title="新增功能",
            description="...",
            category="feature",
        )
    """

    def __init__(self, history_path: str = "docs/change_history.json",
                 config_path: str = "config.py") -> None:
        """初始化变更记录器。

        Args:
            history_path: change_history.json路径（相对路径基于backend根目录）
            config_path: config.py路径（相对路径基于backend根目录）
        """
        self.history_path = self._resolve_path(history_path)
        self.config_path = self._resolve_path(config_path)

    @staticmethod
    def _resolve_path(path_str: str) -> Path:
        """将相对路径解析为基于backend根目录的绝对路径。"""
        p = Path(path_str)
        if p.is_absolute():
            return p
        return _BACKEND_ROOT / p

    # ── 版本管理 ──────────────────────────────────────────

    def get_current_version(self) -> str:
        """从config.py读取当前版本号。

        通过正则匹配 _env_str("APP_VERSION", "x.x.x") 格式。

        Returns:
            当前版本号字符串，读取失败返回 "0.0.0"
        """
        try:
            content = self.config_path.read_text(encoding="utf-8")
            match = _VERSION_PATTERN.search(content)
            if match:
                return match.group(2)
        except (OSError, IOError):
            pass
        return "0.0.0"

    def bump_version(self, level: str = "patch") -> str:
        """升级版本号。

        Args:
            level: 升级级别（patch/minor/major）
                - patch: 2.9.54 -> 2.9.55
                - minor: 2.9.54 -> 2.10.0
                - major: 2.9.54 -> 3.0.0

        Returns:
            升级后的新版本号
        """
        current = self.get_current_version()
        major, minor, patch = self._parse_version(current)

        if level == "major":
            major += 1
            minor = 0
            patch = 0
        elif level == "minor":
            minor += 1
            patch = 0
        else:  # patch
            patch += 1

        return self._format_version(major, minor, patch)

    @staticmethod
    def _parse_version(version_str: str) -> Tuple[int, int, int]:
        """解析版本号字符串为 (major, minor, patch) 元组。

        Args:
            version_str: 版本号字符串，如 "2.9.54"

        Returns:
            (major, minor, patch) 整数元组
        """
        parts = version_str.strip().split(".")
        major = int(parts[0]) if len(parts) > 0 and parts[0].isdigit() else 0
        minor = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        patch = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        return (major, minor, patch)

    @staticmethod
    def _format_version(major: int, minor: int, patch: int) -> str:
        """格式化版本号。

        Args:
            major: 主版本号
            minor: 次版本号
            patch: 修订号

        Returns:
            格式化的版本号字符串，如 "2.9.55"
        """
        return f"{major}.{minor}.{patch}"

    # ── 变更记录生成 ──────────────────────────────────────

    def generate_change_id(self) -> str:
        """生成变更ID。

        基于当前日期和已有记录的最大序号，格式 CHG-YYYYMMDD-NNN。

        Returns:
            新的变更ID字符串
        """
        today = _today_compact()
        prefix = f"CHG-{today}-"
        max_seq = 0

        try:
            history = self.load_history()
            for record in history.get("changes", []):
                rid = record.get("id", "")
                if rid.startswith(prefix):
                    seq_str = rid[len(prefix):]
                    if seq_str.isdigit():
                        max_seq = max(max_seq, int(seq_str))
        except (OSError, IOError, json.JSONDecodeError):
            pass

        return f"{prefix}{max_seq + 1:03d}"

    def create_record(self, title: str, description: str,
                      category: str = "feature",
                      priority: str = "medium",
                      affected_files: Optional[List[str]] = None,
                      affected_nodes: Optional[List[str]] = None,
                      changes: Optional[List[Dict]] = None,
                      extra: Optional[Dict] = None) -> ChangeRecord:
        """创建变更记录对象，自动填充id、version、date、status等。

        Args:
            title: 变更标题
            description: 变更详细描述
            category: 变更分类
            priority: 优先级
            affected_files: 受影响文件列表
            affected_nodes: 受影响节点列表
            changes: 具体变更列表
            extra: 额外字段字典，用于覆盖或补充其他字段

        Returns:
            填充完整的ChangeRecord对象
        """
        record = ChangeRecord(
            id=self.generate_change_id(),
            version=self.get_current_version(),
            date=_today_str(),
            title=title,
            description=description,
            category=category,
            priority=priority,
            status="completed",
            affected_files=list(affected_files) if affected_files else [],
            affected_nodes=list(affected_nodes) if affected_nodes else [],
            changes=list(changes) if changes else [],
        )

        # 应用额外字段
        if extra:
            for key, value in extra.items():
                if hasattr(record, key):
                    setattr(record, key, value)

        return record

    def validate_record(self, record: ChangeRecord) -> Tuple[bool, List[str]]:
        """验证变更记录完整性。

        检查必填字段：id, version, date, title, description, category。

        Args:
            record: 待验证的变更记录

        Returns:
            (是否通过验证, 错误信息列表)
        """
        errors: List[str] = []

        if not record.id:
            errors.append("id 不能为空")
        elif not re.match(r"^CHG-\d{8}-\d{3}$", record.id):
            errors.append(f"id 格式不正确: {record.id}（应为 CHG-YYYYMMDD-NNN）")

        if not record.version:
            errors.append("version 不能为空")

        if not record.date:
            errors.append("date 不能为空")
        elif not re.match(r"^\d{4}-\d{2}-\d{2}$", record.date):
            errors.append(f"date 格式不正确: {record.date}（应为 YYYY-MM-DD）")

        if not record.title:
            errors.append("title 不能为空")

        if not record.description:
            errors.append("description 不能为空")

        if not record.category:
            errors.append("category 不能为空")

        return (len(errors) == 0, errors)

    # ── 变更记录写入 ──────────────────────────────────────

    def load_history(self) -> Dict:
        """加载change_history.json。

        Returns:
            变更历史字典，包含 meta 和 changes
        """
        if not self.history_path.exists():
            return {
                "meta": {
                    "version": self.get_current_version(),
                    "total_changes": 0,
                    "last_updated": _now_iso(),
                    "current_version": self.get_current_version(),
                },
                "changes": [],
            }
        content = self.history_path.read_text(encoding="utf-8")
        return json.loads(content)

    def save_history(self, history: Dict) -> None:
        """保存change_history.json。

        使用UTF-8编码，indent=2，ensure_ascii=False。

        Args:
            history: 变更历史字典
        """
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(history, ensure_ascii=False, indent=2)
        self.history_path.write_text(content, encoding="utf-8")

    def append_record(self, record: ChangeRecord) -> RecordResult:
        """追加变更记录到change_history.json。

        执行流程：
        1. 加载历史
        2. 验证记录
        3. 追加到changes数组
        4. 更新meta（version, total_changes, last_updated, current_version）
        5. 保存文件
        6. 返回RecordResult

        Args:
            record: 要追加的变更记录

        Returns:
            RecordResult操作结果
        """
        try:
            # 1. 加载历史
            history = self.load_history()

            # 2. 验证记录
            valid, errors = self.validate_record(record)
            if not valid:
                return RecordResult(
                    success=False,
                    change_id=record.id,
                    error=f"记录验证失败: {'; '.join(errors)}",
                )

            # 3. 追加到changes数组（新记录放在最前面）
            record_dict = asdict(record)
            history.setdefault("changes", []).insert(0, record_dict)

            # 4. 更新meta
            meta = history.setdefault("meta", {})
            meta["version"] = record.version
            meta["total_changes"] = len(history["changes"])
            meta["last_updated"] = _now_iso()
            meta["current_version"] = record.version

            # 5. 保存文件
            self.save_history(history)

            # 6. 返回结果
            return RecordResult(
                success=True,
                change_id=record.id,
                new_version=record.version,
                record_path=str(self.history_path),
            )
        except Exception as e:
            return RecordResult(
                success=False,
                change_id=getattr(record, "id", ""),
                error=f"追加记录失败: {str(e)}",
            )

    def update_config_version(self, new_version: str) -> bool:
        """更新config.py中的版本号。

        用正则替换 _env_str("APP_VERSION", "旧版本") 为新版本。

        Args:
            new_version: 新版本号

        Returns:
            是否更新成功
        """
        try:
            content = self.config_path.read_text(encoding="utf-8")
            new_content, count = _VERSION_PATTERN.subn(
                lambda m: f'{m.group(1)}"{new_version}"{m.group(3)}',
                content,
            )
            if count == 0:
                return False
            self.config_path.write_text(new_content, encoding="utf-8")
            return True
        except (OSError, IOError):
            return False

    def update_graph_version(self, new_version: str) -> bool:
        """同步更新 function_graph.json 的 meta.version（v2.11.5 机制补漏）。

        Args:
            new_version: 新版本号

        Returns:
            是否更新成功
        """
        try:
            graph_path = self.config_path.parent / "docs" / "function_graph.json"
            if not graph_path.exists():
                return False
            graph = json.loads(graph_path.read_text(encoding="utf-8"))
            meta = graph.setdefault("meta", {})
            meta["version"] = new_version
            meta["last_updated"] = _now_iso()
            graph_path.write_text(
                json.dumps(graph, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            return True
        except Exception:
            return False

    def record(self, title: str, description: str,
               category: str = "feature",
               priority: str = "medium",
               affected_files: Optional[List[str]] = None,
               affected_nodes: Optional[List[str]] = None,
               changes: Optional[List[Dict]] = None,
               bump: bool = True,
               level: str = "patch") -> RecordResult:
        """完整变更记录流程。

        执行：创建记录 -> 升级版本（可选） -> 更新config -> 追加到历史。

        Args:
            title: 变更标题
            description: 变更描述
            category: 变更分类
            priority: 优先级
            affected_files: 受影响文件
            affected_nodes: 受影响节点
            changes: 具体变更列表
            bump: 是否升级版本号
            level: 版本升级级别（patch/minor/major）

        Returns:
            RecordResult操作结果
        """
        try:
            # 1. 升级版本（如果需要）
            new_version = self.get_current_version()
            if bump:
                new_version = self.bump_version(level)
                # 更新config.py
                if not self.update_config_version(new_version):
                    return RecordResult(
                        success=False,
                        error=f"更新config.py版本号失败: {new_version}",
                    )
                # v2.11.5: 同步 function_graph.json meta.version
                # （此前无任何组件更新图谱版本，导致四处版本号漂移）
                self.update_graph_version(new_version)

            # 2. 创建记录（使用升级后的版本号）
            record = self.create_record(
                title=title,
                description=description,
                category=category,
                priority=priority,
                affected_files=affected_files,
                affected_nodes=affected_nodes,
                changes=changes,
            )
            record.version = new_version

            # 3. 追加到历史
            result = self.append_record(record)
            return result
        except Exception as e:
            return RecordResult(
                success=False,
                error=f"记录变更失败: {str(e)}",
            )

    # ── 工具方法 ──────────────────────────────────────────

    def get_recent_changes(self, count: int = 5) -> List[Dict]:
        """获取最近N条变更记录。

        Args:
            count: 返回的记录数量

        Returns:
            最近的变更记录列表（按时间倒序）
        """
        try:
            history = self.load_history()
            changes = history.get("changes", [])
            return changes[:count]
        except (OSError, IOError, json.JSONDecodeError):
            return []

    def get_change_by_id(self, change_id: str) -> Optional[Dict]:
        """按ID查询变更记录。

        Args:
            change_id: 变更ID

        Returns:
            匹配的变更记录字典，未找到返回None
        """
        try:
            history = self.load_history()
            for record in history.get("changes", []):
                if record.get("id") == change_id:
                    return record
        except (OSError, IOError, json.JSONDecodeError):
            pass
        return None

    def get_stats(self) -> Dict:
        """返回变更统计信息。

        Returns:
            包含总数、按category分布、按version分布的统计字典
        """
        try:
            history = self.load_history()
            changes = history.get("changes", [])
        except (OSError, IOError, json.JSONDecodeError):
            changes = []

        by_category: Dict[str, int] = {}
        by_version: Dict[str, int] = {}

        for record in changes:
            cat = record.get("category", "unknown")
            by_category[cat] = by_category.get(cat, 0) + 1

            ver = record.get("version", "unknown")
            by_version[ver] = by_version.get(ver, 0) + 1

        return {
            "total": len(changes),
            "by_category": by_category,
            "by_version": by_version,
        }

    def __repr__(self) -> str:
        version = self.get_current_version()
        return f"<ChangeRecorder version={version} history={self.history_path.name}>"
