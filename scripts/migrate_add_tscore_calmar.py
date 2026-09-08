# -*- coding: utf-8 -*-
"""
数据库迁移脚本：为 funds 表添加 tscore 和 calmar_score 列

可重复执行（幂等）：使用 PRAGMA table_info 检查列是否存在，不存在才添加。
默认值：tscore INTEGER DEFAULT NULL, calmar_score REAL DEFAULT NULL

执行方式：
    cd backend
    python scripts/migrate_add_tscore_calmar.py
"""
import sqlite3
import os
import sys

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fund.db")


def migrate():
    if not os.path.exists(DB_PATH):
        print(f"[ERROR] 数据库文件不存在: {DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    print("=" * 60)
    print("迁移：为 funds 表添加 tscore / calmar_score 列")
    print("=" * 60)
    print(f"数据库: {DB_PATH}")
    print()

    # 检查当前列
    cols = [r[1] for r in cursor.execute("PRAGMA table_info(funds)").fetchall()]
    print(f"当前 funds 表列数: {len(cols)}")
    print(f"  tscore 存在: {'tscore' in cols}")
    print(f"  calmar_score 存在: {'calmar_score' in cols}")
    print()

    added = []

    # 添加 tscore 列（INTEGER，默认 NULL）
    if "tscore" not in cols:
        cursor.execute("ALTER TABLE funds ADD COLUMN tscore INTEGER")
        added.append("tscore")
        print("  [+] 已添加 tscore INTEGER")
    else:
        print("  [=] tscore 已存在，跳过")

    # 添加 calmar_score 列（REAL，默认 NULL）
    if "calmar_score" not in cols:
        cursor.execute("ALTER TABLE funds ADD COLUMN calmar_score REAL")
        added.append("calmar_score")
        print("  [+] 已添加 calmar_score REAL")
    else:
        print("  [=] calmar_score 已存在，跳过")

    conn.commit()

    # 验证
    cols_after = [r[1] for r in cursor.execute("PRAGMA table_info(funds)").fetchall()]
    print()
    print(f"迁移后 funds 表列数: {len(cols_after)}")
    print(f"  tscore 存在: {'tscore' in cols_after}")
    print(f"  calmar_score 存在: {'calmar_score' in cols_after}")

    # 统计已有数据
    total = cursor.execute("SELECT COUNT(*) FROM funds").fetchone()[0]
    has_tscore = cursor.execute("SELECT COUNT(*) FROM funds WHERE tscore IS NOT NULL").fetchone()[0]
    has_calmar = cursor.execute("SELECT COUNT(*) FROM funds WHERE calmar_score IS NOT NULL").fetchone()[0]
    print()
    print(f"基金总数: {total}")
    print(f"  已有 tscore: {has_tscore}")
    print(f"  已有 calmar_score: {has_calmar}")

    conn.close()
    print()
    print("=" * 60)
    if added:
        print(f"迁移完成：新增列 {', '.join(added)}")
    else:
        print("迁移完成：无需新增列（已存在）")
    print("=" * 60)


if __name__ == "__main__":
    migrate()
