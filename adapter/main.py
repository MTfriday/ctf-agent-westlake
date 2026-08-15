"""Aemeath 执行层适配器 — FastAPI 应用入口。

实现蓝皮书「模块一」全部端点 + muteki 契约桥接（P6 重设计）：
    POST /api/challenge/prepare       题目详情 + 黑板上下文
    POST /api/solver/start            启动求解
    GET  /api/solver/status/{run_id}  查询进度
    POST /api/solver/stop/{run_id}    终止引擎
    POST /api/flag/submit             提交 Flag（门禁）
    GET  /api/flag/verify/{problem_id} 验证是否解出
    GET  /api/events/stream           SSE 事件流
    ── muteki 契约（frontend/ 直连）──
    /api/runs* /api/folders* /api/auth/* /api/engines* /api/settings/*

启动: `python -m adapter` 或 `uvicorn adapter.main:app --port 12345`
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from adapter.muteki import routes as muteki_routes
from adapter.routes import blackboard, challenge, flag, orchestrator, solver, stream
from adapter.runtime import SolverRuntime

logger = logging.getLogger(__name__)


def create_app(runtime: Optional[SolverRuntime] = None) -> FastAPI:
    """构建 FastAPI 应用。传入 runtime 可复用（测试时注入 mock）。"""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if app.state.runtime is None:
            app.state.runtime = SolverRuntime()
        rt = app.state.runtime
        # 启动 muteki RunManager 同步循环 + challenge 预注册
        if getattr(rt, "muteki_manager", None) is None:
            from adapter.muteki.manager import RunManager

            rt.muteki_manager = RunManager(rt)
        await rt.muteki_manager.start_sync()
        yield
        try:
            await rt.muteki_manager.stop()
        except Exception:  # noqa: BLE001
            pass
        await rt.close()

    app = FastAPI(title="Aemeath Adapter", version="0.2.0", lifespan=lifespan)
    app.state.runtime = runtime  # 外部注入优先，否则 lifespan 构建

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(challenge.router)
    app.include_router(challenge.list_router)
    app.include_router(blackboard.router)
    app.include_router(solver.router)
    app.include_router(flag.router)
    app.include_router(orchestrator.router)
    app.include_router(stream.router)
    app.include_router(muteki_routes.router)

    @app.get("/api/health")
    async def health() -> dict:
        rt: SolverRuntime = app.state.runtime
        return {"status": "ok", "engine": type(rt.engine).__name__}

    return app


app = create_app()
