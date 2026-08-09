"""Aemeath 共享黑板（记忆层）。

SQLite (WAL) 实现的跨 Worker / 跨进程共享知识库：
- facts     事实 / 死路 / 部分 Flag
- intents   总控下发的行动计划（pending → claimed → done / failed）
- workers   Worker 心跳

用法::

    from src.blackboard import BlackboardStore

    store = BlackboardStore()                        # 自动解析 db 路径
    store.add_fact("crypto-1", "密文为 AES-ECB", source="solver-a")
    store.mark_deadend("crypto-1", "已知明文攻击失败", source="solver-b")
    ctx = store.get_context("crypto-1")              # 注入 Solver 的上下文
"""

from src.blackboard.db import resolve_db_path
from src.blackboard.store import BlackboardStore

__all__ = ["BlackboardStore", "resolve_db_path"]
