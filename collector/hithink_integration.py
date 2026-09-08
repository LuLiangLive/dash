"""
hithink_integration.py —— 同花顺数据源集成层

v2.9.10: 同花顺官方数据源作为主源，失败时自动降级到 AkShare / 天天基金网。

使用方式:
    from collector.hithink_integration import get_fund_nav, get_fund_holdings

    # 优先同花顺，失败自动降级
    nav_data = get_fund_nav("159779", days=250)
    holdings = get_fund_holdings("159779")
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

from config import settings

logger = logging.getLogger(__name__)

# 全局标记：同花顺是否可用
_hithink_available: Optional[bool] = None


def _is_hithink_available() -> bool:
    """检查同花顺API是否可用（带缓存）"""
    global _hithink_available
    if _hithink_available is not None:
        return _hithink_available

    if not getattr(settings, "use_hithink_as_primary", True):
        logger.info("同花顺数据源已禁用（use_hithink_as_primary=False）")
        _hithink_available = False
        return False

    api_key = getattr(settings, "hithink_api_key", "")
    if not api_key:
        logger.warning("同花顺API Key未配置")
        _hithink_available = False
        return False

    try:
        from collector.hithink_client import get_hithink_client
        client = get_hithink_client(api_key)
        if client is None:
            _hithink_available = False
            return False
        # 健康检查
        if client.health_check():
            logger.info("同花顺数据源可用，将作为主数据源")
            _hithink_available = True
            return True
        else:
            logger.warning("同花顺数据源健康检查失败")
            _hithink_available = False
            return False
    except Exception as e:
        logger.warning(f"同花顺数据源初始化失败: {e}")
        _hithink_available = False
        return False


def _get_hithink_client():
    """获取同花顺客户端"""
    if not _is_hithink_available():
        return None
    from collector.hithink_client import get_hithink_client
    return get_hithink_client(getattr(settings, "hithink_api_key", ""))


def _resolve_fund_info(fund_code: str) -> Optional[tuple[str, str]]:
    """
    解析基金代码为 (thscode, fund_type)

    Returns:
        (thscode, fund_type) 或 None
    """
    client = _get_hithink_client()
    if client is None:
        return None
    try:
        return client.resolve_fund_thscode(fund_code)
    except Exception as e:
        logger.debug(f"解析基金代码失败 {fund_code}: {e}")
        return None


# ============================================================
# 基金净值
# ============================================================

def get_fund_nav(fund_code: str, days: int = 250) -> list[tuple[str, float]]:
    """
    获取基金净值（优先同花顺，失败降级）

    Args:
        fund_code: 6位基金代码
        days: 需要的天数

    Returns:
        [(date_str, nav), ...] 列表，按日期升序
    """
    # 尝试同花顺
    client = _get_hithink_client()
    if client is not None:
        fund_info = _resolve_fund_info(fund_code)
        if fund_info:
            thscode, fund_type = fund_info
            try:
                nav_data = client.get_fund_nav_with_types(thscode, fund_type, days)
                if nav_data:
                    # 检查是否有拆分/分红
                    has_split = any(
                        abs(item["unit_nav"] - item["adj_nav"]) > 0.001
                        for item in nav_data
                    )
                    if has_split:
                        logger.debug(f"基金 {fund_code} 有拆分/分红记录，降级到天天基金网")
                    else:
                        logger.debug(f"同花顺获取净值成功: {fund_code} ({len(nav_data)}条)")
                        # 使用adj_nav（单位净值）
                        return [(item["date"], item["adj_nav"]) for item in nav_data]
            except Exception as e:
                logger.debug(f"同花顺获取净值失败 {fund_code}: {e}，降级到备用源")

    # 降级到现有数据源
    logger.debug(f"使用备用源获取净值: {fund_code}")
    try:
        from modules.nav.fetcher import fetch_nav_history
        # 调用现有的净值获取函数
        # 注意：这里需要根据现有函数的实际返回格式做适配
        result = fetch_nav_history(fund_code, days)
        if result:
            # 适配格式为 [(date_str, nav), ...]
            if isinstance(result, list) and result:
                if isinstance(result[0], (list, tuple)) and len(result[0]) >= 2:
                    return [(str(r[0]), float(r[1])) for r in result]
                elif isinstance(result[0], dict):
                    return [(str(r.get("date", "")), float(r.get("nav", 0))) for r in result if r.get("nav")]
    except Exception as e:
        logger.error(f"备用源获取净值也失败 {fund_code}: {e}")

    return []


def get_fund_nav_items(fund_code: str, days: int = 60) -> list[dict]:
    """
    获取基金净值，返回格式与现有fetcher一致（[{date, ljjz, dwjz}]）

    用于一键更新的增量抓取，优先同花顺，失败降级到天天基金网。

    Args:
        fund_code: 6位基金代码
        days: 需要的天数

    Returns:
        [{date, ljjz, dwjz}, ...] 列表，按日期升序
    """
    # 尝试同花顺
    client = _get_hithink_client()
    if client is not None:
        fund_info = _resolve_fund_info(fund_code)
        if fund_info:
            thscode, fund_type = fund_info
            try:
                nav_data = client.get_fund_nav_with_types(thscode, fund_type, days)
                if nav_data:
                    # 检查是否有拆分/分红（unit_nav != adj_nav）
                    # 对于有拆分/分红的基金，同花顺的复权净值与天天基金网的单位净值不一致
                    # 自动降级到天天基金网，确保数据一致性
                    has_split = any(
                        abs(item["unit_nav"] - item["adj_nav"]) > 0.001
                        for item in nav_data
                    )
                    if has_split:
                        logger.debug(f"基金 {fund_code} 有拆分/分红记录，降级到天天基金网确保数据一致性")
                    else:
                        logger.debug(f"同花顺获取净值成功: {fund_code} ({len(nav_data)}条)")
                        # 转换为现有格式 [{date, ljjz, dwjz}]
                        # 字段映射：同花顺 unit_nav=累计净值→dwjz, adj_nav=单位净值→ljjz
                        return [
                            {
                                "date": item["date"],
                                "ljjz": item["adj_nav"],  # 单位净值
                                "dwjz": item["unit_nav"],  # 累计净值
                            }
                            for item in nav_data
                        ]
            except Exception as e:
                logger.debug(f"同花顺获取净值失败 {fund_code}: {e}，降级到备用源")

    # 降级到现有数据源（天天基金网）
    logger.debug(f"使用备用源获取净值: {fund_code}")
    try:
        from modules.nav.fetcher import fetch_nav_history
        result = fetch_nav_history(fund_code, page_size=days)
        if result:
            return result
    except Exception as e:
        logger.error(f"备用源获取净值也失败 {fund_code}: {e}")

    return []


def get_fund_nav_range_items(fund_code: str, start_date: str, end_date: str) -> list[dict]:
    """
    获取指定区间的基金净值，返回格式与现有fetcher一致

    Args:
        fund_code: 6位基金代码
        start_date: 开始日期 YYYY-MM-DD
        end_date: 结束日期 YYYY-MM-DD

    Returns:
        [{date, ljjz, dwjz}, ...] 列表，按日期升序
    """
    # 计算需要的天数
    try:
        from datetime import date
        sd = date.fromisoformat(start_date)
        ed = date.fromisoformat(end_date)
        days = (ed - sd).days + 30  # 多取30天确保覆盖
        days = max(days, 30)
    except Exception:
        days = 60

    # 获取净值后过滤区间
    all_navs = get_fund_nav_items(fund_code, days=days)
    return [n for n in all_navs if start_date <= n.get("date", "") <= end_date]


# ============================================================
# 基金持仓
# ============================================================

def get_fund_holdings(fund_code: str) -> list[dict]:
    """
    获取基金持仓（优先同花顺，失败降级）

    Args:
        fund_code: 6位基金代码

    Returns:
        持仓列表，每项含 stock_name, hold_ratio 等
    """
    # 尝试同花顺
    client = _get_hithink_client()
    if client is not None:
        fund_info = _resolve_fund_info(fund_code)
        if fund_info:
            thscode, fund_type = fund_info
            try:
                holdings = client.get_fund_holdings(thscode, fund_type)
                if holdings:
                    logger.debug(f"同花顺获取持仓成功: {fund_code} ({len(holdings)}只)")
                    # 适配格式
                    return [
                        {
                            "stock_name": h.get("stock_name", ""),
                            "hold_ratio": h.get("hold_ratio", 0),
                            "investment_rank": h.get("investment_rank", 0),
                            "market_value": h.get("position_capital", 0),
                        }
                        for h in holdings
                    ]
            except Exception as e:
                logger.debug(f"同花顺获取持仓失败 {fund_code}: {e}，降级到备用源")

    # 降级到现有数据源
    logger.debug(f"使用备用源获取持仓: {fund_code}")
    try:
        from modules.fund.holdings import fetch_holdings_w
        result = fetch_holdings_w(fund_code)
        if result:
            return result
    except Exception as e:
        logger.error(f"备用源获取持仓也失败 {fund_code}: {e}")

    return []


def get_fund_holdings_simple(fund_code: str) -> list[tuple[str, float]]:
    """
    获取基金持仓，返回简化格式 [(股票名, 占比%), ...]

    优先同花顺，失败降级到天天基金网。用于一键更新的持仓抓取。

    Args:
        fund_code: 6位基金代码

    Returns:
        [(stock_name, hold_ratio), ...] 列表
    """
    # 尝试同花顺
    client = _get_hithink_client()
    if client is not None:
        fund_info = _resolve_fund_info(fund_code)
        if fund_info:
            thscode, fund_type = fund_info
            try:
                holdings = client.get_fund_holdings_simple(thscode, fund_type)
                if holdings:
                    logger.debug(f"同花顺获取持仓成功: {fund_code} ({len(holdings)}只)")
                    return holdings
            except Exception as e:
                logger.debug(f"同花顺获取持仓失败 {fund_code}: {e}，降级到备用源")

    # 降级到天天基金网
    logger.debug(f"使用备用源获取持仓: {fund_code}")
    try:
        from modules.fund.holdings import fetch_holdings_w
        result = fetch_holdings_w(fund_code)
        if result:
            return result
    except Exception as e:
        logger.error(f"备用源获取持仓也失败 {fund_code}: {e}")

    return []


# ============================================================
# 基金基本信息
# ============================================================

def get_fund_profile(fund_code: str) -> Optional[dict]:
    """
    获取基金基本信息（优先同花顺，失败降级）

    Args:
        fund_code: 6位基金代码

    Returns:
        基金信息字典
    """
    # 尝试同花顺
    client = _get_hithink_client()
    if client is not None:
        fund_info = _resolve_fund_info(fund_code)
        if fund_info:
            thscode, fund_type = fund_info
            try:
                profile = client.get_fund_profile(thscode, fund_type)
                if profile:
                    logger.debug(f"同花顺获取基金信息成功: {fund_code}")
                    return {
                        "fund_code": fund_code,
                        "fund_name": profile.get("fund_name", ""),
                        "fund_scale": profile.get("fund_scale", 0),
                        "unit_nav": profile.get("unit_nav", 0),
                        "mgmt_name": profile.get("mgmt_name", ""),
                        "manager_name": profile.get("manager_name", ""),
                        "estab_date": profile.get("estab_date", 0),
                    }
            except Exception as e:
                logger.debug(f"同花顺获取基金信息失败 {fund_code}: {e}，降级到备用源")

    # 降级到现有数据源
    try:
        from modules.fund.fetcher import fetch_fund_name
        name = fetch_fund_name(fund_code)
        if name:
            return {"fund_code": fund_code, "fund_name": name}
    except Exception as e:
        logger.error(f"备用源获取基金信息也失败 {fund_code}: {e}")

    return None


# ============================================================
# 基金收益指标
# ============================================================

def get_fund_returns(fund_code: str) -> Optional[dict]:
    """
    获取基金区间收益（优先同花顺，失败降级）

    Returns:
        收益字典，含 return_month, return_tmonth, return_year 等
    """
    client = _get_hithink_client()
    if client is not None:
        fund_info = _resolve_fund_info(fund_code)
        if fund_info:
            thscode, fund_type = fund_info
            try:
                returns = client.get_fund_returns(thscode, fund_type)
                if returns:
                    return returns
            except Exception as e:
                logger.debug(f"同花顺获取收益失败 {fund_code}: {e}")

    return None


# ============================================================
# ETF实时行情
# ============================================================

def get_etf_snapshot(fund_code: str) -> Optional[dict]:
    """
    获取ETF实时行情（优先同花顺，失败降级）

    Returns:
        行情字典，含 last_price, price_change_ratio_pct 等
    """
    client = _get_hithink_client()
    if client is not None:
        fund_info = _resolve_fund_info(fund_code)
        if fund_info:
            thscode, fund_type = fund_info
            if fund_type == "exchange":
                try:
                    snapshot = client.get_etf_snapshot(thscode)
                    if snapshot:
                        return snapshot
                except Exception as e:
                    logger.debug(f"同花顺获取ETF行情失败 {fund_code}: {e}")

    return None


# ============================================================
# 工具方法
# ============================================================

def get_data_source_status() -> dict:
    """
    获取数据源状态

    Returns:
        数据源状态字典
    """
    return {
        "hithink_available": _is_hithink_available(),
        "hithink_as_primary": getattr(settings, "use_hithink_as_primary", True),
        "fallback": "AkShare + 天天基金网",
    }


def reset_hithink_cache():
    """重置同花顺可用性缓存（用于配置变更后重新检测）"""
    global _hithink_available
    _hithink_available = None
    logger.info("同花顺数据源可用性缓存已重置")
