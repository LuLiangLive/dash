# -*- coding: utf-8 -*-
"""collector/fetch_manager.py —— 净值更新任务管理器(一键更新)

把「抓净值 → 重排榜单 → 上榜+自选统一计算 → 信号同步回榜单」串成一条可监控、
可停止的后台流水线, 供自选「设置」二级页调用。

状态机: idle → running(phase 推进) → done / error / stopped
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# v2.9.7: 数据版本戳管理（解决前端缓存污染问题）
try:
    from data_version import bump_data_version
except Exception:
    def bump_data_version():
        return str(int(time.time()))

PHASES = ["日期判断", "抓净值", "重排榜单", "抓取补充数据", "上榜+自选统一计算", "同步榜单"]
class FetchManager:
    """线程化抓取任务管理器(进程内单例)。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._thread = None
        self._cancel = threading.Event()
        self._state = {
            "status": "idle",          # idle / running / done / error / stopped
            "phase": "",               # 当前阶段中文名
            "phase_idx": 0,            # 0-based
            "phase_total": len(PHASES),
            "done": 0,
            "total": 0,
            "current": "",             # 当前处理基金
            "message": "",
            "scope": "",
            "started_at": None,
            "finished_at": None,
            "result": None,
            "errors": [],
            "log": [],                 # 实时进程日志(滚动缓冲, 最多 80 条)
            "ok": None,
        }

    # ------------------------------------------------------------------
    def _update(self, **kw):
        with self._lock:
            self._state.update(kw)

    def _log(self, text):
        with self._lock:
            self._state.setdefault("log", []).append(text)

    def _phase_log(self, phase_idx: int, message: str = ""):
        """动态生成阶段日志，自动同步PHASES数组的阶段数和名称。

        Args:
            phase_idx: 0-based阶段索引
            message: 附加消息（可选）

        输出格式: [阶段X/N] 阶段名称 · 附加消息
        """
        phase_name = PHASES[phase_idx] if 0 <= phase_idx < len(PHASES) else "未知阶段"
        if message:
            text = f"[阶段{phase_idx + 1}/{len(PHASES)}] {phase_name} · {message}"
        else:
            text = f"[阶段{phase_idx + 1}/{len(PHASES)}] {phase_name}"
        with self._lock:
            self._state.setdefault("log", []).append(text)
            if len(self._state["log"]) > 80:
                self._state["log"] = self._state["log"][-80:]

    def _log_memory(self, tag: str = ""):
        """v2.9.52: 记录当前进程内存使用，用于定位一键更新OOM问题。

        使用 psutil（如可用）获取详细内存信息，否则回退到 resource 模块。
        输出格式: [内存] tag · RSS=xxxMB · VMS=xxxMB · 峰值=xxxMB
        """
        try:
            import psutil
            import os
            proc = psutil.Process(os.getpid())
            mem = proc.memory_info()
            rss_mb = mem.rss / 1024 / 1024
            vms_mb = mem.vms / 1024 / 1024
            # 记录内存峰值
            if not hasattr(self, '_mem_peak_mb'):
                self._mem_peak_mb = 0
            if rss_mb > self._mem_peak_mb:
                self._mem_peak_mb = rss_mb
            text = f"[内存] {tag} · RSS={rss_mb:.0f}MB · VMS={vms_mb:.0f}MB · 峰值={self._mem_peak_mb:.0f}MB"
        except ImportError:
            try:
                import resource
                # Linux/macOS: ru_maxrss 是 KB
                peak_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                peak_mb = peak_kb / 1024
                text = f"[内存] {tag} · 峰值={peak_mb:.0f}MB (resource模块)"
            except Exception:
                text = f"[内存] {tag} · 无法获取内存信息"
        except Exception as e:
            text = f"[内存] {tag} · 获取失败: {e}"
        with self._lock:
            self._state.setdefault("log", []).append(text)
            if len(self._state["log"]) > 80:
                self._state["log"] = self._state["log"][-80:]

    def status(self) -> dict:
        with self._lock:
            return dict(self._state)

    @property
    def running(self) -> bool:
        with self._lock:
            return self._state["status"] == "running"

    # ------------------------------------------------------------------
    def _progress_cb(self, stage, done, total, code="", msg=""):
        """run_collector / recompute_ad 的回调 → 归一化到 6 阶段状态 + 进程日志。

        v2.10.0 6 阶段: 0=日期判断 / 1=抓净值 / 2=重排榜单 / 3=抓取补充数据+自选指标 / 4=上榜+自选统一计算 / 5=同步榜单
        """
        if stage == "fetch":
            self._update(phase_idx=1, phase="抓净值", done=done, total=total,
                         current=code, message=msg or "抓取净值中")
            # 早期阶段消息(无 code, 但 msg 非空)→ 立刻打日志, 避免长时间静默
            if msg and not code:
                self._log(f"[抓净值] {msg}")
            # 全市场 6000 只:每 1% 报一次进度 (每 60 只一行); 小批量逐只报
            _step = 1 if (total <= 50) else max(50, total // 20)
            if code and (done % _step == 0 or done == total or done == 1):
                self._log(f"[抓净值] {done}/{total} · {code}")
        elif stage in ("rank", "rank_full"):
            # v2.11.4 B4: rank_full 上报 stage="rank_full", 与 "rank" 同处理避免进度条卡死
            self._update(phase_idx=2, phase="重排榜单", done=done, total=total,
                         message=msg or "重排榜单中")
            if msg and not code:
                self._log(f"[重排榜单] {msg}")
            else:
                self._log("[重排榜单] " + (msg or "重排榜单中"))
        elif stage == "reco":
            self._update(phase_idx=4, phase="上榜+自选统一计算", done=done, total=total,
                         current=code, message=msg or "上榜+自选统一计算中")
            # 早期阶段消息同样打日志
            if msg and not code:
                self._log(f"[上榜+自选统一计算] {msg}")
            # 推荐信号阶段每只都打, 上榜+自选数量小(几十~几百只)
            if code and (done % 20 == 0 or done == total or done == 1):
                self._log(f"[上榜+自选统一计算] {done}/{total} · {code}")

    # ------------------------------------------------------------------
    def start(self, scope: str = "funds_pool", max_funds: int = None) -> dict:
        """触发一键更新。

        v0.47.0: 默认走 funds 全量池(scope='funds_pool'),抓数+排榜统一全量口径。
                  'displayed' 作为 alias; 'full_rebuild' 走 FULL_MARKET=1 重建。

        返回 {ok, message}; 已在运行时返回错误。"""
        if self.running:
            return {"ok": False, "message": "已有更新任务在运行中"}
        self._cancel.clear()
        # 同步预置运行状态+日志, 避免前端点击后首查仍显示 idle/无日志
        self._update(status="running", phase_idx=1, phase="抓净值", done=0, total=0,
                     current="", message="准备启动…", started_at=time.time(),
                     finished_at=None, errors=[], result=None, ok=None,
                     scope=scope, log=[])
        self._log("开始一键更新")
        self._thread = threading.Thread(
            target=self._run_monitored, args=(scope, max_funds), daemon=True)
        self._thread.start()
        return {"ok": True, "message": "更新任务已启动"}

    def _run_monitored(self, scope: str, max_funds: int):
        """包装 _run：无论 done/error/stopped 哪个终态, 都写一条 data_update_logs。

        v2.9.2 P0 修复：monitor/service.log_data_update 此前零调用者，
        监控页「数据更新状态 / 超时>30min 告警 / 失败 critical 告警」永远无数据源。
        统一在线程出口落日志, 不侵入 _run 内部各 return 分支。
        """
        _start_iso = time.strftime("%Y-%m-%dT%H:%M:%S")
        try:
            self._run(scope, max_funds)
        finally:
            try:
                st = self.status()
                _status_map = {"done": "success", "error": "failed", "stopped": "cancelled"}
                _final = st.get("status") or "unknown"
                _rc = 0
                try:
                    _rc = int((st.get("result") or {}).get("nav_points", 0) or 0)
                except Exception:
                    pass
                from modules.monitor.service import log_data_update
                log_data_update(
                    task_name="one_click_update",
                    status=_status_map.get(_final, _final),
                    start_time=_start_iso,
                    end_time=time.strftime("%Y-%m-%dT%H:%M:%S"),
                    record_count=_rc,
                    error_message="; ".join(str(x) for x in (st.get("errors") or []))[:500],
                )
                # v2.9.4 N4: 榜单重算完成的持久化时间戳（前端轮询锚点，自动刷新榜单/自选）
                if _final == "done":
                    try:
                        import db as _db
                        _iso = time.strftime("%Y-%m-%dT%H:%M:%S")
                        _c = _db.get_conn()
                        _c.execute(
                            "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
                            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                            ("meta.rank_recomputed_at", _iso, _iso))
                        _c.commit()
                    except Exception:
                        pass
            except Exception:
                pass  # 监控接线失败绝不影响更新任务本身

    def stop(self) -> dict:
        """请求停止当前任务(尽快中止, 非强制杀进程)。"""
        if not self.running:
            return {"ok": False, "message": "当前没有运行中的更新任务"}
        self._cancel.set()
        self._update(message="正在停止…")
        return {"ok": True, "message": "已发送停止请求"}

    # ------------------------------------------------------------------
    def _run(self, scope: str, max_funds: int):
        # start() 已预置 running 状态与首条日志, 此处不再清空 log
        errors = []
        try:
            import os
            from collector.pipeline import run_collector
            from collector.fetcher import effective_nav_date, cached_nav_max
            import db

            # v2.10.0: 六阶段流程: ①日期判断 → ②抓净值 → ③重排榜单 → ④抓取补充数据+自选指标 → ⑤上榜+自选统一计算 → ⑥同步榜单
            incremental = scope != "full_rebuild"
            mf = int(max_funds or os.environ.get("COLLECT_MAX", "6000"))

            # v2.9.52: 一键更新开始，记录初始内存
            self._log_memory("一键更新开始")

            # ---- 阶段0: 日期判断 ----
            # v0.47.0: 池 = funds 表全量（无论 scope, 增量/全量都走全量池）
            # 'displayed' 作为 'funds_pool' 的别名保留兼容
            self._update(phase_idx=0, phase="日期判断", done=0, total=0,
                         current="", message="正在判断净值日期…")
            self._phase_log(0)
            _now_str = time.strftime("%H:%M:%S")
            try:
                target_date = effective_nav_date()
            except Exception as _e:
                self._log(f"[日期判断] effective_nav_date 异常: {_e}, 回退今日")
                target_date = __import__("datetime").date.today().isoformat()
            self._log(f"[日期判断] 当前 {_now_str} · 目标日期 {target_date}")
            self._log(f"[日期判断] 19:00 前取缓存最新 / 19:00 后取今日 / 非交易日不抓空值")

            # v0.86.0: 池 = 基金代码库(全市场C类,每周更新) ∪ 当日日涨幅前100 C类(每天更新)
            if scope == "full_rebuild":
                from collector import fetcher as _fch
                _pool_codes = [p["code"] for p in _fch.effective_pool(mode="full", max_funds=mf)]
                _pool_stats = {"全市场": len(_pool_codes), "当日前100": 0, "去重后": len(_pool_codes)}
            else:
                try:
                    from collector import fund_universe as _fu
                    _pool_codes, _pool_stats = _fu.get_effective_pool(max_funds=mf, verbose=True)
                    self._log(f"[日期判断] 有效池: 代码库 {_pool_stats['代码库']} + 当日前100 {_pool_stats['当日前100']} "
                              f"= 去重后 {_pool_stats['去重后']} (新增热门 {_pool_stats.get('新增热门', 0)})")
                except Exception as _e:
                    # 兜底:从funds表读全量
                    self._log(f"[日期判断] 基金代码库加载异常 ({_e}), 回退到funds表全量")
                    try:
                        _cur = db.get_conn()
                        _pool_codes = [r[0] for r in _cur.execute("SELECT code FROM funds ORDER BY code")]
                        _pool_stats = {"funds表": len(_pool_codes), "当日前100": 0, "去重后": len(_pool_codes)}
                    except Exception as _e2:
                        self._log(f"[日期判断] funds表读取也失败 ({_e2}), 回退到全市场模式")
                        from collector import fetcher as _fch
                        _pool_codes = [p["code"] for p in _fch.effective_pool(mode="full", max_funds=mf)]
                        _pool_stats = {"全市场": len(_pool_codes), "当日前100": 0, "去重后": len(_pool_codes)}
            _codes = _pool_codes
            _N = len(_codes)
            self._log(f"[日期判断] 准备校验 {len(_codes)} 只基金的缓存日期…")
            self._update(total=_N, message=f"正在校验 {_N} 只基金的缓存日期…")

            _skipped = 0
            _todo = 0
            for _i, _c in enumerate(_codes, 1):
                if self._cancel.is_set():
                    self._log("⏹ 已手动停止")
                    self._update(status="stopped", message="已手动停止",
                                 finished_at=time.time())
                    return
                try:
                    _cm = cached_nav_max(_c)
                except Exception:
                    _cm = None
                if _cm == target_date:
                    _skipped += 1
                else:
                    _todo += 1
                # 每 50 只 (或最后一只) 报一次进度
                if _i % max(1, _N // 20) == 0 or _i == _N:
                    self._update(done=_i, current=f"{_c}",
                                 message=f"校验缓存 {_i}/{_N} · 缓存命中 {_skipped} · 待补抓 {_todo}")
            self._log(f"[日期判断] 完成: 缓存最新 == {target_date} 的基金 {_skipped} 只; 待补抓 {_todo} 只")
            # 把判断结果存进 state.result 供前端读取
            self._update(message=f"日期判断完成 · 缓存命中 {_skipped} 只 / 待补抓 {_todo} 只")

            # v2.9.52: 阶段1结束，记录内存
            self._log_memory("阶段1结束")

            # ---- 阶段1: 抓净值 ----
            if not incremental:
                os.environ["FULL_MARKET"] = "1"
                self._update(phase_idx=1, phase="抓净值", done=0, total=mf,
                             current="", message=f"开始抓取净值(全量重建, 目标约{mf}只)…")
                self._phase_log(1, f"全量目标约 {mf} 只")
                self._log(f"[提示] 全市场重建需 8-15 分钟; 完成后会生成缓存, 后续增量秒级")
            else:
                self._update(phase_idx=1, phase="抓净值", done=0, total=_N,
                             current="", message=f"开始抓取净值(funds 全量 {_N} 只, 增量到 {target_date})…")
                self._phase_log(1, f"funds 全量池 {_N} 只 · 目标日期 {target_date} · 当前 {_now_str}")
                self._log(f"[提示] 缓存最新 == 目标日期的基金会自动跳过, 不重复拉取")
            try:
                # v0.47.0: 把 funds 全量池透传给 run_collector;由它按增量规则抓净值
                result = run_collector(max_funds=_N, verbose=True,
                                       progress_cb=self._progress_cb,
                                       cancel_flag=self._cancel,
                                       incremental=incremental,
                                       pool_override=_codes if incremental else None)
            except Exception as _e:
                import traceback
                _tb = traceback.format_exc()
                self._log(f"❌ 抓净值阶段异常: {_e}")
                self._update(status="error",
                             message=f"抓净值失败: {_e}",
                             finished_at=time.time(), ok=False,
                             errors=[f"[抓净值阶段] {_e}\n{_tb}"])
                return
            if self._cancel.is_set():
                self._log("⏹ 已手动停止")
                self._update(status="stopped", message="已手动停止",
                             finished_at=time.time())
                return
            if not result.get("ok"):
                raise RuntimeError(result.get("error") or "采集失败")
            rank_refreshed = result.get("rank_refreshed", True)
            if rank_refreshed:
                self._phase_log(1, f"抓净值完成 · 净值 {result.get('nav_points', 0)} 条 / 基金 {result.get('funds', 0)} 只")
            else:
                self._phase_log(1, "抓净值完成 · 覆盖不足, 保留已有缓存")

            # v2.9.52: 阶段2结束，记录内存
            self._log_memory("阶段2结束")

            # v2.11.2 长期兜底: 全池 dd20 按已存净值刷新(被"缓存最新已跳过"的基金
            # 不走 run_collector 重算, 可能残留旧口径; 此处纯本地兜底, 保证全池一致)
            try:
                from modules.rank.ranker import refresh_funds_dd20
                _dd20_n = refresh_funds_dd20()
                self._log(f"[阶段2/6] dd20 全池兜底刷新: 更新 {_dd20_n} 只")
            except Exception as _dd20_e:
                self._log(f"[阶段2/6] dd20 兜底刷新跳过(不影响流程): {_dd20_e}")

            # ---- 阶段3: 重排榜单 ----
            self._update(phase_idx=2, phase="重排榜单", done=0, total=5,
                         current="", message="正在重排榜单（全量 funds）…")
            self._phase_log(2, "(全量 funds)")
            # v0.47.0: 独立阶段 3.5 = rank_full.compute_all_panels
            # 由 funds 全量重算 10 个 sub,覆盖 pipeline 内 build_ranks 写的内容
            try:
                from collector import rank_full
                rf = rank_full.compute_all_panels(progress_cb=self._progress_cb)
                rank_items = sum(len(s) for p in rf["panels"].values() for s in p.values())
                self._phase_log(2, f"rank_full 完成 · {rf['funds_size']} 只 / {rf['listed_size']} 上榜 · {rank_items} 项")
                # 关键:刷新 funds 表 nav_date 等聚合字段 + meta 行(老 API 依赖)
                # v2.11.4 B3: 修正 db.log_finish 签名不匹配
                # (真实签名 log_finish(log_id, status, message, started_ts))
                try:
                    _log_id = db.log_start("rank_full")
                    db.log_finish(_log_id, "ok",
                                  f"重排完成 listed={rf.get('listed_size') or 0} items={rank_items}",
                                  time.time())
                except Exception:
                    pass
            except Exception as _e:
                import traceback
                _tb = traceback.format_exc()
                self._log(f"❌ 重排榜单阶段异常: {_e}")
                self._update(status="error",
                             message=f"重排榜单失败: {_e}",
                             finished_at=time.time(), ok=False,
                             errors=[f"[重排榜单阶段] {_e}\n{_tb}"])
                return
            if self._cancel.is_set():
                self._log("⏹ 已手动停止")
                self._update(status="stopped", message="已手动停止",
                             finished_at=time.time())
                return

            # v2.9.52: 阶段3结束，记录内存
            self._log_memory("阶段3结束")

            # ---- 阶段3: 抓取补充数据 + 自选指标 (v2.10.0: 合并原阶段3+5) ----
            # 执行顺序: 抓持仓/涨跌幅/历史净值 → 清理long_series缓存 → 计算自选基金完整指标 → 清理旧净值
            self._update(phase_idx=3, phase="抓取补充数据", done=0, total=1,
                         message="正在抓取自选+上榜基金持仓与历史净值…")
            self._phase_log(3, "自选+上榜基金持仓 + 2年历史净值 + 自选完整指标 (m1/m3/m6/y1已由阶段1净值计算, 不再重复抓取)")
            try:
                from collector.pipeline import _fetch_top_holdings, _fetch_top_history
                import db as _db
                _today = _db.latest_rank_date() or time.strftime("%Y-%m-%d")
                _fetch_top_holdings(_today, verbose=True)
                # v2.5.3新增: 为上榜+自选基金抓取2年历史净值(解决数据缺失问题)
                # v2.5.4优化: 改用pingzhongdata接口, 速度提升50倍
                _fetch_top_history(_today, verbose=True, history_days=500)

                # v2.5.4新增: 补全数据后清理long_series缓存, 确保后续指标计算用新数据
                try:
                    from modules.nav.nav_series import clear_cache
                    # 获取上榜+自选基金代码
                    _all_codes = set()
                    for _panel_data in rf.get("panels", {}).values():
                        for _items in _panel_data.values():
                            for _meta in _items:
                                _code = _meta.get("code")
                                if _code:
                                    _all_codes.add(_code)
                    _watch_codes = [r[0] for r in _db.get_conn().execute(
                        "SELECT DISTINCT code FROM watchlist WHERE code != ''").fetchall()]
                    _all_codes.update(_watch_codes)
                    # 通过封装接口清理缓存（不直接操作全局变量）
                    _cleared = clear_cache(list(_all_codes))
                    self._log(f"[阶段4/6] 清理long_series缓存: {_cleared}只基金的缓存已清理")
                except Exception as _cache_e:
                    self._log(f"[阶段4/6] 清理long_series缓存失败(不影响后续): {_cache_e}")

                self._log(f"[阶段4/6] 上榜数据抓取完成 · 日期 {_today}")
            except Exception as _e:
                self._log(f"⚠️ 上榜数据抓取失败(不影响榜单): {_e}")

            # v2.9.52: 阶段4结束，记录内存
            self._log_memory("阶段4结束")

            # ---- 阶段3(续): 计算自选基金完整指标 ----
            try:
                from modules.fund.fund_metrics import batch_compute_metrics, save_metrics_to_db, cleanup_old_navs
                import db as _db2

                # 只获取自选基金代码（榜单基金在阶段4计算）
                _watch_codes = [r[0] for r in _db2.get_conn().execute(
                    "SELECT DISTINCT code FROM watchlist WHERE code != ''").fetchall()]

                # 排除已经在榜单中的基金（避免重复计算）
                _rank_codes = set()
                for _panel_data in rf.get("panels", {}).values():
                    for _items in _panel_data.values():
                        for _meta in _items:
                            _code = _meta.get("code")
                            if _code:
                                _rank_codes.add(_code)

                _target_codes = [c for c in _watch_codes if c not in _rank_codes]
                self._log(f"[阶段4/6] 自选指标: 自选{len(_watch_codes)}只 - 榜单已算{len(_watch_codes)-len(_target_codes)}只 = 待算{len(_target_codes)}只")

                if _target_codes:
                    # 批量计算完整指标
                    _metrics_results = batch_compute_metrics(
                        _target_codes, verbose=True,
                        progress_cb=lambda done, total, code: self._update(
                            phase_idx=3, phase="抓取补充数据", done=done, total=total,
                            current=code, message=f"计算自选完整指标 {done}/{total}"))

                    # 保存到数据库
                    _saved = 0
                    for _code, _metrics in _metrics_results.items():
                        if save_metrics_to_db(_code, _metrics):
                            _saved += 1

                    self._log(f"[阶段4/6] 自选完整指标计算完成: 成功{len(_metrics_results)}/{len(_target_codes)}只, 保存{_saved}只")
                else:
                    self._log("[阶段4/6] 无待计算的自选基金（全部已在榜单中计算）")

                # v2.5.4优化: 清理旧净值数据, 上榜+自选基金保留2年, 其他基金只保留90天
                _keep_codes = set()
                for _panel_data in rf.get("panels", {}).values():
                    for _items in _panel_data.values():
                        for _meta in _items:
                            _code = _meta.get("code")
                            if _code:
                                _keep_codes.add(_code)
                _watch_codes = [r[0] for r in _db2.get_conn().execute(
                    "SELECT DISTINCT code FROM watchlist WHERE code != ''").fetchall()]
                _keep_codes.update(_watch_codes)
                _keep_codes = list(_keep_codes)

                # 清理旧数据: 上榜+自选基金排除(保留2年), 其他基金清理365天以上
                # v2.11.6: 窗口天数收敛到 fetcher.NAV_RETENTION_DAYS（与健康判定阈值联动）
                from modules.nav.fetcher import NAV_RETENTION_DAYS
                _cleaned = cleanup_old_navs(days=NAV_RETENTION_DAYS, verbose=True, exclude_codes=_keep_codes)
                self._log(f"[阶段4/6] 清理旧净值数据: 删除{_cleaned}条365天前的数据 "
                          f"(上榜+自选{len(_keep_codes)}只保留2年)")

            except Exception as _e:
                import traceback
                self._log(f"⚠️ 自选完整指标计算失败(不影响榜单): {_e}")
                self._log(traceback.format_exc())

            # v2.9.52: 阶段5结束，记录内存
            self._log_memory("阶段5结束")

            # ---- 阶段4: 上榜+自选统一计算 (v2.10.0: 合并原阶段4+6) ----
            # 执行顺序: 先计算榜单基金完整指标 → 再调用recompute_ad.main(ad/earn/score精确重算)
            self._update(phase_idx=4, phase="上榜+自选统一计算", done=0, total=1,
                         message="正在计算榜单基金完整指标…")
            self._phase_log(4, "(榜单完整指标 + ad/earn/score精确重算)")
            try:
                from modules.fund.fund_metrics import batch_compute_metrics, save_metrics_to_db
                import db as _db_rank

                # 从rank_full返回的panels中提取榜单基金代码
                _rank_codes = set()
                for _panel_data in rf.get("panels", {}).values():
                    for _items in _panel_data.values():
                        for _meta in _items:
                            _code = _meta.get("code")
                            if _code:
                                _rank_codes.add(_code)

                _rank_codes = list(_rank_codes)
                self._log(f"[阶段5/6] 榜单基金数量: {len(_rank_codes)}只")

                if _rank_codes:
                    # 批量计算完整指标
                    _metrics_results = batch_compute_metrics(
                        _rank_codes, verbose=True,
                        progress_cb=lambda done, total, code: self._update(
                            phase_idx=4, phase="上榜+自选统一计算", done=done, total=total,
                            current=code, message=f"计算榜单完整指标 {done}/{total}"))

                    # 保存到数据库
                    _saved = 0
                    for _code, _metrics in _metrics_results.items():
                        if save_metrics_to_db(_code, _metrics):
                            _saved += 1

                    self._log(f"[阶段5/6] 榜单完整指标计算完成: 成功{len(_metrics_results)}/{len(_rank_codes)}只, 保存{_saved}只")

            except Exception as _e:
                import traceback
                self._log(f"⚠️ 榜单完整指标计算失败(不影响榜单): {_e}")
                self._log(traceback.format_exc())

            # v2.9.52: 阶段6结束，记录内存
            self._log_memory("阶段6结束")

            # ---- 阶段4(续): recompute_ad 精确重算 ad/earn/score ----
            try:
                import scripts.recompute_ad as rad
                # v2.10.0: recompute_ad 只算 ad_score/earn_score/score/yindie 精确值,
                # v2.9.44: reco/reco_days 已从pipeline移除, 由recompute_ad阶段4对上榜+自选精确计算
                rad.main(progress_cb=self._progress_cb,
                         cancel_flag=self._cancel,
                         skip_panel_overwrite=True)
            except Exception as _e:
                import traceback
                _tb = traceback.format_exc()
                self._log(f"❌ 上榜+自选统一计算阶段异常: {_e}")
                self._update(status="error",
                             message=f"上榜+自选统一计算失败: {_e}",
                             finished_at=time.time(), ok=False,
                             errors=[f"[上榜+自选统一计算阶段] {_e}\n{_tb}"])
                return
            if self._cancel.is_set():
                self._log("⏹ 已手动停止")
                self._update(status="stopped", message="已手动停止",
                             finished_at=time.time())
                return

            # v2.9.52: 阶段7结束，记录内存
            self._log_memory("阶段7结束")

            # ---- 阶段5: 同步榜单 ----
            self._update(phase_idx=5, phase="同步榜单", done=0, total=1,
                         message="正在同步榜单数据…")
            self._phase_log(5)
            # recompute_ad 已写入 rank_snapshots.meta + refresh_tscores_for_today, 此处仅刷出日志
            self._phase_log(5, "榜单同步完成 (recompute_ad 已 write tscore + meta)")

            # 收尾: 关闭连接并 checkpoint WAL(确保 fund.db 完整)
            try:
                import sqlite3
                db.close_conn()
                with sqlite3.connect(str(db.DB_PATH)) as c:
                    c.execute("PRAGMA wal_checkpoint(TRUNCATE);")
            except Exception:
                pass

            errors = result.get("errors") or []
            duration_s = round(time.time() - (self._state.get("started_at") or time.time()), 1)
            self._log(f"✅ 更新完成 · 净值 {result.get('nav_points', 0)} 条 · 失败 {len(errors)} 只 · 用时 {duration_s}s")
            
            # v2.9.7: 更新数据版本戳，通知前端所有缓存失效
            try:
                new_version = bump_data_version()
                self._log(f"🔄 数据版本戳已更新: {new_version}")
            except Exception as _ve:
                self._log(f"⚠️ 数据版本戳更新失败: {_ve}")

            # v2.11.4 C1: 后端 query 缓存(如 db.invalidate_query_cache)在此刷新,
            # 避免下一轮读到过期缓存 (原函数 0 调用者)
            try:
                db.invalidate_query_cache()
            except Exception:
                pass

            # v2.11.3 两池模型收尾(注意上下游耦合: 必须在 recompute_ad/榜单写入之后执行):
            # 1) 榜单只保留当日最新版(删更早历史行) 2) 清空"非可见基金"的展示型基础指标
            try:
                from modules.rank.ranker import prune_keep_latest_snapshots, clear_non_visible_metrics
                _pruned = prune_keep_latest_snapshots()
                self._log(f"[收尾] 榜单保留当日最新: 删除历史行 {_pruned} 条")
                _cleared = clear_non_visible_metrics()
                self._log(f"[收尾] 非可见基金展示基础指标置空: {_cleared} 只")
            except Exception as _final_e:
                self._log(f"⚠️ 收尾整理跳过(不影响主流程): {_final_e}")
            
            self._update(status="done", phase="完成", phase_idx=5,
                         done=mf, total=mf, current="",
                         message=f"更新完成 · 净值 {result.get('nav_points', 0)} 条 · 失败 {len(errors)} 只",
                         finished_at=time.time(),
                         result=result, errors=errors, ok=True)
        except Exception as e:  # noqa: BLE001
            import traceback
            _tb = traceback.format_exc()
            self._log(f"❌ 更新失败: {e}")
            self._update(status="error", message=f"更新失败: {e}",
                         finished_at=time.time(), ok=False,
                         errors=[f"[未捕获异常] {e}\n{_tb}"])
            traceback.print_exc()


# 进程内单例
_manager = FetchManager()


def get_manager() -> FetchManager:
    return _manager


