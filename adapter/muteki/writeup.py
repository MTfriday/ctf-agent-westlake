"""解题报告（writeup）生成 — 把一道题的求解过程整理成可读 Markdown 报告。

数据来源：
  - 题目信息：platform.fetch_all_challenges()（名称/分类/分值/描述/靶机/附件）
  - run 结果：RunEntry（flag / status / started / finished）
  - 黑板事实：BlackboardStore.list_facts(run_id)（discovery / deadend / partial）
  - 求解过程：logs/trace-{challenge}-{model}-{ts}.jsonl（每步工具调用/输出/模型推理）

生成原则（2026-08-18 修订）：
  1) **优先选用真正解出题的 trace**（含 flag_confirmed / finish:flag_found）
  2) 多模型并行时，不以「最新文件」或失败路径为主线
  3) 过程要压缩成「入口 → 漏洞 → exploit → flag」，禁止全文 dump 工具日志
  4) 黑板只保留与最终链相关的 discovery，丢掉重复失败 grep
  5) 有 BAILIAN_API_KEY → LLM 润色；否则规则拼接
"""

from __future__ import annotations

import glob
import json
import logging
import os
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)

_TRACE_GLOB = "trace-*.jsonl"
_MAX_TRACE_STEPS = 80
_MAX_TRACE_CHARS = 14000
_REPORT_MODEL = "qwen3.7-plus"

# 用于判断「有信息量」的关键词（中英混合）
_KEY_HINTS = (
    "flag", "dasctf", "ctf{", "www.zip", "unserialize", "serialize", "payload",
    "pop", "__wakeup", "__destruct", "__call", "__invoke", "__tostring",
    "cookie", "passwd", "rce", "shell", "exploit", "o:1:", "o:2:", "o:3:",
    "passthru", "system(", "eval(", "base64", "attachment", "distfiles",
    "checksec", "got", "libc", "uaf", "overflow", "rop", "gadget",
    "sql", "union", "xss", "ssti", "lfi", "rfi", "ssrf", "jwt",
    "correct", "accepted", "flag_found", "flag_confirmed",
)


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


def _model_from_trace_path(path: str) -> str:
    """trace-{chal}-{model}-{YYYYMMDD-HHMMSS}.jsonl → model 段（尽力解析）。"""
    base = os.path.basename(path)
    # 去掉前缀 trace- 与后缀 .jsonl
    if base.startswith("trace-") and base.endswith(".jsonl"):
        body = base[len("trace-"):-len(".jsonl")]
    else:
        body = base
    # 末尾是时间戳 YYYYMMDD-HHMMSS
    m = re.search(r"^(?P<head>.+)-(?P<ts>\d{8}-\d{6})$", body)
    head = m.group("head") if m else body
    # head = {sanitized_challenge}-{model}；model 通常不含中文，取最后一段常见 provider 名
    # 更稳：已知模型片段
    for token in (
        "deepseek-v4-flash", "deepseek-v4-pro", "qwen3.7-plus", "qwen3.7-flash",
        "glm-4.6", "glm-4.7", "gpt-4o-mini", "gpt-4o",
    ):
        if token in head:
            return token
    # fallback：最后一个 '-' 后
    if "-" in head:
        return head.rsplit("-", 1)[-1]
    return head


def _trace_flag(events: list[dict[str, Any]]) -> Optional[str]:
    """从 trace 事件中提取已确认 / 结束时的 flag。"""
    flag: Optional[str] = None
    for ev in events:
        t = ev.get("type")
        if t == "flag_confirmed":
            # 有的实现把 flag 放在 args/result
            for k in ("flag", "result"):
                v = ev.get(k)
                if isinstance(v, str) and v.strip():
                    flag = v.strip()
            args = ev.get("args")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = None
            if isinstance(args, dict) and args.get("flag"):
                flag = str(args["flag"]).strip()
        elif t == "finish":
            if ev.get("flag"):
                flag = str(ev.get("flag")).strip()
            if ev.get("status") in ("flag_found", "solved") and ev.get("flag"):
                flag = str(ev.get("flag")).strip()
        elif t == "tool_result" and ev.get("tool") == "submit_flag":
            res = str(ev.get("result") or "")
            if "CORRECT" in res.upper() or "accepted" in res.lower():
                # 回溯同 step 的 call 不太方便；从全文再扫
                pass
    # 再扫一遍 submit_flag 调用参数
    if not flag:
        for ev in events:
            if ev.get("type") == "tool_call" and ev.get("tool") == "submit_flag":
                args = ev.get("args")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        continue
                if isinstance(args, dict) and args.get("flag"):
                    # 仅当后续有 CORRECT 才算
                    flag_cand = str(args["flag"]).strip()
                    # 看后面有没有 correct
                    for ev2 in events:
                        if ev2.get("type") == "tool_result" and ev2.get("tool") == "submit_flag":
                            if "CORRECT" in str(ev2.get("result") or "").upper():
                                return flag_cand
                    # finish confirmed 也会覆盖
                    flag = flag_cand
    return flag or None


def _trace_solved(events: list[dict[str, Any]]) -> bool:
    for ev in events:
        t = ev.get("type")
        if t == "flag_confirmed":
            return True
        if t == "finish" and (
            ev.get("status") in ("flag_found", "solved")
            or (ev.get("confirmed") and ev.get("flag"))
        ):
            return True
        if t == "tool_result" and ev.get("tool") == "submit_flag":
            if "CORRECT" in str(ev.get("result") or "").upper():
                return True
    return False


def _score_trace(path: str, events: list[dict[str, Any]]) -> tuple[int, dict[str, Any]]:
    """给 trace 打分，分高者作为 writeup 主线。

    优先级：
      已解出 >> 有实质步骤 >> 文件更大/更新
    """
    solved = _trace_solved(events)
    flag = _trace_flag(events)
    n_calls = sum(1 for e in events if e.get("type") == "tool_call")
    n_res = sum(1 for e in events if e.get("type") == "tool_result")
    # 空跑（start+stop 几乎无步骤）重罚
    empty = n_calls == 0
    try:
        size = os.path.getsize(path)
        mtime = os.path.getmtime(path)
    except OSError:
        size, mtime = 0, 0

    score = 0
    if solved:
        score += 1_000_000
    if flag:
        score += 100_000
    if empty:
        score -= 500_000
    score += min(n_calls, 200) * 100
    score += min(n_res, 200) * 50
    score += min(size // 1024, 500)  # 每 KB +1，上限 500
    score += int(mtime) % 100_000  # 同质量时偏新

    meta = {
        "path": path,
        "model": _model_from_trace_path(path),
        "solved": solved,
        "flag": flag,
        "n_calls": n_calls,
        "size": size,
        "score": score,
        "empty": empty,
    }
    return score, meta


def _select_primary_trace(
    paths: list[str],
) -> tuple[Optional[str], list[dict[str, Any]], dict[str, Any]]:
    """从多个 trace 中选主线；返回 (path, events, meta)。"""
    if not paths:
        return None, [], {}

    best_path: Optional[str] = None
    best_events: list[dict[str, Any]] = []
    best_meta: dict[str, Any] = {}
    best_score = -10**18
    all_meta: list[dict[str, Any]] = []

    for path in paths:
        events = _parse_trace(path)
        score, meta = _score_trace(path, events)
        all_meta.append(meta)
        if score > best_score:
            best_score = score
            best_path = path
            best_events = events
            best_meta = meta

    best_meta["candidates"] = all_meta
    return best_path, best_events, best_meta


def _args_preview(args: Any) -> str:
    if args is None:
        return ""
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            return args[:300]
    if isinstance(args, dict):
        for k in ("command", "path", "url", "flag", "content", "query"):
            if args.get(k):
                return str(args[k])[:300]
        try:
            return json.dumps(args, ensure_ascii=False)[:300]
        except Exception:
            return str(args)[:300]
    return str(args)[:300]


def _is_noisy_step(tool: str, cmd: str, result: str) -> bool:
    """过滤明显无信息量的刷屏步骤。"""
    c = (cmd or "").lower()
    r = (result or "").lower()
    # 连续无意义 grep 失败
    if tool == "bash" and "grep" in c and ("[exit 1]" in r or r.strip() == "" or "no output" in r):
        # 若命令本身不含关键线索词，视为噪音
        if not any(h in c for h in _KEY_HINTS):
            return True
    # 空 list / 纯 head 工具说明
    if "cat /tools.txt" in c:
        return True
    return False


def _is_key_step(tool: str, cmd: str, result: str) -> bool:
    blob = f"{tool} {cmd} {result}".lower()
    if any(h in blob for h in _KEY_HINTS):
        return True
    if tool in ("submit_flag", "read_file", "write_file"):
        return True
    # 反序列化/下载/解压/读源码
    if any(k in blob for k in (".zip", "unzip", "curl -", "post ", "serialize", "class.php", "index.php")):
        return True
    return False


def _summarize_trace(
    events: list[dict[str, Any]],
    *,
    prefer_key_only: bool = True,
) -> str:
    """把 trace 事件压缩成可读的求解步骤文本。

    prefer_key_only=True 时优先保留关键步骤；若关键步骤过少再回退全量（仍截断）。
    注意：solver 常批量发多个 tool_call 再统一回 tool_result，必须用队列配对。
    """
    from collections import deque

    rows: list[tuple[bool, str]] = []  # (is_key, line)
    pending: deque[tuple[str, str]] = deque()  # (tool, cmd)

    for ev in events:
        t = ev.get("type")
        if t == "start":
            challenge = str(ev.get("challenge") or "")
            model = str(ev.get("model") or "")
            rows.append((True, f"# 求解开始 — {challenge} ({model})"))
        elif t == "tool_call":
            tool = str(ev.get("tool") or "?")
            cmd = _args_preview(ev.get("args"))
            pending.append((tool, cmd))
        elif t == "tool_result":
            if pending:
                tool_c, cmd = pending.popleft()
            else:
                tool_c, cmd = "?", ""
            tool = str(ev.get("tool") or tool_c or "?")
            result = str(ev.get("result") or "")
            if _is_noisy_step(tool, cmd, result):
                continue
            key = _is_key_step(tool, cmd, result)
            preview = result[:500].replace("\n", " ⏎ ")
            line = f"[步骤] ▶ {tool}: {cmd}\n      ↳ {preview}"
            rows.append((key, line))
        elif t == "flag_confirmed":
            rows.append((True, "# ✅ Flag 已确认"))
        elif t == "finish":
            rows.append((
                True,
                f"# 结束: {ev.get('status')} flag={ev.get('flag')} "
                f"confirmed={ev.get('confirmed')} cost=${ev.get('cost_usd')}",
            ))
        elif t == "error":
            err = str(ev.get("error") or "")[:200]
            rows.append((True, f"! 错误: {err}"))
        elif t == "stop":
            rows.append((False, f"# 停止（共 {ev.get('step_count')} 步）"))

    key_lines = [ln for is_key, ln in rows if is_key]
    if prefer_key_only and len(key_lines) >= 6:
        chosen = key_lines
        note = "（已过滤重复/无效试错，仅保留关键步骤）"
    else:
        chosen = [ln for _, ln in rows]
        note = ""

    # 截断
    out: list[str] = []
    if note:
        out.append(f"# {note}")
    n = 0
    for ln in chosen:
        out.append(ln)
        if ln.startswith("[步骤]"):
            n += 1
        if n >= _MAX_TRACE_STEPS:
            out.append("…（步骤过多，已截断）")
            break
    return "\n".join(out)


def _filter_facts(
    facts: list[dict[str, Any]],
    *,
    flag: Optional[str],
    solved: bool,
    limit: int = 15,
) -> list[dict[str, Any]]:
    """过滤黑板事实：解出后优先保留与利用链/flag 相关的 discovery。"""
    if not facts:
        return []

    def score(f: dict[str, Any]) -> int:
        t = str(f.get("type") or "")
        c = str(f.get("content") or "").lower()
        s = 0
        if t == "discovery":
            s += 10
        elif t == "deadend":
            s += 2 if not solved else -20
        elif t == "partial":
            s += 3
        if flag and flag.lower() in c:
            s += 100
        if any(h in c for h in _KEY_HINTS):
            s += 20
        # 惩罚刷屏工具摘要 / 失败试错
        if c.count("[") > 8 and "bash command" in c:
            s -= 15
        if c.startswith("submit_flag|") or "__submits__" in c:
            s -= 50
        # 已解出时：纯失败 grep/扫目录噪音降权
        noisy_fail = any(x in c for x in (
            "grep cookie", "exit 1", "[exit 1]", "no output",
            "404 not found", "fail", "未找到", "cannot",
        ))
        has_signal = (flag and flag.lower() in c) or any(
            h in c for h in (
                "www.zip", "payload", "unserialize", "pop", "__wakeup",
                "__call", "dasctf", "flag{", "o:1:", "passthru", "rce",
            )
        )
        if solved and noisy_fail and not has_signal:
            s -= 40
        if solved and t == "discovery" and not has_signal and len(c) < 80:
            s -= 10
        return s

    ranked = sorted(facts, key=score, reverse=True)
    # 去重（内容前 120 字符）
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for f in ranked:
        key = str(f.get("content") or "")[:120]
        if key in seen:
            continue
        seen.add(key)
        if score(f) < 0 and solved:
            continue
        out.append(f)
        if len(out) >= limit:
            break
    return out


def _extract_payloads(events: list[dict[str, Any]]) -> list[str]:
    """从 trace 中尽量抽出 exploit payload / 关键命令。"""
    found: list[str] = []
    pats = [
        re.compile(r"O:\d+:\"[^\"]+\".{0,400}"),
        re.compile(r"DASCTF\{[^}]+\}", re.I),
        re.compile(r"flag\{[^}]+\}", re.I),
        re.compile(r"curl[^\n]{0,300}(?:data-urlencode|POST)[^\n]{0,200}", re.I),
    ]
    for ev in events:
        blob = ""
        if ev.get("type") == "tool_call":
            blob = _args_preview(ev.get("args"))
        elif ev.get("type") == "tool_result":
            blob = str(ev.get("result") or "")
        for p in pats:
            for m in p.finditer(blob):
                s = m.group(0).strip()
                if s and s not in found:
                    found.append(s[:500])
        if len(found) >= 12:
            break
    return found


async def _collect_material_async(
    runtime: Any,
    run_id: str,
    entry: Any,
) -> dict[str, Any]:
    """异步收集素材（平台题目信息 + 黑板事实 + 主线 trace）。"""
    # 1) 题目信息
    challenge: dict[str, Any] = {}
    try:
        challenges = await runtime.platform.fetch_all_challenges()
        challenge = next((c for c in challenges if c.get("name") == run_id), {}) or {}
        # id 回退
        if not challenge:
            challenge = next(
                (c for c in challenges if str(c.get("id") or "") == str(run_id)),
                {},
            ) or {}
    except Exception as e:  # noqa: BLE001
        logger.warning("writeup: fetch challenge %s failed: %s", run_id, e)

    # 2) 黑板事实
    facts_raw: list[dict[str, Any]] = []
    try:
        store = runtime.store
        for f in store.list_facts(run_id, limit=200):
            facts_raw.append({
                "type": f.get("type"),
                "content": f.get("content"),
                "source": f.get("source"),
            })
    except Exception as e:  # noqa: BLE001
        logger.warning("writeup: list_facts %s failed: %s", run_id, e)

    # 3) 选择主线 trace（优先已解出）
    paths = _find_trace_files(run_id)
    primary_path, events, tmeta = _select_primary_trace(paths)

    solved = bool(getattr(entry, "solved", False)) or bool(tmeta.get("solved"))
    flag = getattr(entry, "flag", None) or tmeta.get("flag")
    if isinstance(flag, str):
        flag = flag.strip() or None

    if events:
        trace_text = _summarize_trace(events, prefer_key_only=True)
        payloads = _extract_payloads(events)
    else:
        trace_text = "（无 trace 记录——该题可能为平台历史已解出或未实际求解）"
        payloads = []

    facts = _filter_facts(facts_raw, flag=flag, solved=solved, limit=15)

    # 其它候选模型摘要（避免完全丢失并行信息，但不当主线）
    others: list[str] = []
    for m in tmeta.get("candidates") or []:
        if m.get("path") == primary_path:
            continue
        tag = "已解出" if m.get("solved") else ("空跑" if m.get("empty") else f"{m.get('n_calls', 0)}步")
        others.append(f"- {m.get('model')}: {tag}, {m.get('size', 0)}B")

    return {
        "challenge": challenge,
        "run": {
            "run_id": entry.run_id,
            "name": entry.name or entry.run_id,
            "category": entry.category,
            "value": entry.value,
            "status": entry.status,
            "solved": solved or bool(entry.solved),
            "flag": flag or entry.flag,
            "prompt": entry.prompt,
        },
        "facts": facts,
        "trace": trace_text,
        "payloads": payloads,
        "trace_meta": {
            "primary_model": tmeta.get("model"),
            "primary_path": primary_path,
            "primary_solved": bool(tmeta.get("solved")),
            "others": others,
        },
    }


def _build_markdown(material: dict[str, Any]) -> str:
    """规则兜底：把素材整理成结构化 Markdown 报告（利用链导向）。"""
    run = material["run"]
    ch = material["challenge"]
    tmeta = material.get("trace_meta") or {}
    lines: list[str] = []

    lines.append(f"# {run['name']} — 解题报告")
    lines.append("")
    lines.append("> 自动生成 · Aemeath CTF Agent（规则模板）")
    if tmeta.get("primary_model"):
        lines.append(f"> 主线 trace 模型：`{tmeta['primary_model']}`"
                     f"{' · 已解出' if tmeta.get('primary_solved') else ''}")
    lines.append("")

    # 题目信息
    lines.append("## 题目信息")
    lines.append("")
    lines.append(f"- **分类**: {run['category'] or ch.get('category') or '?'}")
    lines.append(f"- **分值**: {run['value'] or ch.get('value') or '?'}")
    desc = ch.get("description") or run.get("prompt") or ""
    if desc:
        lines.append(f"- **描述**: {str(desc)[:500]}")
    if ch.get("connection_info"):
        lines.append(f"- **靶机**: `{ch.get('connection_info')}`")
    files = ch.get("files") or []
    if files:
        names = ", ".join(str(f.get("name")) for f in files if isinstance(f, dict))
        if names:
            lines.append(f"- **附件**: {names}")
    lines.append("")

    # 结果
    lines.append("## 求解结果")
    lines.append("")
    lines.append(f"- **状态**: {run['status']} ({'已解出' if run['solved'] else '未解出'})")
    if run.get("flag"):
        lines.append(f"- **Flag**: `{run['flag']}`")
    else:
        lines.append("- **Flag**: 未找到")
    lines.append("")

    # 关键 payload / 命令
    payloads = material.get("payloads") or []
    if payloads:
        lines.append("## 关键 Payload / 命令")
        lines.append("")
        for p in payloads[:8]:
            lines.append("```")
            lines.append(p)
            lines.append("```")
            lines.append("")

    # 黑板事实（已过滤）
    facts = material.get("facts") or []
    if facts:
        lines.append("## 关键发现")
        lines.append("")
        for f in facts:
            tag = {
                "discovery": "发现",
                "deadend": "死路",
                "partial": "候选",
            }.get(f.get("type"), f.get("type") or "事实")
            src = f"（{f['source']}）" if f.get("source") else ""
            content = str(f.get("content") or "")
            # 截断超长工具摘要
            if len(content) > 400:
                content = content[:400] + "…"
            lines.append(f"- **[{tag}]** {content} {src}")
        lines.append("")

    # 其它模型（并行）
    others = tmeta.get("others") or []
    if others:
        lines.append("## 并行模型")
        lines.append("")
        lines.append("以下模型同时参与，但**不是**本报告主线：")
        lines.extend(others)
        lines.append("")

    # 求解过程（压缩后的主线）
    lines.append("## 求解过程（主线）")
    lines.append("")
    trace = str(material.get("trace") or "").strip()
    if trace and not trace.startswith("（无 trace"):
        lines.append("```")
        lines.append(trace)
        lines.append("```")
    else:
        lines.append(trace or "（无过程记录）")
    lines.append("")

    if run.get("flag"):
        lines.append("## Flag")
        lines.append("")
        lines.append(f"`{run['flag']}`")
        lines.append("")

    return "\n".join(lines)


_SYSTEM_PROMPT = (
    "你是一名 CTF 解题报告（writeup）撰写专家。\n"
    "你会拿到：题目信息、【主线】求解 trace（已尽量过滤无效试错）、"
    "关键 payload、过滤后的黑板发现、以及其它并行模型的旁注。\n"
    "\n"
    "硬性要求：\n"
    "1. 用中文撰写，直接输出 Markdown 正文（不要用大代码块包整篇）。\n"
    "2. 必须按「利用链」叙事，推荐结构：\n"
    "   `# 题目名 — 解题报告`\n"
    "   `## 题目信息`\n"
    "   `## 解题思路`（入口怎么发现、漏洞是什么、为什么能打）\n"
    "   `## 关键步骤`（只保留通向 flag 的命令/操作，给可复现命令）\n"
    "   `## 攻击链总览`（可用简图/列表）\n"
    "   `## Flag`\n"
    "3. **禁止**把失败模型的无效 grep/重复试错当成正文主线。\n"
    "4. 若素材含 flag_confirmed / CORRECT / 最终 payload，必须写清 "
    "   如何构造 payload、如何触发、flag 如何得到。\n"
    "5. 若未解出：说明卡点、已确认事实、建议下一步；不要编造 flag。\n"
    "6. 源码/类定义/payload 用 fenced code block；命令可复现。\n"
    "7. 文风简洁，像正式比赛 writeup，不要日志倾倒。\n"
)


async def _llm_writeup(llm: Any, material: dict[str, Any]) -> Optional[str]:
    """用 LLM 生成润色报告；失败返回 None。"""
    run = material["run"]
    ch = material["challenge"]
    facts = material.get("facts") or []
    trace = str(material.get("trace") or "")
    tmeta = material.get("trace_meta") or {}
    payloads = material.get("payloads") or []

    user: list[str] = [
        f"题目: {run['name']}",
        f"分类: {run['category'] or ch.get('category') or '?'}  "
        f"分值: {run['value'] or ch.get('value') or '?'}",
        f"状态: {run['status']}  solved={run['solved']}  flag={run.get('flag') or '无'}",
    ]
    if tmeta.get("primary_model"):
        user.append(
            f"主线模型: {tmeta.get('primary_model')} "
            f"(solved={tmeta.get('primary_solved')})"
        )
    if ch.get("description"):
        user.append(f"描述: {str(ch['description'])[:600]}")
    if run.get("prompt") and run.get("prompt") != ch.get("description"):
        user.append(f"提示: {str(run['prompt'])[:400]}")
    if ch.get("connection_info"):
        user.append(f"靶机: {ch.get('connection_info')}")
    files = ch.get("files") or []
    if files:
        names = ", ".join(str(f.get("name")) for f in files if isinstance(f, dict))
        if names:
            user.append(f"附件: {names}")

    if payloads:
        user.append("")
        user.append("从主线 trace 抽出的关键 payload/命令（务必优先采用）：")
        for p in payloads[:8]:
            user.append(f"- {p}")

    if facts:
        user.append("")
        user.append("过滤后的关键发现（不要展开成工具流水账）：")
        for f in facts[:15]:
            content = str(f.get("content") or "")
            if len(content) > 280:
                content = content[:280] + "…"
            user.append(f"- [{f.get('type')}] {content}")

    others = tmeta.get("others") or []
    if others:
        user.append("")
        user.append("其它并行模型（仅供参考，非主线）：")
        user.extend(others)

    user.append("")
    user.append("主线求解过程（已压缩）：")
    user.append(trace[:_MAX_TRACE_CHARS])

    try:
        return await llm.chat(
            _SYSTEM_PROMPT, "\n".join(user), temperature=0.25, max_tokens=5000
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("writeup LLM failed: %s", e)
        return None


async def generate_writeup(runtime: Any, run_id: str, entry: Any) -> dict[str, Any]:
    """生成解题报告。返回 {ok, markdown, source, trace_model?}。

    source ∈ "llm" | "rule"（LLM 成功 / 规则兜底）。
    """
    material = await _collect_material_async(runtime, run_id, entry)
    tmeta = material.get("trace_meta") or {}
    logger.info(
        "writeup %s: primary_model=%s solved=%s path=%s",
        run_id,
        tmeta.get("primary_model"),
        tmeta.get("primary_solved"),
        tmeta.get("primary_path"),
    )

    # 尝试 LLM 润色
    from src.orchestrator.llm import llm_from_settings

    llm = llm_from_settings(runtime.settings, _REPORT_MODEL)
    markdown: Optional[str] = None
    if llm is not None:
        markdown = await _llm_writeup(llm, material)

    if markdown:
        return {
            "ok": True,
            "markdown": markdown,
            "source": "llm",
            "trace_model": tmeta.get("primary_model"),
        }

    return {
        "ok": True,
        "markdown": _build_markdown(material),
        "source": "rule",
        "trace_model": tmeta.get("primary_model"),
    }
