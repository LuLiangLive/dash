"""
fund_metrics.py —— 基金完整指标计算服务（v2.1.8）

用于一键更新时，对榜单+自选基金计算完整指标（用long_series获取260天完整数据），
只存结果到funds表，不保存原始净值到nav_history，减少数据库体积。

核心功能：
- compute_fund_metrics(code): 计算单只基金的完整指标
- batch_compute_metrics(codes): 批量计算多只基金的指标
- cleanup_old_navs(days): 清理nav_history表中超过指定天数的数据
"""
from __future__ import annotations

import time
from typing import Optional

import db


def compute_fund_metrics(code: str, verbose: bool = False) -> dict:
    """计算单只基金的完整指标（用long_series获取260天完整数据）。

    Returns:
        指标字典 {mdd, down_vol, max_daily_drop, mdd_days, mdd_status, vol, calmar, ...}
        计算失败返回空dict
    """
    try:
        from modules.nav.nav_series import long_series
        from collector.ranker import calc_metrics

        # 获取260天完整净值数据
        dates, vals = long_series(code, days=260)
        if len(vals) < 10:
            if verbose:
                pass
            return {}

        # v2.5.4新增: 数据完整性检查, 记录数据不足的情况便于排查
        if len(vals) < 260:
            # 数据不足260天, 记录日志(只在verbose模式下, 避免日志过多)
            if verbose:
                pass
            # 标记数据不完整, 后续指标可能不准确
            _data_incomplete = True
        else:
            _data_incomplete = False

        # 计算基础统计指标（使用ranker.calc_metrics，返回30+个完整指标）
        st = calc_metrics(vals) or {}

        # v2.1.9: 抗跌分全部指标统一为2个月（42个交易日）口径
        # 原calc_metrics使用全部260天数据，现统一为2个月口径
        # v2.9.13: 修复导入错误 - _max_dd/_daily_rets/_pstdev在modules.common.metrics中
        from modules.common.metrics import _max_dd, _daily_rets, _pstdev
        import math
        vals_2m = vals[-42:] if len(vals) > 42 else vals
        dates_2m = dates[-42:] if len(dates) > 42 else dates
        if len(vals_2m) >= 2:
            rets_2m = _daily_rets(vals_2m)
            # 区间最大回撤
            mdd_2m, mdd_days_2m, mdd_status_2m, _ = _max_dd(vals_2m)
            # v2.11.4 Q2: mdd/mdd_days/mdd_status 沿用 calc_metrics(:49) 的全序列口径,
            # 不再用近2月覆盖; mdd_2m 局部变量保留供 calmar(:88) 使用
            # 单日最大跌幅
            max_daily_drop_2m = min(rets_2m) if rets_2m else None
            st["max_daily_drop"] = max_daily_drop_2m
            # v2.9.8: 波动率增加数据完整性检查，至少需要30天数据才计算年化波动率
            # 下行波动率（2个月口径，年化）
            down_rets_2m = [r for r in rets_2m if r < 0]
            if len(down_rets_2m) >= 15:
                st["down_vol"] = _pstdev(down_rets_2m) * math.sqrt(252)
            else:
                st["down_vol"] = None
            # 年化波动率（2个月口径，年化）
            if len(rets_2m) >= 30:
                st["vol"] = _pstdev(rets_2m) * math.sqrt(252)
            else:
                st["vol"] = None
            # v2.9.8: 重新计算下行夏普比率和卡玛比率（用2个月口径，分子分母时间周期匹配）
            ret2m = (vals_2m[-1] / vals_2m[0] - 1) * 100 if len(vals_2m) >= 2 else None
            if ret2m is not None:
                st["ret2m"] = ret2m
                # 下行夏普比率 = 2个月收益 / 下行波动率
                if st.get("down_vol") and st["down_vol"] > 0:
                    st["down_sharpe"] = ret2m / st["down_vol"]
                # 卡玛比率 = 2个月收益 / 近2月最大回撤
                if mdd_2m and abs(mdd_2m) > 0.001:
                    st["calmar"] = ret2m / abs(mdd_2m)

        # 计算近6月最大回撤（126个交易日）
        from modules.nav.nav_series import window_mdd
        mdd6 = window_mdd(vals, window=126)
        mdd1y = window_mdd(vals, window=252)

        # 计算大跌日相关指标（2个月口径：只使用最近2个月范围内的大跌日）
        dd_avg = None
        repair_5d = None
        repair_10d = None
        try:
            from modules.fund.fund_service import load_idx
            idx_map, _ = load_idx()
            if idx_map:
                _items_idx = [(d, m) for d, m in idx_map.items() if isinstance(m, dict)]
                _ddown = [(d, m.get("sh", 0.0)) for d, m in sorted(_items_idx)
                          if m.get("sh", 0.0) <= -0.01 or m.get("cyb", 0.0) <= -0.04 or m.get("kc", 0.0) <= -0.04]
                if len(_ddown) < 3:
                    _ddown = [(d, m.get("sh", 0.0)) for d, m in sorted(_items_idx)
                              if m.get("sh", 0.0) < 0 or m.get("cyb", 0.0) < 0 or m.get("kc", 0.0) < 0]

                if _ddown:
                    # v2.1.9: 只使用最近2个月范围内的大跌日
                    lo_2m, hi_2m = dates_2m[0], dates_2m[-1]
                    in_range = [(d, r) for d, r in _ddown if lo_2m <= d <= hi_2m]
                    if in_range:
                        # 计算2个月范围内的基金日收益
                        fret_2m = {}
                        for i in range(1, len(dates_2m)):
                            fret_2m[dates_2m[i]] = (vals_2m[i] / vals_2m[i - 1] - 1) * 100
                        fund_rets = [fret_2m.get(d, 0) for d, _ in in_range if d in fret_2m]
                        if fund_rets:
                            dd_avg = sum(fund_rets) / len(fund_rets)

                        # 计算大跌后5日/10日反弹（使用2个月数据）
                        from modules.fund.fund_detail import rebound_avg
                        rebound = rebound_avg(dates_2m, vals_2m, {"down_days": len(in_range), "detail": [{"date": d} for d, _ in in_range]})
                        if rebound:
                            repair_5d = rebound.get("n5")
                            repair_10d = rebound.get("n10")
        except Exception as e:
            if verbose:
                pass

        # 组装结果
        # v2.9.13: 增加m3/m6/y1长期收益计算（从260天净值序列直接计算）
        _m3 = round((vals[-1] / vals[-63] - 1) * 100, 4) if len(vals) >= 64 and vals[-63] else None
        _m6 = round((vals[-1] / vals[-126] - 1) * 100, 4) if len(vals) >= 127 and vals[-126] else None
        _y1 = round((vals[-1] / vals[-252] - 1) * 100, 4) if len(vals) >= 253 and vals[-252] else None
        result = {
            "mdd": st.get("mdd"),
            "mdd6": mdd6,
            "mdd1y": mdd1y,
            "mdd_days": st.get("mdd_days"),
            "mdd_status": st.get("mdd_status"),
            "down_vol": st.get("down_vol"),
            "max_daily_drop": st.get("max_daily_drop"),
            "vol": st.get("vol"),
            "calmar": st.get("calmar"),
            "down_sharpe": st.get("down_sharpe"),
            "hi_cnt": st.get("hi_cnt"),
            "dd_from_hi": st.get("dd_from_hi"),
            "pl": st.get("pl"),
            "dd_avg": dd_avg,
            "repair_5d": repair_5d,
            "repair_10d": repair_10d,
            # 各周期收益
            "d3": st.get("d3"),
            "d5": st.get("d5"),
            "d7": st.get("d7"),
            "d10": st.get("d10"),
            "m1": st.get("m1"),
            "m3": _m3,
            "m6": _m6,
            "y1": _y1,
            "ret2m": st.get("ret2m"),
            "ret3m": st.get("ret3m"),
        }

        # 过滤掉None值
        result = {k: v for k, v in result.items() if v is not None}

        # 计算分数（线性评分，确保数据库中有分数）
        try:
            from collector.scoring_v3 import compute_earn_score, compute_ad_score, compute_dual_score
            # 收益分
            earn_score = compute_earn_score(result, None, None, None, repair_5d=result.get("repair_5d"))
            # 抗跌分
            ad_metrics = {
                "dd_avg": result.get("dd_avg"),
                "repair_5d": result.get("repair_5d"),
                "repair_10d": result.get("repair_10d"),
                "mdd": result.get("mdd"),
                "down_vol": result.get("down_vol"),
                "max_daily_drop": result.get("max_daily_drop"),
            }
            ad_score = compute_ad_score(ad_metrics)
            # v2.11.2: 卡玛分统一口径——单只实时计算无池内百分位, 与 recompute_ad /
            # rank_full._anti_score_simple 一致缺省中位50(真实百分位由 compute_pool_scores 池内计算后覆盖)。
            calmar = result.get("calmar", 0)
            calmar_score = 50
            # 综合分 = 收益42.5% + 抗跌42.5% + 卡玛15%(V3统一口径)
            score = compute_dual_score(earn_score, ad_score, calmar_score)
            result["earn_score"] = earn_score
            result["ad_score"] = ad_score
            result["calmar_score"] = calmar_score
            result["score"] = score
            result["tscore"] = score  # 同时设置tscore，前端优先使用此字段
        except Exception as e:
            if verbose:
                pass

        if verbose:
            pass

        return result

    except Exception as e:
        if verbose:
            import traceback
            print(f"[compute_fund_metrics] {code} 计算失败: {e}")
            traceback.print_exc()
        return {}


def batch_compute_metrics(codes: list[str], verbose: bool = True,
                           progress_cb=None) -> dict[str, dict]:
    """批量计算多只基金的完整指标。

    Args:
        codes: 基金代码列表
        verbose: 是否打印详细日志
        progress_cb: 进度回调 fn(done, total, code)

    Returns:
        {code: metrics_dict}
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import threading

    results = {}
    lock = threading.Lock()
    total = len(codes)
    done = 0

    def _work(code):
        nonlocal done
        try:
            metrics = compute_fund_metrics(code, verbose=False)
            with lock:
                done += 1
                if progress_cb:
                    try:
                        progress_cb(done, total, code)
                    except Exception:
                        pass
            return code, metrics
        except Exception as e:
            with lock:
                done += 1
            return code, {}

    # 并发计算（workers=4，避免对东方财富服务器造成太大压力）
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = {ex.submit(_work, c): c for c in codes}
        for fut in as_completed(futures):
            code, metrics = fut.result()
            if metrics:
                results[code] = metrics

    if verbose:
        pass

    return results


def save_metrics_to_db(code: str, metrics: dict) -> bool:
    """将计算好的指标保存到funds表。

    Args:
        code: 基金代码
        metrics: 指标字典

    Returns:
        是否保存成功
    """
    if not metrics:
        return False

    try:
        conn = db.get_conn()
        # 获取funds表的所有字段，只更新存在的字段（容错）
        rows = conn.execute("PRAGMA table_info(funds)").fetchall()
        valid_fields = {r["name"] for r in rows}

        # 构建UPDATE语句，只包含存在的字段
        fields = []
        values = []
        for k, v in metrics.items():
            if k in valid_fields and v is not None:
                fields.append(f"{k}=?")
                values.append(v)
        values.append(code)

        if fields:
            sql = f"UPDATE funds SET {', '.join(fields)} WHERE code=?"
            conn.execute(sql, values)
            conn.commit()
            return True
        return False
    except Exception as e:
        return False


def cleanup_old_navs(days: int = 90, verbose: bool = True, exclude_codes: list = None) -> int:
    """清理nav_history表中超过指定天数的数据。

    v2.5.4优化: 增加exclude_codes参数, 排除指定基金代码(如上榜+自选基金),
    这些基金保留更长时间的历史数据(如2年), 其他基金只保留指定天数。

    Args:
        days: 保留最近多少天的数据，默认90天
        verbose: 是否打印详细日志
        exclude_codes: 排除的基金代码列表, 这些基金不清理(保留更长时间)

    Returns:
        删除的记录数
    """
    try:
        import datetime as _dt
        cutoff = (_dt.date.today() - _dt.timedelta(days=days)).isoformat()

        conn = db.get_conn()

        # 构建排除条件
        if exclude_codes and len(exclude_codes) > 0:
            # 分批处理, 避免SQL语句过长
            batch_size = 500
            total_deleted = 0
            for i in range(0, len(exclude_codes), batch_size):
                batch = exclude_codes[i:i+batch_size]
                marks = ",".join("?" * len(batch))
                # 先统计要删除的数量
                row = conn.execute(
                    f"SELECT COUNT(*) as cnt FROM nav_history WHERE date < ? AND code NOT IN ({marks})",
                    (cutoff, *batch)
                ).fetchone()
                cnt = row["cnt"] if row else 0
                if cnt > 0:
                    conn.execute(
                        f"DELETE FROM nav_history WHERE date < ? AND code NOT IN ({marks})",
                        (cutoff, *batch)
                    )
                    conn.commit()
                    total_deleted += cnt
            if verbose:
                pass
            return total_deleted
        else:
            # 先统计要删除的数量
            row = conn.execute("SELECT COUNT(*) as cnt FROM nav_history WHERE date < ?", (cutoff,)).fetchone()
            cnt = row["cnt"] if row else 0

            if cnt > 0:
                conn.execute("DELETE FROM nav_history WHERE date < ?", (cutoff,))
                conn.commit()

            if verbose:
                pass

            return cnt
    except Exception as e:
        if verbose:
            pass
        return 0


