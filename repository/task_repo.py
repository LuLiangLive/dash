"""
repository/task_repo.py —— 任务日志表 Repository

封装 task_logs 表的数据访问操作。
"""
from __future__ import annotations

from typing import Optional

from repository.base import BaseRepository


class TaskRepository(BaseRepository):
    """task_logs 表 Repository。"""

    table_name = "task_logs"
    primary_key = "id"

    def list_recent(self, limit: int = 10) -> list[dict]:
        """查询最近的任务日志（按 ID 降序）。"""
        return self.query(
            "SELECT * FROM task_logs ORDER BY id DESC LIMIT ?",
            (limit,)
        )

    def get_latest(self) -> Optional[dict]:
        """查询最新的任务日志。"""
        return self.query_one("SELECT * FROM task_logs ORDER BY id DESC LIMIT 1")

    def get_latest_finished(self) -> Optional[dict]:
        """查询最新已完成的任务日志。"""
        return self.query_one(
            "SELECT * FROM task_logs WHERE status IN ('ok', 'error', 'stopped') "
            "ORDER BY id DESC LIMIT 1"
        )

    def get_running(self) -> Optional[dict]:
        """查询正在运行的任务。"""
        return self.query_one(
            "SELECT * FROM task_logs WHERE status = 'running' ORDER BY id DESC LIMIT 1"
        )

    def create(self, task: str, status: str = "running", message: str = "") -> int:
        """创建任务日志。"""
        import db
        return db.log_task(task, status, message)

    def update_status(self, task_id: int, status: str, message: str = "") -> bool:
        """更新任务状态。"""
        import db
        return db.update_task(task_id, status, message)

    def count_by_status(self, status: str) -> int:
        """按状态统计任务数。"""
        row = self.query_one(
            "SELECT COUNT(*) as cnt FROM task_logs WHERE status = ?",
            (status,)
        )
        return row["cnt"] if row else 0
