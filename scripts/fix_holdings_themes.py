# -*- coding: utf-8 -*-
"""
scripts/fix_holdings_themes.py —— 持仓主题占比补抓

天天基金 fundf10 jjcc 接口对并发敏感:采集主流程(并行抓 63 只净值+持仓)期间
部分基金持仓抓取会因限流失败,回落为配置兜底(null 占比)。

本脚本在限流恢复后,遍历 DB 中 themes 为空/含 null 占比的基金,逐个补抓真实
持仓主题占比并写回。建议每次采集后运行一次。

用法:
    python scripts/fix_holdings_themes.py
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db
from collector import fetcher


def main():
    fs = db.list_funds()
    targets = []
    for f in fs:
        th = f.get("themes") or []
        has_null = any(isinstance(t, dict) and t.get("pct") is None for t in th)
        if not th or has_null:
            targets.append(f["code"])

    if not targets:
        print("没有需要补抓的基金,持仓主题均已就绪。")
        return

    print(f"待补抓 {len(targets)} 只: {targets}")
    ok, fail = 0, []
    for i, code in enumerate(targets, 1):
        th = fetcher.holdings_themes(fetcher.fetch_holdings_w(code))
        if th:
            f = db.get_fund(code)
            if f:
                f["themes"] = th
                db.upsert_fund(f)
            ok += 1
        else:
            fail.append(code)
        if i % 5 == 0 or i == len(targets):
            print(f"  进度 {i}/{len(targets)}, 已成功 {ok}")
        time.sleep(0.5)

    print(f"\n完成: 成功 {ok}, 失败 {len(fail)} {fail[:10]}")


if __name__ == "__main__":
    main()
