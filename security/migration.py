"""
security/migration.py —— 敏感数据迁移模块

负责将数据库中已有的明文敏感数据迁移为加密存储。
迁移策略：
1. 检测未加密的数据（不以 "enc::v1::" 开头）
2. 对敏感字段进行加密
3. 记录迁移日志
4. 支持回滚（解密回明文）

需要加密的敏感字段：
- settings.value: 用户配置（可能包含 API 密钥、阈值等敏感配置）
- alert_rules.name: 用户自定义提醒名称（可能包含个人信息）
- portfolio: 持仓信息（投资金额、成本等敏感财务数据）
"""
from __future__ import annotations

import logging
from typing import Optional

import db
from security.crypto import encrypt, decrypt, is_encrypted, ENC_PREFIX

logger = logging.getLogger("security.migration")

# 需要加密的表和字段配置
# 注意：不加密用作查询键/计算的字段（如 portfolio.code、金额字段），
# 仅加密用户输入的文本型敏感数据
ENCRYPT_FIELDS = {
    "settings": ["value"],           # 用户配置（算法阈值、调度参数等）
    "alert_rules": ["name"],         # 用户自定义提醒名称
    "investment_records": ["note"],  # 投资记录备注（可能含个人信息）
}

# 主键字段配置
TABLE_PK = {
    "settings": "key",
    "alert_rules": "id",
    "investment_records": "id",
}


def _table_exists(conn, table_name: str) -> bool:
    """检查表是否存在。"""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return row is not None


def _column_exists(conn, table_name: str, column_name: str) -> bool:
    """检查列是否存在。"""
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table_name})").fetchall()]
    return column_name in cols


def migrate_encrypted_fields(dry_run: bool = False) -> dict:
    """
    迁移数据库中的明文敏感数据为加密存储。

    Args:
        dry_run: 如果为 True，只统计不实际修改

    Returns:
        迁移结果统计字典
    """
    conn = db.get_conn()
    stats = {
        "dry_run": dry_run,
        "tables": {},
        "total_encrypted": 0,
        "total_skipped": 0,
        "total_failed": 0,
        "errors": [],
    }

    for table_name, fields in ENCRYPT_FIELDS.items():
        if not _table_exists(conn, table_name):
            stats["tables"][table_name] = {"status": "table_not_found", "encrypted": 0}
            continue

        pk = TABLE_PK.get(table_name, "id")
        if not _column_exists(conn, table_name, pk):
            stats["tables"][table_name] = {"status": "pk_not_found", "encrypted": 0}
            continue

        table_stats = {"encrypted": 0, "skipped": 0, "failed": 0, "fields": fields}

        for field in fields:
            if not _column_exists(conn, table_name, field):
                table_stats[f"{field}_status"] = "column_not_found"
                continue

            try:
                # 读取所有行
                rows = conn.execute(
                    f"SELECT {pk}, {field} FROM {table_name} WHERE {field} IS NOT NULL"
                ).fetchall()

                for row in rows:
                    pk_value = row[0]
                    field_value = row[1]

                    if field_value is None or field_value == "":
                        table_stats["skipped"] += 1
                        continue

                    # 已经加密则跳过
                    if is_encrypted(str(field_value)):
                        table_stats["skipped"] += 1
                        continue

                    # 执行加密
                    try:
                        encrypted_value = encrypt(str(field_value))
                        if not dry_run:
                            conn.execute(
                                f"UPDATE {table_name} SET {field}=? WHERE {pk}=?",
                                (encrypted_value, pk_value),
                            )
                        table_stats["encrypted"] += 1
                        stats["total_encrypted"] += 1
                    except Exception as e:
                        table_stats["failed"] += 1
                        stats["total_failed"] += 1
                        stats["errors"].append(
                            f"{table_name}.{field} pk={pk_value}: {str(e)[:100]}"
                        )

            except Exception as e:
                stats["errors"].append(f"{table_name}.{field}: {str(e)[:100]}")
                table_stats["failed"] += 1

        if not dry_run and table_stats["encrypted"] > 0:
            conn.commit()

        stats["tables"][table_name] = table_stats
        stats["total_skipped"] += table_stats["skipped"]

    logger.info(
        "敏感数据迁移完成: encrypted=%d, skipped=%d, failed=%d, dry_run=%s",
        stats["total_encrypted"],
        stats["total_skipped"],
        stats["total_failed"],
        dry_run,
    )

    return stats


def rollback_encryption(dry_run: bool = False) -> dict:
    """
    回滚加密：将已加密的数据解密回明文。

    Args:
        dry_run: 如果为 True，只统计不实际修改

    Returns:
        回滚结果统计字典
    """
    conn = db.get_conn()
    stats = {
        "dry_run": dry_run,
        "tables": {},
        "total_decrypted": 0,
        "total_skipped": 0,
        "errors": [],
    }

    for table_name, fields in ENCRYPT_FIELDS.items():
        if not _table_exists(conn, table_name):
            continue

        pk = TABLE_PK.get(table_name, "id")
        if not _column_exists(conn, table_name, pk):
            continue

        table_stats = {"decrypted": 0, "skipped": 0}

        for field in fields:
            if not _column_exists(conn, table_name, field):
                continue

            try:
                rows = conn.execute(
                    f"SELECT {pk}, {field} FROM {table_name} WHERE {field} IS NOT NULL"
                ).fetchall()

                for row in rows:
                    pk_value = row[0]
                    field_value = row[1]

                    if not is_encrypted(str(field_value)):
                        table_stats["skipped"] += 1
                        continue

                    try:
                        decrypted_value = decrypt(str(field_value))
                        if not dry_run:
                            conn.execute(
                                f"UPDATE {table_name} SET {field}=? WHERE {pk}=?",
                                (decrypted_value, pk_value),
                            )
                        table_stats["decrypted"] += 1
                        stats["total_decrypted"] += 1
                    except Exception as e:
                        stats["errors"].append(
                            f"{table_name}.{field} pk={pk_value}: {str(e)[:100]}"
                        )

            except Exception as e:
                stats["errors"].append(f"{table_name}.{field}: {str(e)[:100]}")

        if not dry_run and table_stats["decrypted"] > 0:
            conn.commit()

        stats["tables"][table_name] = table_stats
        stats["total_skipped"] += table_stats["skipped"]

    logger.info(
        "加密回滚完成: decrypted=%d, skipped=%d, dry_run=%s",
        stats["total_decrypted"],
        stats["total_skipped"],
        dry_run,
    )

    return stats


def get_encryption_status() -> dict:
    """
    获取数据库加密状态统计。

    Returns:
        各表加密状态统计
    """
    conn = db.get_conn()
    status = {"tables": {}, "total_encrypted": 0, "total_plaintext": 0}

    for table_name, fields in ENCRYPT_FIELDS.items():
        if not _table_exists(conn, table_name):
            continue

        table_status = {"fields": {}}

        for field in fields:
            if not _column_exists(conn, table_name, field):
                continue

            try:
                total = conn.execute(
                    f"SELECT COUNT(*) FROM {table_name} WHERE {field} IS NOT NULL AND {field} != ''"
                ).fetchone()[0]

                encrypted = conn.execute(
                    f"SELECT COUNT(*) FROM {table_name} WHERE {field} LIKE '{ENC_PREFIX}%'"
                ).fetchone()[0]

                plaintext = total - encrypted
                table_status["fields"][field] = {
                    "total": total,
                    "encrypted": encrypted,
                    "plaintext": plaintext,
                }
                status["total_encrypted"] += encrypted
                status["total_plaintext"] += plaintext
            except Exception as e:
                table_status["fields"][field] = {"error": str(e)[:100]}

        status["tables"][table_name] = table_status

    return status
