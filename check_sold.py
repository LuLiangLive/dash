import sqlite3

db_path = 'fund.db'
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row

print("=" * 80)
print("1. 查询portfolio表中所有基金（包括清仓的）")
print("=" * 80)

rows = conn.execute('''
    SELECT code, name, total_shares, total_amount, total_profit, avg_cost, current_nav, updated_at
    FROM portfolio
    ORDER BY total_amount DESC
''').fetchall()

print(f"\n共 {len(rows)} 只基金记录\n")
print(f"{'代码':<10} {'名称':<25} {'份额':>12} {'金额':>12} {'盈亏':>12} {'状态'}")
print("-" * 90)

holding_count = 0
sold_count = 0
for r in rows:
    d = dict(r)
    status = "持有中" if (d['total_shares'] or 0) > 0 else "已清仓"
    if (d['total_shares'] or 0) > 0:
        holding_count += 1
    else:
        sold_count += 1
    print(f"{d['code']:<10} {d['name'][:24]:<25} {d['total_shares'] or 0:>12.2f} {d['total_amount'] or 0:>12.2f} {d['total_profit'] or 0:>12.2f} {status}")

print("-" * 90)
print(f"持有中: {holding_count} 只  已清仓: {sold_count} 只")

print()
print("=" * 80)
print("2. 查询investment_records表中的卖出记录")
print("=" * 80)

sell_rows = conn.execute('''
    SELECT id, code, name, date, type, amount, shares, nav, fee, note, source, created_at
    FROM investment_records
    WHERE type = 'sell'
    ORDER BY date DESC, id DESC
    LIMIT 10
''').fetchall()

print(f"\n最近10条卖出记录（共查询前10条）\n")
for r in sell_rows:
    d = dict(r)
    print(f"ID:{d['id']} {d['code']} {d.get('name','')[:20]} date={d['date']} shares={d['shares']} nav={d['nav']} amount={d['amount']} source={d.get('source','')}")

print()
print("=" * 80)
print("3. 查询卖出后portfolio表是否更新（total_shares是否减少）")
print("=" * 80)

# 查询有卖出记录的基金
sell_codes = conn.execute('''
    SELECT DISTINCT code FROM investment_records WHERE type = 'sell'
''').fetchall()

print(f"\n有卖出记录的基金: {len(sell_codes)} 只\n")
for r in sell_codes:
    code = r['code']
    portfolio = conn.execute('SELECT * FROM portfolio WHERE code = ?', (code,)).fetchone()
    buy_total = conn.execute('SELECT SUM(shares) as total FROM investment_records WHERE code = ? AND type = ?', (code, 'buy')).fetchone()
    sell_total = conn.execute('SELECT SUM(shares) as total FROM investment_records WHERE code = ? AND type = ?', (code, 'sell')).fetchone()
    
    buy_shares = buy_total['total'] or 0
    sell_shares = sell_total['total'] or 0
    remaining = buy_shares - sell_shares
    
    if portfolio:
        p = dict(portfolio)
        print(f"  {code} {p.get('name','')[:20]}: 买入={buy_shares:.2f} 卖出={sell_shares:.2f} 剩余={remaining:.2f} portfolio份额={p.get('total_shares',0):.2f} 状态={'一致' if abs(remaining - (p.get('total_shares',0) or 0)) < 0.01 else '不一致⚠️'}")
    else:
        print(f"  {code}: 买入={buy_shares:.2f} 卖出={sell_shares:.2f} 剩余={remaining:.2f} portfolio中无记录⚠️")

conn.close()
