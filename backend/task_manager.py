"""Task state machine and priority scheduler for challenge solving.

Provides:
- TaskState enum with valid transitions
- Task record with metadata
- PriorityScheduler for task ordering

The existing ChallengeSwarm can optionally integrate with this for state tracking.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


class TaskState(str, enum.Enum):
    """Task state machine states."""

    NEW = "new"
    ANALYZING = "analyzing"
    EXPLOITING = "exploiting"
    SUBMITTED = "submitted"
    SOLVED = "solved"
    FAILED = "failed"
    NEEDS_HUMAN = "needs_human"
    CANCELLED = "cancelled"

    def can_transition_to(self, target: TaskState) -> bool:
        """Check if transition to target state is valid."""
        return target in _TASK_TRANSITIONS.get(self, set())

    def transition_to(self, target: TaskState) -> TaskState:
        """Perform state transition. Raises ValueError if invalid."""
        if self.can_transition_to(target):
            return target
        raise ValueError(f"Invalid transition: {self.value} -> {target.value}")


# Valid transitions map (module-level to avoid Enum attribute restrictions)
_TASK_TRANSITIONS: dict[TaskState, set[TaskState]] = {
    TaskState.NEW: {TaskState.ANALYZING, TaskState.CANCELLED},
    TaskState.ANALYZING: {TaskState.EXPLOITING, TaskState.FAILED, TaskState.NEEDS_HUMAN, TaskState.CANCELLED},
    TaskState.EXPLOITING: {TaskState.SUBMITTED, TaskState.FAILED, TaskState.NEEDS_HUMAN, TaskState.CANCELLED},
    TaskState.SUBMITTED: {TaskState.SOLVED, TaskState.EXPLOITING, TaskState.FAILED, TaskState.CANCELLED},
    TaskState.FAILED: {TaskState.ANALYZING, TaskState.NEEDS_HUMAN, TaskState.CANCELLED},
    TaskState.NEEDS_HUMAN: {TaskState.ANALYZING, TaskState.CANCELLED},
    TaskState.SOLVED: set(),
    TaskState.CANCELLED: set(),
}


@dataclass
class TaskRecord:
    """A single challenge solving task with state tracking."""

    challenge_id: str | int
    challenge_name: str
    category: str = ""
    value: int = 0
    priority: int = 0  # Higher = more urgent
    state: TaskState = TaskState.NEW
    attempts: int = 0
    max_attempts: int = 3
    flag: str | None = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    solved_at: float | None = None

    # Human intervention
    human_note: str = ""
    human_hints: list[str] = field(default_factory=list)

    def transition_to(self, target: TaskState) -> None:
        """Transition to target state, updating timestamps."""
        old_state = self.state
        self.state = self.state.transition_to(target)
        self.updated_at = time.time()
        if target == TaskState.SOLVED:
            self.solved_at = time.time()
        logger.debug("Task %s: %s -> %s", self.challenge_name, old_state.value, target.value)

    def can_retry(self) -> bool:
        """Check if the task can be retried (within max attempts)."""
        return self.attempts < self.max_attempts

    def mark_attempt(self) -> None:
        """Increment attempt counter."""
        self.attempts += 1
        self.updated_at = time.time()

    def add_hint(self, hint: str) -> None:
        """Add a human-provided hint."""
        self.human_hints.append(hint)
        self.updated_at = time.time()


class PriorityScheduler:
    """Async task scheduler with priority queue support.

    Maintains a heap of tasks ordered by priority (higher first),
    with optional tie-breaking by creation time (earlier first).
    """

    def __init__(self, max_concurrent: int = 10) -> None:
        self.max_concurrent = max_concurrent
        self._tasks: dict[str | int, TaskRecord] = {}
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._running: set[str | int] = set()
        self._lock: asyncio.Lock = asyncio.Lock()

    def add_task(self, task: TaskRecord) -> None:
        """Add a task to the scheduler."""
        self._tasks[task.challenge_id] = task
        # Priority queue uses negative priority for max-heap behavior
        self._queue.put_nowait((-task.priority, time.time(), task.challenge_id))
        logger.info("Task queued: %s (priority=%d)", task.challenge_name, task.priority)

    async def get_next_task(self) -> TaskRecord | None:
        """Get the next highest-priority task that should be processed."""
        if len(self._running) >= self.max_concurrent:
            return None

        while not self._queue.empty():
            neg_priority, created_at, task_id = await self._queue.get()
            task = self._tasks.get(task_id)
            if task is None or task.state in (TaskState.SOLVED, TaskState.CANCELLED):
                continue
            if task.state == TaskState.NEEDS_HUMAN:
                continue  # Skip tasks waiting for human intervention
            self._running.add(task_id)
            return task

        return None

    def complete_task(self, task_id: str | int) -> None:
        """Mark a task as completed (removed from running set)."""
        self._running.discard(task_id)

    def adjust_priority(self, task_id: str | int, new_priority: int) -> bool:
        """Dynamically adjust a task's priority. Returns True if found."""
        task = self._tasks.get(task_id)
        if not task:
            return False
        task.priority = new_priority
        # Re-queue with new priority
        self._queue.put_nowait((-new_priority, time.time(), task_id))
        logger.info("Priority adjusted: %s -> %d", task.challenge_name, new_priority)
        return True

    def get_task(self, task_id: str | int) -> TaskRecord | None:
        """Get a task by ID."""
        return self._tasks.get(task_id)

    @property
    def active_count(self) -> int:
        return len(self._running)

    @property
    def pending_count(self) -> int:
        pending = 0
        for t in self._tasks.values():
            if t.state in (TaskState.NEW, TaskState.ANALYZING, TaskState.EXPLOITING,
                           TaskState.FAILED):
                pending += 1
        return pending

    @property
    def solved_count(self) -> int:
        return sum(1 for t in self._tasks.values() if t.state == TaskState.SOLVED)

    def get_summary(self) -> dict[str, Any]:
        """Get summary of all task states for dashboard display."""
        state_counts: dict[str, int] = {}
        for t in self._tasks.values():
            state_counts[t.state.value] = state_counts.get(t.state.value, 0) + 1

        return {
            "total": len(self._tasks),
            "running": self.active_count,
            "pending": self.pending_count,
            "solved": self.solved_count,
            "by_state": state_counts,
            "tasks": [
                {
                    "id": t.challenge_id,
                    "name": t.challenge_name,
                    "category": t.category,
                    "state": t.state.value,
                    "priority": t.priority,
                    "attempts": t.attempts,
                    "has_flag": t.flag is not None,
                    "needs_human": t.state == TaskState.NEEDS_HUMAN,
                }
                for t in sorted(self._tasks.values(), key=lambda x: -x.priority)
            ],
        }
