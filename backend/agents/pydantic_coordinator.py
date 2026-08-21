"""Pydantic AI coordinator — runs the shared event loop with any OpenAI-compatible model.

Lets the coordinator use DeepSeek / Bailian / OpenAI models via pydantic-ai,
so no Claude or Codex CLI is required.
"""

from __future__ import annotations

import logging
from typing import Any

from backend.agents.coordinator_core import (
    do_broadcast,
    do_bump_agent,
    do_check_swarm_status,
    do_fetch_challenges,
    do_get_solve_status,
    do_kill_swarm,
    do_read_solver_trace,
    do_spawn_swarm,
    do_submit_flag,
)
from backend.agents.coordinator_loop import build_deps, run_event_loop
from backend.config import Settings
from backend.deps import CoordinatorDeps
from backend.models import model_id_from_spec, provider_from_spec, resolve_model, resolve_model_settings

logger = logging.getLogger(__name__)

COORDINATOR_PROMPT = """\
You are a CTF competition coordinator running for the ENTIRE duration of a live competition.
Your job is to maximize the number of challenges solved.

Strategy:
- Call fetch_challenges to see what's available, then spawn_swarm for unsolved challenges,
  prioritizing easy ones (fewer solves first).
- Use read_solver_trace to monitor what each solver is doing and where it's stuck.
- When agents are stuck, read their traces, then craft targeted bumps with specific technical guidance.
- Use broadcast to share cross-solver insights (e.g. flag format discovery, shared vulnerabilities).

CRITICAL RULES:
- NEVER kill a swarm. Solvers will keep trying indefinitely with different approaches.
  Even when stuck, they often unstick themselves after several bumps. Your job is to
  HELP them, not give up on them. The only time a swarm should die is when the flag
  is confirmed correct.
- When a solver seems stuck, bump it with very specific technical guidance based on
  its trace. Tell it exactly what to try next — specific tools, techniques, approaches.
- Cost is not a concern. Keep all swarms running.

You will receive event messages. Respond with tool calls to manage the competition.
"""


def _build_agent(deps: CoordinatorDeps, spec: str):
    """Build a pydantic-ai Agent with the coordinator tools."""
    from pydantic_ai import Agent

    model = resolve_model(spec, deps.settings)
    model_settings = resolve_model_settings(spec)

    agent = Agent(
        model,
        system_prompt=COORDINATOR_PROMPT,
        model_settings=model_settings,
    )

    @agent.tool_plain
    async def fetch_challenges() -> str:
        """List all challenges with category, points, solve count, and status."""
        return await do_fetch_challenges(deps)

    @agent.tool_plain
    async def get_solve_status() -> str:
        """Check which challenges are solved and which swarms are running."""
        return await do_get_solve_status(deps)

    @agent.tool_plain
    async def spawn_swarm(challenge_name: str) -> str:
        """Launch all solver models on a challenge."""
        return await do_spawn_swarm(deps, challenge_name)

    @agent.tool_plain
    async def check_swarm_status(challenge_name: str) -> str:
        """Get per-agent progress for a swarm."""
        return await do_check_swarm_status(deps, challenge_name)

    @agent.tool_plain
    async def submit_flag(challenge_name: str, flag: str) -> str:
        """Submit a flag to the platform."""
        return await do_submit_flag(deps, challenge_name, flag)

    @agent.tool_plain
    async def kill_swarm(challenge_name: str) -> str:
        """Cancel all agents for a challenge."""
        return await do_kill_swarm(deps, challenge_name)

    @agent.tool_plain
    async def bump_agent(challenge_name: str, model_spec: str, insights: str) -> str:
        """Send targeted insights to a stuck agent."""
        return await do_bump_agent(deps, challenge_name, model_spec, insights)

    @agent.tool_plain
    async def broadcast(challenge_name: str, message: str) -> str:
        """Broadcast a strategic hint to ALL solvers on a challenge."""
        return await do_broadcast(deps, challenge_name, message)

    @agent.tool_plain
    async def read_solver_trace(challenge_name: str, model_spec: str, last_n: int = 20) -> str:
        """Read recent trace events from a specific solver to understand where it's stuck."""
        return await do_read_solver_trace(deps, challenge_name, model_spec, last_n)

    return agent


async def run_pydantic_coordinator(
    settings: Settings,
    model_specs: list[str] | None = None,
    challenges_root: str = "challenges",
    no_submit: bool = False,
    coordinator_model: str | None = None,
    msg_port: int = 0,
) -> dict[str, Any]:
    """Run the pydantic-ai coordinator with the shared event loop.

    coordinator_model: a spec like "deepseek/deepseek-v4-flash" or "bailian/qwen-max".
    Defaults to settings.coordinator_model or "deepseek/deepseek-v4-flash".
    """
    ctfd, cost_tracker, deps = build_deps(
        settings, model_specs, challenges_root, no_submit,
    )
    deps.msg_port = msg_port

    spec = coordinator_model or getattr(settings, "coordinator_model", "") or "deepseek/deepseek-v4-flash"
    agent = _build_agent(deps, spec)
    logger.info("Pydantic coordinator using model: %s", spec)

    async def turn_fn(msg: str) -> None:
        # 无 request_limit：coordinator 首次 turn 要 spawn 所有未解题（几十次 LLM
        # 往返），pydantic-ai 默认 request_limit=50 会在中途抛 UsageLimitExceeded。
        from pydantic_ai.usage import UsageLimits
        from backend.llm_net import run_with_retry

        result = await run_with_retry(agent, msg, usage_limits=UsageLimits(request_limit=None))
        # Log cost if available
        try:
            usage = result.usage  # pydantic-ai 2.18+: RunUsage attribute, not a method
            cost_tracker.record(
                "coordinator",
                usage,
                model_id_from_spec(spec),
                provider_spec=provider_from_spec(spec),
            )
        except Exception:
            pass

    return await run_event_loop(deps, ctfd, cost_tracker, turn_fn)
