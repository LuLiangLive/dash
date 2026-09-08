"""
迁移脚本：将investment_records中的止盈目标同步到alert_rules表
运行方式：python backend/scripts/migrate_profit_alerts.py
"""
import sqlite3
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from security.crypto import encrypt

DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'fund.db')

def migrate():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    
    # 获取所有设置了止盈目标的基金（去重）
    rows = conn.execute("""
        SELECT DISTINCT r.code, COALESCE(f.name, r.name, r.code) as name, r.profit_target
        FROM investment_records r
        LEFT JOIN funds f ON f.code = r.code
        WHERE r.type = 'buy' AND r.profit_target IS NOT NULL AND r.profit_target > 0
    """).fetchall()
    
    print(f"找到 {len(rows)} 只设置了止盈目标的基金")
    
    migrated = 0
    skipped = 0
    for row in rows:
        code = row['code']
        name = row['name']
        target = row['profit_target']
        
        # 检查是否已存在profit类型的规则
        exist = conn.execute(
            "SELECT id FROM alert_rules WHERE type = 'profit' AND code = ?",
            (code,)
        ).fetchone()
        
        if exist:
            # 更新阈值
            conn.execute(
                "UPDATE alert_rules SET threshold = ?, enabled = 1 WHERE id = ?",
                (target, exist['id'])
            )
            print(f"  更新: {code} {name} → 目标 {target}%")
            skipped += 1
        else:
            # 创建新规则
            enc_name = encrypt(name) if name else None
            conn.execute("""
                INSERT INTO alert_rules (type, code, name, condition, threshold, enabled)
                VALUES ('profit', ?, ?, 'above', ?, 1)
            """, (code, enc_name, target))
            print(f"  创建: {code} {name} → 目标 {target}%")
            migrated += 1
    
    conn.commit()
    conn.close()
    print(f"\n完成：新增 {migrated} 条，更新 {skipped} 条")

if __name__ == '__main__':
    migrate()
