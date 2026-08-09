"""SSE 事件流端点 — 实时推送引擎日志/黑板/模式/提交事件。

用原生 Starlette StreamingResponse 手动构造 SSE 帧（避免 sse_starlette
对连续快速事件的流式丢失问题）。断线后前端可用 Last-Event-ID 续传。
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends
from starlette.responses import StreamingResponse

from adapter.deps import get_runtime
from adapter.runtime import SolverRuntime

router = APIRouter(tags=["stream"])

# 心跳间隔（秒）
_KEEPALIVE = 15


@router.get("/api/events/stream")
async def events_stream(runtime: SolverRuntime = Depends(get_runtime)) -> StreamingResponse:
    """订阅全局事件流（SSE）。"""

    queue = runtime.bus.subscribe()

    async def gen():
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=_KEEPALIVE)
                    # SSE 帧规范要求 CRLF
                    yield event.to_sse().replace("\n", "\r\n")
                except asyncio.TimeoutError:
                    yield ": keep-alive\r\n\r\n"
        finally:
            runtime.bus.unsubscribe(queue)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )
