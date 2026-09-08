"""
数据质量检查模块
- 净值数据覆盖率检查
- 最近一年数据完整性检查（v2.9.54新增）
- 重复数据检测
- 连续两天净值相同检测
- full_quality_check: 单基金数据质量校验（与rank_full.py兼容）
- QualityStats: 批量数据质量统计（与pipeline.py兼容）
"""
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta
from dataclasses import dataclass, field


@dataclass
class QualityResult:
    """数据质量检查结果（与rank_full.py兼容）"""
    d1: float = None  # 最新一日涨幅（suspect状态时为None）
    status: str = "ok"  # 数据状态：ok/suspect/stale/error
    suspect: bool = False  # 是否可疑（连续两天净值相同）


class QualityStats:
    """v2.9.19: 批量数据质量统计（与pipeline.py兼容）
    
    统计全量基金的数据状态分布，用于一键更新后的质量报告。
    """
    
    def __init__(self):
        self.total = 0
        self.ok = 0
        self.suspect = 0
        self.conflict = 0
        self.stale = 0
        self.error = 0
        self.other = 0
        self.suspect_ratio = 0.0
        self.should_alert = False
        self._finalized = False
    
    def add(self, status: str):
        """添加一个基金的数据状态"""
        self.total += 1
        status = (status or "ok").lower()
        if status == "ok":
            self.ok += 1
        elif status == "suspect":
            self.suspect += 1
        elif status == "conflict":
            self.conflict += 1
        elif status == "stale":
            self.stale += 1
        elif status == "error":
            self.error += 1
        else:
            self.other += 1
        self._finalized = False
    
    def finalize(self):
        """完成统计，计算比率等"""
        if self.total > 0:
            self.suspect_ratio = self.suspect / self.total
        else:
            self.suspect_ratio = 0.0
        # 超过5%阈值时告警
        self.should_alert = self.suspect_ratio > 0.05
        self._finalized = True
    
    def to_dict(self) -> dict:
        """转换为字典"""
        if not self._finalized:
            self.finalize()
        return {
            "total": self.total,
            "ok": self.ok,
            "suspect": self.suspect,
            "conflict": self.conflict,
            "stale": self.stale,
            "error": self.error,
            "other": self.other,
            "suspect_ratio": round(self.suspect_ratio, 4),
            "should_alert": self.should_alert,
        }


def full_quality_check(navs: list, nav_dates: list = None) -> QualityResult:
    """v2.9.19: 单基金数据质量校验
    
    检查最新两天净值是否相同（数据源延迟/重复），如果相同则标记为suspect，
    d1返回None，不参与排名。
    
    Args:
        navs: 净值列表（按日期升序）
        nav_dates: 日期列表（可选，用于日志）
    
    Returns:
        QualityResult: 包含d1和status
    """
    result = QualityResult()
    
    try:
        if not navs or len(navs) < 2:
            result.status = "stale"
            result.d1 = None
            return result
        
        latest = navs[-1]
        prev = navs[-2]
        
        if not latest or not prev:
            result.status = "stale"
            result.d1 = None
            return result
        
        # 检查连续两天净值是否相同（suspect状态）
        if abs(float(latest) - float(prev)) < 0.0001:
            result.suspect = True
            result.status = "suspect"
            result.d1 = None  # suspect状态时d1为None，不参与排名
        else:
            result.suspect = False
            result.status = "ok"
            result.d1 = round((float(latest) / float(prev) - 1) * 100, 4)
        
        return result
        
    except Exception as e:
        result.status = "error"
        result.d1 = None
        return result


def check_nav_coverage(db_path: str = 'fund.db', days: int = 10) -> dict:
    """检查最近N个交易日的净值数据覆盖率
    
    Returns:
        {
            'total_funds': int,           # 基金总数
            'dates': [str, ...],          # 检查的日期列表
            'coverage': {date: count},    # 每个日期有净值的基金数
            'coverage_pct': {date: pct},  # 每个日期的覆盖率
            'missing_funds': {date: [code, ...]},  # 每个日期缺失的基金
            'overall_coverage': float,     # 整体平均覆盖率
            'status': 'ok' | 'warning' | 'error',
            'warning_dates': [date, ...],  # 覆盖率低于95%的日期
        }
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    
    # 获取基金总数
    total_funds = conn.execute("SELECT COUNT(*) FROM funds").fetchone()[0]
    
    # 获取最近N个有数据的交易日
    recent_dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM nav_history ORDER BY date DESC LIMIT ?", (days,)
    ).fetchall()]
    recent_dates.reverse()  # 升序
    
    coverage = {}
    coverage_pct = {}
    missing_funds = {}
    
    for date in recent_dates:
        # 获取该日期有净值的基金数
        count = conn.execute(
            "SELECT COUNT(DISTINCT code) FROM nav_history WHERE date=?", (date,)
        ).fetchone()[0]
        coverage[date] = count
        coverage_pct[date] = round(count / total_funds * 100, 1) if total_funds > 0 else 0
        
        # 获取该日期缺失的基金（只返回前100个，避免数据量过大）
        missing = [r[0] for r in conn.execute("""
            SELECT code FROM funds 
            WHERE code NOT IN (SELECT DISTINCT code FROM nav_history WHERE date=?)
            LIMIT 100
        """, (date,)).fetchall()]
        missing_funds[date] = missing
    
    # 整体平均覆盖率
    overall_coverage = round(sum(coverage_pct.values()) / len(coverage_pct), 1) if coverage_pct else 0
    
    # 状态判断
    warning_dates = [d for d, p in coverage_pct.items() if p < 95]
    if overall_coverage >= 98 and not warning_dates:
        status = 'ok'
    elif overall_coverage >= 90 or len(warning_dates) <= 2:
        status = 'warning'
    else:
        status = 'error'
    
    conn.close()
    
    return {
        'total_funds': total_funds,
        'dates': recent_dates,
        'coverage': coverage,
        'coverage_pct': coverage_pct,
        'missing_funds': missing_funds,
        'overall_coverage': overall_coverage,
        'status': status,
        'warning_dates': warning_dates,
    }


def check_yearly_coverage(db_path: str = 'fund.db', min_days: int = 200) -> dict:
    """v2.9.54: 检查最近一年（365天）净值数据完整性
    
    统计每只基金最近一年的净值天数，判断是否达到最低要求（默认200天，约1年交易日的80%）。
    
    Args:
        db_path: 数据库路径
        min_days: 最近一年最低净值天数要求（默认200天）
    
    Returns:
        {
            'total_funds': int,           # 基金总数
            'complete_funds': int,        # 最近一年数据完整的基金数
            'complete_pct': float,        # 完整率（%）
            'incomplete_funds': int,      # 最近一年数据不足的基金数
            'incomplete_pct': float,      # 不足率（%）
            'distribution': {range: count},  # 按最近一年天数分布
            'avg_days': float,            # 平均最近一年天数
            'median_days': int,           # 中位数最近一年天数
            'missing_samples': [{'code': str, 'name': str, 'days': int}, ...],  # 前20个数据不足的基金
            'status': 'ok' | 'warning' | 'error',
            'target_date': str,           # 目标日期（最新净值日期）
            'yearly_since': str,          # 最近一年起始日期
            'min_days': int,              # 最低天数要求
        }
    """
    import sqlite3
    from datetime import datetime, timedelta
    
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    
    # 获取基金总数
    total_funds = conn.execute("SELECT COUNT(*) FROM funds").fetchone()[0]
    
    # 获取最新净值日期作为目标日期
    target_date_row = conn.execute(
        "SELECT MAX(date) as d FROM nav_history WHERE code NOT IN ('sh000001', 'sz399006', 'sh000688', 'sh000906')"
    ).fetchone()
    target_date = target_date_row['d'] if target_date_row and target_date_row['d'] else datetime.now().strftime('%Y-%m-%d')
    
    # 计算最近一年起始日期
    try:
        target_dt = datetime.strptime(target_date, '%Y-%m-%d')
        yearly_since = (target_dt - timedelta(days=365)).strftime('%Y-%m-%d')
    except Exception:
        yearly_since = target_date
    
    # 统计每只基金最近一年的净值天数
    yearly_stats = conn.execute("""
        SELECT f.code, f.name, COUNT(n.date) as yearly_days
        FROM funds f
        LEFT JOIN nav_history n ON f.code = n.code AND n.date >= ? AND n.dwjz IS NOT NULL
        GROUP BY f.code
        ORDER BY yearly_days ASC
    """, (yearly_since,)).fetchall()
    
    # 统计完整和不足的基金数
    complete_funds = sum(1 for r in yearly_stats if (r['yearly_days'] or 0) >= min_days)
    incomplete_funds = total_funds - complete_funds
    
    complete_pct = round(complete_funds / total_funds * 100, 1) if total_funds > 0 else 0
    incomplete_pct = round(incomplete_funds / total_funds * 100, 1) if total_funds > 0 else 0
    
    # 按最近一年天数分布
    distribution = {}
    ranges = [
        ('0-30天', 0, 30),
        ('30-60天', 30, 60),
        ('60-100天', 60, 100),
        ('100-150天', 100, 150),
        ('150-200天', 150, 200),
        ('200-250天', 200, 250),
        ('250天+', 250, 9999),
    ]
    for range_name, min_val, max_val in ranges:
        count = sum(1 for r in yearly_stats if min_val <= (r['yearly_days'] or 0) < max_val)
        if count > 0:
            distribution[range_name] = count
    
    # 计算平均和中位数
    all_days = [(r['yearly_days'] or 0) for r in yearly_stats]
    avg_days = round(sum(all_days) / len(all_days), 1) if all_days else 0
    sorted_days = sorted(all_days)
    median_days = sorted_days[len(sorted_days) // 2] if sorted_days else 0
    
    # 获取前20个数据不足的基金
    missing_samples = []
    for r in yearly_stats:
        if (r['yearly_days'] or 0) < min_days:
            missing_samples.append({
                'code': r['code'],
                'name': r['name'] or '',
                'days': r['yearly_days'] or 0
            })
            if len(missing_samples) >= 20:
                break
    
    # 状态判断
    if complete_pct >= 95:
        status = 'ok'
    elif complete_pct >= 80:
        status = 'warning'
    else:
        status = 'error'
    
    conn.close()
    
    return {
        'total_funds': total_funds,
        'complete_funds': complete_funds,
        'complete_pct': complete_pct,
        'incomplete_funds': incomplete_funds,
        'incomplete_pct': incomplete_pct,
        'distribution': distribution,
        'avg_days': avg_days,
        'median_days': median_days,
        'missing_samples': missing_samples,
        'status': status,
        'target_date': target_date,
        'yearly_since': yearly_since,
        'min_days': min_days,
    }


def check_duplicate_navs(db_path: str = 'fund.db') -> dict:
    """检查重复的净值记录（同一基金同一日期有多条记录）
    
    Returns:
        {
            'total_duplicates': int,  # 重复记录总数
            'duplicate_funds': int,   # 有重复记录的基金数
            'samples': [{'code': str, 'date': str, 'count': int}, ...],  # 前20个重复样本
        }
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    
    # 查找重复记录
    duplicates = conn.execute("""
        SELECT code, date, COUNT(*) as cnt 
        FROM nav_history 
        GROUP BY code, date 
        HAVING cnt > 1
        ORDER BY cnt DESC
        LIMIT 20
    """).fetchall()
    
    # 统计重复记录总数
    total_dup = conn.execute("""
        SELECT SUM(cnt - 1) FROM (
            SELECT code, date, COUNT(*) as cnt 
            FROM nav_history 
            GROUP BY code, date 
            HAVING cnt > 1
        )
    """).fetchone()[0] or 0
    
    # 有重复记录的基金数
    dup_funds = conn.execute("""
        SELECT COUNT(DISTINCT code) FROM (
            SELECT code, date FROM nav_history 
            GROUP BY code, date HAVING COUNT(*) > 1
        )
    """).fetchone()[0]
    
    samples = [{'code': r['code'], 'date': r['date'], 'count': r['cnt']} for r in duplicates]
    
    conn.close()
    
    return {
        'total_duplicates': total_dup,
        'duplicate_funds': dup_funds,
        'samples': samples,
    }


def cleanup_duplicate_navs(db_path: str = 'fund.db') -> dict:
    """清理重复的净值记录，保留最新的一条（按rowid）
    
    Returns:
        {
            'deleted': int,  # 删除的重复记录数
            'before': int,   # 清理前记录数
            'after': int,    # 清理后记录数
        }
    """
    conn = sqlite3.connect(db_path)
    
    before = conn.execute("SELECT COUNT(*) FROM nav_history").fetchone()[0]
    
    # 删除重复记录，保留rowid最大的一条
    deleted = conn.execute("""
        DELETE FROM nav_history 
        WHERE rowid NOT IN (
            SELECT MAX(rowid) FROM nav_history 
            GROUP BY code, date
        )
    """).rowcount
    
    conn.commit()
    
    after = conn.execute("SELECT COUNT(*) FROM nav_history").fetchone()[0]
    
    conn.close()
    
    return {
        'deleted': deleted,
        'before': before,
        'after': after,
    }


if __name__ == '__main__':
    # 测试
    print("=== 净值数据覆盖率检查 ===")
    result = check_nav_coverage()
    print(f"基金总数: {result['total_funds']}")
    print(f"检查日期: {result['dates']}")
    print(f"整体覆盖率: {result['overall_coverage']}%")
    print(f"状态: {result['status']}")
    print(f"警告日期: {result['warning_dates']}")
    for date in result['dates']:
        print(f"  {date}: {result['coverage'][date]}/{result['total_funds']} ({result['coverage_pct'][date]}%)")
    
    print("\n=== 最近一年数据完整性检查 ===")
    yearly = check_yearly_coverage()
    print(f"目标日期: {yearly['target_date']}")
    print(f"最近一年起始: {yearly['yearly_since']}")
    print(f"最低天数要求: {yearly['min_days']}天")
    print(f"基金总数: {yearly['total_funds']}")
    print(f"数据完整: {yearly['complete_funds']}只 ({yearly['complete_pct']}%)")
    print(f"数据不足: {yearly['incomplete_funds']}只 ({yearly['incomplete_pct']}%)")
    print(f"平均天数: {yearly['avg_days']}天")
    print(f"中位数天数: {yearly['median_days']}天")
    print(f"状态: {yearly['status']}")
    print("天数分布:")
    for range_name, count in yearly['distribution'].items():
        print(f"  {range_name}: {count}只")
    if yearly['missing_samples']:
        print("数据不足样本（前10只）:")
        for s in yearly['missing_samples'][:10]:
            print(f"  {s['code']} {s['name']}: {s['days']}天")
    
    print("\n=== 重复数据检查 ===")
    dup = check_duplicate_navs()
    print(f"重复记录总数: {dup['total_duplicates']}")
    print(f"有重复记录的基金数: {dup['duplicate_funds']}")
    if dup['samples']:
        print("重复样本:")
        for s in dup['samples'][:5]:
            print(f"  {s['code']} {s['date']}: {s['count']}条")
    
    print("\n=== full_quality_check 测试 ===")
    # 测试正常情况
    qa1 = full_quality_check([1.0, 1.01, 1.02], ['2026-09-01', '2026-09-02', '2026-09-03'])
    print(f"正常情况: d1={qa1.d1}, status={qa1.status}, suspect={qa1.suspect}")
    
    # 测试suspect情况（连续两天净值相同）
    qa2 = full_quality_check([1.0, 1.01, 1.01], ['2026-09-01', '2026-09-02', '2026-09-03'])
    print(f"suspect情况: d1={qa2.d1}, status={qa2.status}, suspect={qa2.suspect}")
    
    # 测试数据不足情况
    qa3 = full_quality_check([1.0], ['2026-09-01'])
    print(f"数据不足: d1={qa3.d1}, status={qa3.status}, suspect={qa3.suspect}")
    
    print("\n=== QualityStats 测试 ===")
    qs = QualityStats()
    qs.add("ok")
    qs.add("ok")
    qs.add("suspect")
    qs.add("stale")
    qs.finalize()
    print(f"总计: {qs.total}, ok={qs.ok}, suspect={qs.suspect}, stale={qs.stale}")
    print(f"suspect比率: {qs.suspect_ratio*100:.1f}%, 告警: {qs.should_alert}")
    print(f"字典: {qs.to_dict()}")
