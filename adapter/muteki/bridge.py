"""muteki 契约 — 事件桥（Aemeath Event → muteki MutekiEvent）。

muteki 前端（frontend/lib/events.ts）消费命名 SSE 事件流，事件名与
`EventType` 枚举一一对应。本桥把 Aemeath 执行层事件（adapter/events.py）
翻译为 muteki 前端可折叠的 MutekiEvent，并把 run_id 归一化为前端 run_id
（= challenge name / problem_id）。

翻译要点（docs/p6-muteki-adaptation.md 事件映射表）：
  engine.log              → text.delta            （主线程日志气泡）
  blackboard.delta(fact)  → blackboard.delta(fact_added) + sharedgraph.delta + insight.event
  intent.*                → blackboard.delta(intent_*) + reason.intent
  mode.changed            → coordinator.guidance
  flag.*                  → insight.event + solvegraph.delta(flag)
  run.finished            → run.finished          （终态折叠）
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional

from adapter.events import Event as AemeathEvent
from adapter.events import EventType as AEventType

# muteki 事件名（与 frontend/lib/events.ts EventType 枚举同步）
RUN_STARTED = "run.started"
RUN_TITLED = "run.titled"
RUN_FINISHED = "run.finished"
TEXT_DELTA = "text.delta"
REASONING_DELTA = "reasoning.delta"
TOOL_START = "tool.start"
TOOL_CALL_ARGS = "tool.args"
TOOL_RESULT = "tool.result"
TERMINAL_OUTPUT = "terminal.output"
SOLVE_GRAPH_DELTA = "solvegraph.delta"
INSIGHT_EVENT = "insight.event"
SHARED_GRAPH_DELTA = "sharedgraph.delta"
REASON_INTENT = "reason.intent"
BLACKBOARD_DELTA = "blackboard.delta"
COST_UPDATE = "cost.update"
GUIDANCE = "coordinator.guidance"
HITL_REQUEST = "hitl.request"
WORKER_STATUS = "worker.status"
WORKER_LIFECYCLE = "worker.lifecycle"


@dataclass
class MutekiEvent:
    """与前端 `interface MutekiEvent` 字段完全一致。"""

    event_type: str
    seq: int
    ts: float
    run_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    challenge_id: Optional[str] = None
    solver_id: Optional[str] = None

    def to_sse(self) -> str:
        data = {
            "event_type": self.event_type,
            "seq": self.seq,
            "ts": self.ts,
            "run_id": self.run_id,
            "challenge_id": self.challenge_id,
            "solver_id": self.solver_id,
            "payload": self.payload,
        }
        body = __import__("json").dumps(data, ensure_ascii=False)
        return f"id: {self.seq}\nevent: {self.event_type}\ndata: {body}\n\n"


class EventBridge:
    """Aemeath 事件 → muteki 事件翻译器（每运行实例一个，维护独立 seq/fact_seq）。"""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self._seq = 0
        self._fact_seq: int = 0

    # ── 构造 ─────────────────────────────────────────────────────────────
    def _mk(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        solver_id: Optional[str] = None,
    ) -> MutekiEvent:
        self._seq += 1
        return MutekiEvent(
            event_type=event_type,
            seq=self._seq,
            ts=time.time(),
            run_id=self.run_id,
            challenge_id=self.run_id,
            solver_id=solver_id,
            payload=payload,
        )

    def _next_fact_seq(self) -> int:
        self._fact_seq += 1
        return self._fact_seq

    # ── 翻译 ─────────────────────────────────────────────────────────────
    def translate(self, aev: AemeathEvent) -> list[MutekiEvent]:
        """把一个 Aemeath 事件翻译成 0..n 个 muteki 事件。"""
        t = aev.event_type
        p = aev.payload or {}
        rid = self.run_id
        out: list[MutekiEvent] = []

        if t is AEventType.RUN_STARTED:
            out.append(self._mk(RUN_STARTED, {
                "challenge": {
                    "name": p.get("challenge_name") or rid,
                    "category": p.get("category", ""),
                    "target": p.get("target", ""),
                    "description": p.get("description", ""),
                    "expected_flags": 1,
                    "multi_flag": False,
                },
            }))

        elif t is AEventType.ENGINE_LOG:
            # 逐步过程事件（swarm trace）：tool_call → tool.start，
            # tool_result → tool.result，model_response/其它 → text.delta
            kind = str(p.get("kind") or "")
            solver = str(p.get("solver") or p.get("solver_id") or "")
            if kind == "tool_call":
                out.append(self._mk(TOOL_START, {
                    "tool": p.get("tool", "?"),
                    "args": str(p.get("args", ""))[:12000],
                }, solver_id=solver or None))
                if p.get("args"):
                    out.append(self._mk(TOOL_CALL_ARGS, {
                        "tool": p.get("tool", "?"),
                        "args": str(p.get("args", ""))[:12000],
                    }, solver_id=solver or None))
            elif kind == "tool_result":
                out.append(self._mk(TOOL_RESULT, {
                    "tool": p.get("tool", "?"),
                    "result": str(p.get("result", ""))[:12000],
                }, solver_id=solver or None))
            else:
                text = str(p.get("message") or p.get("text") or "")
                if not text and p.get("type"):
                    text = f"[{p.get('type', '')}]"
                if text:
                    out.append(self._mk(TEXT_DELTA, {"text": text, "main_thread": True},
                                        solver_id=solver or None))

        elif t is AEventType.BLACKBOARD_DELTA:
            kind = p.get("kind")
            if kind == "discovery":
                out.extend(self._fact_delta(p))
            elif kind in ("intent", "intent_proposed"):
                out.extend(self._intent_proposed(p))

        elif t is AEventType.INTENT_PROPOSED:
            out.extend(self._intent_proposed(p))

        elif t is AEventType.INTENT_CLAIMED:
            intent_id = str(p.get("intent_id", ""))
            worker = str(p.get("worker_id") or "")
            out.append(self._mk(BLACKBOARD_DELTA, {
                "kind": "intent_claimed", "intent_id": intent_id,
                "worker": worker, "actor": worker or "engine",
            }, solver_id=worker or None))

        elif t is AEventType.INTENT_CONCLUDED:
            intent_id = str(p.get("intent_id", ""))
            status = p.get("status", "done")
            result = "done" if status == "done" else str(p.get("reason", "failed"))
            out.append(self._mk(BLACKBOARD_DELTA, {
                "kind": "intent_concluded", "intent_id": intent_id,
                "result": result, "actor": str(p.get("worker_id") or "engine"),
            }))

        elif t is AEventType.MODE_CHANGED:
            mode = str(p.get("mode", ""))
            text = f"协调模式切换为 {mode}"
            if p.get("reason"):
                text += f"（{p['reason']}）"
            out.append(self._mk(GUIDANCE, {"text": text, "mode": mode}))

        elif t is AEventType.FLAG_SUBMITTED:
            flag = p.get("flag", "")
            out.append(self._mk(INSIGHT_EVENT, {"kind": "FlagSubmitted",
                                                "flag": flag, "text": f"提交候选 flag: {flag}"}))

        elif t is AEventType.FLAG_VERIFIED:
            flag = p.get("flag", "")
            verified = bool(p.get("valid", p.get("verified", True)))
            out.append(self._mk(INSIGHT_EVENT, {
                "kind": "FlagVerified", "flag": flag,
                "text": f"flag 验证{'通过' if verified else '失败'}: {flag}",
                "verified": verified,
            }))

        elif t is AEventType.FLAG_SOLVED:
            flag = p.get("flag", "")
            out.append(self._mk(INSIGHT_EVENT, {
                "kind": "FlagFound", "flag": flag, "text": f"解出 flag: {flag}"}))
            out.append(self._mk(SOLVE_GRAPH_DELTA, {"kind": "flag", "flag": flag}))

        elif t is AEventType.RUN_STATUS:
            # orchestrator 用 run.status 表达进度；终态由 RUN_FINISHED 折叠，
            # 这里只在 solved 时补一条 flag 洞察（若引擎未发）。
            status = p.get("status")
            if status == "solved" and p.get("flag"):
                out.append(self._mk(INSIGHT_EVENT, {
                    "kind": "FlagFound", "flag": p["flag"],
                    "text": f"解出 flag: {p['flag']}"}))

        elif t is AEventType.RUN_FINISHED:
            status = p.get("status", "finished")
            reason = status if status in ("solved", "failed", "stopped") else "finished"
            outcome = "solved" if status == "solved" else (
                "operator_stop" if status == "stopped" else
                ("runtime_failure" if status == "failed" else "finished"))
            payload: dict[str, Any] = {"reason": reason, "outcome": outcome, "status": status}
            if p.get("flag"):
                payload["flag"] = p["flag"]
            if p.get("error"):
                payload["error"] = str(p["error"])
            out.append(self._mk(RUN_FINISHED, payload))

        return out

    # ── 子翻译 ───────────────────────────────────────────────────────────
    def _fact_delta(self, p: dict[str, Any]) -> list[MutekiEvent]:
        """黑板事实/发现 → fact_added + sharedgraph.delta + insight.event。"""
        fact = str(p.get("fact") or p.get("message") or p.get("text") or "")
        if not fact:
            return []
        verified = bool(p.get("verified", False))
        confidence = float(p.get("confidence", 0.5))
        actor = str(p.get("actor") or p.get("source") or "engine")
        fact_seq = self._next_fact_seq()
        return [
            self._mk(BLACKBOARD_DELTA, {
                "kind": "fact_added", "fact": fact, "verified": verified,
                "confidence": confidence, "actor": actor,
                "verifier": actor, "fact_seq": fact_seq,
            }, solver_id=actor if actor != "engine" else None),
            self._mk(SHARED_GRAPH_DELTA, {
                "fact": fact, "verified": verified, "confidence": confidence,
                "actor": actor, "verifier": actor, "fact_seq": fact_seq,
            }, solver_id=actor if actor != "engine" else None),
            self._mk(INSIGHT_EVENT, {
                "kind": "Discovery" if not verified else "VerifiedFact",
                "text": fact, "actor": actor,
            }),
        ]

    def _intent_proposed(self, p: dict[str, Any]) -> list[MutekiEvent]:
        intent_id = str(p.get("intent_id") or "")
        goal = str(p.get("action_type") or p.get("goal") or "solve")
        worker_class = str(p.get("worker_class") or "code")
        target = str(p.get("target") or "")
        if target:
            goal = f"{goal}: {target}"
        bb = self._mk(BLACKBOARD_DELTA, {
            "kind": "intent_proposed", "intent_id": intent_id,
            "goal": goal, "worker_class": worker_class,
            "actor": str(p.get("source") or "planner"),
        })
        ri = self._mk(REASON_INTENT, {
            "intents": [{"id": intent_id, "goal": goal, "worker_class": worker_class}],
            "goal_met": False, "audit": [],
        })
        return [bb, ri]
