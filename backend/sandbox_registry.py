"""Sandbox transparency registry — per-challenge container views + op logs.

Solver containers register here on creation (backend/agents/solver.py); the
muteki routes read them to render the transparent-sandbox panel (every command
run in the container, its output, and the container file tree) without reaching
into solver internals. All live in the adapter process (swarm runs in-process).
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Optional

_OPS_MAX = 800  # per-container op log


@dataclass
class SandboxOp:
    ts: float
    model_spec: str
    command: str
    exit_code: int
    stdout_head: str = ""
    stderr_head: str = ""
    duration_s: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "ts": round(self.ts, 3),
            "model_spec": self.model_spec,
            "command": self.command,
            "exit_code": self.exit_code,
            "stdout_head": self.stdout_head,
            "stderr_head": self.stderr_head,
            "duration_s": round(self.duration_s, 3),
        }


@dataclass
class SandboxView:
    container_id: str
    image: str
    workspace_dir: str
    model_spec: str
    started_ts: float = field(default_factory=time.time)
    ops: deque = field(default_factory=lambda: deque(maxlen=_OPS_MAX))
    # 隐藏引用：供 tree 查询复用容器执行（不参与序列化）
    _sandbox: Any = field(default=None, repr=False)

    def as_dict(self) -> dict[str, Any]:
        return {
            "container_id": self.container_id,
            "image": self.image,
            "workspace_dir": self.workspace_dir,
            "model_spec": self.model_spec,
            "started_ts": round(self.started_ts, 3),
            "ops_count": len(self.ops),
        }


# problem_id（challenge name）→ sandboxes
_REGISTRY: dict[str, list[SandboxView]] = {}


def register(
    challenge_name: str,
    container_id: str,
    image: str,
    workspace_dir: str,
    model_spec: str,
    *,
    sandbox: Any = None,
) -> Optional[SandboxView]:
    """Register a live container under its challenge name."""
    if not container_id:
        return None
    view = SandboxView(
        container_id=container_id,
        image=image,
        workspace_dir=workspace_dir,
        model_spec=model_spec,
        _sandbox=sandbox,
    )
    _REGISTRY.setdefault(challenge_name, []).append(view)
    return view


def unregister(container_id: str) -> None:
    for name in list(_REGISTRY.keys()):
        views = _REGISTRY[name]
        before = len(views)
        _REGISTRY[name] = [v for v in views if v.container_id != container_id]
        if not _REGISTRY[name]:
            _REGISTRY.pop(name, None)
        elif len(_REGISTRY[name]) != before:
            pass  # 部分移除（同名题多容器）


def find_view(container_id: str) -> Optional[SandboxView]:
    for views in _REGISTRY.values():
        for v in views:
            if v.container_id == container_id:
                return v
    return None


def sandboxes(problem_id: str) -> list[SandboxView]:
    return list(_REGISTRY.get(problem_id, []))


def containers(problem_id: str) -> list[dict[str, Any]]:
    return [v.as_dict() for v in _REGISTRY.get(problem_id, [])]


def ops(problem_id: str) -> list[dict[str, Any]]:
    """All container ops for a challenge, flattened + time-sorted."""
    out: list[dict[str, Any]] = []
    for v in _REGISTRY.get(problem_id, []):
        for op in v.ops:
            d = op.as_dict()
            d["container_id"] = v.container_id
            out.append(d)
    out.sort(key=lambda d: d["ts"])
    return out


def append_op(container_id: str, op: SandboxOp) -> None:
    v = find_view(container_id)
    if v is not None:
        v.ops.append(op)


async def tree_rows(problem_id: str, maxdepth: int = 5, timeout_s: int = 45) -> list[dict[str, Any]]:
    """Container file-tree rows via `find` in the first live sandbox.

    Returns flattened rows [{type, size, path}] — the frontend renders the tree.
    Filters system dirs so the view focuses on challenge-relevant content.
    """
    for v in _REGISTRY.get(problem_id, []):
        sb = getattr(v, "_sandbox", None)
        if sb is None or getattr(sb, "_container", None) is None:
            continue
        cmd = (
            f"find / -maxdepth {maxdepth} "
            "-not -path '/proc*' -not -path '/sys*' -not -path '/dev*' "
            "-not -path '/var/*' -not -path '/etc/*' -not -path '/usr/*' "
            "-not -path '/opt/*' -not -path '/bin/*' -not -path '/sbin/*' "
            "-not -path '/lib*' -not -path '/root/.cache*' "
            "-printf '%y\\t%s\\t%p\\n' | head -3000"
        )
        try:
            r = await sb.exec(cmd, timeout_s=timeout_s)
        except Exception:  # noqa: BLE001
            continue
        rows: list[dict[str, Any]] = []
        for line in (r.stdout or "").splitlines():
            parts = line.split("\t", 2)
            if len(parts) != 3:
                continue
            rows.append({
                "type": "d" if parts[0] == "d" else "f",
                "size": int(parts[1] or 0),
                "path": parts[2],
            })
        if rows:
            return rows
    return []
