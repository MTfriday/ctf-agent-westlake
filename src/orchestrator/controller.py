"""总控控制器 — 组合 mode_router / planner / dispatcher / scheduler。

供 P5 的 /api/orchestrator/* 端点调用：
    start / stop / pause / resume / status / status_all
    lock_mode / add_manual_intent
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from src.orchestrator.dispatcher import Dispatcher
from src.orchestrator.llm import llm_from_settings
from src.orchestrator.mode_router import MODES, ModeRouter
from src.orchestrator.planner import Planner
from src.orchestrator.scheduler import OrchestratorScheduler

logger = logging.getLogger(__name__)

# 哨兵：区分「未传」与「显式 None（禁用 LLM）」
_NO_LLM = object()


def orchestrator_settings(settings: Optional[Any] = None) -> dict[str, Any]:
    """读取 config.yaml orchestrator.* 配置（无则用默认）。"""
    if settings is None:
        from backend.config import Settings

        settings = Settings()
    get = lambda name, default: getattr(settings, name, default)  # noqa: E731
    return {
        "enabled": bool(get("orchestrator_enabled", True)),
        "main_model": get("orchestrator_main_model", "qwen3.7-max"),
        "router_model": get("orchestrator_router_model", "qwen3.6-flash"),
        "max_intents": int(get("orchestrator_max_intents", 4)),
        "intent_timeout_seconds": float(get("orchestrator_intent_timeout_seconds", 120)),
        "observe_interval_seconds": float(get("orchestrator_observe_interval_seconds", 10)),
        "max_rounds": int(get("orchestrator_max_rounds", 20)),
    }


class OrchestratorController:
    """管理每题一个 OrchestratorScheduler 实例。"""

    def __init__(
        self,
        runtime: Any,
        *,
        config: Optional[dict] = None,
        router_llm: Any = _NO_LLM,
        main_llm: Any = _NO_LLM,
    ) -> None:
        """controller = 组合调度器。

        router_llm / main_llm: 显式注入 LLM 客户端；默认从 Settings 构建
        （百炼 qwen，未配 key 自动回退规则）。传 None 强制禁用 LLM（离线/测试）。
        """
        self.runtime = runtime
        self.store = runtime.store
        self.bus = runtime.bus
        self.config = config or orchestrator_settings(runtime.settings)
        # 路由用轻量模型，规划用主力模型
        self.router_llm = (
            router_llm
            if router_llm is not _NO_LLM
            else llm_from_settings(runtime.settings, self.config["router_model"])
        )
        self.main_llm = (
            main_llm
            if main_llm is not _NO_LLM
            else llm_from_settings(runtime.settings, self.config["main_model"])
        )
        self._schedulers: dict[str, OrchestratorScheduler] = {}

    # ------------------------------------------------------------------ 控制
    def _make_scheduler(self, problem_id: str) -> OrchestratorScheduler:
        mode_router = ModeRouter(llm=self.router_llm, store=self.store, bus=self.bus)
        planner = Planner(
            llm=self.main_llm, store=self.store, bus=self.bus,
            max_intents=self.config["max_intents"],
        )
        dispatcher = Dispatcher(
            store=self.store, bus=self.bus, timeout=self.config["intent_timeout_seconds"]
        )
        return OrchestratorScheduler(
            problem_id,
            store=self.store,
            bus=self.bus,
            mode_router=mode_router,
            planner=planner,
            dispatcher=dispatcher,
            get_challenge=self.runtime.get_challenge,
            observe_interval=self.config["observe_interval_seconds"],
            intent_timeout=self.config["intent_timeout_seconds"],
            max_rounds=self.config["max_rounds"],
        )

    def get_scheduler(self, problem_id: str) -> Optional[OrchestratorScheduler]:
        return self._schedulers.get(problem_id)

    async def start(self, problem_id: str, mode: Optional[str] = None) -> dict[str, Any]:
        sched = self._schedulers.get(problem_id)
        if sched is None:
            sched = self._make_scheduler(problem_id)
            self._schedulers[problem_id] = sched
        if mode:
            sched.locked_mode = mode if mode in MODES else None
        await sched.start()
        return sched.status_dict()

    async def stop(self, problem_id: str) -> dict[str, Any]:
        sched = self.get_scheduler(problem_id)
        if sched is None:
            return {"problem_id": problem_id, "status": "not_running"}
        await sched.stop()
        return sched.status_dict()

    async def pause(self, problem_id: str) -> dict[str, Any]:
        sched = self.get_scheduler(problem_id)
        if sched is None:
            return {"problem_id": problem_id, "status": "not_running"}
        await sched.pause()
        return sched.status_dict()

    async def resume(self, problem_id: str) -> dict[str, Any]:
        sched = self.get_scheduler(problem_id)
        if sched is None:
            return await self.start(problem_id)
        sched.resume()
        return sched.status_dict()

    async def lock_mode(self, problem_id: str, mode: Optional[str]) -> dict[str, Any]:
        sched = self.get_scheduler(problem_id)
        if sched is None:
            sched = self._make_scheduler(problem_id)
            self._schedulers[problem_id] = sched
        await sched.lock_mode(mode)
        return sched.status_dict()

    async def add_manual_intent(
        self, problem_id: str, action_type: str, target: Optional[str] = None, reasoning: Optional[str] = None
    ) -> dict[str, Any]:
        sched = self.get_scheduler(problem_id)
        if sched is None:
            sched = self._make_scheduler(problem_id)
            self._schedulers[problem_id] = sched
        return await sched.add_manual_intent(action_type, target, reasoning)

    def status(self, problem_id: str) -> dict[str, Any]:
        sched = self.get_scheduler(problem_id)
        if sched is None:
            return {
                "problem_id": problem_id,
                "status": "idle", "current_mode": "auto",
                "mode_label": "auto", "mode_reason": "尚未启动总控",
                "locked_mode": None, "active_intents": 0, "uptime": 0.0,
            }
        return sched.status_dict()

    def status_all(self) -> list[dict[str, Any]]:
        return [s.status_dict() for s in self._schedulers.values()]

    async def shutdown(self) -> None:
        for sched in self._schedulers.values():
            await sched.stop()
