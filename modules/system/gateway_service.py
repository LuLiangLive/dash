"""
gateway_service.py —— 变更准入网关服务层

v2.9.23: 从router.py拆分出网关相关的辅助函数，包括：
- 影响分析（_analyze_impact）
- 重复检查（_check_duplicate）
- 任务清单生成（_generate_gateway_task_list）
- 架构匹配（_get_architecture_match）
- 实际验证（_validate_actual_artifacts）

API端点仍在router.py中，调用本模块的函数。
"""
from __future__ import annotations

import json

import time
from pathlib import Path

# 路径常量
ROOT = Path(__file__).resolve().parent.parent.parent
CHANGE_HISTORY_PATH = ROOT / "docs" / "change_history.json"
FUNCTION_GRAPH_PATH = ROOT / "docs" / "function_graph.json"

# 不参与影响传播的边类型（v2.9.23: 横切约束和文档同步不参与BFS）
NON_PROPAGATION_TYPES = {"constrains", "doc_sync"}


def _load_json(path):
    """加载JSON文件"""
    import json
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return None


def _save_json(path, data):
    """保存JSON文件"""
    import json
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _analyze_impact(graph, target_node_id):
    """分析节点变更的影响范围

    v2.9.23: BFS遍历时排除constrains（横切约束）和doc_sync（文档同步）边，
    这些边不参与功能影响传播，只表示约束关系和文档同步需求。
    """
    nodes = {n["id"]: n for n in graph.get("nodes", [])}
    edges = graph.get("edges", [])
    # v2.11.5: 边端点双命名兼容（历史坏边曾用 from/to 命名），
    # 避免 KeyError 导致网关/图谱接口整体失败
    edges = [
        {**e, "source": e.get("source", e.get("from", "")), "target": e.get("target", e.get("to", ""))}
        for e in edges
    ]

    # 找所有下游节点（BFS），排除constrains和doc_sync边
    downstream = set()
    queue = [target_node_id]
    while queue:
        curr = queue.pop(0)
        for e in edges:
            if (e["source"] == curr
                and e["target"] not in downstream
                and e.get("type") not in NON_PROPAGATION_TYPES):
                downstream.add(e["target"])
                queue.append(e["target"])

    # 找所有上游节点，同样排除constrains和doc_sync边
    upstream = set()
    queue = [target_node_id]
    while queue:
        curr = queue.pop(0)
        for e in edges:
            if (e["target"] == curr
                and e["source"] not in upstream
                and e.get("type") not in NON_PROPAGATION_TYPES):
                upstream.add(e["source"])
                queue.append(e["source"])

    # 受影响的节点详情
    affected_nodes = []
    for nid in downstream:
        if nid in nodes:
            node = nodes[nid]
            # 找连接边的类型
            edge_types = set()
            for e in edges:
                if e["source"] == target_node_id and e["target"] == nid:
                    edge_types.add(e.get("type", "unknown"))
            affected_nodes.append({
                "id": nid,
                "name": node.get("name", nid),
                "layer": node.get("layer", "unknown"),
                "description": node.get("description", ""),
                "edge_types": list(edge_types),
                "check_points": node.get("check_points", []),
                "known_issues": node.get("known_issues", []),
                "code_files": node.get("code_files", []),
                "doc_sections": node.get("doc_sections", []),
                "config_keys": node.get("config_keys", [])
            })

    # 按层级分组
    layer_order = ["data", "business", "display", "system"]
    affected_nodes.sort(key=lambda x: layer_order.index(x["layer"]) if x["layer"] in layer_order else 99)

    # 需要更新的文档
    docs_to_update = []
    for n in affected_nodes:
        for doc in n.get("doc_sections", []):
            if doc not in docs_to_update:
                docs_to_update.append(doc)

    # 需要检查的配置
    configs_to_check = []
    for n in affected_nodes:
        for cfg in n.get("config_keys", []):
            if cfg not in configs_to_check:
                configs_to_check.append(cfg)

    # 需要回归的测试路径
    test_paths = []
    for n in affected_nodes:
        for cp in n.get("check_points", []):
            test_paths.append({"node_id": n["id"], "node_name": n["name"], "check_point": cp})

    # 病点预警（有未修复或历史严重病点的节点）
    risk_warnings = []
    for n in affected_nodes:
        open_issues = [i for i in n.get("known_issues", []) if i.get("status") != "resolved"]
        high_severity = [i for i in n.get("known_issues", []) if i.get("severity") == "high"]
        if open_issues or high_severity:
            risk_warnings.append({
                "node_id": n["id"],
                "node_name": n["name"],
                "open_issues": len(open_issues),
                "high_severity": len(high_severity),
                "issues": open_issues[:3]
            })

    return {
        "target_node": target_node_id,
        "downstream_count": len(downstream),
        "upstream_count": len(upstream),
        "downstream_nodes": list(downstream),
        "upstream_nodes": list(upstream),
        "affected_nodes": affected_nodes,
        "docs_to_update": docs_to_update,
        "configs_to_check": configs_to_check,
        "test_paths": test_paths,
        "risk_warnings": risk_warnings
    }


def _check_duplicate(graph, history, title, target_node_ids):
    """检查是否有重复或相似的变更"""
    duplicates = []
    # 检查变更历史中是否有相似标题
    for c in history.get("changes", []):
        if c.get("status") in ["pending", "in_progress"]:
            # 简单相似度：标题关键词匹配
            title_words = set(title.replace("的", " ").replace("了", " ").split())
            c_words = set(c.get("title", "").replace("的", " ").replace("了", " ").split())
            overlap = title_words & c_words
            if len(overlap) >= 2 and len(overlap) / max(len(title_words), 1) > 0.5:
                duplicates.append({
                    "change_id": c["id"],
                    "title": c["title"],
                    "status": c["status"],
                    "created_at": c.get("created_at", ""),
                    "similarity": round(len(overlap) / max(len(title_words | c_words), 1), 2)
                })
    # v2.9.22 P1-3: 检查进行中变更的节点重叠度（交集>70%告警，不阻止）
    for c in history.get("changes", []):
        if c.get("status") in ["pending", "in_progress"]:
            c_targets = set(c.get("target_node_ids", []))
            new_targets = set(target_node_ids)
            if c_targets and new_targets:
                overlap = c_targets & new_targets
                overlap_ratio = len(overlap) / max(len(new_targets), 1)
                if overlap_ratio > 0.7:
                    duplicates.append({
                        "type": "node_overlap_warning",
                        "change_id": c["id"],
                        "title": c["title"],
                        "status": c["status"],
                        "overlap_ratio": round(overlap_ratio, 2),
                        "overlap_nodes": list(overlap),
                        "warning": f"与进行中变更节点重叠度{overlap_ratio:.0%}，请确认是否重复"
                    })

    # 检查节点是否有正在进行的变更
    nodes = {n["id"]: n for n in graph.get("nodes", [])}
    for nid in target_node_ids:
        if nid in nodes:
            node = nodes[nid]
            open_issues = [i for i in node.get("known_issues", []) if i.get("status") != "resolved"]
            if open_issues:
                duplicates.append({
                    "type": "node_open_issue",
                    "node_id": nid,
                    "node_name": node.get("name", nid),
                    "open_issues": len(open_issues),
                    "warning": f"节点{node.get('name', nid)}有{len(open_issues)}个未解决病点"
                })

    return duplicates


def _generate_gateway_task_list(graph, target_node_ids, change_type):
    """生成七类必做项任务清单"""
    nodes = {n["id"]: n for n in graph.get("nodes", [])}
    task_list = []
    task_id = 1

    # 收集所有受影响节点
    all_affected = set()
    all_code_files = set()
    all_doc_sections = set()
    all_config_keys = set()
    all_check_points = []

    for nid in target_node_ids:
        if nid in nodes:
            all_affected.add(nid)
            node = nodes[nid]
            all_code_files.update(node.get("code_files", []))
            all_doc_sections.update(node.get("doc_sections", []))
            all_config_keys.update(node.get("config_keys", []))
            for cp in node.get("check_points", []):
                all_check_points.append({"node_id": nid, "node_name": node.get("name", nid), "check_point": cp})
            # 分析下游影响
            impact = _analyze_impact(graph, nid)
            for n in impact.get("affected_nodes", []):
                all_affected.add(n["id"])
                all_code_files.update(n.get("code_files", []))
                all_doc_sections.update(n.get("doc_sections", []))
                all_config_keys.update(n.get("config_keys", []))

    # 1. 代码类任务
    if all_code_files:
        for cf in list(all_code_files)[:5]:  # 最多列5个
            task_list.append({
                "id": task_id,
                "category": "code",
                "description": f"修改代码: {cf}",
                "priority": "high",
                "completed": False
            })
            task_id += 1

    # 1b. 编码安全任务
    task_list.append({
        "id": task_id,
        "category": "code",
        "description": "编码安全：含中文文件(.vue/.py/.md)必须用Python脚本指定encoding=utf-8修改，禁止用PowerShell Set-Content",
        "priority": "high",
        "completed": False
    })
    task_id += 1

    # 2. 数据类任务（强制必做）
    task_list.append({
        "id": task_id,
        "category": "data",
        "description": "检查是否需要数据库表结构变更或数据迁移（如不涉及可直接标记完成）",
        "priority": "medium",
        "completed": False
    })
    task_id += 1

    # 3. 文档类任务（强制必做）
    task_list.append({
        "id": task_id,
        "category": "doc",
        "description": f"更新功能链文档（{len(all_doc_sections)}个章节）" if all_doc_sections else "更新功能链文档/变更说明",
        "priority": "medium",
        "completed": False
    })
    task_id += 1

    # 3b. 架构认知任务
    task_list.append({
        "id": task_id,
        "category": "doc",
        "description": "阅读系统架构文档（backend/docs/SYSTEM_ARCHITECTURE.md），确认变更涉及的模块和文件",
        "priority": "high",
        "completed": False
    })
    task_id += 1

    # 4. 测试类任务
    for cp in all_check_points[:5]:  # 最多列5个
        task_list.append({
            "id": task_id,
            "category": "test",
            "description": f"回归验证: {cp['node_name']} - {cp['check_point']}",
            "priority": "high",
            "completed": False
        })
        task_id += 1

    # 5. 神经网络同步任务（强制）
    task_list.append({
        "id": task_id,
        "category": "graph",
        "description": "更新功能神经网络：节点状态/版本/检查点/新增边",
        "priority": "high",
        "completed": False
    })
    task_id += 1

    # 6. 版本号升级任务（强制）
    task_list.append({
        "id": task_id,
        "category": "version",
        "description": "升级三处版本号：backend/config.py(APP_VERSION) + src/utils/version.ts + _pack.py(VERSION)",
        "priority": "high",
        "completed": False
    })
    task_id += 1

    # 7. 构建部署任务（强制）
    task_list.append({
        "id": task_id,
        "category": "build",
        "description": "构建前端(npm run build)并复制到backend/static，清理旧assets，验证页面可正常访问",
        "priority": "high",
        "completed": False
    })
    task_id += 1

    return task_list, all_affected


def _get_architecture_match(target_node_ids):
    """v2.9.22: 根据节点ID匹配相关的前后端文件和模块架构"""
    # 节点到架构模块的映射
    node_architecture_map = {
        "data_source": {"module": "数据源管理", "backend": ["modules/datasource/"], "frontend": ["src/api/"]},
        "fund_universe": {"module": "基金代码库", "backend": ["modules/funds/"], "frontend": []},
        "nav_fetch": {"module": "净值抓取", "backend": ["modules/funds/nav.py", "modules/funds/fetch.py"], "frontend": []},
        "data_quality": {"module": "数据质量校验", "backend": ["modules/common/data_quality.py"], "frontend": []},
        "holdings_fetch": {"module": "持仓抓取", "backend": ["modules/funds/holdings.py"], "frontend": []},
        "metrics_calc": {"module": "指标计算", "backend": ["modules/funds/metrics.py"], "frontend": []},
        "one_click_update": {"module": "一键更新", "backend": ["modules/system/update_task.py"], "frontend": ["src/views/MineView.vue"]},
        "rank_calc": {"module": "榜单计算", "backend": ["modules/ranks/"], "frontend": []},
        "watchlist": {"module": "自选管理", "backend": ["modules/watch/"], "frontend": ["src/views/WatchView.vue"]},
        "compare": {"module": "对比分析", "backend": ["modules/compare/"], "frontend": ["src/views/CompareView.vue"]},
        "portfolio": {"module": "持仓管理", "backend": ["modules/portfolio/"], "frontend": ["src/views/PortfolioView.vue"]},
        "profit_alert": {"module": "止盈提醒", "backend": ["modules/alerts/"], "frontend": []},
        "algo_config": {"module": "算法配置", "backend": ["algo_config.py"], "frontend": ["src/views/SettingsView.vue"]},
        "rank_page": {"module": "榜单页", "backend": [], "frontend": ["src/views/MarketView.vue", "src/components/FundCard.vue"]},
        "watch_page": {"module": "自选页", "backend": [], "frontend": ["src/views/WatchView.vue", "src/components/FundCard.vue"]},
        "compare_page": {"module": "对比页", "backend": [], "frontend": ["src/views/CompareView.vue"]},
        "portfolio_page": {"module": "持仓页", "backend": [], "frontend": ["src/views/PortfolioView.vue"]},
        "market_page": {"module": "市场页", "backend": [], "frontend": ["src/views/MarketView.vue"]},
        "mine_page": {"module": "我的页", "backend": [], "frontend": ["src/views/MineView.vue"]},
        "fund_card": {"module": "基金卡片", "backend": [], "frontend": ["src/components/FundCard.vue"]},
        "fund_modal": {"module": "基金弹窗", "backend": [], "frontend": ["src/components/FundModal.vue"]},
        "settings": {"module": "设置面板", "backend": [], "frontend": ["src/views/SettingsView.vue"]},
        "chain_doc": {"module": "功能链文档", "backend": ["docs/CHAIN_TEST_MANUAL.md"], "frontend": []},
        "system_monitor": {"module": "系统监控", "backend": ["modules/system/"], "frontend": ["src/views/MineView.vue"]},
        "log_manager": {"module": "日志管理", "backend": ["modules/logging/"], "frontend": []},
        "version_mgmt": {"module": "版本管理", "backend": ["config.py", "_pack.py"], "frontend": ["src/utils/version.ts"]},
        "change_gateway": {"module": "变更准入网关", "backend": ["modules/system/gateway_service.py", "modules/system/router.py"], "frontend": ["src/views/FunctionGraphView.vue"]},
        "ui_design": {"module": "UI设计规范", "backend": [], "frontend": ["src/styles/", "src/components/"]},
    }

    matches = []
    for nid in target_node_ids:
        if nid in node_architecture_map:
            matches.append({
                "node_id": nid,
                **node_architecture_map[nid]
            })
        else:
            matches.append({"node_id": nid, "module": "未知", "backend": [], "frontend": []})

    return matches


def _validate_actual_artifacts():
    """v2.9.22 P1: 实际验证——不只是检查completed字段，还要验证真实产物存在"""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent.parent
    issues = []

    # 1. 检查三处版本号一致性
    versions = {}
    # backend/config.py
    config_path = root / "config.py"
    if config_path.exists():
        content = config_path.read_text(encoding="utf-8")
        import re
        m = re.search(r'APP_VERSION["\']?\s*[,)]?\s*["\']([\d.]+)', content)
        if m:
            versions["config.py"] = m.group(1)
        else:
            issues.append("config.py中未找到APP_VERSION")
    else:
        issues.append("config.py不存在")

    # src/utils/version.ts
    version_ts = root.parent / "src" / "utils" / "version.ts"
    if version_ts.exists():
        content = version_ts.read_text(encoding="utf-8")
        import re
        m = re.search(r'APP_VERSION\s*=\s*["\']([\d.]+)', content)
        if m:
            versions["version.ts"] = m.group(1)
        else:
            issues.append("version.ts中未找到APP_VERSION")
    else:
        issues.append("version.ts不存在")

    # _pack.py
    pack_py = root.parent / "_pack.py"
    if pack_py.exists():
        content = pack_py.read_text(encoding="utf-8")
        import re
        m = re.search(r'VERSION\s*=\s*["\']([\d.]+)', content)
        if m:
            versions["_pack.py"] = m.group(1)
        else:
            issues.append("_pack.py中未找到VERSION")
    else:
        issues.append("_pack.py不存在")

    # backend/docs/function_graph.json meta.version（v2.11.5: 纳入第四处版本号）
    graph_json = root / "docs" / "function_graph.json"
    if graph_json.exists():
        try:
            gmeta = json.loads(graph_json.read_text(encoding="utf-8")).get("meta", {})
            if gmeta.get("version"):
                versions["function_graph.json"] = gmeta["version"]
        except Exception:
            issues.append("function_graph.json 解析失败")
    else:
        issues.append("function_graph.json不存在")

    # 检查版本号一致性
    version_values = set(versions.values())
    if len(version_values) > 1:
        issues.append(f"版本号不一致（config.py/version.ts/_pack.py/图谱meta）: {versions}")
    elif len(version_values) == 1:
        pass  # 一致

    # 2. 检查构建产物存在
    static_dir = root / "static"  # v2.11.5: root=backend
    if not static_dir.exists():
        issues.append("backend/static目录不存在（前端未构建）")
    else:
        index_html = static_dir / "index.html"
        if not index_html.exists():
            issues.append("backend/static/index.html不存在（前端未构建）")
        assets_dir = static_dir / "assets"
        if not assets_dir.exists():
            issues.append("backend/static/assets目录不存在（前端未构建）")

    return {
        "passed": len(issues) == 0,
        "issues": issues,
        "versions": versions,
        "reason": "; ".join(issues) if issues else "所有验证通过"
    }
