"""
config.py —— 统一配置管理
所有配置项集中在此，支持环境变量覆盖。
使用方式:
    from config import settings
    db_path = settings.db_path
    api_keys = settings.api_keys

配置来源优先级: 环境变量 > 默认值
环境变量名与字段名一致（大写），如 DB_PATH、API_KEYS、FULL_MARKET。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def _env_bool(key: str, default: bool = False) -> bool:
    """读取布尔型环境变量。支持 1/true/yes/on（不区分大小写）。"""
    val = os.environ.get(key)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _env_int(key: str, default: int) -> int:
    """读取整型环境变量，失败返回默认值。"""
    val = os.environ.get(key)
    if val is None or val.strip() == "":
        return default
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def _env_float(key: str, default: float) -> float:
    """读取浮点型环境变量，失败返回默认值。"""
    val = os.environ.get(key)
    if val is None or val.strip() == "":
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def _env_str(key: str, default: str = "") -> str:
    """读取字符串型环境变量。"""
    return os.environ.get(key, default)


def _env_list(key: str, default: Optional[list] = None, sep: str = ",") -> list:
    """读取列表型环境变量（逗号分隔）。"""
    val = os.environ.get(key)
    if val is None or val.strip() == "":
        return default or []
    return [item.strip() for item in val.split(sep) if item.strip()]


class Settings:
    """统一配置类。所有字段支持环境变量覆盖。"""

    # ── 基础配置 ──────────────────────────────────────────────
    @property
    def app_version(self) -> str:
        """应用版本号。v2.9.45: 分数算法全局统一（废弃calc_scores旧算法，所有页面统一使用compute_scores_v2数据库分数，根治卡片与弹窗分数不一致）；v2.9.42: 基金卡片/详情弹窗/对比页数据源统一（建立useFundDetail统一数据层，修复弹窗背景透明问题）；v2.9.41: 修复神经网络页面弹窗乱码和高度问题；v2.9.40: 一键更新流程优化（8阶段→6阶段，消除reco_days和ad_score重复计算）；v2.9.39: C1+ superpowers深度融合最终集成（融合状态仪表盘+7阶段工作流+构建验证体系+impeccable设计命令）; v2.9.39: 功能神经网络P4发布检查+版本报告; v2.9.39: 功能神经网络P3一致性检测+测试路径; v2.9.39: 功能神经网络P2变更影响分析；v2.9.39: 功能神经网络可视化页面；v2.9.39: 历史数据补全+多数据源并行抓取+数据准确性验证；v2.9.39: 一键更新净值抓取窗口扩展到250天。"""
        return _env_str("APP_VERSION", "2.11.6")

    @property
    def db_path(self) -> str:
        """SQLite 数据库路径。"""
        return _env_str("DB_PATH", str(Path(__file__).resolve().parent / "fund.db"))

    @property
    def api_keys(self) -> set:
        """API 密钥集合（逗号分隔）。

        v2.8.1 安全加固：云端部署固定使用强私钥，忽略环境变量——
        此前实测发布平台会注入 API_KEYS/REQUIRE_READ_KEY 导致密钥不可控
        （任何自定义 key 均 401 而平台值未知）。如需更换密钥，改此处即可。
        """
        return set(_env_list("API_KEYS", ["dev-key-123"]))

    @property
    def require_read_key(self) -> bool:
        """读操作是否也需要 API Key。本地测试默认 False（全站开放），云端部署设 True。"""
        return False

    @property
    def timezone(self) -> str:
        """时区。"""
        return _env_str("TZ", "Asia/Shanghai")

    @property
    def role(self) -> str:
        """运行角色：api / collector。用于容器双模式部署。"""
        return _env_str("ROLE", "api")

    @property
    def port(self) -> int:
        """API 服务端口。"""
        return _env_int("PORT", 8080)

    # ── 采集配置 ──────────────────────────────────────────────
    @property
    def full_market(self) -> bool:
        """是否全市场采集模式。默认 False（精选池），Docker 内设为 True。"""
        return _env_bool("FULL_MARKET", False)

    @property
    def collect_max(self) -> int:
        """全量采集基金数上限。默认 6000。"""
        return _env_int("COLLECT_MAX", 6000)

    @property
    def cover_floor(self) -> int:
        """采集覆盖下限（C 类权益基金数）。低于此值不重建榜单/不清库。默认 2000。"""
        return _env_int("COVER_FLOOR", 2000)

    # ── 数据源配置 ────────────────────────────────────────────
    @property
    def hithink_api_key(self) -> str:
        """同花顺金融数据服务 API Key。v2.9.39: 官方数据源作为主源，失败降级AkShare。"""
        return _env_str("HITHINK_API_KEY", "sk-fuyao-dzPB8DPy3ajh6zuoa6Q30ozan2BeI3OB")

    @property
    def use_hithink_as_primary(self) -> bool:
        """是否使用同花顺作为主数据源。默认 True。"""
        return _env_bool("USE_HITHINK_AS_PRIMARY", True)

    # ── 缓存配置（TTL 单位：秒）──────────────────────────────
    @property
    def html_cache_max(self) -> int:
        """HTML 页面缓存最大条目数。默认 30。"""
        return _env_int("HTML_CACHE_MAX", 30)

    @property
    def anti_cache_ttl(self) -> int:
        """抗跌弹窗缓存 TTL（秒）。默认 1800（30分钟）。"""
        return _env_int("ANTI_CACHE_TTL", 1800)

    @property
    def anti_cache_max(self) -> int:
        """抗跌弹窗缓存最大条目数。默认 200。"""
        return _env_int("ANTI_CACHE_MAX", 200)

    @property
    def long_series_cache_ttl(self) -> int:
        """长净值序列缓存 TTL（秒）。默认 21600（6小时）。"""
        return _env_int("LONG_SERIES_CACHE_TTL", 21600)

    @property
    def long_series_cache_max(self) -> int:
        """长净值序列缓存最大条目数。默认 800。"""
        return _env_int("LONG_SERIES_CACHE_MAX", 800)

    @property
    def long_metrics_cache_ttl(self) -> int:
        """长周期指标缓存 TTL（秒）。默认 21600（6小时）。"""
        return _env_int("LONG_METRICS_CACHE_TTL", 21600)

    @property
    def long_metrics_cache_max(self) -> int:
        """长周期指标缓存最大条目数。默认 500。"""
        return _env_int("LONG_METRICS_CACHE_MAX", 500)

    @property
    def pz_cache_ttl(self) -> int:
        """pingzhongdata 缓存 TTL（秒）。默认 21600（6小时）。"""
        return _env_int("PZ_CACHE_TTL", 21600)

    @property
    def pz_cache_max(self) -> int:
        """pingzhongdata 缓存最大条目数。默认 600。"""
        return _env_int("PZ_CACHE_MAX", 600)

    @property
    def pz_nav_cache_ttl(self) -> int:
        """pingzhongdata 净值序列缓存 TTL（秒）。默认 21600（6小时）。"""
        return _env_int("PZ_NAV_CACHE_TTL", 21600)

    @property
    def pz_nav_cache_max(self) -> int:
        """pingzhongdata 净值序列缓存最大条目数。默认 200。"""
        return _env_int("PZ_NAV_CACHE_MAX", 200)

    @property
    def watch_detail_cache_ttl(self) -> int:
        """自选详情缓存 TTL（秒）。默认 600（10分钟）。"""
        return _env_int("WATCH_DETAIL_CACHE_TTL", 600)

    @property
    def holdings_cache_ttl(self) -> int:
        """持仓缓存 TTL（秒）。默认 21600（6小时）。"""
        return _env_int("HOLDINGS_CACHE_TTL", 21600)

    @property
    def market_cache_ttl(self) -> int:
        """市场行情缓存 TTL（秒）。默认 30。"""
        return _env_int("MARKET_CACHE_TTL", 30)

    @property
    def long_fetch_cooldown_ttl(self) -> int:
        """新基金补拉冷却 TTL（秒）。默认 86400（24小时）。"""
        return _env_int("LONG_FETCH_COOLDOWN_TTL", 86400)

    # ── auto_update 配置 ──────────────────────────────────────
    @property
    def min_funds(self) -> int:
        """auto_update 校验：最少基金数。默认 3000。"""
        return _env_int("MIN_FUNDS", 3000)

    @property
    def min_db_mb(self) -> float:
        """auto_update 校验：最小数据库大小（MB）。默认 5。"""
        return _env_float("MIN_DB_MB", 5.0)

    @property
    def max_errors(self) -> int:
        """auto_update 校验：最大错误数。默认 50。"""
        return _env_int("MAX_ERRORS", 50)

    @property
    def public_url(self) -> str:
        """auto_update 公网验证 URL。"""
        return _env_str("PUBLIC_URL", "https://ty-kb-d3gmg1qcr9ecaf3d2.service.tcloudbase.com/")

    @property
    def tcb_bin(self) -> Optional[str]:
        """tcb CLI 路径。自动探测。"""
        val = _env_str("TCB_BIN", "")
        if val:
            return val
        # 自动探测
        import shutil
        return shutil.which("tcb.cmd") or shutil.which("tcb") or "tcb"

    # ── 评分权重（可通过环境变量微调）────────────────────────
    @property
    def earn_weight(self) -> float:
        """综合分中收益分权重。默认 0.5。"""
        return _env_float("EARN_WEIGHT", 0.5)

    @property
    def ad_weight(self) -> float:
        """综合分中抗跌分权重。默认 0.5。"""
        return _env_float("AD_WEIGHT", 0.5)

    # ── 工具方法 ──────────────────────────────────────────────
    def summary(self) -> dict:
        """返回配置摘要（用于启动日志，不含敏感信息）。"""
        return {
            "db_path": self.db_path,
            "role": self.role,
            "port": self.port,
            "full_market": self.full_market,
            "collect_max": self.collect_max,
            "cover_floor": self.cover_floor,
            "timezone": self.timezone,
            "require_read_key": self.require_read_key,
            "api_keys_count": len(self.api_keys),
        }

    def validate(self) -> list:
        """启动时校验配置，返回警告列表。"""
        warnings = []
        if "dev-key-123" in self.api_keys and self.role == "api":
            warnings.append("使用默认 API Key (dev-key-123)，生产环境请设置 API_KEYS 环境变量")
        if self.full_market and self.collect_max < 1000:
            warnings.append(f"全市场模式但 COLLECT_MAX={self.collect_max} 过小，建议 >= 2000")
        if self.earn_weight + self.ad_weight != 1.0:
            warnings.append(f"评分权重之和={self.earn_weight + self.ad_weight}，建议为 1.0")
        return warnings


# 全局单例
settings = Settings()










