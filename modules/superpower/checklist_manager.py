"""
检查清单管理器（ChecklistManager）

Superpower工作流引擎的核心模块之一，负责：
- 从网关分析结果自动生成变更检查清单
- 跟踪检查项状态（pending/in_progress/completed/skipped）
- 验证检查项执行结果
- 生成进度报告和文本报告

工作流阶段：分析阶段 -> 修改阶段 -> 验证阶段 -> 更新阶段 -> 记录阶段

版本历史：
  1.0.0 (2026-09-07) - 初始版本，实现检查清单生成、跟踪、验证和报告
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Dict, List, Literal, Optional, Tuple


# ── 阶段缩写映射 ──────────────────────────────────────────
_STAGE_ABBR: Dict[str, str] = {
    "分析阶段": "ANA",
    "修改阶段": "MOD",
    "验证阶段": "VER",
    "更新阶段": "UPD",
    "记录阶段": "REC",
    "基础框架": "BASE",
    "网关分析器": "GATE",
    "检查清单管理器": "CHK",
    "神经网络更新器": "NEU",
    "变更记录器": "CHG",
    "集成和测试": "INT",
}

# 工作流标准阶段（generate_from_analysis 使用）
_WORKFLOW_STAGES: List[str] = ["分析阶段", "修改阶段", "验证阶段", "更新阶段", "记录阶段"]


def _now_iso() -> str:
    """返回当前时间的ISO格式字符串。"""
    return datetime.now().isoformat()


def _stage_abbr(stage: str) -> str:
    """获取阶段缩写，未知阶段返回前3个字符大写。"""
    return _STAGE_ABBR.get(stage, stage[:3].upper())


@dataclass
class ChecklistItem:
    """单个检查项数据结构。

    Attributes:
        id: 唯一标识，格式 CL-{阶段缩写}-{序号}
        stage: 所属阶段（分析阶段/修改阶段/验证阶段/更新阶段/记录阶段等）
        description: 检查项描述
        priority: 优先级（must/should/optional）
        status: 状态（pending/in_progress/completed/skipped）
        verified: 是否已验证通过
        verified_at: 验证时间
        notes: 备注信息
        created_at: 创建时间
        updated_at: 最后更新时间
    """
    id: str
    stage: str
    description: str
    priority: Literal["must", "should", "optional"] = "should"
    status: Literal["pending", "in_progress", "completed", "skipped"] = "pending"
    verified: bool = False
    verified_at: Optional[str] = None
    notes: Optional[str] = None
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)


@dataclass
class ChecklistReport:
    """检查清单统计报告。

    Attributes:
        total: 检查项总数
        completed: 已完成数
        pending: 待处理数
        in_progress: 进行中数
        skipped: 已跳过数
        verified: 已验证通过数
        must_completed: must优先级已完成数
        must_total: must优先级总数
        coverage_rate: 整体完成率（0.0~1.0）
        by_stage: 按阶段统计
        incomplete_must: 未完成的must检查项id列表
    """
    total: int
    completed: int
    pending: int
    in_progress: int
    skipped: int
    verified: int
    must_completed: int
    must_total: int
    coverage_rate: float
    by_stage: Dict[str, Dict]
    incomplete_must: List[str]


class ChecklistManager:
    """检查清单管理器。

    负责生成、跟踪、验证变更检查清单，支持从网关分析结果自动生成，
    也支持手动增删改。提供完整的进度统计和文本报告能力。

    Usage:
        manager = ChecklistManager()
        items = manager.generate_from_analysis(analysis_result)
        manager.start_item(items[0].id)
        manager.complete_item(items[0].id, notes="已完成")
        report = manager.generate_report()
        print(manager.generate_text_report())
    """

    def __init__(self, items: Optional[List[ChecklistItem]] = None) -> None:
        """初始化检查清单管理器。

        Args:
            items: 初始检查项列表，为空时创建空清单
        """
        self._items: List[ChecklistItem] = list(items) if items else []
        self._counters: Dict[str, int] = {}  # 按阶段的序号计数器

    # ── 生成方法 ──────────────────────────────────────────

    def _next_id(self, stage: str) -> str:
        """生成指定阶段的下一个检查项ID。"""
        abbr = _stage_abbr(stage)
        self._counters[abbr] = self._counters.get(abbr, 0) + 1
        return f"CL-{abbr}-{self._counters[abbr]:03d}"

    def _add_internal(self, stage: str, description: str,
                      priority: str = "should") -> ChecklistItem:
        """内部添加检查项（不触发公开接口的额外逻辑）。"""
        item = ChecklistItem(
            id=self._next_id(stage),
            stage=stage,
            description=description,
            priority=priority,  # type: ignore[arg-type]
        )
        self._items.append(item)
        return item

    def generate_from_analysis(self, analysis_result: Dict) -> List[ChecklistItem]:
        """从网关分析结果生成检查清单。

        根据影响范围（forward_affected/backward_affected节点数）和风险点
        （risks）自动生成检查项，按工作流阶段分组。

        Args:
            analysis_result: 网关分析结果字典，可能包含：
                - forward_affected: 前向影响节点列表或数量
                - backward_affected: 后向影响节点列表或数量
                - risks: 风险列表，每项含 level(HIGH/MEDIUM/LOW) 和 description
                - affected_modules: 受影响模块列表

        Returns:
            生成的检查项列表
        """
        # 解析影响节点数
        forward = analysis_result.get("forward_affected", [])
        backward = analysis_result.get("backward_affected", [])
        fwd_count = len(forward) if isinstance(forward, list) else int(forward or 0)
        bwd_count = len(backward) if isinstance(backward, list) else int(backward or 0)
        total_affected = fwd_count + bwd_count

        # 解析风险
        risks = analysis_result.get("risks", [])
        high_risks = [r for r in risks if str(r.get("level", "")).upper() == "HIGH"]
        medium_risks = [r for r in risks if str(r.get("level", "")).upper() == "MEDIUM"]
        low_risks = [r for r in risks if str(r.get("level", "")).upper() == "LOW"]

        generated: List[ChecklistItem] = []

        # ── 分析阶段 ──
        generated.append(self._add_internal(
            "分析阶段",
            f"确认变更影响范围：前向影响{fwd_count}个节点，后向影响{bwd_count}个节点，共{total_affected}个",
            "must",
        ))
        generated.append(self._add_internal(
            "分析阶段",
            "审查网关分析报告，确认风险评估和传播路径准确性",
            "should",
        ))

        # ── 修改阶段：每个风险点生成对应检查项 ──
        for risk in high_risks:
            desc = risk.get("description", str(risk))
            generated.append(self._add_internal(
                "修改阶段",
                f"[高风险] {desc}",
                "must",
            ))
        for risk in medium_risks:
            desc = risk.get("description", str(risk))
            generated.append(self._add_internal(
                "修改阶段",
                f"[中风险] {desc}",
                "should",
            ))
        for risk in low_risks:
            desc = risk.get("description", str(risk))
            generated.append(self._add_internal(
                "修改阶段",
                f"[低风险] {desc}",
                "optional",
            ))

        if not risks:
            generated.append(self._add_internal(
                "修改阶段",
                "执行代码修改，确保变更符合分析报告要求",
                "should",
            ))

        # ── 验证阶段：受影响节点验证 ──
        if total_affected > 0:
            generated.append(self._add_internal(
                "验证阶段",
                f"验证所有{total_affected}个受影响节点的功能正确性",
                "must",
            ))
            # 为每个受影响节点（最多5个）生成单独验证项
            affected_nodes: List[str] = []
            if isinstance(forward, list):
                affected_nodes.extend(str(n) for n in forward)
            if isinstance(backward, list):
                affected_nodes.extend(str(n) for n in backward)
            for node_name in affected_nodes[:5]:
                generated.append(self._add_internal(
                    "验证阶段",
                    f"回归验证节点：{node_name}",
                    "should",
                ))
        else:
            generated.append(self._add_internal(
                "验证阶段",
                "执行单元测试和集成测试，验证变更正确性",
                "must",
            ))

        generated.append(self._add_internal(
            "验证阶段",
            "确认无回归问题，所有相关测试通过",
            "should",
        ))

        # ── 更新阶段 ──
        generated.append(self._add_internal(
            "更新阶段",
            "更新function_graph.json神经网络图（如有节点结构变更）",
            "should",
        ))
        generated.append(self._add_internal(
            "更新阶段",
            "同步更新前端版本号和后端config.py版本号",
            "optional",
        ))

        # ── 记录阶段 ──
        generated.append(self._add_internal(
            "记录阶段",
            "生成变更记录并写入change_history.json",
            "must",
        ))
        generated.append(self._add_internal(
            "记录阶段",
            "升级版本号（patch/minor/major）",
            "should",
        ))

        return generated

    def add_item(self, stage: str, description: str,
                 priority: str = "should") -> ChecklistItem:
        """手动添加检查项。

        Args:
            stage: 所属阶段
            description: 检查项描述
            priority: 优先级（must/should/optional）

        Returns:
            新创建的检查项
        """
        return self._add_internal(stage, description, priority)

    def remove_item(self, item_id: str) -> bool:
        """手动删除检查项。

        Args:
            item_id: 检查项ID

        Returns:
            是否删除成功
        """
        for i, item in enumerate(self._items):
            if item.id == item_id:
                self._items.pop(i)
                return True
        return False

    def update_item(self, item_id: str, **kwargs) -> Optional[ChecklistItem]:
        """修改检查项属性。

        Args:
            item_id: 检查项ID
            **kwargs: 要修改的属性键值对

        Returns:
            修改后的检查项，未找到返回None
        """
        item = self._find_item(item_id)
        if item is None:
            return None
        allowed = {"stage", "description", "priority", "status", "verified", "notes"}
        for key, value in kwargs.items():
            if key in allowed and hasattr(item, key):
                setattr(item, key, value)
        item.updated_at = _now_iso()
        return item

    # ── 跟踪方法 ──────────────────────────────────────────

    def _find_item(self, item_id: str) -> Optional[ChecklistItem]:
        """按ID查找检查项。"""
        for item in self._items:
            if item.id == item_id:
                return item
        return None

    def start_item(self, item_id: str) -> bool:
        """标记检查项为进行中。

        Args:
            item_id: 检查项ID

        Returns:
            是否操作成功
        """
        item = self._find_item(item_id)
        if item is None:
            return False
        item.status = "in_progress"  # type: ignore[assignment]
        item.updated_at = _now_iso()
        return True

    def complete_item(self, item_id: str, notes: Optional[str] = None) -> bool:
        """标记检查项为已完成。

        Args:
            item_id: 检查项ID
            notes: 完成备注

        Returns:
            是否操作成功
        """
        item = self._find_item(item_id)
        if item is None:
            return False
        item.status = "completed"  # type: ignore[assignment]
        if notes is not None:
            item.notes = notes
        item.updated_at = _now_iso()
        return True

    def skip_item(self, item_id: str, reason: str) -> bool:
        """标记检查项为已跳过。

        must优先级的检查项不允许跳过。

        Args:
            item_id: 检查项ID
            reason: 跳过原因

        Returns:
            是否操作成功（must项返回False）
        """
        item = self._find_item(item_id)
        if item is None:
            return False
        if item.priority == "must":
            return False
        item.status = "skipped"  # type: ignore[assignment]
        item.notes = reason
        item.updated_at = _now_iso()
        return True

    def verify_item(self, item_id: str, passed: bool,
                    notes: Optional[str] = None) -> bool:
        """验证检查项，设置verified字段。

        Args:
            item_id: 检查项ID
            passed: 是否验证通过
            notes: 验证备注

        Returns:
            是否操作成功
        """
        item = self._find_item(item_id)
        if item is None:
            return False
        item.verified = passed
        item.verified_at = _now_iso()
        if notes is not None:
            item.notes = notes
        item.updated_at = _now_iso()
        return True

    # ── 验证方法 ──────────────────────────────────────────

    def verify_all(self) -> Tuple[bool, List[str]]:
        """逐项验证所有must优先级且status=completed的项。

        Returns:
            (是否全部通过, 未通过项id列表)
        """
        failed: List[str] = []
        for item in self._items:
            if item.priority == "must" and item.status == "completed":
                if not item.verified:
                    failed.append(item.id)
        return (len(failed) == 0, failed)

    def get_incomplete_must(self) -> List[ChecklistItem]:
        """获取所有未完成的must优先级检查项。

        Returns:
            未完成的must检查项列表
        """
        return [
            item for item in self._items
            if item.priority == "must" and item.status != "completed"
        ]

    def is_ready_for_verification(self) -> bool:
        """判断是否所有must项都已completed，可以进入验证阶段。

        Returns:
            True表示所有must项已完成
        """
        return len(self.get_incomplete_must()) == 0

    # ── 报告方法 ──────────────────────────────────────────

    def generate_report(self) -> ChecklistReport:
        """生成检查清单统计报告。

        Returns:
            ChecklistReport统计报告对象
        """
        total = len(self._items)
        completed = sum(1 for i in self._items if i.status == "completed")
        pending = sum(1 for i in self._items if i.status == "pending")
        in_progress = sum(1 for i in self._items if i.status == "in_progress")
        skipped = sum(1 for i in self._items if i.status == "skipped")
        verified = sum(1 for i in self._items if i.verified)

        must_items = [i for i in self._items if i.priority == "must"]
        must_total = len(must_items)
        must_completed = sum(1 for i in must_items if i.status == "completed")

        # 按阶段统计
        by_stage: Dict[str, Dict] = {}
        for item in self._items:
            if item.stage not in by_stage:
                by_stage[item.stage] = {
                    "total": 0, "completed": 0, "pending": 0,
                    "in_progress": 0, "skipped": 0, "must_total": 0,
                    "must_completed": 0,
                }
            s = by_stage[item.stage]
            s["total"] += 1
            s[item.status] += 1
            if item.priority == "must":
                s["must_total"] += 1
                if item.status == "completed":
                    s["must_completed"] += 1

        incomplete_must = [i.id for i in self.get_incomplete_must()]

        coverage_rate = (completed / total) if total > 0 else 0.0

        return ChecklistReport(
            total=total,
            completed=completed,
            pending=pending,
            in_progress=in_progress,
            skipped=skipped,
            verified=verified,
            must_completed=must_completed,
            must_total=must_total,
            coverage_rate=round(coverage_rate, 4),
            by_stage=by_stage,
            incomplete_must=incomplete_must,
        )

    def generate_text_report(self) -> str:
        """生成可读的文本格式报告。

        包含进度条、按阶段分组、未完成must项高亮。

        Returns:
            格式化的文本报告字符串
        """
        report = self.generate_report()
        lines: List[str] = []

        lines.append("=" * 60)
        lines.append("检查清单报告")
        lines.append("=" * 60)

        # 总进度条
        pct = report.coverage_rate * 100
        filled = int(pct / 5)  # 20格进度条
        bar = "\u2588" * filled + "\u2591" * (20 - filled)
        lines.append(f"总进度: [{bar}] {pct:.1f}% ({report.completed}/{report.total})")

        # Must进度条
        if report.must_total > 0:
            must_pct = (report.must_completed / report.must_total) * 100
            must_filled = int(must_pct / 5)
            must_bar = "\u2588" * must_filled + "\u2591" * (20 - must_filled)
            lines.append(f"Must项: [{must_bar}] {must_pct:.1f}% ({report.must_completed}/{report.must_total})")

        lines.append(
            f"状态分布: 待处理={report.pending}  进行中={report.in_progress}  "
            f"已完成={report.completed}  已跳过={report.skipped}  已验证={report.verified}"
        )
        lines.append("")

        # 按阶段分组
        for stage in _WORKFLOW_STAGES:
            if stage not in report.by_stage:
                continue
            s = report.by_stage[stage]
            lines.append(f"── {stage} ({s['completed']}/{s['total']}) ──")
            stage_items = [i for i in self._items if i.stage == stage]
            for item in stage_items:
                if item.status == "completed":
                    mark = "\u2713" if item.verified else "\u25cb"
                elif item.status == "in_progress":
                    mark = "\u25b6"
                elif item.status == "skipped":
                    mark = "\u23ed"
                else:
                    mark = "\u25a1"
                pri_tag = f"[{item.priority.upper()}]"
                lines.append(f"  {mark} {item.id} {pri_tag} {item.description}")
                if item.notes:
                    lines.append(f"      \u2514 {item.notes}")
            lines.append("")

        # 未完成must项高亮
        if report.incomplete_must:
            lines.append("\u26a0 未完成的Must项（必须完成才能进入验证阶段）:")
            for item_id in report.incomplete_must:
                item = self._find_item(item_id)
                if item:
                    lines.append(f"  - [{item.status}] {item.id}: {item.description}")
            lines.append("")

        # 验证状态
        all_pass, failed = self.verify_all()
        if all_pass:
            lines.append("\u2705 所有Must项验证通过")
        else:
            lines.append(f"\u274c {len(failed)}个Must项未验证: {', '.join(failed)}")

        lines.append("=" * 60)
        return "\n".join(lines)

    def to_dict(self) -> Dict:
        """序列化为字典。

        Returns:
            包含所有检查项的字典
        """
        return {
            "items": [asdict(item) for item in self._items],
            "counters": dict(self._counters),
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "ChecklistManager":
        """从字典反序列化创建ChecklistManager。

        Args:
            data: to_dict()生成的字典

        Returns:
            反序列化后的ChecklistManager实例
        """
        manager = cls()
        for item_data in data.get("items", []):
            item = ChecklistItem(**item_data)
            manager._items.append(item)
        manager._counters = dict(data.get("counters", {}))
        return manager

    @property
    def items(self) -> List[ChecklistItem]:
        """返回所有检查项的副本。"""
        return list(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __repr__(self) -> str:
        report = self.generate_report()
        return f"<ChecklistManager total={report.total} completed={report.completed} rate={report.coverage_rate:.1%}>"
