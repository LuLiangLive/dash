"""
db.py —— SQLite 数据访问层

5 张表:
1. funds          基金基础信息(code 主键)
2. nav_history    净值历史(code, date 复合主键)
3. rank_snapshots 每日榜单快照(id 主键, date+panel+sub 唯一)
4. watchlist      用户自选(code 主键, position 排序)
5. task_logs      任务日志(id 自增)

线程安全:每个线程独立连接(sqlite3.check_same_thread=False + 线程局部连接)。
写操作用事务,配合 WAL 模式支持读并发。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Optional
from config import settings

# ---------------------------------------------------------------------------
# 路径与连接管理
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(settings.db_path)
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

_local = threading.local()


def get_conn() -> sqlite3.Connection:
    """返回当前线程的连接(惰性创建)。"""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=20000;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA cache_size=-64000;")  # 64MB内存缓存
        conn.execute("PRAGMA temp_store=MEMORY;")   # 临时表存内存
        conn.execute("PRAGMA mmap_size=268435456;") # 256MB内存映射
        _local.conn = conn
    return conn


def close_conn():
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS funds (
    code        TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    ftype       TEXT DEFAULT '',
    scale       REAL,
    est         TEXT,
    manager     TEXT,
    track       TEXT,
    sec         TEXT DEFAULT '其他',
    themes      TEXT DEFAULT '[]',      -- JSON 数组 [{'name':..., 'pct':...}]
    stocks      TEXT DEFAULT '[]',      -- JSON 数组
    score       INTEGER,                -- 综合分(收益60%+抗跌40%)
    ad_score    INTEGER,
    earn_score  INTEGER,
    nav         REAL,
    nav_date    TEXT,
    d3  REAL, d5  REAL, d7  REAL, d10  REAL,
    max_daily_drop_7d REAL, dn7 INTEGER, ms  TEXT, dd20 REAL,
    m1 REAL, m3 REAL, m6 REAL, y1 REAL,
    mdd REAL, mdd_days INTEGER, mdd_status TEXT,
    vol REAL, down_vol REAL, down_sharpe REAL, calmar REAL,
    pl REAL, hi_cnt INTEGER, dd_from_hi REAL,
    tscore INTEGER,              -- 综合趋势分(面板统一,与score对齐)
    calmar_score REAL,           -- Calmar评分(池内百分位归一化)
    verdict     TEXT,                   -- 持有结论
    suggest     TEXT DEFAULT '{}',      -- JSON {note, reasons, pos, period, risk}
    is_etf      INTEGER DEFAULT 0,
    streak      INTEGER,                -- 连涨天数
    updated_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_funds_sec ON funds(sec);
CREATE INDEX IF NOT EXISTS idx_funds_score ON funds(score DESC);
CREATE INDEX IF NOT EXISTS idx_funds_nav_date ON funds(nav_date);
-- v2.6.0 perf: 复合索引消除 sec 过滤 + score 排序的 TEMP B-TREE
CREATE INDEX IF NOT EXISTS idx_funds_sec_score ON funds(sec, score DESC);
-- v2.6.0 perf: ftype 过滤常用，单独索引
CREATE INDEX IF NOT EXISTS idx_funds_ftype ON funds(ftype);

CREATE TABLE IF NOT EXISTS nav_history (
    code  TEXT NOT NULL,
    date  TEXT NOT NULL,
    ljjz  REAL NOT NULL,
    dwjz  REAL,
    PRIMARY KEY (code, date)
);
CREATE INDEX IF NOT EXISTS idx_nav_code_date ON nav_history(code, date);
CREATE INDEX IF NOT EXISTS idx_nav_date ON nav_history(date);

CREATE TABLE IF NOT EXISTS rank_snapshots (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    date       TEXT NOT NULL,           -- 榜单交易日 YYYY-MM-DD
    panel      TEXT NOT NULL,           -- day | reco | warn (v0.96.1: rank/rot 已删除)
    sub        TEXT NOT NULL,           -- 当日/两日/三日/七日/综合 | 抗跌/自选/质量/ETF
    rank       INTEGER,                 -- 榜单内排名(1 起)
    code       TEXT NOT NULL,
    name       TEXT NOT NULL,
    meta       TEXT DEFAULT '{}',       -- JSON: ftype/sec/rtag/涨幅/评分/抗跌收益等渲染字段
    created_at TEXT,
    UNIQUE (date, panel, sub, rank)
);
CREATE INDEX IF NOT EXISTS idx_rank_date ON rank_snapshots(date, panel, sub);
CREATE INDEX IF NOT EXISTS idx_rank_code ON rank_snapshots(code);
-- v2.6.0 perf: 复合索引消除按 code 查历史时的 TEMP B-TREE 排序
CREATE INDEX IF NOT EXISTS idx_rank_code_date ON rank_snapshots(code, date DESC);
-- v2.6.0 perf: panel+date 组合查询（榜单面板按日期过滤）
CREATE INDEX IF NOT EXISTS idx_rank_panel_date ON rank_snapshots(panel, date);

CREATE TABLE IF NOT EXISTS watchlist (
    code      TEXT PRIMARY KEY,
    name      TEXT NOT NULL,
    position  INTEGER,
    added_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_watch_position ON watchlist(position);

CREATE TABLE IF NOT EXISTS task_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task        TEXT NOT NULL,
    status      TEXT NOT NULL,          -- running | ok | error
    message     TEXT DEFAULT '',
    started_at  TEXT,
    finished_at TEXT,
    duration_ms INTEGER
);
CREATE INDEX IF NOT EXISTS idx_task_date ON task_logs(started_at);

-- v0.50.x: 设置项 KV 存储(算法阈值、调度参数等持久化配置)
CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT
);

-- v2.8.0: 审计日志表（INSERT ONLY，不可篡改，记录关键操作）
CREATE TABLE IF NOT EXISTS audit_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT NOT NULL,           -- 操作时间 ISO8601
    action      TEXT NOT NULL,           -- 操作类型（watchlist_add/portfolio_remove等）
    operator    TEXT NOT NULL,           -- 操作人（user_id 或 system）
    detail      TEXT NOT NULL,           -- 操作描述（人类可读）
    ip          TEXT,                     -- 客户端IP
    request_id  TEXT,                     -- 请求ID（关联访问日志）
    extra       TEXT DEFAULT '{}'        -- 额外结构化数据（JSON）
);
CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_logs(timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_logs(action);
CREATE INDEX IF NOT EXISTS idx_audit_operator ON audit_logs(operator);
"""


def _ensure_column(table: str, col: str, ddl: str):
    """给已存在的表补充新列(幂等)。"""
    conn = get_conn()
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if col not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
        conn.commit()


def init_db():
    conn = get_conn()
    conn.executescript(SCHEMA)
    _ensure_column("funds", "streak", "INTEGER")
    _ensure_column("funds", "yindie", "TEXT")   # 阴跌/进行中深调 标签
    # v0.72.0: 统一字段标准 - 添加 d1/d2 单日/2日涨幅, reco 信号字段
    _ensure_column("funds", "d1", "REAL")
    _ensure_column("funds", "d2", "REAL")
    _ensure_column("funds", "reco", "TEXT")
    _ensure_column("funds", "reco_score", "INTEGER")
    _ensure_column("funds", "reco_days", "INTEGER")
    _ensure_column("funds", "prev_reco", "TEXT")
    _ensure_column("funds", "prev_reco_days", "INTEGER")
    # v2026-09: tscore/calmar_score 持久化(此前仅运行时计算,可能导致数据不一致)
    _ensure_column("funds", "tscore", "INTEGER")
    _ensure_column("funds", "calmar_score", "REAL")
    # v2026-09: 确保持仓相关表存在（migrate_db.py中定义，但数据库重建时需要先创建）
    conn.execute('''
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
    conn.execute('''
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
        )
    ''')
    # v2026-09: 持仓性能优化 - 添加investment_records和portfolio表索引
    conn.execute("CREATE INDEX IF NOT EXISTS idx_investment_records_code_type ON investment_records(code, type)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_investment_records_code_date ON investment_records(code, date DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_portfolio_total_amount ON portfolio(total_amount DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_portfolio_code ON portfolio(code)")
    conn.commit()
    # v2026-08: 清理过期 running 僵尸记录(服务器重启/崩溃等场景遗留)
    # 规则: status='running' 且 finished_at IS NULL → 视为异常中断, 标记为 stopped + 补 finished_at
    # 避免「最近更新」列表自动出现「进行中」任务
    # v2.9.52: 改进清理逻辑，记录更详细的异常信息（启动时间、持续时长、当前阶段）
    _stale = conn.execute(
        "SELECT id, task, started_at, message FROM task_logs WHERE status='running' AND finished_at IS NULL"
    ).fetchall()
    if _stale:
        _now = time.strftime("%Y-%m-%dT%H:%M:%S+08:00")
        for _row in _stale:
            _id = _row[0]
            _task_type = _row[1] or 'unknown'
            _started = _row[2] or ''
            _old_msg = _row[3] or ''
            # 计算持续时长
            _duration_str = ''
            try:
                if _started:
                    from datetime import datetime
                    _start_dt = datetime.fromisoformat(_started.replace('+08:00', ''))
                    _now_dt = datetime.now()
                    _duration = (_now_dt - _start_dt).total_seconds()
                    if _duration < 60:
                        _duration_str = f"{_duration:.0f}秒"
                    elif _duration < 3600:
                        _duration_str = f"{_duration/60:.1f}分钟"
                    else:
                        _duration_str = f"{_duration/3600:.1f}小时"
            except Exception:
                pass
            # 构建详细的异常消息
            _detail = f"进程异常中断(可能OOM/崩溃) · 启动={_started} · 持续={_duration_str}"
            if _old_msg and _old_msg != '任务异常中断,已自动清理':
                _new_msg = f"{_detail} · 原消息={_old_msg[:100]}"
            else:
                _new_msg = _detail
            conn.execute(
                "UPDATE task_logs SET status='stopped', finished_at=?, message=? WHERE id=?",
                (_now, _new_msg, _id),
            )
        conn.commit()
        print(f"[db] 已清理 {len(_stale)} 个僵尸任务记录")


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------

def _j(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def _uj(raw: str, default=None):
    if raw is None:
        return default if default is not None else {}
    try:
        return json.loads(raw)
    except Exception:
        return default if default is not None else {}


def _row_to_dict(row) -> dict:
    if row is None:
        return None
    return dict(row)


# ---------------------------------------------------------------------------
# funds CRUD
# ---------------------------------------------------------------------------

# v0.92.0: 这些字段在更新时采用 COALESCE 语义 —— 新值为空则保留库中已有值。
# 原因: 净值/指标链路(pipeline)与档案链路(f10 / pingzhongdata)是两套数据源。
#       pipeline 构造的 dict 里没有 scale/est/manager, f.get(k) 为 None,
#       原来的 `k=excluded.k` 无条件覆盖 → 一键更新会把已补齐的档案清空。
#       实测: 全量补齐后 est/manager 达 100%, 跑一次一键更新后掉回 4.3%。
# 同理 m1/m3/m6/y1 从残缺净值序列算不出来时(次新基金), 也不该抹掉接口取到的值。
_PRESERVE_ON_UPDATE = frozenset({
    "scale", "est", "manager", "track",   # 静态档案: 另一条链路维护
    "m1", "m3", "m6", "y1",               # 区间收益: 算不出时保留接口值
    "name",                               # 名称: 抓取失败时会回填空串, 不该抹掉已有名称
    # v2.11.4 A1: 保护评分字段, 阶段1 pipeline 不再无条件抹成 NULL,
    # 保留旧值直到阶段4 recompute_ad 精确重算覆盖
    "score", "ad_score", "earn_score", "yindie", "tscore", "calmar_score",
})


def upsert_fund(f: dict):
    """插入或更新一只基金。f 含全部 funds 字段(带 themes/stocks/suggest JSON 对象)。
    返回受影响行数(1=新增,0=更新)。"""
    conn = get_conn()
    fields = [
        "code", "name", "ftype", "scale", "est", "manager", "track",
        "sec", "themes", "stocks", "score", "ad_score", "earn_score",
        "nav", "nav_date", "d1", "d2", "d3", "d5", "d7", "d10", "max_daily_drop_7d", "dn7", "ms",
        "dd20", "m1", "m3", "m6", "y1", "mdd", "mdd_days", "mdd_status",
        "vol", "down_vol", "down_sharpe", "calmar", "pl", "hi_cnt",
        "dd_from_hi", "verdict", "suggest", "is_etf", "streak", "yindie", "updated_at",
        "reco", "reco_score", "reco_days", "prev_reco", "prev_reco_days",
        "tscore", "calmar_score",
    ]
    data = {k: f.get(k) for k in fields}
    data["themes"] = _j(data.get("themes") or [])
    data["stocks"] = _j(data.get("stocks") or [])
    # v2.11.4 Q11: 前端零引用, pipeline 不再写入 suggest; 但若调用方显式传入仍尊重
    if "suggest" in f:
        data["suggest"] = _j(data.get("suggest") or {})
    keys = list(data.keys())
    placeholders = ",".join(["?"] * len(keys))
    # NULLIF(excluded.k,'') 让空串也等价于 NULL: 抓取失败时 name 会回填空串,
    # 单纯 COALESCE 挡不住(SQLite 中空串不是 NULL), 会把已有名称抹成空。
    updates = ",".join([
        (f"{k}=COALESCE(NULLIF(excluded.{k}, ''), funds.{k})" if k in _PRESERVE_ON_UPDATE
         else f"{k}=excluded.{k}")
        for k in keys
    ])
    cur = conn.execute(
        f"INSERT INTO funds ({','.join(keys)}) VALUES ({placeholders}) "
        f"ON CONFLICT(code) DO UPDATE SET {updates}",
        [data[k] for k in keys],
    )
    conn.commit()
    return cur.rowcount


def get_fund(code: str) -> Optional[dict]:
    row = get_conn().execute("SELECT * FROM funds WHERE code=?", (code,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["themes"] = _uj(d.get("themes"))
    d["stocks"] = _uj(d.get("stocks"))
    d["suggest"] = _uj(d.get("suggest"))
    return d


def list_funds(sec: Optional[str] = None, limit: int = 200) -> list[dict]:
    q = "SELECT * FROM funds"
    args = []
    if sec:
        q += " WHERE sec=?"
        args.append(sec)
    q += " ORDER BY score DESC LIMIT ?"
    args.append(limit)
    rows = get_conn().execute(q, args).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["themes"] = _uj(d.get("themes"))
        d["suggest"] = _uj(d.get("suggest"))
        out.append(d)
    return out


def bulk_upsert_funds(funds) -> int:
    """批量插入或更新基金。v2.9.45: 重写为真正的批量 executemany + 单次 commit。

    与 upsert_fund 逻辑完全一致（相同字段列表、相同 COALESCE/NULLIF 规则），
    将 commit 次数从 N 降到 1，解决一键更新 database is locked 问题。
    批量执行失败时自动回退到逐只写入，保证数据不丢。
    """
    funds = list(funds)
    if not funds:
        return 0
    conn = get_conn()
    fields = [
        "code", "name", "ftype", "scale", "est", "manager", "track",
        "sec", "themes", "stocks", "score", "ad_score", "earn_score",
        "nav", "nav_date", "d1", "d2", "d3", "d5", "d7", "d10", "max_daily_drop_7d", "dn7", "ms",
        "dd20", "m1", "m3", "m6", "y1", "mdd", "mdd_days", "mdd_status",
        "vol", "down_vol", "down_sharpe", "calmar", "pl", "hi_cnt",
        "dd_from_hi", "verdict", "suggest", "is_etf", "streak", "yindie", "updated_at",
        "reco", "reco_score", "reco_days", "prev_reco", "prev_reco_days",
        "tscore", "calmar_score",
    ]
    placeholders = ",".join(["?"] * len(fields))
    updates = ",".join([
        (f"{k}=COALESCE(NULLIF(excluded.{k}, ''), funds.{k})" if k in _PRESERVE_ON_UPDATE
         else f"{k}=excluded.{k}")
        for k in fields
    ])
    sql = (
        f"INSERT INTO funds ({','.join(fields)}) VALUES ({placeholders}) "
        f"ON CONFLICT(code) DO UPDATE SET {updates}"
    )
    rows = []
    for f in funds:
        data = {k: f.get(k) for k in fields}
        data["themes"] = _j(data.get("themes") or [])
        data["stocks"] = _j(data.get("stocks") or [])
        # v2.11.4 Q11: pipeline 不再写入 suggest; 调用方显式传入时才序列化
        if "suggest" in f:
            data["suggest"] = _j(data.get("suggest") or {})
        rows.append([data[k] for k in fields])
    try:
        conn.executemany(sql, rows)
        conn.commit()
        return len(rows)
    except Exception:
        conn.rollback()
        ok = 0
        for row in rows:
            try:
                conn.execute(sql, row)
                ok += 1
            except Exception:
                conn.rollback()
        conn.commit()
        return ok


def upsert_nav(code: str, date: str, ljjz: float, dwjz: Optional[float] = None):
    conn = get_conn()
    conn.execute(
        "INSERT INTO nav_history (code,date,ljjz,dwjz) VALUES (?,?,?,?) "
        "ON CONFLICT(code,date) DO UPDATE SET ljjz=excluded.ljjz, dwjz=excluded.dwjz",
        (code, date, ljjz, dwjz),
    )


def bulk_upsert_navs(code: str, items: Iterable[dict], commit: bool = True):
    """items: [{date, ljjz, dwjz?}, ...]

    v0.46.0+: 末尾同步 funds.nav_date = MAX(nav_history.date WHERE code=?)，
    保证增量补抓后 funds.nav_date 与 nav_history 立即一致，避免下游用旧 nav_date 算指标。

    v2.5.0(perf): 新增 commit 参数。循环内批量调用时传 commit=False，
    循环结束后统一 db.get_conn().commit()，避免 6000 次逐基金 fsync。

    v1.1.3(数据安全): 冲突更新改为「只补不砸」——
        ljjz = COALESCE(NULLIF(excluded.ljjz, 0), nav_history.ljjz)
        dwjz = COALESCE(excluded.dwjz,                nav_history.dwjz)
    此前是无条件 `dwjz=excluded.dwjz`, 只要某次抓取/降级返回的行里 dwjz 缺失或为 0,
    就会把库里已有的好数据原地抹成 NULL。实测后果:017856 在补数完成后又被一轮增量
    更新写回 266 行 dwjz=NULL(且 ljjz 错位), d1 重新变错。
    注意:指数(sh/sz 开头)本就只写 ljjz, dwjz 恒为 NULL, 这条规则对它们无副作用。
    """
    conn = get_conn()
    rows = list(items)
    for it in rows:
        conn.execute(
            "INSERT INTO nav_history (code,date,ljjz,dwjz) VALUES (?,?,?,?) "
            "ON CONFLICT(code,date) DO UPDATE SET "
            "  ljjz=COALESCE(NULLIF(excluded.ljjz,0), nav_history.ljjz), "
            "  dwjz=COALESCE(excluded.dwjz, nav_history.dwjz)",
            (code, it.get("date"), float(it.get("ljjz") or 0), it.get("dwjz")),
        )
    # 同步 funds.nav_date: 仅在 nav_history 出现更新日期时刷新
    if rows:
        conn.execute(
            "UPDATE funds SET nav_date=(SELECT MAX(date) FROM nav_history WHERE code=?) "
            "WHERE code=? AND (nav_date IS NULL OR nav_date < "
            "(SELECT MAX(date) FROM nav_history WHERE code=?))",
            (code, code, code),
        )
    if commit:
        conn.commit()


def get_nav(code: str, limit: int = 300, asc: bool = False) -> list[dict]:
    """查询基金净值历史(取「最近」limit 条,再按 asc 决定返回顺序)。

    修复前为 `ORDER BY date ASC LIMIT ?`,对净值行数 > limit 的基金取到的是**最早**的
    一段历史(如 000217 取到 2013-08~2014-03,且 dwjz 全为 NULL),导致增量路径下这些
    基金的 m3/m6/y1/d1 等指标全部基于 10+ 年前的脏数据计算,甚至直接抛 TypeError。
    正确语义:LIMIT 应作用于「最近的 N 条」,asc 只控制最终返回顺序。
    """
    q = ("SELECT date, ljjz, dwjz FROM ("
         "  SELECT date, ljjz, dwjz FROM nav_history WHERE code=? ORDER BY date DESC LIMIT ?"
         ") ORDER BY date")
    q += " ASC" if asc else " DESC"
    rows = get_conn().execute(q, (code, limit)).fetchall()
    return [dict(r) for r in rows]


def get_nav_range(code: str, start: str, end: str) -> list[dict]:
    rows = get_conn().execute(
        "SELECT date, ljjz, dwjz FROM nav_history WHERE code=? AND date BETWEEN ? AND ? ORDER BY date",
        (code, start, end),
    ).fetchall()
    return [dict(r) for r in rows]


def latest_nav_date() -> Optional[str]:
    """v2.6.0 perf: 添加60秒内存缓存，MAX(date) 只需在数据更新后变化。"""
    try:
        from modules.common.cache_service import cache
        cached = cache.get("db:latest_nav_date")
        if cached is not None:
            return cached or None
    except Exception:
        pass
    row = get_conn().execute("SELECT MAX(date) AS d FROM nav_history").fetchone()
    result = row["d"] if row and row["d"] else None
    try:
        from modules.common.cache_service import cache
        cache.set("db:latest_nav_date", result or "", ttl=60)
    except Exception:
        pass
    return result


# ---------------------------------------------------------------------------
# rank_snapshots CRUD
# ---------------------------------------------------------------------------
def save_rank(date: str, panel: str, sub: str, rank: int, code: str, name: str, meta: dict, created_at: str):
    conn = get_conn()
    conn.execute(
        "INSERT INTO rank_snapshots (date,panel,sub,rank,code,name,meta,created_at) "
        "VALUES (?,?,?,?,?,?,?,?) "
        "ON CONFLICT(date,panel,sub,rank) DO UPDATE SET "
        "code=excluded.code, name=excluded.name, meta=excluded.meta",
        (date, panel, sub, rank, code, name, _j(meta), created_at),
    )


def get_ranks(date: str, panel: Optional[str] = None, sub: Optional[str] = None) -> list[dict]:
    q = "SELECT * FROM rank_snapshots WHERE date=?"
    args = [date]
    if panel:
        q += " AND panel=?"
        args.append(panel)
    if sub:
        q += " AND sub=?"
        args.append(sub)
    q += " ORDER BY panel, sub, rank"
    rows = get_conn().execute(q, args).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["meta"] = _uj(d.get("meta"))
        out.append(d)
    return out


def latest_rank_date() -> Optional[str]:
    """v2.6.0 perf: 添加60秒内存缓存，榜单日期仅在每日榜单计算后变化。"""
    try:
        from modules.common.cache_service import cache
        cached = cache.get("db:latest_rank_date")
        if cached is not None:
            return cached or None
    except Exception:
        pass
    row = get_conn().execute("SELECT MAX(date) AS d FROM rank_snapshots").fetchone()
    result = row["d"] if row and row["d"] else None
    try:
        from modules.common.cache_service import cache
        cache.set("db:latest_rank_date", result or "", ttl=60)
    except Exception:
        pass
    return result


def list_rank_dates(limit: int = 30) -> list[str]:
    rows = get_conn().execute(
        "SELECT DISTINCT date FROM rank_snapshots ORDER BY date DESC LIMIT ?", (limit,)
    ).fetchall()
    return [r["date"] for r in rows]


# ---------------------------------------------------------------------------
# watchlist CRUD
# ---------------------------------------------------------------------------

def list_watchlist() -> list[dict]:
    rows = get_conn().execute("SELECT * FROM watchlist ORDER BY position, added_at").fetchall()
    return [dict(r) for r in rows]


def get_watchlist_codes() -> list[str]:
    rows = get_conn().execute("SELECT code FROM watchlist ORDER BY position").fetchall()
    return [r["code"] for r in rows]


def add_watch(code: str, name: str) -> int:
    conn = get_conn()
    exists = conn.execute("SELECT code FROM watchlist WHERE code=?", (code,)).fetchone()
    if exists:
        return 0
    pos = conn.execute("SELECT COALESCE(MAX(position),0)+1 AS p FROM watchlist").fetchone()["p"]
    conn.execute(
        "INSERT INTO watchlist (code,name,position,added_at) VALUES (?,?,?,?)",
        (code, name, pos, time.strftime("%Y-%m-%dT%H:%M:%S+08:00")),
    )
    conn.commit()
    return 1


def remove_watch(code: str) -> int:
    conn = get_conn()
    cur = conn.execute("DELETE FROM watchlist WHERE code=?", (code,))
    conn.commit()
    return cur.rowcount


def set_watchlist(codes: list[str], name_of=None):
    """整体覆盖(前端 sync 用)。"""
    conn = get_conn()
    conn.execute("DELETE FROM watchlist")
    now = time.strftime("%Y-%m-%dT%H:%M:%S+08:00")
    for i, c in enumerate(codes, start=1):
        nm = name_of.get(c, c) if name_of else c
        conn.execute(
            "INSERT INTO watchlist (code,name,position,added_at) VALUES (?,?,?,?)",
            (c, nm, i, now),
        )
    conn.commit()


def clear_watchlist():
    conn = get_conn()
    conn.execute("DELETE FROM watchlist")
    conn.commit()


def list_watchlist_detail() -> dict[str, dict]:
    """批量查询自选基金详情(含 rtag),替代 index() 中的 N+1 查询。

    旧逻辑: 每只基金 4 次查询(get_fund + board_days + MAX(date) + rtag)。
    新逻辑: 2 次批量查询(funds + rtag),12 只自选从 48 次降到 2 次。

    返回 {code: fund_dict},结构与 index() 逐只拼装完全一致:
      - funds 全字段(themes/stocks/suggest 已解析为对象)
      - _v=2, yindie 透传
      - rtag: "当日#3 两日#5..." 格式(最多5个,无则 None)

    v0.76.2 perf: 消除自选页 N+1 查询。
    v2.11.4 C2/Q8: board_days 全链路下线, 删除近30天在榜天数查询与赋值。
    """
    conn = get_conn()
    # 1) 自选代码(按 position 排序,保持展示顺序)
    wl_rows = conn.execute(
        "SELECT code FROM watchlist ORDER BY position, added_at"
    ).fetchall()
    codes = [r["code"] for r in wl_rows]
    if not codes:
        return {}
    marks = ",".join("?" * len(codes))

    # 2) 批量 funds 基础信息(一次查询替代 N 次 get_fund)
    fund_map: dict[str, dict] = {}
    for r in conn.execute(
        f"SELECT * FROM funds WHERE code IN ({marks})", codes
    ).fetchall():
        d = dict(r)
        d["themes"] = _uj(d.get("themes"))
        d["stocks"] = _uj(d.get("stocks"))
        d["suggest"] = _uj(d.get("suggest"))
        d["_v"] = 2
        fund_map[d["code"]] = d

    # 3) 批量 rtag(最新上榜日的 day 面板排名标签)
    #    先取每只基金的最新上榜日,再关联取该日的 day 面板排名
    rtag_map: dict[str, str] = {}
    try:
        latest_rows = conn.execute(
            f"SELECT code, MAX(date) AS d FROM rank_snapshots "
            f"WHERE code IN ({marks}) GROUP BY code",
            codes,
        ).fetchall()
        # 按日期分组查询,避免每只基金一次查询
        date_groups: dict[str, list[str]] = {}
        for r in latest_rows:
            if r["d"]:
                date_groups.setdefault(r["d"], []).append(r["code"])
        for dt, dt_codes in date_groups.items():
            dt_marks = ",".join("?" * len(dt_codes))
            tag_rows = conn.execute(
                f"SELECT code, sub, rank FROM rank_snapshots "
                f"WHERE code IN ({dt_marks}) AND date=? AND panel='day' ORDER BY code, sub",
                dt_codes + [dt],
            ).fetchall()
            by_code: dict[str, list[str]] = {}
            for tr in tag_rows:
                if tr["sub"] and tr["rank"]:
                    by_code.setdefault(tr["code"], []).append(
                        f"{tr['sub']}#{tr['rank']}"
                    )
            for c, tags in by_code.items():
                rtag_map[c] = " ".join(tags[:5])
    except Exception:
        pass  # rtag 失败不阻断主流程

    # 4) 合并(按 watchlist 顺序,缺失的基金也保留 code 占位)
    result: dict[str, dict] = {}
    for c in codes:
        f = fund_map.get(c)
        if not f:
            continue
        f["rtag"] = rtag_map.get(c)
        result[c] = f
    return result


# ---------------------------------------------------------------------------
# task_logs CRUD
# ---------------------------------------------------------------------------

def log_start(task: str) -> int:
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO task_logs (task,status,started_at) VALUES (?,?,?)",
        (task, "running", time.strftime("%Y-%m-%dT%H:%M:%S+08:00")),
    )
    conn.commit()
    return cur.lastrowid


def log_finish(log_id: int, status: str, message: str, started_ts: float):
    conn = get_conn()
    conn.execute(
        "UPDATE task_logs SET status=?, message=?, finished_at=?, duration_ms=? WHERE id=?",
        (
            status,
            message[:2000],
            time.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
            int((time.time() - started_ts) * 1000),
            log_id,
        ),
    )
    conn.commit()


def recent_logs(limit: int = 20) -> list[dict]:
    rows = get_conn().execute(
        "SELECT * FROM task_logs ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 元信息:最近更新时间
# ---------------------------------------------------------------------------

def get_fund_update_stats() -> dict:
    """获取基金更新统计：基金库总数量、当天更新的基金数量

    v2.6.0 perf: 添加30秒内存缓存，避免每次页面加载都执行 COUNT(*) 全表扫描。
    数据仅在采集任务完成后变化，30秒TTL足够。
    """
    # v2.6.0: 内存缓存（LRU，30秒TTL）
    try:
        from modules.common.cache_service import cache
        cached = cache.get("db:fund_update_stats")
        if cached is not None:
            return cached
    except Exception:
        pass

    conn = get_conn()
    try:
        total = conn.execute("SELECT COUNT(*) FROM funds").fetchone()[0]
    except:
        total = 0

    try:
        latest_date = latest_nav_date()
        if latest_date:
            updated = conn.execute(
                "SELECT COUNT(DISTINCT code) FROM nav_history WHERE date=?",
                (latest_date,)
            ).fetchone()[0]
        else:
            updated = 0
    except:
        updated = 0

    result = {
        "total_funds": total,
        "updated_funds": updated,
        "update_ratio": f"{updated}/{total}" if total > 0 else "0/0"
    }

    try:
        from modules.common.cache_service import cache
        cache.set("db:fund_update_stats", result, ttl=30)
    except Exception:
        pass

    return result


def meta_updated() -> dict:
    """返回最近任务完成时间、榜单日期、净值日期、基金更新统计。
    v0.46.0: 加 latest_finished_at + latest_task_id, 让 _HTML_CACHE key 能跟踪所有任务变化
    v2026-08: 兼容 status='done'(fetch_manager)和 status='ok'(历史), 任一状态都视为有效。
    v2026-08-28: 字段名统一为 latest_rank_date/latest_nav_date，与前端保持一致。
    v2026-08-28: 添加基金更新统计字段。"""
    log = get_conn().execute(
        "SELECT id, started_at, finished_at, message FROM task_logs "
        "WHERE status IN ('ok','done') ORDER BY id DESC LIMIT 1"
    ).fetchone()
    
    fund_stats = get_fund_update_stats()
    
    return {
        "latest_task_at": log["started_at"] if log else None,
        "latest_finished_at": log["finished_at"] if log else None,
        "latest_task_id": log["id"] if log else None,
        "latest_rank_date": latest_rank_date(),
        "latest_nav_date": latest_nav_date(),
        "latest_task_msg": log["message"] if log else None,
        # 基金更新统计
        "total_funds": fund_stats["total_funds"],
        "updated_funds": fund_stats["updated_funds"],
        "update_ratio": fund_stats["update_ratio"],
        # 兼容旧字段名
        "rank_date": latest_rank_date(),
        "nav_date": latest_nav_date(),
    }


def invalidate_query_cache() -> None:
    """v2.6.0: 数据更新完成后调用，失效所有查询级缓存。

    在采集任务完成、榜单重算、净值更新后调用，确保下一次查询拿到最新数据。
    """
    try:
        from modules.common.cache_service import cache
        cache.delete("db:fund_update_stats")
        cache.delete("db:latest_nav_date")
        cache.delete("db:latest_rank_date")
    except Exception:
        pass


init_db()


# ---------------------------------------------------------------------------
# 异步数据库支持（v0.94.3: 为全链路异步化做准备）
# 现有同步函数继续工作，保持向后兼容。
# 未来可逐步将业务代码迁移到异步函数。
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 异步数据库连接池（v0.95.1 perf: 避免频繁创建/销毁连接）
# ---------------------------------------------------------------------------
import asyncio as _asyncio
_conn_pool: list = []
_conn_pool_lock = _asyncio.Lock()
_MAX_POOL_SIZE = 5  # SQLite 写操作串行化，连接池不宜过大


async def _create_conn():
    """创建一个新的异步数据库连接。"""
    import aiosqlite
    conn = await aiosqlite.connect(DB_PATH)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA synchronous=NORMAL")
    await conn.execute("PRAGMA cache_size=-64000")
    return conn


async def aget_conn():
    """从连接池获取异步数据库连接。"""
    global _conn_pool
    async with _conn_pool_lock:
        if _conn_pool:
            return _conn_pool.pop()
    # 连接池为空，创建新连接
    return await _create_conn()


async def arelease_conn(conn):
    """归还连接到连接池。"""
    global _conn_pool
    async with _conn_pool_lock:
        if len(_conn_pool) < _MAX_POOL_SIZE:
            _conn_pool.append(conn)
            return
    # 连接池已满，关闭连接
    await conn.close()


async def aquery(sql: str, params: tuple = ()) -> list[dict]:
    """异步查询，返回字典列表。"""
    conn = await aget_conn()
    try:
        async with conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]
    finally:
        await arelease_conn(conn)


async def aquery_one(sql: str, params: tuple = ()) -> dict | None:
    """异步查询单条记录。"""
    rows = await aquery(sql, params)
    return rows[0] if rows else None


async def aexecute(sql: str, params: tuple = ()) -> int:
    """异步执行（INSERT/UPDATE/DELETE），返回受影响行数。"""
    conn = await aget_conn()
    try:
        cur = await conn.execute(sql, params)
        await conn.commit()
        return cur.rowcount
    finally:
        await arelease_conn(conn)


async def aexecute_many(sql: str, params_list: list[tuple]) -> int:
    """异步批量执行，返回受影响行数。"""
    conn = await aget_conn()
    try:
        cur = await conn.executemany(sql, params_list)
        await conn.commit()
        return cur.rowcount
    finally:
        await arelease_conn(conn)


# ---------------------------------------------------------------------------
# 常用业务函数的异步版本（v0.94.4: 业务代码异步化迁移）
# ---------------------------------------------------------------------------

async def arecent_logs(limit: int = 20) -> list[dict]:
    """异步查询最近任务日志。"""
    rows = await aquery(
        "SELECT * FROM task_logs ORDER BY id DESC LIMIT ?",
        (limit,)
    )
    return rows


async def ameta_updated() -> dict:
    """异步查询最近更新时间。
    与同步版 meta_updated() 保持字段语义一致:
    - 任务状态兼容 'ok'(历史) 与 'done'(fetch_manager)，此前只查 'success' 导致永远查不到
    - update_ratio 统一为 "已更新/总数" 字符串，此前返回百分比数字与同步版不一致
    """
    latest_task = await aquery_one(
        "SELECT * FROM task_logs WHERE status IN ('ok','done') ORDER BY id DESC LIMIT 1"
    )
    latest_rank_date_row = await aquery_one("SELECT MAX(date) as d FROM rank_snapshots")
    latest_nav_date_row = await aquery_one("SELECT MAX(date) as d FROM nav_history")
    _latest_nav = (latest_nav_date_row or {}).get("d", "")
    # v2.2.3 修复：已更新统计逻辑从"nav_date不为空"改为"nav_date等于最新净值日期"
    # 之前只要有净值日期就算已更新，导致QDII等晚更新的基金也被算成100%已更新
    if _latest_nav:
        fund_stats = await aquery_one(
            "SELECT COUNT(*) as total, SUM(CASE WHEN nav_date = ? THEN 1 ELSE 0 END) as updated FROM funds",
            (_latest_nav,)
        )
    else:
        fund_stats = await aquery_one("SELECT COUNT(*) as total, 0 as updated FROM funds")

    _total = (fund_stats or {}).get("total", 0) or 0
    _updated = (fund_stats or {}).get("updated", 0) or 0

    return {
        "latest_task_at": (latest_task or {}).get("finished_at", ""),
        "latest_task_msg": (latest_task or {}).get("message", ""),
        "latest_rank_date": (latest_rank_date_row or {}).get("d", ""),
        "latest_nav_date": (latest_nav_date_row or {}).get("d", ""),
        "total_funds": _total,
        "updated_funds": _updated,
        # 与同步版 get_fund_update_stats() 格式一致: "已更新/总数"
        "update_ratio": f"{_updated}/{_total}" if _total > 0 else "0/0",
        "rank_date": (latest_rank_date_row or {}).get("d", ""),
        "nav_date": (latest_nav_date_row or {}).get("d", ""),
    }


async def alatest_rank_date() -> str | None:
    """异步查询最新榜单日期。"""
    row = await aquery_one("SELECT MAX(date) as d FROM rank_snapshots")
    return row["d"] if row else None


async def aget_ranks(date: str, panel: str | None = None, sub: str | None = None) -> list[dict]:
    """异步查询榜单快照。"""
    if panel and sub:
        return await aquery(
            "SELECT * FROM rank_snapshots WHERE date = ? AND panel = ? AND sub = ? ORDER BY rank",
            (date, panel, sub)
        )
    elif panel:
        return await aquery(
            "SELECT * FROM rank_snapshots WHERE date = ? AND panel = ? ORDER BY sub, rank",
            (date, panel)
        )
    else:
        return await aquery(
            "SELECT * FROM rank_snapshots WHERE date = ? ORDER BY panel, sub, rank",
            (date,)
        )


async def alist_rank_dates(limit: int = 30) -> list[str]:
    """异步查询历史榜单日期列表。"""
    rows = await aquery(
        "SELECT DISTINCT date FROM rank_snapshots ORDER BY date DESC LIMIT ?",
        (limit,)
    )
    return [r["date"] for r in rows]


async def alist_funds(sec: str | None = None, limit: int = 200) -> list[dict]:
    """异步查询基金列表（按综合分降序）。"""
    if sec:
        return await aquery(
            "SELECT * FROM funds WHERE sec = ? ORDER BY score DESC LIMIT ?",
            (sec, limit)
        )
    return await aquery(
        "SELECT * FROM funds ORDER BY score DESC LIMIT ?",
        (limit,)
    )


async def aget_fund(code: str) -> dict | None:
    """异步查询基金基础信息。"""
    return await aquery_one("SELECT * FROM funds WHERE code = ?", (code,))


async def aget_nav(code: str, limit: int = 300, asc: bool = False) -> list[dict]:
    """异步查询基金净值历史(取「最近」limit 条,再按 asc 决定返回顺序)。

    与同步版 get_nav 保持一致:先按日期倒序取最近 N 条,再按 asc 决定返回顺序,
    避免 asc=True 时取到最早一段历史脏数据。
    """
    order = "ASC" if asc else "DESC"
    return await aquery(
        f"SELECT * FROM ("
        f"  SELECT * FROM nav_history WHERE code = ? ORDER BY date DESC LIMIT ?"
        f") ORDER BY date {order}",
        (code, limit)
    )


async def alist_watchlist() -> list[dict]:
    """异步查询自选基金列表（按 position 排序）。"""
    return await aquery("SELECT * FROM watchlist ORDER BY position")


async def alist_watchlist_detail() -> dict[str, dict]:
    """异步查询自选基金详情（带基金信息），返回 {code: fund_dict}。"""
    import asyncio
    return await asyncio.to_thread(list_watchlist_detail)
