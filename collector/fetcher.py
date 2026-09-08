"""
fetcher.py —— 数据抓取兼容层

v2.5.5 架构重构完成：
  - 所有核心函数已迁移到 modules/ 下的领域模块
  - 本文件保留为兼容层，通过 re-export 方式导出所有函数
  - 确保所有现有引用（from collector.fetcher import xxx）不受影响

已迁移的模块：
  - modules/fund/classifier.py —— 基金类型判断
  - modules/common/fallback.py —— 本地降级
  - modules/market/index.py —— 指数日K
  - modules/fund/fetcher.py —— 基金基础信息
  - modules/nav/fetcher.py —— 净值数据抓取
  - modules/rank/fetcher.py —— 榜单数据抓取
  - modules/fund/profile.py —— 基金概况数据
  - modules/fund/universe.py —— 基金池构建
  - modules/fund/pingzhong.py —— pingzhongdata统一解析
  - modules/fund/mobile.py —— 移动端接口
  - modules/fund/holdings.py —— 持仓数据获取
  - modules/fund/profile_combined.py —— 统一档案组合
"""
from __future__ import annotations

from pathlib import Path

# v2.5.5 架构重构 - 阶段1：从新模块 re-export（兼容层）
from modules.fund.classifier import (
    BOND_FUND_KWS, FIXED_INCOME_TYPES,
    is_bond_fund as _is_bond_fund,
    is_fixed_income_type as _is_fixed_income_type,
    is_c_share as _is_c_share,
)
from modules.common.fallback import load_json as _load_json, local_nav_fallback
from modules.market.index import (
    INDEX_CODES, fetch_index_kline, _load_indices_from_db, fetch_all_indices,
)
from modules.fund.fetcher import fetch_fund_name, fetch_fund_stocks

# v2.5.5 架构重构 - 阶段2：净值抓取模块 re-export（兼容层）
from modules.nav.fetcher import (
    MIN_RECENT_NAV_POINTS, _now, effective_nav_date, cached_nav_max,
    nav_recent_points, _batch_nav_health, nav_cached_max_many, nav_is_healthy,
    fetch_nav_incremental, merge_navs_to_db, fetch_nav_history, fetch_nav_range,
    nav_pairs, fetch_nav_many,
)

# v2.5.5 架构重构 - 阶段3：中等复杂度模块 re-export（兼容层）
from modules.rank.fetcher import fetch_rank, fetch_all_market
from modules.fund.profile import fetch_basic, fetch_f10_profile
from modules.fund.universe import (
    _load_local_pool, _infer_meta, _build_full_market_pool, effective_pool,
)

# v2.5.5 架构重构 - 阶段4：核心复杂模块 re-export（兼容层）
from modules.fund.pingzhong import (
    fetch_pingzhong, fetch_fund_returns,
)
from modules.fund.mobile import (
    MOBILE_ENABLED, mobile_available, fetch_mobile_profile,
)
from modules.fund.holdings import (
    fetch_holdings_w, fetch_holdings_w_full, holdings_themes,
    fetch_stock_industry, holdings_total, holdings_breakdown,
    fetch_holdings_many, HOLDINGS_MIN_TOTAL, THEME_KWS,
)
from modules.fund.profile_combined import (
    fetch_profile, fetch_f10_profile_cached, fetch_basic_many,
)

# 兼容旧引用：is_bond_fund, is_fixed_income_type, is_c_share
is_bond_fund = _is_bond_fund
is_fixed_income_type = _is_fixed_income_type
is_c_share = _is_c_share

# 路径常量（保持向后兼容）
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

# 配置常量（保持向后兼容，从config读取）
from config import settings as _settings
COVER_FLOOR = _settings.cover_floor

# 兼容旧版本的净值缓存函数（v2.5.5后已废弃，数据存数据库）
# 保留空实现以确保旧代码不会报错
def _load_nav_cache():
    """加载净值缓存（已废弃，返回空缓存）"""
    return None, {}

def _save_nav_cache(cache_date, navs_all):
    """保存净值缓存（已废弃，不执行任何操作）"""
    pass
