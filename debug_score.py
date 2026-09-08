# -*- coding: utf-8 -*-
import sys
sys.path.insert(0, '.')
from modules.anti.anti_detail import anti_detail_sync

result = anti_detail_sync('001191')
print('=== API返回结果 ===')
print(f'ad_score: {result["fund"]["resist"]["ad_score"]}')
print(f'earn_score: {result["fund"]["resist"]["earn_score"]}')
print(f'dual: {result["fund"]["resist"]["dual"]}')

print()
print('=== 直接查数据库 ===')
import db
f = db.get_fund('001191')
print(f'ad_score: {f.get("ad_score")}')
print(f'earn_score: {f.get("earn_score")}')
print(f'score: {f.get("score")}')
