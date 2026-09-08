"""
repository/nav_repo.py —— 净值历史表 Repository

封装 nav_history 表的数据访问操作。
现有 db.py 中的函数继续工作，未来可逐步迁移到此处。
"""
from __future__ import annotations

from typing import Optional

from repository.base import BaseRepository


class NavRepository(BaseRepository):
    """nav_history 表 Repository。"""

    table_name = "nav_history"
    primary_key = "code"  # 复合主键 (code, date)，这里用 code 作为主要查询键

    def get_by_code(self, code: str, limit: int = 120) -> list[dict]:
        """按基金代码查询净值历史（按日期降序）。"""
        return self.query(
            "SELECT * FROM nav_history WHERE code = ? ORDER BY date DESC LIMIT ?",
            (code, limit)
        )

    def get_by_code_and_date(self, code: str, date: str) -> Optional[dict]:
        """按基金代码和日期查询单条净值。"""
        return self.query_one(
            "SELECT * FROM nav_history WHERE code = ? AND date = ?",
            (code, date)
        )

    def get_latest_date(self, code: str) -> Optional[str]:
        """查询某基金的最新净值日期。"""
        row = self.query_one(
            "SELECT MAX(date) as max_date FROM nav_history WHERE code = ?",
            (code,)
        )
        return row["max_date"] if row else None

    def count_by_code(self, code: str) -> int:
        """统计某基金的净值记录数。"""
        row = self.query_one(
            "SELECT COUNT(*) as cnt FROM nav_history WHERE code = ?",
            (code,)
        )
        return row["cnt"] if row else 0

    def bulk_upsert(self, code: str, navs: list[dict]) -> int:
        """批量插入或更新净值（调用 db.bulk_upsert_navs）。"""
        import db
        return db.bulk_upsert_navs(code, navs)

    def delete_by_code(self, code: str) -> int:
        """删除某基金的所有净值记录。"""
        conn = self._conn()
        cur = conn.execute("DELETE FROM nav_history WHERE code = ?", (code,))
        conn.commit()
        return cur.rowcount
