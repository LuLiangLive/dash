"""
hithink_client.py —— 同花顺金融数据服务 API 客户端

官方数据源，作为主源使用；失败时自动降级到 AkShare / 天天基金网。

文档: https://fuyao.aicubes.cn/docs/
API契约: docs/api/endpoints-fund.md
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)

# 统一时区：北京时间 UTC+8（所有日期处理必须使用此时区，禁止依赖系统时区）
BEIJING_TZ = timezone(timedelta(hours=8))

# 基础配置
BASE_URL = "https://fuyao.aicubes.cn"
DEFAULT_TIMEOUT = 15
MAX_RETRIES = 3
RETRY_BACKOFF = 1.0  # 秒，指数退避基数

# 错误码
CODE_SUCCESS = 0
CODE_RATE_LIMIT = 4001
CODE_DATA_NOT_READY = 3002
CODE_NOT_FOUND = 3001
CODE_NOT_SUPPORTED = 3004


class HithinkApiError(Exception):
    """同花顺API错误"""
    def __init__(self, code: int, message: str, request_id: str = ""):
        self.code = code
        self.message = message
        self.request_id = request_id
        super().__init__(f"[{code}] {message}")


class HithinkRateLimitError(HithinkApiError):
    """限流错误"""
    pass


class HithinkClient:
    """同花顺金融数据服务 API 客户端"""

    def __init__(self, api_key: str, timeout: int = DEFAULT_TIMEOUT):
        self.api_key = api_key
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"X-api-key": api_key})
        self._last_request_time = 0.0
        self._min_interval = 0.1  # 最小请求间隔，避免触发限流

    def _request(self, endpoint: str, params: Optional[dict] = None) -> dict:
        """
        发送API请求，带限流重试和错误处理

        Args:
            endpoint: API端点，如 /api/fund/performance/nav
            params: 查询参数

        Returns:
            data字段的内容

        Raises:
            HithinkRateLimitError: 限流错误
            HithinkApiError: 其他API错误
        """
        url = f"{BASE_URL}{endpoint}"
        last_error = None

        for attempt in range(MAX_RETRIES):
            # 限流控制
            elapsed = time.time() - self._last_request_time
            if elapsed < self._min_interval:
                time.sleep(self._min_interval - elapsed)

            try:
                self._last_request_time = time.time()
                resp = self.session.get(url, params=params, timeout=self.timeout)
                resp.raise_for_status()
                result = resp.json()

                code = result.get("code", -1)
                message = result.get("message", "")
                request_id = result.get("request_id", "")

                if code == CODE_SUCCESS:
                    return result.get("data", {})

                # 限流错误，指数退避重试
                if code == CODE_RATE_LIMIT:
                    wait_time = RETRY_BACKOFF * (2 ** attempt)
                    logger.warning(f"同花顺API限流，{wait_time}秒后重试 (attempt {attempt+1}/{MAX_RETRIES})")
                    time.sleep(wait_time)
                    last_error = HithinkRateLimitError(code, message, request_id)
                    continue

                # 数据尚未准备，不重试
                if code == CODE_DATA_NOT_READY:
                    logger.debug(f"数据尚未准备: {endpoint} params={params}")
                    raise HithinkApiError(code, message, request_id)

                # 其他错误，不重试
                raise HithinkApiError(code, message, request_id)

            except requests.exceptions.RequestException as e:
                logger.warning(f"同花顺API请求异常: {e} (attempt {attempt+1}/{MAX_RETRIES})")
                last_error = e
                if attempt < MAX_RETRIES - 1:
                    time.sleep(RETRY_BACKOFF * (2 ** attempt))

        raise last_error or HithinkApiError(-1, "未知错误")

    # ============================================================
    # 标的检索
    # ============================================================

    def search_ticker(self, query: str, limit: int = 5) -> list[dict]:
        """
        搜索标的（基金/股票），消歧为唯一thscode

        Args:
            query: 基金代码或名称
            limit: 返回数量上限

        Returns:
            标的列表，每项含 thscode, name, asset_type 等
        """
        data = self._request("/api/meta/tickers/search", {"q": query, "limit": limit})
        return data.get("item", [])

    def resolve_fund_thscode(self, fund_code: str) -> Optional[tuple[str, str]]:
        """
        解析基金代码为 (thscode, fund_type)

        Args:
            fund_code: 6位基金代码，如 "159779"

        Returns:
            (thscode, fund_type) 元组，如 ("159779.SZ", "exchange")
            找不到返回 None
        """
        results = self.search_ticker(fund_code, limit=10)
        for item in results:
            asset_type = item.get("asset_type", "")
            thscode = item.get("thscode", "")
            if asset_type == "fund-etf" or asset_type == "fund-lof":
                return (thscode, "exchange")
            if asset_type == "fund-otc":
                return (thscode, "otc")
        # 如果没找到明确类型，默认用第一个
        if results:
            thscode = results[0].get("thscode", "")
            return (thscode, "otc")
        return None

    # ============================================================
    # 基金基本资料
    # ============================================================

    def get_fund_profile(self, thscode: str, fund_type: str = "otc") -> Optional[dict]:
        """
        获取基金基本资料

        Args:
            thscode: 完整基金代码，如 "159779.SZ"
            fund_type: otc / exchange / reits

        Returns:
            基金基本资料字典，含 fund_name, fund_scale, unit_nav, mgmt_name 等
        """
        try:
            data = self._request("/api/fund/profile/detail", {
                "fund_type": fund_type,
                "thscode": thscode,
            })
            items = data.get("item", [])
            return items[0] if items else None
        except HithinkApiError as e:
            if e.code == CODE_NOT_FOUND:
                logger.debug(f"基金未找到: {thscode}")
                return None
            raise

    # ============================================================
    # 基金净值
    # ============================================================

    def get_fund_nav(self, thscode: str, fund_type: str = "otc",
                     range: str = "year", nav_type: str = "unit") -> list[dict]:
        """
        获取基金净值历史

        Args:
            thscode: 完整基金代码
            fund_type: otc / exchange / reits
            range: week / month / tmonth / hyear / year / twoyear / tyear / fyear
            nav_type: unit / adj / unit,adj

        Returns:
            净值列表，每项含 nav_date(毫秒时间戳), unit_nav, adj_nav
        """
        data = self._request("/api/fund/performance/nav", {
            "fund_type": fund_type,
            "thscode": thscode,
            "range": range,
            "nav_type": nav_type,
        })
        return data.get("item", [])

    def get_fund_nav_simple(self, thscode: str, fund_type: str = "otc",
                             days: int = 250) -> list[tuple[str, float]]:
        """
        获取基金净值，简化格式

        Args:
            thscode: 完整基金代码
            fund_type: 基金类型
            days: 需要的天数（近似映射到range）

        Returns:
            [(date_str, nav), ...] 列表，按日期升序
        """
        # 根据天数选择range
        if days <= 7:
            range_str = "week"
        elif days <= 30:
            range_str = "month"
        elif days <= 90:
            range_str = "tmonth"
        elif days <= 180:
            range_str = "hyear"
        elif days <= 365:
            range_str = "year"
        elif days <= 730:
            range_str = "twoyear"
        elif days <= 1095:
            range_str = "tyear"
        else:
            range_str = "fyear"

        items = self.get_fund_nav(thscode, fund_type, range_str, "unit")
        result = []
        for item in items:
            nav_date_ms = item.get("nav_date")
            unit_nav = item.get("unit_nav")
            if nav_date_ms and unit_nav:
                date_str = datetime.fromtimestamp(nav_date_ms / 1000, tz=BEIJING_TZ).strftime("%Y-%m-%d")
                result.append((date_str, float(unit_nav)))
        return result

    def get_fund_nav_with_types(self, thscode: str, fund_type: str = "otc",
                                 days: int = 250) -> list[dict]:
        """
        获取基金净值，同时返回单位净值和累计净值

        注意：同花顺API的字段映射与天天基金网不同：
        - unit_nav = 累计净值（对应天天基金网 dwjz）
        - adj_nav = 复权/单位净值（对应天天基金网 ljjz）

        Args:
            thscode: 完整基金代码
            fund_type: 基金类型
            days: 需要的天数

        Returns:
            [{date, unit_nav, adj_nav}, ...] 列表，按日期升序
        """
        # 根据天数选择range
        if days <= 7:
            range_str = "week"
        elif days <= 30:
            range_str = "month"
        elif days <= 90:
            range_str = "tmonth"
        elif days <= 180:
            range_str = "hyear"
        elif days <= 365:
            range_str = "year"
        elif days <= 730:
            range_str = "twoyear"
        elif days <= 1095:
            range_str = "tyear"
        else:
            range_str = "fyear"

        items = self.get_fund_nav(thscode, fund_type, range_str, "unit,adj")
        result = []
        for item in items:
            nav_date_ms = item.get("nav_date")
            unit_nav = item.get("unit_nav")
            adj_nav = item.get("adj_nav")
            if nav_date_ms and unit_nav:
                date_str = datetime.fromtimestamp(nav_date_ms / 1000, tz=BEIJING_TZ).strftime("%Y-%m-%d")
                result.append({
                    "date": date_str,
                    "unit_nav": float(unit_nav),
                    "adj_nav": float(adj_nav) if adj_nav else float(unit_nav),
                })
        return result

    # ============================================================
    # 基金区间收益
    # ============================================================

    def get_fund_returns(self, thscode: str, fund_type: str = "otc") -> Optional[dict]:
        """
        获取基金区间收益

        Args:
            thscode: 完整基金代码
            fund_type: 基金类型

        Returns:
            收益字典，含 return_month, return_tmonth, return_hyear, return_year 等
        """
        try:
            data = self._request("/api/fund/performance/returns", {
                "fund_type": fund_type,
                "thscode": thscode,
            })
            items = data.get("item", [])
            return items[0] if items else None
        except HithinkApiError as e:
            if e.code == CODE_DATA_NOT_READY:
                return None
            raise

    # ============================================================
    # 最大回撤
    # ============================================================

    def get_fund_drawdowns(self, thscode: str, fund_type: str = "otc") -> Optional[dict]:
        """
        获取基金最大回撤（固定区间）

        Args:
            thscode: 完整基金代码
            fund_type: 基金类型

        Returns:
            回撤字典，含 week, month, tmonth, hyear, year, twoyear 等
        """
        try:
            data = self._request("/api/fund/performance/drawdowns", {
                "fund_type": fund_type,
                "thscode": thscode,
            })
            items = data.get("item", [])
            return items[0] if items else None
        except HithinkApiError as e:
            if e.code == CODE_DATA_NOT_READY:
                return None
            raise

    # ============================================================
    # 基金持仓
    # ============================================================

    def get_fund_holdings(self, thscode: str, fund_type: str = "otc") -> list[dict]:
        """
        获取基金定期披露重仓股

        Args:
            thscode: 完整基金代码
            fund_type: 基金类型

        Returns:
            持仓列表，每项含 stock_name, hold_ratio, investment_rank 等
        """
        try:
            data = self._request("/api/fund/portfolio/holdings", {
                "fund_type": fund_type,
                "thscode": thscode,
            })
            return data.get("item", [])
        except HithinkApiError as e:
            if e.code == CODE_DATA_NOT_READY:
                return []
            raise

    def get_fund_holdings_simple(self, thscode: str, fund_type: str = "otc") -> list[tuple[str, float]]:
        """
        获取基金持仓，简化格式

        Args:
            thscode: 完整基金代码
            fund_type: 基金类型

        Returns:
            [(stock_name, hold_ratio), ...] 列表
        """
        holdings = self.get_fund_holdings(thscode, fund_type)
        result = []
        for h in holdings:
            name = h.get("stock_name", "")
            ratio = h.get("hold_ratio", 0)
            if name and ratio:
                result.append((name, float(ratio)))
        return result

    # ============================================================
    # 基金行业配置
    # ============================================================

    def get_fund_industry_allocation(self, thscode: str, fund_type: str = "otc") -> list[dict]:
        """获取基金行业配置"""
        try:
            data = self._request("/api/fund/portfolio/industry-allocation", {
                "fund_type": fund_type,
                "thscode": thscode,
            })
            return data.get("item", [])
        except HithinkApiError:
            return []

    # ============================================================
    # 基金资产配置
    # ============================================================

    def get_fund_asset_allocation(self, thscode: str, fund_type: str = "otc") -> list[dict]:
        """获取基金资产配置（股票/债券/现金比例）"""
        try:
            data = self._request("/api/fund/portfolio/asset-allocation", {
                "fund_type": fund_type,
                "thscode": thscode,
            })
            return data.get("item", [])
        except HithinkApiError:
            return []

    # ============================================================
    # ETF/LOF 场内行情
    # ============================================================

    def get_etf_snapshot(self, thscode: str) -> Optional[dict]:
        """
        获取ETF/LOF实时行情快照

        Args:
            thscode: 完整基金代码，如 "510300.SH"

        Returns:
            行情字典，含 last_price, price_change_ratio_pct, volume, turnover 等
        """
        try:
            data = self._request("/api/fund/market/snapshot", {"thscode": thscode})
            items = data.get("item", [])
            return items[0] if items else None
        except HithinkApiError as e:
            if e.code == CODE_NOT_SUPPORTED:
                logger.debug(f"该基金不支持场内行情: {thscode}")
                return None
            raise

    def get_etf_historical(self, thscode: str, start: datetime, end: datetime) -> list[dict]:
        """
        获取ETF历史日线行情

        Args:
            thscode: 完整ETF代码
            start: 起始日期
            end: 结束日期

        Returns:
            行情列表，每项含 date_ms, open_price, high_price, low_price, close_price, volume, turnover
        """
        start_ms = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        data = self._request("/api/fund/market/historical", {
            "thscode": thscode,
            "interval": "1d",
            "start": start_ms,
            "end": end_ms,
        })
        return data.get("item", [])

    # ============================================================
    # 基金持有人结构
    # ============================================================

    def get_fund_holders(self, thscode: str, fund_type: str = "otc") -> list[dict]:
        """获取基金持有人结构"""
        try:
            data = self._request("/api/fund/holders/detail", {
                "fund_type": fund_type,
                "thscode": thscode,
            })
            return data.get("item", [])
        except HithinkApiError:
            return []

    # ============================================================
    # 基金经理
    # ============================================================

    def get_manager_detail(self, manager_id: str) -> Optional[dict]:
        """获取基金经理详情"""
        try:
            data = self._request("/api/fund/managers/detail", {"manager_id": manager_id})
            items = data.get("item", [])
            return items[0] if items else None
        except HithinkApiError:
            return None

    def get_manager_performance(self, manager_id: str, range: str = "year") -> list[dict]:
        """获取基金经理业绩"""
        try:
            data = self._request("/api/fund/managers/performance", {
                "manager_id": manager_id,
                "range": range,
            })
            return data.get("item", [])
        except HithinkApiError:
            return []

    # ============================================================
    # 基金诊断
    # ============================================================

    def get_fund_diagnostics(self, thscode: str, fund_type: str = "otc") -> Optional[dict]:
        """获取基金诊断（多维度评分）"""
        try:
            data = self._request("/api/fund/diagnostics/detail", {
                "fund_type": fund_type,
                "thscode": thscode,
            })
            items = data.get("item", [])
            return items[0] if items else None
        except HithinkApiError:
            return None

    # ============================================================
    # 基金资讯
    # ============================================================

    def get_fund_news(self, thscode: str, fund_type: str = "otc", limit: int = 20) -> dict:
        """获取基金资讯列表"""
        try:
            return self._request("/api/fund/news/article-list", {
                "fund_type": fund_type,
                "thscode": thscode,
                "limit": limit,
            })
        except HithinkApiError:
            return {"item": [], "has_more": False}

    # ============================================================
    # 工具方法
    # ============================================================

    def health_check(self) -> bool:
        """健康检查：测试API Key是否有效"""
        try:
            self.search_ticker("510300", limit=1)
            return True
        except Exception as e:
            logger.error(f"同花顺API健康检查失败: {e}")
            return False


# 全局单例
_client: Optional[HithinkClient] = None


def get_hithink_client(api_key: Optional[str] = None) -> Optional[HithinkClient]:
    """
    获取同花顺客户端单例

    Args:
        api_key: API Key，如果为None则从环境变量或配置读取

    Returns:
        HithinkClient实例，如果没有配置API Key则返回None
    """
    global _client
    if _client is not None:
        return _client

    if api_key is None:
        # 尝试从环境变量读取
        import os
        api_key = os.environ.get("HITHINK_FINANCE_API_KEY", "")
        if not api_key:
            # 尝试从配置读取
            try:
                from config import settings
                api_key = getattr(settings, "hithink_api_key", "") or ""
            except Exception:
                pass

    if not api_key:
        logger.warning("同花顺API Key未配置，将使用备用数据源")
        return None

    _client = HithinkClient(api_key)
    return _client


def is_hithink_available() -> bool:
    """检查同花顺API是否可用"""
    client = get_hithink_client()
    return client is not None
