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
import json
import logging
import time
import uuid
from collections import deque
from urllib.parse import urlparse

import httpx
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

# auto-solve 跳过集：用户已放弃的题（提交次数用尽等）不自动重跑，白烧额度
_AUTO_SOLVE_SKIP: set[str] = {"解压缩"}

# 单 run 环形历史容量（Last-Event-ID 续传窗口）
_HISTORY_LEN = 600


class PlatformChallengeMissing(RuntimeError):
    """题目在平台上找不到（平台不可达/题目不存在）——区别于"已解出"拒绝。"""


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
    # 操作员手动停止（hitl stop）：禁止 auto-solve 断点续传自动拉起，直到 resume
    manual_stopped: bool = False
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
    # 非提交（本地）模式：不访问平台、不提交 flag，仅本地求解并回显网页
    no_submit: bool = False
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
            "no_submit": self.no_submit,
        }

    def meta(self) -> dict[str, Any]:
        """仅元数据子集（持久化）。"""
        return {
            "name": self.name, "category": self.category, "value": self.value,
            "solved": self.solved, "status": self.status,
            "prompt": self.prompt,
            "no_submit": self.no_submit,
            "flag": self.flag, "error": self.error,
            "pinned": self.pinned, "pinned_at": self.pinned_at,
            "archived": self.archived, "folder_id": self.folder_id, "order": self.order,
        }

    def apply_meta(self, meta: dict[str, Any]) -> None:
        for k in ("name", "category", "value", "solved", "status", "prompt",
                  "flag", "error", "no_submit",
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
        # 平台题目名白名单（seed 时收集）：auto-solve 只自动求解平台题，
        # 不碰前端自建的 run-XXXX 草稿
        self._platform_names: set[str] = set()
        self._load_meta()

    # ── 生命周期 ─────────────────────────────────────────────────────────
    async def start_sync(self) -> None:
        """订阅 Aemeath EventBus，同步 run 状态 + 翻译事件（lifespan 调用）。"""
        if self._sync_task is not None:
            return
        self._sync_task = asyncio.create_task(self._sync_loop(), name="muteki-sync")
        await self._seed_challenges()
        # 全自动求解：启动后自动对未解出题目发起求解（蓝皮书 P1 轮询器语义）
        if getattr(self.runtime.settings, "adapter_auto_solve", True):
            self._auto_task = asyncio.create_task(
                self._auto_solve_loop(), name="muteki-auto-solve"
            )
            logger.info("auto-solve enabled — scanning unsolved challenges")

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
        """周期扫描：自动发起对未解出 run 的求解；检测平台新题/已解出。

        已解出的题目跳过（不浪费 token）；已 running/solved 的跳过。
        中断的 run（failed/stopped 且黑板有上下文）会自动从黑板续跑。
        串行逐个启动，避免同时拉爆资源；一轮内全部启动后等待下个周期。
        """
        interval = float(getattr(self.runtime.settings, "adapter_auto_poll_interval", 20.0) or 20.0)
        cycle = 0
        try:
            while True:
                try:
                    # 平台 429 冷却期内整轮跳过：不测窗口、不重启 run、不强刷 seed，
                    # 让限流窗口自然关闭（否则每 20s 的重试/10 分钟强刷会不断延长窗口）
                    plat_cooldown = getattr(
                        self.runtime.platform, "in_cooldown", lambda: False
                    )()
                    if plat_cooldown:
                        await asyncio.sleep(interval)
                        continue
                    # 定期重拉平台题目：发现新题自动预注册 + 检测已解出。
                    # _seeded 守卫使普通 seed 恒为 no-op；每 30 轮（~10 分钟）
                    # force 强刷一次，平台 429 恢复后错误标记自愈、新题可被发现。
                    cycle += 1
                    await self._seed_challenges(force=(cycle % 30 == 0))
                    for entry in sorted(self._runs.values(), key=lambda r: r.order):
                        # 只自动求解平台题目；前端自建 run（run-XXXX）一律跳过
                        if entry.run_id not in self._platform_names:
                            continue
                        # 用户已放弃的题（提交次数用尽）不自动重跑
                        if entry.run_id in _AUTO_SOLVE_SKIP:
                            continue
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
                                    if isinstance(e, PlatformChallengeMissing):
                                        entry.error = "platform_missing:" + str(e)
                                    logger.warning("auto-resume %s skipped: %s", entry.run_id, e)
                                except Exception as e:  # noqa: BLE001
                                    logger.warning("auto-resume %s failed: %s", entry.run_id, e)
                            continue
                        # 断点续传：上次失败/停止 且 黑板留有上下文 → 自动续跑
                        # （操作员手动 stop 过的题不自动拉起）
                        if (entry.started and entry.finished and not entry.manual_stopped
                                and entry.status in ("failed", "stopped")):
                            # 平台不可达/题目不存在时不再反复续跑（seed 成功后清空 error 再恢复）
                            if entry.error and (
                                "platform_missing" in entry.error
                                or "not found on platform" in entry.error
                            ):
                                continue
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
                                    if isinstance(e, PlatformChallengeMissing):
                                        entry.error = "platform_missing:" + str(e)
                                    logger.warning("auto-resume %s skipped: %s", entry.run_id, e)
                                except Exception as e:  # noqa: BLE001
                                    logger.warning("auto-resume %s failed: %s", entry.run_id, e)
                            continue
                        if entry.started:
                            continue
                        # 平台不可达时不再反复尝试启动（seed 成功后清空 error 再恢复）
                        if entry.error and (
                            "platform_missing" in entry.error
                            or "not found on platform" in entry.error
                        ):
                            continue
                        # 已解出但运行中结束、或从未启动 → 自动启动求解
                        logger.info("auto-solve: starting %r (unsolved)", entry.run_id)
                        try:
                            await self.start(
                                entry.run_id,
                                {"kind": "swarm", "prompt": entry.prompt,
                                 "challenge": {"name": entry.run_id,
                                               "category": entry.category}},
                            )
                        except RuntimeError as e:
                            # start() 的"已解出"拒绝已被上方 entry.solved 过滤；此处的
                            # RuntimeError 只可能是启动失败（平台不可达等）——不能误标为已解出。
                            if isinstance(e, PlatformChallengeMissing):
                                entry.error = "platform_missing:" + str(e)
                            else:
                                entry.error = str(e)
                            logger.warning("auto-solve %s failed: %s", entry.run_id, e)
                        except Exception as e:  # noqa: BLE001
                            logger.warning("auto-solve %s failed: %s", entry.run_id, e)
                except Exception as e:  # noqa: BLE001
                    logger.warning("auto-solve cycle error: %s", e)
                # 清理遗留的"running"死状态：重启后引擎已丢失、且本周期未被
                # 重启的 run（跳过集/非平台题等不会被 auto-solve 重启），状态
                # 永远停在"运行中"→ 左侧列表误导 + 沙箱面板无容器。标 stopped
                # 恢复真实状态；平台题有黑板上下文时仍会走"断点续传"路径重启。
                changed = False
                for entry in list(self._runs.values()):
                    if not (entry.started and not entry.finished):
                        continue
                    alive = False
                    if entry.engine_run_id and self.runtime.engine is not None:
                        alive = entry.engine_run_id in (self.runtime.engine.runs or {})
                    if not alive:
                        logger.info(
                            "auto-solve: stale running %r -> stopped (engine lost, not relaunched)",
                            entry.run_id,
                        )
                        entry.finished = True
                        entry.status = "stopped"
                        entry.error = "interrupted by adapter restart"
                        changed = True
                if changed:
                    self._save_meta()
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            logger.info("auto-solve loop stopped")

    def _has_blackboard_ctx(self, run_id: str) -> bool:
        """黑板是否留有该 run 的实质上下文（发现/死路/部分结果/活跃计划）。"""
        try:
            ctx = self.runtime.store.get_context(run_id)
            return bool(ctx and "暂无历史记忆" not in ctx)
        except Exception:  # noqa: BLE001
            return False

    # ── challenge 预注册 ─────────────────────────────────────────────────
    async def _seed_challenges(self, force: bool = False) -> None:
        if self._seeded and not force:
            return
        self._seeded = True
        try:
            # light 模式：seed 只需要 name/solved，不逐题拉详情
            # （p15：全量详情 4 连发必中平台 429 → 300s 冷却 → auto-solve 锁死）
            challenges = await self.runtime.platform.fetch_all_challenges(light=True)
        except Exception as e:  # noqa: BLE001
            logger.warning("seed challenges failed: %s", e)
            return
        for ch in challenges:
            name = str(ch.get("name") or "").strip()
            if not name:
                continue
            self._platform_names.add(name)
            existing = self._runs.get(name)
            if existing is not None:
                # 平台恢复可达 → 清除 platform_missing/not-found 标记（必须先于
                # _deleted 检查：已删除题的错误标记若残留，auto-solve 的 error
                # 拦截会永久跳过该题，平台列表也无法手动重启；_deleted 只阻止
                # 重新注册新条目，不影响已有条目的自愈）
                if existing.error and (
                    "platform_missing" in existing.error
                    or "not found on platform" in existing.error
                ):
                    existing.error = ""
                    logger.info("cleared stale platform error for %r", name)
            # 用户已删除的题：不重新注册（防止自动复活）
            if name in self._deleted:
                continue
            # 平台已解出的题标记为 solved（前端显示已解出 + 不可启动，避免浪费 token）
            already_solved = bool(ch.get("solved") or ch.get("solved_by_me") or False)
            desc = str(ch.get("description") or "").strip()
            value = ch.get("value")
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
        logger.info("seeded %d challenge runs (%d already solved)",
                    len(self._runs), sum(1 for r in self._runs.values() if r.solved))

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

    def create(self, name: str = "") -> RunEntry:
        """创建 run：给名字则用名字作 run_id（解题模式/平台题同名），否则随机 run-XXXX。"""
        name = str(name or "").strip()[:80]
        if name:
            if name in self._runs:
                return self._runs[name]
            entry = RunEntry(run_id=name, name=name, order=self._next_order())
            self._runs[name] = entry
            self._save_meta()
            return entry
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
        """启动一个 run：翻译 muteki 启动体 → Aemeath launch_solver。

        0x03 双模式：
          - 比赛模式（默认 / mode=competition）：problem_id 必须是平台题目名；
            run-XXXX 草稿启动时若带平台题目名，自动迁移 run_id（修复网页端秒失败）。
          - 解题模式（mode=manual / no_submit=true）：本地构建题目 + 下载附件，
            不访问平台、不提交 flag，解出的 flag 只回显网页。
        """
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
        # 非提交模式开关：body 里显式带 no_submit 才更新，之后按 run 粘住
        # （auto-solve 续跑 / resolve 不传时保持原值）
        if body.get("no_submit") is not None:
            entry.no_submit = bool(body.get("no_submit"))

        mode = str(body.get("mode") or body.get("kind") or "swarm")
        ch_name = str(ch.get("name") or "").strip()
        is_platform = bool(ch_name) and ch_name in self._platform_names
        if body.get("no_submit") or mode in ("manual", "local"):
            # 解题模式：本地直解（不访问/不提交平台）
            entry.no_submit = True
            mode = "swarm"
        elif is_platform and entry.run_id != ch_name:
            # 比赛模式：run-XXXX 草稿 → 迁移为平台题目名（rail 已有同名 run 则复用）
            if ch_name in self._runs:
                entry = self._runs[ch_name]
                entry.prompt = str(body.get("prompt") or ch.get("description") or entry.prompt)
            else:
                self._runs.pop(run_id, None)
                entry.run_id = ch_name
                self._runs[ch_name] = entry
        problem_id = entry.run_id

        # 解题模式附件：body.challenge.attachments = [{name?, url?} | {path} | "url"]
        attach_paths: list[str] = []
        if entry.no_submit:
            for a in (ch.get("attachments") or []):
                if isinstance(a, str):
                    # 网页端直接把附件路径当字符串传 → 非 URL 视为本地绝对路径（0x02）
                    a = {"url": a} if a.startswith(("http://", "https://")) else {"path": a}
                if not isinstance(a, dict):
                    continue
                p = str(a.get("path") or "")
                url = str(a.get("url") or "")
                if p and Path(p).is_file():
                    attach_paths.append(p)
                elif url.startswith(("http://", "https://")):
                    dest = await self._download_attachment(problem_id, url)
                    if dest:
                        attach_paths.append(dest)

        if mode not in ("swarm", "orchestrated", "hybrid", "auto"):
            mode = "auto"
        if entry.no_submit:
            mode = "swarm"

        try:
            res = await self.runtime.launch_solver(
                problem_id,
                prompt=entry.prompt,
                target=str(ch.get("target") or ""),
                attachments=attach_paths or None,
                category=entry.category,
                mode=mode,
                no_submit=entry.no_submit,
            )
        except KeyError as e:
            raise PlatformChallengeMissing(
                f"平台未找到题目 {problem_id!r}，请从左侧题目列表选择。({e})"
            ) from e
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
        return {"run_id": problem_id, "ok": True, "engine_run_id": entry.engine_run_id,
                "mode": "manual" if entry.no_submit else "competition"}

    async def _download_attachment(self, problem_id: str, url: str) -> str | None:
        """解题模式：把附件 URL 下载到 challenges/{problem_id}/distfiles/。

        平台资源（pro-resource.dasctf.com）需要 X-Agent-AccessKey 头——先裸请求，
        403 时带 AccessKey 重试；其余 URL 裸请求即可。
        """
        try:
            dest_dir = Path(self.runtime.challenges_root) / problem_id / "distfiles"
            dest_dir.mkdir(parents=True, exist_ok=True)
            name = Path(urlparse(url).path).name or f"attachment_{int(time.time())}"
            dest = dest_dir / name
            async with httpx.AsyncClient(trust_env=False, timeout=120,
                                         follow_redirects=True) as client:
                # 传输层/DNS 瞬断重试 3 次（VM DNS 间歇性失败，实测会抽风）
                resp = None
                last_exc: Exception | None = None
                for attempt in range(3):
                    try:
                        resp = await client.get(url)
                        break
                    except (httpx.TransportError, OSError) as e:  # noqa: BLE001
                        last_exc = e
                        logger.warning("attachment download attempt %d/3 failed %s: %s",
                                       attempt + 1, url, e)
                        await asyncio.sleep(1.5 * (attempt + 1))
                if resp is None:
                    raise RuntimeError(f"attachment download failed after 3 attempts: {last_exc}")
                if resp.status_code in (401, 403):
                    ak = getattr(self.runtime.settings, "slab_access_key", "") or ""
                    if ak:
                        resp = await client.get(url, headers={"X-Agent-AccessKey": ak})
                if resp.status_code != 200:
                    logger.warning("attachment download failed %s: HTTP %d",
                                   url, resp.status_code)
                    return None
                dest.write_bytes(resp.content)
            logger.info("attachment downloaded: %s (%d bytes)", dest, dest.stat().st_size)
            return str(dest)
        except Exception as e:  # noqa: BLE001
            logger.warning("attachment download error %s: %s", url, e)
            return None

    async def sync_challenges(self) -> dict[str, Any]:
        """强制重新拉取平台题目（网页端「同步题目」按钮）。"""
        before = set(self._runs.keys())
        self._seeded = False
        await self._seed_challenges(force=True)
        after = set(self._runs.keys())
        return {
            "ok": True,
            "total": len(self._runs),
            "added": sorted(after - before),
            "unsolved": [r.run_id for r in self._runs.values() if not r.solved],
        }

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
            entry.manual_stopped = False
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
        elif action == "stop":
            # 真停止：停引擎（与 pause 不同——终结，不自动续跑）。
            # 前端「停止解题」按钮走这里；删除 run（DELETE）是另一条用户认可的路径。
            if entry.engine_run_id:
                try:
                    await self.runtime.stop_engine(entry.engine_run_id)
                except Exception as e:  # noqa: BLE001
                    logger.warning("stop engine failed: %s", e)
            entry.finished = True
            entry.status = "stopped"
            entry.manual_stopped = True
            self._guidance(entry, "操作员停止了求解（引擎已停止，黑板上下文已保存）")
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

    # ── worker 增删（前端「worker 面板」实时控制）────────────────────────
    async def spawn_worker(self, run_id: str, spec: str) -> None:
        """给活跃 swarm 动态增加一条 solver lane（模型池扩容）。"""
        entry = self.ensure(run_id)
        engine = self.runtime.engine
        if engine is None:
            raise RuntimeError("引擎不可用")
        if not (entry.started and not entry.finished):
            raise RuntimeError("run 不在运行中")
        swarm = self._get_active_swarm(engine, entry.run_id)
        if not await swarm.spawn_solver(spec):
            raise RuntimeError(f"solver lane 已存在: {spec}")
        if spec not in self.runtime.model_specs:
            self.runtime.model_specs.append(spec)
        logger.info("spawned worker lane %s on %r", spec, run_id)

    async def kill_worker(self, run_id: str, spec: str) -> None:
        """取消一条 solver lane（worker 杀进程）。

        幂等删除（0x06）：run 已结束/无活跃 swarm、或 lane 早已消亡时视为
        删除成功——用户对已结束 run 的 worker 面板点删除不应报错，只有
        run 本身不存在/引擎不可用才算失败。"""
        entry = self.ensure(run_id)
        engine = self.runtime.engine
        if engine is None:
            raise RuntimeError("引擎不可用")
        swarm = (getattr(engine, "_swarms", None) or {}).get(entry.run_id)
        if swarm is not None and not swarm.cancel_event.is_set():
            await swarm.kill_solver(spec)
        logger.info("killed worker lane %s on %r (idempotent)", spec, run_id)

    @staticmethod
    def _get_active_swarm(engine: Any, problem_id: str) -> Any:
        swarms = getattr(engine, "_swarms", None) or {}
        swarm = swarms.get(problem_id)
        if swarm is None:
            raise RuntimeError(f"swarm 不存在: {problem_id}")
        if swarm.cancel_event.is_set():
            raise RuntimeError(f"swarm 已结束: {problem_id}")
        return swarm

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
