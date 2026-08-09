"""题目工具端点 — 获取题目详情 + 黑板上下文。"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from adapter.deps import get_runtime
from adapter.runtime import SolverRuntime

router = APIRouter(prefix="/api/challenge", tags=["challenge"])


class PrepareRequest(BaseModel):
    problem_id: str


@router.post("/prepare")
async def prepare(body: PrepareRequest, runtime: SolverRuntime = Depends(get_runtime)) -> dict:
    """获取题目详情并执行基础判定（供 LLM 做「一眼出」决策）。"""
    try:
        ch = await runtime.get_challenge(body.problem_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"platform error: {e}") from e

    return {
        "problem_id": body.problem_id,
        "name": ch.get("name"),
        "category": ch.get("category", ""),
        "value": ch.get("value", 0),
        "description": ch.get("description", ""),
        "target": ch.get("connection_info", ""),  # 靶机地址（entry）
        "attachments": [f.get("name") for f in (ch.get("files") or [])],  # 附件名列表
        "solved": ch.get("solved", False),
        "blackboard_context": runtime.store.get_context(body.problem_id),
    }


# 题目列表（独立前缀 /api/challenges，供驾驶舱首页）
list_router = APIRouter(tags=["challenge"])


@list_router.get("/api/challenges")
async def list_challenges(runtime: SolverRuntime = Depends(get_runtime)) -> dict:
    """题目列表摘要（名称/分类/分值/solves/solved）。"""
    try:
        challenges = await runtime.platform.fetch_all_challenges()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"platform error: {e}") from e
    try:
        solved = await runtime.platform.fetch_solved_names()
    except Exception:  # noqa: BLE001
        solved = set()
    return {
        "challenges": [
            {
                "id": str(c.get("id", "")),
                "name": c.get("name"),
                "category": c.get("category", ""),
                "value": c.get("value", 0),
                "solves": c.get("solves", 0),
                "solved": c.get("name") in solved,
            }
            for c in challenges
        ]
    }
