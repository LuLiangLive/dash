"""
modules/alert/router.py —— 提醒通知管理API

从 api/alerts.py 迁移（v2.5.5架构重构 - 第4步按领域模块重组）
负责：提醒规则CRUD、提醒历史、提醒检查触发
v2.7.1: 敏感字段（name）加密存储
"""
from typing import Optional
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
import db
from security.crypto import encrypt, decrypt

router = APIRouter(prefix='/api/advanced', tags=['提醒通知管理'])


def get_db():
    return db.get_conn()


def _decrypt_rule(rule):
    if rule and rule.get("name"):
        rule["name"] = decrypt(rule["name"])
    return rule


# ============================================================
# 数据模型
# ============================================================

class AlertRuleCreate(BaseModel):
    type: str  # price/score/signal/profit
    code: Optional[str] = None
    name: Optional[str] = None
    condition: str  # above/below/change/upgrade
    threshold: float
    enabled: bool = True


class AlertRuleUpdate(BaseModel):
    name: Optional[str] = None
    condition: Optional[str] = None
    threshold: Optional[float] = None
    enabled: Optional[bool] = None


# ============================================================
# 提醒规则管理API
# ============================================================

@router.get('/alerts/rules', summary='获取提醒规则列表', responses={200: {'description': '提醒规则列表'}})
async def list_alert_rules(type: Optional[str] = Query(default=None, description="按提醒类型筛选：price/score/signal/profit")):
    """获取提醒规则列表（支持按类型筛选）。"""
    db_conn = get_db()
    if type:
        rows = db_conn.execute('SELECT * FROM alert_rules WHERE type = ? ORDER BY id', (type,)).fetchall()
    else:
        rows = db_conn.execute('SELECT * FROM alert_rules ORDER BY type, id').fetchall()
    return {'ok': True, 'rules': [_decrypt_rule(dict(r)) for r in rows]}


@router.post('/alerts/rules', summary='创建提醒规则', responses={200: {'description': '创建成功，返回规则ID'}})
async def create_alert_rule(data: AlertRuleCreate):
    """创建提醒规则。

    支持价格提醒(price)、评分提醒(score)、信号提醒(signal)、收益提醒(profit)四种类型。
    """
    db_conn = get_db()
    enc_name = encrypt(data.name) if data.name else None
    # 空字符串 code 转为 None（表示全局提醒）
    rule_code = data.code if data.code else None
    cursor = db_conn.execute('''
        INSERT INTO alert_rules (type, code, name, condition, threshold, enabled)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (data.type, rule_code, enc_name, data.condition, data.threshold, 1 if data.enabled else 0))
    db_conn.commit()
    return {'ok': True, 'id': cursor.lastrowid}


@router.put('/alerts/rules/{rule_id}', summary='更新提醒规则', responses={200: {'description': '更新成功'}, 400: {'description': '无更新字段'}})
async def update_alert_rule(rule_id: int, data: AlertRuleUpdate):
    """更新提醒规则（名称/条件/阈值/启用状态）。

    - **rule_id**: 规则ID
    """
    db_conn = get_db()
    updates = []
    params = []
    if data.name is not None:
        updates.append('name = ?')
        params.append(encrypt(data.name))
    if data.condition is not None:
        updates.append('condition = ?')
        params.append(data.condition)
    if data.threshold is not None:
        updates.append('threshold = ?')
        params.append(data.threshold)
    if data.enabled is not None:
        updates.append('enabled = ?')
        params.append(1 if data.enabled else 0)
    if not updates:
        raise HTTPException(400, '没有需要更新的字段')
    params.append(rule_id)
    db_conn.execute(f'UPDATE alert_rules SET {", ".join(updates)} WHERE id = ?', params)
    db_conn.commit()
    return {'ok': True}


@router.delete('/alerts/rules/{rule_id}', summary='删除提醒规则', responses={200: {'description': '删除成功'}})
async def delete_alert_rule(rule_id: int):
    """删除提醒规则。

    - **rule_id**: 规则ID
    """
    db_conn = get_db()
    db_conn.execute('DELETE FROM alert_rules WHERE id = ?', (rule_id,))
    db_conn.commit()
    return {'ok': True}


# ============================================================
# 提醒历史API
# ============================================================

@router.get('/alerts/history', summary='获取提醒历史', responses={200: {'description': '提醒历史列表及未读数量'}})
async def list_alert_history(limit: int = Query(default=50, description="返回数量"), unread_only: bool = Query(default=False, description="仅返回未读提醒")):
    """获取提醒历史列表（含未读数量统计）。"""
    db_conn = get_db()
    if unread_only:
        rows = db_conn.execute('''
            SELECT * FROM alert_history WHERE read = 0
            ORDER BY triggered_at DESC LIMIT ?
        ''', (limit,)).fetchall()
    else:
        rows = db_conn.execute('''
            SELECT * FROM alert_history
            ORDER BY triggered_at DESC LIMIT ?
        ''', (limit,)).fetchall()
    unread_count = db_conn.execute('SELECT COUNT(*) FROM alert_history WHERE read = 0').fetchone()[0]
    return {'ok': True, 'history': [dict(r) for r in rows], 'unread_count': unread_count}


@router.post('/alerts/history/{alert_id}/read', summary='标记提醒已读', responses={200: {'description': '标记成功'}})
async def mark_alert_read(alert_id: int):
    """标记单条提醒为已读。

    - **alert_id**: 提醒记录ID
    """
    db_conn = get_db()
    db_conn.execute('UPDATE alert_history SET read = 1 WHERE id = ?', (alert_id,))
    db_conn.commit()
    return {'ok': True}


@router.post('/alerts/history/read-all', summary='全部标记已读', responses={200: {'description': '标记成功'}})
async def mark_all_alerts_read():
    """批量标记所有未读提醒为已读。"""
    db_conn = get_db()
    db_conn.execute('UPDATE alert_history SET read = 1 WHERE read = 0')
    db_conn.commit()
    return {'ok': True}


@router.delete('/alerts/history/{alert_id}', summary='删除提醒历史', responses={200: {'description': '删除成功'}, 404: {'description': '记录不存在'}})
async def delete_alert_history(alert_id: int):
    """删除单条提醒历史。"""
    db_conn = get_db()
    row = db_conn.execute('SELECT id FROM alert_history WHERE id = ?', (alert_id,)).fetchone()
    if not row:
        raise HTTPException(404, '未找到该提醒历史')
    db_conn.execute('DELETE FROM alert_history WHERE id = ?', (alert_id,))
    db_conn.commit()
    return {'ok': True}


# ============================================================
# 提醒检查触发API
# ============================================================

@router.post('/alerts/check', summary='触发提醒检查', responses={200: {'description': '检查完成，返回触发的提醒列表'}})
async def check_alerts():
    """手动触发提醒检查（遍历所有启用规则，匹配条件的写入提醒历史）。"""
    db_conn = get_db()
    rules = db_conn.execute('SELECT * FROM alert_rules WHERE enabled = 1').fetchall()
    triggered = []

    for rule in rules:
        try:
            if rule['type'] == 'score':
                funds = db_conn.execute('SELECT code, name, score, ad_score, earn_score FROM funds').fetchall()
                for f in funds:
                    if rule['condition'] == 'above' and f['score'] and f['score'] >= rule['threshold']:
                        _trigger_alert(db_conn, rule, f['code'], f['name'], f['score'],
                                        f'综合分 {f["score"]} 达到阈值 {rule["threshold"]}')
                        triggered.append({'code': f['code'], 'name': f['name'], 'score': f['score']})

            elif rule['type'] == 'price':
                funds = db_conn.execute('SELECT code, name, d1 FROM funds').fetchall()
                for f in funds:
                    if f['d1'] is None:
                        continue
                    if rule['condition'] == 'above' and f['d1'] >= rule['threshold']:
                        _trigger_alert(db_conn, rule, f['code'], f['name'], f['d1'],
                                        f'日涨幅 {f["d1"]}% 超过阈值 {rule["threshold"]}%')
                        triggered.append({'code': f['code'], 'name': f['name'], 'change': f['d1']})
                    elif rule['condition'] == 'below' and f['d1'] <= -rule['threshold']:
                        _trigger_alert(db_conn, rule, f['code'], f['name'], f['d1'],
                                        f'日跌幅 {f["d1"]}% 超过阈值 {rule["threshold"]}%')
                        triggered.append({'code': f['code'], 'name': f['name'], 'change': f['d1']})

            elif rule['type'] == 'signal':
                funds = db_conn.execute('SELECT code, name, reco, prev_reco FROM funds WHERE reco IS NOT NULL').fetchall()
                for f in funds:
                    if f['reco'] and f['prev_reco'] and f['reco'] != f['prev_reco']:
                        _trigger_alert(db_conn, rule, f['code'], f['name'], 0,
                                        f'推荐信号从 {f["prev_reco"]} 变为 {f["reco"]}')
                        triggered.append({'code': f['code'], 'name': f['name'], 'reco': f['reco']})

            elif rule['type'] == 'profit':
                # v2.8.1: 规则绑定了 code 时只检查该基金——此前无过滤，
                # A 基金的止盈目标会对所有持仓触发提醒
                if rule['code']:
                    portfolios = db_conn.execute('SELECT * FROM portfolio WHERE code = ?', (rule['code'],)).fetchall()
                else:
                    portfolios = db_conn.execute('SELECT * FROM portfolio').fetchall()
                for p in portfolios:
                    if p['profit_pct'] is None:
                        continue
                    if rule['condition'] == 'above' and p['profit_pct'] >= rule['threshold']:
                        _trigger_alert(db_conn, rule, p['code'], p['name'], p['profit_pct'],
                                        f'{p["name"]} 收益率 {p["profit_pct"]}% 达到目标 {rule["threshold"]}%')
                        triggered.append({'code': p['code'], 'name': p['name'], 'profit_pct': p['profit_pct']})

        except Exception as e:
            continue

    return {'ok': True, 'triggered': triggered, 'count': len(triggered)}


# ============================================================
# 内部工具函数
# ============================================================

def _trigger_alert(db_conn, rule, code, name, value, message):
    recent = db_conn.execute('''
        SELECT id FROM alert_history
        WHERE rule_id = ? AND code = ? AND triggered_at > datetime('now', '-1 hour')
    ''', (rule['id'], code)).fetchone()
    if recent:
        return

    db_conn.execute('''
        INSERT INTO alert_history (rule_id, type, code, name, message, value, threshold)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (rule['id'], rule['type'], code, name, message, value, rule['threshold']))

    db_conn.execute('''
        UPDATE alert_rules SET last_triggered = datetime('now', 'localtime'),
        trigger_count = trigger_count + 1 WHERE id = ?
    ''', (rule['id'],))
    db_conn.commit()
