#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
核心数据导入模块
- v2.9.60: 部署包包含fund_core_data.db（净值数据+基金基本信息）
- 第一次启动时，如果nav_history表为空，自动从fund_core_data.db导入核心数据
- 到新平台后无需重新拉取大量数据，只需要走一遍一键更新即可
"""
import os
import sqlite3
import logging

logger = logging.getLogger(__name__)

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MAIN_DB = None  # 延迟初始化，从 config.settings.db_path 读取
CORE_DB = os.path.join(BASE, "fund_core_data.db")

# 需要导入的核心表
CORE_TABLES = ['nav_history', 'funds']

def _ensure_main_db():
    """延迟初始化 MAIN_DB，支持 DB_PATH 环境变量覆盖。"""
    global MAIN_DB
    if MAIN_DB is None:
        from config import settings
        MAIN_DB = str(settings.db_path)
    return MAIN_DB


def should_import_core_data():
    """
    判断是否需要导入核心数据
    - fund_core_data.db存在
    - fund.db存在
    - nav_history表为空（或者数据量很少，<1000条）
    """
    main_db = _ensure_main_db()
    if not os.path.exists(CORE_DB):
        return False, "fund_core_data.db不存在"

    if not os.path.exists(MAIN_DB):
        return False, "fund.db不存在"

    try:
        conn = sqlite3.connect(MAIN_DB)
        # 检查表是否存在
        table_exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='nav_history'"
        ).fetchone()
        if not table_exists:
            conn.close()
            return False, "nav_history表不存在（需要先执行迁移）"

        # 检查数据量
        count = conn.execute("SELECT COUNT(*) FROM nav_history").fetchone()[0]
        conn.close()

        if count < 1000:
            return True, f"nav_history表数据量很少（{count}条），需要导入核心数据"
        else:
            return False, f"nav_history表已有{count}条数据，无需导入"
    except Exception as e:
        return False, f"检查失败: {e}"

def import_core_data():
    """
    从fund_core_data.db导入核心数据到fund.db
    - nav_history: 净值数据
    - funds: 基金基本信息
    """
    print("=" * 60)
    print("核心数据导入")
    print("=" * 60)

    should_import, reason = should_import_core_data()
    if not should_import:
        print(f"  跳过导入: {reason}")
        return False

    print(f"  导入原因: {reason}")

    try:
        # 连接两个数据库
        src_conn = sqlite3.connect(CORE_DB)
        dst_conn = sqlite3.connect(MAIN_DB)

        total_imported = 0

        # v2.11.3 修复: 原实现按 `SELECT *` + `VALUES(...)` 列序盲插, 源/目标表列不一致时
        # (例如新版 seed 的 funds 为 53 列含遗留 max_dd/dd7, 全新运行时 funds 为 52 列用
        #  max_daily_drop_7d/mdd) 会列数错位/导入失败, 甚至先 DELETE 后失败导致目标表被清空。
        # 现改为: 按「目标表列名」对齐导入, 多出的源列忽略、缺失的源列置 NULL,
        # 并做语义别名映射(max_daily_drop_7d←dd7, mdd←max_dd 兜底), 兼容新旧 seed。
        _COL_ALIAS = {
            "max_daily_drop_7d": ("dd7", "max_daily_drop_7d"),
            "mdd": ("mdd", "max_dd"),
        }

        def _table_cols(conn, table):
            return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]

        for table in CORE_TABLES:
            print(f"\n  导入表: {table} ...")

            # 检查源表是否存在
            src_exists = src_conn.execute(
                f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table}'"
            ).fetchone()
            if not src_exists:
                print(f"    ⚠️  源表 {table} 不存在，跳过")
                continue

            src_cols = _table_cols(src_conn, table)
            dst_cols = _table_cols(dst_conn, table)
            if not dst_cols:
                print(f"    ⚠️  目标表 {table} 无列，跳过")
                continue
            # 目标列 ← 源列映射 (None 表示源无此列 → 置 NULL)
            col_map = []
            for _t in dst_cols:
                if _t in src_cols:
                    col_map.append(src_cols.index(_t))
                    continue
                _alias_i = None
                for _cand in _COL_ALIAS.get(_t, ()):
                    if _cand in src_cols:
                        _alias_i = src_cols.index(_cand)
                        break
                col_map.append(_alias_i)
            print(f"    源列 {len(src_cols)} → 目标列 {len(dst_cols)}（按列名对齐; "
                  f"别名映射 {sum(1 for _m in col_map if _m is None) and '含mdd/dd7语义' or '无'}）")

            # 获取源表数据量
            src_count = src_conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            print(f"    源数据量: {src_count:,}条")

            # 清空目标表（避免重复）
            dst_conn.execute(f"DELETE FROM {table}")
            dst_conn.commit()

            # 分批导入
            batch_size = 10000
            imported = 0
            _insert_sql = f"INSERT INTO {table} ({','.join(dst_cols)}) VALUES ({','.join('?' * len(dst_cols))})"
            for offset in range(0, src_count, batch_size):
                rows = src_conn.execute(
                    f"SELECT * FROM {table} LIMIT ? OFFSET ?",
                    (batch_size, offset)
                ).fetchall()
                if rows:
                    mapped_rows = [
                        tuple((row[_i] if _i is not None else None) for _i in col_map)
                        for row in rows
                    ]
                    dst_conn.executemany(_insert_sql, mapped_rows)
                    imported += len(rows)
                    if imported % 100000 == 0:
                        print(f"    已导入: {imported:,}/{src_count:,}条 ({imported/src_count*100:.1f}%)")

            dst_conn.commit()
            total_imported += imported
            print(f"    ✓ 导入完成: {imported:,}条")

        src_conn.close()
        dst_conn.close()

        print("\n" + "=" * 60)
        print(f"✓ 核心数据导入完成！共导入 {total_imported:,} 条数据")
        print("=" * 60)
        print("""
下一步：
1. 核心数据已导入（净值数据+基金基本信息）
2. 只需要走一遍「一键更新」，就会自动：
   - 重算排名快照
   - 重新拉取市场新闻
   - 更新最新净值数据
   - 重算所有指标
""")

        return True

    except Exception as e:
        print(f"\n  ✗ 导入失败: {e}")
        logger.error(f"核心数据导入失败: {e}", exc_info=True)
        return False

if __name__ == '__main__':
    import_core_data()
