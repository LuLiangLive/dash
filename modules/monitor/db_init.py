"""
modules/monitor/db_init.py —— 监控相关数据库表初始化

创建 4 张独立表，不影响现有业务表：
- monitor_errors: 错误日志
- data_update_logs: 数据更新历史
- system_metrics: 系统资源使用历史
- monitor_alerts: 告警记录
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_MONITOR_SCHEMA = """
CREATE TABLE IF NOT EXISTS monitor_errors (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT,
    level       TEXT DEFAULT 'ERROR',
    message     TEXT,
    traceback   TEXT,
    method      TEXT,
    path        TEXT,
    params      TEXT,
    client_ip   TEXT,
    user_agent  TEXT,
    resolved    INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_monitor_errors_ts ON monitor_errors(timestamp);
CREATE INDEX IF NOT EXISTS idx_monitor_errors_path ON monitor_errors(path);
CREATE INDEX IF NOT EXISTS idx_monitor_errors_resolved ON monitor_errors(resolved);

CREATE TABLE IF NOT EXISTS data_update_logs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task_name       TEXT,
    status          TEXT,
    start_time      TEXT,
    end_time        TEXT,
    duration        REAL,
    record_count    INTEGER,
    error_message   TEXT
);
CREATE INDEX IF NOT EXISTS idx_data_update_task ON data_update_logs(task_name);
CREATE INDEX IF NOT EXISTS idx_data_update_status ON data_update_logs(status);
CREATE INDEX IF NOT EXISTS idx_data_update_start ON data_update_logs(start_time);

CREATE TABLE IF NOT EXISTS system_metrics (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT,
    cpu_percent     REAL,
    memory_percent  REAL,
    memory_used_mb  REAL,
    memory_total_mb REAL,
    disk_percent    REAL,
    disk_used_gb    REAL,
    disk_total_gb   REAL,
    db_size_mb      REAL
);
CREATE INDEX IF NOT EXISTS idx_system_metrics_ts ON system_metrics(timestamp);

CREATE TABLE IF NOT EXISTS monitor_alerts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT,
    alert_type  TEXT,
    severity    TEXT,
    message     TEXT,
    source      TEXT,
    acknowledged INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_monitor_alerts_ts ON monitor_alerts(timestamp);
CREATE INDEX IF NOT EXISTS idx_monitor_alerts_ack ON monitor_alerts(acknowledged);
CREATE INDEX IF NOT EXISTS idx_monitor_alerts_type ON monitor_alerts(alert_type);
"""


def init_monitor_tables() -> bool:
    """创建所有监控相关表（幂等）。返回是否成功。"""
    try:
        import db as _db
        conn = _db.get_conn()
        conn.executescript(_MONITOR_SCHEMA)
        conn.commit()
        logger.info("监控相关表初始化完成")
        return True
    except Exception as e:
        logger.error("监控表初始化失败: %s", e)
        return False


# 模块加载时自动初始化（try-except 包裹，失败不影响主应用）
try:
    init_monitor_tables()
except Exception:
    pass
