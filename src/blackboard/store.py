"""BlackboardStore — Aemeath 共享黑板的存取层（记忆层核心）。

提供 facts / intents / workers 三张表的完整 CRUD，外加两个集成入口：
- get_context(): Solver 执行前读取历史，把死路/发现/计划注入 System Prompt
- get_stats():   供总控 mode_router 做模式决策的统计口径

并发模型：单连接 + RLock（muteki 的 "one long-lived connection per instance"）。
操作均为毫秒级轻量读写，可直接在 asyncio 事件循环中调用；高并发场景下
调用方可自行以 asyncio.to_thread 包装。Intent 认领用单条原子 UPDATE
（guarded by rowcount），跨进程同样无 TOCTOU 窗口。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from typing import Any, Optional

from src.blackboard.db import connect

logger = logging.getLogger(__name__)

# facts.type 合法值
FACT_TYPE = ("discovery", "deadend", "partial")
# intents.status 合法值
INTENT_STATUS = ("pending", "claimed", "done", "failed")

# get_context 单次注入上限（防止上下文膨胀）
_MAX_FACTS_IN_CONTEXT = 50
_MAX_INTENTS_IN_CONTEXT = 20
# Worker 心跳超时阈值（秒）
WORKER_STALE_SECONDS = 90


def _ts_at(epoch: float) -> str:
    """UTC 时间戳（毫秒精度，字符串等长可字典序比较，避免秒级截断竞态）。"""
    sec = int(epoch)
    ms = int((epoch - sec) * 1000)
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(sec)) + f".{ms:03d}"


def _ts() -> str:
    return _ts_at(time.time())


class BlackboardStore:
    """线程安全的 SQLite 黑板存取器。"""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ 连接
    def _ensure(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = connect(self.db_path)
        return self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # ------------------------------------------------------------------ Facts
    def add_fact(
        self,
        problem_id: str,
        content: str,
        type: str = "discovery",
        source: Optional[str] = None,
    ) -> int:
        """写入一条事实（discovery / deadend / partial），返回其 id。"""
        if type not in FACT_TYPE:
            raise ValueError(f"invalid fact type: {type!r} (expected one of {FACT_TYPE})")
        with self._lock:
            conn = self._ensure()
            cur = conn.execute(
                "INSERT INTO facts (problem_id, content, type, source) VALUES (?, ?, ?, ?)",
                (problem_id, content, type, source),
            )
            conn.commit()
            return int(cur.lastrowid)

    def add_discovery(self, problem_id: str, content: str, source: Optional[str] = None) -> int:
        return self.add_fact(problem_id, content, "discovery", source)

    def mark_deadend(self, problem_id: str, content: str, source: Optional[str] = None) -> int:
        """记录一条死路（已试过且失败的方法）。"""
        return self.add_fact(problem_id, content, "deadend", source)

    def add_partial(self, problem_id: str, content: str, source: Optional[str] = None) -> int:
        """记录部分结果 / Flag 候选。"""
        return self.add_fact(problem_id, content, "partial", source)

    def list_facts(
        self,
        problem_id: str,
        type: Optional[str] = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """列出某题事实，最新在前。type 为空则返回全部类型。"""
        if type is not None and type not in FACT_TYPE:
            raise ValueError(f"invalid fact type: {type!r} (expected one of {FACT_TYPE})")
        with self._lock:
            conn = self._ensure()
            if type is None:
                rows = conn.execute(
                    "SELECT * FROM facts WHERE problem_id=? ORDER BY id DESC LIMIT ?",
                    (problem_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM facts WHERE problem_id=? AND type=? ORDER BY id DESC LIMIT ?",
                    (problem_id, type, limit),
                ).fetchall()
            return [dict(r) for r in rows]

    # ----------------------------------------------------------------- Intents
    def create_intent(
        self,
        problem_id: str,
        action_type: str,
        target: Optional[str] = None,
        reasoning: Optional[str] = None,
    ) -> int:
        """下发一条行动计划（status=pending），返回其 id。"""
        with self._lock:
            conn = self._ensure()
            cur = conn.execute(
                "INSERT INTO intents (problem_id, action_type, target, reasoning) VALUES (?, ?, ?, ?)",
                (problem_id, action_type, target, reasoning),
            )
            conn.commit()
            return int(cur.lastrowid)

    def claim_intent(self, problem_id: str, worker_id: str) -> Optional[dict[str, Any]]:
        """原子认领一条 pending Intent（单条 UPDATE ... RETURNING，零 TOCTOU）。

        返回被认领的 Intent 字典；若无可认领项则返回 None。
        单条 UPDATE 是隐式事务，跨进程同样无竞态窗口。
        """
        with self._lock:
            conn = self._ensure()
            cur = conn.execute(
                """
                UPDATE intents
                   SET status = 'claimed', claimed_by = ?, claimed_at = ?
                 WHERE id = (
                           SELECT id FROM intents
                            WHERE problem_id = ? AND status = 'pending'
                            ORDER BY id ASC LIMIT 1
                       )
                   AND status = 'pending'
                RETURNING *
                """,
                (worker_id, _ts(), problem_id),
            )
            row = cur.fetchone()
            conn.commit()
            return dict(row) if row else None

    def complete_intent(self, intent_id: int) -> bool:
        """将一条已认领 Intent 标记为 done。返回是否成功。"""
        with self._lock:
            conn = self._ensure()
            cur = conn.execute(
                "UPDATE intents SET status='done', completed_at=? WHERE id=? AND status='claimed'",
                (_ts(), intent_id),
            )
            conn.commit()
            return cur.rowcount == 1

    def fail_intent(self, intent_id: int) -> bool:
        """将一条 pending/claimed Intent 标记为 failed。返回是否成功。"""
        with self._lock:
            conn = self._ensure()
            cur = conn.execute(
                "UPDATE intents SET status='failed', completed_at=? "
                "WHERE id=? AND status IN ('pending', 'claimed')",
                (_ts(), intent_id),
            )
            conn.commit()
            return cur.rowcount == 1

    def list_intents(
        self,
        problem_id: str,
        status: Optional[str] = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """列出某题意图，最新在前。status 为空则返回全部状态。"""
        if status is not None and status not in INTENT_STATUS:
            raise ValueError(f"invalid intent status: {status!r} (expected one of {INTENT_STATUS})")
        with self._lock:
            conn = self._ensure()
            if status is None:
                rows = conn.execute(
                    "SELECT * FROM intents WHERE problem_id=? ORDER BY id DESC LIMIT ?",
                    (problem_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM intents WHERE problem_id=? AND status=? ORDER BY id DESC LIMIT ?",
                    (problem_id, status, limit),
                ).fetchall()
            return [dict(r) for r in rows]

    def active_intents(self, problem_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """列出活跃意图（pending + claimed），供调度器/前端展示。"""
        with self._lock:
            rows = self._ensure().execute(
                "SELECT * FROM intents WHERE problem_id=? AND status IN ('pending', 'claimed') "
                "ORDER BY id ASC LIMIT ?",
                (problem_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    # ---------------------------------------------------------------- Workers
    def heartbeat(
        self,
        worker_id: str,
        problem_id: Optional[str] = None,
        status: str = "alive",
    ) -> None:
        """记录/刷新一个 Worker 的心跳（UPSERT）。"""
        with self._lock:
            conn = self._ensure()
            conn.execute(
                """
                INSERT INTO workers (id, problem_id, last_heartbeat, status)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    problem_id = excluded.problem_id,
                    last_heartbeat = excluded.last_heartbeat,
                    status = excluded.status
                """,
                (worker_id, problem_id, _ts(), status),
            )
            conn.commit()

    def list_workers(self) -> list[dict[str, Any]]:
        """列出全部 Worker，最近心跳在前。"""
        with self._lock:
            rows = self._ensure().execute(
                "SELECT * FROM workers ORDER BY last_heartbeat DESC"
            ).fetchall()
            return [dict(r) for r in rows]

    def prune_stale_workers(self, max_age_seconds: int = WORKER_STALE_SECONDS) -> int:
        """删除超过阈值未心跳的 Worker 记录，返回删除数。"""
        cutoff = _ts_at(time.time() - max_age_seconds)
        with self._lock:
            conn = self._ensure()
            cur = conn.execute("DELETE FROM workers WHERE last_heartbeat < ?", (cutoff,))
            conn.commit()
            return cur.rowcount

    # ------------------------------------------------------------ 上下文注入
    def get_context(self, problem_id: str) -> str:
        """组装供 Solver 注入的上下文文本（执行前调用）。

        包含：已确认死路（勿重复）、已有发现、部分结果、活跃计划。
        无历史时返回占位说明。
        """
        facts = self.list_facts(problem_id, limit=_MAX_FACTS_IN_CONTEXT)
        intents = self.list_intents(problem_id, status=None, limit=_MAX_INTENTS_IN_CONTEXT)
        active = [i for i in intents if i["status"] in ("pending", "claimed")]
        deadends = [f for f in facts if f["type"] == "deadend"]
        discoveries = [f for f in facts if f["type"] == "discovery"]
        partials = [f for f in facts if f["type"] == "partial"]

        lines: list[str] = [f"# 黑板上下文 — {problem_id}", ""]
        if deadends:
            lines.append("## 已确认死路（以下方法已试过，请勿重复）")
            for f in deadends:
                src = f"（来源: {f['source']}）" if f["source"] else ""
                lines.append(f"- {f['content']}{src}")
            lines.append("")
        if discoveries:
            lines.append("## 已有发现")
            for f in discoveries:
                lines.append(f"- {f['content']}")
            lines.append("")
        if partials:
            lines.append("## 部分结果 / 候选")
            for f in partials:
                lines.append(f"- {f['content']}")
            lines.append("")
        if active:
            lines.append("## 活跃计划（其他 Worker 正在执行）")
            for i in active:
                tag = "执行中" if i["status"] == "claimed" else "待认领"
                target = f" → {i['target']}" if i["target"] else ""
                reason = f"（{i['reasoning']}）" if i["reasoning"] else ""
                lines.append(f"- [{tag}] {i['action_type']}{target}{reason}")
            lines.append("")
        if not facts and not active:
            lines.append("（暂无历史记忆）")
        return "\n".join(lines)

    # ---------------------------------------------------------------- 统计
    def get_stats(self, problem_id: str) -> dict[str, Any]:
        """统计口径，供 mode_router 决策（尝试次数/死路数/活跃计划数）。"""
        facts = self.list_facts(problem_id, limit=10_000)
        intents = self.list_intents(problem_id, status=None, limit=10_000)
        by_type: dict[str, int] = {}
        for f in facts:
            by_type[f["type"]] = by_type.get(f["type"], 0) + 1
        by_status: dict[str, int] = {}
        for i in intents:
            by_status[i["status"]] = by_status.get(i["status"], 0) + 1
        return {
            "problem_id": problem_id,
            "attempts": by_type.get("deadend", 0) + by_type.get("partial", 0),
            "deadends": by_type.get("deadend", 0),
            "discoveries": by_type.get("discovery", 0),
            "partials": by_type.get("partial", 0),
            "active_intents": by_status.get("pending", 0) + by_status.get("claimed", 0),
            "done_intents": by_status.get("done", 0),
            "failed_intents": by_status.get("failed", 0),
        }

    # ---------------------------------------------------------------- 管理
    def clear_problem(self, problem_id: str) -> dict[str, int]:
        """清空某题所有黑板记忆（前端「重置此题黑板」）。返回各表删除行数。"""
        with self._lock:
            conn = self._ensure()
            del_facts = conn.execute(
                "DELETE FROM facts WHERE problem_id=?", (problem_id,)
            ).rowcount
            del_intents = conn.execute(
                "DELETE FROM intents WHERE problem_id=?", (problem_id,)
            ).rowcount
            del_workers = conn.execute(
                "DELETE FROM workers WHERE problem_id=?", (problem_id,)
            ).rowcount
            conn.commit()
            return {"facts": del_facts, "intents": del_intents, "workers": del_workers}
