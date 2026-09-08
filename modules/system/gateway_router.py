"""
api/gateway_router.py —— 变更准入网关路由模块（v2.9.23从router.py拆分）

包含：
- 网关辅助函数：影响分析BFS、重复检查、实际验证、架构匹配、任务清单生成
- 网关API：创建变更、开始变更、完成验证、更新任务、进行中变更列表

所有路由通过 APIRouter 注册，main.py 中 include_router 引入。
"""
from __future__ import annotations

import time
from pathlib import Path

from fastapi import APIRouter, Body, Depends, Query
from pydantic import BaseModel

# 从router.py导入公共工具和Pydantic模型
from modules.system.router import (
    ROOT,
    CHANGE_HISTORY_PATH,
    FUNCTION_GRAPH_PATH,
    GATEWAY_CONFIG_PATH,
    _load_json,
    _save_json,
    _load_gateway_config,
    ChangeAnalyzeRequest,
    ChangeCreateRequest,
    ChangeUpdateRequest,
    ChangeGatewayRequest,
)

router = APIRouter(tags=["变更准入网关"])

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
    
    # 不参与影响传播的边类型
    NON_PROPAGATION_TYPES = {"constrains", "doc_sync"}
    
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
                "open_issues": open_issues,
                "high_severity_history": high_severity,
                "warning": f"节点「{n['name']}」有{len(open_issues)}个未修复病点、{len(high_severity)}个历史高危病点，需重点验证"
            })
    
    # 生成任务清单
    task_list = []
    task_id = 1
    
    # 高优先级：直接下游的核心业务节点
    for n in affected_nodes:
        if n["layer"] == "business" and "impact" in n["edge_types"]:
            task_list.append({
                "id": task_id,
                "description": f"验证「{n['name']}」功能正常",
                "node_id": n["id"],
                "priority": "high",
                "completed": False
            })
            task_id += 1
    
    # 中优先级：展示层节点
    for n in affected_nodes:
        if n["layer"] == "display":
            task_list.append({
                "id": task_id,
                "description": f"检查「{n['name']}」页面显示正确",
                "node_id": n["id"],
                "priority": "medium",
                "completed": False
            })
            task_id += 1
    
    # 文档同步
    if docs_to_update:
        task_list.append({
            "id": task_id,
            "description": f"同步更新功能链文档（{len(docs_to_update)}个章节）",
            "node_id": None,
            "priority": "medium",
            "completed": False
        })
        task_id += 1
    
    # 病点验证
    for w in risk_warnings:
        task_list.append({
            "id": task_id,
            "description": f"重点验证「{w['node_name']}」历史病点不复发",
            "node_id": w["node_id"],
            "priority": "high",
            "completed": False
        })
        task_id += 1
    
    # v2.9.22 P1-4: 上游节点详情（只展示不强制，提醒可能需要调整上游）
    upstream_nodes = []
    for nid in upstream:
        if nid in nodes and nid != target_node_id:
            node = nodes[nid]
            upstream_nodes.append({
                "id": nid,
                "name": node.get("name", nid),
                "layer": node.get("layer", "unknown"),
                "description": node.get("description", ""),
                "note": "上游节点，变更可能需要其提供新数据或字段"
            })
    
    return {
        "target_node": nodes.get(target_node_id, {}),
        "downstream_count": len(downstream),
        "upstream_count": len(upstream),
        "affected_nodes": affected_nodes,
        "upstream_nodes": upstream_nodes,
        "docs_to_update": docs_to_update,
        "configs_to_check": configs_to_check,
        "test_paths": test_paths,
        "risk_warnings": risk_warnings,
        "task_list": task_list
    }


@router.post("/api/function-graph/change/analyze", summary="分析变更影响", responses={200: {"description": "返回受影响节点、文档、配置、测试路径和病点预警"}})
async def change_analyze(body: ChangeAnalyzeRequest):
    """分析指定节点变更的影响范围，生成任务清单和病点预警。"""
    try:
        graph = _load_json(FUNCTION_GRAPH_PATH)
        if not graph:
            return {"ok": False, "msg": "功能图谱不存在"}
        
        nodes = {n["id"]: n for n in graph.get("nodes", [])}
        if body.target_node_id not in nodes:
            return {"ok": False, "msg": f"节点 {body.target_node_id} 不存在"}
        
        impact = _analyze_impact(graph, body.target_node_id)
        return {"ok": True, "analysis": impact}
    except Exception as e:
        return {"ok": False, "msg": str(e)[:200]}


@router.post("/api/function-graph/change/create", summary="创建变更记录（已废弃，请使用网关）", responses={200: {"description": "返回废弃提示"}})
async def change_create(body: ChangeCreateRequest):
    """v2.9.22: 此API已废弃。所有变更必须通过变更准入网关创建，以确保七类必做项齐全和影响分析完整。"""
    return {
        "ok": False,
        "msg": "此API已废弃，请使用 POST /api/function-graph/change/gateway 创建变更记录",
        "deprecated": True,
        "replacement": "/api/function-graph/change/gateway"
    }


@router.get("/api/function-graph/change/history", summary="获取变更历史", responses={200: {"description": "返回所有变更记录"}})
async def change_history(limit: int = Query(default=20)):
    """获取变更历史记录列表。"""
    try:
        history = _load_json(CHANGE_HISTORY_PATH) or {"meta": {"version": "1.0", "total_changes": 0}, "changes": []}
        changes = history.get("changes", [])[:limit]
        return {"ok": True, "changes": changes, "total": len(history.get("changes", []))}
    except Exception as e:
        return {"ok": False, "msg": str(e)[:200]}


@router.post("/api/function-graph/change/update", summary="更新变更状态", responses={200: {"description": "返回更新后的变更记录"}})
async def change_update(body: ChangeUpdateRequest):
    """更新变更记录的状态或任务完成情况。"""
    try:
        import time
        history = _load_json(CHANGE_HISTORY_PATH)
        if not history:
            return {"ok": False, "msg": "变更历史不存在"}
        
        change = None
        for c in history.get("changes", []):
            if c["id"] == body.change_id:
                change = c
                break
        
        if not change:
            return {"ok": False, "msg": f"变更记录 {body.change_id} 不存在"}
        
        if body.status:
            if body.status == "completed":
                return {"ok": False, "msg": "禁止直接标记完成，请使用验证门: POST /api/function-graph/change/gateway/{change_id}/complete"}
            change["status"] = body.status
            if body.status == "completed":
                change["completed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S+08:00")
        
        if body.task_updates:
            for update in body.task_updates:
                for task in change.get("task_list", []):
                    if task["id"] == update.get("task_id"):
                        task["completed"] = update.get("completed", False)
        
        # v2.9.23: 动态添加任务
        added_tasks = []
        if body.task_additions:
            max_id = max([t.get("id", 0) for t in change.get("task_list", [])], default=0)
            for addition in body.task_additions:
                max_id += 1
                new_task = {
                    "id": max_id,
                    "category": addition.get("category", "code"),
                    "description": addition.get("description", ""),
                    "priority": addition.get("priority", "medium"),
                    "completed": False,
                    "dynamic_added": True
                }
                change["task_list"].append(new_task)
                added_tasks.append(new_task)
        
        # v2.9.23: 动态删除任务
        removed_count = 0
        if body.task_removals:
            original_len = len(change.get("task_list", []))
            change["task_list"] = [
                t for t in change.get("task_list", [])
                if t.get("id") not in body.task_removals
            ]
            removed_count = original_len - len(change["task_list"])
        
        _save_json(CHANGE_HISTORY_PATH, history)
        return {
            "ok": True, 
            "change": change,
            "added_tasks": len(added_tasks),
            "removed_tasks": removed_count
        }
    except Exception as e:
        return {"ok": False, "msg": str(e)[:200]}


# ---------------------------------------------------------------------------
# v2.9.20: 变更准入网关 — 所有功能更新/新增的强制前置入口
# ---------------------------------------------------------------------------


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
                    "open_issues": [{"id": i["id"], "title": i["title"]} for i in open_issues]
                })
    return duplicates




def _validate_actual_artifacts() -> dict:
    """v2.9.22 P1: 实际验证，检查版本号和构建产物是否真的存在。"""
    import re
    from pathlib import Path
    
    backend_root = Path(__file__).parent.parent.parent
    
    # 1. 检查三处版本号是否一致
    versions = {}
    
    # config.py
    config_path = backend_root / "config.py"
    if config_path.exists():
        with open(config_path, 'r', encoding='utf-8') as f:
            config_content = f.read()
        m = re.search(r'APP_VERSION["\']?\s*[,)]?\s*["\']([\d.]+)["\']', config_content)
        if m:
            versions['config.py'] = m.group(1)
    
    # version.ts
    version_ts_path = backend_root.parent / "src" / "utils" / "version.ts"
    if version_ts_path.exists():
        with open(version_ts_path, 'r', encoding='utf-8') as f:
            ts_content = f.read()
        m = re.search(r"APP_VERSION\s*=\s*['\"]([\d.]+)['\"]", ts_content)
        if m:
            versions['version.ts'] = m.group(1)
    
    # _pack.py
    pack_path = backend_root.parent / "_pack.py"
    if pack_path.exists():
        with open(pack_path, 'r', encoding='utf-8') as f:
            pack_content = f.read()
        m = re.search(r"VERSION\s*=\s*['\"]([\d.]+)['\"]", pack_content)
        if m:
            versions['_pack.py'] = m.group(1)
    
    # backend/docs/function_graph.json meta.version（v2.11.5: 纳入第四处版本号）
    graph_json_path = backend_root / "docs" / "function_graph.json"
    if graph_json_path.exists():
        try:
            gmeta = json.loads(graph_json_path.read_text(encoding="utf-8")).get("meta", {})
            if gmeta.get("version"):
                versions['function_graph.json'] = gmeta["version"]
        except Exception:
            pass

    # 检查版本号一致性
    version_values = list(versions.values())
    if len(version_values) >= 2 and len(set(version_values)) > 1:
        return {
            "passed": False,
            "reason": f"版本号不一致: {versions}",
            "versions": versions
        }
    
    # 2. 检查构建产物是否存在
    static_assets = backend_root / "static" / "assets"
    if not static_assets.exists() or not any(static_assets.iterdir()):
        return {
            "passed": False,
            "reason": "构建产物不存在: backend/static/assets/ 为空，请先运行 npm run build",
            "versions": versions
        }
    
    return {
        "passed": True,
        "reason": "验证通过",
        "versions": versions,
        "build_artifacts": True
    }


def _get_architecture_match(target_node_ids: list) -> dict:
    """v2.9.23: 根据变更涉及的节点，匹配系统架构中的相关模块和文件。从gateway_config.json加载。"""
    # v2.9.23: 从配置文件加载节点架构映射（不再硬编码）
    gateway_config = _load_gateway_config()
    node_architecture_map = gateway_config.get("node_architecture_map", {})
    if not node_architecture_map:
        # 配置文件加载失败时的兜底（空映射，不阻断流程）
        node_architecture_map = {}
    matched_modules = []
    all_backend_files = set()
    all_frontend_files = set()
    all_data_files = set()
    
    for node_id in target_node_ids:
        if node_id in node_architecture_map:
            info = node_architecture_map[node_id]
            matched_modules.append(info["module"])
            all_backend_files.update(info["backend_files"])
            all_frontend_files.update(info["frontend_files"])
            all_data_files.update(info["data_files"])
    
    return {
        "matched_nodes": len(target_node_ids),
        "matched_modules": matched_modules,
        "related_backend_files": sorted(list(all_backend_files)),
        "related_frontend_files": sorted(list(all_frontend_files)),
        "related_data_files": sorted(list(all_data_files)),
        "architecture_doc": "backend/docs/SYSTEM_ARCHITECTURE.md",
        "tech_stack": gateway_config.get("tech_stack", {
            "frontend": "Vue 3 + Vite + TypeScript + Pinia + Naive UI + ECharts",
            "backend": "Python + FastAPI + Uvicorn",
            "database": "SQLite",
            "build": "npm run build → dist/ → backend/static/"
        })
    }


def _generate_gateway_task_list(graph, target_node_ids, change_type):
    """生成五类必做项任务清单"""
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
    
    # v2.9.23: 从gateway_config.json加载固定任务模板
    gateway_config = _load_gateway_config()
    task_templates = gateway_config.get("task_templates", {})
    
    # 1b/1c/2/3/3b: 固定任务模板从配置文件加载
    for tpl_name, tpl in task_templates.items():
        # 检查是否适用：always为true，或者change_type在change_types中
        is_applicable = tpl.get("always", False) or (change_type in tpl.get("change_types", []))
        if not is_applicable:
            continue
        desc = tpl["description"]
        # 文档任务动态替换章节数
        if tpl_name == "doc_update" and all_doc_sections:
            desc = f"更新功能链文档（{len(all_doc_sections)}个章节）"
        task_list.append({
            "id": task_id,
            "category": tpl["category"],
            "description": desc,
            "priority": tpl.get("priority", "medium"),
            "completed": False,
            "template": tpl_name  # 标记来自哪个模板
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
    
    # v2.9.23: 5/6/7任务（神经网络同步/版本号升级/构建打包）已通过配置文件模板加载
    
    return task_list, all_affected


@router.post("/api/function-graph/change/gateway", summary="变更准入网关（强制前置检查）",
             responses={200: {"description": "返回重复检查、影响分析、五类必做项任务清单"}})


async def change_gateway(body: ChangeGatewayRequest):
    """v2.9.20: 变更准入网关。所有功能更新/新增必须先通过此入口，生成完整任务清单后才能实施。
    
    检查项：
    1. 重复检查：搜索已有变更历史和节点开放病点
    2. 影响分析：遍历所有涉及节点的下游影响
    3. 任务清单：代码/数据/文档/测试/神经网络/版本号/构建 七类必做项
    """
    try:
        import time
        graph = _load_json(FUNCTION_GRAPH_PATH)
        if not graph:
            return {"ok": False, "msg": "功能图谱不存在"}
        
        history = _load_json(CHANGE_HISTORY_PATH) or {"meta": {"version": "1.0", "total_changes": 0}, "changes": []}
        
        nodes = {n["id"]: n for n in graph.get("nodes", [])}
        
        # 验证节点存在
        invalid_nodes = [nid for nid in body.target_node_ids if nid not in nodes]
        if invalid_nodes:
            return {"ok": False, "msg": f"节点不存在: {invalid_nodes}"}
        
        # 1. 重复检查
        duplicates = _check_duplicate(graph, history, body.title, body.target_node_ids)
        
        # 2. 影响分析 + 任务清单
        task_list, all_affected = _generate_gateway_task_list(graph, body.target_node_ids, body.change_type)
        
        # 3. 汇总影响节点详情
        affected_details = []
        for nid in all_affected:
            if nid in nodes:
                n = nodes[nid]
                affected_details.append({
                    "id": nid,
                    "name": n.get("name", nid),
                    "layer": n.get("layer", "unknown"),
                    "status": n.get("status", "unknown"),
                    "version": n.get("version", ""),
                    "open_issues": len([i for i in n.get("known_issues", []) if i.get("status") != "resolved"])
                })
        
        # 4. 创建变更记录（状态=pending，等待确认后才进入in_progress）
        change_id = f"CHG-{time.strftime('%Y%m%d')}-{len(history.get('changes', [])) + 1:03d}"
        change = {
            "id": change_id,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
            "title": body.title,
            "change_type": body.change_type,
            "priority": body.priority,
            "description": body.description,
            "target_node_ids": body.target_node_ids,
            "status": "pending",
            "duplicate_check": duplicates,
            "affected_nodes": list(all_affected),
            "task_list": task_list,
            "completed_at": None
        }
        
        history["changes"].insert(0, change)
        history["meta"]["total_changes"] = len(history["changes"])
        history["meta"]["last_updated"] = time.strftime("%Y-%m-%d")
        _save_json(CHANGE_HISTORY_PATH, history)
        
        audit_log(AuditAction.ADMIN_ACTION, f"变更准入: {body.title}", 
                  extra={"change_id": change_id, "target_nodes": body.target_node_ids, "duplicates": len(duplicates)})
        
        return {
            "ok": True,
            "change_id": change_id,
            "duplicate_check": {
                "has_duplicate": len(duplicates) > 0,
                "duplicates": duplicates
            },
            "impact_analysis": {
                "target_count": len(body.target_node_ids),
                "affected_count": len(all_affected),
                "affected_nodes": affected_details
            },
            "architecture_match": _get_architecture_match(body.target_node_ids),
            "task_list": task_list,
            "task_summary": {
                "code": len([t for t in task_list if t["category"] == "code"]),
                "data": len([t for t in task_list if t["category"] == "data"]),
                "doc": len([t for t in task_list if t["category"] == "doc"]),
                "test": len([t for t in task_list if t["category"] == "test"]),
                "graph": len([t for t in task_list if t["category"] == "graph"]),
                "version": len([t for t in task_list if t["category"] == "version"]),
                "build": len([t for t in task_list if t["category"] == "build"]),
                "total": len(task_list)
            },
            "warning": "⚠️ 必须完成所有任务项才能标记变更完成，尤其是神经网络同步和版本号升级" if len(duplicates) == 0 else "⚠️ 发现可能重复的变更，请确认后再继续"
        }
    except Exception as e:
        return {"ok": False, "msg": str(e)[:200]}


@router.post("/api/function-graph/change/gateway/{change_id}/start", summary="确认并开始变更",
             responses={200: {"description": "变更状态从pending改为in_progress"}})
async def change_gateway_start(change_id: str):
    """确认变更准入检查通过，开始实施变更。"""
    try:
        history = _load_json(CHANGE_HISTORY_PATH)
        if not history:
            return {"ok": False, "msg": "变更历史不存在"}
        
        for c in history.get("changes", []):
            if c["id"] == change_id:
                if c["status"] != "pending":
                    return {"ok": False, "msg": f"变更状态为{c['status']}，无法开始"}
                c["status"] = "in_progress"
                c["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%S+08:00")
                _save_json(CHANGE_HISTORY_PATH, history)
                return {"ok": True, "change": c, "msg": "变更已开始，请按任务清单逐项执行"}
        
        return {"ok": False, "msg": f"变更记录 {change_id} 不存在"}
    except Exception as e:
        return {"ok": False, "msg": str(e)[:200]}


@router.post("/api/function-graph/change/gateway/{change_id}/complete",
             summary="完成变更（强制验证门）",
             responses={200: {"description": "验证通过，变更完成，神经网络已自动同步"}})
async def change_gateway_complete(change_id: str):
    """v2.9.21: 完成变更验证门。
    
    强制校验：
    1. 所有任务项必须100%完成
    2. 七类必做项（代码/数据/文档/测试/神经网络/版本号/构建）缺一不可
    3. 通过后自动同步神经网络：更新涉及节点version和last_verified
    
    未通过校验时返回缺失的任务项，不允许标记完成。
    """
    try:
        import time
        history = _load_json(CHANGE_HISTORY_PATH)
        if not history:
            return {"ok": False, "msg": "变更历史不存在"}
        
        change = None
        for c in history.get("changes", []):
            if c["id"] == change_id:
                change = c
                break
        
        if not change:
            return {"ok": False, "msg": f"变更记录 {change_id} 不存在"}
        
        if change["status"] == "completed":
            return {"ok": False, "msg": "变更已完成，无需重复操作"}
        
        if change["status"] != "in_progress":
            return {"ok": False, "msg": f"变更状态为{change['status']}，请先开始变更"}
        
        # 1. 检查所有任务项完成情况
        task_list = change.get("task_list", [])
        total_tasks = len(task_list)
        completed_tasks = len([t for t in task_list if t.get("completed")])
        incomplete_tasks = [t for t in task_list if not t.get("completed")]
        
        # 2. 检查七类必做项：每类必须至少有1个任务，且所有任务必须完成
        required_categories = ["code", "data", "doc", "test", "graph", "version", "build"]
        category_status = {}
        missing_categories = []
        empty_categories = []
        for cat in required_categories:
            cat_tasks = [t for t in task_list if t.get("category") == cat]
            cat_completed = [t for t in cat_tasks if t.get("completed")]
            category_status[cat] = {
                "total": len(cat_tasks),
                "completed": len(cat_completed)
            }
            # v2.9.22修复：每类必须至少有1个任务（防止任务清单生成时遗漏）
            if len(cat_tasks) == 0:
                empty_categories.append(cat)
            # 有任务但未完成的，标记为缺失
            elif len(cat_completed) == 0:
                missing_categories.append(cat)
        
        # 3. 验证门判断
        if empty_categories:
            return {
                "ok": False,
                "msg": f"验证未通过：任务清单缺少必做类别: {empty_categories}，请重新通过网关生成任务清单",
                "validation": {
                    "total": total_tasks,
                    "completed": completed_tasks,
                    "empty_categories": empty_categories,
                    "missing_categories": missing_categories,
                    "category_status": category_status
                }
            }
        
        if incomplete_tasks:
            return {
                "ok": False,
                "msg": f"验证未通过：还有{len(incomplete_tasks)}项任务未完成",
                "validation": {
                    "total": total_tasks,
                    "completed": completed_tasks,
                    "incomplete": incomplete_tasks,
                    "missing_categories": missing_categories,
                    "category_status": category_status
                }
            }
        
        if missing_categories:
            return {
                "ok": False,
                "msg": f"验证未通过：以下必做类别缺少已完成任务: {missing_categories}",
                "validation": {
                    "total": total_tasks,
                    "completed": completed_tasks,
                    "missing_categories": missing_categories,
                    "category_status": category_status
                }
            }
        
        # 3b. v2.9.22 P1: 实际验证（不只是检查completed字段）
        actual_validation = _validate_actual_artifacts()
        if not actual_validation["passed"]:
            return {
                "ok": False,
                "msg": f"验证未通过：实际验证失败 - {actual_validation['reason']}",
                "validation": {
                    "total": total_tasks,
                    "completed": completed_tasks,
                    "category_status": category_status,
                    "actual_validation": actual_validation
                }
            }
        
        # 4. 验证通过，自动同步神经网络
        graph = _load_json(FUNCTION_GRAPH_PATH)
        if graph:
            current_version = graph.get("meta", {}).get("version", "v2.9.21")
            today = time.strftime("%Y-%m-%d")
            updated_nodes = []
            
            # v2.9.22 P1: 只同步target_node_ids，affected_nodes标记为"待验证"
            target_ids = set(change.get("target_node_ids", []))
            affected_ids = set(change.get("affected_nodes", [])) - target_ids
            pending_nodes = []
            
            for node in graph.get("nodes", []):
                if node["id"] in target_ids:
                    node["version"] = current_version
                    node["last_verified"] = today
                    node["verify_result"] = "pass"
                    updated_nodes.append(node["id"])
                elif node["id"] in affected_ids:
                    # 受影响节点不自动标记已验证，而是标记为待验证
                    node["verify_result"] = "pending"
                    node["last_verified"] = today
                    pending_nodes.append(node["id"])
            
            # 更新图谱meta
            graph["meta"]["last_updated"] = today
            _save_json(FUNCTION_GRAPH_PATH, graph)
        
        # 5. 标记变更完成
        change["status"] = "completed"
        change["completed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S+08:00")
        change["validation_passed"] = True
        change["synced_nodes"] = updated_nodes if graph else []
        
        _save_json(CHANGE_HISTORY_PATH, history)
        
        audit_log(AuditAction.ADMIN_ACTION, f"变更完成: {change['title']}", 
                  extra={"change_id": change_id, "tasks_completed": completed_tasks, "synced_nodes": len(updated_nodes)})
        
        return {
            "ok": True,
            "msg": "✅ 验证通过，变更已完成，神经网络已自动同步",
            "change": change,
            "validation": {
                "total": total_tasks,
                "completed": completed_tasks,
                "category_status": category_status
            },
            "synced_nodes": updated_nodes if graph else [],
            "pending_nodes": pending_nodes if graph else []
        }
    except Exception as e:
        return {"ok": False, "msg": str(e)[:200]}


@router.get("/api/function-graph/change/active", summary="获取进行中的变更列表",
            responses={200: {"description": "返回所有pending和in_progress的变更"}})
async def change_active_list():
    """v2.9.21: 获取进行中的变更列表，防止遗漏。"""
    try:
        history = _load_json(CHANGE_HISTORY_PATH) or {"meta": {"version": "1.0", "total_changes": 0}, "changes": []}
        active = [c for c in history.get("changes", []) if c.get("status") in ["pending", "in_progress"]]
        return {
            "ok": True,
            "active_count": len(active),
            "changes": active
        }
    except Exception as e:
        return {"ok": False, "msg": str(e)[:200]}


# ---------------------------------------------------------------------------
# v2.9.17: 功能神经网络 P3 — 文档一致性检测 + 测试路径自动生成
# ---------------------------------------------------------------------------
