"""模式路由 — 根据黑板统计 + 题目信息决策求解模式。

模式:
- swarm（🏎️ 竞速）  简单题 / 剩余时间少，追求速度
- orchestrated（🧠 总控） 复杂题 / 死路多，追求解决率
- hybrid（🔀 混合）  先用竞速试探，超时转总控

决策方式: 轻量 LLM（qwen3.6-flash）→ 失败回退规则（离线可用）。
决策理由 mode_reason 供前端展示。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from src.orchestrator.llm import LLMError

logger = logging.getLogger(__name__)

MODES = ("swarm", "orchestrated", "hybrid")
MODE_LABEL = {"swarm": "🏎️竞速", "orchestrated": "🧠总控", "hybrid": "🔀混合"}

_SYSTEM_PROMPT = """你是 CTF 解题系统的模式路由器。根据题目信息与黑板统计，\
输出严格 JSON：{"mode": "swarm|orchestrated|hybrid", "reason": "简短中文理由"}。
- swarm 竞速：简单题（解出人数多/低分/单步可解）、剩余时间少
- orchestrated 总控：复杂题（高分/解出少）、已有多条死路需要换思路
- hybrid 混合：不确定难度，先竞速试探，超时再转总控"""


class ModeRouter:
    """模式决策器。llm 可为 None（纯规则）。"""

    def __init__(self, llm: Any = None, store: Any = None, bus: Any = None) -> None:
        # store/bus 由外部注入（黑板与事件总线），无共同 Protocol，故用 Any
        self.llm = llm
        self.store = store
        self.bus = bus

    async def decide(
        self,
        problem_id: str,
        *,
        stats: Optional[dict] = None,
        challenge: Optional[dict] = None,
        remaining_time: Optional[float] = None,
        locked: Optional[str] = None,
    ) -> dict[str, Any]:
        """返回 {mode, mode_reason, source}。locked 优先于一切。"""
        if locked:
            if locked in MODES:
                return {"mode": locked, "mode_reason": "选手锁定模式", "source": "locked"}
            return {"mode": "hybrid", "mode_reason": "非法锁定值，回退混合", "source": "locked"}

        if self.llm is not None:
            try:
                return await self._llm_decide(problem_id, stats, challenge, remaining_time)
            except LLMError as e:
                logger.warning("mode_router LLM failed for %s: %s", problem_id, e)

        return self._fallback(stats, challenge, remaining_time)

    # ------------------------------------------------------------------ LLM
    async def _llm_decide(
        self,
        problem_id: str,
        stats: Optional[dict],
        challenge: Optional[dict],
        remaining_time: Optional[float],
    ) -> dict[str, Any]:
        user = _build_prompt(problem_id, stats, challenge, remaining_time)
        data = await self.llm.chat_json(_SYSTEM_PROMPT, user, temperature=0.0)
        mode = data.get("mode", "hybrid")
        if mode not in MODES:
            mode = "hybrid"
        return {"mode": mode, "mode_reason": data.get("reason", "LLM 决策"), "source": "llm"}

    # ------------------------------------------------------------------ 规则
    def _fallback(
        self,
        stats: Optional[dict],
        challenge: Optional[dict],
        remaining_time: Optional[float],
    ) -> dict[str, Any]:
        value = int((challenge or {}).get("value", 0) or 0)
        solves = (challenge or {}).get("solves")
        deadends = int((stats or {}).get("deadends", 0) or 0)

        if remaining_time is not None and remaining_time < 600:  # <10 分钟
            return {"mode": "swarm", "mode_reason": "剩余时间不足，切换竞速追分", "source": "rule"}
        if deadends >= 3:
            return {
                "mode": "orchestrated",
                "mode_reason": f"已产生 {deadends} 条死路，需要总控换思路",
                "source": "rule",
            }
        if solves is not None and solves >= 50:
            return {"mode": "swarm", "mode_reason": f"解出人数多（{solves}），判定为简单题", "source": "rule"}
        if value >= 300:
            return {"mode": "orchestrated", "mode_reason": f"高分题（{value} 分），追求解决率", "source": "rule"}
        return {"mode": "hybrid", "mode_reason": "默认混合模式，先竞速后总控", "source": "rule"}


def _build_prompt(
    problem_id: str,
    stats: Optional[dict],
    challenge: Optional[dict],
    remaining_time: Optional[float],
) -> str:
    lines = [f"题目: {problem_id}"]
    if challenge:
        lines.append(f"分类: {challenge.get('category', '?')}  分值: {challenge.get('value', 0)}  "
                      f"解出数: {challenge.get('solves', '?')}")
        if challenge.get("connection_info"):
            lines.append(f"有靶机: {challenge['connection_info']}")
        files = challenge.get("files") or []
        if files:
            lines.append(f"附件: {', '.join(f.get('name', '?') for f in files)}")
    if stats:
        lines.append(
            f"黑板统计: 尝试 {stats.get('attempts', 0)} 次, 死路 {stats.get('deadends', 0)} 条, "
            f"发现 {stats.get('discoveries', 0)} 条, 活跃计划 {stats.get('active_intents', 0)} 个"
        )
    if remaining_time is not None:
        lines.append(f"剩余时间: {int(remaining_time)} 秒")
    return "\n".join(lines)
