"""
Superpower工作流引擎模块包

本模块包提供Superpower变更工作流的核心能力，包括：
- 网关分析器（GatewayAnalyzer）：基于神经网络function_graph.json的变更影响传播分析
- 检查清单管理器（ChecklistManager）：生成、跟踪、验证变更检查清单
- 神经网络更新器（NeuralUpdater）：解析代码结构，自动更新function_graph.json
- 变更记录器（ChangeRecorder）：记录变更到change_history.json，升级版本号

工作流状态机：TRIGGER -> ANALYZE -> MODIFY -> VERIFY -> UPDATE -> RECORD -> COMPLETED

版本历史：
  1.0.0 (2026-09-07) - 初始版本，实现网关分析器和工作流引擎基础框架
"""

__version__ = "1.0.0"
__author__ = "Superpower Team"
__all__ = [
    "GatewayAnalyzer",
    "GatewayAnalysisResult",
    "RiskItem",
    "ChecklistItem",
]

# 延迟导入，避免在模块未完全就绪时导入失败
try:
    from .gateway_analyzer import (
        GatewayAnalyzer,
        GatewayAnalysisResult,
        RiskItem,
        ChecklistItem,
    )
except ImportError:
    # 允许在gateway_analyzer尚未实现时仍能导入包
    pass
