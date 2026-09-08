# -*- coding: utf-8 -*-
import sys
sys.path.insert(0, '.')
import db
import urllib.request
import json

codes = ['001174', '001191', '001330', '001332', '001400']
headers = {'X-API-Key': '35a92aeb9e0acef9bfbdcbe733f74c4d'}

print('=== 多基金分数一致性验证 ===')
all_match = True
for code in codes:
    f = db.get_fund(code)
    if not f:
        print(f'{code}: 未找到')
        continue
    db_ad = f.get('ad_score')
    db_er = f.get('earn_score')
    db_sc = f.get('score')

    try:
        req = urllib.request.Request(f'http://127.0.0.1:8002/api/fund/anti_detail?code={code}', headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        api_ad = data['fund']['resist']['ad_score']
        api_er = data['fund']['resist']['earn_score']
        api_sc = data['fund']['resist']['dual']
    except Exception as e:
        print(f'{code}: API请求失败 {e}')
        continue

    match = (db_ad == api_ad and db_er == api_er and db_sc == api_sc)
    status = '✅ 一致' if match else '❌ 不一致'
    if not match:
        all_match = False
    print(f'{code} {f.get("name", "")[:15]:15s}: DB=[{db_ad},{db_er},{db_sc}] API=[{api_ad},{api_er},{api_sc}] {status}')

print()
print(f'总体结果: {"✅ 全部一致" if all_match else "❌ 存在不一致"}')
