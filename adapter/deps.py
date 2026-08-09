"""FastAPI 依赖注入 — 从 app.state 取共享运行时。"""

from __future__ import annotations

from fastapi import Request

from adapter.runtime import SolverRuntime


def get_runtime(request: Request) -> SolverRuntime:
    return request.app.state.runtime
