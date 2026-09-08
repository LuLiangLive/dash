"""
modules/monitor/service.py —— 监控业务逻辑

功能：
- 错误日志 CRUD
- 数据更新日志记录与查询
- 系统资源采集（CPU/内存/磁盘/数据库大小）
- 告警管理
- 后台采集线程
"""
from __future__ import annotations

import logging
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── 告警通知接口（预留扩展点）─────────────────────────────

def send_alert(alert_type: str, severity: str, message: str, source: str = "monitor"):
    """发送告警通知。

    当前实现：仅写日志。
    预留扩展点：可在此添加邮件、Webhook、企业微信等通知方式。
    """
    try:
        logger.warning("[ALERT][%s][%s] %s (source=%s)", severity, alert_type, message, source)
        # 预留：邮件通知
        # if settings.alert_email:
        #     _send_email(...)
        # 预留：Webhook 通知
        # if settings.alert_webhook:
        #     _send_webhook(...)
    except Exception as e:
        logger.debug("告警通知发送失败: %s", e)


# ── 告警管理 ──────────────────────────────────────────────

def create_alert(alert_type: str, severity: str, message: str, source: str = "monitor") -> Optional[int]:
    """创建一条告警记录。返回告警 ID。"""
    try:
        import db as _db
        conn = _db.get_conn()
        cur = conn.execute(
            """INSERT INTO monitor_alerts (timestamp, alert_type, severity, message, source, acknowledged)
               VALUES (?, ?, ?, ?, ?, 0)""",
            (time.strftime("%Y-%m-%dT%H:%M:%S+08:00"), alert_type, severity, message[:1000], source),
        )
        conn.commit()
        # 同时触发通知
        send_alert(alert_type, severity, message, source)
        return cur.lastrowid
    except Exception as e:
        logger.debug("告警记录创建失败: %s", e)
        return None


def list_alerts(acknowledged: Optional[int] = None, limit: int = 50) -> list[dict]:
    """获取告警列表。"""
    try:
        import db as _db
        conn = _db.get_conn()
        if acknowledged is not None:
            rows = conn.execute(
                "SELECT * FROM monitor_alerts WHERE acknowledged = ? ORDER BY id DESC LIMIT ?",
                (acknowledged, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM monitor_alerts ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.debug("获取告警列表失败: %s", e)
        return []


def acknowledge_alert(alert_id: int) -> bool:
    """确认告警。"""
    try:
        import db as _db
        conn = _db.get_conn()
        conn.execute("UPDATE monitor_alerts SET acknowledged = 1 WHERE id = ?", (alert_id,))
        conn.commit()
        return True
    except Exception as e:
        logger.debug("确认告警失败: %s", e)
        return False


# ── 错误日志查询 ──────────────────────────────────────────

def list_errors(page: int = 1, limit: int = 20, resolved: Optional[int] = None) -> dict:
    """获取错误日志列表（分页）。"""
    try:
        import db as _db
        conn = _db.get_conn()
        offset = (page - 1) * limit
        if resolved is not None:
            total = conn.execute(
                "SELECT COUNT(*) FROM monitor_errors WHERE resolved = ?", (resolved,)
            ).fetchone()[0]
            rows = conn.execute(
                "SELECT * FROM monitor_errors WHERE resolved = ? ORDER BY id DESC LIMIT ? OFFSET ?",
                (resolved, limit, offset),
            ).fetchall()
        else:
            total = conn.execute("SELECT COUNT(*) FROM monitor_errors").fetchone()[0]
            rows = conn.execute(
                "SELECT * FROM monitor_errors ORDER BY id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return {
            "total": total,
            "page": page,
            "limit": limit,
            "items": [dict(r) for r in rows],
        }
    except Exception as e:
        logger.debug("获取错误日志失败: %s", e)
        return {"total": 0, "page": page, "limit": limit, "items": []}


def get_error(error_id: int) -> Optional[dict]:
    """获取单条错误详情。"""
    try:
        import db as _db
        conn = _db.get_conn()
        row = conn.execute("SELECT * FROM monitor_errors WHERE id = ?", (error_id,)).fetchone()
        return dict(row) if row else None
    except Exception as e:
        logger.debug("获取错误详情失败: %s", e)
        return None


def clear_errors() -> int:
    """清空错误日志。返回删除行数。"""
    try:
        import db as _db
        conn = _db.get_conn()
        cur = conn.execute("DELETE FROM monitor_errors")
        conn.commit()
        return cur.rowcount
    except Exception as e:
        logger.debug("清空错误日志失败: %s", e)
        return 0


# ── 数据更新日志 ──────────────────────────────────────────

def log_data_update(
    task_name: str,
    status: str,
    start_time: str,
    end_time: str,
    record_count: int = 0,
    error_message: str = "",
):
    """记录一次数据更新任务。供现有数据更新任务调用。

    告警规则：
    - 更新时间 > 30分钟触发超时告警
    - status='failed' 触发失败告警
    """
    try:
        import db as _db
        # 计算耗时
        duration = 0.0
        try:
            st = time.mktime(time.strptime(start_time[:19], "%Y-%m-%dT%H:%M:%S"))
            et = time.mktime(time.strptime(end_time[:19], "%Y-%m-%dT%H:%M:%S"))
            duration = round(et - st, 2)
        except Exception:
            pass

        conn = _db.get_conn()
        conn.execute(
            """INSERT INTO data_update_logs
               (task_name, status, start_time, end_time, duration, record_count, error_message)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (task_name, status, start_time, end_time, duration, record_count, error_message[:1000]),
        )
        conn.commit()

        # 超时告警：> 30分钟
        if duration > 1800:
            create_alert(
                alert_type="data_update_timeout",
                severity="warning",
                message=f"数据更新任务 [{task_name}] 耗时 {duration}秒，超过30分钟阈值",
                source="data_update",
            )

        # 失败告警
        if status == "failed":
            create_alert(
                alert_type="data_update_failed",
                severity="critical",
                message=f"数据更新任务 [{task_name}] 失败: {error_message[:200]}",
                source="data_update",
            )
    except Exception as e:
        logger.debug("数据更新日志记录失败: %s", e)


def list_data_updates(page: int = 1, limit: int = 20, task_name: Optional[str] = None) -> dict:
    """获取数据更新历史（分页）。"""
    try:
        import db as _db
        conn = _db.get_conn()
        offset = (page - 1) * limit
        if task_name:
            total = conn.execute(
                "SELECT COUNT(*) FROM data_update_logs WHERE task_name = ?", (task_name,)
            ).fetchone()[0]
            rows = conn.execute(
                "SELECT * FROM data_update_logs WHERE task_name = ? ORDER BY id DESC LIMIT ? OFFSET ?",
                (task_name, limit, offset),
            ).fetchall()
        else:
            total = conn.execute("SELECT COUNT(*) FROM data_update_logs").fetchone()[0]
            rows = conn.execute(
                "SELECT * FROM data_update_logs ORDER BY id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return {
            "total": total,
            "page": page,
            "limit": limit,
            "items": [dict(r) for r in rows],
        }
    except Exception as e:
        logger.debug("获取数据更新历史失败: %s", e)
        return {"total": 0, "page": page, "limit": limit, "items": []}


def get_latest_data_updates() -> list[dict]:
    """获取各任务最新更新状态。"""
    try:
        import db as _db
        conn = _db.get_conn()
        rows = conn.execute(
            """SELECT t.* FROM data_update_logs t
               INNER JOIN (
                   SELECT task_name, MAX(id) as max_id FROM data_update_logs GROUP BY task_name
               ) latest ON t.id = latest.max_id
               ORDER BY t.task_name"""
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.debug("获取最新更新状态失败: %s", e)
        return []


def get_data_update_stats() -> dict:
    """获取更新统计（总次数、成功率、平均耗时、最近更新时间）。"""
    try:
        import db as _db
        conn = _db.get_conn()
        row = conn.execute(
            """SELECT
               COUNT(*) as total,
               SUM(CASE WHEN status IN ('success','ok','done') THEN 1 ELSE 0 END) as success_count,
               SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed_count,
               AVG(duration) as avg_duration,
               MAX(end_time) as last_update
               FROM data_update_logs"""
        ).fetchone()
        total = row["total"] or 0
        success = row["success_count"] or 0
        return {
            "total_runs": total,
            "success_count": success,
            "failed_count": row["failed_count"] or 0,
            "success_rate": round(success / total * 100, 2) if total > 0 else 0.0,
            "avg_duration": round(row["avg_duration"] or 0, 2),
            "last_update_time": row["last_update"],
        }
    except Exception as e:
        logger.debug("获取更新统计失败: %s", e)
        return {
            "total_runs": 0, "success_count": 0, "failed_count": 0,
            "success_rate": 0.0, "avg_duration": 0.0, "last_update_time": None,
        }


# ── 系统资源监控 ──────────────────────────────────────────

# 采集间隔（秒）
METRICS_INTERVAL = 60
# 告警阈值
CPU_ALERT_THRESHOLD = 80.0
MEMORY_ALERT_THRESHOLD = 85.0

# 上一次 CPU 时间（用于计算 CPU 使用率）
_last_cpu_time = None
_last_cpu_wall = None


def _get_cpu_percent_windows() -> float:
    """Windows 下获取 CPU 使用率（使用 ctypes 调用 GetSystemTimes）。"""
    global _last_cpu_time, _last_cpu_wall
    try:
        import ctypes
        from ctypes import wintypes

        class FILETIME(ctypes.Structure):
            _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

        def ft_to_int(ft):
            return (ft.dwHighDateTime << 32) + ft.dwLowDateTime

        idle_time = FILETIME()
        kernel_time = FILETIME()
        user_time = FILETIME()
        ctypes.windll.kernel32.GetSystemTimes(
            ctypes.byref(idle_time), ctypes.byref(kernel_time), ctypes.byref(user_time)
        )

        idle = ft_to_int(idle_time)
        kernel = ft_to_int(kernel_time)
        user = ft_to_int(user_time)
        total = kernel + user  # kernel 包含 idle

        now = time.time()
        if _last_cpu_time is not None and _last_cpu_wall is not None:
            dt = now - _last_cpu_wall
            if dt > 0:
                total_delta = total - _last_cpu_time[0]
                idle_delta = idle - _last_cpu_time[1]
                if total_delta > 0:
                    cpu_percent = (1.0 - idle_delta / total_delta) * 100.0
                    _last_cpu_time = (total, idle)
                    _last_cpu_wall = now
                    return round(min(max(cpu_percent, 0.0), 100.0), 1)

        _last_cpu_time = (total, idle)
        _last_cpu_wall = now
        return 0.0
    except Exception as e:
        logger.debug("CPU 使用率获取失败: %s", e)
        return 0.0


def _get_memory_info_windows() -> tuple[float, float, float]:
    """Windows 下获取内存信息（使用 ctypes 调用 GlobalMemoryStatusEx）。

    返回 (percent, used_mb, total_mb)。
    """
    try:
        import ctypes
        from ctypes import wintypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", wintypes.DWORD),
                ("dwMemoryLoad", wintypes.DWORD),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        mem = MEMORYSTATUSEX()
        mem.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem))

        total_mb = mem.ullTotalPhys / (1024 * 1024)
        avail_mb = mem.ullAvailPhys / (1024 * 1024)
        used_mb = total_mb - avail_mb
        percent = mem.dwMemoryLoad
        return round(percent, 1), round(used_mb, 1), round(total_mb, 1)
    except Exception as e:
        logger.debug("内存信息获取失败: %s", e)
        return 0.0, 0.0, 0.0


def _get_disk_info() -> tuple[float, float, float]:
    """获取磁盘使用信息（使用 shutil.disk_usage，跨平台）。

    返回 (percent, used_gb, total_gb)。
    """
    try:
        # 使用数据库所在磁盘
        db_path = Path(__file__).resolve().parent.parent.parent / "fund.db"
        drive = os.path.splitdrive(str(db_path))[0] or "/"
        usage = shutil.disk_usage(drive if drive else "/")
        total_gb = usage.total / (1024 ** 3)
        used_gb = usage.used / (1024 ** 3)
        percent = (usage.used / usage.total) * 100 if usage.total > 0 else 0
        return round(percent, 1), round(used_gb, 2), round(total_gb, 2)
    except Exception as e:
        logger.debug("磁盘信息获取失败: %s", e)
        return 0.0, 0.0, 0.0


def _get_db_size_mb() -> float:
    """获取数据库文件大小（MB）。"""
    try:
        db_path = Path(__file__).resolve().parent.parent.parent / "fund.db"
        if db_path.exists():
            return round(os.path.getsize(db_path) / (1024 * 1024), 2)
        return 0.0
    except Exception:
        return 0.0


def collect_system_metrics() -> dict:
    """采集一次系统资源使用数据。"""
    try:
        cpu = _get_cpu_percent_windows()
        mem_percent, mem_used, mem_total = _get_memory_info_windows()
        disk_percent, disk_used, disk_total = _get_disk_info()
        db_size = _get_db_size_mb()

        metrics = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
            "cpu_percent": cpu,
            "memory_percent": mem_percent,
            "memory_used_mb": mem_used,
            "memory_total_mb": mem_total,
            "disk_percent": disk_percent,
            "disk_used_gb": disk_used,
            "disk_total_gb": disk_total,
            "db_size_mb": db_size,
        }

        # 写入数据库
        try:
            import db as _db
            conn = _db.get_conn()
            conn.execute(
                """INSERT INTO system_metrics
                   (timestamp, cpu_percent, memory_percent, memory_used_mb, memory_total_mb,
                    disk_percent, disk_used_gb, disk_total_gb, db_size_mb)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    metrics["timestamp"], metrics["cpu_percent"], metrics["memory_percent"],
                    metrics["memory_used_mb"], metrics["memory_total_mb"],
                    metrics["disk_percent"], metrics["disk_used_gb"],
                    metrics["disk_total_gb"], metrics["db_size_mb"],
                ),
            )
            conn.commit()
        except Exception as e:
            logger.debug("系统指标写入失败: %s", e)

        # 告警检查
        if cpu > CPU_ALERT_THRESHOLD:
            create_alert(
                alert_type="cpu_high",
                severity="warning",
                message=f"CPU 使用率 {cpu}% 超过阈值 {CPU_ALERT_THRESHOLD}%",
                source="system_metrics",
            )
        if mem_percent > MEMORY_ALERT_THRESHOLD:
            create_alert(
                alert_type="memory_high",
                severity="critical",
                message=f"内存使用率 {mem_percent}% 超过阈值 {MEMORY_ALERT_THRESHOLD}%",
                source="system_metrics",
            )

        return metrics
    except Exception as e:
        logger.debug("系统指标采集失败: %s", e)
        return {}


def get_latest_system_metrics() -> Optional[dict]:
    """获取最新系统资源使用情况。"""
    try:
        import db as _db
        conn = _db.get_conn()
        row = conn.execute(
            "SELECT * FROM system_metrics ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None
    except Exception as e:
        logger.debug("获取最新系统指标失败: %s", e)
        return None


def get_system_metrics_history(hours: int = 24) -> list[dict]:
    """获取历史资源使用数据（默认最近24小时）。"""
    try:
        import db as _db
        conn = _db.get_conn()
        # 计算时间阈值
        cutoff = time.strftime("%Y-%m-%dT%H:%M:%S+08:00",
                                time.localtime(time.time() - hours * 3600))
        rows = conn.execute(
            "SELECT * FROM system_metrics WHERE timestamp >= ? ORDER BY id ASC",
            (cutoff,),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.debug("获取系统指标历史失败: %s", e)
        return []


# ── 后台采集线程 ──────────────────────────────────────────

_metrics_thread: Optional[threading.Thread] = None
_metrics_stop_event = threading.Event()


def _metrics_worker():
    """后台采集线程主循环。"""
    logger.info("系统资源采集后台线程启动（间隔 %d 秒）", METRICS_INTERVAL)
    # 启动时先采集一次
    collect_system_metrics()
    while not _metrics_stop_event.is_set():
        try:
            _metrics_stop_event.wait(METRICS_INTERVAL)
            if not _metrics_stop_event.is_set():
                collect_system_metrics()
        except Exception as e:
            logger.debug("系统指标采集循环异常: %s", e)
    logger.info("系统资源采集后台线程停止")


def start_metrics_collection():
    """启动系统资源采集后台线程（幂等）。"""
    global _metrics_thread
    try:
        if _metrics_thread is not None and _metrics_thread.is_alive():
            return
        _metrics_stop_event.clear()
        _metrics_thread = threading.Thread(target=_metrics_worker, daemon=True, name="monitor-metrics")
        _metrics_thread.start()
    except Exception as e:
        logger.error("启动系统指标采集线程失败: %s", e)


def stop_metrics_collection():
    """停止系统资源采集后台线程。"""
    try:
        _metrics_stop_event.set()
    except Exception:
        pass
