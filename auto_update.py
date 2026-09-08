#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
auto_update.py —— 本地全量采集 → 自动上传同步云函数（公网每日自动更新）

流程（5 步）:
  1. FULL_MARKET=1 全量采集（默认上限 6000 只，走净值缓存增量）
  2. 校验: ok=True 且基金数>=阈值 且 错误数<=容错
  3. WAL checkpoint 合并后复制 fund.db → functions/invest-api/fund.db
  4. tcb fn deploy invest-api 上传云函数
  5. 公网健康检查（HTTP 200 + 新版本标记），任一步失败即中止并返回非 0 退出码

用法:
  python auto_update.py                     # 全流程（推荐计划任务用）
  python auto_update.py --max 6000          # 指定采集上限
  python auto_update.py --verbose           # 显示采集进度
  python auto_update.py --skip-collect      # 跳过采集，用现有 fund.db 直接同步+部署
  python auto_update.py --skip-deploy       # 只采集+同步，不部署
  python auto_update.py --check             # 环境自检（tcb/curl/fund.db）

定时（Windows 任务计划程序，建议每天 20:45，避开基金净值更新晚高峰）:
  schtasks /Create /TN "fund-auto-update" ^
    /TR "cmd /c C:\\Users\\<user>\\WorkBuddy\\2026-08-20-17-57-54\\start_auto_update.bat" ^
    /SC DAILY /ST 20:45 /F
  schtasks /Run /TN fund-auto-update        # 立即手动触发一次

定时（Linux cron，每天 20:45）:
  45 20 * * * cd /path/to/project && python auto_update.py >> logs/cron.log 2>&1

退出码: 0 成功 | 1 采集/校验失败 | 2 同步失败 | 3 部署失败 | 4 公网验证失败 | 5 已有任务在运行
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from config import settings

ROOT = Path(__file__).resolve().parent
FUNC_DIR = ROOT / "functions" / "invest-api"
DB_SRC = ROOT / "fund.db"
DB_DST = FUNC_DIR / "fund.db"
LOG_DIR = ROOT / "logs"
LOG_FILE = LOG_DIR / "auto_update.log"
LOCK_FILE = ROOT / "auto_update.lock"

PUBLIC_URL = settings.public_url
# Windows 上 subprocess 无法解析无扩展名的 `tcb`(git-bash shim)，需用 tcb.cmd 完整路径
TCB_BIN = settings.tcb_bin
MIN_FUNDS = settings.min_funds
MIN_DB_MB = settings.min_db_mb
MAX_ERRORS = settings.max_errors


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:  # 日志失败不阻断主流程
        print(f"[{ts}] [warn] 日志写入失败: {e}", flush=True)


def _run(cmd, timeout=None):
    """运行命令，返回 (rc, output)。"""
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", cwd=str(ROOT), timeout=timeout,
        )
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except FileNotFoundError:
        return -1, f"命令不存在: {cmd[0]}"
    except subprocess.TimeoutExpired as e:
        return -2, f"命令超时: {e}"


# ---------------------------------------------------------------------------
# 锁：避免计划任务与手动运行并发
# ---------------------------------------------------------------------------

def acquire_lock() -> bool:
    if LOCK_FILE.exists():
        age = time.time() - LOCK_FILE.stat().st_mtime
        if age < 3 * 3600:
            log(f"检测到 auto_update.lock（已有任务运行或上次异常退出），本次跳过")
            return False
        log("发现过期 lock 文件，清理后继续")
        try:
            LOCK_FILE.unlink()
        except OSError:
            pass
    try:
        LOCK_FILE.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass
    return True


def release_lock() -> None:
    try:
        LOCK_FILE.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 各步骤
# ---------------------------------------------------------------------------

def step_collect(max_funds: int, verbose: bool) -> dict:
    log(f"[1/5] 全量采集 FULL_MARKET=1 max={max_funds} ...")
    os.environ["FULL_MARKET"] = "1"
    sys.path.insert(0, str(ROOT))
    import db
    from collector.pipeline import run_collector

    t0 = time.time()
    result = run_collector(max_funds=max_funds, verbose=verbose)
    # 关闭连接并把 WAL 合并进主库，确保复制的 fund.db 完整
    try:
        db.close_conn()
        with sqlite3.connect(str(db.DB_PATH)) as c:
            c.execute("PRAGMA wal_checkpoint(TRUNCATE);")
    except Exception as e:
        log(f"[warn] WAL checkpoint 失败: {e}")
    result["_cost_s"] = round(time.time() - t0, 1)
    return result


def validate(result: dict) -> str | None:
    if not result.get("ok"):
        return f"采集失败: {result.get('error')}"
    funds = result.get("funds", 0)
    if funds < MIN_FUNDS:
        return f"基金数过少: {funds} < MIN_FUNDS({MIN_FUNDS})"
    errs = result.get("errors") or []
    if len(errs) > MAX_ERRORS:
        return f"错误过多: {len(errs)} > MAX_ERRORS({MAX_ERRORS})"
    return None


def step_sync() -> str | None:
    if not DB_SRC.exists():
        return f"fund.db 不存在: {DB_SRC}"
    mb = DB_SRC.stat().st_size / 1024 / 1024
    if mb < MIN_DB_MB:
        return f"fund.db 过小({mb:.1f}MB < {MIN_DB_MB}MB)，中止同步"
    wal = ROOT / "fund.db-wal"
    if wal.exists() and wal.stat().st_size > 1024:
        return f"fund.db-wal 非空({wal.stat().st_size}B)，请先确认采集已 checkpoint"
    log(f"[2/5] 同步 fund.db -> {DB_DST}（{mb:.1f}MB）")
    try:
        FUNC_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(DB_SRC, DB_DST)
    except Exception as e:
        return f"复制失败（目标文件可能被占用）: {e}"
    if DB_DST.stat().st_size != DB_SRC.stat().st_size:
        return "复制校验失败: 源/目标大小不一致"
    return None


def step_deploy() -> str | None:
    cmd = [TCB_BIN, "fn", "deploy", "invest-api", "--dir", str(FUNC_DIR),
           "--path", "/", "--force", "--install-dependency", "false"]
    log("[3/5] 部署云函数 invest-api（约 30~60s）...")
    rc, out = _run(cmd, timeout=600)
    if rc != 0:
        return f"tcb deploy 失败(rc={rc}):\n{out[-800:]}"
    tail = out.strip().splitlines()
    log(f"[3/5] deploy 完成: {tail[-1] if tail else ''}")
    return None


def step_verify() -> str | None:
    log("[4/5] 公网健康检查 ...")
    out = ""
    for i in range(3):
        rc, out = _run(
            ["curl", "-s", "--max-time", "60", "-w", "\n%{http_code}", PUBLIC_URL],
            timeout=90,
        )
        if rc == 0:
            parts = out.rsplit("\n", 1)
            code = (parts[-1].strip() if parts else "")
            body = (parts[0] if parts else "")
            if code == "200" and "nav-fade" in body:
                log("[4/5] 公网验证通过（HTTP 200 + 新版本标记 nav-fade）")
                return None
        time.sleep(5)
    return f"公网验证失败: {out[-200:]}"


def do_check() -> int:
    print("== 环境自检 ==")
    rc, out = _run([TCB_BIN, "--version"])
    print(f"tcb        : {'OK ' + out.strip().splitlines()[0] if rc == 0 else '不可用: ' + out[:200]}")
    rc2, _ = _run(["curl", "--version"])
    print(f"curl       : {'OK' if rc2 == 0 else '不可用'}")
    print(f"python     : {sys.version.split()[0]}")
    print(f"项目目录   : {ROOT}")
    print(f"fund.db    : {DB_SRC.exists() and f'{DB_SRC.stat().st_size/1024/1024:.1f}MB' or '缺失'}")
    print(f"云函数目录 : {'存在' if FUNC_DIR.exists() else '缺失'}（含 fund.db "
          f"{DB_DST.exists() and f'{DB_DST.stat().st_size/1024/1024:.1f}MB' or '缺失'}）")
    print(f"公网地址   : {PUBLIC_URL}")
    ok = rc == 0 and rc2 == 0 and DB_SRC.exists() and FUNC_DIR.exists()
    print("== " + ("自检通过 ✅" if ok else "存在异常，请按上方提示处理 ❌") + " ==")
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="本地全量采集 → 自动同步云函数")
    ap.add_argument("--max", type=int, default=6000, help="全量采集基金数上限(默认 6000)")
    ap.add_argument("--verbose", action="store_true", help="显示采集过程输出")
    ap.add_argument("--skip-collect", action="store_true", help="跳过采集，用现有 fund.db 同步+部署")
    ap.add_argument("--skip-deploy", action="store_true", help="只采集+同步，不部署")
    ap.add_argument("--check", action="store_true", help="环境自检后退出")
    args = ap.parse_args()

    if args.check:
        return do_check()

    if not acquire_lock():
        return 5
    try:
        if not args.skip_collect:
            result = step_collect(args.max, args.verbose)
            errs = result.get("errors") or []
            log(f"[1/5] 采集完成: ok={result.get('ok')} 基金={result.get('funds')} "
                f"净值={result.get('nav_points')} 榜单={result.get('rank_items')} "
                f"错误={len(errs)} 耗时={result.get('_cost_s')}s")
            if errs:
                log("      最近错误: " + ("；".join(str(e) for e in errs[:5])))
            bad = validate(result)
            if bad:
                log(f"[x] 校验未通过，中止: {bad}")
                return 1
        else:
            log("[1/5] 跳过采集（使用现有 fund.db）")

        bad = step_sync()
        if bad:
            log(f"[x] 同步失败: {bad}")
            return 2

        if not args.skip_deploy:
            bad = step_deploy()
            if bad:
                log(f"[x] 部署失败: {bad}")
                return 3
            bad = step_verify()
            if bad:
                log(f"[x] {bad}")
                return 4
        else:
            log("[3-4/5] 跳过部署（--skip-deploy）")

        log("[5/5] 自动更新完成 ✅（公网已同步最新数据）")
        return 0
    finally:
        release_lock()


if __name__ == "__main__":
    sys.exit(main())
