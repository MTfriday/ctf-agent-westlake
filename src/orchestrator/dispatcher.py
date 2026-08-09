"""调度器 — Intent 认领入口 + 超时/死路处理（Act 阶段）。

- claim_for_worker: 供 Worker（Solver）原子认领 pending Intent（P5 集成）
- dispatch:         OODA 每轮扫描 claimed 超时 → failed + 记录死路
- complete:         Worker 完成后回写 done / failed
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from adapter.events import EventType, ev

logger = logging.getLogger(__name__)

_CLAIMED_FMT = "%Y-%m-%d %H:%M:%S.%f"


def _claimed_age_seconds(claimed_at: Optional[str]) -> Optional[float]:
    """解析黑板 claimed_at（UTC 毫秒字符串）为已过去秒数。"""
    if not claimed_at:
        return None
    try:
        ts = datetime.strptime(claimed_at, _CLAIMED_FMT).replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds()
    except ValueError:
        return None


class Dispatcher:
    """Intent 认领与生命周期管理。"""

    def __init__(self, store: Any, bus: Any, timeout: float = 120.0) -> None:
        self.store = store
        self.bus = bus
        self.timeout = timeout

    async def dispatch(self, problem_id: str) -> dict[str, Any]:
        """一轮调度：标记超时的 claimed Intent 为 failed 并记录死路。

        返回 {"timed_out": [intent_id, ...]}。
        """
        result: dict[str, Any] = {"timed_out": []}
        for it in self.store.active_intents(problem_id):
            if it["status"] != "claimed":
                continue
            age = _claimed_age_seconds(it.get("claimed_at"))
            if age is not None and age > self.timeout:
                intent_id = it["id"]
                self.store.fail_intent(intent_id)
                summary = f"Intent 超时({int(age)}s): {it['action_type']} → {it.get('target') or '—'}"
                self.store.mark_deadend(problem_id, summary, source="dispatcher")
                await self.bus.publish(
                    ev(EventType.INTENT_CONCLUDED, problem_id=problem_id, intent_id=intent_id,
                       status="failed", reason="timeout", age=int(age))
                )
                result["timed_out"].append(intent_id)
        if result["timed_out"]:
            logger.info("dispatcher: %d intent(s) timed out for %s", len(result["timed_out"]), problem_id)
        return result

    async def claim_for_worker(self, problem_id: str, worker_id: str) -> Optional[dict[str, Any]]:
        """原子认领一条 pending Intent（供 Worker 调用）。"""
        it = self.store.claim_intent(problem_id, worker_id)
        if it:
            await self.bus.publish(
                ev(EventType.INTENT_CLAIMED, problem_id=problem_id,
                   intent_id=it["id"], worker_id=worker_id,
                   action_type=it["action_type"], target=it.get("target"))
            )
        return it

    async def complete(self, problem_id: str, intent_id: int, status: str = "done") -> bool:
        """Worker 完成回写。status ∈ done | failed。"""
        ok = (
            self.store.complete_intent(intent_id)
            if status == "done"
            else self.store.fail_intent(intent_id)
        )
        await self.bus.publish(
            ev(EventType.INTENT_CONCLUDED, problem_id=problem_id,
               intent_id=intent_id, status=status)
        )
        return ok
