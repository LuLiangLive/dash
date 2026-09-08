"""
modules/monitor/models.py —— 监控模块 Pydantic 数据模型
"""
from __future__ import annotations

from typing import Optional
from pydantic import BaseModel


# ── 性能监控 ──────────────────────────────────────────────

class EndpointPerformance(BaseModel):
    endpoint: str
    method: str
    request_count: int
    error_count: int
    error_rate: float
    avg_response_time: float
    p50: float
    p95: float
    p99: float


class PerformanceOverview(BaseModel):
    total_requests: int
    total_errors: int
    overall_error_rate: float
    overall_avg_response_time: float
    endpoints: list[EndpointPerformance]


class SlowQueryItem(BaseModel):
    timestamp: str
    method: str
    path: str
    duration_ms: float
    status_code: int


class ErrorRateItem(BaseModel):
    time_bucket: str
    endpoint: str
    request_count: int
    error_count: int
    error_rate: float


# ── 错误日志 ──────────────────────────────────────────────

class ErrorLogItem(BaseModel):
    id: int
    timestamp: str
    level: str
    message: str
    traceback: Optional[str] = None
    method: Optional[str] = None
    path: Optional[str] = None
    params: Optional[str] = None
    client_ip: Optional[str] = None
    user_agent: Optional[str] = None
    resolved: int


class ErrorLogListResponse(BaseModel):
    total: int
    page: int
    limit: int
    items: list[ErrorLogItem]


# ── 数据更新 ──────────────────────────────────────────────

class DataUpdateLogItem(BaseModel):
    id: int
    task_name: str
    status: str
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    duration: Optional[float] = None
    record_count: Optional[int] = None
    error_message: Optional[str] = None


class DataUpdateStats(BaseModel):
    total_runs: int
    success_count: int
    failed_count: int
    success_rate: float
    avg_duration: float
    last_update_time: Optional[str] = None


# ── 系统资源 ──────────────────────────────────────────────

class SystemMetricsItem(BaseModel):
    timestamp: str
    cpu_percent: float
    memory_percent: float
    memory_used_mb: float
    memory_total_mb: float
    disk_percent: float
    disk_used_gb: float
    disk_total_gb: float
    db_size_mb: float


# ── 告警 ──────────────────────────────────────────────────

class AlertItem(BaseModel):
    id: int
    timestamp: str
    alert_type: str
    severity: str
    message: str
    source: str
    acknowledged: int
