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
    # 续传上下文：启动时注入的黑板上下文 + 操作员提示（断点续传关键）
    prompt: str = ""

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
        run.prompt = prompt  # 保存黑板上下文 + 操作员提示，供 solver 续传使用
        run.status = "running"
        await self.runtime.bus.publish(
            ev(EventType.RUN_STARTED, run_id=run.run_id, problem_id=problem_id, mode=mode)
        )
        run.task = asyncio.create_task(self._run_swarm(run), name=f"swarm-{run.run_id}")
        return run.run_id

    async def _run_swarm(self, run: RunRecord) -> None:
        trace_task: Optional[asyncio.Task] = None
        try:
            await self._emit(run, f"[swarm:{run.mode}] 启动真实求解引擎")
            platform = self.runtime.platform

            # 1. 定位题目
            challenges = await platform.fetch_all_challenges()
            ch = next((c for c in challenges if c.get("name") == run.problem_id), None)
            if ch is None:
                raise RuntimeError(f"Challenge not found on platform: {run.problem_id}")

            # 1.5 动态容器题：connection_info 为空时尝试启动实例，拿到真实入口
            if not str(ch.get("connection_info") or "").strip():
                entry = await self._start_platform_env(ch)
                if entry:
                    ch["connection_info"] = entry
                    await self._emit(run, f"已启动动态实例: {entry}", "success")

            # 2. 拉取附件 + 元数据
            from backend.prompts import ChallengeMeta

            ch_dir = await platform.pull_challenge(ch, self.runtime.challenges_root)
            meta = ChallengeMeta.from_yaml(str(Path(ch_dir) / "metadata.yml"))
            if not meta.connection_info and str(ch.get("connection_info") or "").strip():
                meta.connection_info = str(ch["connection_info"]).strip()

            # 3. 构建 ChallengeSwarm 并行求解，逐步过程经队列转发到前端
            from backend.agents.swarm import ChallengeSwarm
            from backend.solver_base import FLAG_FOUND

            trace_queue: asyncio.Queue = asyncio.Queue(maxsize=2000)

            def _trace_sink(model_spec: str, event: dict) -> None:
                try:
                    trace_queue.put_nowait((model_spec, event))
                except asyncio.QueueFull:
                    pass

            async def _forward_trace() -> None:
                while True:
                    model_spec, event = await trace_queue.get()
                    kind = event.get("type", "log")
                    payload = {"kind": kind, "solver": model_spec, **event}
                    await self.runtime.bus.publish(
                        ev(EventType.ENGINE_LOG, run_id=run.run_id,
                           problem_id=run.problem_id, **payload)
                    )

            trace_task = asyncio.create_task(_forward_trace(), name=f"trace-{run.run_id}")

            swarm = ChallengeSwarm(
                challenge_dir=ch_dir,
                meta=meta,
                ctfd=platform,
                cost_tracker=self.runtime.cost_tracker,
                settings=self.runtime.settings,
                model_specs=self.runtime.model_specs,
                no_submit=self.runtime.no_submit,
                trace_sink=_trace_sink,
                # 断点续传：把黑板上下文 + 操作员提示作为额外系统提示喂给 solver，
                # 让新启动的 solver 知道之前的发现 / 死路 / 操作员意图。
                extra_context=run.prompt,
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
                # 前端 flag 区点亮：FLAG_SOLVED → solvegraph.delta(flag)
                await self.runtime.bus.publish(
                    ev(EventType.FLAG_SOLVED, run_id=run.run_id, problem_id=run.problem_id,
                       flag=run.flag)
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
        finally:
            if trace_task is not None:
                trace_task.cancel()
                try:
                    await trace_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass

    async def _start_platform_env(self, ch: dict[str, Any]) -> str:
        """尝试为动态容器题获取实例入口（host:port / URL）。

        优先从 challenge detail 的 context.instanceEntry 读取（容器可能已存在，
        列表接口不含入口，详情接口才有）；读不到再尝试 start_environment 创建。
        """
        platform = self.runtime.platform
        cid = str(ch.get("id") or "")
        if not cid:
            return ""
        try:
            # 1) 详情接口：已有实例的入口在这里（列表接口不含）
            if hasattr(platform, "get_challenge_detail"):
                try:
                    detail = await platform.get_challenge_detail(cid)
                    entry = getattr(detail, "connection_info", "") or ""
                    if str(entry).strip():
                        return str(entry).strip()
                except Exception as e:  # noqa: BLE001
                    logger.warning("get_challenge_detail for %s: %s", ch.get("name"), e)
            # 2) 无已有实例 → 尝试创建动态容器
            if hasattr(platform, "start_environment"):
                env = await platform.start_environment(cid)
                entry = getattr(env, "entry", "") or ""
                if str(entry).strip():
                    return str(entry).strip()
        except Exception as e:  # noqa: BLE001
            logger.warning("start_environment failed for %s: %s", ch.get("name"), e)
        return ""

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
