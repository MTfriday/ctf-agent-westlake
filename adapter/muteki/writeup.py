"""解题报告（writeup）生成 — 把一道题的求解过程整理成可读 Markdown 报告。

数据来源：
  - 题目信息：platform.fetch_all_challenges()（名称/分类/分值/描述/靶机/附件）
  - run 结果：RunEntry（flag / status / started / finished）
  - 黑板事实：BlackboardStore.list_facts(run_id)（discovery / deadend / partial）
  - 求解过程：logs/trace-{challenge}-{model}-{ts}.jsonl（每步工具调用/输出/模型推理）

生成方式：
  1) 有 BAILIAN_API_KEY → 用百炼 qwen 把素材润色成结构化报告
  2) 无 key / LLM 失败 → 规则拼接（完整还原求解步骤）
"""

from __future__ import annotations

import glob
import json
import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)

# trace 文件命名：trace-{sanitized challenge}-{model}-{ts}.jsonl
_TRACE_GLOB = "trace-*.jsonl"
_MAX_TRACE_STEPS = 120          # 单个 trace 最多纳入的步骤数（防报告过长）
_MAX_TRACE_CHARS = 16000        # 喂给 LLM 的素材最大字符数
_REPORT_MODEL = "qwen3.7-plus"  # 报告润色模型（能力/成本均衡，1M 上下文）


def _sanitize(name: str) -> str:
    """与 backend/tracing.py 的 _sanitize 保持一致，用于匹配 trace 文件名。"""
    out = []
    for ch in name:
        if ch in '<>:"/\\|?*' or ord(ch) < 32:
            out.append("_")
        elif ch in " \t":
            out.append("_")
        else:
            out.append(ch)
    return "".join(out)


def _find_trace_files(run_id: str, logs_dir: str = "logs") -> list[str]:
    """按 run_id（= challenge name）匹配 logs 下的 trace 文件。"""
    prefix = _sanitize(run_id)
    pattern = os.path.join(logs_dir, f"trace-{prefix}-*.jsonl")
    return sorted(glob.glob(pattern))


def _parse_trace(path: str) -> list[dict[str, Any]]:
    """读取并解析一个 trace jsonl 文件 → 事件列表。"""
    events: list[dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError as e:
        logger.warning("read trace %s failed: %s", path, e)
    return events


def _summarize_trace(events: list[dict[str, Any]]) -> str:
    """把 trace 事件压缩成可读的求解步骤文本。"""
    lines: list[str] = []
    step = 0
    for ev in events:
        t = ev.get("type")
        if t == "start":
            lines.append(f"# 求解开始 — {ev.get('challenge')} ({ev.get('model')})")
        elif t == "tool_call":
            step += 1
            args = ev.get("args") or ""
            try:
                a = json.loads(args) if isinstance(args, str) else args
                cmd = a.get("command") or a.get("path") or a.get("url") or json.dumps(a)
            except json.JSONDecodeError:
                cmd = args
            lines.append(f"[步骤{step}] ▶ {ev.get('tool')}: {str(cmd)[:300]}")
        elif t == "tool_result":
            result = str(ev.get("result") or "")
            # 只保留有信息量的输出首段（去掉超长截断）
            preview = result[:600].replace("\n", " ⏎ ")
            lines.append(f"      ↳ {preview}")
        elif t == "model_response":
            txt = str(ev.get("text") or "").strip()
            if txt:
                lines.append(f"      💬 {txt[:200].replace(chr(10), ' ')}")
        elif t == "findings_injected":
            lines.append(f"      ⓘ 注入队友发现 (step {ev.get('step')})")
        elif t == "finish":
            lines.append(f"# 结束: {ev.get('status')} flag={ev.get('flag')} cost=${ev.get('cost_usd')}")
        elif t == "error":
            lines.append(f"! 错误: {str(ev.get('error'))[:200]}")
        elif t == "stop":
            lines.append(f"# 停止（共 {ev.get('step_count')} 步）")
        if step >= _MAX_TRACE_STEPS:
            lines.append("…（步骤过多，已截断）")
            break
    return "\n".join(lines)


async def _collect_material_async(
    runtime: Any,
    run_id: str,
    entry: Any,
) -> dict[str, Any]:
    """异步收集素材（平台题目信息 + 黑板事实 + trace）。"""
    # 1) 题目信息
    challenge: dict[str, Any] = {}
    try:
        challenges = await runtime.platform.fetch_all_challenges()
        challenge = next((c for c in challenges if c.get("name") == run_id), {})
    except Exception as e:  # noqa: BLE001
        logger.warning("writeup: fetch challenge %s failed: %s", run_id, e)

    # 2) 黑板事实
    facts: list[dict[str, Any]] = []
    try:
        store = runtime.store
        for f in store.list_facts(run_id, limit=100):
            facts.append({
                "type": f.get("type"),
                "content": f.get("content"),
                "source": f.get("source"),
            })
    except Exception as e:  # noqa: BLE001
        logger.warning("writeup: list_facts %s failed: %s", run_id, e)

    # 3) trace 求解过程（取最近的一个文件）
    trace_text = ""
    traces = _find_trace_files(run_id)
    if traces:
        events = _parse_trace(traces[-1])
        trace_text = _summarize_trace(events)
    else:
        trace_text = "（无 trace 记录——该题可能为平台历史已解出或未实际求解）"

    return {
        "challenge": challenge,
        "run": {
            "run_id": entry.run_id,
            "name": entry.name or entry.run_id,
            "category": entry.category,
            "value": entry.value,
            "status": entry.status,
            "solved": entry.solved,
            "flag": entry.flag,
            "prompt": entry.prompt,
        },
        "facts": facts,
        "trace": trace_text,
    }


def _build_markdown(material: dict[str, Any]) -> str:
    """规则兜底：把素材整理成结构化 Markdown 报告。"""
    run = material["run"]
    ch = material["challenge"]
    lines: list[str] = []

    lines.append(f"# {run['name']} — 解题报告")
    lines.append("")
    lines.append("> 自动生成 · Aemeath CTF Agent")
    lines.append("")

    # 题目信息
    lines.append("## 题目信息")
    lines.append("")
    lines.append(f"- **分类**: {run['category'] or ch.get('category') or '?'}")
    lines.append(f"- **分值**: {run['value'] or ch.get('value') or '?'}")
    if ch.get("description"):
        lines.append(f"- **描述**: {str(ch['description'])[:500]}")
    if ch.get("connection_info"):
        lines.append(f"- **靶机**: {ch.get('connection_info')}")
    files = ch.get("files") or []
    if files:
        names = ", ".join(str(f.get("name")) for f in files)
        lines.append(f"- **附件**: {names}")
    lines.append("")

    # 结果
    lines.append("## 求解结果")
    lines.append("")
    lines.append(f"- **状态**: {run['status']} ({'已解出' if run['solved'] else '未解出'})")
    if run["flag"]:
        lines.append(f"- **Flag**: `{run['flag']}`")
    else:
        lines.append("- **Flag**: 未找到")
    if run["prompt"]:
        lines.append(f"- **初始提示**: {str(run['prompt'])[:300]}")
    lines.append("")

    # 黑板事实
    facts = material.get("facts") or []
    if facts:
        lines.append("## 关键发现")
        lines.append("")
        for f in facts[-20:]:
            tag = {
                "discovery": "发现",
                "deadend": "死路",
                "partial": "候选",
            }.get(f.get("type"), f.get("type") or "事实")
            src = f"（{f['source']}）" if f.get("source") else ""
            lines.append(f"- **[{tag}]** {f.get('content')} {src}")
        lines.append("")

    # 求解过程
    lines.append("## 求解过程")
    lines.append("")
    trace = str(material.get("trace") or "").strip()
    if trace and not trace.startswith("（无 trace"):
        lines.append("```")
        lines.append(trace)
        lines.append("```")
    else:
        lines.append(trace or "（无过程记录）")
    lines.append("")

    return "\n".join(lines)


_SYSTEM_PROMPT = (
    "你是一名 CTF 解题报告撰写专家。根据给定的题目信息、求解过程与黑板发现，"
    "撰写一份结构化、可读的 Markdown 解题报告（writeup）。要求：\n"
    "1. 用中文撰写；\n"
    "2. 结构：`# 题目名 — 解题报告`、`## 题目信息`、`## 解题思路`、"
    "`## 关键步骤`（提炼核心命令/操作，不要逐字复述全部工具调用）、"
    "`## Flag`；\n"
    "3. 提炼求解过程中的关键思路与命令，略去冗余的试错；\n"
    "4. 若未解出，如实说明卡点与已尝试方向；\n"
    "5. 直接输出 Markdown 正文，不要用代码块包裹整篇。"
)


async def _llm_writeup(llm: Any, material: dict[str, Any]) -> Optional[str]:
    """用 LLM 生成润色报告；失败返回 None。"""
    run = material["run"]
    ch = material["challenge"]
    facts = material.get("facts") or []
    trace = str(material.get("trace") or "")

    # 拼 user 素材（限制长度）
    user = [
        f"题目: {run['name']}",
        f"分类: {run['category'] or ch.get('category') or '?'} 分值: {run['value'] or ch.get('value') or '?'}",
    ]
    if ch.get("description"):
        user.append(f"描述: {str(ch['description'])[:600]}")
    if ch.get("connection_info"):
        user.append(f"靶机: {ch.get('connection_info')}")
    if facts:
        user.append("")
        user.append("关键发现:")
        for f in facts[-20:]:
            user.append(f"- [{f.get('type')}] {f.get('content')}")
    user.append("")
    user.append("求解过程:")
    user.append(trace[:_MAX_TRACE_CHARS])

    try:
        return await llm.chat(
            _SYSTEM_PROMPT, "\n".join(user), temperature=0.3, max_tokens=4000
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("writeup LLM failed: %s", e)
        return None


async def generate_writeup(runtime: Any, run_id: str, entry: Any) -> dict[str, Any]:
    """生成解题报告。返回 {ok, markdown, source}。

    source ∈ "llm" | "rule"（LLM 成功 / 规则兜底）。
    """
    material = await _collect_material_async(runtime, run_id, entry)

    # 尝试 LLM 润色
    from src.orchestrator.llm import llm_from_settings

    llm = llm_from_settings(runtime.settings, _REPORT_MODEL)
    markdown: Optional[str] = None
    if llm is not None:
        markdown = await _llm_writeup(llm, material)

    if markdown:
        return {"ok": True, "markdown": markdown, "source": "llm"}

    return {"ok": True, "markdown": _build_markdown(material), "source": "rule"}
