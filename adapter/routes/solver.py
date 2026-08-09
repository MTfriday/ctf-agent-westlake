"""求解器端点 — 启动求解 / 查询进度。"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from adapter.deps import get_runtime
from adapter.runtime import SolverRuntime

router = APIRouter(prefix="/api/solver", tags=["solver"])

# 允许的模式（P4 由 mode_router 决策；此处接受显式指定）
VALID_MODES = ("swarm", "orchestrated", "hybrid", "auto")


class StartRequest(BaseModel):
    problem_id: str
    mode: str = "auto"
    prompt: str = ""
    target: str = ""
    attachments: Optional[list[str]] = None


@router.post("/start")
async def start(body: StartRequest, runtime: SolverRuntime = Depends(get_runtime)) -> dict:
    """启动求解。mode ∈ swarm | orchestrated | hybrid | auto。"""
    mode = body.mode if body.mode in VALID_MODES else "auto"
    return await runtime.launch_solver(
        body.problem_id,
        prompt=body.prompt,
        target=body.target,
        attachments=body.attachments,
        mode=mode,
    )


@router.get("/status/{run_id}")
async def status(run_id: str, runtime: SolverRuntime = Depends(get_runtime)) -> dict:
    """查询求解进度与日志。"""
    st = await runtime.solver_status(run_id)
    if st is None:
        raise HTTPException(status_code=404, detail=f"Unknown run_id: {run_id}")
    return st


@router.post("/stop/{run_id}")
async def stop(run_id: str, runtime: SolverRuntime = Depends(get_runtime)) -> dict:
    """强制终止引擎运行。"""
    ok = await runtime.stop_engine(run_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Unknown run_id: {run_id}")
    return {"run_id": run_id, "status": "stopped"}
