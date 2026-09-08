"""
collector/run.py —— 兼容层

v2.5.5 架构重构：已迁移到 modules.common.run
本文件保留为兼容层，直接执行时调用新模块
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from modules.common.run import main

if __name__ == "__main__":
    main()
