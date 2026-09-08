"""
modules/datasource/manager.py —— 数据源管理器

负责数据源的注册、优先级管理、切换和 fallback 机制。

核心功能：
1. 注册多个第三方数据源
2. 按优先级排序，获取当前活跃数据源
3. 数据源不可用时自动 fallback 到下一个
4. 支持手动切换数据源
5. 持久化数据源配置到数据库 settings 表
"""
from __future__ import annotations

import json
import threading
import time
from typing import Optional

from .base import BaseDataSource, FundInfo, NavPoint, HoldingItem


class DataSourceManager:
    """
    数据源管理器（单例模式）。

    用法：
        mgr = get_manager()
        mgr.register(EastMoneySource())
        mgr.register(DanjuanSource())
        info = mgr.get_fund_info("000001")  # 自动按优先级+fallback
    """

    SETTINGS_KEY = "datasource_config"

    def __init__(self):
        self._sources: dict[str, BaseDataSource] = {}
        self._priority: list[str] = []  # 按优先级排序的数据源名称列表
        self._active_source: Optional[str] = None  # 手动指定的活跃数据源
        self._lock = threading.Lock()
        self._fallback_count: dict[str, int] = {}  # fallback 统计
        self._last_check_time = 0.0

    # ------------------------------------------------------------------
    # 注册与配置
    # ------------------------------------------------------------------
    def register(self, source: BaseDataSource, priority: int = 0) -> None:
        """注册数据源。

        Args:
            source: 数据源实例
            priority: 优先级（数字越小优先级越高，0 为最高）
        """
        with self._lock:
            name = source.name
            self._sources[name] = source
            # 按优先级插入
            if name not in self._priority:
                self._priority.append(name)
            self._fallback_count.setdefault(name, 0)
        # 加载持久化配置
        self._load_config()

    def unregister(self, name: str) -> bool:
        """注销数据源，返回是否成功。"""
        with self._lock:
            if name in self._sources:
                del self._sources[name]
                if name in self._priority:
                    self._priority.remove(name)
                if self._active_source == name:
                    self._active_source = None
                return True
            return False

    def get_sources(self) -> list[dict]:
        """获取所有已注册数据源的信息列表（按优先级排序）。"""
        with self._lock:
            result = []
            for i, name in enumerate(self._priority):
                src = self._sources.get(name)
                if src:
                    stats = src.get_stats()
                    stats["priority"] = i
                    stats["is_active"] = (self._active_source == name) or \
                        (self._active_source is None and i == 0)
                    stats["fallback_count"] = self._fallback_count.get(name, 0)
                    result.append(stats)
            return result

    def get_source(self, name: str) -> Optional[BaseDataSource]:
        """按名称获取数据源实例。"""
        return self._sources.get(name)

    # ------------------------------------------------------------------
    # 优先级与切换
    # ------------------------------------------------------------------
    def set_priority(self, priorities: list[str]) -> bool:
        """设置数据源优先级顺序。

        Args:
            priorities: 按优先级从高到低排列的数据源名称列表

        Returns:
            是否设置成功
        """
        with self._lock:
            # 验证所有名称都已注册
            for name in priorities:
                if name not in self._sources:
                    return False
            self._priority = priorities
        self._save_config()
        return True

    def set_active_source(self, name: Optional[str]) -> bool:
        """手动设置活跃数据源（None 表示自动模式，按优先级+fallback）。

        Returns:
            是否设置成功
        """
        with self._lock:
            if name is not None and name not in self._sources:
                return False
            self._active_source = name
        self._save_config()
        return True

    def get_active_source_name(self) -> str:
        """获取当前活跃数据源名称。"""
        if self._active_source:
            return self._active_source
        # 自动模式：返回优先级最高且可用的
        for name in self._priority:
            src = self._sources.get(name)
            if src and src.is_available:
                return name
        return self._priority[0] if self._priority else ""

    def _get_effective_source(self) -> Optional[BaseDataSource]:
        """获取当前有效的数据源（考虑手动指定和可用性）。"""
        if self._active_source:
            src = self._sources.get(self._active_source)
            if src and src.is_available:
                return src
            # 手动指定的数据源不可用，fallback
        # 按优先级找第一个可用的
        for name in self._priority:
            src = self._sources.get(name)
            if src and src.is_available:
                return src
        # 全部不可用时，返回优先级最高的（最后尝试）
        if self._priority:
            return self._sources.get(self._priority[0])
        return None

    # ------------------------------------------------------------------
    # 数据获取（带 fallback）
    # ------------------------------------------------------------------
    def get_fund_info(self, code: str) -> Optional[FundInfo]:
        """获取基金基本信息（自动 fallback）。"""
        return self._execute_with_fallback("get_fund_info", code)

    def get_latest_nav(self, code: str) -> Optional[NavPoint]:
        """获取最新净值（自动 fallback）。"""
        return self._execute_with_fallback("get_latest_nav", code)

    def get_nav_history(self, code: str, page_size: int = 60,
                         start_date: Optional[str] = None,
                         end_date: Optional[str] = None) -> list[NavPoint]:
        """获取历史净值（自动 fallback）。"""
        result = self._execute_with_fallback(
            "get_nav_history", code, page_size, start_date, end_date
        )
        return result if result is not None else []

    def get_holdings(self, code: str) -> list[HoldingItem]:
        """获取持仓（自动 fallback）。"""
        result = self._execute_with_fallback("get_holdings", code)
        return result if result is not None else []

    def get_score(self, code: str) -> Optional[dict]:
        """获取评分（自动 fallback）。"""
        return self._execute_with_fallback("get_score", code)

    def _execute_with_fallback(self, method: str, *args) -> Optional[object]:
        """执行数据源方法，失败时自动 fallback 到下一个数据源。"""
        # 构建尝试顺序：活跃数据源优先，然后按优先级
        attempt_order = []
        if self._active_source and self._active_source in self._sources:
            attempt_order.append(self._active_source)
        for name in self._priority:
            if name not in attempt_order:
                attempt_order.append(name)

        last_error = None
        for name in attempt_order:
            src = self._sources.get(name)
            if not src:
                continue
            try:
                func = getattr(src, method)
                result = func(*args)
                if result is not None and result != []:
                    src._record_success()
                    return result
                # 空结果也算失败，继续 fallback
            except Exception as e:
                last_error = e
                src._record_error()
                self._fallback_count[name] = self._fallback_count.get(name, 0) + 1
                continue

        return None

    # ------------------------------------------------------------------
    # 监控与健康检查
    # ------------------------------------------------------------------
    def health_check_all(self) -> dict:
        """对所有数据源执行健康检查。"""
        results = {}
        for name in self._priority:
            src = self._sources.get(name)
            if src:
                try:
                    ok = src.health_check()
                    results[name] = {"available": ok, "error": None}
                except Exception as e:
                    results[name] = {"available": False, "error": str(e)[:100]}
        self._last_check_time = time.time()
        return results

    def get_monitor_stats(self) -> dict:
        """获取数据源监控统计信息。"""
        return {
            "sources": self.get_sources(),
            "active_source": self.get_active_source_name(),
            "auto_mode": self._active_source is None,
            "last_health_check": self._last_check_time,
            "total_fallbacks": sum(self._fallback_count.values()),
            "fallback_by_source": dict(self._fallback_count),
        }

    def reset_all_availability(self) -> None:
        """重置所有数据源的可用性状态。"""
        for src in self._sources.values():
            src.reset_availability()

    # ------------------------------------------------------------------
    # 配置持久化
    # ------------------------------------------------------------------
    def _save_config(self) -> None:
        """保存数据源配置到数据库 settings 表。"""
        try:
            import db
            config = {
                "priority": list(self._priority),
                "active_source": self._active_source,
            }
            conn = db.get_conn()
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
                (self.SETTINGS_KEY, json.dumps(config, ensure_ascii=False),
                 time.strftime("%Y-%m-%dT%H:%M:%S+08:00"))
            )
            conn.commit()
        except Exception:
            pass  # 配置保存失败不影响运行

    def _load_config(self) -> None:
        """从数据库加载数据源配置。"""
        try:
            import db
            conn = db.get_conn()
            row = conn.execute(
                "SELECT value FROM settings WHERE key = ?",
                (self.SETTINGS_KEY,)
            ).fetchone()
            if row:
                config = json.loads(row["value"])
                saved_priority = config.get("priority", [])
                # 只保留已注册的数据源
                valid_priority = [n for n in saved_priority if n in self._sources]
                if valid_priority:
                    # 补充未在配置中的新注册数据源
                    for n in self._sources:
                        if n not in valid_priority:
                            valid_priority.append(n)
                    self._priority = valid_priority
                active = config.get("active_source")
                if active and active in self._sources:
                    self._active_source = active
        except Exception:
            pass  # 配置加载失败使用默认


# 全局单例
_manager_instance: Optional[DataSourceManager] = None
_manager_lock = threading.Lock()


def get_manager() -> DataSourceManager:
    """获取全局数据源管理器单例。"""
    global _manager_instance
    if _manager_instance is None:
        with _manager_lock:
            if _manager_instance is None:
                _manager_instance = DataSourceManager()
    return _manager_instance
