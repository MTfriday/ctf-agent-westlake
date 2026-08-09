"""黑板端点 — 快照 / 重置 / 上下文注入（P6 配套，供前端驾驶舱）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from adapter.deps import get_runtime
from adapter.events import EventType, ev
from adapter.runtime import SolverRuntime

router = APIRouter(prefix="/api/blackboard", tags=["blackboard"])


@router.get("/{problem_id}")
async def snapshot(problem_id: str, runtime: SolverRuntime = Depends(get_runtime)) -> dict:
    """黑板快照：facts + intents + stats + 注入用上下文。"""
    store = runtime.store
    return {
        "problem_id": problem_id,
        "facts": store.list_facts(problem_id, limit=500),
        "intents": store.list_intents(problem_id, status=None, limit=200),
        "stats": store.get_stats(problem_id),
        "context": store.get_context(problem_id),
    }


@router.post("/{problem_id}/reset")
async def reset(problem_id: str, runtime: SolverRuntime = Depends(get_runtime)) -> dict:
    """重置此题黑板（前端「重置此题黑板」按钮）。"""
    deleted = runtime.store.clear_problem(problem_id)
    await runtime.bus.publish(
        ev(EventType.BLACKBOARD_DELTA, problem_id=problem_id, kind="reset", deleted=deleted)
    )
    return {"problem_id": problem_id, "deleted": deleted}


@router.post("/{problem_id}/inject")
async def inject(problem_id: str, runtime: SolverRuntime = Depends(get_runtime)) -> dict:
    """记录「强制注入上下文」动作（黑板摘要已注入后续 solver prompt）。"""
    context = runtime.store.get_context(problem_id)
    fact_id = runtime.blackboard_write(
        problem_id,
        "context_inject",
        f"黑板上下文已强制注入（{len(context)} 字符）",
        source="operator",
    )
    await runtime.bus.publish(
        ev(EventType.BLACKBOARD_DELTA, problem_id=problem_id, kind="inject", fact_id=fact_id)
    )
    return {"problem_id": problem_id, "injected": True, "fact_id": fact_id}
