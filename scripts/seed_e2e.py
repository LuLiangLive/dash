#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
seed_e2e.py —— 为「用户整链路 E2E」准备本地种子数据（幂等，可重复执行）

用途：仓库里 data_backup 只含用户业务数据，不含行情库。运行
tests/e2e/*.fullchain.spec.ts 前，需要 fund.db 中存在 510300 / 159915
两只基金及其净值，前端卡片/持仓计算才有数据可展示。

用法：
    cd backend
    python3 scripts/seed_e2e.py [fund.db 路径]   # 默认 ./fund.db

说明：
- 仅写 funds / nav_history 两表，不触碰 watchlist/portfolio/alert 等业务表；
- 使用 6 位数字的公开指数代码（510300 沪深300ETF / 159915 创业板ETF），
  名称与净值日期为模拟数据，仅用于本地/CI 跑链路，不会污染云端快照。
"""
import os
import sqlite3
import sys
import datetime

FUNDS = [
    {
        'code': '510300', 'name': '沪深300ETF易方达', 'nav': 3.9056,
        'nav_date': '2026-09-03', 'd1': 0.35, 'score': 82, 'ad_score': 80,
        'earn_score': 84, 'tscore': 82.0, 'sec': '指数型',
    },
    {
        'code': '159915', 'name': '创业板ETF', 'nav': 2.4567,
        'nav_date': '2026-09-03', 'd1': 0.28, 'score': 78, 'ad_score': 75,
        'earn_score': 80, 'tscore': 78.0, 'sec': '指数型',
    },
]


def main() -> int:
    db_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), '..', 'fund.db')
    db_path = os.path.abspath(db_path)
    if not os.path.exists(db_path):
        print(f'[错误] 数据库不存在: {db_path}（请先启动一次后端生成）', file=sys.stderr)
        return 1

    conn = sqlite3.connect(db_path)
    try:
        # 确认表存在
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if 'funds' not in tables or 'nav_history' not in tables:
            print('[错误] fund.db 缺少 funds/nav_history 表，请先执行 migrate_db.py', file=sys.stderr)
            return 1

        cols = [r[1] for r in conn.execute('PRAGMA table_info(funds)').fetchall()]
        for f in FUNDS:
            code = f['code']
            # 清理该 code 的旧数据（幂等）
            conn.execute("DELETE FROM nav_history WHERE code=?", (code,))
            conn.execute("DELETE FROM funds WHERE code=?", (code,))

            # funds：仅写有意义的列，其余走默认
            insert_cols = ['code', 'name', 'nav', 'nav_date', 'd1', 'd2', 'd3', 'd5',
                           'd7', 'd10', 'dd7', 'dn7', 'ms', 'dd20', 'm1', 'm3', 'm6', 'y1',
                           'score', 'ad_score', 'earn_score', 'tscore', 'calmar',
                           'calmar_score', 'mdd', 'vol', 'down_vol', 'down_sharpe',
                           'pl', 'hi_cnt', 'dd_from_hi', 'is_etf', 'streak', 'sec',
                           'themes', 'verdict', 'suggest', 'updated_at']
            insert_cols = [c for c in insert_cols if c in cols]
            vals = {
                'code': code, 'name': f['name'], 'nav': f['nav'], 'nav_date': f['nav_date'],
                'd1': f['d1'], 'd2': 0.42, 'd3': 0.5, 'd5': 0.8, 'd7': 1.2, 'd10': 2.5,
                'dd7': 0.3, 'dn7': -1, 'ms': 3, 'dd20': 2.8, 'm1': 1.5, 'm3': 3.2,
                'm6': 6.8, 'y1': 9.5, 'score': f['score'], 'ad_score': f['ad_score'],
                'earn_score': f['earn_score'], 'tscore': f['tscore'], 'calmar': 1.1,
                'calmar_score': 0.83, 'mdd': 6.5, 'vol': 18.2, 'down_vol': 12.4,
                'down_sharpe': 0.9, 'pl': 60, 'hi_cnt': 3, 'dd_from_hi': 4.2,
                'is_etf': 1, 'streak': 0, 'sec': f['sec'], 'themes': '指数型',
                'verdict': '观望', 'suggest': '观望',
                'updated_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            }
            ph = ','.join(['?'] * len(insert_cols))
            conn.execute(
                f"INSERT INTO funds ({','.join(insert_cols)}) VALUES ({ph})",
                [vals[c] for c in insert_cols])

            # nav_history：回填近 30 个交易日
            base = datetime.date.fromisoformat(f['nav_date'])
            nav = f['nav']
            day_i = 0
            for i in range(60):
                d = base - datetime.timedelta(days=i)
                if d.weekday() >= 5:  # 跳过周末
                    continue
                day_i += 1
                nav = round(nav + (i % 3 - 1) * 0.01, 4)
                conn.execute(
                    "INSERT OR REPLACE INTO nav_history (code,date,ljjz,dwjz) VALUES (?,?,?,?)",
                    (code, d.isoformat(), nav, nav))
                if day_i >= 30:
                    break
            print(f'[OK] {code} {f["name"]}: funds + {min(day_i, 30)} 条净值')

        conn.commit()
        print('\n种子数据就绪。现在可运行:')
        print('  PLAYWRIGHT_BASE_URL=<后端地址> npx playwright test \\')
        print('    --config=playwright.chain.config.ts tests/e2e/*.fullchain.spec.ts')
        return 0
    finally:
        conn.close()


if __name__ == '__main__':
    sys.exit(main())
