"""OODA 主循环 — 每道题一个调度器实例。

每轮:
- Observe  读黑板统计 + 活跃 Intent + 题目解出状态
- Reason   调用 ModeRouter 决策模式（orchestrated/hybrid 才规划）
- Decide   Planner 生成 ≤max_intents 个 Intent 写黑板（pending）
- Act      Dispatcher 处理超时/认领

容灾:
- 启动时扫描黑板，恢复超时 claimed Intent（标记 failed + 死路）
- 前端断开不影响后台循环（独立 asyncio task）
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Optional

from adapter.events import EventType, ev
from src.orchestrator.mode_router import MODES, MODE_LABEL, ModeRouter

logger = logging.getLogger(__name__)

ChallengeProvider = Callable[[str], Awaitable[Optional[dict]]]


class OrchestratorScheduler:
    """单题 OODA 调度器。"""

    def __init__(
        self,
        problem_id: str,
        *,
        store: Any,
        bus: Any,
        mode_router: ModeRouter,
        planner: Any,
        dispatcher: Any,
        get_challenge: Optional[ChallengeProvider] = None,
        observe_interval: float = 10.0,
        intent_timeout: float = 120.0,
        max_rounds: int = 20,
    ) -> None:
        self.problem_id = problem_id
        self.store = store
        self.bus = bus
        self.mode_router = mode_router
        self.planner = planner
        self.dispatcher = dispatcher
        self._get_challenge = get_challenge
        self.observe_interval = observe_interval
        self.intent_timeout = intent_timeout
        self.max_rounds = max_rounds

        self.status = "idle"  # idle | running | paused | stopped | solved
        self.current_cycle = 0
        self.current_mode = "auto"
        self.mode_reason = ""
        self.locked_mode: Optional[str] = None
        self.started_at: Optional[float] = None
        self._task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------ 控制
    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self.status = "running"
        self.started_at = time.time()
        await self._recover_intents()
        self._task = asyncio.create_task(self._run_loop(), name=f"ooda-{self.problem_id}")

    async def stop(self) -> None:
        self.status = "stopped"
        if self._task and not self._task.done():
            self._task.cancel()

    async def pause(self) -> None:
        if self.status == "running":
            self.status = "paused"

    def resume(self) -> None:
        if self.status == "paused":
            self.status = "running"

    async def lock_mode(self, mode: Optional[str]) -> None:
        self.locked_mode = mode if mode in MODES else None
        await self.bus.publish(
            ev(EventType.MODE_CHANGED, problem_id=self.problem_id,
               mode=self.locked_mode or "auto", reason="mode locked", source="operator")
        )

    async def add_manual_intent(
        self, action_type: str, target: Optional[str] = None, reasoning: Optional[str] = None
    ) -> dict[str, Any]:
        intent_id = self.store.create_intent(
            self.problem_id, action_type, target=target or None, reasoning=reasoning
        )
        await self.bus.publish(
            ev(EventType.INTENT_PROPOSED, problem_id=self.problem_id, intent_id=intent_id,
               action_type=action_type, target=target or "", reasoning=reasoning or "",
               source="manual")
        )
        return {"id": intent_id, "action_type": action_type, "target": target or "", "reasoning": reasoning or ""}

    def status_dict(self) -> dict[str, Any]:
        return {
            "problem_id": self.problem_id,
            "status": self.status,
            "current_cycle": self.current_cycle,
            "current_mode": self.current_mode,
            "mode_label": MODE_LABEL.get(self.current_mode, self.current_mode),
            "mode_reason": self.mode_reason,
            "locked_mode": self.locked_mode,
            "active_intents": len(self.store.active_intents(self.problem_id)),
            "uptime": round((time.time() - self.started_at), 1) if self.started_at else 0.0,
        }

    # ------------------------------------------------------------------ 容灾
    async def _recover_intents(self) -> None:
        """启动时恢复：超时 claimed Intent → failed + 死路；pending 保留可认领。"""
        for it in self.store.active_intents(self.problem_id):
            if it["status"] != "claimed":
                continue
            age = _age_of(it.get("claimed_at"))
            if age is not None and age > self.intent_timeout:
                self.store.fail_intent(it["id"])
                self.store.mark_deadend(
                    self.problem_id,
                    f"重启恢复: Intent 超时({int(age)}s) {it['action_type']}",
                    source="recover",
                )
                await self.bus.publish(
                    ev(EventType.INTENT_CONCLUDED, problem_id=self.problem_id,
                       intent_id=it["id"], status="failed", reason="recover-timeout")
                )

    # ------------------------------------------------------------------ OODA
    async def _run_loop(self) -> None:
        try:
            while self.status == "running":
                if self.max_rounds and self.current_cycle >= self.max_rounds:
                    logger.info("ooda %s: max rounds reached, stopping", self.problem_id)
                    self.status = "stopped"
                    break
                await self._cycle()
                await asyncio.sleep(self.observe_interval)
        except asyncio.CancelledError:
            self.status = "stopped"
        except Exception as e:  # noqa: BLE001
            logger.exception("ooda loop crashed for %s", self.problem_id)
            self.status = "stopped"
            await self.bus.publish(
                ev(EventType.RUN_STATUS, problem_id=self.problem_id, status="failed", error=str(e))
            )

    async def _cycle(self) -> dict[str, Any]:
        self.current_cycle += 1
        # ── Observe ──
        stats = self.store.get_stats(self.problem_id)
        challenge = None
        if self._get_challenge:
            try:
                challenge = await self._get_challenge(self.problem_id)
            except KeyError as e:
                logger.warning(
                    "ooda %s: challenge not found on platform: %s", self.problem_id, e
                )
                self.status = "stopped"
                await self.bus.publish(
                    ev(EventType.RUN_STATUS, problem_id=self.problem_id,
                       status="failed", error=str(e))
                )
                return {"cycle": self.current_cycle, "status": "failed"}
        if challenge and challenge.get("solved"):
            self.status = "solved"
            await self.bus.publish(
                ev(EventType.RUN_STATUS, problem_id=self.problem_id, status="solved")
            )
            return {"cycle": self.current_cycle, "status": "solved"}

        # ── Reason ──
        decision = await self.mode_router.decide(
            self.problem_id, stats=stats, challenge=challenge, locked=self.locked_mode
        )
        mode_changed = decision["mode"] != self.current_mode
        self.current_mode = decision["mode"]
        self.mode_reason = decision["mode_reason"]
        if mode_changed:
            await self.bus.publish(
                ev(EventType.MODE_CHANGED, problem_id=self.problem_id,
                   mode=self.current_mode, reason=self.mode_reason, source=decision.get("source", "router"))
            )

        # ── Decide ──
        active = len(self.store.active_intents(self.problem_id))
        created = []
        if self.current_mode in ("orchestrated", "hybrid") and active < self.planner.max_intents:
            created = await self.planner.plan(
                self.problem_id, self.current_mode, challenge=challenge, stats=stats
            )

        # ── Act ──
        acted = await self.dispatcher.dispatch(self.problem_id)

        await self.bus.publish(
            ev(EventType.RUN_STATUS, problem_id=self.problem_id,
               cycle=self.current_cycle, mode=self.current_mode,
               mode_reason=self.mode_reason, created=len(created),
               timed_out=len(acted.get("timed_out", [])), active_intents=active)
        )
        return {
            "cycle": self.current_cycle,
            "mode": self.current_mode,
            "created": len(created),
            "timed_out": len(acted.get("timed_out", [])),
        }


def _age_of(claimed_at: Optional[str]) -> Optional[float]:
    """黑板 claimed_at（UTC 毫秒字符串）→ 已过去秒数。"""
    from datetime import datetime, timezone

    if not claimed_at:
        return None
    try:
        ts = datetime.strptime(claimed_at, "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds()
    except ValueError:
        return None
