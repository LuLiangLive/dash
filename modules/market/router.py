"""
api/sectors.py —— 板块/行业轮动分析API

从 api/advanced.py 拆分（v2.5.5架构重构）
负责：板块数据列表、板块成分股、板块历史数据
"""
from typing import Optional
from fastapi import APIRouter
import db

router = APIRouter(prefix='/api/advanced', tags=['板块轮动分析'])


def get_db():
    """获取数据库连接"""
    return db.get_conn()


# ============================================================
# 板块轮动分析API
# ============================================================

@router.get('/sectors', summary='获取板块数据列表', responses={200: {'description': '板块涨跌幅/资金流数据列表'}})
async def list_sectors(date: Optional[str] = None):
    """获取板块数据列表"""
    db_conn = get_db()
    if not date:
        date = db_conn.execute('SELECT MAX(date) FROM sector_data').fetchone()[0]
    if not date:
        return {'ok': True, 'sectors': [], 'date': None}
    rows = db_conn.execute('''
        SELECT * FROM sector_data WHERE date = ? ORDER BY rank
    ''', (date,)).fetchall()
    return {'ok': True, 'sectors': [dict(r) for r in rows], 'date': date}


@router.get('/sectors/{sector_name}/stocks', summary='获取板块成分股', responses={200: {'description': '板块成分股列表（按权重排序）'}})
async def list_sector_stocks(sector_name: str):
    """获取板块成分股"""
    db_conn = get_db()
    rows = db_conn.execute('''
        SELECT * FROM sector_stocks WHERE sector_name = ? ORDER BY weight DESC
    ''', (sector_name,)).fetchall()
    return {'ok': True, 'stocks': [dict(r) for r in rows]}


@router.get('/sectors/{sector_name}/history', summary='获取板块历史数据', responses={200: {'description': '板块历史走势数据（涨跌幅/资金流）'}})
async def sector_history(sector_name: str, days: int = 30):
    """获取板块历史数据（用于走势图）"""
    db_conn = get_db()
    rows = db_conn.execute('''
        SELECT date, change_pct, fund_flow, avg_change
        FROM sector_data
        WHERE sector_name = ?
        ORDER BY date DESC LIMIT ?
    ''', (sector_name, days)).fetchall()
    return {'ok': True, 'history': [dict(r) for r in reversed(rows)]}
