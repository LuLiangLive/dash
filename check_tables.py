import sqlite3
conn = sqlite3.connect('fund.db')
cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
tables = [row[0] for row in cursor.fetchall()]
print(f'当前数据库共 {len(tables)} 个表：')
for i, t in enumerate(tables, 1):
    try:
        cnt = conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
        print(f'  {i:2d}. {t:30s} ({cnt} 条记录)')
    except:
        print(f'  {i:2d}. {t:30s}')
conn.close()
