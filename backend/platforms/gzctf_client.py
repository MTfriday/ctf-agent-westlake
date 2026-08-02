"""GZCTF platform client — adapted for GZCTF Server API (api-1.json).

Base URL example: http://150.158.131.227:65534/

Auth:
  - API token via `Authorization: Bearer <token>` (recommended for automation)
  - OR session login via POST /api/account/login (cookie-based)

Challenge flow (per game):
  GET  /api/game                        → list games
  GET  /api/game/{id}/details           → challenges grouped by category + rank (my solves)
  GET  /api/game/{id}/challenges/{cId}  → challenge detail (content, hints, attachment url)
  POST /api/game/{id}/challenges/{cId}  → submit flag, returns submitId
  GET  /api/game/{id}/challenges/{cId}/status/{submitId} → Accepted = correct
  POST /api/game/{id}/container/{cId}   → create dynamic container (instanceEntry)

Response format differs from CTF2: GZCTF uses {data, length, total} for lists
(no {success} envelope) and plain objects for single resources.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from backend.platforms.base import ChallengeInfo, PlatformClient, SubmitResult
from backend.platforms.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

USER_AGENT = "CTF-Agent/1.0 (GZCTFClient)"

# Compound challenge ID separator: game::<game_id>::<challenge_id>
ID_SEP = "::"

# GZCTF answer results
ACCEPTED = "Accepted"
FLAG_SUBMITTED = "FlagSubmitted"
WRONG_ANSWER = "WrongAnswer"


def _make_challenge_id(game_id: str, challenge_id: str) -> str:
    return f"game{ID_SEP}{game_id}{ID_SEP}{challenge_id}"


def _parse_challenge_id(compound_id: str) -> tuple[str, str]:
    """Parse compound ID into (game_id, challenge_id)."""
    parts = compound_id.split(ID_SEP, 2)
    if len(parts) == 3:
        return parts[1], parts[2]
    return "", str(compound_id)


@dataclass
class GZCTFEnvironment:
    """Dynamic container info for a challenge."""

    entry: str = ""          # connection info (host:port or URL)
    status: str = ""
    started_at: int = 0
    expect_stop_at: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


class GZCTFClient(PlatformClient):
    """Client for GZCTF-based CTF platforms."""

    def __init__(
        self,
        api_base_url: str = "",
        username: str = "",
        password: str = "",
        token: str = "",
        rate_limit_rps: int = 5,
    ) -> None:
        self.api_base_url = api_base_url.rstrip("/")
        self.username = username
        self.password = password
        self.auth_token = token
        self.rate_limiter = RateLimiter(rps=rate_limit_rps)

        self._client: httpx.AsyncClient | None = None
        self._authenticated: bool = False
        self._last_error: str = ""
        # game_id -> list of ChallengeInfo
        self._game_challenges: dict[str, list[ChallengeInfo]] = {}
        # compound_id -> ChallengeInfo
        self._by_id: dict[str, ChallengeInfo] = {}
        # compound_id -> GZCTFEnvironment
        self._env_cache: dict[str, GZCTFEnvironment] = {}
        # compound ids my team has solved
        self._solved_ids: set[str] = set()

    # ── Factory ──────────────────────────────────────────────────────────────

    @classmethod
    def from_settings(cls, settings: object) -> GZCTFClient:
        """Create from the unified Settings object."""
        return cls(
            api_base_url=getattr(settings, "platform_api_base_url", ""),
            username=getattr(settings, "gzctf_username", ""),
            password=getattr(settings, "gzctf_password", ""),
            token=getattr(settings, "gzctf_token", ""),
            rate_limit_rps=getattr(settings, "rate_limit_rps", 5),
        )

    # ── HTTP Client ──────────────────────────────────────────────────────────

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            headers = {"User-Agent": USER_AGENT}
            if self.auth_token:
                headers["Authorization"] = f"Bearer {self.auth_token}"
            self._client = httpx.AsyncClient(
                base_url=self.api_base_url,
                headers=headers,
                timeout=30.0,
                verify=False,
                follow_redirects=True,
            )
        return self._client

    async def _ensure_authenticated(self) -> None:
        """Login if a token isn't set and we have credentials."""
        if self._authenticated or self.auth_token:
            return
        if not (self.username and self.password):
            self._last_error = (
                "No GZCTF credentials configured. Set GZCTF_USERNAME/GZCTF_PASSWORD "
                "or GZCTF_TOKEN in .env."
            )
            raise RuntimeError(self._last_error)

        client = await self._ensure_client()
        resp = await client.post("/api/account/login", json={
            "userName": self.username,
            "password": self.password,
            "challenge": None,
        })
        # GZCTF login returns 200 on success and sets auth cookie
        if resp.status_code != 200:
            self._last_error = f"GZCTF login failed: HTTP {resp.status_code}"
            raise RuntimeError(self._last_error)
        self._authenticated = True
        logger.info("GZCTF logged in as %s", self.username)

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        """Make an authenticated API request with rate limiting."""
        await self._ensure_authenticated()
        async with self.rate_limiter:
            client = await self._ensure_client()
            resp = await client.request(method, path, **kwargs)

        if resp.status_code == 401:
            # Token may be revoked — retry once via fresh session if credentials exist
            self._authenticated = False
            await self._ensure_authenticated()
            async with self.rate_limiter:
                resp = await client.request(method, path, **kwargs)

        if resp.status_code == 429:
            return {"status": 429, "title": "Rate limited"}

        try:
            return resp.json()
        except Exception:
            return {"title": resp.text[:200], "status": resp.status_code}

    @staticmethod
    def _data_of(payload: Any, default: Any = None) -> Any:
        """Unwrap GZCTF envelope responses where present.

        GZCTF uses two envelope styles:
          - list endpoints: ArrayResponseOfX -> {data, length, total}
          - errors:         RequestResponse -> {title, data, status}
        Single resources (GameDetailModel / ChallengeDetailModel /
        ContainerInfoModel) are returned directly and MUST be kept intact,
        so we only unwrap a `data` key when the payload is not a known model.
        """
        if isinstance(payload, dict) and "data" in payload:
            # GameDetailModel / ChallengeDetailModel carry these keys — keep intact
            if not any(k in payload for k in ("challenges", "rank", "teamToken", "challengeCount", "content", "title")):
                return payload["data"]
        return payload

    async def _request_data(self, method: str, path: str, **kwargs: Any) -> Any:
        """Request and return the unwrapped `data` payload for envelope endpoints."""
        payload = await self._request(method, path, **kwargs)
        if isinstance(payload, dict) and "data" in payload:
            return payload["data"]
        return payload

    # ── Game & Challenge Listing ─────────────────────────────────────────────

    async def list_games(self) -> list[dict[str, Any]]:
        """List games (paginated via count/skip)."""
        games: list[dict[str, Any]] = []
        skip = 0
        page_size = 50
        while True:
            data = await self._request_data("GET", f"/api/game?count={page_size}&skip={skip}")
            if not isinstance(data, list):
                break
            games.extend(data)
            if len(data) < page_size:
                break
            skip += page_size
        return games

    async def get_game_details(self, game_id: str) -> dict[str, Any]:
        """Get game challenge details grouped by category + my rank/solves.

        Returns the full GameDetailModel: {challenges, rank, teamToken, ...}
        """
        data = await self._request("GET", f"/api/game/{game_id}/details")
        return self._data_of(data, {}) or {}

    async def get_challenge_detail(self, challenge_id: str | int) -> ChallengeInfo:
        """Fetch detailed info for a challenge (game::challenge compound id)."""
        compound_id = str(challenge_id)
        game_id, cid = _parse_challenge_id(compound_id)
        cached = self._by_id.get(compound_id)
        if not (game_id and cid):
            return cached or ChallengeInfo(id=compound_id, name="Unknown")

        try:
            data = await self._request("GET", f"/api/game/{game_id}/challenges/{cid}")
        except Exception as e:
            self._last_error = str(e)
            return cached or ChallengeInfo(id=compound_id, name="Unknown (fetch failed)")

        detail = self._data_of(data, {})
        if not isinstance(detail, dict):
            return cached or ChallengeInfo(id=compound_id, name="Unknown")

        # Detect a RequestResponse error like {title, data: null, status: 404}
        if "content" not in detail and "score" not in detail and isinstance(detail.get("data", ""), (int, type(None))):
            return cached or ChallengeInfo(id=compound_id, name="Unknown (fetch failed)")

        ctx = detail.get("context", {}) or {}
        info = ChallengeInfo(
            id=compound_id,
            name=detail.get("title", "Unknown"),
            category=detail.get("category", ""),
            description=detail.get("content", ""),
            value=detail.get("score", 0),
            tags=detail.get("hints", []),
            connection_info=ctx.get("instanceEntry", ""),
            files=[{"name": "attachment", "url": ctx.get("url", "")}] if ctx.get("url") else [],
            raw=detail,
        )
        self._by_id[compound_id] = info
        return info

    async def fetch_challenges(self) -> list[ChallengeInfo]:
        """Fetch all challenges across visible games.

        Uses GET /api/game/{id}/details which requires active team participation
        in that game (like CTF2, the team must have joined the game first).
        """
        self._game_challenges = {}
        self._by_id = {}
        all_challenges: list[ChallengeInfo] = []
        seen: set[str] = set()

        try:
            games = await self.list_games()
            for game in games:
                game_id = str(game.get("id", ""))
                if not game_id:
                    continue
                game_title = game.get("title", "?")
                try:
                    details = await self.get_game_details(game_id)
                except Exception as e:
                    self._last_error = (
                        f"Game {game_title} details failed: {e} — 需要先加入该比赛/队伍"
                    )
                    logger.warning("%s", self._last_error)
                    continue

                challenges_map = details.get("challenges", {}) or {}
                # My team's solved challenge ids come from rank.solvedChallenges.
                # ChallengeInfo.solved is the NUMBER of teams that solved it —
                # NOT whether my team solved it, so don't use it for that.
                my_solved = self._my_solved_ids(details, game_id)
                game_challenges: list[ChallengeInfo] = []
                if isinstance(challenges_map, dict):
                    for category, items in challenges_map.items():
                        for ch in (items or []):
                            ch_id = str(ch.get("id", ""))
                            compound_id = _make_challenge_id(game_id, ch_id)
                            name = ch.get("title", "Unknown")
                            if name in seen:
                                continue
                            seen.add(name)
                            info = ChallengeInfo(
                                id=compound_id,
                                name=name,
                                category=ch.get("category", category),
                                description="",
                                value=ch.get("score", 0),
                                solved=compound_id in my_solved,
                                raw={
                                    **ch,
                                    "_game_id": game_id,
                                    "_game_title": game_title,
                                    "_challenge_id": ch_id,
                                    "_solve_count": ch.get("solved", 0),
                                },
                            )
                            self._by_id[compound_id] = info
                            game_challenges.append(info)
                            all_challenges.append(info)
                self._game_challenges[game_id] = game_challenges
                self._solved_ids |= my_solved
        except Exception as e:
            self._last_error = f"fetch_challenges failed: {e}"
            logger.warning("%s", self._last_error)

        if not all_challenges and self._last_error:
            logger.warning("GZCTF fetched 0 challenges: %s", self._last_error)
        logger.info("Fetched %d challenges from GZCTF", len(all_challenges))
        return all_challenges

    @staticmethod
    def _my_solved_ids(details: dict[str, Any], game_id: str) -> set[str]:
        """Extract my team's solved challenge compound ids from GameDetailModel.rank."""
        rank = details.get("rank", {}) or {}
        solved: set[str] = set()
        for item in (rank.get("solvedChallenges", []) or []):
            cid = str(item.get("id", ""))
            if cid:
                solved.add(_make_challenge_id(game_id, cid))
        return solved

    async def fetch_solved(self) -> set[str | int]:
        """Set of solved challenge compound IDs (my team's rank.solvedChallenges)."""
        # Reuse data captured during fetch_challenges when possible
        if self._solved_ids and self._game_challenges:
            return set(self._solved_ids)
        solved: set[str | int] = set()
        for game_id, challenges in self._game_challenges.items():
            try:
                details = await self.get_game_details(game_id)
                solved |= self._my_solved_ids(details, game_id)
            except Exception:
                continue
        return solved

    # ── Attachment Download ──────────────────────────────────────────────────

    async def download_attachment(self, challenge_id: str | int, filename: str) -> bytes | None:
        """Download a challenge attachment from its URL (local /assets/... or remote)."""
        compound_id = str(challenge_id)
        cached = self._by_id.get(compound_id)
        if not cached or not cached.files:
            return None

        url = cached.files[0].get("url", "")
        if not url:
            return None

        try:
            async with self.rate_limiter:
                client = await self._ensure_client()
                if url.startswith("http"):
                    resp = await client.get(url)
                else:
                    resp = await client.get(url)  # relative to base_url
                if resp.status_code == 200:
                    return resp.content
        except Exception as e:
            logger.warning("Failed to download %s: %s", filename, e)
        return None

    # ── Flag Submission ──────────────────────────────────────────────────────

    async def submit_flag(self, challenge_id: str | int, flag: str) -> SubmitResult:
        """Submit a flag: POST then poll status until Accepted/WrongAnswer."""
        compound_id = str(challenge_id)
        game_id, cid = _parse_challenge_id(compound_id)
        flag = flag.strip()

        if not (game_id and cid):
            return SubmitResult("unknown", "bad id", f"Invalid challenge id: {compound_id}")
        if not flag:
            return SubmitResult("incorrect", "Empty flag", "Empty flag — nothing to submit.")

        try:
            # 1. Submit → returns submitId (int)
            resp = await self._request(
                "POST", f"/api/game/{game_id}/challenges/{cid}",
                json={"flag": flag},
            )
            submit_id = resp if isinstance(resp, int) else resp.get("data", resp.get("status"))

            if not isinstance(submit_id, int) or submit_id <= 0:
                msg = resp.get("title", "") if isinstance(resp, dict) else str(resp)
                return SubmitResult(
                    "incorrect", msg, f"INCORRECT — {msg}" if msg else "INCORRECT — flag rejected.",
                )

            # 2. Query status
            status = await self._request(
                "GET", f"/api/game/{game_id}/challenges/{cid}/status/{submit_id}",
            )
            result = self._data_of(status)
            if isinstance(result, str):
                result = {"result": result}
            answer = result.get("result", "") if isinstance(result, dict) else str(result)

            if answer == ACCEPTED or answer == FLAG_SUBMITTED:
                return SubmitResult("correct", answer, f"CORRECT — flag accepted.")
            if answer == WRONG_ANSWER:
                return SubmitResult("incorrect", answer, "INCORRECT — flag rejected.")
            if answer == "CheatDetected":
                return SubmitResult("incorrect", answer, "Cheat detected — submission rejected.")
            if answer == "NotFound":
                return SubmitResult("incorrect", answer, "Challenge not found.")

            return SubmitResult("unknown", answer, f"Unknown answer result: {answer}")

        except Exception as e:
            self._last_error = str(e)
            return SubmitResult("unknown", str(e), f"Submit error: {e}")

    # ── Dynamic Containers ───────────────────────────────────────────────────

    async def start_environment(self, challenge_id: str) -> GZCTFEnvironment:
        """Create a dynamic container for a challenge. Returns entry point."""
        compound_id = str(challenge_id)
        game_id, cid = _parse_challenge_id(compound_id)
        if not (game_id and cid):
            return GZCTFEnvironment()

        try:
            data = await self._request("POST", f"/api/game/{game_id}/container/{cid}")
        except Exception as e:
            self._last_error = str(e)
            return GZCTFEnvironment()

        payload = self._data_of(data, {})
        if not isinstance(payload, dict):
            return GZCTFEnvironment()

        env = GZCTFEnvironment(
            entry=payload.get("entry", ""),
            status=payload.get("status", ""),
            started_at=payload.get("startedAt", 0),
            expect_stop_at=payload.get("expectStopAt", 0),
            raw=payload,
        )
        self._env_cache[compound_id] = env
        return env

    async def extend_environment(self, challenge_id: str) -> GZCTFEnvironment:
        """Extend container lifetime."""
        game_id, cid = _parse_challenge_id(str(challenge_id))
        try:
            data = await self._request("POST", f"/api/game/{game_id}/container/{cid}/extend")
            payload = self._data_of(data, {})
            env = GZCTFEnvironment(
                entry=payload.get("entry", ""),
                status=payload.get("status", ""),
                raw=payload if isinstance(payload, dict) else {},
            )
            self._env_cache[str(challenge_id)] = env
            return env
        except Exception as e:
            self._last_error = str(e)
            return GZCTFEnvironment()

    # ── Misc ─────────────────────────────────────────────────────────────────

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    def diagnostics(self) -> dict[str, Any]:
        """Return diagnostics for troubleshooting."""
        return {
            "platform": "gzctf",
            "base_url": self.api_base_url,
            "auth": "token" if self.auth_token else ("session" if self.username else "none"),
            "last_error": self._last_error,
            "games_found": len(self._game_challenges),
            "challenges_cached": len(self._by_id),
        }
