"""
api/groups.py —— 自选基金分组管理API

从 api/advanced.py 拆分（v2.5.5架构重构）
负责：分组CRUD、分组成员管理
"""
from typing import Optional
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
import db
from modules.fund.fund_service import enrich_with_fund_details

router = APIRouter(prefix='/api/advanced', tags=['自选分组管理'])


def get_db():
    """获取数据库连接"""
    return db.get_conn()


# ============================================================
# 数据模型
# ============================================================

class GroupCreate(BaseModel):
    name: str
    color: str = '#6366f1'
    icon: str = '📁'


class GroupUpdate(BaseModel):
    name: Optional[str] = None
    color: Optional[str] = None
    icon: Optional[str] = None
    sort_order: Optional[int] = None


class GroupItemAdd(BaseModel):
    group_id: int
    code: str


class GroupItemReorder(BaseModel):
    group_id: int
    codes: list[str]


class GroupItemMove(BaseModel):
    from_group_id: int
    to_group_id: int
    code: str
    to_index: int = 0


# ============================================================
# 分组管理API
# ============================================================

@router.get('/groups',
         summary="获取所有分组",
         responses={200: {"description": "自选分组列表（含各分组成员数量）"}})
async def list_groups():
    """获取所有自选分组（含各分组成员数量），按排序字段和ID排序。"""
    db_conn = get_db()
    rows = db_conn.execute('''
        SELECT g.*, COUNT(gi.id) as item_count
        FROM watch_groups g
        LEFT JOIN watch_group_items gi ON g.id = gi.group_id
        GROUP BY g.id
        ORDER BY g.sort_order, g.id
    ''').fetchall()
    return {'ok': True, 'groups': [dict(r) for r in rows]}


@router.post('/groups',
         summary="创建分组",
         responses={200: {"description": "创建成功，返回分组ID"}})
async def create_group(data: GroupCreate):
    """创建新的自选分组。

    **请求体示例**：
    ```json
    {"name": "核心持仓", "color": "#6366f1", "icon": "📁"}
    ```
    """
    db_conn = get_db()
    max_order = db_conn.execute('SELECT COALESCE(MAX(sort_order), 0) FROM watch_groups').fetchone()[0]
    cursor = db_conn.execute('''
        INSERT INTO watch_groups (name, color, icon, sort_order)
        VALUES (?, ?, ?, ?)
    ''', (data.name, data.color, data.icon, max_order + 1))
    db_conn.commit()
    return {'ok': True, 'id': cursor.lastrowid}


@router.put('/groups/{group_id}',
         summary="更新分组",
         responses={
             200: {"description": "更新成功"},
             400: {"description": "没有需要更新的字段"},
         })
async def update_group(group_id: int, data: GroupUpdate):
    """更新分组的名称、颜色、图标或排序。

    - **group_id**: 分组ID
    """
    db_conn = get_db()
    updates = []
    params = []
    if data.name is not None:
        updates.append('name = ?')
        params.append(data.name)
    if data.color is not None:
        updates.append('color = ?')
        params.append(data.color)
    if data.icon is not None:
        updates.append('icon = ?')
        params.append(data.icon)
    if data.sort_order is not None:
        updates.append('sort_order = ?')
        params.append(data.sort_order)
    if not updates:
        raise HTTPException(400, '没有需要更新的字段')
    params.append(group_id)
    db_conn.execute(f'UPDATE watch_groups SET {", ".join(updates)} WHERE id = ?', params)
    db_conn.commit()
    return {'ok': True}


@router.delete('/groups/{group_id}',
         summary="删除分组",
         responses={200: {"description": "删除成功"}})
async def delete_group(group_id: int):
    """删除分组及其所有成员关系。

    - **group_id**: 分组ID
    """
    db_conn = get_db()
    db_conn.execute('DELETE FROM watch_group_items WHERE group_id = ?', (group_id,))
    db_conn.execute('DELETE FROM watch_groups WHERE id = ?', (group_id,))
    db_conn.commit()
    return {'ok': True}


@router.post('/groups/items',
         summary="添加基金到分组",
         responses={200: {"description": "添加成功"}, 400: {"description": "添加失败"}})
async def add_group_item(data: GroupItemAdd):
    """将指定基金添加到分组中（已存在则忽略）。"""
    db_conn = get_db()
    try:
        db_conn.execute('''
            INSERT OR IGNORE INTO watch_group_items (group_id, code)
            VALUES (?, ?)
        ''', (data.group_id, data.code))
        db_conn.commit()
        return {'ok': True}
    except Exception as e:
        raise HTTPException(400, str(e))


@router.delete('/groups/{group_id}/items/{code}',
         summary="从分组移除基金",
         responses={200: {"description": "移除成功"}})
async def remove_group_item(group_id: int, code: str):
    """从分组中移除指定基金。

    - **group_id**: 分组ID
    - **code**: 基金代码
    """
    db_conn = get_db()
    db_conn.execute('DELETE FROM watch_group_items WHERE group_id = ? AND code = ?', (group_id, code))
    db_conn.commit()
    return {'ok': True}


@router.get('/groups/{group_id}/items',
         summary="获取分组成员列表",
         responses={200: {"description": "分组内基金列表（含基金详情数据）"}})
async def list_group_items(group_id: int):
    """获取指定分组内的基金列表（使用统一基金详情服务，保证字段一致）。

    - **group_id**: 分组ID
    """
    db_conn = get_db()
    rows = db_conn.execute('''
        SELECT gi.*
        FROM watch_group_items gi
        WHERE gi.group_id = ?
        ORDER BY gi.sort_order, gi.id
    ''', (group_id,)).fetchall()
    items = [dict(r) for r in rows]
    enriched = enrich_with_fund_details(items)
    return {'ok': True, 'items': enriched}


@router.put('/groups/{group_id}/items/reorder',
         summary="批量更新分组成员排序",
         responses={200: {"description": "排序更新成功"}, 400: {"description": "group_id不匹配"}})
async def reorder_group_items(group_id: int, data: GroupItemReorder):
    """按传入的代码顺序重新排列分组成员。

    - **group_id**: 分组ID（需与请求体中的group_id一致）
    """
    if data.group_id != group_id:
        raise HTTPException(400, 'group_id不匹配')
    db_conn = get_db()
    for idx, code in enumerate(data.codes):
        db_conn.execute(
            'UPDATE watch_group_items SET sort_order = ? WHERE group_id = ? AND code = ?',
            (idx, group_id, code),
        )
    db_conn.commit()
    return {'ok': True}


@router.post('/groups/items/move',
         summary="在分组间移动基金",
         responses={200: {"description": "移动成功"}})
async def move_group_item(data: GroupItemMove):
    """将基金从一个分组移动到另一个分组的指定位置，并更新目标分组排序。"""
    db_conn = get_db()
    # 从源分组移除
    db_conn.execute(
        'DELETE FROM watch_group_items WHERE group_id = ? AND code = ?',
        (data.from_group_id, data.code),
    )
    # 目标分组中插入到指定位置，先把后面的元素后移
    db_conn.execute(
        'UPDATE watch_group_items SET sort_order = sort_order + 1 WHERE group_id = ? AND sort_order >= ?',
        (data.to_group_id, data.to_index),
    )
    db_conn.execute(
        'INSERT OR IGNORE INTO watch_group_items (group_id, code, sort_order) VALUES (?, ?, ?)',
        (data.to_group_id, data.code, data.to_index),
    )
    db_conn.commit()
    return {'ok': True}
