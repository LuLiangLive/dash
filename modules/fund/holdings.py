"""
modules/fund/holdings.py —— 持仓数据获取

从 collector.fetcher 迁移（v2.5.5架构重构 - 阶段4）
负责：基金前十大持仓、持仓主题、行业归类、持仓分布
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

from collector.http_utils import _get

ROOT = Path(__file__).resolve().parent.parent.parent
DATA = ROOT / "data"


def fetch_holdings_w(code: str) -> list:
    """基金前十大持仓 [(股票名, 占净值比例%), ...];网络失败重试2次,解析为空直接返回。

    v2026-08-29 性能优化: 原实现在「页面拿到了但解析不出持仓」时也继续重试,
    对 ETF / 指数基金(没有股票持仓数据)而言重试毫无意义, 单只白跑 3 次请求 + 0.8s
    sleep = 1.57s, 是部分基金自选添加慢的主因。现改为: 仅在请求失败(无响应)时重试。
    """
    for _attempt in range(3):
        txt = _get(f"https://fundf10.eastmoney.com/FundArchivesDatas.aspx?type=jjcc&code={code}&topline=10&year=&month=",
                   referer=f"https://fundf10.eastmoney.com/jjcc_{code}.html")
        if not txt:
            time.sleep(0.4)
            continue
        out = []
        for row in re.findall(r'<tr[^>]*>(.*?)</tr>', txt, re.S):
            tds = re.findall(r'<td[^>]*>(.*?)</td>', row, re.S)
            if len(tds) < 7:
                continue
            name = re.sub(r'<[^>]+>', '', tds[2]).strip()
            wm = re.search(r'([\d.]+)%', re.sub(r'<[^>]+>', '', tds[6]))
            if name and wm and name not in ("", "股票代码"):
                out.append((name, float(wm.group(1))))
        # 页面已拿到: 无论是否解析出持仓都直接返回, 不再重试
        return out[:10]
    return []


def fetch_holdings_w_full(code: str) -> list:
    """基金前十大持仓 [(股票名, 占净值比例%, 股票代码), ...]。

    与 fetch_holdings_w 的唯一差别是多返回「股票代码」, 供行业归类使用
    (持仓表格本身带代码列, 此前的实现只取了名称与占比、丢掉了代码)。
    """
    for _attempt in range(3):
        txt = _get(f"https://fundf10.eastmoney.com/FundArchivesDatas.aspx?type=jjcc&code={code}&topline=10&year=&month=",
                   referer=f"https://fundf10.eastmoney.com/jjcc_{code}.html")
        if not txt:
            time.sleep(0.4)
            continue
        out = []
        for row in re.findall(r'<tr[^>]*>(.*?)</tr>', txt, re.S):
            tds = re.findall(r'<td[^>]*>(.*?)</td>', row, re.S)
            if len(tds) < 7:
                continue
            sname = re.sub(r'<[^>]+>', '', tds[2]).strip()
            scode = re.sub(r'<[^>]+>', '', tds[1]).strip()
            wm = re.search(r'([\d.]+)%', re.sub(r'<[^>]+>', '', tds[6]))
            if sname and wm and sname not in ("", "股票代码"):
                out.append((sname, float(wm.group(1)), scode))
        return out[:10]
    return []


# 持仓主题标签:股票名→细分主题(按持仓比重聚合,与线上版 night_fund_monitor 一致)
THEME_KWS = {
    "CPO/光模块": ["中际旭创", "新易盛", "天孚通信", "光迅科技", "剑桥科技", "太辰光", "华工科技", "光库科技", "源杰科技", "腾景科技", "鼎通科技", "杰普特", "科创新源", "仕佳光子", "博创科技", "德科立", "联特科技", "铭普光磁", "华西股份", "兆龙互连"],
    "PCB": ["沪电股份", "生益科技", "深南电路", "兴森科技", "胜宏科技", "景旺电子", "东山精密", "鹏鼎控股", "世运电路", "威尔高", "金安国纪", "华正新材", "中富电路", "本川智能", "明阳电路"],
    "MLCC/被动元件": ["三环集团", "风华高科", "洁美科技", "国瓷材料", "江海股份", "顺络电子", "火炬电子", "宏达电子", "艾华集团"],
    "存储": ["兆易创新", "江波龙", "佰维存储", "北京君正", "澜起科技", "德明利", "香农芯创", "深科技", "同有科技"],
    "半导体设备": ["北方华创", "中微公司", "拓荆科技", "盛美上海", "华海清科", "至纯科技", "长川科技", "精测电子", "芯源微", "华峰测控", "晶升股份", "埃科光电", "微导纳米", "中科飞测", "赛腾股份", "正帆科技", "富创精密", "新莱应材"],
    "半导体材料": ["沪硅产业", "立昂微", "雅克科技", "江丰电子", "神工股份", "彤程新材", "鼎龙股份", "安集科技"],
    "AI算力/服务器": ["工业富联", "浪潮信息", "中科曙光", "寒武纪", "海光信息", "拓维信息", "神州数码", "紫光股份", "浪潮软件"],
    "AI应用/软件": ["金山办公", "科大讯飞", "用友网络", "恒生电子", "同花顺", "中科创达", "万兴科技", "金蝶国际"],
    "机器人": ["绿的谐波", "埃斯顿", "汇川技术", "双环传动", "鸣志电器", "拓斯达", "三花智控", "雷赛智能"],
    "光学/机器视觉": ["中润光学", "永新光学", "联创电子", "水晶光电", "福光股份", "奥普特", "天准科技", "矩子科技"],
    "消费电子/散热": ["中石科技", "领益智造", "飞荣达", "精研科技", "光弘科技", "传音控股", "立讯精密", "欣旺达", "歌尔股份", "蓝思科技", "闻泰科技"],
    "军工电子": ["中航光电", "航天电器", "中航沈飞", "中航西飞", "航发动力", "中兵红箭", "振华科技", "中航重机", "中直股份", "内蒙一机"],
    "锂电/新能源车": ["宁德时代", "比亚迪", "亿纬锂能", "赣锋锂业", "天齐锂业", "华友钴业", "恩捷股份", "璞泰来", "天赐材料", "国轩高科", "中伟股份", "容百科技"],
    "光伏": ["隆基绿能", "通威股份", "阳光电源", "天合光能", "晶澳科技", "福斯特", "大全能源", "晶科能源", "阿特斯", "钧达股份"],
    "创新药": ["恒瑞医药", "药明康德", "百济神州", "信达生物", "智飞生物", "康龙化成", "泰格医药", "凯莱英", "药明生物"],
    "医疗器械/服务": ["迈瑞医疗", "爱尔眼科", "联影医疗", "惠泰医疗", "爱博医疗", "乐普医疗", "通策医疗", "欧普康视", "大博医疗"],
    "中药/疫苗": ["片仔癀", "云南白药", "同仁堂", "东阿阿胶", "华润三九", "万泰生物", "沃森生物", "康泰生物"],
    "白酒/消费": ["贵州茅台", "五粮液", "泸州老窖", "山西汾酒", "洋河股份", "伊利股份", "海天味业"],
    "家电": ["美的集团", "格力电器", "海尔智家", "海信家电", "苏泊尔", "海信视像"],
    "汽车/零部件": ["长安汽车", "赛力斯", "长城汽车", "拓普集团", "伯特利", "银轮股份", "福耀玻璃", "星宇股份"],
    "传媒/游戏": ["三七互娱", "恺英网络", "世纪华通", "昆仑万维", "神州泰岳", "芒果超媒", "完美世界"],
    "通信设备": ["中兴通讯", "烽火通信", "亨通光电", "中天科技", "长飞光纤"],
    "云计算/IDC": ["宝信软件", "光环新网", "润泽科技", "数据港", "奥飞数据"],
    "网络安全": ["奇安信", "深信服", "启明星辰", "天融信", "安恒信息"],
    "工程机械": ["三一重工", "徐工机械", "中联重科", "恒立液压", "艾迪精密", "柳工"],
    "化工": ["万华化学", "华鲁恒升", "扬农化工", "卫星化学", "荣盛石化", "宝丰能源"],
    "电力/绿电": ["长江电力", "华能国际", "国投电力", "三峡能源", "龙源电力", "川投能源"],
    "煤炭/能源": ["中国神华", "陕西煤业", "兖矿能源", "山西焦煤", "潞安环能"],
    "农业/养殖": ["牧原股份", "温氏股份", "新希望", "海大集团", "隆平高科", "大北农"],
    "航空/机场": ["春秋航空", "中国国航", "南方航空", "上海机场", "白云机场", "吉祥航空"],
    "航运/港口": ["中远海控", "招商轮船", "上港集团", "宁波港", "中远海能"],
    "房地产": ["保利发展", "万科A", "招商蛇口", "金地集团", "滨江集团"],
    "金融": ["中信证券", "招商银行", "工商银行", "中国平安", "中国人寿", "华泰证券", "东方财富"],
    "有色/资源": ["紫金矿业", "北方稀土", "洛阳钼业", "山东黄金", "中金黄金", "中国石油", "中国海油"],
    "小金属/特钢": ["厦门钨业", "盛和资源", "宝钛股份", "抚顺特钢", "钢研高纳"],
    "消费/奢侈品": ["法拉利", "历峰", "路易威登", "LVMH", "爱马仕", "开云", "Kering", "欧莱雅", "万豪", "希尔顿", "Tapestry", "可口可乐", "耐克"],
}


def holdings_themes(holdings: list, limit: int = 3, use_industry: bool = True) -> list:
    """持仓 → 「概念优先 / 行业兜底」的分布标签,按持仓比重聚合降序。

    v1.1.8 重构: 原实现只用 THEME_KWS 人工概念白名单归类, 实测覆盖率仅约 44%
      (极端样本 010853 只有 22%), 页面上大半仓位会凭空消失。现在改为:
        1) 命中 THEME_KWS 热门概念 -> 概念名(如「CPO/光模块」「创新药」)
        2) 未命中且带股票代码的 A 股 -> 东财一级行业(如「医药生物」「基础化工」)
        3) 未命中且为港股          -> 「港股」(东财对港股不提供行业字段)
        4) 行业查询失败            -> 「其他」
      实测 4 只样本基金覆盖率 44% -> 100%。

    holdings: [(股票名, 占比%), ...] 或 [(股票名, 占比%, 股票代码), ...]
      只有三元组(带代码)才能走行业兜底; 二元组沿用旧行为 —— 仅按概念归类,
      未命中的直接丢弃(避免给历史调用方凭空塞进一堆「其他」)。
    """
    agg: dict = {}
    for item in holdings or []:
        try:
            name, w = item[0], float(item[1])
        except Exception:
            continue
        scode = item[2] if len(item) > 2 else ""
        label = None
        for theme, kws in THEME_KWS.items():
            if any(kw in name for kw in kws):
                label = theme
                break
        if label is None and use_industry and scode:
            mkt = _stock_market(scode)
            if mkt == "HK":
                label = "港股"
            else:
                ind = fetch_stock_industry(scode)
                label = ind or "其他"
        if label:
            agg[label] = agg.get(label, 0.0) + w
    return sorted(agg.items(), key=lambda x: x[1], reverse=True)[:limit]


# 股票行业缓存: 内存 + 磁盘双缓存(行业几乎不变, 可长期缓存)
_STOCK_INDUSTRY_CACHE: dict[str, str] = {}
_STOCK_INDUSTRY_FILE = DATA / "stock_industry_cache.json"
_STOCK_INDUSTRY_LOCK = threading.Lock()
_STOCK_INDUSTRY_LOADED = False


def _stock_market(scode: str) -> str:
    """股票代码 -> 市场后缀(东财 SECUCODE 用)。5 位纯数字代码视为港股。"""
    s = (scode or "").strip()
    if len(s) == 5 and s.isdigit():
        return "HK"
    if s.startswith("6"):
        return "SH"
    if s[:1] in ("4", "8"):
        return "BJ"
    return "SZ"


def _load_industry_cache() -> None:
    """一次性把行业缓存从磁盘载入内存(行业几乎不变, 可长期缓存)。"""
    global _STOCK_INDUSTRY_LOADED
    with _STOCK_INDUSTRY_LOCK:
        if _STOCK_INDUSTRY_LOADED:
            return
        try:
            if _STOCK_INDUSTRY_FILE.exists():
                _raw = json.loads(_STOCK_INDUSTRY_FILE.read_text(encoding="utf-8") or "{}")
                # v1.3.0: 历史文件里可能存过 null(瞬时失败被永久缓存), 载入时剔除让其重查
                _STOCK_INDUSTRY_CACHE.update(
                    {k: v for k, v in _raw.items() if v})
        except Exception:
            pass
        _STOCK_INDUSTRY_LOADED = True


def _save_industry_cache() -> None:
    """行业缓存落盘(仅成功结果; tmp+os.replace 原子写, 多线程安全)。"""
    try:
        with _STOCK_INDUSTRY_LOCK:
            _ok = {k: v for k, v in _STOCK_INDUSTRY_CACHE.items() if v}
            _STOCK_INDUSTRY_FILE.parent.mkdir(parents=True, exist_ok=True)
            _tmp = _STOCK_INDUSTRY_FILE.with_suffix(".json.tmp")
            _tmp.write_text(json.dumps(_ok, ensure_ascii=False), encoding="utf-8")
            os.replace(_tmp, _STOCK_INDUSTRY_FILE)
    except Exception:
        pass


def fetch_stock_industry(scode: str) -> Optional[str]:
    """查询个股的东财一级行业(如「医药生物」「基础化工」), 带内存 + 磁盘缓存。

    数据源: 东财 datacenter F10(实测 push2 行情接口在部分网络环境不可达,
    而 datacenter.eastmoney.com 可用)。港股不提供行业字段, 返回 None。

    v1.3.0: ① 查询失败(空结果/异常)重试一次; ② None(失败)只进内存缓存不落盘
    —— 旧实现把瞬时网络失败永久写进磁盘, 该股行业从此不再重查。
    """
    import urllib.parse

    scode = (scode or "").strip()
    if not scode:
        return None
    _load_industry_cache()
    if scode in _STOCK_INDUSTRY_CACHE:
        return _STOCK_INDUSTRY_CACHE[scode]
    mkt = _stock_market(scode)
    if mkt == "HK":
        return None                      # 港股无行业接口, 交由上层归为「港股」
    q = urllib.parse.quote('(SECUCODE="%s.%s")' % (scode, mkt))
    url = (
        "https://datacenter.eastmoney.com/securities/api/data/v1/get?"
        "reportName=RPT_F10_BASIC_ORGINFO&columns=SECUCODE,EM2016"
        "&filter=" + q + "&pageSize=1&source=HSF10&client=PC"
    )
    ind = None
    for _attempt in range(2):            # v1.3.0: 失败重试一次(限流/瞬时网络)
        try:
            txt = _get(url, referer="https://emweb.securities.eastmoney.com/")
            if txt:
                rows = ((json.loads(txt) or {}).get("result") or {}).get("data") or []
                if rows:
                    ind = ((rows[0].get("EM2016") or "").split("-")[0]) or None
        except Exception:
            ind = None
        if ind:
            break
        time.sleep(0.5)
    with _STOCK_INDUSTRY_LOCK:
        # 成功 → 内存缓存(落盘); 失败 → 仅内存缓存, 进程重启后重查
        _STOCK_INDUSTRY_CACHE[scode] = ind
    if ind:
        _save_industry_cache()
    return ind


# 十大持仓占比合计低于此值 → 认为「没有有意义的股票持仓」, 不展示持仓分布。
# 背景: ETF 联接基金的资金主要持有母基金, 十大股票合计常在 0.1%~0.5%
#   (实测 160424=0.1%、018173=0.03%、011613=1.2%), 展示出来全是 0.0% 的噪音。
#   主动权益基金的十大持仓合计普遍 >25%, 5% 的阈值不会误伤。
HOLDINGS_MIN_TOTAL = 5.0


def holdings_total(holdings_full: list) -> float:
    """十大持仓占比合计(%%)。"""
    t = 0.0
    for item in holdings_full or []:
        try:
            t += float(item[1])
        except Exception:
            pass
    return t


def holdings_breakdown(holdings_full: list, limit: int = 8,
                       min_total: float = HOLDINGS_MIN_TOTAL) -> list:
    """持仓分布(卡片「持仓分布」数据源)。

    归类口径见 holdings_themes: 概念优先 / 行业兜底。

    额外规则: 十大持仓占比合计 < min_total 时**返回空列表** —— ETF 联接基金
    的资金主要在母基金上, 股票持仓是个位小数, 展示「农林牧渔 0.1%」这类内容
    是噪音而非信息。返回空后前端「持仓分布」显示「—」。
    """
    if holdings_total(holdings_full) < min_total:
        return []
    return holdings_themes(holdings_full, limit=limit)


def fetch_holdings_many(codes: list[str], workers: int = 3) -> dict:
    """并行抓取持仓主题,返回 {code: [{name, pct}]};抓取为空时重试2次(限流兜底)。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import threading

    lock = threading.Lock()
    results = {}

    def work(code):
        th = holdings_themes(fetch_holdings_w(code))
        for _ in range(2):
            if th:
                break
            time.sleep(1.0)
            th = holdings_themes(fetch_holdings_w(code))
        with lock:
            results[code] = th
        time.sleep(0.2)
        return code

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(work, c) for c in codes]
        for fut in as_completed(futures):
            try:
                fut.result()
            except Exception:
                pass
    return results
