"""Aemeath SSE 事件模型 — 执行层与前端/总控之间的事件契约。

借鉴 muteki `core/events.py` 设计：类型化事件 + payload 构造器，保证前端
不散拼字典、生产者不漂移。前端经 `/api/events/stream` (SSE) 订阅；总控
（src/orchestrator，P4）与引擎均向同一事件总线发布。
"""

from __future__ import annotations

import time
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class EventType(str, Enum):
    # ── Run 生命周期 ──
    RUN_STARTED = "run.started"
    RUN_STATUS = "run.status"
    RUN_FINISHED = "run.finished"
    # ── 引擎日志 ──
    ENGINE_LOG = "engine.log"
    # ── 黑板变更 ──
    BLACKBOARD_DELTA = "blackboard.delta"
    # ── 总控模式 / 意图 ──
    MODE_CHANGED = "mode.changed"
    INTENT_PROPOSED = "intent.proposed"
    INTENT_CLAIMED = "intent.claimed"
    INTENT_CONCLUDED = "intent.concluded"
    # ── 提交门禁 ──
    FLAG_SUBMITTED = "flag.submitted"
    FLAG_VERIFIED = "flag.verified"
    FLAG_SOLVED = "flag.solved"


class Event(BaseModel):
    event_type: EventType
    seq: int = 0  # 单调递增，用于 SSE Last-Event-ID 续传
    ts: float = 0.0  # epoch 秒
    run_id: str = ""
    problem_id: Optional[str] = None
    payload: dict[str, Any] = Field(default_factory=dict)

    def model_post_init(self, __context: Any) -> None:
        if not self.ts:
            object.__setattr__(self, "ts", time.time())

    def to_sse(self) -> str:
        """渲染为 Server-Sent-Events 帧（id 携带 seq 供续传）。"""
        return f"id: {self.seq}\nevent: {self.event_type.value}\ndata: {self.model_dump_json()}\n\n"


def ev(
    event_type: EventType,
    *,
    problem_id: Optional[str] = None,
    run_id: str = "",
    **payload: Any,
) -> Event:
    """Typed payload 构造器。"""
    return Event(event_type=event_type, problem_id=problem_id, run_id=run_id, payload=payload)
