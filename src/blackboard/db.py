"""SQLite 连接管理 — Aemeath 共享黑板。

关键工程实践（借鉴 muteki shared_graph.py）:
- WAL 模式: 支持多进程/多线程并发读写（容器间通过 Volume 共享时同样安全）
- busy_timeout: SQLITE_BUSY → 自动排队而非丢写
- synchronous=NORMAL: WAL 下安全且快
- 单连接 + 线程锁: 轻量操作，避免频繁建连开销
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Optional

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"
_DEFAULT_DB_PATH = "data/blackboard.db"


def resolve_db_path(explicit: Optional[str] = None) -> str:
    """解析黑板数据库路径。

    优先级: explicit 参数 > 环境变量 AEMEATH_BLACKBOARD_PATH >
    config.yaml blackboard.db_path（经 backend.config.Settings）> 默认 data/blackboard.db
    """
    if explicit:
        return explicit
    env = os.environ.get("AEMEATH_BLACKBOARD_PATH")
    if env:
        return env
    try:
        from backend.config import Settings

        p = getattr(Settings(), "blackboard_db_path", "")
        if p:
            return p
    except Exception:
        pass  # backend 不可用（独立运行 src.blackboard）时回退默认
    return _DEFAULT_DB_PATH


def connect(db_path: Optional[str] = None) -> sqlite3.Connection:
    """建立黑板连接：确保目录存在、设置 PRAGMA、幂等执行 DDL。"""
    path = resolve_db_path(db_path)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    _init_schema(conn)
    return conn


def _init_schema(conn: sqlite3.Connection) -> None:
    """幂等执行 schema.sql（全部为 IF NOT EXISTS）。"""
    ddl = _SCHEMA_PATH.read_text(encoding="utf-8")
    conn.executescript(ddl)
    conn.commit()
