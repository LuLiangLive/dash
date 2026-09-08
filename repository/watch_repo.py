"""
repository/watch_repo.py —— 自选基金表 Repository

封装 watchlist 表的数据访问操作。
"""
from __future__ import annotations

from typing import Optional

from repository.base import BaseRepository


class WatchRepository(BaseRepository):
    """watchlist 表 Repository。"""

    table_name = "watchlist"
    primary_key = "code"

    def list_all(self) -> list[dict]:
        """查询所有自选基金（按 position 排序）。"""
        return self.query("SELECT * FROM watchlist ORDER BY position")

    def get_by_code(self, code: str) -> Optional[dict]:
        """按基金代码查询自选。"""
        return self.get_by_id(code)

    def add(self, code: str, name: Optional[str] = None) -> bool:
        """添加自选基金。"""
        import db
        return db.add_watchlist(code, name)

    def remove(self, code: str) -> bool:
        """删除自选基金。"""
        return self.delete_by_id(code)

    def replace_all(self, codes: list[str]) -> int:
        """整体覆盖自选列表。"""
        import db
        return db.replace_watchlist(codes)

    def count(self) -> int:
        """统计自选基金数量。"""
        return super().count()

    def list_detail(self) -> dict[str, dict]:
        """查询自选基金详情（带基金信息），返回 {code: fund_dict}。"""
        import db
        return db.list_watchlist_detail()
