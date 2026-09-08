"""
modules/datasource/eastmoney.py —— 天天基金数据源实现

基于天天基金公开 API（fund.eastmoney.com / api.fund.eastmoney.com），
实现统一的数据源接口。

使用的公开接口：
1. pingzhongdata/{code}.js —— 基金基本信息（名称、类型、规模、经理等）
2. api.fund.eastmoney.com/f10/lsjz —— 历史净值
3. fundgz.1234567.com.cn/{code}.js —— 实时估值（可选）
4. api.fund.eastmoney.com/f10/FundArchivesDatas.aspx —— 持仓信息

法律合规：仅使用天天基金网站公开的、无需登录即可访问的数据接口。
"""
from __future__ import annotations

import json
import re
import time
from typing import Optional

from .base import BaseDataSource, FundInfo, NavPoint, HoldingItem
from .cache import get_cache


class EastMoneySource(BaseDataSource):
    """天天基金数据源。"""

    name = "eastmoney"
    display_name = "天天基金"
    base_url = "https://fund.eastmoney.com"
    request_interval = 0.08  # 请求间隔 80ms

    def __init__(self):
        super().__init__()
        self._cache = get_cache()

    # ------------------------------------------------------------------
    # HTTP 工具（复用 collector.http_utils._get）
    # ------------------------------------------------------------------
    def _http_get(self, url: str, referer: Optional[str] = None) -> Optional[str]:
        """发送 GET 请求，带频率控制和错误记录。"""
        self._wait_for_rate_limit()
        try:
            from collector.http_utils import _get
            text = _get(url, referer=referer or self.base_url)
            if text:
                self._record_success()
                return text
            self._record_error()
            return None
        except Exception:
            self._record_error()
            return None

    # ------------------------------------------------------------------
    # 基金基本信息
    # ------------------------------------------------------------------
    def get_fund_info(self, code: str) -> Optional[FundInfo]:
        """从 pingzhongdata 获取基金基本信息。"""
        # 检查缓存
        cached = self._cache.get(self.name, "fund_info", code)
        if cached is not None:
            return cached

        url = f"https://fund.eastmoney.com/pingzhongdata/{code}.js"
        text = self._http_get(url, referer=f"https://fund.eastmoney.com/{code}.html")
        if not text:
            return None

        try:
            info = FundInfo(code=code, source=self.name)

            # 基金名称
            m = re.search(r'fS_name\s*=\s*"([^"]+)"', text)
            if m:
                info.name = m.group(1)

            # 基金代码
            m = re.search(r'fS_code\s*=\s*"([^"]+)"', text)
            if m:
                info.code = m.group(1)

            # 基金类型
            m = re.search(r'fund_sourceRate.*?|.*?"([^"]+)"', text)
            # 尝试从其他字段提取类型
            m = re.search(r'fS_name\s*=\s*"[^"]*".*?', text)
            # 类型通常在 Data_netWorthTrend 附近，用更通用的方式
            type_match = re.search(r'"fundtype"\s*:\s*"([^"]+)"', text)
            if type_match:
                info.ftype = type_match.group(1)

            # 基金规模（从 Data_assetAllocation 或 fund_minsg 提取）
            scale_match = re.search(r'Data_assetAllocation\s*=\s*(\{[^}]+\})', text)
            # 规模通常在 fS_name 后面的变量中，尝试 fund_scale
            scale_match = re.search(r'fund_scale\s*=\s*([\d.]+)', text)
            if scale_match:
                try:
                    info.scale = float(scale_match.group(1))
                except (ValueError, TypeError):
                    pass

            # 基金经理
            manager_match = re.search(r'Data_currentFundManager\s*=\s*(\[.*?\])', text, re.DOTALL)
            if manager_match:
                try:
                    managers = json.loads(manager_match.group(1))
                    if managers:
                        info.manager = managers[0].get("name", "")
                except (json.JSONDecodeError, IndexError, KeyError):
                    pass

            # 成立日期
            est_match = re.search(r'fS_name\s*=\s*"[^"]*".*?(\d{4}-\d{2}-\d{2})', text)
            # 尝试 Data_buySedemption 或其他字段
            est_match = re.search(r'"fund_buy_date"\s*:\s*"([^"]+)"', text)
            if est_match:
                info.est = est_match.group(1)

            # 最新净值（从 Data_netWorthTrend 最后一个点）
            nav_match = re.search(r'Data_netWorthTrend\s*=\s*(\[.*?\])\s*;', text, re.DOTALL)
            if nav_match:
                try:
                    nav_data = json.loads(nav_match.group(1))
                    if nav_data:
                        latest = nav_data[-1]
                        # 时间戳转日期
                        ts = latest.get("x", 0)
                        if ts:
                            info.nav_date = time.strftime("%Y-%m-%d", time.localtime(ts / 1000))
                        info.nav = latest.get("y")
                except (json.JSONDecodeError, IndexError, KeyError, TypeError, ValueError, OSError):
                    pass

            # 缓存结果
            self._cache.set(self.name, "fund_info", code, info)
            return info

        except Exception:
            self._record_error()
            return None

    # ------------------------------------------------------------------
    # 最新净值
    # ------------------------------------------------------------------
    def get_latest_nav(self, code: str) -> Optional[NavPoint]:
        """获取基金最新净值。"""
        cached = self._cache.get(self.name, "latest_nav", code)
        if cached is not None:
            return cached

        # 从历史净值接口取最新一条
        history = self.get_nav_history(code, page_size=1)
        if history:
            latest = history[-1]
            self._cache.set(self.name, "latest_nav", code, latest)
            return latest
        return None

    # ------------------------------------------------------------------
    # 历史净值
    # ------------------------------------------------------------------
    def get_nav_history(self, code: str, page_size: int = 60,
                         start_date: Optional[str] = None,
                         end_date: Optional[str] = None) -> list[NavPoint]:
        """从天天基金 API 获取历史净值。"""
        cache_key = f"{code}_{page_size}_{start_date or ''}_{end_date or ''}"
        cached = self._cache.get(self.name, "nav_history", cache_key)
        if cached is not None:
            return cached

        real_size = 20  # 天天基金单页固定 20 条
        need_pages = max(1, -(-page_size // real_size))
        need_pages = min(need_pages, 10)  # 最多 10 页
        sd = (start_date or "").strip()
        ed = (end_date or "").strip()

        seen: dict[str, NavPoint] = {}
        for pg in range(1, need_pages + 1):
            url = (f"https://api.fund.eastmoney.com/f10/lsjz?fundCode={code}"
                   f"&pageIndex={pg}&pageSize={real_size}&startDate={sd}&endDate={ed}")
            text = self._http_get(url, referer="http://fundf10.eastmoney.com/")
            if not text:
                break
            try:
                data = json.loads(text)
            except (json.JSONDecodeError, ValueError):
                break
            if data.get("ErrCode") != 0:
                break
            lst = data.get("Data", {}).get("LSJZList", [])
            if not lst:
                break
            for it in lst:
                date = it.get("FSRQ")
                if not date:
                    continue
                if sd and date < sd:
                    break
                if ed and date > ed:
                    continue
                try:
                    ljjz = float(it.get("LJJZ") or 0) or float(it.get("DWJZ") or 0)
                    dwjz = float(it.get("DWJZ")) if it.get("DWJZ") else None
                    jzzzl = float(it.get("JZZZL")) if it.get("JZZZL") else None
                except (ValueError, TypeError):
                    continue
                seen[date] = NavPoint(
                    date=date, ljjz=ljjz, dwjz=dwjz, jzzzl=jzzzl
                )
            # 本页已触及区间下界
            if sd and min((it.get("FSRQ") or "") for it in lst) < sd:
                break
            time.sleep(0.05)

        items = sorted(seen.values(), key=lambda x: x.date)
        if items:
            self._cache.set(self.name, "nav_history", cache_key, items)
        return items

    # ------------------------------------------------------------------
    # 持仓
    # ------------------------------------------------------------------
    def get_holdings(self, code: str) -> list[HoldingItem]:
        """获取基金前十大持仓股票。"""
        cached = self._cache.get(self.name, "holdings", code)
        if cached is not None:
            return cached

        # 方法1：从 pingzhongdata 提取 stockCodes
        url = f"https://fund.eastmoney.com/pingzhongdata/{code}.js"
        text = self._http_get(url, referer=f"https://fund.eastmoney.com/{code}.html")
        if not text:
            return []

        holdings: list[HoldingItem] = []
        try:
            # 提取股票代码列表
            m = re.search(r'var stockCodes=(\[[^\]]*\])', text)
            if m:
                codes = json.loads(m.group(1))
                for c in codes[:10]:
                    holdings.append(HoldingItem(name=str(c), code=str(c), pct=0.0))

            # 方法2：尝试从 Data_stockPositions 提取详细持仓（含占比）
            pos_match = re.search(r'Data_stockPositions\s*=\s*(\[.*?\])\s*;', text, re.DOTALL)
            if pos_match:
                try:
                    positions = json.loads(pos_match.group(1))
                    if positions:
                        holdings = []
                        for p in positions[:10]:
                            holdings.append(HoldingItem(
                                name=p.get("name", ""),
                                code=p.get("code", ""),
                                pct=float(p.get("pct", 0) or 0)
                            ))
                except (json.JSONDecodeError, ValueError, TypeError):
                    pass
        except (json.JSONDecodeError, ValueError):
            pass

        self._cache.set(self.name, "holdings", code, holdings)
        return holdings

    # ------------------------------------------------------------------
    # 评分（天天基金不直接提供评分，返回 None）
    # ------------------------------------------------------------------
    def get_score(self, code: str) -> Optional[dict]:
        """天天基金不直接提供评分，返回 None。"""
        return None

    # ------------------------------------------------------------------
    # 健康检查
    # ------------------------------------------------------------------
    def health_check(self) -> bool:
        """通过请求一个已知基金验证数据源可用性。"""
        try:
            result = self.get_fund_info("000001")
            available = result is not None and result.name != ""
            self._available = available
            return available
        except Exception:
            self._available = False
            return False
