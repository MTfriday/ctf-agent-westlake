"""统一提交门禁 — 去重校验 + 串行化提交 + 排行榜验证。

所有 Flag 提交（引擎返回 / 人工 / 「一眼出」直提）都必须经过本门禁：
1. 去重：内存缓存 + 黑板持久化（facts type='partial'），防止重复提交导致扣分/风控
2. 串行化：每题一把 asyncio.Lock，避免多 Worker 并发提交
3. 验证：以 fetch_solved 排行榜为最终判定依据（GZCTF 提交陷阱）
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from adapter.events import Event, EventType, ev

logger = logging.getLogger(__name__)

# 黑板 partial fact 的提交记录前缀（用于持久化去重状态）
_SUBMISSION_PREFIX = "SUBMIT_FLAG|"


class SubmitGate:
    """线程/协程安全的提交门禁。"""

    def __init__(self, platform: Any, store: Any, bus: Any, no_submit: bool = False) -> None:
        self.platform = platform
        self.store = store
        self.bus = bus
        self.no_submit = no_submit
        self._locks: dict[str, asyncio.Lock] = {}
        self._submitted: dict[str, set[str]] = {}  # problem_id -> {flag,...} 内存缓存

    # ------------------------------------------------------------------ 去重
    def _lock_for(self, problem_id: str) -> asyncio.Lock:
        if problem_id not in self._locks:
            self._locks[problem_id] = asyncio.Lock()
        return self._locks[problem_id]

    def _load_seen(self, problem_id: str) -> set[str]:
        """从黑板恢复该题已提交过的 flag。"""
        seen: set[str] = set()
        for f in self.store.list_facts(problem_id, type="partial", limit=10000):
            content = f.get("content", "") or ""
            if content.startswith(_SUBMISSION_PREFIX):
                rest = content[len(_SUBMISSION_PREFIX):]
                flag = rest.split("|", 1)[0]
                if flag:
                    seen.add(flag.strip())
        return seen

    def _seen(self, problem_id: str) -> set[str]:
        if problem_id not in self._submitted:
            self._submitted[problem_id] = self._load_seen(problem_id)
        return self._submitted[problem_id]

    # ------------------------------------------------------------------ 提交
    async def submit(self, problem_id: str, flag: str, source: str = "manual") -> dict[str, Any]:
        """提交一个 Flag（带门禁）。返回 {status, solved, message, flag}。

        status ∈ duplicate | correct | already_solved | incorrect | error | dry-run
        """
        flag = (flag or "").strip()
        async with self._lock_for(problem_id):
            # 1. 去重（空 flag 直接拒绝）
            if not flag:
                return {"status": "incorrect", "solved": False, "message": "Empty flag.", "flag": ""}
            if flag in self._seen(problem_id):
                return {
                    "status": "duplicate", "solved": False,
                    "message": "Duplicate — this flag was already submitted.", "flag": flag,
                }

            # 2. 平台提交
            if self.no_submit:
                status, display = "dry-run", f"DRY RUN — would submit {flag}"
            else:
                try:
                    result = await self.platform.submit_flag(problem_id, flag)
                    status = result.status
                    display = result.display
                except Exception as e:  # noqa: BLE001 — 门禁兜底，任何异常都转可读错误
                    logger.warning("submit failed for %s: %s", problem_id, e)
                    status, display = "error", f"Submit error: {e}"

            # 3. 记录（无论成败都去重，防止反复重试同一 flag）
            self._seen(problem_id).add(flag)
            self.store.add_partial(
                problem_id, f"{_SUBMISSION_PREFIX}{flag}|{status}", source=f"submit:{source}"
            )
            await self.bus.publish(
                ev(EventType.BLACKBOARD_DELTA, problem_id=problem_id, kind="submission", status=status)
            )
            await self.bus.publish(
                ev(EventType.FLAG_SUBMITTED, problem_id=problem_id,
                   flag=flag, status=status, source=source, display=display)
            )

            # 4. 排行榜验证（GZCTF 陷阱：以 solved 状态为最终依据）
            verified = await self.verify(problem_id)
            if verified["solved"]:
                await self.bus.publish(ev(EventType.FLAG_SOLVED, problem_id=problem_id, flag=flag))

            return {"status": status, "solved": verified["solved"], "message": display, "flag": flag}

    # ------------------------------------------------------------------ 验证
    async def verify(self, problem_id: str) -> dict[str, Any]:
        """查询排行榜，确认该题是否已解出。"""
        try:
            solved_names = await self.platform.fetch_solved_names()
            solved = problem_id in solved_names
            names = sorted(solved_names)
        except Exception as e:  # noqa: BLE001
            logger.warning("verify failed for %s: %s", problem_id, e)
            solved, names = False, []
        await self.bus.publish(ev(EventType.FLAG_VERIFIED, problem_id=problem_id, solved=solved))
        return {"problem_id": problem_id, "solved": solved, "solved_names": names}
