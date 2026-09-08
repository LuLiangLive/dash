"""
api/system.py —— 系统管理路由模块（v2.9.23拆分：网关→gateway_router.py，图谱→graph_router.py）

从 main.py 拆分出的系统管理相关路由，包括：
- 健康检查
- 元信息/任务日志
- 算法阈值配置
- 高危管理操作（重算/清缓存/重置快照/代码库管理）
- 一键更新任务控制

所有路由通过 APIRouter 注册，main.py 中 include_router 引入。
"""
from __future__ import annotations

import asyncio
import time
import re
from pathlib import Path

from fastapi import APIRouter, Body, Depends, Query
from pydantic import BaseModel

import db
from auth import optional_api_key, require_api_key
from config import settings
from algo_config import get_algo_config, save_algo_config, get_config_meta, invalidate_cache
from modules.logging.audit import audit_log, AuditAction

# v2.9.7: 数据版本戳管理
try:
    from data_version import get_data_version, bump_data_version
except Exception:
    def get_data_version():
        return "0"
    def bump_data_version():
        return str(int(time.time()))

router = APIRouter(tags=["系统管理"])

# ---------------------------------------------------------------------------
# 算法阈值配置（已迁移到 algo_config.py 统一管理，此处保留兼容接口）
# ---------------------------------------------------------------------------
_ALGO_CFG_DEFAULTS = {
    "pool_size": 6000,
    "min_days": 300,
    "rank_topn": 30,
    "ddown_th": -1.0,
    "stk_pct": 3.0,
    "earn_cap": 4.0,
}

_ALGO_CFG_RANGE = {
    "pool_size": (1000, 10000),
    "min_days": (60, 800),
    "rank_topn": (10, 100),
    "ddown_th": (-5.0, -0.1),
    "stk_pct": (0.5, 20.0),
    "earn_cap": (0.0, 10.0),
}

ROOT = Path(__file__).resolve().parent.parent.parent  # backend/（v2.5.5 迁入 modules/system 后需三层）


def _load_algo_cfg() -> dict:
    """从统一配置模块读取（兼容旧接口）。"""
    return get_algo_config().to_dict()


def _save_algo_cfg(cfg: dict) -> tuple[bool, str]:
    """保存到统一配置模块（兼容旧接口）。"""
    return save_algo_config(cfg)


# ---------------------------------------------------------------------------
# 一键更新任务模型
# ---------------------------------------------------------------------------
class FetchStart(BaseModel):
    """v0.47.0: 默认 funds 全量池（≥6000 只），与新全量榜口径一致
    funds_pool  : 增量抓 funds 表全量（与新榜口径一致）
    displayed   : funds_pool 的别名(旧客户端兼容)
    full_rebuild: FULL_MARKET=1 全量重建(覆盖更广)
    """
    scope: str = "funds_pool"
    max_funds: int | None = None


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------

# v2.9.7: 数据版本戳接口（用于前端缓存失效判断）
@router.get("/api/data-version", summary="获取数据版本戳", responses={200: {"description": "返回当前数据版本戳"}})
async def data_version():
    """获取当前数据版本戳。每次一键更新后版本戳会变化，前端据此判断缓存是否失效。"""
    return {"ok": True, "data_version": get_data_version()}

@router.get("/api/health", summary="健康检查", responses={200: {"description": "服务状态及版本信息"}})
async def health():
    """健康检查。"""
    return {"ok": True, "ts": time.strftime("%Y-%m-%dT%H:%M:%S+08:00"), "version": settings.app_version}


@router.get("/api/meta/updated", dependencies=[Depends(optional_api_key)], summary="获取最近更新时间", responses={200: {"description": "各数据类型最近更新时间"}})
async def meta_updated():
    """最近更新时间。"""
    return await db.ameta_updated()


@router.get("/api/meta/rank-recomputed", dependencies=[Depends(optional_api_key)], summary="榜单重算完成时间戳", responses={200: {"description": "最近一次一键更新完成时间；从未发生过返回 ts=null"}})
async def meta_rank_recomputed():
    """v2.9.4 N4：一键更新(done)写榜单重算时间戳（fetch_manager），前端轮询此值自动刷新榜单/自选。"""
    try:
        row = db.get_conn().execute(
            "SELECT value FROM settings WHERE key = 'meta.rank_recomputed_at'").fetchone()
        return {"ok": True, "ts": row[0] if row else None}
    except Exception as e:
        return {"ok": False, "msg": str(e)[:100]}


@router.get("/api/tasks", dependencies=[Depends(optional_api_key)], summary="获取任务日志", responses={200: {"description": "最近任务执行日志"}})
async def tasks(limit: int = Query(default=10)):
    """任务日志。"""
    return {"logs": await db.arecent_logs(limit)}


@router.get("/api/cfg/algo", dependencies=[Depends(optional_api_key)], summary="获取算法阈值配置", responses={200: {"description": "当前算法阈值配置"}})
async def cfg_algo_get():
    """获取算法阈值配置。"""
    return {"ok": True, "cfg": _load_algo_cfg()}


@router.get("/api/cfg/algo/meta", dependencies=[Depends(optional_api_key)], summary="获取算法配置元信息", responses={200: {"description": "配置项默认值/范围/分组/标签"}})
async def cfg_algo_meta():
    """获取算法配置元信息（默认值、范围、分组、标签），供前端设置页面使用。"""
    return {"ok": True, "meta": get_config_meta()}


@router.post("/api/cfg/algo", dependencies=[Depends(require_api_key)], summary="保存算法阈值配置", responses={200: {"description": "保存成功，返回最新配置"}})
async def cfg_algo_set(body: dict = Body(...)):
    """保存算法阈值配置。"""
    ok, msg = await asyncio.to_thread(_save_algo_cfg, body or {})
    if not ok:
        return {"ok": False, "msg": msg or "保存失败"}
    audit_log(AuditAction.ALGO_CONFIG_CHANGE, "修改算法配置", extra={"config_keys": list(body.keys()) if body else []})
    return {"ok": True, "cfg": await asyncio.to_thread(_load_algo_cfg)}


@router.post("/api/admin/recompute-all", dependencies=[Depends(require_api_key)], summary="强制重算所有榜单", responses={200: {"description": "重算任务已下发"}})
async def admin_recompute_all():
    """强制重算所有榜单(异步)。"""
    try:
        from collector import fetch_manager
        mgr = fetch_manager.get_manager()
        mgr.start(scope="funds_pool")
        audit_log(AuditAction.ADMIN_ACTION, "管理员操作: 强制重算所有榜单", extra={"action": "recompute_all"})
        return {"ok": True, "msg": "已下发全量重算任务"}
    except Exception as e:
        return {"ok": False, "msg": str(e)[:100]}


@router.post("/api/admin/clear-cache", dependencies=[Depends(require_api_key)], summary="清空净值缓存", responses={200: {"description": "缓存已清空"}})
def _do_clear_cache():
    conn = db.get_conn()
    cur = conn.execute("DELETE FROM nav_history")
    deleted = cur.rowcount
    try:
        _cache = ROOT / "data" / "fund_cache.json"
        if _cache.exists():
            _cache.unlink()
    except Exception:
        pass
    conn.commit()
    return deleted

async def admin_clear_cache():
    """清空净值缓存(nav_history 与 nav_cache 表)。"""
    try:
        deleted = await asyncio.to_thread(_do_clear_cache)
        audit_log(AuditAction.ADMIN_ACTION, f"管理员操作: 清空净值缓存 {deleted} 行", extra={"action": "clear_cache", "deleted": deleted})
        return {"ok": True, "msg": f"已删除 {deleted} 行净值(下次采集自动重建)"}
    except Exception as e:
        return {"ok": False, "msg": str(e)[:100]}


@router.post("/api/admin/reset-snaps", dependencies=[Depends(require_api_key)], summary="重置榜单快照", responses={200: {"description": "榜单快照已删除"}})
def _do_reset_snaps():
    conn = db.get_conn()
    cur = conn.execute("DELETE FROM rank_snapshots")
    deleted = cur.rowcount
    conn.commit()
    return deleted

async def admin_reset_snaps():
    """删除 rank_snapshots 全部历史快照。"""
    try:
        deleted = await asyncio.to_thread(_do_reset_snaps)
        audit_log(AuditAction.ADMIN_ACTION, f"管理员操作: 删除榜单快照 {deleted} 行", extra={"action": "reset_snaps", "deleted": deleted})
        return {"ok": True, "msg": f"已删除 {deleted} 行榜单快照"}
    except Exception as e:
        return {"ok": False, "msg": str(e)[:100]}


# ---------------------------------------------------------------------------
# 高危操作：清空全部业务数据 / 重置所有设置（v2.9.2 补齐前端 DangerZone 契约）
# ---------------------------------------------------------------------------

# clear-all-data 清空范围：业务数据表（不含 settings / audit_logs / 监控运维日志，
# 审计与运维日志 insert-only 保留，settings 由 reset-settings 单独负责）
_ALL_DATA_TABLES = [
    "funds", "nav_history", "rank_snapshots",
    "watchlist", "watch_groups", "watch_group_items",
    "portfolio", "investment_records",
    "alert_rules", "alert_history", "portfolio_profit_history",
]


def _backup_db_before_danger(tag: str) -> str:
    """高危操作前整库 SQL 快照到 data_backup/，同名前缀保留最近 5 份。

    返回快照路径；失败返回 ''（不阻断主操作，本操作语义即清库，快照只是逃生门）。
    """
    try:
        backup_dir = ROOT / "data_backup"
        backup_dir.mkdir(exist_ok=True)
        fname = backup_dir / f"{tag}_{time.strftime('%Y%m%d_%H%M%S')}.sql"
        conn = db.get_conn()
        with open(fname, "w", encoding="utf-8") as f:
            for line in conn.iterdump():
                f.write(line + "\n")
        olds = sorted(backup_dir.glob(f"{tag}_*.sql"), key=lambda p: p.name)[:-5]
        for p in olds:
            try:
                p.unlink()
            except Exception:
                pass
        return str(fname)
    except Exception:
        return ""


def _invalidate_all_caches():
    """失效统一缓存 / HTML 首页缓存 / 榜单指标进程缓存（清库或改配置后必调）。"""
    try:
        from modules.common.cache_service import cache as _cache_svc, invalidate_index_cache
        _cache_svc.invalidate_all()
        invalidate_index_cache()
    except Exception:
        pass
    try:
        from modules.rank.rank_full import clear_metrics_cache
        clear_metrics_cache()
    except Exception:
        pass


def _do_clear_all_data() -> dict:
    conn = db.get_conn()
    counts = {}
    for t in _ALL_DATA_TABLES:
        try:
            n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            conn.execute(f"DELETE FROM {t}")
            counts[t] = n
        except Exception:
            continue  # 旧库无此表则跳过
    conn.commit()
    return counts


@router.post("/api/admin/clear-all-data", dependencies=[Depends(require_api_key)], summary="清空所有业务数据", responses={200: {"description": "已清空（操作前自动整库快照）"}})
async def admin_clear_all_data():
    """清空全部基金数据/榜单快照/自选/持仓/提醒（不可恢复，操作前自动整库快照）。"""
    try:
        backup = await asyncio.to_thread(_backup_db_before_danger, "pre_clear_all_data")
        counts = await asyncio.to_thread(_do_clear_all_data)
        _invalidate_all_caches()
        total = sum(counts.values())
        audit_log(AuditAction.ADMIN_ACTION,
                  f"管理员操作: 清空全部业务数据 {total} 行 ({len(counts)} 张表)",
                  extra={"action": "clear_all_data", "tables": counts, "backup": backup})
        msg = f"已清空 {len(counts)} 张表 · {total} 行"
        if backup:
            msg += f"，快照已存 data_backup/{Path(backup).name}（保留最近5份）"
        msg += "。点『一键更新数据』重建基金/净值/榜单（自选与持仓需从快照恢复）"
        return {"ok": True, "msg": msg}
    except Exception as e:
        return {"ok": False, "msg": str(e)[:100]}


def _do_reset_settings() -> tuple:
    conn = db.get_conn()
    n_settings = conn.execute("SELECT COUNT(*) FROM settings").fetchone()[0]
    conn.execute("DELETE FROM settings")  # algo.* 覆盖项与用户 KV 全部回落代码默认
    n_groups = 0
    try:
        n_groups = conn.execute("SELECT COUNT(*) FROM watch_groups").fetchone()[0]
        conn.execute("DELETE FROM watch_group_items")
        conn.execute("DELETE FROM watch_groups")
        conn.execute("UPDATE watchlist SET group_id = 0")  # 自选保留，仅解除分组归属
    except Exception:
        pass  # 旧库无分组表时仅重置 settings
    conn.commit()
    return n_settings, n_groups


@router.post("/api/admin/reset-settings", dependencies=[Depends(require_api_key)], summary="重置所有设置", responses={200: {"description": "设置已重置（操作前自动整库快照）"}})
async def admin_reset_settings():
    """重置后端设置到默认值（算法参数回落 + 清空自选分组）；界面主题存于浏览器本地不受影响。"""
    try:
        backup = await asyncio.to_thread(_backup_db_before_danger, "pre_reset_settings")
        n_settings, n_groups = await asyncio.to_thread(_do_reset_settings)
        _invalidate_all_caches()
        invalidate_cache()  # 算法配置进程缓存回落 DEFAULT_CONFIG
        audit_log(AuditAction.ADMIN_ACTION,
                  f"管理员操作: 重置所有设置 {n_settings} 项 + 清空分组 {n_groups} 个",
                  extra={"action": "reset_settings", "settings": n_settings,
                         "groups": n_groups, "backup": backup})
        return {"ok": True,
                "msg": f"已重置 {n_settings} 项配置并清空 {n_groups} 个自选分组"
                       + (f"（快照 {Path(backup).name}）" if backup else "")
                       + "。主题/配色存于浏览器本地，可在『外观』中恢复"}
    except Exception as e:
        return {"ok": False, "msg": str(e)[:100]}


@router.get("/api/admin/event-hub-status", dependencies=[Depends(optional_api_key)], summary="事件中枢状态", responses={200: {"description": "调度器运行状态与最近一次扫描信息"}})
async def admin_event_hub_status():
    """v2.9.3 事件中枢（N2）运行状态：提醒扫描 / 资讯三档 / 最近错误。"""
    try:
        from services.event_hub import hub_status
        return {"ok": True, "hub": hub_status()}
    except Exception as e:
        return {"ok": False, "msg": str(e)[:100]}


@router.get("/api/admin/data-source-status", dependencies=[Depends(optional_api_key)], summary="数据源状态", responses={200: {"description": "同花顺和备用数据源的状态"}})
async def admin_data_source_status():
    """v2.9.10 数据源状态：同花顺可用性、是否作为主源、备用源信息。"""
    try:
        from collector.hithink_integration import get_data_source_status
        return {"ok": True, "data_source": get_data_source_status()}
    except Exception as e:
        return {"ok": False, "msg": str(e)[:100]}


@router.get("/api/admin/universe-status", summary="查看基金代码库状态", responses={200: {"description": "代码库和当日前100状态"}})
async def admin_universe_status():
    """查看基金代码库和当日前100的状态。"""
    try:
        from collector import fund_universe as _fu
        import json
        universe_data = None
        if _fu.UNIVERSE_PATH.exists():
            universe_data = json.load(open(_fu.UNIVERSE_PATH, encoding="utf-8"))
        daily_data = None
        if _fu.DAILY_TOP_PATH.exists():
            daily_data = json.load(open(_fu.DAILY_TOP_PATH, encoding="utf-8"))
        return {
            "ok": True,
            "universe": {
                "count": universe_data.get("count", 0) if universe_data else 0,
                "updated_at": universe_data.get("updated_at_str", "") if universe_data else "",
                "expired": _fu.is_universe_expired(),
                "path": str(_fu.UNIVERSE_PATH),
            },
            "daily_top100": {
                "count": daily_data.get("count", 0) if daily_data else 0,
                "updated_at": daily_data.get("updated_at_str", "") if daily_data else "",
                "date": daily_data.get("date", "") if daily_data else "",
                "expired": _fu.is_daily_top_expired(),
                "path": str(_fu.DAILY_TOP_PATH),
            },
        }
    except Exception as e:
        return {"ok": False, "msg": str(e)[:100]}


@router.post("/api/admin/rebuild-universe", dependencies=[Depends(require_api_key)], summary="重建基金代码库", responses={200: {"description": "代码库重建完成"}})
async def admin_rebuild_universe():
    """重建基金代码库(全市场C类,约6000只)。"""
    try:
        from collector import fund_universe as _fu
        pool = await asyncio.to_thread(_fu.rebuild_fund_universe, max_funds=6000, verbose=True)
        return {"ok": True, "msg": f"基金代码库重建完成: {len(pool)} 只"}
    except Exception as e:
        return {"ok": False, "msg": str(e)[:100]}


@router.post("/api/admin/refresh-daily-top", dependencies=[Depends(require_api_key)], summary="刷新当日前100", responses={200: {"description": "前100刷新完成"}})
async def admin_refresh_daily_top():
    """刷新当日日涨幅前100 C类基金。"""
    try:
        from collector import fund_universe as _fu
        if _fu.DAILY_TOP_PATH.exists():
            _fu.DAILY_TOP_PATH.unlink()
        funds = await asyncio.to_thread(_fu.ensure_daily_top100, verbose=True)
        return {"ok": True, "msg": f"当日前100刷新完成: {len(funds)} 只"}
    except Exception as e:
        return {"ok": False, "msg": str(e)[:100]}


@router.get("/api/fetch/status", dependencies=[Depends(optional_api_key)], summary="获取更新任务状态", responses={200: {"description": "当前任务状态及最近日志"}})
async def fetch_status():
    """当前更新任务状态(轮询用)。"""
    from collector.fetch_manager import get_manager
    st = get_manager().status()
    logs = await db.arecent_logs(3)
    latest = await db.ameta_updated()
    return {
        "state": st,
        "logs": logs,
        "latest_task_at": latest.get("latest_task_at"),
        "latest_task": latest.get("latest_task_at"),  # 兼容老 renderRecent
        "latest_task_msg": latest.get("latest_task_msg"),
        "latest_rank_date": latest.get("latest_rank_date"),
        "latest_nav_date": latest.get("latest_nav_date"),
        "total_funds": latest.get("total_funds"),
        "updated_funds": latest.get("updated_funds"),
        "update_ratio": latest.get("update_ratio"),
    }


@router.post("/api/fetch/start", dependencies=[Depends(optional_api_key)], summary="触发一键更新", responses={200: {"description": "更新任务已启动"}})
async def fetch_start(body: FetchStart):
    """触发一键更新(funds 全量池 → 抓净值 → 全量重排 10 sub → 算信号 → 同步)。"""
    from collector.fetch_manager import get_manager
    mgr = get_manager()
    return await asyncio.to_thread(mgr.start, scope=body.scope, max_funds=body.max_funds)


@router.post("/api/fetch/stop", dependencies=[Depends(optional_api_key)], summary="停止更新任务", responses={200: {"description": "任务已停止"}})
async def fetch_stop():
    """停止当前更新任务。"""
    from collector.fetch_manager import get_manager
    mgr = get_manager()
    return await asyncio.to_thread(mgr.stop)


# ---------------------------------------------------------------------------
# v2.9.15: 功能神经网络图谱
# ---------------------------------------------------------------------------


CHANGE_HISTORY_PATH = ROOT / "docs" / "change_history.json"
FUNCTION_GRAPH_PATH = ROOT / "docs" / "function_graph.json"
GATEWAY_CONFIG_PATH = ROOT / "docs" / "gateway_config.json"  # v2.9.23: 网关配置（架构映射+任务模板）


def _load_gateway_config():
    """v2.9.23: 加载网关配置文件，失败时返回空配置"""
    import json
    if GATEWAY_CONFIG_PATH.exists():
        try:
            return json.loads(GATEWAY_CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"node_architecture_map": {}, "task_templates": {}, "tech_stack": {}}


def _load_json(path):
    """加载JSON文件"""
    import json
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return None


def _save_json(path, data):
    """保存JSON文件"""
    import json
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


class ChangeAnalyzeRequest(BaseModel):
    target_node_id: str
    title: str
    description: str = ""


class ChangeCreateRequest(BaseModel):
    title: str
    description: str = ""
    target_node_id: str
    impact_analysis: dict = {}
    task_list: list = []


class ChangeUpdateRequest(BaseModel):
    change_id: str
    status: str | None = None
    task_updates: list | None = None  # [{task_id, completed}]
    task_additions: list | None = None  # v2.9.23: 动态添加任务 [{description, category, priority}]
    task_removals: list | None = None  # v2.9.23: 动态删除任务 [task_id]


class ChangeGatewayRequest(BaseModel):
    """v2.9.20: 变更准入网关请求"""
    title: str
    change_type: str = "feature_update"  # feature_update/new_feature/bugfix/refactor
    priority: str = "P2"  # P0/P1/P2/P3
    target_node_ids: list = []  # 涉及的节点ID列表
    description: str = ""


# ---------------------------------------------------------------------------
# v2.9.34: Mine 页面可视化管理卡片 —— UI质量 / 变更流程 / 设计命令
# ---------------------------------------------------------------------------

PROJECT_ROOT = ROOT.parent  # 项目根（backend/ 的上一级）


def _read_json_safe(path: Path):
    """安全读取 JSON，失败返回 None"""
    import json
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None


@router.get("/api/ui-check/latest", dependencies=[Depends(optional_api_key)],
            summary="最新UI检测结果摘要", responses={200: {"description": "UI质量检测摘要"}})
async def ui_check_latest(source: str = Query("build", description="数据来源: build=构建时检测, live=实时检测, all=合并")):
    """读取UI检测结果，返回按优先级统计的摘要。支持source参数选择数据来源。"""
    try:
        gate_mode_path = PROJECT_ROOT / ".ui-check" / "gate-mode.json"
        gate_data = _read_json_safe(gate_mode_path)
        gate_mode = "观察"
        if gate_data:
            gate_mode = gate_data.get("mode", gate_data.get("gate_mode", "观察"))

        def _empty_result(src):
            return {
                "success": True,
                "data": {
                    "source": src,
                    "latest_check_time": None,
                    "total_issues": 0,
                    "by_priority": {"P0": 0, "P1": 0, "P2": 0, "P3": 0},
                    "gate_mode": gate_mode,
                    "recent_build_summary": "暂无检测数据",
                },
            }

        def _build_from_issues(issues, latest_time, src, summary_prefix):
            by_priority = {"P0": 0, "P1": 0, "P2": 0, "P3": 0}
            for issue in issues:
                p = issue.get("priority", "P3")
                if p in by_priority:
                    by_priority[p] += 1
                t = issue.get("detected_at", "")
                if t and t > latest_time:
                    latest_time = t
            total = len(issues)
            summary = f"{summary_prefix}：P0={by_priority['P0']}, P1={by_priority['P1']}, 共{total}个问题"
            return {
                "success": True,
                "data": {
                    "source": src,
                    "latest_check_time": latest_time or None,
                    "total_issues": total,
                    "by_priority": by_priority,
                    "gate_mode": gate_mode,
                    "recent_build_summary": summary,
                },
            }

        if source == "build":
            ui_check_path = PROJECT_ROOT / ".ui-check" / "latest-merged.json"
            data = _read_json_safe(ui_check_path)
            if not data or not data.get("issues"):
                return _empty_result("build")
            return _build_from_issues(data["issues"], "", "build", "最近构建检测")

        if source == "live":
            live_path = PROJECT_ROOT / ".ui-check" / "live-latest.json"
            data = _read_json_safe(live_path)
            if not data or not data.get("issues"):
                return _empty_result("live")
            issues = data["issues"]
            latest_time = data.get("timestamp", "")
            return _build_from_issues(issues, latest_time, "live", "最近Live检测")

        if source == "all":
            build_path = PROJECT_ROOT / ".ui-check" / "latest-merged.json"
            live_path = PROJECT_ROOT / ".ui-check" / "live-latest.json"
            build_data = _read_json_safe(build_path)
            live_data = _read_json_safe(live_path)

            merged_issues = []
            latest_time = ""

            if build_data and build_data.get("issues"):
                merged_issues.extend(build_data["issues"])
            if live_data and live_data.get("issues"):
                for issue in live_data["issues"]:
                    issue_copy = dict(issue)
                    issue_copy["source"] = "live"
                    merged_issues.append(issue_copy)
                lt = live_data.get("timestamp", "")
                if lt > latest_time:
                    latest_time = lt

            if not merged_issues:
                return _empty_result("all")

            return _build_from_issues(merged_issues, latest_time, "all", "构建+Live合并检测")

        ui_check_path = PROJECT_ROOT / ".ui-check" / "latest-merged.json"
        data = _read_json_safe(ui_check_path)
        if not data or not data.get("issues"):
            return _empty_result("build")
        return _build_from_issues(data["issues"], "", "build", "最近构建检测")

    except Exception as e:
        return {"success": False, "error": str(e)[:200]}




@router.get("/api/ui-live/auto-status", dependencies=[Depends(optional_api_key)],
            summary="Live检测自动触发状态", responses={200: {"description": "Live检测自动触发状态"}})
async def ui_live_auto_status():
    """返回当前Live检测自动触发状态，包括后台监听进程状态和最近检测结果。"""
    import json as _json
    import os as _os

    pid_file = PROJECT_ROOT / ".ui-check" / "live-watch.pid"
    latest_file = PROJECT_ROOT / ".ui-check" / "live-latest.json"

    result = {
        "success": True,
        "data": {
            "mode": "none",
            "background_running": False,
            "pid": None,
            "start_time": None,
            "last_detection_time": None,
            "problem_count": 0,
            "by_priority": {"P0": 0, "P1": 0, "P2": 0, "P3": 0},
            "checked_file": None
        }
    }

    # 读取pid文件，检查后台进程状态
    pid_data = _read_json_safe(pid_file)
    if pid_data:
        pid = pid_data.get("pid")
        result["data"]["pid"] = pid
        result["data"]["start_time"] = pid_data.get("start_time")
        # 检查进程是否在运行
        running = False
        if pid:
            try:
                if _os.name == "nt":
                    import subprocess as _sp
                    _sp.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                            capture_output=True, timeout=5)
                    out = _sp.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                                   capture_output=True, text=True, timeout=5)
                    running = str(pid) in out.stdout
                else:
                    _os.kill(pid, 0)
                    running = True
            except Exception:
                running = False
        result["data"]["background_running"] = running
        if running:
            result["data"]["mode"] = "complex"

    # 读取最近检测结果
    latest = _read_json_safe(latest_file)
    if latest:
        result["data"]["last_detection_time"] = latest.get("timestamp")
        result["data"]["checked_file"] = latest.get("checked_file")
        issues = latest.get("issues", [])
        result["data"]["problem_count"] = len(issues)
        by_pri = {"P0": 0, "P1": 0, "P2": 0, "P3": 0}
        for issue in issues:
            p = issue.get("priority", "P3")
            if p in by_pri:
                by_pri[p] += 1
        result["data"]["by_priority"] = by_pri
        # 如果有检测结果但没有后台运行，说明是简单变更模式
        if not result["data"]["background_running"] and len(issues) > 0:
            result["data"]["mode"] = "simple"

    return result

@router.get("/api/change-workflow/status", dependencies=[Depends(optional_api_key)],
            summary="变更流程状态", responses={200: {"description": "当前变更流程状态"}})
async def change_workflow_status():
    """读取 .change-workflow-state.json + change_history.json，返回流程状态摘要。"""
    try:
        state_path = PROJECT_ROOT / ".change-workflow-state.json"
        state = _read_json_safe(state_path)

        # 7 阶段定义（与 scripts/change-workflow.mjs 一致）
        stage_names = ["brainstorming", "writing-plans", "git-worktrees",
                       "subagent-dev", "code-review", "verification", "finishing"]
        stage_completion = {name: 0 for name in stage_names}

        active_count = 0
        if state:
            current_stage = state.get("current_stage", 0)
            stages = state.get("stages", {})
            # 进行中的变更：current_stage 在 1-7 之间
            if 1 <= current_stage <= 7:
                active_count = 1
            # 统计各阶段完成情况
            for idx, name in enumerate(stage_names, start=1):
                s = stages.get(str(idx), stages.get(idx, {}))
                if isinstance(s, dict) and s.get("status") == "completed":
                    stage_completion[name] = 100
                elif isinstance(s, dict) and s.get("status") == "in_progress":
                    # 检查清单完成度
                    checklist = s.get("checklist", {})
                    if isinstance(checklist, dict) and checklist:
                        done = sum(1 for v in checklist.values() if v)
                        stage_completion[name] = int(done / len(checklist) * 100)
                    else:
                        stage_completion[name] = 50

        # 从 change_history.json 读取最近5条已完成变更
        recent_changes = []
        hist = _read_json_safe(CHANGE_HISTORY_PATH)
        if hist and isinstance(hist, dict):
            changes = hist.get("changes", [])
            if isinstance(changes, list):
                for c in changes[:5]:
                    if isinstance(c, dict):
                        recent_changes.append({
                            "id": c.get("id", ""),
                            "title": c.get("title", ""),
                            "created_at": c.get("created_at", ""),
                            "status": c.get("status", "completed"),
                            "change_type": c.get("change_type", ""),
                        })

        message = "暂无进行中的变更流程" if active_count == 0 else f"当前有 {active_count} 个变更进行中"

        # 自动触发相关字段（v2.9.36）
        trigger_type = "manual"
        auto_tools_called = []
        auto_trigger_compliance = "not_applicable"
        if state:
            trigger_type = state.get("trigger_type", "manual")
            auto_tools_called = state.get("auto_tools_called", []) or []
            if trigger_type == "auto":
                stages_data = state.get("stages", {})
                has_auto_actions = any(
                    isinstance(s, dict) and s.get("auto_actions")
                    for s in stages_data.values()
                )
                auto_trigger_compliance = "compliant" if has_auto_actions else "pending"

        return {
            "success": True,
            "data": {
                "active_count": active_count,
                "recent_changes": recent_changes,
                "stage_completion_rate": stage_completion,
                "message": message,
                "trigger_type": trigger_type,
                "auto_tools_called": auto_tools_called,
                "auto_trigger_compliance": auto_trigger_compliance,
            },
        }
    except Exception as e:
        return {"success": False, "error": str(e)[:200]}




@router.get("/api/change-workflow/auto-check", dependencies=[Depends(optional_api_key)],
            summary="变更自动触发合规性检查", responses={200: {"description": "自动触发合规性评分与检查结果"}})
async def change_workflow_auto_check():
    """读取 .change-workflow-state.json，检查自动触发合规性（与 change-workflow.mjs auto-check 逻辑一致）。"""
    try:
        state_path = PROJECT_ROOT / ".change-workflow-state.json"
        state = _read_json_safe(state_path)

        if not state or state.get("current_stage", 0) == 0:
            return {
                "success": True,
                "data": {
                    "compliance_score": 0,
                    "trigger_type": "none",
                    "passed_checks": [],
                    "failed_checks": ["无进行中的变更"],
                    "recommendations": ["请先启动变更流程"],
                    "message": "暂无进行中的变更",
                },
            }

        passed = []
        failed = []
        recommendations = []

        # 检查1: trigger_type 是否存在
        trigger_type = state.get("trigger_type", "")
        if trigger_type:
            passed.append("trigger_type 已记录")
        else:
            failed.append("trigger_type 缺失")
            recommendations.append("启动变更时应使用 --auto 参数记录触发方式")

        # 检查2: 是否有阶段使用 auto_actions
        has_auto_actions = False
        auto_action_count = 0
        for s in (state.get("stages") or {}).values():
            if isinstance(s, dict):
                actions = s.get("auto_actions") or []
                if actions:
                    has_auto_actions = True
                    auto_action_count += len(actions)
        if has_auto_actions:
            passed.append(f"存在自动操作记录 (共{auto_action_count}项)")
        else:
            failed.append("无自动操作记录")
            recommendations.append("自动触发流程应在每个阶段记录 auto_actions")

        # 检查3: 每阶段是否有 complete 记录
        completed_stages = 0
        total_started = 0
        for idx in range(1, 8):
            s = (state.get("stages") or {}).get(str(idx), (state.get("stages") or {}).get(idx))
            if isinstance(s, dict) and s.get("status") != "pending":
                total_started += 1
                if s.get("status") == "completed":
                    completed_stages += 1
        if total_started > 0 and completed_stages == total_started:
            passed.append(f"所有已开始阶段均已完成 ({completed_stages}/{total_started})")
        elif total_started > 0:
            failed.append(f"存在未完成阶段 ({completed_stages}/{total_started})")
            recommendations.append("每个阶段完成后应调用 complete 更新状态")
        else:
            failed.append("无进行中的阶段")

        # 检查4: 是否调用了 finish (complete 7)
        if state.get("current_stage") == 8:
            passed.append("已调用 complete 7 完成流程")
        else:
            failed.append("尚未完成全部7阶段")
            recommendations.append("变更完成后应调用 complete 7 收尾")

        compliance_score = round(len(passed) / 4 * 100)

        return {
            "success": True,
            "data": {
                "compliance_score": compliance_score,
                "trigger_type": trigger_type or "unknown",
                "passed_checks": passed,
                "failed_checks": failed,
                "recommendations": recommendations,
            },
        }
    except Exception as e:
        return {"success": False, "error": str(e)[:200]}

@router.get("/api/design-commands", dependencies=[Depends(optional_api_key)],
            summary="impeccable设计命令列表", responses={200: {"description": "23条设计命令结构化数据"}})
async def design_commands():
    """解析 docs/impeccable-design-commands.md，返回结构化命令列表。"""
    try:
        # 优先项目根 docs/，其次 backend/docs/
        doc_path = PROJECT_ROOT / "docs" / "impeccable-design-commands.md"
        if not doc_path.exists():
            doc_path = ROOT / "docs" / "impeccable-design-commands.md"
        if not doc_path.exists():
            return {"success": True, "data": {"total": 0, "categories": [], "commands": []}}

        text = doc_path.read_text(encoding="utf-8")
        lines = text.split("\n")

        # 分类映射：根据 ## 标题中的分类名
        category_map = {
            "Create": "创建",
            "Evaluate": "评估",
            "Refine": "精炼",
            "Simplify": "简化",
            "Harden": "加固",
            "System": "系统",
            "Setup": "初始化",
        }

        commands = []
        categories = []
        current_category = ""
        current_cmd = None

        for line in lines:
            # 匹配 ## 一、Create（创建）— 2 条 这样的分类标题
            m = re.match(r"^##\s+[一二三四五六七八九十]+、(\w+)", line)
            if m:
                cat_en = m.group(1)
                current_category = category_map.get(cat_en, cat_en)
                if current_category not in categories:
                    categories.append(current_category)
                continue

            # 匹配 ### 1. `/impeccable` 这样的命令标题
            m = re.match(r"^###\s+(\d+)\.\s+`(/\w+)`", line)
            if m:
                if current_cmd:
                    commands.append(current_cmd)
                current_cmd = {
                    "id": int(m.group(1)),
                    "title": m.group(2),
                    "category": current_category,
                    "description": "",
                    "scenario": "",
                }
                continue

            # 收集命令内容
            if current_cmd:
                stripped = line.strip()
                if stripped.startswith("【原版内容】"):
                    continue
                if stripped.startswith("【中文注释】"):
                    continue
                if stripped.startswith(">"):
                    # 引用块是原版描述
                    desc = stripped.lstrip("> ").strip()
                    if desc and not current_cmd["description"]:
                        current_cmd["description"] = desc
                elif stripped and not stripped.startswith("---") and not stripped.startswith("#"):
                    # 中文注释行作为适用场景
                    if not current_cmd["scenario"] and len(stripped) > 5:
                        current_cmd["scenario"] = stripped[:120]

        if current_cmd:
            commands.append(current_cmd)

        return {
            "success": True,
            "data": {
                "total": len(commands),
                "categories": categories,
                "commands": commands,
            },
        }
    except Exception as e:
        return {"success": False, "error": str(e)[:200]}
