"""
modules/datasource/danjuan.py —— 蛋卷基金数据源实现

基于蛋卷基金公开 API（danjuanapp.com/djapi/），实现统一的数据源接口。

使用的公开接口：
1. /djapi/fund/{code} —— 基金基本信息
2. /djapi/fund/nav/history/{code} —— 历史净值
3. /djapi/fund/detail/{code} —— 基金详情（含持仓）

法律合规：仅使用蛋卷基金 APP 公开的、无需登录即可访问的数据接口。
"""
from __future__ import annotations

import json
import time
from typing import Optional

from .base import BaseDataSource, FundInfo, NavPoint, HoldingItem
from .cache import get_cache


class DanjuanSource(BaseDataSource):
    """蛋卷基金数据源。"""

    name = "danjuan"
    display_name = "蛋卷基金"
    base_url = "https://danjuanapp.com"
    request_interval = 0.15  # 请求间隔 150ms（蛋卷接口频率限制较严格）

    # 蛋卷 API 需要特定的 User-Agent 和 Header
    _HEADERS = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) "
                      "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 "
                      "danjuanapp/6.0.0",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": "https://danjuanapp.com/",
    }

    def __init__(self):
        super().__init__()
        self._cache = get_cache()

    # ------------------------------------------------------------------
    # HTTP 工具
    # ------------------------------------------------------------------
    def _http_get_json(self, url: str) -> Optional[dict]:
        """发送 GET 请求并解析 JSON，带频率控制和错误记录。"""
        self._wait_for_rate_limit()
        try:
            import httpx
            resp = httpx.get(url, headers=self._HEADERS, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                # 蛋卷 API 返回格式：{"result_code": 0, "data": {...}}
                if data.get("result_code") == 0 or data.get("code") == 0:
                    self._record_success()
                    return data.get("data", data)
                self._record_error()
                return None
            self._record_error()
            return None
        except Exception:
            self._record_error()
            return None

    # ------------------------------------------------------------------
    # 基金基本信息
    # ------------------------------------------------------------------
    def get_fund_info(self, code: str) -> Optional[FundInfo]:
        """从蛋卷 API 获取基金基本信息。"""
        cached = self._cache.get(self.name, "fund_info", code)
        if cached is not None:
            return cached

        url = f"https://danjuanapp.com/djapi/fund/{code}"
        data = self._http_get_json(url)
        if not data:
            return None

        try:
            info = FundInfo(code=code, source=self.name)

            # 基金名称
            info.name = data.get("fd_name", "") or data.get("name", "")

            # 基金代码
            info.code = data.get("fd_code", code) or code

            # 基金类型
            info.ftype = data.get("fund_type", "") or data.get("type", "")

            # 基金规模
            scale = data.get("fund_scale") or data.get("scale")
            if scale:
                try:
                    info.scale = float(scale)
                except (ValueError, TypeError):
                    pass

            # 基金经理
            manager = data.get("manager_name") or data.get("manager", "")
            if isinstance(manager, list):
                info.manager = ",".join(m.get("name", "") for m in manager if isinstance(m, dict))
            else:
                info.manager = str(manager)

            # 成立日期
            info.est = data.get("found_date", "") or data.get("establish_date", "")

            # 跟踪指数
            info.track = data.get("track_index", "") or data.get("index_name", "")

            # 最新净值
            nav_data = data.get("nav") or data.get("latest_nav") or {}
            if isinstance(nav_data, dict):
                info.nav = nav_data.get("nav") or nav_data.get("value")
                info.nav_date = nav_data.get("nav_date") or nav_data.get("date", "")
            else:
                # 可能直接是数值
                try:
                    info.nav = float(nav_data) if nav_data else None
                except (ValueError, TypeError):
                    pass

            # 板块分类
            info.sec = data.get("sec", "其他") or "其他"

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
        """从蛋卷 API 获取历史净值。"""
        cache_key = f"{code}_{page_size}_{start_date or ''}_{end_date or ''}"
        cached = self._cache.get(self.name, "nav_history", cache_key)
        if cached is not None:
            return cached

        # 蛋卷净值历史接口
        # page=1&size=20 格式
        url = (f"https://danjuanapp.com/djapi/fund/nav/history/{code}"
               f"?page=1&size={page_size}")
        data = self._http_get_json(url)
        if not data:
            return []

        items: list[NavPoint] = []
        try:
            # 蛋卷返回格式可能是 {"items": [...]} 或 {"list": [...]} 或直接是列表
            nav_list = data.get("items") or data.get("list") or data.get("nav_list") or []
            if isinstance(data, list):
                nav_list = data

            for it in nav_list:
                if not isinstance(it, dict):
                    continue
                date = it.get("nav_date") or it.get("date") or it.get("x")
                if not date:
                    continue
                # 处理时间戳格式
                if isinstance(date, (int, float)) and date > 1e12:
                    date = time.strftime("%Y-%m-%d", time.localtime(date / 1000))
                elif isinstance(date, (int, float)) and date > 1e9:
                    date = time.strftime("%Y-%m-%d", time.localtime(date))

                # 日期过滤
                if start_date and date < start_date:
                    continue
                if end_date and date > end_date:
                    continue

                try:
                    ljjz = float(it.get("total_nav") or it.get("ljjz") or it.get("accumulated_nav") or 0)
                    dwjz = it.get("nav") or it.get("dwjz") or it.get("unit_nav")
                    dwjz = float(dwjz) if dwjz else None
                    jzzzl = it.get("nav_rate") or it.get("jzzzl") or it.get("change_rate")
                    jzzzl = float(jzzzl) if jzzzl else None
                except (ValueError, TypeError):
                    continue

                if ljjz > 0:
                    items.append(NavPoint(
                        date=str(date), ljjz=ljjz, dwjz=dwjz, jzzzl=jzzzl
                    ))
        except Exception:
            self._record_error()
            return []

        items.sort(key=lambda x: x.date)
        if items:
            self._cache.set(self.name, "nav_history", cache_key, items)
        return items

    # ------------------------------------------------------------------
    # 持仓
    # ------------------------------------------------------------------
    def get_holdings(self, code: str) -> list[HoldingItem]:
        """获取基金前十大持仓。"""
        cached = self._cache.get(self.name, "holdings", code)
        if cached is not None:
            return cached

        url = f"https://danjuanapp.com/djapi/fund/detail/{code}"
        data = self._http_get_json(url)
        if not data:
            return []

        holdings: list[HoldingItem] = []
        try:
            # 持仓数据可能在 stock_positions / holdings / top_holdings 字段
            pos_data = (data.get("stock_positions") or
                        data.get("holdings") or
                        data.get("top_holdings") or
                        data.get("stock_holdings") or [])

            if isinstance(pos_data, dict):
                pos_data = pos_data.get("items") or pos_data.get("list") or []

            for p in pos_data[:10]:
                if not isinstance(p, dict):
                    continue
                try:
                    holdings.append(HoldingItem(
                        name=p.get("name", "") or p.get("stock_name", ""),
                        code=p.get("code", "") or p.get("stock_code", ""),
                        pct=float(p.get("pct", 0) or p.get("ratio", 0) or 0)
                    ))
                except (ValueError, TypeError):
                    continue
        except Exception:
            pass

        self._cache.set(self.name, "holdings", code, holdings)
        return holdings

    # ------------------------------------------------------------------
    # 评分（蛋卷基金提供蛋卷评分）
    # ------------------------------------------------------------------
    def get_score(self, code: str) -> Optional[dict]:
        """获取蛋卷基金评分（如果提供）。"""
        cached = self._cache.get(self.name, "score", code)
        if cached is not None:
            return cached

        url = f"https://danjuanapp.com/djapi/fund/{code}"
        data = self._http_get_json(url)
        if not data:
            return None

        try:
            score_data = data.get("score") or data.get("rating") or {}
            if isinstance(score_data, dict) and score_data:
                result = {
                    "score": score_data.get("score") or score_data.get("value"),
                    "rank": score_data.get("rank") or score_data.get("level"),
                    "source": self.name,
                }
                self._cache.set(self.name, "score", code, result)
                return result
            # 也可能直接是数值
            if isinstance(score_data, (int, float)):
                result = {"score": score_data, "source": self.name}
                self._cache.set(self.name, "score", code, result)
                return result
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------
    # 健康检查
    # ------------------------------------------------------------------
    def health_check(self) -> bool:
        """通过请求一个已知基金验证数据源可用性。"""
        try:
            result = self.get_fund_info("000001")
            available = result is not None
            self._available = available
            return available
        except Exception:
            self._available = False
            return False
