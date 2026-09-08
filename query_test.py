# -*- coding: utf-8 -*-
import sqlite3
conn = sqlite3.connect('data/funds.db')
conn.row_factory = sqlite3.Row
rows = conn.execute('SELECT code, name, ad_score, earn_score, score FROM funds WHERE ad_score IS NOT NULL AND earn_score IS NOT NULL LIMIT 5').fetchall()
for r in rows:
    print(f'{r["code"]} {r["name"]}: 抗跌={r["ad_score"]} 收益={r["earn_score"]} 综合={r["score"]}')
conn.close()
