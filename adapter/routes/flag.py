"""Flag 端点 — 提交（带门禁）/ 验证。"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from adapter.deps import get_runtime
from adapter.runtime import SolverRuntime

router = APIRouter(prefix="/api/flag", tags=["flag"])


class SubmitRequest(BaseModel):
    problem_id: str
    flag: str
    source: str = "manual"  # manual | engine | one-shot


@router.post("/submit")
async def submit(body: SubmitRequest, runtime: SolverRuntime = Depends(get_runtime)) -> dict:
    """提交 Flag（统一门禁：去重 + 串行化 + 排行榜验证）。"""
    return await runtime.submit_flag(body.problem_id, body.flag, source=body.source)


@router.get("/verify/{problem_id}")
async def verify(problem_id: str, runtime: SolverRuntime = Depends(get_runtime)) -> dict:
    """验证题目是否已解出（以排行榜 solved 为准）。"""
    return await runtime.verify_solved(problem_id)
