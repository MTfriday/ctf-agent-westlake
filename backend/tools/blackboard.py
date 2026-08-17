"""黑板读写工具 — hybrid 模式：solver 与编排器共享黑板记忆。

- blackboard_read   读取当前题的黑板上下文（发现/死路/候选 flag/活跃计划）
- blackboard_write  写入一条发现 / 死路 / 部分结果

写入后通过 EventBus 广播 BLACKBOARD_DELTA，前端知识黑板/证据链面板实时更新。
仅当 SolverDeps.store 非空时工具才可用（swarm 模式不接入则自动隐藏）。
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic_ai import RunContext

from backend.deps import SolverDeps

logger = logging.getLogger(__name__)


async def blackboard_read(ctx: RunContext[SolverDeps]) -> str:
    """读取当前题目的共享黑板：已有发现、已确认死路、候选 Flag、活跃计划。

    求解前 / 卡住时调用，避免重复尝试已确认失败的方向，并利用已有发现。
    """
    store = getattr(ctx.deps, "store", None)
    if store is None:
        return "（黑板未接入）"
    problem_id = ctx.deps.challenge_name
    try:
        context = store.get_context(problem_id)
        return context
    except Exception as e:  # noqa: BLE001
        logger.warning("blackboard_read %s failed: %s", problem_id, e)
        return f"（读取黑板失败: {e}）"


async def blackboard_write(
    ctx: RunContext[SolverDeps],
    content: str,
    kind: str = "discovery",
) -> str:
    """把一条求解发现写入共享黑板，供其他 Worker 与总控参考。

    kind: discovery（发现/结论）| deadend（已确认失败的方向）| partial（部分结果/候选）。
    每条 ≤200 字符。已解出的题无需再写。
    """
    store = getattr(ctx.deps, "store", None)
    if store is None:
        return "（黑板未接入）"
    if kind not in ("discovery", "deadend", "partial"):
        kind = "discovery"
    problem_id = ctx.deps.challenge_name
    text = (content or "").strip()[:200]
    if not text:
        return "内容为空，未写入。"
    try:
        if kind == "deadend":
            fact_id = store.mark_deadend(problem_id, text, source=f"solver:{ctx.deps.model_spec}")
        elif kind == "partial":
            fact_id = store.add_partial(problem_id, text, source=f"solver:{ctx.deps.model_spec}")
        else:
            fact_id = store.add_discovery(problem_id, text, source=f"solver:{ctx.deps.model_spec}")
        # 广播黑板变更 → 前端面板 + 其它 solver 可见
        await _publish(ctx, problem_id, kind, fact_id)
        return f"已写入黑板（{kind}）: {text}"
    except Exception as e:  # noqa: BLE001
        logger.warning("blackboard_write %s failed: %s", problem_id, e)
        return f"（写入黑板失败: {e}）"


async def _publish(ctx: RunContext[SolverDeps], problem_id: str, kind: str, fact_id: int) -> None:
    """通过 notify_coordinator 之外的事件通道广播。SolverDeps 无 bus 引用，
    这里通过 message_bus 作为旁路（swarm 内共享）；真正的 EventBus 广播由
    ChallengeSwarm._report_fact 在引擎侧完成（避免重复）。"""
    # 无 EventBus 引用，留给引擎层的 on_fact_added 回调处理前端广播
    _ = (ctx, problem_id, kind, fact_id)
