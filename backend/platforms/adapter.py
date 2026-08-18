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


def _safe_filename(name: str) -> str:
    """Keep original attachment basename but strip path / illegal chars."""
    base = Path(name).name.strip() or "attachment"
    base = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", base)
    return base or "attachment"


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

    async def fetch_notices(self) -> list[dict[str, Any]]:
        """公告列表（平台支持时透传；否则返回空列表）。"""
        client = self._client
        if hasattr(client, "fetch_notices"):
            try:
                return list(await client.fetch_notices() or [])
            except Exception as e:  # noqa: BLE001
                logger.warning("fetch_notices failed: %s", e)
                return []
        return []

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

        cid = str(challenge_data.get("id", "") or "")
        files = list(challenge_data.get("files") or [])
        description = str(challenge_data.get("description") or "")
        connection_info = str(challenge_data.get("connection_info") or "")
        value = challenge_data.get("value", 0)
        tags = list(challenge_data.get("tags") or [])
        category = challenge_data.get("category", "")

        # 列表接口常缺附件/描述：有 id 时强制补详情
        need_detail = (not files) or (not description) or (not connection_info) or (not value)
        if cid and need_detail:
            try:
                detail = await self._client.get_challenge_detail(cid)
                if not files:
                    files = list(detail.files or [])
                if not description:
                    description = str(detail.description or "")
                if not connection_info:
                    connection_info = str(detail.connection_info or "")
                if not value and getattr(detail, "value", 0):
                    value = detail.value
                if not tags and getattr(detail, "tags", None):
                    tags = list(detail.tags or [])
                if not category and getattr(detail, "category", ""):
                    category = detail.category
                # 缓存 id/name，便于后续 submit / 查找
                self._by_id[str(detail.id)] = detail
                if detail.name:
                    self._by_name[detail.name] = detail
            except Exception as e:
                logger.warning("Failed to fetch challenge detail for pull: %s", e)

        # Build metadata.yml
        meta = {
            "name": name,
            "id": cid,
            "category": category,
            "value": value,
            "description": description,
            "connection_info": connection_info,
            "tags": tags,
            "solves": challenge_data.get("solves", 0),
            "files": [
                {k: f.get(k) for k in ("name", "url", "ext", "key") if f.get(k)}
                for f in files
                if isinstance(f, dict)
            ],
        }
        meta_path = ch_dir / "metadata.yml"
        try:
            import yaml
            with open(meta_path, "w", encoding="utf-8") as f:
                yaml.safe_dump(meta, f, allow_unicode=True)
        except Exception as e:
            logger.warning("Failed to write metadata.yml: %s", e)

        # Download distfiles（传完整 meta，便于带 key/signature 的资源站下载）
        for f in files:
            if not isinstance(f, dict):
                continue
            fname = _safe_filename(str(f.get("name") or "attachment"))
            if not fname:
                continue
            try:
                # 优先传完整 dict；旧 client 只认 str 时回退 name/url
                data = None
                try:
                    data = await self._client.download_attachment(cid, f)  # type: ignore[arg-type]
                except TypeError:
                    data = await self._client.download_attachment(
                        cid, str(f.get("url") or f.get("name") or fname)
                    )
                if data:
                    dest = dist_dir / fname
                    dest.write_bytes(data)
                    logger.info("Downloaded %s (%d bytes)", fname, len(data))
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
