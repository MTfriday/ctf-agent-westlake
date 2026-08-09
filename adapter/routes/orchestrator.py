"""总控端点 — 前端对 OODA 循环的双向控制通道（模块四）。

端点（对应蓝皮书 §模块四）:
    POST /api/orchestrator/start          启动总控（problem_id, mode 可选）
    POST /api/orchestrator/stop           强制终止
    POST /api/orchestrator/pause          暂停（Worker 完成当前 Intent 后不再领新任务）
    POST /api/orchestrator/resume         恢复
    GET  /api/orchestrator/status/{problem_id}  单题状态
    GET  /api/orchestrator/status/all     所有调度器状态
    POST /api/orchestrator/mode/lock      锁定模式（mode 或 null 解锁）
    POST /api/orchestrator/intent/manual  人工添加 Intent（绕过 AI 规划）
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from adapter.deps import get_runtime
from adapter.runtime import SolverRuntime

router = APIRouter(prefix="/api/orchestrator", tags=["orchestrator"])

VALID_MODES = ("swarm", "orchestrated", "hybrid")


class ProblemRequest(BaseModel):
    problem_id: str


class StartRequest(BaseModel):
    problem_id: str
    mode: Optional[str] = None  # swarm | orchestrated | hybrid | null=auto


class LockRequest(BaseModel):
    problem_id: str
    mode: Optional[str] = None  # null 解锁


class ManualIntentRequest(BaseModel):
    problem_id: str
    action_type: str
    target: Optional[str] = None
    reasoning: Optional[str] = None


def _mode_of(mode: Optional[str]) -> Optional[str]:
    return mode if mode in VALID_MODES else None


@router.post("/start")
async def start(body: StartRequest, runtime: SolverRuntime = Depends(get_runtime)) -> dict:
    """启动该题的总控 OODA 循环。mode 可选（锁定模式）。"""
    return await runtime.orchestrator.start(body.problem_id, mode=_mode_of(body.mode))


@router.post("/stop")
async def stop(body: ProblemRequest, runtime: SolverRuntime = Depends(get_runtime)) -> dict:
    """强制终止该题总控。"""
    return await runtime.orchestrator.stop(body.problem_id)


@router.post("/pause")
async def pause(body: ProblemRequest, runtime: SolverRuntime = Depends(get_runtime)) -> dict:
    """暂停：Worker 完成当前 Intent 后不再领新任务。"""
    return await runtime.orchestrator.pause(body.problem_id)


@router.post("/resume")
async def resume(body: ProblemRequest, runtime: SolverRuntime = Depends(get_runtime)) -> dict:
    """恢复暂停的总控。"""
    return await runtime.orchestrator.resume(body.problem_id)


@router.get("/status/all")
async def status_all(runtime: SolverRuntime = Depends(get_runtime)) -> dict:
    """返回全部已启动的总控状态。注意：必须在 /status/{problem_id} 之前注册。"""
    return {"orchestrators": runtime.orchestrator.status_all()}


@router.get("/status/{problem_id}")
async def status(problem_id: str, runtime: SolverRuntime = Depends(get_runtime)) -> dict:
    """返回 status / current_cycle / active_intents / current_mode / mode_reason / uptime。"""
    return runtime.orchestrator.status(problem_id)


@router.post("/mode/lock")
async def lock_mode(body: LockRequest, runtime: SolverRuntime = Depends(get_runtime)) -> dict:
    """选手强制锁定模式（mode 或 null 解锁）。"""
    return await runtime.orchestrator.lock_mode(body.problem_id, _mode_of(body.mode))


@router.post("/intent/manual")
async def manual_intent(body: ManualIntentRequest, runtime: SolverRuntime = Depends(get_runtime)) -> dict:
    """选手手动添加 Intent（绕过 AI 规划）。"""
    return await runtime.orchestrator.add_manual_intent(
        body.problem_id, body.action_type, target=body.target, reasoning=body.reasoning
    )
