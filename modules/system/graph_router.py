"""
api/graph_router.py —— 功能神经网络图谱路由模块（v2.9.23从router.py拆分）

包含：
- 获取功能图谱
- 文档一致性检测
- 生成测试路径
- 扫描代码变更
- 生成发布检查清单
- 生成版本变更报告

所有路由通过 APIRouter 注册，main.py 中 include_router 引入。
"""
from __future__ import annotations

import time
from pathlib import Path

from fastapi import APIRouter, Body, Depends, Query
from pydantic import BaseModel

# 从router.py导入公共工具
from modules.system.router import (
    ROOT,
    FUNCTION_GRAPH_PATH,
    CHANGE_HISTORY_PATH,
    _load_json,
    _save_json,
)

router = APIRouter(tags=["功能神经网络"])

@router.get("/api/function-graph", summary="获取功能神经网络图谱", responses={200: {"description": "返回节点、边、层级定义"}})
async def function_graph():
    """v2.9.15: 功能神经网络图谱，用于变更影响分析和测试路径遍历。"""
    try:
        import json
        graph_path = ROOT / "docs" / "function_graph.json"
        if graph_path.exists():
            data = json.loads(graph_path.read_text(encoding="utf-8"))
            return {"ok": True, "graph": data}
        return {"ok": False, "msg": "function_graph.json not found"}
    except Exception as e:
        return {"ok": False, "msg": str(e)[:100]}


# ---------------------------------------------------------------------------
# v2.9.16: 功能神经网络 P2 — 变更影响分析
# ---------------------------------------------------------------------------


@router.get("/api/function-graph/consistency/check", summary="文档一致性检测", responses={200: {"description": "返回各节点的一致性检测结果"}})
async def consistency_check():
    """检测所有节点的代码文件、文档章节、配置参数是否一致存在。"""
    try:
        graph = _load_json(FUNCTION_GRAPH_PATH)
        if not graph:
            return {"ok": False, "msg": "功能图谱不存在"}
        
        # v2.9.23: 用项目根目录检查code_files（code_files写的是backend/xxx、src/xxx）
        PROJECT_ROOT = ROOT.parent
        
        chain_doc_path = ROOT / "docs" / "CHAIN_TEST_MANUAL.md"
        chain_doc_content = ""
        if chain_doc_path.exists():
            chain_doc_content = chain_doc_path.read_text(encoding="utf-8")
        
        config_path = ROOT / "config.py"
        config_content = ""
        if config_path.exists():
            config_content = config_path.read_text(encoding="utf-8")
        
        results = []
        total_issues = 0
        
        for node in graph.get("nodes", []):
            node_result = {
                "node_id": node["id"],
                "node_name": node.get("name", node["id"]),
                "layer": node.get("layer", "unknown"),
                "status": "ok",
                "issues": [],
                "checks": {}
            }
            
            # 1. 检查代码文件是否存在（v2.9.23: 从项目根目录找，支持backend/xxx和src/xxx）
            code_files = node.get("code_files", [])
            missing_files = []
            for cf in code_files:
                file_path = None
                # 优先从项目根目录找（backend/xxx, src/xxx, _pack.py等）
                candidate = PROJECT_ROOT / cf
                if candidate.exists():
                    file_path = candidate
                elif "/" in cf or "\\" in cf:
                    # 相对路径，从ROOT找（兼容旧格式modules/xxx）
                    file_path = ROOT / cf
                else:
                    # 纯文件名，在ROOT下搜索
                    for p in ROOT.rglob(cf):
                        if p.is_file():
                            file_path = p
                            break
                
                if file_path and file_path.exists():
                    node_result["checks"][cf] = "exists"
                else:
                    missing_files.append(cf)
                    node_result["checks"][cf] = "missing"
            
            if missing_files:
                node_result["issues"].append({
                    "type": "missing_code_file",
                    "severity": "high",
                    "message": f"代码文件不存在: {', '.join(missing_files)}"
                })
            
            # 2. 检查文档章节是否存在
            doc_sections = node.get("doc_sections", [])
            missing_docs = []
            for ds in doc_sections:
                # 提取关键词搜索
                keywords = ds.replace("§", "").replace("：", ":").split()
                found = any(kw in chain_doc_content for kw in keywords if len(kw) > 2)
                if found:
                    node_result["checks"][ds] = "exists"
                else:
                    missing_docs.append(ds)
                    node_result["checks"][ds] = "missing"
            
            if missing_docs:
                node_result["issues"].append({
                    "type": "missing_doc_section",
                    "severity": "medium",
                    "message": f"文档章节未找到: {', '.join(missing_docs)}"
                })
            
            # 3. 检查配置参数是否定义
            config_keys = node.get("config_keys", [])
            missing_configs = []
            for ck in config_keys:
                if ck in config_content:
                    node_result["checks"][ck] = "exists"
                else:
                    missing_configs.append(ck)
                    node_result["checks"][ck] = "missing"
            
            if missing_configs:
                node_result["issues"].append({
                    "type": "missing_config",
                    "severity": "medium",
                    "message": f"配置参数未定义: {', '.join(missing_configs)}"
                })
            
            # 4. 检查检查要点是否有对应的测试路径
            check_points = node.get("check_points", [])
            if not check_points:
                node_result["issues"].append({
                    "type": "no_check_points",
                    "severity": "low",
                    "message": "节点未定义检查要点"
                })
            
            # 汇总状态
            if node_result["issues"]:
                severities = [i["severity"] for i in node_result["issues"]]
                if "high" in severities:
                    node_result["status"] = "error"
                elif "medium" in severities:
                    node_result["status"] = "warning"
                else:
                    node_result["status"] = "info"
                total_issues += len(node_result["issues"])
            
            results.append(node_result)
        
        # 统计
        stats = {
            "total_nodes": len(results),
            "ok_nodes": sum(1 for r in results if r["status"] == "ok"),
            "error_nodes": sum(1 for r in results if r["status"] == "error"),
            "warning_nodes": sum(1 for r in results if r["status"] == "warning"),
            "info_nodes": sum(1 for r in results if r["status"] == "info"),
            "total_issues": total_issues
        }
        
        return {"ok": True, "results": results, "stats": stats}
    except Exception as e:
        return {"ok": False, "msg": str(e)[:200]}


class TestPathRequest(BaseModel):
    node_id: str
    max_depth: int = Query(default=5)
    max_paths: int = Query(default=20)


@router.post("/api/function-graph/test-paths", summary="生成测试路径", responses={200: {"description": "返回从指定节点出发的所有测试路径"}})
async def generate_test_paths(body: TestPathRequest):
    """从指定节点出发，BFS遍历所有关联测试路径，生成测试用例清单。"""
    try:
        graph = _load_json(FUNCTION_GRAPH_PATH)
        if not graph:
            return {"ok": False, "msg": "功能图谱不存在"}
        
        nodes = {n["id"]: n for n in graph.get("nodes", [])}
        edges = graph.get("edges", [])
        # v2.11.5: 边端点双命名兼容（历史坏边曾用 from/to 命名），
        # 避免 KeyError 导致网关/图谱接口整体失败
        edges = [
            {**e, "source": e.get("source", e.get("from", "")), "target": e.get("target", e.get("to", ""))}
            for e in edges
        ]
        
        if body.node_id not in nodes:
            return {"ok": False, "msg": f"节点 {body.node_id} 不存在"}
        
        # 构建邻接表
        adjacency = {}
        for e in edges:
            if e["source"] not in adjacency:
                adjacency[e["source"]] = []
            adjacency[e["source"]].append({
                "target": e["target"],
                "type": e.get("type", "unknown"),
                "label": e.get("label", "")
            })
        
        # BFS生成所有路径
        all_paths = []
        queue = [(body.node_id, [body.node_id], [])]
        
        while queue and len(all_paths) < body.max_paths:
            current, path, edge_types = queue.pop(0)
            
            if len(path) > 1:
                all_paths.append({
                    "nodes": path,
                    "edge_types": edge_types,
                    "length": len(path) - 1
                })
            
            if len(path) <= body.max_depth and current in adjacency:
                for next_edge in adjacency[current]:
                    if next_edge["target"] not in path:  # 避免循环
                        queue.append((
                            next_edge["target"],
                            path + [next_edge["target"]],
                            edge_types + [next_edge["type"]]
                        ))
        
        # 为每条路径生成测试用例
        test_cases = []
        for idx, p in enumerate(all_paths):
            path_nodes = [nodes.get(nid, {"name": nid}) for nid in p["nodes"]]
            check_points = []
            for n in path_nodes:
                check_points.extend(n.get("check_points", [])[:2])  # 每个节点最多取2个检查点
            
            test_cases.append({
                "id": f"TC-{idx+1:03d}",
                "path": [n.get("name", nid) for nid, n in zip(p["nodes"], path_nodes)],
                "path_ids": p["nodes"],
                "edge_types": p["edge_types"],
                "length": p["length"],
                "check_points": check_points[:5],  # 最多5个检查点
                "priority": "high" if p["length"] <= 2 else "medium"
            })
        
        # 按优先级和长度排序
        test_cases.sort(key=lambda x: (0 if x["priority"] == "high" else 1, x["length"]))
        
        # 统计
        stats = {
            "start_node": nodes[body.node_id].get("name", body.node_id),
            "total_paths": len(all_paths),
            "high_priority": sum(1 for tc in test_cases if tc["priority"] == "high"),
            "medium_priority": sum(1 for tc in test_cases if tc["priority"] == "medium"),
            "max_depth": body.max_depth
        }
        
        return {"ok": True, "test_cases": test_cases, "stats": stats}
    except Exception as e:
        return {"ok": False, "msg": str(e)[:200]}


# ---------------------------------------------------------------------------
# v2.9.18: 功能神经网络 P4 — CI/CD集成 + 图谱自动更新
# ---------------------------------------------------------------------------

class ScanChangesRequest(BaseModel):
    file_patterns: list[str] | None = None  # 要扫描的文件模式，None表示全部


@router.post("/api/function-graph/scan-changes", summary="扫描代码变更", responses={200: {"description": "返回变更的节点列表和更新后的状态"}})
async def scan_changes(body: ScanChangesRequest | None = None):
    """扫描代码文件变更，自动更新节点的验证状态。
    检查节点绑定的code_files的修改时间，与last_verified对比，标记需要重新验证的节点。
    """
    try:
        graph = _load_json(FUNCTION_GRAPH_PATH)
        if not graph:
            return {"ok": False, "msg": "功能图谱不存在"}
        
        import time
        now = time.time()
        changed_nodes = []
        verified_nodes = []
        
        for node in graph.get("nodes", []):
            code_files = node.get("code_files", [])
            if not code_files:
                continue
            
            latest_mtime = 0
            existing_files = []
            
            for cf in code_files:
                # 查找文件
                file_path = None
                if "/" in cf or "\\" in cf:
                    candidate = ROOT / cf
                    if candidate.exists():
                        file_path = candidate
                else:
                    for p in ROOT.rglob(cf):
                        if p.is_file():
                            file_path = p
                            break
                
                if file_path and file_path.exists():
                    mtime = file_path.stat().st_mtime
                    latest_mtime = max(latest_mtime, mtime)
                    existing_files.append(str(file_path))
            
            if existing_files:
                # 解析last_verified时间
                last_verified = node.get("last_verified", "")
                last_verified_ts = 0
                if last_verified:
                    try:
                        from datetime import datetime
                        dt = datetime.fromisoformat(last_verified.replace("Z", "+00:00"))
                        last_verified_ts = dt.timestamp()
                    except:
                        pass
                
                # 如果文件修改时间晚于最后验证时间，标记为需要重新验证
                if latest_mtime > last_verified_ts:
                    node["verify_result"] = "needs_recheck"
                    node["last_scanned"] = time.strftime("%Y-%m-%dT%H:%M:%S+08:00")
                    changed_nodes.append({
                        "id": node["id"],
                        "name": node.get("name", node["id"]),
                        "reason": "代码文件已变更，需重新验证",
                        "changed_files": existing_files
                    })
                else:
                    if node.get("verify_result") != "verified":
                        node["verify_result"] = "verified"
                    verified_nodes.append(node["id"])
        
        # 保存更新后的图谱
        _save_json(FUNCTION_GRAPH_PATH, graph)
        
        audit_log(AuditAction.ADMIN_ACTION, "扫描代码变更", extra={
            "changed_count": len(changed_nodes),
            "verified_count": len(verified_nodes)
        })
        
        return {
            "ok": True,
            "changed_nodes": changed_nodes,
            "verified_count": len(verified_nodes),
            "changed_count": len(changed_nodes),
            "scan_time": time.strftime("%Y-%m-%dT%H:%M:%S+08:00")
        }
    except Exception as e:
        return {"ok": False, "msg": str(e)[:200]}


class ReleaseCheckRequest(BaseModel):
    version: str = ""
    include_all: bool = False  # 是否包含所有节点，False只包含需验证的


@router.post("/api/function-graph/release-check", summary="生成发布检查清单", responses={200: {"description": "返回发布前必须验证的检查清单"}})
async def release_check(body: ReleaseCheckRequest):
    """生成发布前检查清单，基于节点状态和历史病点。
    优先检查：verify_result为needs_recheck的节点、有未修复病点的节点、核心业务层节点。
    """
    try:
        graph = _load_json(FUNCTION_GRAPH_PATH)
        if not graph:
            return {"ok": False, "msg": "功能图谱不存在"}
        
        import time
        
        # 分类节点
        needs_recheck = []
        has_issues = []
        core_business = []
        all_checks = []
        
        for node in graph.get("nodes", []):
            nid = node["id"]
            name = node.get("name", nid)
            layer = node.get("layer", "unknown")
            verify_result = node.get("verify_result", "unknown")
            known_issues = node.get("known_issues", [])
            check_points = node.get("check_points", [])
            open_issues = [i for i in known_issues if i.get("status") != "resolved"]
            
            node_info = {
                "id": nid,
                "name": name,
                "layer": layer,
                "verify_result": verify_result,
                "open_issues_count": len(open_issues),
                "check_points": check_points[:5],
                "priority": "medium"
            }
            
            # 需重新验证的节点（最高优先级）
            if verify_result == "needs_recheck":
                node_info["priority"] = "high"
                node_info["reason"] = "代码已变更，需重新验证"
                needs_recheck.append(node_info)
            
            # 有未修复病点的节点
            if open_issues:
                node_info["priority"] = "high"
                node_info["reason"] = f"有{len(open_issues)}个未修复病点"
                has_issues.append(node_info)
            
            # 核心业务层节点
            if layer == "business":
                node_info["priority"] = "medium"
                node_info["reason"] = "核心业务层"
                core_business.append(node_info)
            
            all_checks.append(node_info)
        
        # 合并去重，按优先级排序
        seen = set()
        final_checklist = []
        
        for item in needs_recheck + has_issues + core_business:
            if item["id"] not in seen:
                seen.add(item["id"])
                final_checklist.append(item)
        
        # 排序：高优先级在前
        final_checklist.sort(key=lambda x: 0 if x["priority"] == "high" else 1)
        
        # 生成统计
        stats = {
            "version": body.version or graph.get("meta", {}).get("version", "unknown"),
            "total_nodes": len(graph.get("nodes", [])),
            "needs_recheck": len(needs_recheck),
            "has_open_issues": len(has_issues),
            "core_business": len(core_business),
            "total_checks": len(final_checklist),
            "high_priority": sum(1 for c in final_checklist if c["priority"] == "high"),
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S+08:00")
        }
        
        return {
            "ok": True,
            "checklist": final_checklist if not body.include_all else all_checks,
            "stats": stats
        }
    except Exception as e:
        return {"ok": False, "msg": str(e)[:200]}


@router.get("/api/function-graph/version-report", summary="生成版本变更报告", responses={200: {"description": "返回当前版本的功能变更报告"}})
async def version_report():
    """生成版本变更报告，汇总当前版本的功能更新、节点变化和测试要点。"""
    try:
        graph = _load_json(FUNCTION_GRAPH_PATH)
        if not graph:
            return {"ok": False, "msg": "功能图谱不存在"}
        
        history = _load_json(CHANGE_HISTORY_PATH) or {"changes": []}
        
        import time
        
        # 统计节点状态
        nodes = graph.get("nodes", [])
        edges = graph.get("edges", [])
        # v2.11.5: 边端点双命名兼容（历史坏边曾用 from/to 命名），
        # 避免 KeyError 导致网关/图谱接口整体失败
        edges = [
            {**e, "source": e.get("source", e.get("from", "")), "target": e.get("target", e.get("to", ""))}
            for e in edges
        ]
        
        layer_stats = {}
        status_stats = {"verified": 0, "needs_recheck": 0, "unknown": 0}
        total_check_points = 0
        total_issues = 0
        resolved_issues = 0
        
        for node in nodes:
            layer = node.get("layer", "unknown")
            layer_stats[layer] = layer_stats.get(layer, 0) + 1
            
            vr = node.get("verify_result", "unknown")
            status_stats[vr] = status_stats.get(vr, 0) + 1
            
            total_check_points += len(node.get("check_points", []))
            issues = node.get("known_issues", [])
            total_issues += len(issues)
            resolved_issues += sum(1 for i in issues if i.get("status") == "resolved")
        
        # 最近的变更记录
        recent_changes = history.get("changes", [])[:5]
        
        report = {
            "version": graph.get("meta", {}).get("version", "unknown"),
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
            "graph_stats": {
                "total_nodes": len(nodes),
                "total_edges": len(edges),
                "layers": layer_stats,
                "verify_status": status_stats,
                "total_check_points": total_check_points,
                "total_issues": total_issues,
                "resolved_issues": resolved_issues,
                "issue_resolution_rate": round(resolved_issues / total_issues * 100, 1) if total_issues else 0
            },
            "recent_changes": recent_changes,
            "release_readiness": {
                "needs_recheck": status_stats.get("needs_recheck", 0),
                "open_issues": total_issues - resolved_issues,
                "ready": status_stats.get("needs_recheck", 0) == 0 and (total_issues - resolved_issues) == 0,
                "recommendation": "可以发布" if status_stats.get("needs_recheck", 0) == 0 else "建议先完成重新验证"
            }
        }
        
        return {"ok": True, "report": report}
    except Exception as e:
        return {"ok": False, "msg": str(e)[:200]}
