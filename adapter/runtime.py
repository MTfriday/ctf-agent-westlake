"""SolverRuntime — Aemeath 执行层运行时单例。

聚合：平台客户端 + 共享黑板 + 引擎后端 + 提交门禁 + SSE 事件总线。
认知层（LLM）与 API 端点都通过本运行时调用工具函数，保证执行层单一入口。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from adapter.events import Event, EventType, ev
from adapter.gate import SubmitGate
from src.blackboard import BlackboardStore

logger = logging.getLogger(__name__)


class EventBus:
    """进程内广播总线（SSE 前端订阅）。慢消费者丢弃，避免拖垮生产者。"""

    def __init__(self, max_queue: int = 200) -> None:
        self._seq = 0
        self._subs: list[asyncio.Queue] = []
        self._max_queue = max_queue

    async def publish(self, event: Event) -> None:
        self._seq += 1
        event.seq = self._seq
        for q in list(self._subs):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # 丢弃最旧一条，保住最新事件
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except Exception:  # noqa: BLE001
                    pass

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._max_queue)
        self._subs.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        try:
            self._subs.remove(q)
        except ValueError:
            pass


class SolverRuntime:
    """执行层运行时：提供 LLM 可调用的工具函数。"""

    def __init__(
        self,
        settings: Optional[Any] = None,
        platform: Optional[Any] = None,
        store: Optional[BlackboardStore] = None,
        engine_backend: Optional[Any] = None,
        no_submit: bool = False,
    ) -> None:
        from backend.agents.coordinator_loop import _build_platform_client
        from backend.cost_tracker import CostTracker
        from backend.model_health import ModelHealthRegistry
        from backend.models import resolve_model_specs

        self.settings = settings if settings is not None else _settings()
        self.platform = platform or _build_platform_client(self.settings)
        self.store = store or BlackboardStore()
        self.cost_tracker = CostTracker()
        self.model_specs = resolve_model_specs(settings=self.settings)
        # 运行中实时感知模型不可用并自动剔除（自动更换到其余模型）
        self.model_health = ModelHealthRegistry()
        self.challenges_root = "challenges"
        self.no_submit = no_submit
        self.bus = EventBus()
        self.gate = SubmitGate(self.platform, self.store, self.bus, no_submit=self.no_submit)
        if engine_backend is None:
            from adapter.engines import build_engine_backend

            backend_name = getattr(self.settings, "adapter_engine_backend", "swarm")
            engine_backend = build_engine_backend(self, backend_name)
        self.engine = engine_backend
        self._challenge_cache: dict[str, dict[str, Any]] = {}
        self._orchestrator: Optional[Any] = None

    # ------------------------------------------------------------ 工具函数
    async def get_challenge(self, problem_id: str) -> dict[str, Any]:
        """platform_get_challenge — 题目详情（描述/附件/靶机/解出状态）。"""
        if problem_id in self._challenge_cache:
            return self._challenge_cache[problem_id]
        challenges = await self.platform.fetch_all_challenges()
        ch = next((c for c in challenges if c.get("name") == problem_id), None)
        if ch is None:
            raise KeyError(f"Challenge {problem_id!r} not found on platform")
        self._challenge_cache[problem_id] = ch
        return ch

    def get_active_model_specs(self) -> list[str]:
        """返回当前可用的模型规格（剔除被健康注册表禁用的模型）。

        运行中某模型失效（key 失效/模型下线/配额用尽/限流）会被实时剔除，
        其余模型继续顶替求解；冷却期过后自动恢复尝试。
        """
        return self.model_health.active_specs(self.model_specs)

    async def launch_solver(
        self,
        problem_id: str,
        *,
        prompt: str = "",
        target: str = "",
        attachments: Optional[list[str]] = None,
        mode: str = "auto",
    ) -> dict[str, Any]:
        """engine_launch_solver — 启动求解引擎，返回 run_id。"""
        # 执行前自动注入黑板上下文（死路/发现/计划），避免重复失败路径（蓝皮书：执行前注入）
        blackboard_ctx = self.store.get_context(problem_id)
        if blackboard_ctx and "暂无历史记忆" not in blackboard_ctx:
            prompt = (prompt + "\n\n" + blackboard_ctx).strip() if prompt else blackboard_ctx
        run_id = await self.engine.launch(
            problem_id, prompt=prompt, target=target, attachments=attachments, mode=mode
        )
        # 黑板：记录本次求解意图
        self.store.create_intent(
            problem_id, "solve", target=target or None, reasoning=f"engine:{mode}"
        )
        await self.bus.publish(
            ev(EventType.BLACKBOARD_DELTA, problem_id=problem_id,
               kind="intent", action="solve", mode=mode)
        )
        return {"run_id": run_id, "problem_id": problem_id, "mode": mode, "status": "running"}

    async def wait_result(self, run_id: str, timeout: float = 300.0) -> dict[str, Any]:
        """engine_wait_for_result — 阻塞等待引擎完成，超时则强制停止。"""
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            st = await self.engine.status(run_id)
            if st is None:
                return {"status": "unknown", "error": "unknown run_id"}
            if st["status"] in ("solved", "failed", "timeout", "stopped"):
                return st
            await asyncio.sleep(0.5)
        await self.stop_engine(run_id)
        return {"run_id": run_id, "status": "timeout", "error": "engine timed out"}

    async def stop_engine(self, run_id: str) -> bool:
        """engine_stop — 强制终止指定引擎运行。"""
        return await self.engine.stop(run_id)

    async def solver_status(self, run_id: str) -> Optional[dict[str, Any]]:
        return await self.engine.status(run_id)

    async def submit_flag(self, problem_id: str, flag: str, source: str = "manual") -> dict[str, Any]:
        """platform_submit_flag（带门禁）→ 返回 {status, solved, message, flag}。"""
        return await self.gate.submit(problem_id, flag, source=source)

    async def verify_solved(self, problem_id: str) -> dict[str, Any]:
        """platform_verify_solved — 排行榜 solved 状态（唯一可信判定）。"""
        return await self.gate.verify(problem_id)

    def blackboard_write(
        self, problem_id: str, key: str, value: str, source: str = "llm"
    ) -> int:
        """blackboard_write — 记录解题思路/错误/中间结果。"""
        return self.store.add_fact(problem_id, f"{key}: {value}", "discovery", source=source)

    @property
    def orchestrator(self) -> Any:
        """懒加载总控控制器（OrchestratorController），供 /api/orchestrator/* 使用。"""
        if self._orchestrator is None:
            from src.orchestrator import OrchestratorController

            self._orchestrator = OrchestratorController(self)
        return self._orchestrator

    @orchestrator.setter
    def orchestrator(self, value: Any) -> None:
        """允许外部注入/替换总控控制器（测试或定制）。"""
        self._orchestrator = value

    async def close(self) -> None:
        try:
            if self._orchestrator is not None:
                await self._orchestrator.shutdown()
        except Exception:  # noqa: BLE001
            pass
        try:
            await self.platform.close()
        except Exception:  # noqa: BLE001
            pass
        self.store.close()


def _settings() -> Any:
    from backend.config import Settings

    return Settings()
