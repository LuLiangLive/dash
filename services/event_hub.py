# -*- coding: utf-8 -*-
"""
services/event_hub.py —— v2.9.3 服务端事件中枢（功能链矩阵 N2）

让"系统主动产生事件"不再依赖用户开着页面：
  ① 定时扫描提醒规则——直接复用 modules.alert.router.check_alerts
     （与手动「检查提醒」同一规则引擎、同一 1 小时去重窗口），
     命中写 alert_history，前端凭未读计数做角标/消息中心；
  ② 按早/午/晚报三档（NEWS_SLOTS，默认 07:00 / 12:00 / 20:00）自动采集资讯，
     当日幂等标记写 settings 表（key=evhub.news.<date>.<slot>），服务重启不重复跑；
  ③ 中枢自身 job 失败 → monitor.create_alert 自告警（与 v2.9.2 监控链同表）。

开关（环境变量）：
  EVENT_HUB=0        整体停用（默认启用）
  ALERT_SCAN_MIN=5   告警扫描间隔分钟（下限 1）
  NEWS_AUTO=0        停用资讯自动采集（只保留告警扫描）

测试入口：run_scan_once() / run_news_slot(key, force) 可直接调用；hub_status() 查状态。
"""
from __future__ import annotations

import asyncio
import os
import threading
import time

import db

_running = False
_lock = threading.Lock()
_state: dict = {
    "started_at": None,
    "alert_scan_count": 0,
    "last_alert_scan": None,
    "last_alert_triggered": 0,
    "last_error": None,
    "news_date": None,
    "news_done_today": [],
}
_last_scan_ts = 0.0


def _env_enabled() -> bool:
    return os.environ.get("EVENT_HUB", "1") != "0"


def _scan_interval_min() -> int:
    try:
        return max(1, int(os.environ.get("ALERT_SCAN_MIN", "5")))
    except ValueError:
        return 5


def _self_alert(message: str) -> None:
    """中枢自身故障 → 写监控告警（event_hub_error），避免静默死掉。"""
    try:
        from modules.monitor.service import create_alert
        create_alert(alert_type="event_hub_error", severity="warning",
                     message=f"[事件中枢] {message}", source="event_hub")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 任务 ①：提醒规则扫描
# ---------------------------------------------------------------------------

def run_scan_once() -> int:
    """跑一轮提醒扫描，返回本轮**实际新增**的提醒条数。

    check_alerts 的 count 含 1h 去重窗内的重复命中（命中但未落库也算），
    以 alert_history 前后计数差为准，避免每轮扫描虚报触发、刷自告警噪音。
    check_alerts 是 FastAPI 装饰的原协程（内部无 await），
    asyncio.run 单起事件循环执行，与手动 POST /alerts/check 行为完全一致。
    """
    global _last_scan_ts
    from modules.alert.router import check_alerts
    conn = db.get_conn()
    before = conn.execute("SELECT COUNT(*) FROM alert_history").fetchone()[0]
    asyncio.run(check_alerts())
    after = conn.execute("SELECT COUNT(*) FROM alert_history").fetchone()[0]
    n = max(0, after - before)
    _last_scan_ts = time.time()
    _state["last_alert_scan"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    _state["alert_scan_count"] += 1
    _state["last_alert_triggered"] = n
    if n > 0:
        # 触发过提醒也记一条监控动态（低严重度，不打扰），便于复盘"系统几点推了什么"
        try:
            from modules.monitor.service import create_alert
            create_alert(alert_type="event_hub_scan", severity="info",
                         message=f"[事件中枢] 提醒扫描新增 {n} 条", source="event_hub")
        except Exception:
            pass
    return n


# ---------------------------------------------------------------------------
# 任务 ②：资讯三档采集（早/午/晚）
# ---------------------------------------------------------------------------

def _news_key(date_str: str, slot_key: str) -> str:
    return f"evhub.news.{date_str}.{slot_key}"


def run_news_slot(slot_key: str, force: bool = False) -> bool:
    """执行一档资讯采集；非 force 时以 settings 当日标记幂等。返回是否真实执行。"""
    from modules.market.market_service import run_news_collect
    date_str = time.strftime("%Y-%m-%d")
    conn = db.get_conn()
    if not force:
        row = conn.execute("SELECT value FROM settings WHERE key = ?",
                           (_news_key(date_str, slot_key),)).fetchone()
        if row:
            return False
    try:
        run_news_collect(slot_key, force=force)
    except TypeError:
        run_news_collect(slot_key)  # 兼容旧签名（无 force 形参）
    conn.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (_news_key(date_str, slot_key), "1",
         time.strftime("%Y-%m-%dT%H:%M:%S+08:00")))
    conn.commit()
    # 清理前两天的幂等标记，防 settings 无限增长
    for back in (1, 2):
        try:
            old = time.strftime("%Y-%m-%d", time.localtime(time.time() - back * 86400))
            conn.execute("DELETE FROM settings WHERE key LIKE ?", (f"evhub.news.{old}.%",))
            conn.commit()
        except Exception:
            pass
    return True


# ---------------------------------------------------------------------------
# 调度主循环
# ---------------------------------------------------------------------------

def _tick(now_struct=None) -> None:
    """单次调度判定（独立出来便于测试注入时间）。"""
    now_struct = now_struct or time.localtime()
    date_str = time.strftime("%Y-%m-%d", now_struct)
    if _state["news_date"] != date_str:
        _state["news_date"] = date_str
        _state["news_done_today"] = []

    # ① 告警扫描
    try:
        if time.time() - _last_scan_ts >= _scan_interval_min() * 60:
            run_scan_once()
    except Exception as e:
        _state["last_error"] = f"alert_scan: {e}"[:200]
        _self_alert(f"提醒扫描失败: {e}")

    # ② 资讯三档
    if os.environ.get("NEWS_AUTO", "1") != "0":
        try:
            from modules.market.market_service import NEWS_SLOTS
            hhmm = time.strftime("%H:%M", now_struct)
            for slot in NEWS_SLOTS:
                if slot["key"] in _state["news_done_today"]:
                    continue
                if hhmm >= slot["time"]:
                    if run_news_slot(slot["key"]):
                        _state["news_done_today"].append(slot["key"])
        except Exception as e:
            _state["last_error"] = f"news: {e}"[:200]
            _self_alert(f"资讯采集调度失败: {e}")


def _loop() -> None:
    while _running:
        _tick()
        time.sleep(30)


# ---------------------------------------------------------------------------
# 生命周期 / 状态
# ---------------------------------------------------------------------------

def start_event_hub() -> bool:
    """启动事件中枢后台线程（幂等；EVENT_HUB=0 时不启动）。"""
    global _running
    with _lock:
        if _running:
            return False
        if not _env_enabled():
            return False
        _running = True
    _state["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    threading.Thread(target=_loop, name="event-hub", daemon=True).start()
    return True


def stop_event_hub() -> None:
    global _running
    _running = False


def hub_status() -> dict:
    return {
        "running": _running,
        "enabled_env": _env_enabled(),
        "scan_interval_min": _scan_interval_min(),
        "news_auto": os.environ.get("NEWS_AUTO", "1") != "0",
        **{k: _state[k] for k in list(_state.keys())},
    }
