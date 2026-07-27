"""CTF2 platform client — adapted for ctf2.dasctf.com Open API.

API Base: https://ctf2.dasctf.com/api/open/v1
Auth: X-CTF2-API-Key header (Personal Access Token)
      or Authorization: Bearer (OAuth2 access token)

Challenge hierarchy:
  Competitions → Stages → Challenges  (competition mode)
  Practice Grounds → Challenges        (practice mode)

Key differences from GenericRestClient:
  - Multi-level challenge traversal (no flat list endpoint)
  - Environment start required before connecting
  - Flag submission requires confirmation: true
  - Pagination uses page/page_size
  - Path-parameter-based IDs instead of query params
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from backend.platforms.base import ChallengeInfo, PlatformClient, SubmitResult
from backend.platforms.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

USER_AGENT = "CTF-Agent/1.0 (CTF2Client)"

# Separator for compound challenge IDs
ID_SEP = "::"


@dataclass
class CTF2Environment:
    """Information about a started challenge environment."""

    host: str = ""
    port: int = 0
    container_id: str = ""
    status: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


def _make_challenge_id(parent_type: str, parent_id: str, challenge_id: str) -> str:
    """Create a compound challenge ID that encodes parent context."""
    return f"{parent_type}{ID_SEP}{parent_id}{ID_SEP}{challenge_id}"


def _parse_challenge_id(compound_id: str) -> tuple[str, str, str]:
    """Parse compound ID back into (parent_type, parent_id, challenge_id)."""
    parts = compound_id.split(ID_SEP, 2)
    if len(parts) == 3:
        return tuple(parts)  # type: ignore[return-value]
    return ("practice", "", compound_id)


class CTF2Client(PlatformClient):
    """Client for the CTF2 platform (ctf2.dasctf.com)."""

    def __init__(
        self,
        api_base_url: str = "https://ctf2.dasctf.com",
        api_path: str = "/api/open/v1",
        auth_token: str = "",
        auth_type: str = "ApiKey",
        rate_limit_rps: int = 5,
    ) -> None:
        self.api_base_url = api_base_url.rstrip("/")
        self.api_path = api_path.rstrip("/")
        self.auth_token = auth_token
        self.auth_type = auth_type
        self.rate_limiter = RateLimiter(rps=rate_limit_rps)

        self._client: httpx.AsyncClient | None = None
        # Cache: compound_id -> ChallengeInfo
        self._challenge_cache: dict[str, ChallengeInfo] = {}
        # Cache: compound_id -> CTF2Environment
        self._env_cache: dict[str, CTF2Environment] = {}

    # ── Factory ──────────────────────────────────────────────────────────────

    @classmethod
    def from_config(cls, config: dict[str, Any] | None = None) -> CTF2Client:
        """Create from a config dict (usually from config.yaml's platform section)."""
        if config is None:
            import yaml
            from pathlib import Path
            with open(Path("config.yaml")) as f:
                cfg = yaml.safe_load(f) or {}
            config = cfg.get("platform", {})

        return cls(
            api_base_url=config.get("api_base_url", "https://ctf2.dasctf.com"),
            api_path=config.get("api_path", "/api/open/v1"),
            auth_token=config.get("auth_credential", ""),
            auth_type=config.get("auth_type", "ApiKey"),
            rate_limit_rps=config.get("rate_limit_rps", 5),
        )

    # ── HTTP Client ──────────────────────────────────────────────────────────

    def _build_url(self, path: str) -> str:
        """Build full URL from API base + path."""
        return f"{self.api_base_url}{self.api_path}{path}"

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            headers = {"User-Agent": USER_AGENT}
            if self.auth_type == "ApiKey":
                headers["X-CTF2-API-Key"] = self.auth_token
            elif self.auth_type == "Bearer" and self.auth_token:
                headers["Authorization"] = f"Bearer {self.auth_token}"

            self._client = httpx.AsyncClient(
                headers=headers,
                timeout=30.0,
                verify=False,
            )
        return self._client

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        """Make an API request with rate limiting and response parsing."""
        url = self._build_url(path)
        async with self.rate_limiter:
            client = await self._ensure_client()
            resp = await client.request(method, url, **kwargs)

        if resp.status_code == 429:
            logger.warning("Rate limited on %s %s", method, path)
            return {"success": False, "error": {"code": "RATE_LIMITED"}}

        resp.raise_for_status()
        data = resp.json()

        # Check for API-level error
        if isinstance(data, dict) and data.get("success") is False:
            err = data.get("error", {})
            logger.warning("CTF2 API error: %s — %s", err.get("code"), err.get("key"))
            return data

        return data

    async def _paginated_get(self, path: str, page_size: int = 100) -> list[dict[str, Any]]:
        """GET a paginated endpoint, collecting all pages."""
        all_items: list[dict[str, Any]] = []
        page = 1

        while True:
            sep = "&" if "?" in path else "?"
            page_path = f"{path}{sep}page={page}&page_size={page_size}"
            data = await self._request("GET", page_path)

            if not isinstance(data, dict) or not data.get("success"):
                break

            payload = data.get("data", {})
            items = payload.get("items", [])
            if not items:
                break

            all_items.extend(items)
            total = payload.get("total", 0)
            if len(all_items) >= total:
                break
            page += 1

        return all_items

    # ── Competition & Stage Traversal ────────────────────────────────────────

    async def list_competitions(self) -> list[dict[str, Any]]:
        """List all visible competitions."""
        return await self._paginated_get("/user/competitions/")

    async def list_competition_stages(self, competition_id: str) -> list[dict[str, Any]]:
        """List stages for a competition."""
        return await self._paginated_get(f"/user/competitions/{competition_id}/stages/")

    async def list_stage_challenges(self, stage_id: str) -> list[dict[str, Any]]:
        """List challenges in a stage."""
        return await self._paginated_get(f"/user/stages/{stage_id}/challenges/")

    async def list_practice_grounds(self) -> list[dict[str, Any]]:
        """List visible practice grounds."""
        return await self._paginated_get("/user/practice/")

    async def list_practice_challenges(self, practice_id: str) -> list[dict[str, Any]]:
        """List challenges in a practice ground."""
        # Practice ground detail returns challenge info
        path = f"/user/practice/{practice_id}/challenges/"
        return await self._paginated_get(path)

    async def get_practice_challenge_detail(self, practice_id: str, challenge_id: str) -> dict[str, Any]:
        """Get detailed info for a practice challenge."""
        data = await self._request("GET", f"/user/practice/{practice_id}/challenges/{challenge_id}/")
        if isinstance(data, dict) and data.get("success"):
            return data.get("data", {}) or {}
        return {}

    # ── Environment Management ───────────────────────────────────────────────

    async def start_environment(self, challenge_id: str) -> CTF2Environment:
        """Start a challenge environment. Returns connection info.

        For practice challenges, calls POST /practice/{id}/challenges/{challengeId}/environment/start/
        """
        parent_type, parent_id, cid = _parse_challenge_id(challenge_id)

        if parent_type == "practice" and parent_id:
            path = f"/user/practice/{parent_id}/challenges/{cid}/environment/start/"
            data = await self._request("POST", path, json={})
        else:
            logger.warning("Environment start not supported for challenge %s (no practice context)", challenge_id)
            return CTF2Environment()

        env_data = {}
        if isinstance(data, dict) and data.get("success"):
            env_data = data.get("data", {}).get("environment", {}) or {}

        env = CTF2Environment(
            host=env_data.get("host", env_data.get("ip", "")),
            port=env_data.get("port", 0),
            container_id=env_data.get("container_id", env_data.get("id", "")),
            status=env_data.get("status", "started"),
            raw=env_data,
        )
        self._env_cache[challenge_id] = env
        return env

    async def get_environment(self, challenge_id: str) -> CTF2Environment | None:
        """Get cached environment info for a challenge."""
        return self._env_cache.get(challenge_id)

    # ── PlatformClient Interface ─────────────────────────────────────────────

    async def fetch_challenges(self) -> list[ChallengeInfo]:
        """Fetch all visible challenges by traversing competitions and practice grounds.

        Returns challenges with compound IDs that encode parent context.
        """
        self._challenge_cache = {}
        all_challenges: list[ChallengeInfo] = []
        seen_names: set[str] = set()

        # Strategy 1: Traverse competitions → stages → challenges
        try:
            competitions = await self.list_competitions()
            for comp in competitions:
                comp_id = comp.get("id", "")
                stages = await self.list_competition_stages(comp_id)
                for stage in stages:
                    stage_id = stage.get("id", "")
                    stage_name = stage.get("name", "")
                    challenges = await self.list_stage_challenges(stage_id)
                    for ch in challenges:
                        ch_id = ch.get("id", "")
                        compound_id = _make_challenge_id("stage", stage_id, ch_id)
                        name = ch.get("name", ch.get("title", "Unknown"))
                        if name not in seen_names:
                            seen_names.add(name)
                        info = ChallengeInfo(
                            id=compound_id,
                            name=name,
                            category=ch.get("category", ch.get("type", "")),
                            description=ch.get("description", ""),
                            value=ch.get("value", ch.get("points", 0)),
                            tags=ch.get("tags", []),
                            solved=ch.get("solved", False),
                            connection_info=ch.get("connection_info", ""),
                            raw={
                                **ch,
                                "_parent_type": "stage",
                                "_parent_id": stage_id,
                                "_stage_name": stage_name,
                                "_competition_id": comp_id,
                                "_challenge_id": ch_id,
                            },
                        )
                        self._challenge_cache[compound_id] = info
                        all_challenges.append(info)
        except Exception as e:
            logger.warning("Competition traversal failed: %s", e)

        # Strategy 2: Traverse practice grounds → challenges
        try:
            grounds = await self.list_practice_grounds()
            for ground in grounds:
                ground_id = ground.get("id", "")
                ground_name = ground.get("name", "")
                challenges = await self.list_practice_challenges(ground_id)
                for ch in challenges:
                    ch_id = ch.get("id", "")
                    compound_id = _make_challenge_id("practice", ground_id, ch_id)
                    name = ch.get("name", ch.get("title", "Unknown"))
                    if name in seen_names:
                        # Skip if already found via competition path
                        continue
                    seen_names.add(name)
                    info = ChallengeInfo(
                        id=compound_id,
                        name=name,
                        category=ch.get("category", ch.get("type", "")),
                        description=ch.get("description", ""),
                        value=ch.get("value", ch.get("points", 0)),
                        tags=ch.get("tags", []),
                        solved=ch.get("solved", False),
                        connection_info=ch.get("connection_info", ""),
                        raw={
                            **ch,
                            "_parent_type": "practice",
                            "_parent_id": ground_id,
                            "_ground_name": ground_name,
                            "_challenge_id": ch_id,
                        },
                    )
                    self._challenge_cache[compound_id] = info
                    all_challenges.append(info)
        except Exception as e:
            logger.warning("Practice ground traversal failed: %s", e)

        logger.info("Fetched %d challenges from CTF2", len(all_challenges))
        return all_challenges

    async def get_challenge_detail(self, challenge_id: str | int) -> ChallengeInfo:
        """Get detailed challenge info.

        For practice challenges, fetches the full detail from the practice endpoint.
        For stage challenges, returns cached info from the listing.
        """
        compound_id = str(challenge_id)
        cached = self._challenge_cache.get(compound_id)
        parent_type, parent_id, cid = _parse_challenge_id(compound_id)

        if parent_type == "practice" and parent_id:
            detail = await self.get_practice_challenge_detail(parent_id, cid)
            if detail:
                info = ChallengeInfo(
                    id=compound_id,
                    name=detail.get("name", cached.name if cached else "Unknown"),
                    category=detail.get("category", ""),
                    description=detail.get("description", ""),
                    value=detail.get("value", detail.get("points", 0)),
                    tags=detail.get("tags", []),
                    connection_info=detail.get("connection_info", ""),
                    files=[
                        {"name": f.get("name", ""), "url": f.get("url", "")}
                        for f in (detail.get("files") or [])
                    ],
                    solved=detail.get("solved", False),
                    raw=detail,
                )
                self._challenge_cache[compound_id] = info
                return info

        # Fall back to cached
        if cached:
            return cached

        return ChallengeInfo(id=compound_id, name="Unknown (not fetched)")

    async def download_attachment(self, challenge_id: str | int, filename: str) -> bytes | None:
        """Download a challenge attachment.

        CTF2 provides file download URLs in challenge details. We fetch from the
        resolved URL rather than a dedicated download endpoint.
        """
        compound_id = str(challenge_id)
        cached = self._challenge_cache.get(compound_id)

        if cached and cached.files:
            for f in cached.files:
                if f.get("name") == filename or filename in f.get("url", ""):
                    url = f.get("url", "")
                    if not url:
                        continue
                    try:
                        async with self.rate_limiter:
                            client = await self._ensure_client()
                            resp = await client.get(url)
                            if resp.status_code == 200:
                                return resp.content
                    except Exception as e:
                        logger.warning("Failed to download %s: %s", filename, e)
                    return None

        logger.warning("Attachment %s not found for challenge %s", filename, challenge_id)
        return None

    async def submit_flag(self, challenge_id: str | int, flag: str) -> SubmitResult:
        """Submit a flag with CTF2's required confirmation field.

        For practice challenges:
          POST /practice/{id}/challenges/{challengeId}/submit/
          Body: {"flag": "...", "confirmation": true}

        For stage challenges (competition mode):
          POST /stages/{stageId}/submissions/
          Body: {"flag": "...", "challenge_id": "..."}
        """
        compound_id = str(challenge_id)
        flag = flag.strip()
        parent_type, parent_id, cid = _parse_challenge_id(compound_id)

        if not flag:
            return SubmitResult("incorrect", "Empty flag", "Empty flag — nothing to submit.")

        try:
            if parent_type == "practice" and parent_id:
                path = f"/user/practice/{parent_id}/challenges/{cid}/submit/"
                body = {"flag": flag, "confirmation": True}
            elif parent_type == "stage" and parent_id:
                path = f"/user/stages/{parent_id}/submissions/"
                body = {"flag": flag, "challenge_id": cid}
            else:
                # Fallback: try competition submission
                path = f"/user/submissions/"
                body = {"flag": flag}

            data = await self._request("POST", path, json=body)

            if isinstance(data, dict) and data.get("success"):
                return SubmitResult(
                    "correct",
                    "Flag accepted",
                    f"CORRECT — flag accepted for challenge.",
                )

            # Extract error info
            error = data.get("error", {}) if isinstance(data, dict) else {}
            code = error.get("code", "unknown")

            if code == "RATE_LIMITED":
                return SubmitResult("rate_limited", "Rate limited", "Rate limited — try again later.")

            if code in ("ALREADY_SOLVED", "DUPLICATE"):
                return SubmitResult("already_solved", "Already solved", "ALREADY SOLVED — flag was already submitted.")

            return SubmitResult(
                "incorrect",
                f"CTF2 error: {code}",
                f"INCORRECT — {error.get('key', 'flag rejected')}",
            )

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                return SubmitResult("rate_limited", "Rate limited", "Rate limited — try again later.")
            return SubmitResult("incorrect", str(e), f"Submit error: {e}")
        except Exception as e:
            logger.error("Submit flag error: %s", e, exc_info=True)
            return SubmitResult("unknown", str(e), f"Submit error: {e}")

    async def fetch_solved(self) -> set[str | int]:
        """Fetch set of solved challenge compound IDs."""
        solved: set[str | int] = set()
        try:
            submissions = await self._paginated_get("/user/submissions/")
            for sub in submissions:
                chal = sub.get("challenge", {}) or {}
                ch_id = chal.get("id", "")
                if ch_id:
                    # Match against cached challenges
                    for cid, info in self._challenge_cache.items():
                        raw = info.raw or {}
                        if raw.get("_challenge_id") == ch_id:
                            solved.add(cid)
                            break
                    # If not found in cache, add by submission challenge id
                    solved.add(ch_id)
        except Exception as e:
            logger.warning("Failed to fetch solved challenges: %s", e)
        return solved

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    # ── Convenience Methods ──────────────────────────────────────────────────

    async def ensure_environment(self, challenge_id: str) -> CTF2Environment:
        """Get or start an environment for a challenge."""
        existing = self._env_cache.get(challenge_id)
        if existing and existing.status == "started":
            return existing
        return await self.start_environment(challenge_id)
