# -*- coding: utf-8 -*-
import sys
sys.path.insert(0, '.')
import db

f = db.get_fund('001191')
if f:
    print(f'基金名称: {f.get("name")}')
    print(f'ad_score: {f.get("ad_score")} (类型: {type(f.get("ad_score"))})')
    print(f'earn_score: {f.get("earn_score")} (类型: {type(f.get("earn_score"))})')
    print(f'score: {f.get("score")} (类型: {type(f.get("score"))})')
    print(f'所有字段: {list(f.keys())}')
else:
    print('未找到基金')
