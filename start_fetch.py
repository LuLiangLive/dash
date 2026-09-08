import urllib.request
import json

# 触发一键更新
data = json.dumps({"scope": "funds_pool"}).encode('utf-8')
req = urllib.request.Request(
    'http://127.0.0.1:8002/api/fetch/start',
    data=data,
    headers={'Content-Type': 'application/json'},
    method='POST'
)
try:
    resp = urllib.request.urlopen(req, timeout=10)
    print('触发成功:', json.loads(resp.read()))
except Exception as e:
    print(f'触发失败: {e}')
    # 尝试不带/api前缀
    try:
        req2 = urllib.request.Request(
            'http://127.0.0.1:8002/fetch/start',
            data=data,
            headers={'Content-Type': 'application/json'},
            method='POST'
        )
        resp2 = urllib.request.urlopen(req2, timeout=10)
        print('触发成功(不带/api):', json.loads(resp2.read()))
    except Exception as e2:
        print(f'触发失败(不带/api): {e2}')
