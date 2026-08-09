"""可插拔引擎后端 — 封装求解引擎调度（竞速 swarm / 总控 OODA）。

EngineBackend 协议:
    launch(problem_id, *, prompt, target, attachments, mode) -> run_id
    status(run_id) -> dict | None
    stop(run_id) -> bool

实现:
- MockEngineBackend    模拟引擎（无平台/无 LLM 时联调与测试）
- SwarmEngineBackend   对接现有 ChallengeSwarm 真实求解引擎
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from adapter.events import Event, EventType, ev

logger = logging.getLogger(__name__)

_TERMINAL = {"solved", "failed", "timeout", "stopped"}


@dataclass
class RunRecord:
    """单个求解 run 的状态记录。"""

    run_id: str
    problem_id: str
    mode: str = "auto"
    status: str = "starting"  # starting | running | solved | failed | timeout | stopped
    flag: Optional[str] = None
    error: Optional[str] = None
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    logs: list[dict] = field(default_factory=list)  # [{"ts","level","message"}]
    task: Optional[asyncio.Task] = None

    def to_dict(self, log_tail: int = 50) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "problem_id": self.problem_id,
            "mode": self.mode,
            "status": self.status,
            "flag": self.flag,
            "error": self.error,
            "elapsed": round((self.finished_at or time.time()) - self.started_at, 2),
            "log_tail": self.logs[-log_tail:],
        }


class EngineBackend(ABC):
    """引擎后端基类：维护 run 注册表 + 日志/事件广播。"""

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        self.runs: dict[str, RunRecord] = {}

    def _new_run(self, problem_id: str, mode: str) -> RunRecord:
        run = RunRecord(run_id=uuid.uuid4().hex[:12], problem_id=problem_id, mode=mode)
        self.runs[run.run_id] = run
        return run

    async def _emit(self, run: RunRecord, message: str, level: str = "info") -> None:
        run.logs.append({"ts": time.time(), "level": level, "message": message})
        await self.runtime.bus.publish(
            ev(EventType.ENGINE_LOG, run_id=run.run_id, problem_id=run.problem_id,
               level=level, message=message, mode=run.mode)
        )

    @abstractmethod
    async def launch(
        self,
        problem_id: str,
        *,
        prompt: str = "",
        target: str = "",
        attachments: Optional[list[str]] = None,
        mode: str = "auto",
    ) -> str:
        """启动求解，返回 run_id。"""

    async def status(self, run_id: str) -> Optional[dict[str, Any]]:
        run = self.runs.get(run_id)
        return run.to_dict() if run else None

    async def stop(self, run_id: str) -> bool:
        run = self.runs.get(run_id)
        if run is None:
            return False
        if run.task and not run.task.done():
            run.task.cancel()
        run.status = "stopped"
        run.finished_at = time.time()
        await self.runtime.bus.publish(
            ev(EventType.RUN_FINISHED, run_id=run_id, problem_id=run.problem_id, status="stopped")
        )
        return True


class MockEngineBackend(EngineBackend):
    """模拟引擎 — 无平台/无 LLM 时用于前端联调与集成测试。

    mode 影响行为：swarm=快(约2s)成功、hybrid=中速、orchestrated=慢(多步日志)。
    启动时向黑板写入发现，模拟引擎与记忆层联动。
    """

    _DELAY = {"swarm": 1, "hybrid": 2, "orchestrated": 4}

    async def launch(
        self,
        problem_id: str,
        *,
        prompt: str = "",
        target: str = "",
        attachments: Optional[list[str]] = None,
        mode: str = "auto",
    ) -> str:
        run = self._new_run(problem_id, mode)
        run.status = "running"
        await self.runtime.bus.publish(
            ev(EventType.RUN_STARTED, run_id=run.run_id, problem_id=problem_id, mode=mode)
        )
        run.task = asyncio.create_task(self._simulate(run), name=f"mock-run-{run.run_id}")
        return run.run_id

    async def _simulate(self, run: RunRecord) -> None:
        try:
            await self._emit(run, "读取题目信息…")
            await asyncio.sleep(0.5)
            await self._emit(run, "分析附件与靶机…")
            delay = self._DELAY.get(run.mode, 2)
            await asyncio.sleep(delay)
            run.status = "solved"
            run.flag = f"flag{{mock-{run.problem_id[:8]}}}"
            run.finished_at = time.time()
            await self._emit(run, f"求解完成，候选 flag: {run.flag}", "success")
            self.runtime.store.add_discovery(
                run.problem_id, f"[mock] 引擎给出候选 flag {run.flag}", source=f"engine:{run.run_id}"
            )
            await self.runtime.bus.publish(
                ev(EventType.BLACKBOARD_DELTA, problem_id=run.problem_id, kind="discovery")
            )
            await self.runtime.bus.publish(
                ev(EventType.RUN_FINISHED, run_id=run.run_id, problem_id=run.problem_id,
                   status="solved", flag=run.flag)
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            run.status = "failed"
            run.error = str(e)
            run.finished_at = time.time()
            await self.runtime.bus.publish(
                ev(EventType.RUN_FINISHED, run_id=run.run_id, problem_id=run.problem_id,
                   status="failed", error=str(e))
            )


class SwarmEngineBackend(EngineBackend):
    """对接现有 ChallengeSwarm 引擎（真实求解）。

    launch 时自动从平台拉取题目（challenges/ 目录），创建 ChallengeSwarm
    并行求解；求解结果同步到 run 记录与黑板。
    """

    def __init__(self, runtime: Any) -> None:
        super().__init__(runtime)
        self._swarms: dict[str, Any] = {}

    async def launch(
        self,
        problem_id: str,
        *,
        prompt: str = "",
        target: str = "",
        attachments: Optional[list[str]] = None,
        mode: str = "auto",
    ) -> str:
        run = self._new_run(problem_id, mode)
        run.status = "running"
        await self.runtime.bus.publish(
            ev(EventType.RUN_STARTED, run_id=run.run_id, problem_id=problem_id, mode=mode)
        )
        run.task = asyncio.create_task(self._run_swarm(run), name=f"swarm-{run.run_id}")
        return run.run_id

    async def _run_swarm(self, run: RunRecord) -> None:
        try:
            await self._emit(run, f"[swarm:{run.mode}] 启动真实求解引擎")
            platform = self.runtime.platform

            # 1. 定位题目
            challenges = await platform.fetch_all_challenges()
            ch = next((c for c in challenges if c.get("name") == run.problem_id), None)
            if ch is None:
                raise RuntimeError(f"Challenge not found on platform: {run.problem_id}")

            # 2. 拉取附件 + 元数据
            from backend.prompts import ChallengeMeta

            ch_dir = await platform.pull_challenge(ch, self.runtime.challenges_root)
            meta = ChallengeMeta.from_yaml(str(Path(ch_dir) / "metadata.yml"))

            # 3. 构建 ChallengeSwarm 并行求解
            from backend.agents.swarm import ChallengeSwarm
            from backend.solver_base import FLAG_FOUND

            swarm = ChallengeSwarm(
                challenge_dir=ch_dir,
                meta=meta,
                ctfd=platform,
                cost_tracker=self.runtime.cost_tracker,
                settings=self.runtime.settings,
                model_specs=self.runtime.model_specs,
                no_submit=self.runtime.no_submit,
            )
            self._swarms[run.problem_id] = swarm
            await self._emit(run, f"swarm 已就绪（{len(self.runtime.model_specs)} 个模型）")

            result = await swarm.run()
            if result and result.status == FLAG_FOUND:
                run.status = "solved"
                run.flag = result.flag
                self.runtime.store.add_discovery(
                    run.problem_id, f"[swarm] 引擎确认 flag {result.flag}", source=f"engine:{run.run_id}"
                )
                await self.runtime.bus.publish(
                    ev(EventType.BLACKBOARD_DELTA, problem_id=run.problem_id, kind="discovery")
                )
            else:
                run.status = "failed"
                run.error = "engine finished without a confirmed flag"
            run.finished_at = time.time()
            await self.runtime.bus.publish(
                ev(EventType.RUN_FINISHED, run_id=run.run_id, problem_id=run.problem_id,
                   status=run.status, flag=run.flag, error=run.error)
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            run.status = "failed"
            run.error = str(e)
            run.finished_at = time.time()
            await self.runtime.bus.publish(
                ev(EventType.RUN_FINISHED, run_id=run.run_id, problem_id=run.problem_id,
                   status="failed", error=str(e))
            )

    async def status(self, run_id: str) -> Optional[dict[str, Any]]:
        base = await super().status(run_id)
        if base is None:
            return None
        run = self.runs.get(run_id)
        if run and run.status == "running":
            swarm = self._swarms.get(run.problem_id)
            if swarm:
                base["swarm"] = swarm.get_status()
        return base

    async def stop(self, run_id: str) -> bool:
        run = self.runs.get(run_id)
        if run and run.problem_id in self._swarms:
            self._swarms[run.problem_id].kill()
        return await super().stop(run_id)


def build_engine_backend(runtime: Any, name: str) -> EngineBackend:
    """按名称构建引擎后端（adapter_engine_backend 配置项）。"""
    if name == "mock":
        return MockEngineBackend(runtime)
    return SwarmEngineBackend(runtime)
