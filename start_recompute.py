import sys
sys.path.insert(0, '.')

from collector import fetch_manager

mgr = fetch_manager.get_manager()
mgr.start(scope="funds_pool")
print("全量重算任务已启动")
print("状态:", mgr.state.get("status"))
