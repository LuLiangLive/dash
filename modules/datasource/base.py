"""
modules/datasource/base.py —— 数据源抽象基类与接口规范

定义所有第三方数据源必须实现的统一接口，确保数据格式与现有系统一致。

数据格式规范（与 funds 表 / nav_history 表对齐）：
- FundInfo: 基金基本信息（code, name, ftype, scale, manager, nav, nav_date 等）
- NavPoint: 净值点（date, ljjz, dwjz, jzzzl）
- HoldingItem: 持仓项（name, code, pct）
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class FundInfo:
    """基金基本信息（与 funds 表字段对齐）。"""
    code: str
    name: str = ""
    ftype: str = ""          # 基金类型（股票型/混合型/债券型等）
    scale: Optional[float] = None   # 基金规模（亿元）
    manager: str = ""        # 基金经理
    est: str = ""            # 成立日期
    track: str = ""          # 跟踪指数
    sec: str = "其他"        # 所属板块
    nav: Optional[float] = None     # 最新单位净值
    nav_date: str = ""       # 最新净值日期 YYYY-MM-DD
    source: str = ""         # 数据来源标识


@dataclass
class NavPoint:
    """净值数据点（与 nav_history 表字段对齐）。"""
    date: str                 # YYYY-MM-DD
    ljjz: float               # 累计净值
    dwjz: Optional[float] = None  # 单位净值
    jzzzl: Optional[float] = None  # 日增长率（%）


@dataclass
class HoldingItem:
    """持仓项（与 funds.stocks JSON 字段对齐）。"""
    name: str
    code: str = ""
    pct: float = 0.0         # 持仓占比（%）


class BaseDataSource(ABC):
    """
    第三方数据源抽象基类。

    所有数据源必须实现以下方法：
    - get_fund_info: 获取基金基本信息
    - get_latest_nav: 获取最新净值
    - get_nav_history: 获取历史净值序列
    - get_holdings: 获取前十大持仓

    可选实现：
    - get_score: 获取数据源评分（如果数据源提供）
    - health_check: 数据源可用性检查
    """

    # 子类必须设置的属性
    name: str = "base"
    display_name: str = "基础数据源"
    base_url: str = ""
    request_interval: float = 0.1  # 请求间隔（秒），用于频率限制

    def __init__(self):
        self._last_request_time = 0.0
        self._consecutive_errors = 0
        self._available = True

    # ------------------------------------------------------------------
    # 频率控制
    # ------------------------------------------------------------------
    def _wait_for_rate_limit(self):
        """请求间隔控制，避免触发第三方 API 频率限制。"""
        elapsed = time.time() - self._last_request_time
        if elapsed < self.request_interval:
            time.sleep(self.request_interval - elapsed)
        self._last_request_time = time.time()

    def _record_success(self):
        """记录请求成功，重置连续错误计数。"""
        self._consecutive_errors = 0
        self._available = True

    def _record_error(self):
        """记录请求失败，连续失败超过阈值时标记为不可用。"""
        self._consecutive_errors += 1
        if self._consecutive_errors >= 5:
            self._available = False

    @property
    def is_available(self) -> bool:
        """数据源是否可用（连续失败过多时标记为不可用）。"""
        return self._available

    def reset_availability(self):
        """重置可用性状态（用于手动恢复）。"""
        self._consecutive_errors = 0
        self._available = True

    # ------------------------------------------------------------------
    # 核心接口（子类必须实现）
    # ------------------------------------------------------------------
    @abstractmethod
    def get_fund_info(self, code: str) -> Optional[FundInfo]:
        """获取基金基本信息。

        Args:
            code: 基金代码（6位数字）

        Returns:
            FundInfo 对象，获取失败返回 None
        """
        ...

    @abstractmethod
    def get_latest_nav(self, code: str) -> Optional[NavPoint]:
        """获取基金最新净值。

        Args:
            code: 基金代码

        Returns:
            NavPoint 对象，获取失败返回 None
        """
        ...

    @abstractmethod
    def get_nav_history(self, code: str, page_size: int = 60,
                         start_date: Optional[str] = None,
                         end_date: Optional[str] = None) -> list[NavPoint]:
        """获取基金历史净值序列。

        Args:
            code: 基金代码
            page_size: 返回条数上限
            start_date: 起始日期 YYYY-MM-DD（可选）
            end_date: 结束日期 YYYY-MM-DD（可选）

        Returns:
            NavPoint 列表，按日期升序排列
        """
        ...

    @abstractmethod
    def get_holdings(self, code: str) -> list[HoldingItem]:
        """获取基金前十大持仓。

        Args:
            code: 基金代码

        Returns:
            HoldingItem 列表
        """
        ...

    # ------------------------------------------------------------------
    # 可选接口（子类可覆盖）
    # ------------------------------------------------------------------
    def get_score(self, code: str) -> Optional[dict]:
        """获取数据源提供的基金评分（如果支持）。

        Returns:
            评分字典，如 {"score": 85, "rank": "A"}，不支持返回 None
        """
        return None

    def health_check(self) -> bool:
        """数据源可用性检查（通过请求一个已知基金验证）。

        Returns:
            True 表示可用，False 表示不可用
        """
        try:
            result = self.get_fund_info("000001")
            self._available = result is not None
            return self._available
        except Exception:
            self._available = False
            return False

    def get_stats(self) -> dict:
        """返回数据源统计信息。"""
        return {
            "name": self.name,
            "display_name": self.display_name,
            "available": self._available,
            "consecutive_errors": self._consecutive_errors,
            "last_request_time": self._last_request_time,
        }
