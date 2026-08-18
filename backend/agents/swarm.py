"""ChallengeSwarm — Parallel solvers racing on one challenge."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

from backend.agents.solver import Solver
from backend.cost_tracker import CostTracker
from backend.ctfd import CTFdClient
from backend.message_bus import ChallengeMessageBus
from backend.models import provider_from_spec, resolve_model_specs
from backend.prompts import ChallengeMeta
from backend.solver_base import (
    CANCELLED,
    ERROR,
    FLAG_FOUND,
    GAVE_UP,
    QUOTA_ERROR,
    SolverProtocol,
    SolverResult,
)

if TYPE_CHECKING:
    from backend.config import Settings

logger = logging.getLogger(__name__)


# Quota fallback: map subscription-backed providers to API-backed equivalents
QUOTA_FALLBACK: dict[str, str] = {
    "claude-sdk/claude-opus-4-6": "bedrock/us.anthropic.claude-opus-4-6-v1",
    "codex/gpt-5.4": "azure/gpt-5.4",
    "codex/gpt-5.4-mini": "azure/gpt-5.4-mini",
    "codex/gpt-5.3-codex-spark": "zen/gpt-5.3-codex-spark",
}


def _quota_fallback_spec(model_spec: str) -> str | None:
    return QUOTA_FALLBACK.get(model_spec)


@dataclass
class ChallengeSwarm:
    """Parallel solvers racing on one challenge."""

    challenge_dir: str
    meta: ChallengeMeta
    ctfd: CTFdClient
    cost_tracker: CostTracker
    settings: Settings
    model_specs: list[str] = field(default_factory=list)
    no_submit: bool = False
    coordinator_inbox: asyncio.Queue | None = None
    # 外部逐步过程 sink：收到 solver 的 tool_call/tool_result/model_response 事件
    # （同步回调，供 adapter 转发到前端 SSE）
    trace_sink: Callable[[str, dict], None] | None = None
    # 断点续传：上次求解的黑板上下文 + 操作员提示，注入 solver 系统提示
    extra_context: str = ""
    # 模型健康注册表（实时感知不可用模型并剔除）；None 表示不接入
    health: Any = None
    # 模型被禁用时回调（供 adapter 转发 ENGINE_LOG 到前端），签名 on_disabled(spec, detail)
    on_model_disabled: Callable[[str, str], None] | None = None
    # 共享黑板 store（hybrid 模式）；None 表示 swarm 纯竞速不接黑板
    store: Any = None
    # solver 写入黑板事实后回调（供 adapter 广播 BLACKBOARD_DELTA），签名 on_fact(kind)
    on_fact_added: Callable[[str], None] | None = None

    def __post_init__(self) -> None:
        """Resolve model specs from settings if not explicitly provided."""
        if not self.model_specs:
            self.model_specs = resolve_model_specs(settings=self.settings)

    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    solvers: dict[str, SolverProtocol] = field(default_factory=dict)
    findings: dict[str, str] = field(default_factory=dict)
    winner: SolverResult | None = None
    confirmed_flag: str | None = None
    _flag_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _submit_count: dict[str, int] = field(default_factory=dict)  # per-model wrong submission count
    _submitted_flags: set[str] = field(default_factory=set)  # dedup exact flags
    _last_submit_time: dict[str, float] = field(default_factory=dict)  # per-model last submit timestamp
    _total_submits: int = 0  # 本题实际提交到平台的总次数（平台每题上限保护）
    message_bus: ChallengeMessageBus = field(default_factory=ChallengeMessageBus)

    def _create_solver(self, model_spec: str):
        """Create the right solver type based on provider.

        - claude-sdk/* → ClaudeSolver (Claude Agent SDK, subscription-first)
        - codex/* → CodexSolver (Codex App Server, subscription-first)
        - bedrock/*, azure/*, zen/*, google/* → Pydantic AI Solver (API)
        """
        provider = provider_from_spec(model_spec)

        def _submit_fn(flag): return self.try_submit_flag(flag, model_spec)
        _notify = self._make_notify_fn(model_spec)

        def _trace(event: dict) -> None:
            if self.trace_sink is not None:
                self.trace_sink(model_spec, event)

        if provider == "claude-sdk":
            from backend.agents.claude_solver import ClaudeSolver
            return ClaudeSolver(
                model_spec=model_spec,
                challenge_dir=self.challenge_dir,
                meta=self.meta,
                ctfd=self.ctfd,
                cost_tracker=self.cost_tracker,
                settings=self.settings,
                cancel_event=self.cancel_event,
                no_submit=self.no_submit,
                submit_fn=_submit_fn,
                message_bus=self.message_bus,
                notify_coordinator=_notify,
                trace_sink=_trace if self.trace_sink is not None else None,
                extra_context=self.extra_context,
            )

        if provider == "codex":
            from backend.agents.codex_solver import CodexSolver
            return CodexSolver(
                model_spec=model_spec,
                challenge_dir=self.challenge_dir,
                meta=self.meta,
                ctfd=self.ctfd,
                cost_tracker=self.cost_tracker,
                settings=self.settings,
                cancel_event=self.cancel_event,
                no_submit=self.no_submit,
                submit_fn=_submit_fn,
                message_bus=self.message_bus,
                notify_coordinator=_notify,
                trace_sink=_trace if self.trace_sink is not None else None,
                extra_context=self.extra_context,
            )

        return self._create_pydantic_solver(model_spec)

    def _make_notify_fn(self, model_spec: str):
        """Create a callback that pushes solver messages to the coordinator inbox."""
        async def _notify(message: str) -> None:
            if self.coordinator_inbox:
                self.coordinator_inbox.put_nowait(
                    f"[{self.meta.name}/{model_spec}] {message}"
                )
        return _notify

    def _create_pydantic_solver(self, model_spec: str, sandbox=None, owns_sandbox: bool | None = None) -> Solver:
        """Create a Pydantic AI solver. Pass sandbox to reuse an existing container (quota fallback)."""
        solver = Solver(
            model_spec=model_spec,
            challenge_dir=self.challenge_dir,
            meta=self.meta,
            ctfd=self.ctfd,
            cost_tracker=self.cost_tracker,
            settings=self.settings,
            cancel_event=self.cancel_event,
            sandbox=sandbox,
            owns_sandbox=owns_sandbox,
            trace_sink=(
                (lambda event: self.trace_sink(model_spec, event))
                if self.trace_sink is not None else None
            ),
            extra_context=self.extra_context,
        )
        solver.deps.message_bus = self.message_bus
        solver.deps.model_spec = model_spec
        solver.deps.no_submit = self.no_submit
        solver.deps.submit_fn = lambda flag: self.try_submit_flag(flag, model_spec)
        solver.deps.notify_coordinator = self._make_notify_fn(model_spec)
        # hybrid 模式：把共享黑板注入 solver（工具集据此注册 blackboard_read/write）
        solver.deps.store = self.store
        if self.on_fact_added is not None:
            solver.deps.on_blackboard_update = lambda kind: self.on_fact_added(kind)
        return solver

    def _gather_sibling_insights(self, exclude_model: str) -> str:
        parts: list[str] = []
        for model, finding in self.findings.items():
            if model != exclude_model and finding:
                parts.append(f"[{model}]: {finding}")
        # 黑板历史发现（hybrid）：把该题已沉淀的 discovery/deadend/partial 也喂给
        # 下一轮 bump，实现「没找到 → 记黑板 → 下一轮从黑板继续」的闭环，
        # 避免 solver 重复已试过的方向。
        if self.store is not None:
            try:
                facts = self.store.list_facts(self.meta.name, limit=60)
                board = [
                    f for f in facts
                    if f.get("type") in ("discovery", "deadend", "partial")
                    and not str(f.get("content") or "").startswith("__SUBMITS__:")
                ]
                if board:
                    board_lines = [
                        f"- [{f.get('type')}] {str(f.get('content'))[:300]}"
                        for f in board[-25:]
                    ]
                    parts.append("[黑板历史发现]:\n" + "\n".join(board_lines))
            except Exception:  # noqa: BLE001
                pass
        return "\n\n".join(parts) if parts else "No sibling insights available yet."

    # 提交计数持久化：用黑板 partial 事实记录累计提交次数（约定前缀 __SUBMITS__:），
    # 使预算跨 swarm 实例生效——auto-solve 恢复会新建 ChallengeSwarm（内存计数归零），
    # 不持久化则每次恢复预算都被重置，模型可反复消耗平台提交次数。
    def _read_submit_count(self) -> int:
        if self.store is None:
            return self._total_submits
        try:
            for f in self.store.list_facts(self.meta.name, type="partial", limit=200):
                c = str(f.get("content") or "")
                if c.startswith("__SUBMITS__:"):
                    try:
                        return int(c.split(":", 1)[1].strip())
                    except ValueError:
                        pass
        except Exception:  # noqa: BLE001
            pass
        return self._total_submits

    def _persist_submit_count(self, n: int) -> None:
        if self.store is None:
            return
        try:
            self.store.add_fact(
                self.meta.name, f"__SUBMITS__:{n}", type="partial", source="swarm"
            )
        except Exception:  # noqa: BLE001
            pass

    # Escalating cooldowns after incorrect submissions (per model)
    SUBMISSION_COOLDOWNS = [0, 30, 120, 300, 600]  # 0s, 30s, 2min, 5min, 10min

    async def try_submit_flag(self, flag: str, model_spec: str) -> tuple[str, bool]:
        """Cooldown-gated, deduplicated flag submission. Returns (display, is_confirmed)."""
        async with self._flag_lock:
            if self.confirmed_flag:
                return f"ALREADY SOLVED — flag already confirmed: {self.confirmed_flag}", True

            normalized = flag.strip()

            # Dedup exact flags across all models
            if normalized in self._submitted_flags:
                return "INCORRECT — already tried this exact flag.", False

            # 跨 swarm 实例累计：合并黑板上持久化的提交计数（auto-solve 恢复新建
            # swarm 时内存计数归零，预算会被重置——用持久化计数顶上来）
            persisted = self._read_submit_count()
            if persisted > self._total_submits:
                self._total_submits = persisted

            # 平台规则：每题 flag 最大提交次数（超过后平台拒绝提交）
            max_submit = getattr(self.settings, "flag_max_submit", 50)
            if max_submit > 0 and self._total_submits >= max_submit:
                return (
                    f"STOP — 本题 flag 提交次数已达上限 {max_submit} 次，"
                    "平台将拒绝后续提交。请停止盲目尝试，深入分析并仔细核对 flag。",
                    False,
                )

            # 内部提交预算（防盲猜乱交）：超过后硬性禁止继续提交，强制分析。
            # 模型反复猜测无意义 flag（如 DASCTF{607}、DASCTF{25f}）会耗尽平台
            # 50 次上限，故内部再设一道更低的预算线（持久化，跨 swarm 累计）。
            guess_limit = getattr(self.settings, "flag_guess_limit", 0)
            if guess_limit > 0 and self._total_submits >= guess_limit:
                return (
                    f"HARD STOP — 本题 flag 提交已达内部预算 {guess_limit} 次，"
                    "禁止继续猜测提交（会耗尽平台上限）。请停止提交，继续深入分析；"
                    "仅当你能从服务返回 / 附件内容直接提取到明确 flag 时，"
                    "先在黑板上记录该候选与证据，再考虑是否提交。",
                    False,
                )

            # Escalating cooldown after incorrect submissions
            wrong_count = self._submit_count.get(model_spec, 0)
            cooldown_idx = min(wrong_count, len(self.SUBMISSION_COOLDOWNS) - 1)
            cooldown = self.SUBMISSION_COOLDOWNS[cooldown_idx]
            if cooldown > 0:
                last_time = self._last_submit_time.get(model_spec, 0)
                elapsed = time.monotonic() - last_time
                if elapsed < cooldown:
                    remaining = int(cooldown - elapsed)
                    return (
                        f"COOLDOWN — wait {remaining}s before submitting again. "
                        f"You have {wrong_count} incorrect submissions. "
                        "Use this time to do deeper analysis and verify your flag.",
                        False,
                    )

            self._submitted_flags.add(normalized)
            self._total_submits += 1
            self._persist_submit_count(self._total_submits)

            from backend.tools.core import do_submit_flag
            display, is_confirmed = await do_submit_flag(self.ctfd, self.meta.name, flag)
            if is_confirmed:
                self.confirmed_flag = normalized
            else:
                self._submit_count[model_spec] = wrong_count + 1
                self._last_submit_time[model_spec] = time.monotonic()
            return display, is_confirmed

    async def _run_solver(self, model_spec: str) -> SolverResult | None:
        solver = self._create_solver(model_spec)
        self.solvers[model_spec] = solver

        try:
            result, final_solver = await self._run_solver_loop(solver, model_spec)
            solver = final_solver
            return result
        except Exception as e:
            logger.error(f"[{self.meta.name}/{model_spec}] Fatal: {e}", exc_info=True)
            # 致命异常也上报健康（网络错误/认证错误等），以便实时剔除不可用模型
            if self.health is not None:
                disabled = self.health.report_failure(model_spec, str(e))
                if disabled and self.on_model_disabled is not None:
                    self.on_model_disabled(model_spec, str(e)[:200])
            return None
        finally:
            await solver.stop()

    def _report_success(self, model_spec: str) -> None:
        if self.health is not None:
            self.health.report_success(model_spec)

    def _report_failure(self, model_spec: str, error: str) -> None:
        if self.health is None:
            return
        disabled = self.health.report_failure(model_spec, error)
        if disabled and self.on_model_disabled is not None:
            self.on_model_disabled(model_spec, error[:200])

    async def _run_solver_loop(self, solver, model_spec: str) -> tuple[SolverResult, SolverProtocol]:
        """Inner loop: start → run → bump → run → ..."""
        bump_count = 0
        consecutive_errors = 0
        result = SolverResult(
            flag=None, status=CANCELLED, findings_summary="",
            step_count=0, cost_usd=0.0, log_path="",
        )
        await solver.start()

        while not self.cancel_event.is_set():
            # 实时健康感知：模型已被禁用（致命 403/连续错误）→ 立即终止，不再浪费 token
            if self.health is not None and model_spec not in self.health.active_specs([model_spec]):
                logger.warning(
                    "[%s/%s] 模型已被禁用，提前终止", self.meta.name, model_spec
                )
                break
            result = await solver.run_until_done_or_gave_up()

            # Only broadcast useful findings — skip errors and broken solvers
            if (result.status not in (ERROR, QUOTA_ERROR)
                    and not (result.step_count == 0 and result.cost_usd == 0)
                    and result.findings_summary
                    and not result.findings_summary.startswith(("Error:", "Turn failed:"))):
                self.findings[model_spec] = result.findings_summary
                await self.message_bus.post(model_spec, result.findings_summary[:500])

            # hybrid 模式：solver 本轮进展沉淀到共享黑板（discovery）
            if self.store is not None:
                try:
                    activity = ""
                    try:
                        activity = solver.recent_activity()
                    except Exception:  # noqa: BLE001
                        activity = ""
                    # recent_activity 为空（如一轮直接结束）时，用 findings 兜底，
                    # 保证「没找到」的进展一定记到黑板，供下一轮/断点续跑使用。
                    if not activity and result.findings_summary and not result.findings_summary.startswith(
                        ("Error:", "Turn failed:")
                    ):
                        activity = result.findings_summary
                    if activity:
                        self.store.add_discovery(
                            self.meta.name,
                            f"[{model_spec}] {activity[:400]}",
                            source=f"solver:{model_spec}",
                        )
                        if self.on_fact_added is not None:
                            self.on_fact_added("discovery")
                        logger.info(
                            "[%s/%s] 黑板沉淀 discovery（%d chars）",
                            self.meta.name, model_spec, len(activity),
                        )
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "[%s/%s] 黑板写入失败: %s", self.meta.name, model_spec, e
                    )

            if result.status == FLAG_FOUND:
                self._report_success(model_spec)
                self.cancel_event.set()
                self.winner = result
                logger.info(
                    f"[{self.meta.name}] Flag found by {model_spec}: {result.flag}"
                )
                return result, solver

            if result.status == CANCELLED:
                break

            # Quota exhaustion: fall back to API-backed Pydantic AI solver
            if result.status == QUOTA_ERROR:
                self._report_failure(model_spec, result.findings_summary or "quota error")
                fallback_spec = _quota_fallback_spec(model_spec)
                if fallback_spec:
                    logger.warning(
                        f"[{self.meta.name}/{model_spec}] Quota exhausted — falling back to {fallback_spec}"
                    )
                    existing_sandbox = solver.sandbox
                    # Detach sandbox from old solver so stop() doesn't destroy it
                    solver.sandbox = None  # type: ignore[assignment]
                    await solver.stop()
                    solver = self._create_pydantic_solver(fallback_spec, sandbox=existing_sandbox, owns_sandbox=True)
                    self.solvers[model_spec] = solver
                    await solver.start()
                    continue
                # No fallback available, treat as error
                break

            if result.status in (GAVE_UP, ERROR):
                if result.step_count == 0 and result.cost_usd == 0:
                    logger.warning(
                        f"[{self.meta.name}/{model_spec}] Broken (0 steps, $0) — not bumping"
                    )
                    # 0 步直接失败 → 上报健康（可能模型一启动就挂：认证/模型名错/400）
                    if result.status == ERROR:
                        self._report_failure(model_spec, result.findings_summary or "start error")
                    break

                # 有实际产出但没解出 → 模型本身可用，重置失败计数
                self._report_success(model_spec)

                # Track consecutive errors — stop after 3 in a row
                if result.status == ERROR:
                    consecutive_errors += 1
                    if consecutive_errors >= 3:
                        logger.warning(
                            f"[{self.meta.name}/{model_spec}] {consecutive_errors} consecutive errors — giving up"
                        )
                        break
                else:
                    consecutive_errors = 0

                bump_count += 1
                # Cooldown between bumps — check cancellation during wait
                try:
                    await asyncio.wait_for(
                        self.cancel_event.wait(),
                        timeout=min(bump_count * 30, 300),
                    )
                    break  # cancelled during cooldown
                except TimeoutError:
                    pass  # cooldown elapsed, proceed with bump
                insights = self._gather_sibling_insights(model_spec)
                solver.bump(insights)
                logger.info(
                    f"[{self.meta.name}/{model_spec}] Bumped ({bump_count}), resuming"
                )
                continue

        return result, solver

    async def run(self) -> SolverResult | None:
        """Run all solvers in parallel. Returns the winner's result or None."""
        tasks = [
            asyncio.create_task(self._run_solver(spec), name=f"solver-{spec}")
            for spec in self.model_specs
        ]

        try:
            while tasks:
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

                for task in done:
                    try:
                        result = task.result()
                    except Exception:
                        continue
                    if result and result.status == FLAG_FOUND:
                        self.cancel_event.set()
                        for p in pending:
                            p.cancel()
                        await asyncio.gather(*pending, return_exceptions=True)
                        return result

                tasks = list(pending)

            self.cancel_event.set()
            return self.winner
        except Exception as e:
            logger.error(f"[{self.meta.name}] Swarm error: {e}", exc_info=True)
            self.cancel_event.set()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            return None

    def kill(self) -> None:
        """Cancel all agents for this challenge."""
        self.cancel_event.set()

    def get_status(self) -> dict:
        """Get per-agent progress and findings."""
        return {
            "challenge": self.meta.name,
            "cancelled": self.cancel_event.is_set(),
            "winner": self.winner.flag if self.winner else None,
            "agents": {
                spec: {
                    "findings": self.findings.get(spec, ""),
                    "status": "running" if spec in self.solvers and not self.cancel_event.is_set()
                             else ("won" if self.winner and self.winner.flag else "finished"),
                }
                for spec in self.model_specs
            },
        }
