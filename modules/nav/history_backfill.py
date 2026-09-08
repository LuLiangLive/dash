"""
modules/nav/history_backfill.py —— 历史数据补全

v2.9.14新增: 部署时全量补全场景下，判断历史数据缺口，多数据源并行补全。

调用入口: backfill_history_if_needed(codes, verbose=False, on_progress=None) -> dict
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def backfill_history_if_needed(codes: list[str],
                                min_days: int = 250,
                                verbose: bool = False,
                                on_progress=None) -> dict:
    """历史数据补全：判断缺口，缺口大时多数据源并行补全

    Args:
        codes: 基金代码列表
        min_days: 最低要求的净值天数
        verbose: 是否打印详细日志
        on_progress: 进度回调 fn(done, total, code, msg)

    Returns:
        {
            "need_backfill": N,      # 需要补全的基金数量
            "backfilled": N,         # 实际补全的基金数量
            "use_multi_source": bool, # 是否使用了多源并行
            "stats": {...}           # 详细统计
        }
    """
    from modules.nav.multi_source_fetcher import (
        get_history_gap, should_use_multi_source, fetch_parallel, QUALITY_FULL
    )
    import db

    if not codes:
        return {"need_backfill": 0, "backfilled": 0, "use_multi_source": False, "stats": {}}

    # 1. 判断历史数据缺口
    need_backfill = get_history_gap(codes, min_days=min_days)
    if verbose:
        print(f"[历史补全] 检查{len(codes)}只基金，需要补全{len(need_backfill)}只")

    if not need_backfill:
        return {"need_backfill": 0, "backfilled": 0, "use_multi_source": False, "stats": {}}

    # 2. 判断是否使用多数据源并行
    use_multi = should_use_multi_source(len(need_backfill))
    if verbose:
        print(f"[历史补全] 缺口{len(need_backfill)}只，{'触发多源并行' if use_multi else '单源模式'}")

    # 3. 执行补全
    result = fetch_parallel(
        need_backfill,
        days=QUALITY_FULL,
        use_multi_source=use_multi,
        on_progress=on_progress
    )

    # 4. 保存补全结果到数据库
    backfilled = 0
    for code, data in result["results"].items():
        items = data.get("items", [])
        if items:
            try:
                # 转换格式并保存
                nav_items = [{"date": x["date"], "ljjz": x.get("ljjz"), "dwjz": x.get("dwjz")}
                             for x in items if x.get("date") and x.get("dwjz")]
                if nav_items:
                    db.bulk_upsert_navs(code, nav_items)
                    backfilled += 1
            except Exception as e:
                logger.debug(f"保存补全结果失败 {code}: {e}")

    if verbose:
        stats = result["stats"]
        print(f"[历史补全] 完成: 补全{backfilled}只, 合格{stats['full']}只, "
              f"部分{stats['partial']}只, 失败{stats['failed']}只")

    return {
        "need_backfill": len(need_backfill),
        "backfilled": backfilled,
        "use_multi_source": use_multi,
        "stats": result["stats"],
    }
