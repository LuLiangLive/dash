"""
数据库表结构迁移脚本
新增功能：分组管理、持仓盈亏、提醒通知、板块轮动、数据同步
"""
import sqlite3
import os
import sys

# v2.9.4 部署实抓修复：migrate() 大量 print 含 emoji（⏭✅），在未开 UTF-8 代码页的
# Windows 控制台（GBK）会抛 UnicodeEncodeError，导致启动自动迁移中途失败、
# 监控表等第 13/14 步全部落空。仅替换不可编码字符、不改字节编码，零副作用。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:
        pass

# 迁移目标与运行库保持一致：优先读取 config.settings.db_path（支持发布平台 DB_PATH 环境变量），
# 否则回退到本目录 fund.db。修复：此前硬编码路径，云库若用 DB_PATH 指向他处则迁移落空（发布后业务表缺失 500）。
try:
    from config import settings
    DB_PATH = str(settings.db_path)
except Exception:
    DB_PATH = os.path.join(os.path.dirname(__file__), 'fund.db')

def migrate():
    _dir = os.path.dirname(DB_PATH)
    if _dir:
        os.makedirs(_dir, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    print('=== 开始数据库迁移 ===\n')

    # 1. 自选基金分组表
    print('1. 创建 watch_groups 表（自选分组）')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS watch_groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            color TEXT DEFAULT '#6366f1',
            icon TEXT DEFAULT '📁',
            sort_order INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    ''')

    # 2. 自选分组成员表
    print('2. 创建 watch_group_items 表（分组成员）')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS watch_group_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL,
            code TEXT NOT NULL,
            sort_order INTEGER DEFAULT 0,
            added_at TEXT DEFAULT (datetime('now', 'localtime')),
            FOREIGN KEY (group_id) REFERENCES watch_groups(id) ON DELETE CASCADE,
            UNIQUE(group_id, code)
        )
    ''')

    # 3. 持仓表（汇总）
    print('3. 创建 portfolio 表（持仓汇总）')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS portfolio (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL UNIQUE,
            name TEXT,
            total_shares REAL DEFAULT 0,
            avg_cost REAL DEFAULT 0,
            total_amount REAL DEFAULT 0,
            total_profit REAL DEFAULT 0,
            profit_pct REAL DEFAULT 0,
            current_nav REAL DEFAULT 0,
            nav_date TEXT,
            created_at TEXT DEFAULT (datetime('now', 'localtime')),
            updated_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    ''')

    # 4. 投资记录表（每笔买卖）
    print('4. 创建 investment_records 表（投资记录）')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS investment_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL,
            name TEXT,
            type TEXT NOT NULL DEFAULT 'buy',  -- buy/sell/dividend
            date TEXT NOT NULL,
            amount REAL NOT NULL,  -- 金额
            shares REAL DEFAULT 0,  -- 份额
            nav REAL DEFAULT 0,  -- 成交净值
            fee REAL DEFAULT 0,  -- 手续费
            profit_target REAL,  -- 盈利目标百分比（单笔提醒）
            note TEXT,
            source TEXT DEFAULT 'manual',  -- manual/excel/screenshot
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    ''')

    # 5. 提醒规则表
    print('5. 创建 alert_rules 表（提醒规则）')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS alert_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,  -- price/score/signal/profit
            code TEXT,  -- 为空表示全局规则
            name TEXT,
            condition TEXT,  -- above/below/change/upgrade
            threshold REAL,
            enabled INTEGER DEFAULT 1,
            last_triggered TEXT,
            trigger_count INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    ''')

    # 6. 提醒历史表
    print('6. 创建 alert_history 表（提醒历史）')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS alert_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_id INTEGER,
            type TEXT,
            code TEXT,
            name TEXT,
            message TEXT NOT NULL,
            value REAL,
            threshold REAL,
            triggered_at TEXT DEFAULT (datetime('now', 'localtime')),
            read INTEGER DEFAULT 0
        )
    ''')

    # 7. 板块数据表
    print('7. 创建 sector_data 表（板块数据）')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS sector_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            sector_code TEXT,
            sector_name TEXT NOT NULL,
            change_pct REAL DEFAULT 0,
            fund_flow REAL DEFAULT 0,  -- 资金流向（亿）
            etf_count INTEGER DEFAULT 0,
            avg_change REAL DEFAULT 0,
            rank INTEGER,
            created_at TEXT DEFAULT (datetime('now', 'localtime')),
            UNIQUE(date, sector_name)
        )
    ''')

    # 8. 板块成分股表
    print('8. 创建 sector_stocks 表（板块成分股）')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS sector_stocks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sector_name TEXT NOT NULL,
            stock_code TEXT NOT NULL,
            stock_name TEXT,
            weight REAL DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now', 'localtime')),
            UNIQUE(sector_name, stock_code)
        )
    ''')

    # 9. 数据同步日志表
    print('9. 创建 sync_log 表（同步日志）')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS sync_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT,
            data_type TEXT NOT NULL,  -- watchlist/portfolio/settings/groups
            action TEXT NOT NULL,  -- export/import/sync
            record_count INTEGER DEFAULT 0,
            status TEXT DEFAULT 'success',
            message TEXT,
            file_path TEXT,
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    ''')

    # 10. 创建设备信息表（用于多设备同步）
    print('10. 创建 device_info 表（设备信息）')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS device_info (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT UNIQUE,
            device_name TEXT,
            last_sync TEXT,
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    ''')

    # 为watchlist表增加group_id字段（兼容旧数据）
    print('\n11. 为 watchlist 表增加 group_id 字段')
    try:
        cursor.execute('ALTER TABLE watchlist ADD COLUMN group_id INTEGER DEFAULT 0')
        print('   ✅ 已增加 group_id 字段')
    except sqlite3.OperationalError:
        print('   ⏭️  group_id 字段已存在')

    # 为watchlist表增加fav字段（收藏标记）
    print('12. 为 watchlist 表增加 fav 字段')
    try:
        cursor.execute('ALTER TABLE watchlist ADD COLUMN fav INTEGER DEFAULT 0')
        print('   ✅ 已增加 fav 字段')
    except sqlite3.OperationalError:
        print('   ⏭️  fav 字段已存在')

    # funds.mdd 列兼容（v2.11.2）：旧库 funds 表只有 max_dd 列(2.10.0 命名统一前)，
    # recompute_ad/rank_full 等按 mdd 读写，老库启动即崩。幂等补列并从 max_dd 回填。
    print('\n12.5 为 funds 表兼容 mdd 列（max_dd→mdd）')
    try:
        _fcols = [r[1] for r in cursor.execute('PRAGMA table_info(funds)').fetchall()]
        if 'mdd' not in _fcols and 'max_dd' in _fcols:
            cursor.execute('ALTER TABLE funds ADD COLUMN mdd REAL')
            cursor.execute('UPDATE funds SET mdd = max_dd WHERE max_dd IS NOT NULL')
            print('   ✅ 已为 funds 增加 mdd 列并从 max_dd 回填')
        elif 'mdd' in _fcols:
            print('   ⏭️  funds.mdd 已存在')
        else:
            print('   ⏭️  funds 无 max_dd/mdd 列（新库随建表创建）')
    except sqlite3.OperationalError as _e:
        print(f'   ⚠️ funds.mdd 兼容列处理失败: {_e}')

    # 创建索引
    print('\n13. 创建索引')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_investment_code ON investment_records(code)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_investment_date ON investment_records(date)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_alert_type ON alert_rules(type)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_alert_code ON alert_rules(code)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_alert_history_read ON alert_history(read)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_sector_date ON sector_data(date)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_portfolio_code ON portfolio(code)')
    print('   ✅ 索引创建完成')

    # 14. 初始化监控相关表（monitor_errors / data_update_logs / system_metrics / monitor_alerts）
    #     v2.9.2 P0 修复：db_init 模块此前无任何导入者，监控表从未随启动创建，
    #     导致 fetch_manager 接线 log_data_update 后仍会 no such table。
    print('\n14. 初始化监控相关表')
    try:
        from modules.monitor.db_init import init_monitor_tables
        if init_monitor_tables():
            print('   ✅ 监控表初始化完成')
    except Exception as _e:
        print(f'   ⚠️ 监控表初始化失败（不影响主流程）: {_e}')

    conn.commit()

    # 验证表创建
    print('\n=== 迁移完成，当前数据库表 ===')
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = cursor.fetchall()
    for t in tables:
        cursor.execute(f'SELECT COUNT(*) FROM {t[0]}')
        count = cursor.fetchone()[0]
        print(f'  - {t[0]} ({count} 条记录)')

    conn.close()
    print('\n✅ 数据库迁移成功完成！')

if __name__ == '__main__':
    migrate()
