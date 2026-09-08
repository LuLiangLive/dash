import sqlite3

db_path = 'fund.db'
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row

print("=" * 80)
print("1. 检查portfolio表结构")
print("=" * 80)

columns = conn.execute('PRAGMA table_info(portfolio)').fetchall()
print(f"\nportfolio表字段（共{len(columns)}个）：")
has_status = False
for col in columns:
    print(f"  {col['name']:<20} {col['type']:<15} {'NOT NULL' if col['notnull'] else ''}")
    if col['name'] == 'status':
        has_status = True

if not has_status:
    print("\n⚠️  portfolio表缺少status字段，正在添加...")
    conn.execute("ALTER TABLE portfolio ADD COLUMN status TEXT DEFAULT 'holding'")
    conn.commit()
    print("✅ 已添加status字段，默认值为'holding'")
else:
    print("\n✅ portfolio表已有status字段")

print()
print("=" * 80)
print("2. 重新计算已清仓基金（让它们出现在portfolio表中）")
print("=" * 80)

# 查询有卖出记录但不在portfolio表中的基金
sold_codes = conn.execute('''
    SELECT DISTINCT code FROM investment_records 
    WHERE type = 'sell' 
    AND code NOT IN (SELECT code FROM portfolio)
''').fetchall()

print(f"\n需要重新计算的已清仓基金: {len(sold_codes)} 只")

# 导入_recalculate_portfolio函数
import sys
sys.path.insert(0, '.')
from modules.portfolio.router import _recalculate_portfolio, get_db

for r in sold_codes:
    code = r['code']
    _recalculate_portfolio(conn, code)
    # 查询基金名称
    fund = conn.execute('SELECT name FROM funds WHERE code = ?', (code,)).fetchone()
    name = fund['name'] if fund else code
    print(f"  ✅ {code} {name[:20]} 已重新计算")

conn.commit()

print()
print("=" * 80)
print("3. 验证清仓基金是否出现在portfolio表中")
print("=" * 80)

rows = conn.execute('''
    SELECT code, name, total_shares, total_amount, total_profit, status, updated_at
    FROM portfolio
    ORDER BY status, total_amount DESC
''').fetchall()

print(f"\nportfolio表共 {len(rows)} 只基金\n")
print(f"{'代码':<10} {'名称':<25} {'份额':>10} {'金额':>12} {'盈亏':>12} {'状态':<10}")
print("-" * 85)

holding_count = 0
sold_count = 0
for r in rows:
    d = dict(r)
    status = d.get('status', 'holding')
    if status == 'sold':
        sold_count += 1
    else:
        holding_count += 1
    print(f"{d['code']:<10} {d['name'][:24]:<25} {d['total_shares'] or 0:>10.2f} {d['total_amount'] or 0:>12.2f} {d['total_profit'] or 0:>12.2f} {status:<10}")

print("-" * 85)
print(f"持有中: {holding_count} 只  已清仓: {sold_count} 只")

print()
print("=" * 80)
print("4. 添加止盈目标测试案例")
print("=" * 80)

# 为前3只持有中的基金添加止盈目标
holding_funds = conn.execute('''
    SELECT code, name FROM portfolio 
    WHERE status = 'holding' OR status IS NULL 
    ORDER BY total_amount DESC LIMIT 3
''').fetchall()

print(f"\n为以下3只基金添加13%止盈目标：")
for r in holding_funds:
    code = r['code']
    name = r['name']
    # 更新该基金的所有买入记录，添加profit_target=13
    updated = conn.execute('''
        UPDATE investment_records 
        SET profit_target = 13.0 
        WHERE code = ? AND type = 'buy' AND (profit_target IS NULL OR profit_target = 0)
    ''', (code,))
    print(f"  ✅ {code} {name[:20]}: 更新了 {updated.rowcount} 条买入记录的止盈目标")

conn.commit()

# 验证止盈记录
profit_records = conn.execute('''
    SELECT r.id, r.code, r.name, r.date, r.nav as buy_nav, r.shares, r.profit_target,
           f.nav as current_nav
    FROM investment_records r
    LEFT JOIN funds f ON f.code = r.code
    WHERE r.type = 'buy' AND r.profit_target IS NOT NULL AND r.profit_target > 0
    ORDER BY r.date DESC
''').fetchall()

print(f"\n止盈目标记录数: {len(profit_records)}")
for r in profit_records[:10]:
    d = dict(r)
    buy_nav = d['buy_nav'] or 0
    current_nav = d['current_nav'] or 0
    pct = ((current_nav - buy_nav) / buy_nav * 100) if buy_nav > 0 else 0
    reached = pct >= d['profit_target']
    print(f"  {d['code']} {d.get('name','')[:20]}: 买入={buy_nav:.4f} 当前={current_nav:.4f} 收益率={pct:.2f}% 目标={d['profit_target']}% 达标={'是' if reached else '否'}")

conn.close()

print()
print("=" * 80)
print("所有修复和测试完成")
print("=" * 80)
