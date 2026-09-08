#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据库定期备份脚本
功能：
  1. 备份 fund.db 到 backups/ 目录，文件名含时间戳
  2. 自动清理超过7天的旧备份（保留最近7天）
  3. 记录备份日志到 backups/backup_log.log

使用方法：
  python scripts/backup_db.py
  python scripts/backup_db.py --keep-days 14   # 自定义保留天数
  python scripts/backup_db.py --dry-run          # 仅预览，不实际执行
"""

import os
import sys
import shutil
import argparse
import logging
from datetime import datetime, timedelta

# 路径配置（基于脚本所在位置的上级目录即 backend/）
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BACKEND_DIR = os.path.dirname(SCRIPT_DIR)
DB_PATH = os.path.join(BACKEND_DIR, "fund.db")
BACKUP_DIR = os.path.join(BACKEND_DIR, "backups")
LOG_PATH = os.path.join(BACKUP_DIR, "backup_log.log")

# 备份文件名前缀（用于识别和清理本脚本产生的备份）
BACKUP_PREFIX = "fund.db.auto_"


def setup_logging():
    """配置日志：同时输出到文件和控制台"""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    logger = logging.getLogger("backup_db")
    logger.setLevel(logging.INFO)
    # 避免重复添加handler
    if not logger.handlers:
        fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                                datefmt="%Y-%m-%d %H:%M:%S")
        fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(fmt)
        logger.addHandler(ch)
    return logger


def backup_database(logger, dry_run=False):
    """执行数据库备份"""
    if not os.path.exists(DB_PATH):
        logger.error(f"数据库文件不存在: {DB_PATH}")
        return None

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_name = f"{BACKUP_PREFIX}{timestamp}"
    backup_path = os.path.join(BACKUP_DIR, backup_name)

    db_size = os.path.getsize(DB_PATH)
    logger.info(f"开始备份: {DB_PATH} ({db_size / 1024 / 1024:.2f} MB)")

    if dry_run:
        logger.info(f"[预览] 将备份到: {backup_path}")
        return backup_path

    try:
        shutil.copy2(DB_PATH, backup_path)
        backup_size = os.path.getsize(backup_path)
        logger.info(f"备份完成: {backup_name} ({backup_size / 1024 / 1024:.2f} MB)")
        return backup_path
    except Exception as e:
        logger.error(f"备份失败: {e}")
        return None


def cleanup_old_backups(logger, keep_days=7, dry_run=False):
    """清理超过保留天数的自动备份"""
    if not os.path.exists(BACKUP_DIR):
        return

    cutoff = datetime.now() - timedelta(days=keep_days)
    removed = []
    kept = []

    for filename in os.listdir(BACKUP_DIR):
        if not filename.startswith(BACKUP_PREFIX):
            continue  # 只清理本脚本产生的自动备份，保留手动备份
        filepath = os.path.join(BACKUP_DIR, filename)
        if not os.path.isfile(filepath):
            continue
        try:
            # 从文件名解析时间戳: fund.db.auto_YYYYMMDD_HHMMSS
            ts_str = filename[len(BACKUP_PREFIX):]
            file_time = datetime.strptime(ts_str, "%Y%m%d_%H%M%S")
        except ValueError:
            logger.warning(f"无法解析备份文件名，跳过: {filename}")
            continue

        if file_time < cutoff:
            removed.append((filename, file_time))
        else:
            kept.append((filename, file_time))

    logger.info(f"保留策略: 最近 {keep_days} 天")
    logger.info(f"有效备份数: {len(kept)}, 待清理数: {len(removed)}")

    for filename, file_time in removed:
        filepath = os.path.join(BACKUP_DIR, filename)
        if dry_run:
            logger.info(f"[预览] 将删除: {filename} ({file_time.strftime('%Y-%m-%d %H:%M:%S')})")
        else:
            try:
                os.remove(filepath)
                logger.info(f"已删除旧备份: {filename}")
            except Exception as e:
                logger.error(f"删除失败 {filename}: {e}")

    return len(removed)


def main():
    parser = argparse.ArgumentParser(description="投研看板数据库备份脚本")
    parser.add_argument("--keep-days", type=int, default=7,
                        help="保留最近N天的备份（默认7天）")
    parser.add_argument("--dry-run", action="store_true",
                        help="预览模式，不实际执行备份和删除")
    args = parser.parse_args()

    logger = setup_logging()
    logger.info("=" * 50)
    logger.info("数据库备份任务开始")

    # 1. 执行备份
    backup_path = backup_database(logger, dry_run=args.dry_run)

    # 2. 清理旧备份
    cleanup_old_backups(logger, keep_days=args.keep_days, dry_run=args.dry_run)

    # 3. 输出当前备份目录概览
    if os.path.exists(BACKUP_DIR):
        backups = sorted([f for f in os.listdir(BACKUP_DIR)
                          if f.startswith("fund.db") and os.path.isfile(os.path.join(BACKUP_DIR, f))])
        total_size = sum(os.path.getsize(os.path.join(BACKUP_DIR, f)) for f in backups)
        logger.info(f"当前备份目录共 {len(backups)} 个文件，总计 {total_size / 1024 / 1024:.2f} MB")

    logger.info("数据库备份任务结束")
    logger.info("=" * 50)

    if backup_path is None and not args.dry_run:
        sys.exit(1)


if __name__ == "__main__":
    main()
