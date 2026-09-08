"""
modules/datasource/monitor.py —— 数据源可用性监控

定期检查各数据源的可用性，数据源不可用时自动切换到备用数据源。

核心功能：
1. 定期健康检查（可配置间隔）
2. 数据源不可用时自动 fallback
3. 记录监控历史
4. 提供监控状态查询
"""
from __future__ import annotations

import threading
import time
from typing import Optional

from .manager import get_manager


class DataSourceMonitor:
    """
    数据源可用性监控器（后台线程模式）。

    用法：
        monitor = DataSourceMonitor()
        monitor.start(check_interval=300)  # 每5分钟检查一次
        status = monitor.get_status()
        monitor.stop()
    """

    def __init__(self):
        self._manager = get_manager()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._check_interval = 300  # 默认5分钟
        self._running = False
        self._last_check_time = 0.0
        self._check_history: list[dict] = []
        self._max_history = 100
        self._auto_switch_enabled = True
        self._switch_count = 0

    # ------------------------------------------------------------------
    # 启动/停止
    # ------------------------------------------------------------------
    def start(self, check_interval: int = 300) -> bool:
        """启动后台监控线程。

        Args:
            check_interval: 检查间隔（秒）

        Returns:
            是否启动成功
        """
        if self._running:
            return False
        self._check_interval = check_interval
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()
        self._running = True
        return True

    def stop(self) -> None:
        """停止监控线程。"""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._running = False

    @property
    def is_running(self) -> bool:
        """监控是否在运行。"""
        return self._running

    # ------------------------------------------------------------------
    # 监控循环
    # ------------------------------------------------------------------
    def _monitor_loop(self):
        """后台监控循环。"""
        while not self._stop_event.is_set():
            try:
                self._perform_check()
            except Exception:
                pass
            # 分段等待，便于快速响应停止信号
            self._stop_event.wait(self._check_interval)

    def _perform_check(self) -> dict:
        """执行一次健康检查，返回检查结果。"""
        results = self._manager.health_check_all()
        self._last_check_time = time.time()

        # 记录历史
        record = {
            "timestamp": self._last_check_time,
            "time_str": time.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
            "results": results,
        }
        self._check_history.append(record)
        if len(self._check_history) > self._max_history:
            self._check_history.pop(0)

        # 自动切换：如果当前活跃数据源不可用，自动切换
        if self._auto_switch_enabled:
            active_name = self._manager.get_active_source_name()
            active_result = results.get(active_name, {})
            if not active_result.get("available", True):
                # 活跃数据源不可用，尝试切换
                for name, result in results.items():
                    if result.get("available") and name != active_name:
                        self._manager.set_active_source(None)  # 切回自动模式
                        self._switch_count += 1
                        break

        return results

    # ------------------------------------------------------------------
    # 手动检查
    # ------------------------------------------------------------------
    def check_now(self) -> dict:
        """立即执行一次健康检查（不等待定时）。"""
        return self._perform_check()

    # ------------------------------------------------------------------
    # 状态查询
    # ------------------------------------------------------------------
    def get_status(self) -> dict:
        """获取监控状态。"""
        return {
            "running": self._running,
            "check_interval": self._check_interval,
            "last_check_time": self._last_check_time,
            "last_check_str": (
                time.strftime("%Y-%m-%dT%H:%M:%S+08:00",
                               time.localtime(self._last_check_time))
                if self._last_check_time else ""
            ),
            "auto_switch_enabled": self._auto_switch_enabled,
            "switch_count": self._switch_count,
            "history_count": len(self._check_history),
            "sources": self._manager.get_sources(),
            "active_source": self._manager.get_active_source_name(),
        }

    def get_history(self, limit: int = 20) -> list[dict]:
        """获取最近的检查历史。"""
        return self._check_history[-limit:]

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------
    def set_auto_switch(self, enabled: bool) -> None:
        """设置是否启用自动切换。"""
        self._auto_switch_enabled = enabled

    def set_check_interval(self, interval: int) -> None:
        """设置检查间隔（秒）。"""
        if interval < 30:
            interval = 30  # 最小30秒
        self._check_interval = interval


# 全局单例
_monitor_instance: Optional[DataSourceMonitor] = None
_monitor_lock = threading.Lock()


def get_monitor() -> DataSourceMonitor:
    """获取全局监控器单例。"""
    global _monitor_instance
    if _monitor_instance is None:
        with _monitor_lock:
            if _monitor_instance is None:
                _monitor_instance = DataSourceMonitor()
    return _monitor_instance
