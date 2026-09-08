"""
repository/rank_repo.py —— 榜单快照表 Repository

封装 rank_snapshots 表的数据访问操作。
现有 db.py 中的函数继续工作，未来可逐步迁移到此处。
"""
from __future__ import annotations

from typing import Optional

from repository.base import BaseRepository


class RankRepository(BaseRepository):
    """rank_snapshots 表 Repository。"""

    table_name = "rank_snapshots"
    primary_key = "id"

    def get_by_date(self, date: str) -> list[dict]:
        """按日期查询所有榜单快照。"""
        return self.query(
            "SELECT * FROM rank_snapshots WHERE date = ? ORDER BY panel, sub, rank",
            (date,)
        )

    def get_latest_date(self) -> Optional[str]:
        """查询最新的榜单日期。"""
        row = self.query_one("SELECT MAX(date) as max_date FROM rank_snapshots")
        return row["max_date"] if row else None

    def list_dates(self, limit: int = 30) -> list[str]:
        """查询历史榜单日期列表（降序）。"""
        rows = self.query(
            "SELECT DISTINCT date FROM rank_snapshots ORDER BY date DESC LIMIT ?",
            (limit,)
        )
        return [r["date"] for r in rows]

    def get_by_date_and_panel(self, date: str, panel: str) -> list[dict]:
        """按日期和面板查询榜单。"""
        return self.query(
            "SELECT * FROM rank_snapshots WHERE date = ? AND panel = ? ORDER BY sub, rank",
            (date, panel)
        )

    def get_fund_rank_days(self, code: str, days: int = 30) -> int:
        """查询某基金近 N 天上榜天数。"""
        row = self.query_one(
            "SELECT COUNT(DISTINCT date) as cnt FROM rank_snapshots "
            "WHERE code = ? AND date >= date('now', ?)",
            (code, f"-{days} days")
        )
        return row["cnt"] if row else 0

    def get_fund_latest_rank(self, code: str) -> Optional[dict]:
        """查询某基金最新一天的上榜信息。"""
        return self.query_one(
            "SELECT * FROM rank_snapshots WHERE code = ? ORDER BY date DESC LIMIT 1",
            (code,)
        )

    def count_by_date(self, date: str) -> int:
        """统计某日期的榜单记录数。"""
        row = self.query_one(
            "SELECT COUNT(*) as cnt FROM rank_snapshots WHERE date = ?",
            (date,)
        )
        return row["cnt"] if row else 0

    def delete_by_date(self, date: str) -> int:
        """删除某日期的所有榜单快照。"""
        conn = self._conn()
        cur = conn.execute("DELETE FROM rank_snapshots WHERE date = ?", (date,))
        conn.commit()
        return cur.rowcount

    def save_rank(self, date: str, panel: str, sub: str, rank: int,
                  code: str, name: str, meta: dict, created_at: str) -> bool:
        """保存单条榜单记录。"""
        import db
        return db.save_rank(date, panel, sub, rank, code, name, meta, created_at)
