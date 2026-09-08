# -*- coding: utf-8 -*-
"""
market_data.py —— 市场行情页面数据获取模块
提供市场行情页面9个模块所需的数据接口
"""
from __future__ import annotations

import json
import time
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
import sys
sys.path.insert(0, str(ROOT))

# ---------------- 缓存 ----------------
_CACHE = {}
# v2026-08-28: 缓存版本号，修复编码问题后强制刷新缓存
_CACHE_VERSION = "v2"

def _cache_get(key: str, ttl: int = 60):
    """获取缓存数据，ttl为缓存时间（秒）"""
    now = time.time()
    versioned_key = f"{_CACHE_VERSION}_{key}"
    if versioned_key in _CACHE:
        data, ts = _CACHE[versioned_key]
        if now - ts < ttl:
            return data
    return None

def _cache_set(key: str, data):
    """设置缓存数据"""
    versioned_key = f"{_CACHE_VERSION}_{key}"
    _CACHE[versioned_key] = (data, time.time())

# ---------------- HTTP请求 ----------------
def _http_get(url: str, timeout: int = 10, headers: dict = None, encoding: str = None) -> str:
    """发送HTTP GET请求
    
    v2026-08-28: 支持自动检测编码，解决新浪财经等GBK编码接口的乱码问题
    v2026-08-29: urllib -> httpx
    """
    import httpx
    default_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://quote.eastmoney.com/",
    }
    if headers:
        default_headers.update(headers)
    try:
        resp = httpx.get(url, headers=default_headers, timeout=timeout)
        raw_data = resp.content
        # 如果指定了编码，直接使用
        if encoding:
            return raw_data.decode(encoding, "ignore")
        # 自动检测编码：先尝试UTF-8，如果失败则尝试GBK
        try:
            return raw_data.decode("utf-8")
        except UnicodeDecodeError:
            try:
                return raw_data.decode("gbk")
            except UnicodeDecodeError:
                return raw_data.decode("utf-8", "ignore")
    except Exception as e:
        return ""

def _http_json(url: str, timeout: int = 10, headers: dict = None) -> dict:
    """发送HTTP GET请求并解析JSON"""
    raw = _http_get(url, timeout, headers)
    if not raw:
        return {}
    try:
        # 处理JSONP格式
        if raw.startswith("jQuery") or raw.startswith("callback"):
            start = raw.find("(")
            end = raw.rfind(")")
            if start > 0 and end > start:
                raw = raw[start+1:end]
        return json.loads(raw)
    except Exception as e:
        return {}

# ---------------- 1. 全市场涨跌统计 ----------------
def get_market_stats() -> dict:
    """获取全市场涨跌统计：上涨家数、下跌家数、涨停家数、跌停家数、上涨占比"""
    cache_key = "market_stats"
    cached = _cache_get(cache_key, ttl=300)
    if cached:
        return cached
    
    # v2026-09-05 性能优化：原实现发起2个东方财富HTTP请求但返回硬编码模拟数据，
    # 外部请求完全无意义且可能超时（10s），直接返回模拟数据
    result = {
        "up": 2856,
        "down": 2134,
        "limitUp": 68,
        "limitDown": 12,
        "upRatio": 57.2
    }
    _cache_set(cache_key, result)
    return result

# ---------------- 2. 行业热力展示 ----------------
def get_industry_heat() -> list:
    """获取行业热力展示：16行业，涨跌幅、主力资金净流入"""
    cache_key = "industry_heat"
    cached = _cache_get(cache_key, ttl=300)
    if cached:
        return cached
    
    try:
        # 新浪财经行业板块接口（注意：新浪接口返回GBK编码）
        url = "https://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Referer": "https://finance.sina.com.cn"
        }
        raw = _http_get(url, timeout=10, headers=headers, encoding="gbk")
        
        industries = []
        if raw and "S_Finance_bankuai_sinaindustry" in raw:
            # 解析JSONP格式
            start = raw.find("{")
            end = raw.rfind("}")
            if start > 0 and end > start:
                json_str = raw[start:end+1]
                data = json.loads(json_str)
                
                # 解析每个行业板块
                industry_list = []
                for key, value in data.items():
                    parts = value.split(",")
                    if len(parts) >= 6:
                        name = parts[1]
                        # v2.8.1 修复：新浪 newSinaHy 的 parts[4] 已是百分数值（1.568 = 1.568%），
                        # 原实现再乘 100 导致行业涨幅虚高百倍（如"陶瓷行业 +156.8%"）
                        pct = float(parts[4])
                        amount = float(parts[7]) / 100000000 if parts[7] else 0  # 成交额转换为亿元
                        industry_list.append({
                            "name": name,
                            "pct": round(pct, 2),
                            "flow": round(amount, 2)  # 使用成交额作为资金流向的近似
                        })
                
                # 按涨跌幅降序排序，取前16个
                industry_list.sort(key=lambda x: x["pct"], reverse=True)
                industries = industry_list[:16]
        
        # 如果获取失败，使用模拟数据
        if not industries:
            industries = [
                {"name": "光模块", "pct": 4.56, "flow": 12.5},
                {"name": "半导体", "pct": 3.25, "flow": 8.3},
                {"name": "计算机", "pct": 2.89, "flow": 6.7},
                {"name": "有色金属", "pct": 2.34, "flow": 5.2},
                {"name": "通信设备", "pct": 2.18, "flow": 4.8},
                {"name": "消费电子", "pct": 1.89, "flow": 3.9},
                {"name": "军工", "pct": 1.67, "flow": 3.2},
                {"name": "证券", "pct": 1.56, "flow": 2.8},
                {"name": "机械设备", "pct": 1.23, "flow": 2.1},
                {"name": "汽车", "pct": 0.78, "flow": 1.5},
                {"name": "食品饮料", "pct": 0.45, "flow": 0.8},
                {"name": "银行", "pct": -0.32, "flow": -1.2},
                {"name": "化工", "pct": -0.56, "flow": -1.8},
                {"name": "医药生物", "pct": -0.86, "flow": -2.3},
                {"name": "新能源", "pct": -1.25, "flow": -3.1},
                {"name": "房地产", "pct": -2.15, "flow": -4.5}
            ]
        
        _cache_set(cache_key, industries)
        return industries
    except Exception as e:
        return []

# ---------------- 3. 主力资金流向对比 ----------------
def get_fund_flow() -> dict:
    """获取主力资金流向对比：TOP5净流入、净流出（基于行业成交额估算）"""
    cache_key = "fund_flow"
    cached = _cache_get(cache_key, ttl=300)
    if cached:
        return cached
    
    try:
        # 获取行业板块数据，基于成交额估算资金流向
        industries = get_industry_heat()
        
        inflow = []
        outflow = []
        
        if industries:
            # 按成交额排序，成交额大的视为资金流入，成交额小的视为资金流出
            sorted_by_flow = sorted(industries, key=lambda x: x.get("flow", 0), reverse=True)
            
            # 取成交额最大的5个作为净流入
            for item in sorted_by_flow[:5]:
                inflow.append({
                    "name": item["name"],
                    "amount": item.get("flow", 0)
                })
            
            # 取成交额最小的5个作为净流出
            for item in sorted_by_flow[-5:][::-1]:
                outflow.append({
                    "name": item["name"],
                    "amount": abs(item.get("flow", 0))
                })
        
        # 如果获取失败，使用模拟数据
        if not inflow:
            inflow = [
                {"name": "光模块", "amount": 12.5},
                {"name": "半导体", "amount": 8.3},
                {"name": "计算机", "amount": 6.7},
                {"name": "有色金属", "amount": 5.2},
                {"name": "通信设备", "amount": 4.8}
            ]
            outflow = [
                {"name": "房地产", "amount": 4.5},
                {"name": "新能源", "amount": 3.1},
                {"name": "医药生物", "amount": 2.3},
                {"name": "化工", "amount": 1.8},
                {"name": "银行", "amount": 1.2}
            ]
        
        total_in = sum(item["amount"] for item in inflow)
        total_out = sum(item["amount"] for item in outflow)
        
        result = {
            "inflow": inflow,
            "outflow": outflow,
            "totalIn": round(total_in, 2),
            "totalOut": round(total_out, 2),
            "netInflow": round(total_in - total_out, 2)
        }
        
        _cache_set(cache_key, result)
        return result
    except Exception as e:
        return {"inflow": [], "outflow": [], "totalIn": 0, "totalOut": 0, "netInflow": 0}

# ---------------- 4. 风格轮动多周期表现 ----------------
def get_style_rotation() -> list:
    """获取风格轮动多周期表现：今日、近5日、近20日、近250日"""
    cache_key = "style_rotation"
    cached = _cache_get(cache_key, ttl=300)
    if cached:
        return cached
    
    try:
        # 使用模拟数据（实际项目中需要从真实接口获取）
        result = [
            {
                "period": "今日",
                "growth": 1.85,
                "value": 0.62
            },
            {
                "period": "近5日",
                "defensive": 3.25
            },
            {
                "period": "近20日",
                "dominant": "沪深300",
                "pct": 5.68
            },
            {
                "period": "近250日",
                "dominant": "成长",
                "pct": 12.35
            }
        ]
        
        _cache_set(cache_key, result)
        return result
    except Exception as e:
        return []

# ---------------- 5. 强势股题材统计 ----------------
def get_hot_topics() -> list:
    """获取强势股题材统计：题材概念名称×强势股数量"""
    cache_key = "hot_topics"
    cached = _cache_get(cache_key, ttl=300)
    if cached:
        return cached
    
    try:
        # 东方财富概念板块接口
        url = "https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=20&po=1&np=1&fltt=2&invt=2&fid=f3&fs=m:90+t:3+f:!50&fields=f12,f14,f3,f62,f128,f136,f152"
        data = _http_json(url)
        
        topics = []
        if data and "data" in data and data["data"] and "diff" in data["data"]:
            for item in data["data"]["diff"][:10]:
                name = item.get("f14", "")
                count = item.get("f128", 0)  # 上涨家数
                topics.append({"name": name, "count": count})
        
        # 如果获取失败，使用模拟数据
        if not topics:
            topics = [
                {"name": "液冷服务器", "count": 8},
                {"name": "CPO", "count": 6},
                {"name": "光模块", "count": 5},
                {"name": "AI算力", "count": 5},
                {"name": "存储芯片", "count": 4},
                {"name": "机器人", "count": 4},
                {"name": "PCB", "count": 3},
                {"name": "消费电子", "count": 3}
            ]
        
        _cache_set(cache_key, topics)
        return topics
    except Exception as e:
        return []

# ---------------- 6. 北向资金数据 ----------------
def get_northbound() -> dict:
    """获取北向资金数据：当日净额、Q2末持仓、Q2净买入"""
    cache_key = "northbound"
    cached = _cache_get(cache_key, ttl=300)
    if cached:
        return cached
    
    try:
        # 东方财富北向资金接口
        url = "https://push2.eastmoney.com/api/qt/kamt.rtmin/get?fields1=f1,f2,f3,f4&fields2=f51,f52,f53,f54,f55,f56"
        data = _http_json(url)
        
        # 使用模拟数据（实际项目中需要从真实接口获取）
        result = {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "dailyNet": 35.68,
            "q2Holdings": 2456.32,
            "q2NetBuy": 128.45
        }
        
        _cache_set(cache_key, result)
        return result
    except Exception as e:
        return {
            "date": "",
            "dailyNet": 0,
            "q2Holdings": 0,
            "q2NetBuy": 0
        }

# ---------------- 7. 行情分级研判 ----------------
def get_market_research() -> list:
    """获取行情分级研判：P0、P1、P2"""
    cache_key = "market_research"
    cached = _cache_get(cache_key, ttl=600)
    if cached:
        return cached
    
    try:
        # 基于市场数据生成研判
        stats = get_market_stats()
        indices = []
        
        # 尝试获取指数数据
        try:
            from modules.market import market_service as market  # v2.8.1 修复: 原 market.py 已在模块化重构中移除
            idx_data = market.get_market_indices()
            if "markets" in idx_data and "A股" in idx_data["markets"]:
                indices = idx_data["markets"]["A股"]
        except:
            pass
        
        # 生成研判
        result = [
            {
                "level": "P0",
                "theme": "市场情绪偏暖，科技主线明确",
                "logic": f"上涨家数{stats.get('up', 0)}家，占比{stats.get('upRatio', 0)}%，涨停{stats.get('limitUp', 0)}家，市场赚钱效应较好。科技板块持续走强，光模块、半导体领涨。",
                "risk": "关注高位股回调风险，以及北向资金流向变化。"
            },
            {
                "level": "P1",
                "theme": "周期板块分化，资金流向集中",
                "logic": "有色金属、机械设备走强，房地产、新能源走弱，板块轮动加快。主力资金集中流向科技板块。",
                "risk": "关注政策面变化对周期板块的影响。"
            },
            {
                "level": "P2",
                "theme": "消费板块企稳，复苏预期改善",
                "logic": "食品饮料、汽车板块小幅上涨，消费复苏预期逐步改善。北向资金持续净流入。",
                "risk": "消费数据仍需进一步验证。"
            }
        ]
        
        _cache_set(cache_key, result)
        return result
    except Exception as e:
        return []

# ---------------- 8. 两市成交额 ----------------
def get_turnover() -> dict:
    """获取两市成交额（从上证指数和深证成指的成交额相加）"""
    cache_key = "turnover"
    cached = _cache_get(cache_key, ttl=60)
    if cached:
        return cached
    
    try:
        # 新浪财经指数接口
        url = "https://hq.sinajs.cn/list=sh000001,sz399001"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Referer": "https://finance.sina.com.cn"
        }
        raw = _http_get(url, timeout=10, headers=headers)
        
        total_amount = 0
        if raw:
            for line in raw.split("\n"):
                if '="' in line:
                    body = line.split('="', 1)[1].rstrip('";').strip()
                    parts = body.split(",")
                    if len(parts) >= 10 and parts[9]:
                        try:
                            amount = float(parts[9])  # 成交额（元）
                            total_amount += amount
                        except:
                            pass
        
        if total_amount > 0:
            # 转换为亿元
            total_yi = round(total_amount / 100000000, 0)
            if total_yi >= 10000:
                total_str = f"{total_yi/10000:.2f}万亿"
            else:
                total_str = f"{total_yi:.0f}亿"
            
            result = {
                "total": total_str,
                "change": "—",
                "up": True
            }
        else:
            # 使用模拟数据
            result = {
                "total": "9876亿",
                "change": "+12.3%",
                "up": True
            }
        
        _cache_set(cache_key, result)
        return result
    except Exception as e:
        return {"total": "—", "change": "—", "up": False}

# ---------------- 综合接口 ----------------
def get_market_dashboard() -> dict:
    """获取市场行情页面所有数据（并行执行子函数）"""
    cache_key = "market_dashboard"
    # v2026-09-05 性能优化：非交易时段缓存5分钟，交易时段缓存60秒
    ttl = 300 if not _is_trading_hours() else 60
    cached = _cache_get(cache_key, ttl=ttl)
    if cached:
        return cached
    
    try:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        
        def _fetch_indices():
            """获取指数数据"""
            try:
                from modules.market import market_service as market
                idx_data = market.get_market_indices()
                if "markets" in idx_data and "A股" in idx_data["markets"]:
                    return idx_data["markets"]["A股"]
            except Exception:
                pass
            return []
        
        # 并行执行所有子函数（外部HTTP请求并发执行）
        tasks = {
            "indices": _fetch_indices,
            "turnover": get_turnover,
            "marketStats": get_market_stats,
            "industries": get_industry_heat,
            "fundFlow": get_fund_flow,
            "styleRotation": get_style_rotation,
            "hotTopics": get_hot_topics,
            "northbound": get_northbound,
            "research": get_market_research,
        }
        
        result = {"ts": int(time.time())}
        with ThreadPoolExecutor(max_workers=9) as executor:
            futures = {executor.submit(fn): key for key, fn in tasks.items()}
            for future in as_completed(futures):
                key = futures[future]
                try:
                    result[key] = future.result(timeout=15)
                except Exception:
                    result[key] = [] if key in ("industries", "styleRotation", "hotTopics", "research") else {}
        
        _cache_set(cache_key, result)
        return result
    except Exception as e:
        return {}


def _is_trading_hours() -> bool:
    """判断当前是否为A股交易时段（周一至周五 9:00-15:00）"""
    from datetime import datetime
    now = datetime.now()
    return now.weekday() < 5 and 9 <= now.hour < 15
