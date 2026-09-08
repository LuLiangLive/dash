"""
api/sync.py —— 多设备数据同步API

从 api/advanced.py 拆分（v2.5.5架构重构）
负责：数据导出导入（本地同步方式）
v2.8.1: sync/export 与 sync/import 挂 require_api_key——
此前无鉴权，公网部署时任何人可匿名拉取 自选/持仓/提醒/设置 全量数据（安全风险）。
"""
import datetime
from fastapi import APIRouter, HTTPException, Query, Depends
import db
from auth import require_api_key

router = APIRouter(prefix='/api/advanced', tags=['多设备数据同步'])


def get_db():
    """获取数据库连接"""
    return db.get_conn()


# ============================================================
# 多设备数据同步API（本地导入导出方式）
# ============================================================

@router.get('/sync/export',
         summary="导出同步数据",
         dependencies=[Depends(require_api_key)],
         responses={200: {"description": "导出的数据（JSON格式，含自选/分组/持仓/提醒/设置）"}})
async def sync_export(data_type: str = Query(default='all', description="数据类型：all/watchlist/groups/portfolio/alerts/settings")):
    """导出数据用于多设备同步。

    可导出自选列表、分组、持仓、提醒规则、系统设置等数据。
    """
    db_conn = get_db()
    data = {}

    if data_type in ['all', 'watchlist']:
        data['watchlist'] = [dict(r) for r in db_conn.execute('SELECT * FROM watchlist').fetchall()]
    if data_type in ['all', 'groups']:
        data['groups'] = [dict(r) for r in db_conn.execute('SELECT * FROM watch_groups').fetchall()]
        data['group_items'] = [dict(r) for r in db_conn.execute('SELECT * FROM watch_group_items').fetchall()]
    if data_type in ['all', 'portfolio']:
        data['portfolio'] = [dict(r) for r in db_conn.execute('SELECT * FROM portfolio').fetchall()]
        data['investment_records'] = [dict(r) for r in db_conn.execute('SELECT * FROM investment_records').fetchall()]
    if data_type in ['all', 'alerts']:
        data['alert_rules'] = [dict(r) for r in db_conn.execute('SELECT * FROM alert_rules').fetchall()]
    if data_type in ['all', 'settings']:
        data['settings'] = [dict(r) for r in db_conn.execute('SELECT * FROM settings').fetchall()]

    db_conn.execute('''
        INSERT INTO sync_log (data_type, action, record_count, status)
        VALUES (?, 'export', ?, 'success')
    ''', (data_type, sum(len(v) for v in data.values())))
    db_conn.commit()

    return {'ok': True, 'data': data, 'exported_at': datetime.datetime.now().isoformat()}


@router.post('/sync/import',
         summary="导入同步数据",
         dependencies=[Depends(require_api_key)],
         responses={
             200: {"description": "导入完成（含成功数量和错误列表）"},
             500: {"description": "导入失败"},
         })
async def sync_import(payload: dict):
    """导入数据用于多设备同步。

    支持自选、分组、持仓、提醒、设置等数据的批量导入（INSERT OR REPLACE）。
    """
    db_conn = get_db()
    imported = 0
    errors = []

    try:
        if 'watchlist' in payload:
            for item in payload['watchlist']:
                try:
                    db_conn.execute('''
                        INSERT OR REPLACE INTO watchlist (code, name, position, added_at, group_id, fav)
                        VALUES (?, ?, ?, ?, ?, ?)
                    ''', (item.get('code'), item.get('name'), item.get('position', 0),
                          item.get('added_at'), item.get('group_id', 0), item.get('fav', 0)))
                    imported += 1
                except Exception as e:
                    errors.append(f'watchlist {item.get("code")}: {e}')

        if 'groups' in payload:
            for item in payload['groups']:
                try:
                    db_conn.execute('''
                        INSERT OR REPLACE INTO watch_groups (id, name, color, icon, sort_order, created_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                    ''', (item.get('id'), item.get('name'), item.get('color', '#6366f1'),
                          item.get('icon', '📁'), item.get('sort_order', 0), item.get('created_at')))
                    imported += 1
                except Exception as e:
                    errors.append(f'group {item.get("name")}: {e}')

        if 'group_items' in payload:
            for item in payload['group_items']:
                try:
                    db_conn.execute('''
                        INSERT OR IGNORE INTO watch_group_items (group_id, code, sort_order, added_at)
                        VALUES (?, ?, ?, ?)
                    ''', (item.get('group_id'), item.get('code'),
                          item.get('sort_order', 0), item.get('added_at')))
                    imported += 1
                except Exception as e:
                    errors.append(f'group_item: {e}')

        if 'portfolio' in payload:
            for item in payload['portfolio']:
                try:
                    db_conn.execute('''
                        INSERT OR REPLACE INTO portfolio
                        (code, name, total_shares, avg_cost, total_amount, total_profit,
                         profit_pct, current_nav, nav_date, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (item.get('code'), item.get('name'), item.get('total_shares', 0),
                          item.get('avg_cost', 0), item.get('total_amount', 0),
                          item.get('total_profit', 0), item.get('profit_pct', 0),
                          item.get('current_nav', 0), item.get('nav_date'),
                          item.get('created_at'), item.get('updated_at')))
                    imported += 1
                except Exception as e:
                    errors.append(f'portfolio {item.get("code")}: {e}')

        if 'investment_records' in payload:
            for item in payload['investment_records']:
                try:
                    db_conn.execute('''
                        INSERT OR IGNORE INTO investment_records
                        (code, name, type, date, amount, shares, nav, fee, profit_target, note, source, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (item.get('code'), item.get('name'), item.get('type', 'buy'),
                          item.get('date'), item.get('amount', 0), item.get('shares', 0),
                          item.get('nav', 0), item.get('fee', 0), item.get('profit_target'),
                          item.get('note'), item.get('source', 'manual'), item.get('created_at')))
                    imported += 1
                except Exception as e:
                    errors.append(f'investment {item.get("code")}: {e}')

        if 'alert_rules' in payload:
            for item in payload['alert_rules']:
                try:
                    db_conn.execute('''
                        INSERT OR REPLACE INTO alert_rules
                        (id, type, code, name, condition, threshold, enabled, last_triggered, trigger_count, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (item.get('id'), item.get('type'), item.get('code'), item.get('name'),
                          item.get('condition'), item.get('threshold', 0), item.get('enabled', 1),
                          item.get('last_triggered'), item.get('trigger_count', 0), item.get('created_at')))
                    imported += 1
                except Exception as e:
                    errors.append(f'alert {item.get("name")}: {e}')

        if 'settings' in payload:
            for item in payload['settings']:
                try:
                    db_conn.execute('''
                        INSERT OR REPLACE INTO settings (key, value, updated_at)
                        VALUES (?, ?, ?)
                    ''', (item.get('key'), item.get('value'), item.get('updated_at')))
                    imported += 1
                except Exception as e:
                    errors.append(f'setting {item.get("key")}: {e}')

        db_conn.commit()

        db_conn.execute('''
            INSERT INTO sync_log (data_type, action, record_count, status, message)
            VALUES (?, 'import', ?, 'success', ?)
        ''', ('all', imported, f'errors: {len(errors)}'))
        db_conn.commit()

        return {'ok': True, 'imported': imported, 'errors': errors}

    except Exception as e:
        db_conn.rollback()
        raise HTTPException(500, f'导入失败: {str(e)}')
