"""
modules/datasource/compare.py —— 多数据源数据对比验证

对比不同数据源的基金净值、评分等数据，标记不一致的基金，
生成数据质量报告，支持手动选择使用哪个数据源的数据。

核心功能：
1. 对比两只数据源的基金净值数据
2. 计算差异度（绝对差、相对差）
3. 标记数据不一致的基金
4. 生成数据质量报告
5. 支持批量对比
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from .base import BaseDataSource, FundInfo, NavPoint
from .manager import get_manager


@dataclass
class NavComparison:
    """单只基金的净值对比结果。"""
    code: str
    source_a: str
    source_b: str
    date: str = ""
    nav_a: Optional[float] = None
    nav_b: Optional[float] = None
    abs_diff: Optional[float] = None      # 绝对差
    rel_diff: Optional[float] = None      # 相对差（%）
    consistent: bool = True                # 是否一致
    detail: str = ""                       # 详细说明


@dataclass
class ComparisonReport:
    """数据对比报告。"""
    report_id: str = ""
    generated_at: str = ""
    source_a: str = ""
    source_b: str = ""
    total_funds: int = 0
    consistent_count: int = 0
    inconsistent_count: int = 0
    error_count: int = 0
    avg_abs_diff: float = 0.0
    max_abs_diff: float = 0.0
    inconsistent_funds: list[dict] = field(default_factory=list)
    details: list[NavComparison] = field(default_factory=list)


class DataComparator:
    """
    多数据源数据对比器。

    用法：
        comparator = DataComparator()
        report = comparator.compare_nav(["000001", "110011"], "eastmoney", "danjuan")
    """

    # 一致性阈值
    NAV_ABS_THRESHOLD = 0.001    # 净值绝对差阈值（0.001 元）
    NAV_REL_THRESHOLD = 0.1      # 净值相对差阈值（0.1%）

    def __init__(self):
        self._manager = get_manager()

    # ------------------------------------------------------------------
    # 单只基金净值对比
    # ------------------------------------------------------------------
    def compare_fund_nav(self, code: str, source_a: str = "eastmoney",
                          source_b: str = "danjuan") -> NavComparison:
        """对比单只基金在两个数据源的最新净值。

        Args:
            code: 基金代码
            source_a: 数据源 A 名称
            source_b: 数据源 B 名称

        Returns:
            NavComparison 对比结果
        """
        result = NavComparison(code=code, source_a=source_a, source_b=source_b)

        src_a = self._manager.get_source(source_a)
        src_b = self._manager.get_source(source_b)

        if not src_a or not src_b:
            result.consistent = False
            result.detail = "数据源不存在"
            return result

        # 获取两个数据源的最新净值
        nav_a = self._safe_get_latest_nav(src_a, code)
        nav_b = self._safe_get_latest_nav(src_b, code)

        if nav_a:
            result.nav_a = nav_a.ljjz
            result.date = nav_a.date
        if nav_b:
            result.nav_b = nav_b.ljjz
            if not result.date:
                result.date = nav_b.date

        # 计算差异
        if nav_a and nav_b and nav_a.ljjz and nav_b.ljjz:
            result.abs_diff = abs(nav_a.ljjz - nav_b.ljjz)
            if nav_a.ljjz != 0:
                result.rel_diff = (result.abs_diff / nav_a.ljjz) * 100
            # 判断一致性
            result.consistent = (
                result.abs_diff <= self.NAV_ABS_THRESHOLD or
                (result.rel_diff is not None and result.rel_diff <= self.NAV_REL_THRESHOLD)
            )
            if not result.consistent:
                result.detail = (f"净值差异过大: A={nav_a.ljjz:.4f}, "
                                 f"B={nav_b.ljjz:.4f}, "
                                 f"绝对差={result.abs_diff:.4f}, "
                                 f"相对差={result.rel_diff:.2f}%")
        elif not nav_a and not nav_b:
            result.consistent = False
            result.detail = "两个数据源均无法获取净值"
        else:
            result.consistent = False
            missing = source_a if not nav_a else source_b
            result.detail = f"数据源 {missing} 无法获取净值"

        return result

    def _safe_get_latest_nav(self, source: BaseDataSource, code: str) -> Optional[NavPoint]:
        """安全获取最新净值，异常返回 None。"""
        try:
            return source.get_latest_nav(code)
        except Exception:
            return None

    # ------------------------------------------------------------------
    # 批量对比
    # ------------------------------------------------------------------
    def compare_nav_batch(self, codes: list[str], source_a: str = "eastmoney",
                           source_b: str = "danjuan",
                           on_progress=None) -> ComparisonReport:
        """批量对比多只基金的净值数据。

        Args:
            codes: 基金代码列表
            source_a: 数据源 A 名称
            source_b: 数据源 B 名称
            on_progress: 进度回调 fn(done, total, code)

        Returns:
            ComparisonReport 对比报告
        """
        report = ComparisonReport(
            report_id=f"cmp_{int(time.time())}",
            generated_at=time.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
            source_a=source_a,
            source_b=source_b,
            total_funds=len(codes),
        )

        abs_diffs = []
        for i, code in enumerate(codes):
            comparison = self.compare_fund_nav(code, source_a, source_b)
            report.details.append(comparison)

            if comparison.detail and "无法" in comparison.detail:
                report.error_count += 1
            elif not comparison.consistent:
                report.inconsistent_count += 1
                report.inconsistent_funds.append({
                    "code": code,
                    "date": comparison.date,
                    "nav_a": comparison.nav_a,
                    "nav_b": comparison.nav_b,
                    "abs_diff": comparison.abs_diff,
                    "rel_diff": comparison.rel_diff,
                    "detail": comparison.detail,
                })
            else:
                report.consistent_count += 1

            if comparison.abs_diff is not None:
                abs_diffs.append(comparison.abs_diff)
                report.max_abs_diff = max(report.max_abs_diff, comparison.abs_diff)

            if on_progress:
                try:
                    on_progress(i + 1, len(codes), code)
                except Exception:
                    pass

        # 统计
        if abs_diffs:
            report.avg_abs_diff = sum(abs_diffs) / len(abs_diffs)

        return report

    # ------------------------------------------------------------------
    # 基金基本信息对比
    # ------------------------------------------------------------------
    def compare_fund_info(self, code: str, source_a: str = "eastmoney",
                           source_b: str = "danjuan") -> dict:
        """对比单只基金的基本信息。

        Returns:
            对比结果字典，包含各字段的一致性
        """
        src_a = self._manager.get_source(source_a)
        src_b = self._manager.get_source(source_b)

        if not src_a or not src_b:
            return {"code": code, "error": "数据源不存在"}

        try:
            info_a = src_a.get_fund_info(code)
            info_b = src_b.get_fund_info(code)
        except Exception:
            return {"code": code, "error": "获取基金信息失败"}

        result = {
            "code": code,
            "source_a": source_a,
            "source_b": source_b,
            "fields": {},
            "all_consistent": True,
        }

        if not info_a or not info_b:
            result["all_consistent"] = False
            result["error"] = "一个或多个数据源无法获取基金信息"
            return result

        # 对比各字段
        fields_to_compare = ["name", "ftype", "manager", "est", "nav", "nav_date"]
        for field_name in fields_to_compare:
            val_a = getattr(info_a, field_name, None)
            val_b = getattr(info_b, field_name, None)
            consistent = (val_a == val_b)
            if field_name == "nav" and val_a and val_b:
                consistent = abs(float(val_a) - float(val_b)) <= self.NAV_ABS_THRESHOLD
            result["fields"][field_name] = {
                "value_a": val_a,
                "value_b": val_b,
                "consistent": consistent,
            }
            if not consistent:
                result["all_consistent"] = False

        return result

    # ------------------------------------------------------------------
    # 数据质量报告
    # ------------------------------------------------------------------
    def generate_quality_report(self, codes: list[str],
                                 source_a: str = "eastmoney",
                                 source_b: str = "danjuan") -> dict:
        """生成数据质量报告（摘要版，不含详细逐基对比）。

        Returns:
            质量报告字典
        """
        report = self.compare_nav_batch(codes, source_a, source_b)
        return {
            "report_id": report.report_id,
            "generated_at": report.generated_at,
            "source_a": report.source_a,
            "source_b": report.source_b,
            "summary": {
                "total": report.total_funds,
                "consistent": report.consistent_count,
                "inconsistent": report.inconsistent_count,
                "errors": report.error_count,
                "consistency_rate": (
                    round(report.consistent_count / report.total_funds * 100, 1)
                    if report.total_funds > 0 else 0
                ),
            },
            "stats": {
                "avg_abs_diff": round(report.avg_abs_diff, 6),
                "max_abs_diff": round(report.max_abs_diff, 6),
            },
            "inconsistent_funds": report.inconsistent_funds[:50],  # 最多返回50条
        }


# 全局单例
_comparator_instance: Optional[DataComparator] = None


def get_comparator() -> DataComparator:
    """获取全局数据对比器单例。"""
    global _comparator_instance
    if _comparator_instance is None:
        _comparator_instance = DataComparator()
    return _comparator_instance
