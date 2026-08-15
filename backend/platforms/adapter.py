"""统一平台适配器 — 将任意 PlatformClient 包装为运行时需要的 CTFd 风格接口。

运行时（coordinator / poller / solvers）通过 CTFd 风格的方法名访问平台：
    fetch_challenge_stubs() / fetch_all_challenges() / fetch_solved_names()
    submit_flag(name, flag) / pull_challenge(data, dir) / close()

本适配器把这些调用翻译成底层 PlatformClient（CTF2Client / GenericRestClient）
的标准接口，使现有主流程无需改动即可对接不同平台。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from backend.platforms.base import PlatformClient, SubmitResult

logger = logging.getLogger(__name__)

# CTFd SubmitResult-compatible alias
CtfSubmitResult = SubmitResult


def _slugify(name: str) -> str:
    """Convert a challenge name to a filesystem-safe slug."""
    slug = name.lower().strip()
    slug = re.sub(r'[<>:"/\\|?*.\x00-\x1f]', "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug or "challenge"


class PlatformAdapter:
    """Exposes a CTFd-style interface over any PlatformClient."""

    def __init__(self, client: PlatformClient) -> None:
        self._client = client
        # name -> ChallengeInfo cache (populated by fetch calls)
        self._by_name: dict[str, Any] = {}
        self._by_id: dict[str, Any] = {}

    # ── Challenge listing ────────────────────────────────────────────────────

    async def fetch_challenge_stubs(self) -> list[dict[str, Any]]:
        """Lightweight challenge list (no per-challenge detail)."""
        challenges = await self._client.fetch_challenges()
        stubs: list[dict[str, Any]] = []
        for c in challenges:
            self._by_name[c.name] = c
            self._by_id[str(c.id)] = c
            stubs.append({
                "id": str(c.id),
                "name": c.name,
                "category": c.category,
                "value": c.value,
                "solved": c.solved,
            })
        return stubs

    async def fetch_all_challenges(self) -> list[dict[str, Any]]:
        """Full challenge list with metadata."""
        challenges = await self._client.fetch_challenges()
        result: list[dict[str, Any]] = []
        for c in challenges:
            self._by_name[c.name] = c
            self._by_id[str(c.id)] = c
            result.append({
                "id": str(c.id),
                "name": c.name,
                "category": c.category,
                "value": c.value,
                "solves": 0,  # not available via generic interface
                "description": c.description,
                "connection_info": c.connection_info,
                "files": c.files,
                "tags": c.tags,
                "solved": c.solved,
            })
        return result

    async def fetch_solved_names(self) -> set[str]:
        """Set of solved challenge names."""
        try:
            solved_ids = await self._client.fetch_solved()
        except Exception as e:
            logger.warning("fetch_solved failed: %s", e)
            return set()

        # Also refresh mapping from challenge list
        try:
            await self.fetch_challenge_stubs()
        except Exception:
            pass

        solved: set[str] = set()
        for cid in solved_ids:
            info = self._by_id.get(str(cid))
            if info:
                solved.add(info.name)
            else:
                solved.add(str(cid))
        return solved

    # ── Flag submission ──────────────────────────────────────────────────────

    async def submit_flag(self, challenge_name: str, flag: str) -> CtfSubmitResult:
        """Submit a flag by challenge name."""
        flag = flag.strip()
        if not flag:
            return CtfSubmitResult("incorrect", "Empty flag", "Empty flag — nothing to submit.")

        # Resolve name → challenge id (fetch if not cached)
        info = self._by_name.get(challenge_name)
        if info is None:
            try:
                await self.fetch_all_challenges()
                info = self._by_name.get(challenge_name)
            except Exception as e:
                return CtfSubmitResult("unknown", str(e), f"Failed to list challenges: {e}")

        if info is None:
            return CtfSubmitResult(
                "unknown", "challenge not found",
                f"Challenge '{challenge_name}' not found on platform.",
            )

        result = await self._client.submit_flag(info.id, flag)
        return result

    # ── Challenge pulling (local dir + metadata.yml + distfiles) ────────────

    async def pull_challenge(self, challenge_data: dict[str, Any], output_dir: str) -> str:
        """Download challenge files and write metadata.yml.

        Returns the challenge directory path.
        """
        name = challenge_data.get("name", "Unknown")
        slug = _slugify(name)
        ch_dir = Path(output_dir) / slug
        dist_dir = ch_dir / "distfiles"
        dist_dir.mkdir(parents=True, exist_ok=True)

        # Build metadata.yml
        meta = {
            "name": name,
            "category": challenge_data.get("category", ""),
            "value": challenge_data.get("value", 0),
            "description": challenge_data.get("description", ""),
            "connection_info": challenge_data.get("connection_info", ""),
            "tags": challenge_data.get("tags", []),
            "solves": challenge_data.get("solves", 0),
        }
        meta_path = ch_dir / "metadata.yml"
        try:
            import yaml
            with open(meta_path, "w") as f:
                yaml.safe_dump(meta, f, allow_unicode=True)
        except Exception as e:
            logger.warning("Failed to write metadata.yml: %s", e)

        # Download distfiles
        files = challenge_data.get("files", [])
        if not files:
            # Fetch detail to get files
            try:
                detail = await self._client.get_challenge_detail(challenge_data.get("id", ""))
                files = detail.files
            except Exception as e:
                logger.warning("Failed to fetch challenge detail: %s", e)

        cid = challenge_data.get("id", "")
        for f in files:
            fname = f.get("name", "")
            if not fname:
                continue
            try:
                data = await self._client.download_attachment(cid, fname)
                if data:
                    (dist_dir / fname).write_bytes(data)
                    logger.info("Downloaded %s", fname)
                else:
                    logger.warning("No data for %s", fname)
            except Exception as e:
                logger.warning("Failed to download %s: %s", fname, e)

        return str(ch_dir)

    async def start_environment(self, challenge_id: str) -> Any:
        """Start a dynamic container for a challenge, returning its environment.

        Transparently forwards to the underlying client when supported; returns
        an empty environment-like object otherwise (engines treat empty entry as
        "no dynamic instance available").
        """
        if hasattr(self._client, "start_environment"):
            return await self._client.start_environment(challenge_id)
        # 平台不支持动态容器：返回空环境（entry="")
        return type("EmptyEnv", (), {"entry": "", "status": "", "raw": {}})()

    async def get_challenge_detail(self, challenge_id: str | int) -> Any:
        """Fetch detailed challenge info (includes dynamic instance entry)."""
        return await self._client.get_challenge_detail(challenge_id)

    async def close(self) -> None:
        await self._client.close()

    def diagnostics(self) -> dict[str, Any]:
        """Return platform diagnostics (underlying client's diagnostics if available)."""
        if hasattr(self._client, "diagnostics"):
            return self._client.diagnostics()
        return {"platform": type(self._client).__name__}
