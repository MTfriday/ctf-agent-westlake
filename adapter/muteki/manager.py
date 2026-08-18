"""muteki 契约 — RunManager。

把 Aemeath 执行层（SolverRuntime）包装成 muteki 前端的 run 模型：
  - 预注册平台 challenge 为 runs（rail 展示题目列表，run_id = challenge name）
  - run 元数据（pin/archive/rename/folder/order）持久化到 data/muteki_meta.json
  - start() 把 muteki 启动体翻译为 Aemeath launch_solver
  - 订阅 Aemeath EventBus，用每 run 的 EventBridge 翻译为 muteki 事件，
    写入该 run 的环形历史 + 实时队列（SSE /events 消费，支持 Last-Event-ID）
  - hitl() 把 operator 指令写入黑板 / 控制引擎
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from adapter.muteki.bridge import (
    GUIDANCE,
    EventBridge,
    MutekiEvent,
    RUN_FINISHED,
    RUN_STARTED,
)

logger = logging.getLogger(__name__)

# 单 run 环形历史容量（Last-Event-ID 续传窗口）
_HISTORY_LEN = 600


def _notice_fingerprint(notices: list[dict[str, Any]]) -> str:
    """公告列表指纹：id + 标题/时间等关键字段，用于判断是否有更新。"""
    items: list[dict[str, Any]] = []
    for n in notices or []:
        if not isinstance(n, dict):
            continue
        items.append({
            "id": n.get("id") or n.get("noticeId") or n.get("notice_id"),
            "title": n.get("title") or n.get("name") or n.get("subject") or "",
            "time": n.get("createTime") or n.get("created_at") or n.get("publishTime")
            or n.get("updateTime") or n.get("updated_at") or n.get("time") or "",
            "content": (n.get("content") or n.get("summary") or "")[:200],
        })
    items.sort(key=lambda x: str(x.get("id") or ""))
    raw = json.dumps(items, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass
class RunEntry:
    """一个 muteki run（= 一次 Aemeath 求解）。"""

    run_id: str
    name: str = ""
    category: str = ""
    started: bool = False
    finished: bool = False
    solved: bool = False
    paused: bool = False
    status: str = "draft"  # draft|running|paused|solved|finished|failed
    flag: Optional[str] = None
    error: Optional[str] = None
    pinned: bool = False
    pinned_at: Optional[float] = None
    archived: bool = False
    folder_id: Optional[str] = None
    order: int = 0
    updated: float = field(default_factory=time.time)
    value: int = 0
    # 内部：Aemeath 侧信息
    engine_run_id: Optional[str] = None
    prompt: str = ""
    # 每 run 的桥 + SSE 缓冲
    bridge: Optional[EventBridge] = None
    history: deque = field(default_factory=lambda: deque(maxlen=_HISTORY_LEN))
    subs: list = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "name": self.name or self.run_id,
            "category": self.category,
            "value": self.value,
            "started": self.started,
            "finished": self.finished,
            "solved": self.solved,
            "paused": self.paused,
            "status": self.status,
            "flag": self.flag,
            "pinned": self.pinned,
            "pinned_at": self.pinned_at,
            "archived": self.archived,
            "folder_id": self.folder_id,
            "order": self.order,
            "updated": self.updated,
            "updated_at": self.updated,
        }

    def meta(self) -> dict[str, Any]:
        """仅元数据子集（持久化）。"""
        return {
            "name": self.name, "category": self.category, "value": self.value,
            "solved": self.solved, "status": self.status,
            "prompt": self.prompt,
            "flag": self.flag, "error": self.error,
            "pinned": self.pinned, "pinned_at": self.pinned_at,
            "archived": self.archived, "folder_id": self.folder_id, "order": self.order,
        }

    def apply_meta(self, meta: dict[str, Any]) -> None:
        for k in ("name", "category", "value", "solved", "status", "prompt",
                  "flag", "error",
                  "pinned", "pinned_at", "archived", "folder_id", "order"):
            if k in meta:
                setattr(self, k, meta[k])


@dataclass
class Folder:
    id: str
    name: str
    order: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "order": self.order}


class RunManager:
    """聚合 run 注册表、元数据持久化、Aemeath 桥接与事件分发。"""

    def __init__(self, runtime: Any, meta_path: Optional[str] = None) -> None:
        self.runtime = runtime
        self._meta_path = Path(meta_path) if meta_path else Path("data/muteki_meta.json")
        self._runs: dict[str, RunEntry] = {}
        self._folders: dict[str, Folder] = {}
        self._order_counter = 0
        self._seeded = False
        self._sync_task: Optional[asyncio.Task] = None
        self._auto_task: Optional[asyncio.Task] = None
        # 用户删除的 run_id（防止全自动 seed 把平台上的题重新注册/重新求解）
        self._deleted: set[str] = set()
        # 公告轮询：上次公告指纹；变化时才拉赛题（省流量/避免阶段未开始刷 40001）
        self._notice_fp: str = ""
        self._last_notices: list[dict[str, Any]] = []
        self._load_meta()

    # ── 生命周期 ─────────────────────────────────────────────────────────
    async def start_sync(self) -> None:
        """订阅 Aemeath EventBus，同步 run 状态 + 翻译事件（lifespan 调用）。"""
        if self._sync_task is not None:
            return
        self._sync_task = asyncio.create_task(self._sync_loop(), name="muteki-sync")
        # 启动时先尝试拉一次赛题（阶段未开始会失败，后续靠公告轮询触发）
        await self._seed_challenges(force=True)
        # 全自动求解：启动后自动对未解出题目发起求解（蓝皮书 P1 轮询器语义）
        if getattr(self.runtime.settings, "adapter_auto_solve", True):
            self._auto_task = asyncio.create_task(
                self._auto_solve_loop(), name="muteki-auto-solve"
            )
            logger.info("auto-solve enabled — notice poll + unsolved challenge scan")

    async def stop(self) -> None:
        if self._auto_task is not None:
            self._auto_task.cancel()
            try:
                await self._auto_task
            except asyncio.CancelledError:
                pass
            self._auto_task = None
        if self._sync_task is not None:
            self._sync_task.cancel()
            try:
                await self._sync_task
            except asyncio.CancelledError:
                pass
            self._sync_task = None
        self._save_meta()

    # ── 全自动求解循环 ──────────────────────────────────────────────────
    async def _auto_solve_loop(self) -> None:
        """周期扫描：公告轮询 ->（有更新则）拉赛题 -> 自动求解未解出题。

        默认每 5s 拉一次公告；公告指纹变化时才拉赛题（省流量，也避免
        阶段未开始时反复刷 exercise-list 40001）。公告接口不可用时回退为
        周期性直接拉题。

        已解出的题目跳过（不浪费 token）；已 running/solved 的跳过。
        中断的 run（failed/stopped 且黑板有上下文）会自动从黑板续跑。
        串行逐个启动，避免同时拉爆资源；一轮内全部启动后等待下个周期。
        """
        interval = float(getattr(self.runtime.settings, "adapter_auto_poll_interval", 5.0) or 5.0)
        notice_poll = bool(getattr(self.runtime.settings, "adapter_notice_poll", True))
        try:
            while True:
                try:
                    # 1) 公告驱动 / 直接拉题
                    #    - 公告有更新 → 拉赛题
                    #    - 尚无任何赛题（阶段未开始/首次）→ 每轮都试拉，避免只靠公告漏题
                    #    - notice_poll=false → 每轮直接拉题
                    if notice_poll:
                        changed = await self._poll_notices()
                        no_challenges_yet = not any(
                            r.run_id not in self._deleted for r in self._runs.values()
                        )
                        if changed or no_challenges_yet or not self._seeded:
                            await self._seed_challenges(force=True)
                    else:
                        await self._seed_challenges(force=True)

                    # 2) 自动求解未解出题
                    await self._auto_start_unsolved()
                except Exception as e:  # noqa: BLE001
                    logger.warning("auto-solve cycle error: %s", e)
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            logger.info("auto-solve loop stopped")

    async def _poll_notices(self) -> bool:
        """拉公告并与上次指纹比对。返回 True 表示公告有更新（或首次成功）。

        平台无公告接口 / 拉取失败时返回 False（调用方按策略决定是否仍拉题）。
        """
        platform = self.runtime.platform
        if not hasattr(platform, "fetch_notices"):
            return False
        try:
            notices = await platform.fetch_notices()
        except Exception as e:  # noqa: BLE001
            logger.debug("notice poll failed: %s", e)
            return False
        if not isinstance(notices, list):
            notices = []
        fp = _notice_fingerprint(notices)
        if fp == self._notice_fp:
            return False
        prev = self._notice_fp
        self._notice_fp = fp
        self._last_notices = notices
        if prev:
            logger.info(
                "notices updated: %d item(s) — will refresh challenges",
                len(notices),
            )
        else:
            logger.info("notices baseline set: %d item(s)", len(notices))
        return True

    async def _auto_start_unsolved(self) -> None:
        """对未解出 / 中断的 run 自动发起求解。"""
        for entry in sorted(self._runs.values(), key=lambda r: r.order):
            if entry.solved:
                continue
            # adapter 重启后：引擎已丢失的"running" run → 视为中断，从黑板续跑
            if entry.started and not entry.finished:
                alive = False
                if entry.engine_run_id and self.runtime.engine is not None:
                    alive = entry.engine_run_id in (self.runtime.engine.runs or {})
                if not alive:
                    logger.info(
                        "auto-solve: restarting interrupted %r (engine lost)",
                        entry.run_id,
                    )
                    try:
                        await self.start(
                            entry.run_id,
                            {"kind": "swarm", "prompt": entry.prompt,
                             "challenge": {"name": entry.run_id,
                                           "category": entry.category}},
                        )
                    except RuntimeError as e:
                        logger.warning("auto-resume %s skipped: %s", entry.run_id, e)
                    except Exception as e:  # noqa: BLE001
                        logger.warning("auto-resume %s failed: %s", entry.run_id, e)
                continue
            # 断点续传：上次失败/停止 且 黑板留有上下文 → 自动续跑
            if entry.started and entry.finished and entry.status in ("failed", "stopped"):
                has_ctx = self._has_blackboard_ctx(entry.run_id)
                if has_ctx:
                    logger.info(
                        "auto-solve: resuming %r from blackboard", entry.run_id
                    )
                    try:
                        await self.start(
                            entry.run_id,
                            {"kind": "swarm", "prompt": entry.prompt,
                             "challenge": {"name": entry.run_id,
                                           "category": entry.category}},
                        )
                    except RuntimeError as e:
                        logger.warning("auto-resume %s skipped: %s", entry.run_id, e)
                    except Exception as e:  # noqa: BLE001
                        logger.warning("auto-resume %s failed: %s", entry.run_id, e)
                continue
            if entry.started:
                continue
            # 从未启动 → 自动启动求解
            logger.info("auto-solve: starting %r (unsolved)", entry.run_id)
            try:
                await self.start(
                    entry.run_id,
                    {"kind": "swarm", "prompt": entry.prompt,
                     "challenge": {"name": entry.run_id,
                                   "category": entry.category}},
                )
            except RuntimeError as e:
                # 已解出等拒绝原因——标记并继续
                logger.warning("auto-solve %s skipped: %s", entry.run_id, e)
                entry.solved = True
                entry.status = "solved"
            except Exception as e:  # noqa: BLE001
                logger.warning("auto-solve %s failed: %s", entry.run_id, e)

    def _has_blackboard_ctx(self, run_id: str) -> bool:
        """黑板是否留有该 run 的实质上下文（发现/死路/部分结果/活跃计划）。"""
        try:
            ctx = self.runtime.store.get_context(run_id)
            return bool(ctx and "暂无历史记忆" not in ctx)
        except Exception:  # noqa: BLE001
            return False

    # ── challenge 预注册 ─────────────────────────────────────────────────
    async def _seed_challenges(self, *, force: bool = False) -> None:
        """从平台拉赛题并预注册为 runs。

        force=False 且已 seed 过时直接返回（兼容旧调用）。
        force=True 时每次都重拉：发现新题、更新已解出状态。
        """
        if self._seeded and not force:
            return
        try:
            challenges = await self.runtime.platform.fetch_all_challenges()
        except Exception as e:  # noqa: BLE001
            logger.warning("seed challenges failed: %s", e)
            return
        # 成功拉到列表（哪怕为空）才标记 seeded；阶段未开始失败不标记，便于后续重试
        self._seeded = True
        before = len(self._runs)
        new_count = 0
        for ch in challenges:
            name = str(ch.get("name") or "").strip()
            if not name:
                continue
            # 用户已删除的题：不重新注册（防止自动复活）
            if name in self._deleted:
                continue
            # 平台已解出的题标记为 solved（前端显示已解出 + 不可启动，避免浪费 token）
            already_solved = bool(ch.get("solved") or ch.get("solved_by_me") or False)
            desc = str(ch.get("description") or "").strip()
            value = ch.get("value")
            existing = self._runs.get(name)
            if existing is not None:
                # 已有 run：补齐 solved/描述（若之前未保存），并确保有开场事件
                if already_solved and not existing.solved:
                    existing.solved = True
                    existing.status = "solved"
                if desc and not existing.prompt:
                    existing.prompt = desc
                if value is not None and not existing.value:
                    existing.value = int(value)
                entry = existing
            else:
                entry = RunEntry(
                    run_id=name,
                    name=str(ch.get("title") or ch.get("name") or name),
                    category=str(ch.get("category") or ""),
                    status="solved" if already_solved else "draft",
                    solved=already_solved,
                    order=self._next_order(),
                )
                if desc:
                    entry.prompt = desc
                if value is not None:
                    entry.value = int(value)
                self._runs[name] = entry
                new_count += 1
            # 预注册 run 写入初始 RUN_STARTED 事件（携带题面描述），
            # 这样前端打开草稿时能渲染出题目开场气泡，而不是空白对话区。
            # history 为空（首次 seed 或重启后）时补上；已有事件则保留。
            if not entry.history:
                if entry.bridge is None:
                    entry.bridge = EventBridge(entry.run_id)
                entry.history.append(entry.bridge._mk(RUN_STARTED, {
                    "challenge": {
                        "name": entry.name or name,
                        "category": entry.category,
                        "target": str(ch.get("connection_info") or ""),
                        "description": desc,
                        "expected_flags": 1,
                        "multi_flag": False,
                    },
                }))
        self._save_meta()
        logger.info(
            "seeded challenges: total=%d (+%d new, was %d) already_solved=%d",
            len(self._runs), new_count, before,
            sum(1 for r in self._runs.values() if r.solved),
        )

    # ── runs 列表 / 查询 ─────────────────────────────────────────────────
    def list_runs(self, include_archived: bool = False) -> list[dict[str, Any]]:
        runs = [r for r in self._runs.values() if include_archived or not r.archived]
        runs.sort(key=lambda r: (-int(r.pinned), r.order, r.run_id))
        return [r.summary() for r in runs]

    def get(self, run_id: str) -> Optional[RunEntry]:
        return self._runs.get(run_id)

    def ensure(self, run_id: str) -> RunEntry:
        entry = self._runs.get(run_id)
        if entry is None:
            entry = RunEntry(run_id=run_id, order=self._next_order())
            self._runs[run_id] = entry
        return entry

    def create(self) -> RunEntry:
        run_id = f"run-{uuid.uuid4().hex[:8]}"
        entry = RunEntry(run_id=run_id, order=self._next_order())
        self._runs[run_id] = entry
        self._save_meta()
        return entry

    # ── 元数据 ───────────────────────────────────────────────────────────
    def set_pinned(self, run_id: str, pinned: bool, now: Optional[float] = None) -> bool:
        r = self.get(run_id)
        if r is None:
            return False
        r.pinned = pinned
        r.pinned_at = (now or time.time()) if pinned else None
        r.updated = time.time()
        self._save_meta()
        return True

    def set_archived(self, run_id: str, archived: bool) -> bool:
        r = self.get(run_id)
        if r is None:
            return False
        r.archived = archived
        r.updated = time.time()
        self._save_meta()
        return True

    def rename(self, run_id: str, name: Any) -> bool:
        r = self.get(run_id)
        if r is None or not isinstance(name, str):
            return False
        r.name = name
        r.updated = time.time()
        self._save_meta()
        return True

    def set_folder(self, run_id: str, folder_id: Any) -> bool:
        r = self.get(run_id)
        if r is None:
            return False
        r.folder_id = folder_id
        r.updated = time.time()
        self._save_meta()
        return True

    def set_order(self, run_id: str, order: Any) -> bool:
        r = self.get(run_id)
        if r is None or not isinstance(order, (int, float)):
            return False
        r.order = int(order)
        r.updated = time.time()
        self._save_meta()
        return True

    async def delete(self, run_id: str) -> bool:
        r = self.get(run_id)
        if r is None:
            return False
        # 尝试停止引擎（已启动的 swarm / worker）
        if r.engine_run_id:
            try:
                await self.runtime.stop_engine(r.engine_run_id)
            except Exception:  # noqa: BLE001
                pass
        self._runs.pop(run_id, None)
        # 记录为已删除：全自动 seed 不再重新注册/重新求解该题（防止“复活”浪费 token）
        self._deleted.add(run_id)
        self._save_meta()
        return True

    def open_workspace(self, run_id: str) -> bool:
        # Aemeath 无宿主机工作区暴露 → no-op
        return False

    # ── folders ──────────────────────────────────────────────────────────
    def list_folders(self) -> list[dict[str, Any]]:
        fs = sorted(self._folders.values(), key=lambda f: f.order)
        return [f.as_dict() for f in fs]

    def create_folder(self, name: str) -> dict[str, Any]:
        folder = Folder(id=f"folder-{uuid.uuid4().hex[:6]}", name=name or "未命名", order=self._next_order())
        self._folders[folder.id] = folder
        self._save_meta()
        return folder.as_dict()

    def update_folder(self, folder_id: str, name: Any = None, order: Any = None) -> bool:
        f = self._folders.get(folder_id)
        if f is None:
            return False
        if isinstance(name, str):
            f.name = name
        if isinstance(order, (int, float)):
            f.order = int(order)
        self._save_meta()
        return True

    def delete_folder(self, folder_id: str) -> bool:
        if folder_id not in self._folders:
            return False
        self._folders.pop(folder_id)
        for r in self._runs.values():
            if r.folder_id == folder_id:
                r.folder_id = None
        self._save_meta()
        return True

    # ── start / 引擎桥接 ─────────────────────────────────────────────────
    async def start(self, run_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """启动一个 run：翻译 muteki 启动体 → Aemeath launch_solver。"""
        entry = self.ensure(run_id)
        # 平台已解出的题拒绝再次启动（避免浪费 token 重复求解）
        if entry.solved:
            raise RuntimeError(f"题目 {entry.name or run_id!r} 已在平台解出，无需重复求解。")
        ch = body.get("challenge") or {}
        if ch.get("name"):
            entry.name = str(ch["name"])
        if ch.get("category"):
            entry.category = str(ch["category"])
        entry.prompt = str(body.get("prompt") or ch.get("description") or "")
        problem_id = entry.run_id  # 预注册 run → run_id 即 challenge name

        mode = str(body.get("mode") or body.get("kind") or "swarm")
        if mode not in ("swarm", "orchestrated", "hybrid", "auto"):
            mode = "auto"

        try:
            res = await self.runtime.launch_solver(
                problem_id,
                prompt=entry.prompt,
                target=str(ch.get("target") or ""),
                attachments=list(ch.get("attachments") or []) or None,
                mode=mode,
            )
        except KeyError as e:
            raise RuntimeError(f"平台未找到题目 {problem_id!r}，请从左侧题目列表选择。({e})")
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"启动失败: {e}")

        entry.engine_run_id = res.get("run_id")
        entry.started = True
        entry.finished = False
        entry.solved = False
        entry.paused = False
        entry.status = "running"
        entry.updated = time.time()
        self._save_meta()
        return {"run_id": run_id, "ok": True, "engine_run_id": entry.engine_run_id}

    # ── 事件同步循环 ─────────────────────────────────────────────────────
    async def _sync_loop(self) -> None:
        queue = self.runtime.bus.subscribe()
        try:
            while True:
                aev = await queue.get()
                self._handle_event(aev)
        finally:
            self.runtime.bus.unsubscribe(queue)

    def _handle_event(self, aev: Any) -> None:
        """把一个 Aemeath 事件分发到对应 run：翻译 + 广播 + 状态同步。"""
        # 按 engine_run_id 或 problem_id 定位 run
        entry = self._runs.get(str(aev.problem_id or "")) or next(
            (r for r in self._runs.values() if r.engine_run_id == aev.run_id), None
        )
        if entry is None:
            return
        if entry.bridge is None:
            entry.bridge = EventBridge(entry.run_id)
        events = entry.bridge.translate(aev)
        if not events:
            return
        # 状态同步
        for me in events:
            if me.event_type == RUN_STARTED:
                entry.started = True
                entry.status = "running"
            elif me.event_type == RUN_FINISHED:
                entry.finished = True
                p = me.payload
                entry.status = "solved" if p.get("status") == "solved" else (
                    "failed" if p.get("status") == "failed" else (
                        "finished" if p.get("status") == "stopped" else "finished"))
                entry.solved = bool(p.get("status") == "solved" and p.get("flag"))
                if p.get("flag"):
                    entry.flag = str(p["flag"])
                if p.get("error"):
                    entry.error = str(p["error"])
            entry.updated = time.time()
        # 历史 + 广播
        for me in events:
            entry.history.append(me)
            for q in list(entry.subs):
                try:
                    q.put_nowait(me)
                except asyncio.QueueFull:
                    try:
                        q.get_nowait()
                        q.put_nowait(me)
                    except Exception:  # noqa: BLE001
                        pass

    # ── SSE 订阅 ─────────────────────────────────────────────────────────
    def subscribe(self, run_id: str) -> Optional[tuple[asyncio.Queue, list[MutekiEvent]]]:
        """返回 (实时队列, 历史快照)。run 不存在返回 None。"""
        entry = self.get(run_id)
        if entry is None:
            return None
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        entry.subs.append(q)
        history = list(entry.history)
        return q, history

    def unsubscribe(self, run_id: str, q: asyncio.Queue) -> None:
        entry = self.get(run_id)
        if entry is not None:
            try:
                entry.subs.remove(q)
            except ValueError:
                pass

    # ── hitl ─────────────────────────────────────────────────────────────
    async def hitl(self, run_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """operator 指令 → Aemeath 黑板 / 引擎控制。"""
        entry = self.ensure(run_id)
        action = str(body.get("action") or "")
        text = str(body.get("text") or "")
        target = str(body.get("target") or "")

        store = self.runtime.store
        if action == "pause":
            # 真暂停：停止底层引擎 task（断点续传靠黑板持久化上下文）
            if entry.engine_run_id:
                try:
                    await self.runtime.stop_engine(entry.engine_run_id)
                except Exception as e:  # noqa: BLE001
                    logger.warning("pause stop engine failed: %s", e)
            entry.paused = True
            entry.status = "paused"
            self._guidance(entry, "操作员暂停了求解（引擎已停止，黑板上下文已保存）")
        elif action == "resume":
            entry.paused = False
            entry.status = "running"
            # 真恢复：若引擎已被停止（finished），从黑板续跑重新启动
            if entry.finished or not entry.started or not entry.engine_run_id:
                self._guidance(entry, "操作员恢复求解——从黑板续跑重新启动")
                try:
                    await self.start(
                        run_id,
                        {"kind": "swarm", "prompt": entry.prompt,
                         "challenge": {"name": entry.run_id, "category": entry.category}},
                    )
                except RuntimeError as e:
                    self._guidance(entry, f"续跑启动失败: {e}")
            else:
                self._guidance(entry, "操作员恢复求解")
        elif action == "mark_false":
            flag = str(body.get("flag") or text)
            if flag:
                store.mark_deadend(entry.run_id, f"flag 被判定为误报: {flag}", source="operator")
                self._guidance(entry, f"操作员标记 flag 为误报: {flag}")
        elif action == "redirect":
            url = str(body.get("url") or target or "")
            if url:
                store.add_discovery(entry.run_id, f"[operator] 题目迁移到 {url}", source="operator")
                self._guidance(entry, f"操作员重定向目标: {url}")
        elif action in ("hint", "directive", "focus"):
            if text:
                store.add_discovery(entry.run_id, f"[operator {action}] {text}", source="operator")
                store.create_intent(entry.run_id, action, target=target or None, reasoning=text)
                self._guidance(entry, f"操作员指令({action}): {text}")
        else:
            if text:
                store.add_discovery(entry.run_id, f"[operator] {text}", source="operator")
                self._guidance(entry, f"操作员: {text}")
        entry.updated = time.time()
        self._save_meta()
        return {"ok": True, "run_id": run_id}

    def _guidance(self, entry: RunEntry, text: str) -> None:
        """发布 coordinator.guidance 事件（前端聊天气泡可见）。"""
        if entry.bridge is None:
            entry.bridge = EventBridge(entry.run_id)
        me = entry.bridge._mk(GUIDANCE, {"text": text})
        entry.history.append(me)
        for q in list(entry.subs):
            try:
                q.put_nowait(me)
            except asyncio.QueueFull:
                pass

    # ── 持久化 ───────────────────────────────────────────────────────────
    def _next_order(self) -> int:
        self._order_counter += 1
        return self._order_counter

    def _load_meta(self) -> None:
        try:
            if not self._meta_path.exists():
                return
            data = json.loads(self._meta_path.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            logger.warning("load meta failed: %s", e)
            return
        self._order_counter = 0
        self._deleted = set(data.get("deleted") or [])
        for rid, meta in (data.get("runs") or {}).items():
            entry = RunEntry(run_id=rid)
            entry.apply_meta(meta)
            self._runs[rid] = entry
            if meta.get("order", 0) > self._order_counter:
                self._order_counter = int(meta.get("order", 0))
        for f in (data.get("folders") or []):
            folder = Folder(id=f.get("id", ""), name=f.get("name", ""), order=int(f.get("order", 0)))
            if folder.id:
                self._folders[folder.id] = folder

    def _save_meta(self) -> None:
        try:
            self._meta_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "runs": {rid: r.meta() for rid, r in self._runs.items()},
                "folders": [f.as_dict() for f in self._folders.values()],
                "deleted": sorted(self._deleted),
            }
            self._meta_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("save meta failed: %s", e)
