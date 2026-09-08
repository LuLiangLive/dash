"""
repository/base.py —— Repository 基类

提供统一的数据库访问接口，封装 SQLite 连接管理和常用操作。
所有具体的 Repository 类都继承自 BaseRepository。

设计原则：
- Repository 只负责数据访问，不包含业务逻辑
- 业务层调用 Repository 方法，不直接写 SQL
- 为未来换数据库（PostgreSQL）打基础，只需改 Repository 实现
"""
from __future__ import annotations

from typing import Any, Optional

import db


class BaseRepository:
    """Repository 基类，封装数据库连接和常用操作。"""

    table_name: str = ""
    primary_key: str = "id"

    def __init__(self):
        if not self.table_name:
            raise ValueError("子类必须定义 table_name")

    def _conn(self):
        """获取当前线程的数据库连接。"""
        return db.get_conn()

    def get_by_id(self, id_value: Any) -> Optional[dict]:
        """按主键查询单条记录。"""
        conn = self._conn()
        row = conn.execute(
            f"SELECT * FROM {self.table_name} WHERE {self.primary_key} = ?",
            (id_value,)
        ).fetchone()
        return dict(row) if row else None

    def list_all(self, limit: int = 100, offset: int = 0) -> list[dict]:
        """查询所有记录（分页）。"""
        conn = self._conn()
        rows = conn.execute(
            f"SELECT * FROM {self.table_name} LIMIT ? OFFSET ?",
            (limit, offset)
        ).fetchall()
        return [dict(r) for r in rows]

    def count(self) -> int:
        """统计记录总数。"""
        conn = self._conn()
        row = conn.execute(f"SELECT COUNT(*) as cnt FROM {self.table_name}").fetchone()
        return row["cnt"] if row else 0

    def delete_by_id(self, id_value: Any) -> bool:
        """按主键删除记录。"""
        conn = self._conn()
        cur = conn.execute(
            f"DELETE FROM {self.table_name} WHERE {self.primary_key} = ?",
            (id_value,)
        )
        conn.commit()
        return cur.rowcount > 0

    def execute(self, sql: str, params: tuple = ()):
        """执行自定义 SQL（返回 cursor）。"""
        conn = self._conn()
        return conn.execute(sql, params)

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        """执行查询 SQL，返回字典列表。"""
        conn = self._conn()
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def query_one(self, sql: str, params: tuple = ()) -> Optional[dict]:
        """执行查询 SQL，返回单条记录。"""
        conn = self._conn()
        row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None
