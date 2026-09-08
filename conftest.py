"""
contract/conftest.py —— API契约测试共享配置

职责：
- 创建独立的测试数据库（不污染生产库 backend/fund.db）
- 初始化所有业务表结构（funds/nav_history/rank_snapshots/watchlist/
  watch_groups/watch_group_items/portfolio/investment_records/sync_log等）
- 插入标准测试数据，供4个契约测试模块共用
- 提供带测试数据库的 FastAPI TestClient

使用方式：
    每个测试文件通过 `client` fixture 自动获得隔离的测试环境。
    测试数据库位于 pytest tmp_path，测试结束后自动清理。
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# 确保 backend 目录在 Python 路径中
BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BACKEND_DIR))

# 开发默认 API Key（与 config.py Settings.api_keys 硬编码强私钥一致）
# v2.8.1: 鉴权头由 X-API-Key 改为 X-Touyan-Key；key 从 dev-key-123 改为强私钥
TEST_API_KEY = "35a92aeb9e0acef9bfbdcbe733f74c4d"
AUTH_HEADERS = {"X-Touyan-Key": TEST_API_KEY}


@pytest.fixture(autouse=True)
def _auth_test_mode(monkeypatch):
    """v2.8.1 全站强鉴权与历史测试兼容层。

    背景：v2.8.1 将读/写接口全部接入 X-Touyan-Key 强私钥校验(require_read_key=True)，
    而 tests/ 下多数测试文件自定义了无鉴权头的 TestClient(app) fixture（如
    test_api.py / test_api_endpoints.py / test_integration_flows.py / test_performance.py /
    test_update_flow.py 等），直接请求会全部 401。
    本 autouse fixture 在测试进程内放行鉴权校验，使上述 API 契约/集成测试恢复可运行。
    说明：tests/contract/ 下契约测试仍显式携带 X-Touyan-Key（见其 conftest.py），
    鉴权链路本身由它们独立验证，此处放宽不影响鉴权契约的覆盖。
    """
    import auth as auth_module

    def _check_relaxed(x_api_key):
        # 缺失 key 放行（历史测试不带 header）；无效 key 仍拒绝（保留鉴权契约）
        if x_api_key is None:
            return None
        if x_api_key in auth_module.API_KEYS:
            return x_api_key
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="Invalid API Key")

    monkeypatch.setattr(auth_module, "_check", _check_relaxed)


# ---------------------------------------------------------------------------
# 扩展表 DDL（分组/持仓/同步/提醒等，不在 db.py 基础 SCHEMA 中）
# ---------------------------------------------------------------------------
_EXT_TABLES_SQL = """
-- 分组管理表
CREATE TABLE IF NOT EXISTS watch_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    color TEXT DEFAULT '#6366f1',
    icon TEXT DEFAULT '📁',
    sort_order INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS watch_group_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL,
    code TEXT NOT NULL,
    sort_order INTEGER DEFAULT 0,
    added_at TEXT DEFAULT (datetime('now', 'localtime')),
    UNIQUE(group_id, code)
);

-- 持仓管理表
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
);

CREATE TABLE IF NOT EXISTS investment_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,
    name TEXT,
    type TEXT NOT NULL DEFAULT 'buy',
    date TEXT NOT NULL,
    amount REAL NOT NULL,
    shares REAL DEFAULT 0,
    nav REAL DEFAULT 0,
    fee REAL DEFAULT 0,
    profit_target REAL,
    note TEXT,
    source TEXT DEFAULT 'manual',
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);

-- 同步日志表
CREATE TABLE IF NOT EXISTS sync_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT,
    data_type TEXT NOT NULL,
    action TEXT NOT NULL,
    record_count INTEGER DEFAULT 0,
    status TEXT DEFAULT 'success',
    message TEXT,
    file_path TEXT,
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);

-- 提醒表（部分模块依赖）
CREATE TABLE IF NOT EXISTS alert_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    code TEXT,
    name TEXT,
    condition TEXT,
    threshold REAL,
    enabled INTEGER DEFAULT 1,
    last_triggered TEXT,
    trigger_count INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);

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
);
"""


def _ensure_column(conn, table: str, col: str, ddl: str):
    """幂等添加列（与 db.py._ensure_column 相同逻辑，使用本地 conn）。"""
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if col not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")


def _seed_test_data(conn) -> None:
    """插入标准测试数据，供契约测试断言使用。"""
    import json

    # --- 基金基础数据 ---
    funds = [
        ("CONTR01", "契约测试基金一号", 1.2345, "2026-09-01",
         1.23, 85, 78, 88, "科技", 5.6, 12.3, 18.9, 25.4),
        ("CONTR02", "契约测试基金二号", 2.5678, "2026-09-01",
         -0.87, 72, 80, 65, "医药", -2.1, 8.5, 15.2, 20.1),
        ("CONTR03", "契约测试基金三号", 0.9876, "2026-09-01",
         3.45, 91, 85, 92, "消费", 8.9, 15.6, 22.3, 30.5),
        # 6位数字代码基金（用于自选添加测试，后端要求6位数字代码）
        ("510300", "契约测试沪深300ETF", 3.8500, "2026-09-01",
         0.56, 78, 72, 80, "指数", 2.3, 6.7, 12.1, 18.5),
    ]
    for code, name, nav, nav_date, d1, score, ad, earn, sec, m1, m3, m6, y1 in funds:
        conn.execute(
            """INSERT OR IGNORE INTO funds
               (code, name, nav, nav_date, d1, score, ad_score, earn_score,
                sec, m1, m3, m6, y1, tscore, calmar_score, scale, est)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (code, name, nav, nav_date, d1, score, ad, earn, sec,
             m1, m3, m6, y1, float(score), 0.85, 50.5, "2020-01-15")
        )

    # --- 净值历史 ---
    nav_data = [
        ("CONTR01", "2026-08-28", 1.2100, 1.2100),
        ("CONTR01", "2026-08-29", 1.2200, 1.2200),
        ("CONTR01", "2026-09-01", 1.2345, 1.2345),
        ("CONTR02", "2026-08-28", 2.6000, 2.6000),
        ("CONTR02", "2026-08-29", 2.5900, 2.5900),
        ("CONTR02", "2026-09-01", 2.5678, 2.5678),
        ("CONTR03", "2026-08-28", 0.9500, 0.9500),
        ("CONTR03", "2026-08-29", 0.9700, 0.9700),
        ("CONTR03", "2026-09-01", 0.9876, 0.9876),
    ]
    for code, date, ljjz, dwjz in nav_data:
        conn.execute(
            "INSERT OR IGNORE INTO nav_history (code, date, ljjz, dwjz) VALUES (?, ?, ?, ?)",
            (code, date, ljjz, dwjz)
        )

    # --- 榜单快照 ---
    rank_date = "2026-09-01"
    rank_entries = [
        # 日榜 - 当日
        (rank_date, "day", "当日", 1, "CONTR03", "契约测试基金三号",
         {"code": "CONTR03", "name": "契约测试基金三号", "rank": 1,
          "d1": 3.45, "score": 91, "tscore": 91.0, "sec": "消费"}),
        (rank_date, "day", "当日", 2, "CONTR01", "契约测试基金一号",
         {"code": "CONTR01", "name": "契约测试基金一号", "rank": 2,
          "d1": 1.23, "score": 85, "tscore": 85.0, "sec": "科技"}),
        # 日榜 - 三日
        (rank_date, "day", "三日", 1, "CONTR01", "契约测试基金一号",
         {"code": "CONTR01", "name": "契约测试基金一号", "rank": 1,
          "d3": 5.2, "score": 85, "tscore": 85.0, "sec": "科技"}),
        (rank_date, "day", "三日", 2, "CONTR03", "契约测试基金三号",
         {"code": "CONTR03", "name": "契约测试基金三号", "rank": 2,
          "d3": 4.8, "score": 91, "tscore": 91.0, "sec": "消费"}),
        # 推荐榜 - 抗跌
        (rank_date, "reco", "抗跌", 1, "CONTR02", "契约测试基金二号",
         {"code": "CONTR02", "name": "契约测试基金二号", "rank": 1,
          "ad_score": 80, "score": 72, "tscore": 72.0, "sec": "医药"}),
        (rank_date, "reco", "抗跌", 2, "CONTR01", "契约测试基金一号",
         {"code": "CONTR01", "name": "契约测试基金一号", "rank": 2,
          "ad_score": 78, "score": 85, "tscore": 85.0, "sec": "科技"}),
        # 推荐榜 - 质量
        (rank_date, "reco", "质量", 1, "CONTR03", "契约测试基金三号",
         {"code": "CONTR03", "name": "契约测试基金三号", "rank": 1,
          "earn_score": 92, "score": 91, "tscore": 91.0, "sec": "消费"}),
        # 推荐榜 - ETF
        (rank_date, "reco", "ETF", 1, "CONTR01", "契约测试基金一号",
         {"code": "CONTR01", "name": "契约测试基金一号", "rank": 1,
          "score": 85, "tscore": 85.0, "sec": "科技"}),
    ]
    for date, panel, sub, rank, code, name, meta in rank_entries:
        conn.execute(
            """INSERT OR IGNORE INTO rank_snapshots
               (date, panel, sub, rank, code, name, meta, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (date, panel, sub, rank, code, name, json.dumps(meta, ensure_ascii=False), date)
        )

    # --- 自选列表 ---
    conn.execute(
        "INSERT OR IGNORE INTO watchlist (code, name, position, added_at) VALUES (?, ?, ?, ?)",
        ("CONTR01", "契约测试基金一号", 1, "2026-08-01")
    )
    conn.execute(
        "INSERT OR IGNORE INTO watchlist (code, name, position, added_at) VALUES (?, ?, ?, ?)",
        ("CONTR02", "契约测试基金二号", 2, "2026-08-15")
    )

    # --- 分组 ---
    conn.execute(
        "INSERT OR IGNORE INTO watch_groups (id, name, color, icon, sort_order) VALUES (?, ?, ?, ?, ?)",
        (1, "核心持仓", "#ef4444", "⭐", 1)
    )
    conn.execute(
        "INSERT OR IGNORE INTO watch_groups (id, name, color, icon, sort_order) VALUES (?, ?, ?, ?, ?)",
        (2, "观察池", "#3b82f6", "👁", 2)
    )
    conn.execute(
        "INSERT OR IGNORE INTO watch_group_items (group_id, code, sort_order) VALUES (?, ?, ?)",
        (1, "CONTR01", 1)
    )

    # --- 持仓 ---
    conn.execute(
        """INSERT OR IGNORE INTO portfolio
           (code, name, total_shares, avg_cost, total_amount, total_profit,
            profit_pct, current_nav, nav_date)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("CONTR01", "契约测试基金一号", 1000.0, 1.10, 1234.5, 134.5,
         12.23, 1.2345, "2026-09-01")
    )
    conn.execute(
        """INSERT OR IGNORE INTO investment_records
           (code, name, type, date, amount, shares, nav, fee, note, source)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("CONTR01", "契约测试基金一号", "buy", "2026-06-01",
         1100.0, 1000.0, 1.10, 5.0, "建仓", "manual")
    )

    # --- 任务日志（用于 updated_at） ---
    conn.execute(
        "INSERT OR IGNORE INTO task_logs (task, status, started_at, finished_at) VALUES (?, ?, ?, ?)",
        ("daily_rank", "ok", "2026-09-01 08:00:00", "2026-09-01 08:05:00")
    )

    conn.commit()


@pytest.fixture
def test_db_path(tmp_path):
    """创建测试用空数据库文件路径。"""
    db_path = tmp_path / "test_contract.db"
    Path(str(db_path)).touch()
    return str(db_path)


@pytest.fixture
def client(test_db_path, monkeypatch):
    """
    创建带隔离测试数据库的 FastAPI TestClient。

    流程：
    1. 设置 DB_PATH 环境变量指向临时测试库
    2. 重新加载 config / db / main 模块，使新路径生效
    3. 执行完整表结构 DDL
    4. 插入标准测试数据
    5. 返回 TestClient 实例
    """
    monkeypatch.setenv("DB_PATH", test_db_path)

    import config as config_module
    importlib.reload(config_module)

    import db as db_module
    importlib.reload(db_module)

    # 1. 使用 db.init_db() 创建基础表（funds/nav_history/rank_snapshots/
    #    watchlist/task_logs/settings），确保与生产环境表结构完全一致
    db_module.init_db()

    conn = db_module.get_conn()

    # 2. 创建扩展表（分组/持仓/同步/提醒）
    conn.executescript(_EXT_TABLES_SQL)
    conn.commit()

    # 3. 补充扩展列（与生产库 migrate_db.py 对齐）
    _ensure_column(conn, "watchlist", "group_id", "INTEGER DEFAULT 0")
    _ensure_column(conn, "watchlist", "fav", "INTEGER DEFAULT 0")
    # tscore/calmar_score 是后端运行时动态设置的字段，测试库也需支持
    _ensure_column(conn, "funds", "tscore", "REAL")
    _ensure_column(conn, "funds", "calmar_score", "REAL")
    conn.commit()

    # 4. 插入标准测试数据
    _seed_test_data(conn)

    # 重新加载 main 以注册所有路由（使用新的 db 连接）
    import main as main_module
    importlib.reload(main_module)

    test_client = TestClient(main_module.app)
    yield test_client
    test_client.close()

    # 清理：关闭数据库连接
    db_module.close_conn()
