"""规划器 — 生成 ≤max_intents 个独立 Intent 并写入黑板（pending）。

Reason 阶段（OODA）：orchestrated / hybrid 模式下由主力 LLM（qwen3.7-max）
根据题目 + 黑板上下文规划下一步行动。LLM 失败时回退规则 Intent（离线可用）。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from adapter.events import EventType, ev
from src.orchestrator.llm import LLMError

logger = logging.getLogger(__name__)

# 建议的 action_type 枚举（提示 LLM 使用）
_ACTION_HINTS = ("analyze_attachment", "scan_target", "crack", "brute", "reverse",
                 "research", "exploit", "submit")

# 注意：普通字符串（非 f-string），仅 {max_intents} 是 format 占位；
# JSON 花括号必须写为 {{ }} 转义，避免 .format() 误解析。
_SYSTEM_PROMPT = (
    "你是 CTF 总控规划器。根据题目描述与共享黑板，规划 最多 {max_intents} 个独立、不重叠的行动计划。"
    "只做规划，不执行。输出严格 JSON："
    '{{"intents": [{{"action_type": "...", "target": "...", "reasoning": "简短中文理由"}}]}}\n'
    "action_type 建议取: " + ", ".join(_ACTION_HINTS) + "。target 可为空字符串。"
)


class Planner:
    """Intent 生成器。llm 可为 None（纯规则）。"""

    def __init__(
        self,
        llm: Any = None,
        store: Any = None,
        bus: Any = None,
        max_intents: int = 4,
    ) -> None:
        # store/bus 由外部注入（黑板与事件总线），无共同 Protocol，故用 Any
        self.llm = llm
        self.store = store
        self.bus = bus
        self.max_intents = max_intents

    async def plan(
        self,
        problem_id: str,
        mode: str,
        *,
        challenge: Optional[dict] = None,
        stats: Optional[dict] = None,
    ) -> list[dict[str, Any]]:
        """生成 Intent 写入黑板，返回创建的 Intent 列表。"""
        intents: list[dict[str, Any]] = []
        if self.llm is not None and mode in ("orchestrated", "hybrid"):
            try:
                intents = await self._llm_plan(problem_id, challenge, stats)
            except LLMError as e:
                logger.warning("planner LLM failed for %s: %s", problem_id, e)
                intents = self._fallback_intents(problem_id, challenge)
        else:
            intents = self._fallback_intents(problem_id, challenge)

        created: list[dict[str, Any]] = []
        for it in intents[: self.max_intents]:
            action_type = str(it.get("action_type", "research"))[:40]
            target = str(it.get("target", "") or "")[:200]
            reasoning = str(it.get("reasoning", "") or "")[:300]
            intent_id = self.store.create_intent(
                problem_id, action_type, target=target or None, reasoning=reasoning
            )
            await self.bus.publish(
                ev(EventType.INTENT_PROPOSED, problem_id=problem_id,
                   intent_id=intent_id, action_type=action_type, target=target,
                   reasoning=reasoning, source="planner")
            )
            created.append({"id": intent_id, "action_type": action_type, "target": target, "reasoning": reasoning})
        if created:
            logger.info("planner: %d intent(s) for %s (mode=%s)", len(created), problem_id, mode)
        return created

    # ------------------------------------------------------------------ LLM
    async def _llm_plan(
        self,
        problem_id: str,
        challenge: Optional[dict],
        stats: Optional[dict],
    ) -> list[dict[str, Any]]:
        context = self.store.get_context(problem_id)
        user = _build_prompt(problem_id, challenge, stats, context)
        system = _SYSTEM_PROMPT.format(max_intents=self.max_intents)
        data = await self.llm.chat_json(system, user, temperature=0.3)
        raw = data.get("intents", [])
        if not isinstance(raw, list):
            return []
        return [i for i in raw if isinstance(i, dict)]

    # ------------------------------------------------------------------ 规则
    def _fallback_intents(
        self, problem_id: str, challenge: Optional[dict]
    ) -> list[dict[str, Any]]:
        """无 LLM 时的规则兜底：按附件/靶机/通用研究生成 Intent。"""
        ch = challenge or {}
        files = [f.get("name", "?") for f in (ch.get("files") or [])]
        target = (ch.get("connection_info") or "").strip()
        intents: list[dict[str, Any]] = []
        if files:
            intents.append({
                "action_type": "analyze_attachment",
                "target": files[0],
                "reasoning": "分析附件内容与格式，提取关键线索",
            })
        if target:
            intents.append({
                "action_type": "scan_target",
                "target": target,
                "reasoning": "对靶机做端口/服务探测，寻找攻击面",
            })
        intents.append({
            "action_type": "research",
            "target": "",
            "reasoning": "根据题目描述检索已知解法与关键词",
        })
        return intents


def _build_prompt(
    problem_id: str,
    challenge: Optional[dict],
    stats: Optional[dict],
    context: str,
) -> str:
    lines = [f"题目: {problem_id}"]
    if challenge:
        lines.append(f"分类: {challenge.get('category', '?')}  分值: {challenge.get('value', 0)}")
        if challenge.get("description"):
            lines.append(f"描述: {challenge['description'][:500]}")
        if challenge.get("connection_info"):
            lines.append(f"靶机: {challenge['connection_info']}")
        files = challenge.get("files") or []
        if files:
            lines.append(f"附件: {', '.join(f.get('name', '?') for f in files)}")
    if stats:
        lines.append(f"统计: 死路 {stats.get('deadends', 0)} 条, 发现 {stats.get('discoveries', 0)} 条, "
                      f"活跃计划 {stats.get('active_intents', 0)} 个")
    lines.append("")
    lines.append("共享黑板:")
    lines.append(context)
    return "\n".join(lines)
