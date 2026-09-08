"""
repository/fund_repo.py —— 基金表 Repository

封装 funds 表的数据访问操作。
现有 db.py 中的函数继续工作，未来可逐步迁移到此处。
"""
from __future__ import annotations

from typing import Optional

from repository.base import BaseRepository


class FundRepository(BaseRepository):
    """funds 表 Repository。"""

    table_name = "funds"
    primary_key = "code"

    def get_by_code(self, code: str) -> Optional[dict]:
        """按基金代码查询。"""
        return self.get_by_id(code)

    def list_by_sec(self, sec: Optional[str] = None, limit: int = 100) -> list[dict]:
        """按板块查询基金列表（按综合分降序）。"""
        if sec:
            return self.query(
                "SELECT * FROM funds WHERE sec = ? ORDER BY score DESC LIMIT ?",
                (sec, limit)
            )
        return self.query(
            "SELECT * FROM funds ORDER BY score DESC LIMIT ?",
            (limit,)
        )

    def list_top_scores(self, limit: int = 100) -> list[dict]:
        """查询综合分最高的基金。"""
        return self.query(
            "SELECT code, name, score, ad_score, earn_score, sec, nav, nav_date "
            "FROM funds WHERE score IS NOT NULL ORDER BY score DESC LIMIT ?",
            (limit,)
        )

    def upsert(self, data: dict) -> bool:
        """插入或更新基金信息（调用 db.upsert_fund）。"""
        import db
        return db.upsert_fund(data)

    def count_by_sec(self) -> list[dict]:
        """按板块统计基金数量。"""
        return self.query(
            "SELECT sec, COUNT(*) as cnt FROM funds GROUP BY sec ORDER BY cnt DESC"
        )

    def bulk_upsert(self, funds: list[dict]) -> int:
        """批量插入或更新基金信息。返回成功条数。"""
        import db
        return db.bulk_upsert_funds(funds)
