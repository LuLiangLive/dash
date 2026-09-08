"""
main.py —— FastAPI 服务入口（v2.1.3 架构精简版）

职责：仅负责 FastAPI 应用组装（创建 app、注册中间件、挂载静态文件、注册路由）。
业务逻辑已全部拆分到 services/ 和 api/ 层：
- 抗跌详情 → services/anti_detail.py
- 双基金对比 → services/compare.py
- 净值序列 → services/nav_series.py
- 超额收益 → services/excess.py

运行: uvicorn main:app --host 0.0.0.0 --port 8002
"""
from __future__ import annotations

# ══════════════════════════════════════════════════════════════════════
# 云端部署安全强制项（必须在导入任何业务模块之前执行——auth/config 在
# 模块导入时即读取以下环境变量，决定是否校验 X-API-Key）：
#   REQUIRE_READ_KEY=1 → 全站接口（含读）均需 X-API-Key
#   API_KEYS=…         → 固定私密密钥（不回退公开默认值）
# 无论平台以 run_server.py 或 uvicorn main:app 启动，本段都最先生效。
# ══════════════════════════════════════════════════════════════════════
import os

os.environ.setdefault("REQUIRE_READ_KEY", "0")

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from fastapi import FastAPI, Depends, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

import re

class CachedStaticFiles(StaticFiles):
    """带缓存控制的静态文件服务：带hash的资源长期缓存，其他no-cache"""
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if re.search(r'-[A-Za-z0-9_-]{6,}\.(js|css)$', path):
            response.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
        else:
            response.headers['Cache-Control'] = 'no-cache'
        return response
from pydantic import BaseModel

import db
from modules.market import market_service as market  # 市场行情模块初始化
from auth import optional_api_key, require_api_key
from modules.common.cache_service import get_index_cache, set_index_cache
# v2.1.3: 业务逻辑从 main.py 拆分到 services 层
from modules.anti.anti_detail import anti_detail_sync as _anti_detail_service
from modules.common.compare import watch_compare_sync as _watch_compare_service
from security.rate_limiter import setup_rate_limiter, limiter
from security.headers import setup_security_headers
from security.crypto import init_crypto

# ---------------------------------------------------------------------------
# OpenAPI / Swagger 文档配置
# ---------------------------------------------------------------------------
API_TAGS_METADATA = [
    {
        "name": "基金基础信息",
        "description": "基金列表、基础信息、净值历史、持仓主题与重仓股、轻量档案等基金数据查询接口。",
    },
    {
        "name": "榜单查询",
        "description": "日榜/推荐榜/预警榜等多面板榜单查询，以及历史榜单日期列表。",
    },
    {
        "name": "自选基金管理",
        "description": "自选基金的增删改查、整体覆盖同步，以及线上版兼容别名接口。",
    },
    {
        "name": "自选实时获取",
        "description": "单只/批量基金实时详情获取，含统一评分口径与近21日净值走势。",
    },
    {
        "name": "自选分组管理",
        "description": "自选基金分组的创建、编辑、删除，以及分组成员的添加、移除、排序和跨组移动。",
    },
    {
        "name": "多设备数据同步",
        "description": "自选、分组、持仓、提醒、设置等数据的导出与导入，用于多设备间本地同步。",
    },
    {
        "name": "持仓盈亏管理",
        "description": "持仓列表、投资记录CRUD、交易截图上传、Excel/CSV导入、持仓收益历史走势。",
    },
    {
        "name": "提醒通知管理",
        "description": "价格/评分/信号/收益提醒规则的CRUD、提醒历史查询与已读标记、提醒检查触发。",
    },
    {
        "name": "基金评分历史",
        "description": "基金综合分/抗跌分/收益分的历史快照查询。",
    },
    {
        "name": "市场与资讯",
        "description": "A股/韩股/美股指数行情、降噪资讯、指数技术研判、市场仪表盘（涨跌统计/行业热力/资金流向/风格轮动/题材/北向/分级研判）。",
    },
    {
        "name": "板块轮动分析",
        "description": "板块数据列表、板块成分股、板块历史走势数据。",
    },
    {
        "name": "数据导出",
        "description": "自选列表、持仓收益、榜单数据的CSV/XLSX格式导出。",
    },
    {
        "name": "系统管理",
        "description": "健康检查、元信息/任务日志、算法阈值配置、高危管理操作（重算/清缓存/重置快照/代码库管理）、一键更新任务控制。",
    },
    {
        "name": "监控告警",
        "description": "APM性能监控（响应时间/慢查询/错误率）、错误日志管理、数据更新状态、系统资源监控、告警确认管理。",
    },
    {
        "name": "核心业务接口",
        "description": "抗跌详情、双基金对比、基金页服务端渲染等核心业务接口。",
    },
]

# v2.8.0: 结构化日志系统初始化（在 app 创建之前）
from logging_config import setup_logging, get_logger
setup_logging()
logger = get_logger("main")

app = FastAPI(
    title="投研看板 API",
    description=(
        "投研看板（Touyan Board）后端 API 服务。\n\n"
        "## 功能概览\n\n"
        "- **基金数据**：基金基础信息、净值历史、持仓主题与重仓股\n"
        "- **榜单系统**：日榜/推荐榜/预警榜多面板榜单，支持按板块筛选\n"
        "- **自选管理**：自选基金增删改查、分组管理、多设备同步\n"
        "- **持仓管理**：投资记录CRUD、实时盈亏、收益走势、Excel导入\n"
        "- **提醒系统**：价格/评分/信号/收益多维度提醒规则\n"
        "- **市场行情**：A股/韩股/美股指数、行业热力、资金流向、北向资金\n"
        "- **系统监控**：APM性能监控、错误日志、系统资源、告警管理\n\n"
        "## 认证说明\n\n"
        "部分写操作接口需要 API Key 认证（通过 `X-API-Key` 请求头传递），"
        "读操作接口支持可选 API Key。\n\n"
        "## 在线调试\n\n"
        "本页面支持直接在线调试所有 API：点击任意接口 → **Try it out** → 填写参数 → **Execute**。"
    ),
    version="2.11.4",
    contact={
        "name": "投研看板开发团队",
        "url": "https://github.com/touyan-board",
    },
    license_info={
        "name": "MIT License",
        "url": "https://opensource.org/licenses/MIT",
    },
    openapi_tags=API_TAGS_METADATA,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    swagger_ui_parameters={
        "defaultModelsExpandDepth": -1,
        "docExpansion": "none",
        "filter": True,
        "showExtensions": False,
        "operationsSorter": "method",
        "tagsSorter": "alpha",
    },
)
# GZip 压缩响应(首页 500KB HTML → ~80KB,隧道/弱网明显提速)
app.add_middleware(GZipMiddleware, minimum_size=200)

# v2.8.0: structured logging middleware
try:
    from modules.logging.middleware import RequestLoggingMiddleware
    app.add_middleware(RequestLoggingMiddleware)
except Exception as _e:
    logger.warning("request logging middleware init failed: %s", _e)

# v3.0: 监控告警模块 - APM 性能监控中间件（在 GZip 之后，记录实际响应时间）
try:
    from modules.monitor.middleware import APMMiddleware
    app.add_middleware(APMMiddleware)
except Exception:
    pass  # 监控模块加载失败不影响主应用

# v2.7.1: 安全加固 - 初始化加密模块、配置限流和安全响应头
try:
    init_crypto()
    setup_rate_limiter(app)
    setup_security_headers(app)
except Exception as _e:
    import logging
    logging.getLogger("main").warning("安全模块初始化失败: %s", _e)

# v3.0: 监控告警模块 - 全局错误处理中间件（最外层，捕获所有未处理异常）
try:
    from modules.monitor.error_handler import GlobalErrorHandlerMiddleware
    app.add_middleware(GlobalErrorHandlerMiddleware)
except Exception:
    pass


# ---------------------------------------------------------------------------
# 启动事件：后台预计算批量评分缓存（方案C+）
# ---------------------------------------------------------------------------
@app.on_event("startup")
async def startup_event():
    """服务启动时：1) 自动迁移业务表(幂等)；2) 后台预计算评分缓存等。"""
    # ── v2.8.1 发布修复①：自动迁移业务表（幂等 CREATE TABLE IF NOT EXISTS）──
    # 云端旧库未执行 v2.8.1 迁移时，持仓/提醒等表缺失会导致 500 no such table；
    # 每次启动自动补齐，发布/重建实例不再需要手工 migrate_db.py。
    try:
        import migrate_db
        migrate_db.migrate()
        logger.info("启动迁移完成：业务表已就绪（%s）", migrate_db.DB_PATH)
    except Exception as _mig_err:
        logger.warning("启动自动迁移失败（不影响服务启动，请检查 DB_PATH）: %s", _mig_err)

    # v2.9.60: 核心数据自动导入（部署包包含fund_core_data.db）
    # 第一次启动时，如果nav_history表为空，自动从fund_core_data.db导入净值数据和基金基本信息
    # 到新平台后无需重新拉取大量数据，只需要走一遍一键更新即可
    try:
        from modules.common.core_data_importer import import_core_data
        _imported = import_core_data()
        if _imported:
            logger.info("核心数据导入完成：净值数据和基金基本信息已从fund_core_data.db导入")
    except Exception as _import_err:
        logger.warning("核心数据导入失败（不影响服务启动）: %s", _import_err)

    import threading
    def _precompute():
        try:
            from modules.score.score_service import compute_pool_scores
            results = compute_pool_scores(force=True)
        except Exception as e:
            pass  # 预计算失败不影响服务启动
    threading.Thread(target=_precompute, daemon=True).start()

    # v2.6.0: 缓存预热（独立线程，与评分预计算并行）
    def _preheat():
        try:
            from modules.common.cache_service import preheat_cache
            preheat_cache()
        except Exception:
            pass  # 预热失败不影响服务启动
    threading.Thread(target=_preheat, daemon=True).start()

    # v3.0: 监控告警模块 - 启动系统资源采集后台线程（每60秒采集一次）
    try:
        from modules.monitor.service import start_metrics_collection
        start_metrics_collection()
    except Exception:
        pass  # 监控采集启动失败不影响服务

    # v3.1: 多数据源架构 - 注册第三方数据源并启动监控
    try:
        from modules.datasource.manager import get_manager
        from modules.datasource.eastmoney import EastMoneySource
        from modules.datasource.danjuan import DanjuanSource
        from modules.datasource.monitor import get_monitor
        mgr = get_manager()
        mgr.register(EastMoneySource(), priority=0)
        mgr.register(DanjuanSource(), priority=1)
        # 启动数据源可用性监控（每5分钟检查一次）
        mon = get_monitor()
        if not mon.is_running:
            mon.start(check_interval=300)
    except Exception as _dse:
        import logging
        logging.getLogger('main').warning('多数据源初始化失败: %s', _dse)

    # v2.9.3: 服务端事件中枢（矩阵 N2）——提醒扫描 + 资讯三档调度
    try:
        from services.event_hub import start_event_hub
        if start_event_hub():
            logger.info("事件中枢已启动（提醒扫描+资讯三档，状态见 /api/admin/event-hub-status）")
        else:
            logger.info("事件中枢未启动（EVENT_HUB=0 或已在运行）")
    except Exception as _ehe:
        logger.warning("事件中枢启动失败（不影响服务）: %s", _ehe)


# ---------------------------------------------------------------------------
# 静态文件同域托管
# ---------------------------------------------------------------------------

STATIC_DIR = ROOT / "static"
if STATIC_DIR.exists():
    app.mount("/static", CachedStaticFiles(directory=str(STATIC_DIR)), name="static")

# 上传文件目录
UPLOAD_DIR = ROOT / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)
app.mount("/uploads", StaticFiles(directory=str(UPLOAD_DIR)), name="uploads")

# ---------------------------------------------------------------------------
# 路由模块注册
# ---------------------------------------------------------------------------
from modules.market.market_router import router as market_router
# v2.9.23: system router拆分为三个：系统杂项+网关+图谱
from modules.system.router import router as system_router
from modules.system.gateway_router import router as gateway_router
from modules.system.graph_router import router as graph_router
from modules.rank.router import router as ranks_router
from modules.fund.router import router as funds_router
# watchlist_sync 需一并导入：main.py 的 /api/watch/sync 别名为
# 前端 localStorage 自选同步入口，此前仅导入 router 导致调用时 NameError(500)
from modules.watchlist.watch import router as watch_router, WatchSync, watchlist_sync
from modules.score.router import router as score_router
from modules.watchlist.watch_fetch import router as watch_fetch_router
# v2.5.5 架构重构：按领域模块重组
from modules.watchlist.router import router as groups_router
from modules.portfolio.router import router as portfolio_router
from modules.alert.router import router as alerts_router
from modules.market.router import router as sectors_router
from modules.market.export import router as export_router
from modules.watchlist.sync import router as sync_router
# v3.1: 多数据源架构路由
try:
    from modules.datasource.router import router as datasource_router
except Exception:
    datasource_router = None
# v3.0: 监控告警模块路由
try:
    from modules.monitor.router import router as monitor_router
except Exception:
    monitor_router = None

# v2.8.1: 抗跌详情路由（修复前端 /api/fund/anti_detail 500错误）
try:
    from modules.anti.router import router as anti_router
except Exception:
    anti_router = None

app.include_router(market_router)




app.include_router(system_router)
app.include_router(gateway_router)
app.include_router(graph_router)
app.include_router(ranks_router)
app.include_router(funds_router)
app.include_router(watch_router)
app.include_router(score_router)
app.include_router(watch_fetch_router)
# ── v2.8.1 发布修复②：为缺失鉴权的业务路由整体补挂强制校验（X-Touyan-Key）──
# 此前 portfolio/alerts/groups/sectors/export/logs 从未挂任何依赖，匿名可读持仓/提醒/
# 分组/板块/导出/日志；统一在 include 处用 dependencies 强制 require_api_key。
# 注：/api/logs/* 此前由本文件在下方单独 include，一并补挂。
app.include_router(groups_router, dependencies=[Depends(require_api_key)])
app.include_router(portfolio_router, dependencies=[Depends(require_api_key)])
app.include_router(alerts_router, dependencies=[Depends(require_api_key)])
app.include_router(sectors_router, dependencies=[Depends(require_api_key)])
app.include_router(export_router, dependencies=[Depends(require_api_key)])
# 数据同步（备份导出/导入）同属业务，禁止匿名访问备份数据
app.include_router(sync_router, dependencies=[Depends(require_api_key)])

# v3.1: 多数据源架构路由注册
if datasource_router is not None:
    app.include_router(datasource_router)

# v3.0: 监控告警模块路由注册
if monitor_router is not None:
    app.include_router(monitor_router)

# v2.8.1: 抗跌详情路由注册
if anti_router is not None:
    app.include_router(anti_router)

# v2.8.1 FIX: 日志管理路由注册（此前 /api/logs/* 从未挂载，前端「日志管理」页全部 404）
# 发布修复②：logs 同步补挂强制鉴权——日志含请求/审计记录，禁止匿名读取
try:
    from modules.logging.router import router as logs_router
except Exception:
    logs_router = None
if logs_router is not None:
    app.include_router(logs_router, dependencies=[Depends(require_api_key)])


# ---------------------------------------------------------------------------
# 首页渲染（注入 WATCH_DATA，线上版 watch_js 依赖 window.WATCH_DATA）
# ---------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
async def index():
    # v0.95.1 perf: 首页渲染缓存（30秒TTL），避免每次请求都重新渲染
    cached_html = get_index_cache()
    if cached_html is not None:
        return Response(cached_html, media_type="text/html; charset=utf-8",
                        headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    # 注入 WATCH_DATA(自选基金详情)
    # v0.76.2 perf: 批量查询替代 N+1(12只自选从48次SQL降到3次),失败自动回退旧逻辑
    try:
        watch_data = await db.alist_watchlist_detail()
    except Exception:
        watch_data = {}
        for w in db.list_watchlist():
            f = db.get_fund(w["code"])
            if f:
                f["_v"] = 2
                f["yindie"] = f.get("yindie")
                code = w["code"]
                try:
                    conn = db.get_conn()
                    # v2.11.4 C2/Q8: board_days 全链路下线, 删除 30 天 COUNT DISTINCT
                    ld_row = conn.execute(
                        "SELECT MAX(date) as d FROM rank_snapshots WHERE code=?", (code,)
                    ).fetchone()
                    if ld_row and ld_row["d"]:
                        tag_rows = conn.execute(
                            "SELECT sub, rank FROM rank_snapshots WHERE code=? AND date=? AND panel='day' ORDER BY sub",
                            (code, ld_row["d"])
                        ).fetchall()
                        tags = [f"{r['sub']}#{r['rank']}" for r in tag_rows if r["sub"] and r["rank"]]
                        f["rtag"] = " ".join(tags[:5]) if tags else None
                    else:
                        f["rtag"] = None
                except Exception:
                    f["rtag"] = None
                watch_data[w["code"]] = f
    html = html.replace("__WATCH_DATA_JSON__", json.dumps(watch_data, ensure_ascii=False))
    html = html.replace("__STATIC_NAV_JSON__", "null")   # 动态版无内嵌净值,前端走 API
    html = html.replace("__NOW__", time.strftime("%Y-%m-%d %H:%M:%S"))
    # no-cache: 防止浏览器缓存旧版首页
    set_index_cache(html)
    return Response(html, media_type="text/html; charset=utf-8",
                    headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------

class WatchCompare(BaseModel):
    codes: list[str]


# ---------------------------------------------------------------------------
# 核心 API 端点（业务逻辑在 services 层）
# ---------------------------------------------------------------------------

# 抗跌详情(弹窗数据源,字段与线上版一致)
@app.get("/api/anti/{code}", dependencies=[Depends(optional_api_key)], tags=["核心业务接口"],
         summary="获取基金抗跌详情",
         responses={
             200: {"description": "抗跌详情数据（含最大回撤、抗跌评分、下行风险等指标）"},
             404: {"description": "基金不存在"},
             429: {"description": "请求过于频繁（限流60次/分钟）"},
         })
@limiter.limit("60/minute")
async def anti_detail(request: Request, code: str):
    """获取指定基金的抗跌详情数据。

    包含最大回撤、抗跌评分、下行风险、波动率等风险指标，用于基金详情弹窗展示。

    - **code**: 6位基金代码（如 000001）
    """
    return await _async_to_thread(_anti_detail_service, code)


# v2.9.19: 数据质量统计API
@app.get("/api/data-quality/status", dependencies=[Depends(optional_api_key)], tags=["核心业务接口"],
         summary="获取数据质量统计")
async def data_quality_status():
    """获取当前基金数据质量统计。"""
    import db
    conn = db.get_conn()
    total = conn.execute("SELECT COUNT(*) as cnt FROM funds WHERE nav_date IS NOT NULL").fetchone()['cnt']
    ok = conn.execute("SELECT COUNT(*) as cnt FROM funds WHERE data_status = 'ok' OR data_status IS NULL").fetchone()['cnt']
    suspect = conn.execute("SELECT COUNT(*) as cnt FROM funds WHERE data_status = 'suspect'").fetchone()['cnt']
    conflict = conn.execute("SELECT COUNT(*) as cnt FROM funds WHERE data_status = 'conflict'").fetchone()['cnt']
    stale = conn.execute("SELECT COUNT(*) as cnt FROM funds WHERE data_status = 'stale'").fetchone()['cnt']
    anomaly = conn.execute("SELECT COUNT(*) as cnt FROM funds WHERE data_status = 'anomaly'").fetchone()['cnt']
    suspect_ratio = round((suspect + conflict + stale) / total * 100, 2) if total > 0 else 0
    return {
        "ok": True,
        "total": total,
        "ok_count": ok,
        "suspect": suspect,
        "conflict": conflict,
        "stale": stale,
        "anomaly": anomaly,
        "suspect_ratio": suspect_ratio,
        "should_alert": suspect_ratio > 5,
        "alert_threshold": 5
    }


# 双基金对比(七段式 summary)
@app.post("/api/watch/compare", dependencies=[Depends(optional_api_key)], tags=["核心业务接口"],
          summary="双基金对比分析",
          responses={
              200: {"description": "七段式对比分析结果（收益/风险/评分/持仓等维度）"},
              400: {"description": "请求参数错误"},
              429: {"description": "请求过于频繁（限流30次/分钟）"},
          })
@limiter.limit("30/minute")
async def watch_compare(request: Request, body: WatchCompare):
    """对两只基金进行七段式对比分析。

    对比维度包括：收益表现、风险指标、综合评分、持仓主题、重仓股、净值走势等。

    **请求体示例**：
    ```json
    {"codes": ["000001", "110011"]}
    ```
    """
    return await _async_to_thread(_watch_compare_service, body.codes)


# ---------------------------------------------------------------------------
# 线上版兼容路由(接口别名)
# ---------------------------------------------------------------------------

@app.get("/api/fund/anti_detail", dependencies=[Depends(optional_api_key)], tags=["核心业务接口"],
         summary="抗跌详情（线上版兼容别名）",
         responses={200: {"description": "抗跌详情数据"}, 404: {"description": "基金不存在"}})
async def anti_detail_alias(code: str):
    """线上版弹窗接口别名 → 现有 /api/anti/{code}。

    - **code**: 基金代码
    """
    return await anti_detail(code)


@app.post("/api/watch/sync", dependencies=[Depends(optional_api_key)], tags=["核心业务接口"],
         summary="自选同步（线上版兼容别名）",
         responses={200: {"description": "同步成功"}, 400: {"description": "请求参数错误"}})
async def watch_sync_alias(body: WatchSync):
    """线上版 watch_js 自选副本接口别名 → 现有 watchlist_sync。

    **请求体示例**：
    ```json
    {"codes": ["000001", "110011"]}
    ```
    """
    return await watchlist_sync(body)


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

async def _async_to_thread(func, *args, **kwargs):
    """异步包装同步函数（避免在事件循环中阻塞）。"""
    import asyncio
    return await asyncio.to_thread(func, *args, **kwargs)


# ---------------------------------------------------------------------------

# v2.9.52: 全局异常处理 - 捕获未处理的异常，记录到日志，避免进程静默崩溃
def _global_exception_handler(exc_type, exc_value, exc_traceback):
    """全局异常处理器，记录未处理的异常。"""
    import traceback
    _err_msg = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
    print(f"[FATAL] 未处理的异常: {_err_msg}", file=sys.stderr)
    try:
        import db
        _now = time.strftime("%Y-%m-%dT%H:%M:%S+08:00")
        db.get_conn().execute(
            "INSERT INTO task_logs (task_type, status, message, started_at, finished_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("global_exception", "error", f"进程崩溃: {str(exc_value)[:500]}", _now, _now)
        )
        db.get_conn().commit()
    except Exception:
        pass
    # 调用默认的异常处理
    sys.__excepthook__(exc_type, exc_value, exc_traceback)

sys.excepthook = _global_exception_handler

# v2.9.52: 进程启动时记录内存使用
def _log_startup_memory():
    """进程启动时记录内存使用。"""
    try:
        import psutil
        proc = psutil.Process(os.getpid())
        mem = proc.memory_info()
        print(f"[启动] 进程内存: RSS={mem.rss/1024/1024:.0f}MB · VMS={mem.vms/1024/1024:.0f}MB")
    except ImportError:
        print("[启动] psutil 未安装，无法记录内存使用")
    except Exception as e:
        print(f"[启动] 内存记录失败: {e}")

_log_startup_memory()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(sys.argv[1]) if len(sys.argv) > 1 else 8002)





