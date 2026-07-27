"""Generic REST API client for arbitrary CTF competition platforms.

Reads endpoint templates from config.yaml and adapts to any platform
that exposes a REST API for challenge listing, detail, download, and flag submission.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx
import yaml

from backend.platforms.base import ChallengeInfo, PlatformClient, SubmitResult
from backend.platforms.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

USER_AGENT = "CTF-Agent/1.0"


class GenericRestClient(PlatformClient):
    """Generic REST API client for any CTF competition platform.

    Configuration is read from config.yaml's platform section:
        platform:
        type: generic
        api_base_url: "..."
        auth_type: "Bearer" | "ApiKey" | "Header"
        auth_credential: "..."
        endpoints:
            list_challenges: "..."
            get_challenge: "..."
            download_attachment: "..."
            submit_flag: "..."
    """

    def __init__(
        self,
        api_base_url: str,
        auth_type: str = "Bearer",
        auth_credential: str = "",
        endpoints: dict[str, str] | None = None,
        rate_limit_rps: int = 5,
    ) -> None:
        self.api_base_url = api_base_url.rstrip("/")
        self.auth_type = auth_type
        self.auth_credential = auth_credential
        self.endpoints = endpoints or {}
        self.rate_limiter = RateLimiter(rps=rate_limit_rps)

        self._client: httpx.AsyncClient | None = None

    @classmethod
    def from_config(cls, config_path: str | Path = "config.yaml") -> GenericRestClient:
        """Create a GenericRestClient from config.yaml."""
        with open(config_path) as f:
            cfg = yaml.safe_load(f) or {}
        plat = cfg.get("platform", {})
        rl = cfg.get("rate_limit", {})
        return cls(
            api_base_url=plat.get("api_base_url", ""),
            auth_type=plat.get("auth_type", "Bearer"),
            auth_credential=plat.get("auth_credential", ""),
            endpoints=plat.get("endpoints", {}),
            rate_limit_rps=rl.get("requests_per_second", 5),
        )

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            headers = {"User-Agent": USER_AGENT}
            if self.auth_type == "Bearer" and self.auth_credential:
                headers["Authorization"] = f"Bearer {self.auth_credential}"
            elif self.auth_type == "ApiKey" and self.auth_credential:
                headers["X-API-Key"] = self.auth_credential
            elif self.auth_type == "Header" and self.auth_credential:
                headers[self.auth_credential.split("=")[0]] = self.auth_credential.split("=", 1)[1]

            self._client = httpx.AsyncClient(
                base_url=self.api_base_url,
                headers=headers,
                timeout=30.0,
                verify=False,  # CTF platforms often use self-signed certs
            )
        return self._client

    def _build_url(self, endpoint_key: str, **params: Any) -> str:
        """Build URL from endpoint template, replacing {param} placeholders."""
        template = self.endpoints.get(endpoint_key, "")
        for k, v in params.items():
            template = template.replace(f"{{{k}}}", str(v))
        return template

    async def fetch_challenges(self) -> list[ChallengeInfo]:
        path = self._build_url("list_challenges")
        async with self.rate_limiter:
            client = await self._ensure_client()
            resp = await client.get(path)
            resp.raise_for_status()
        data = self._parse_response(resp.json())

        challenges: list[ChallengeInfo] = []
        for item in data if isinstance(data, list) else data.get("data", data.get("challenges", [])):
            challenges.append(ChallengeInfo(
                id=item.get("id", ""),
                name=item.get("name", item.get("title", "Unknown")),
                category=item.get("category", item.get("type", "")),
                description=item.get("description", ""),
                value=item.get("value", item.get("points", 0)),
                tags=item.get("tags", []),
                connection_info=item.get("connection_info", item.get("host", "")),
                solved=item.get("solved", False),
                raw=item,
            ))
        return challenges

    async def get_challenge_detail(self, challenge_id: str | int) -> ChallengeInfo:
        path = self._build_url("get_challenge", id=str(challenge_id))
        async with self.rate_limiter:
            client = await self._ensure_client()
            resp = await client.get(path)
            resp.raise_for_status()
        data = self._parse_response(resp.json())
        item = data if isinstance(data, dict) else {}
        # Some APIs wrap in a "data" field
        if isinstance(item, dict) and "data" in item:
            item = item["data"]

        return ChallengeInfo(
            id=item.get("id", challenge_id),
            name=item.get("name", item.get("title", "Unknown")),
            category=item.get("category", item.get("type", "")),
            description=item.get("description", ""),
            value=item.get("value", item.get("points", 0)),
            tags=item.get("tags", []),
            connection_info=item.get("connection_info", item.get("host", "")),
            files=[{"name": k, "url": v} for k, v in item.get("files", {}).items()]
            if isinstance(item.get("files"), dict)
            else item.get("files", []),
            raw=item,
        )

    async def download_attachment(self, challenge_id: str | int, filename: str) -> bytes | None:
        path = self._build_url("download_attachment", id=str(challenge_id))
        async with self.rate_limiter:
            client = await self._ensure_client()
            resp = await client.get(path)
            if resp.status_code != 200:
                logger.warning("Failed to download %s for challenge %s", filename, challenge_id)
                return None
            return resp.content

    async def submit_flag(self, challenge_id: str | int, flag: str) -> SubmitResult:
        path = self._build_url("submit_flag")
        body = {"challenge_id": str(challenge_id), "flag": flag.strip()}
        async with self.rate_limiter:
            client = await self._ensure_client()
            resp = await client.post(path, json=body)
            data = resp.json()
        data = self._parse_response(data) if isinstance(data, dict) else {"status": "unknown"}

        status = data.get("status", "unknown")
        message = data.get("message", data.get("msg", ""))

        if resp.status_code == 429:
            return SubmitResult("rate_limited", "Rate limited", "Rate limited — try again later")

        if status in ("correct", "success", "accepted"):
            return SubmitResult("correct", message, f"CORRECT — flag accepted. {message}".strip())
        if status in ("already_solved", "duplicate"):
            return SubmitResult("already_solved", message, f"ALREADY SOLVED — {message}".strip())
        if status in ("incorrect", "wrong", "failed"):
            return SubmitResult("incorrect", message, f"INCORRECT — flag rejected. {message}".strip())

        return SubmitResult("unknown", message, f"Unknown status: {status} — {message}")

    async def fetch_solved(self) -> set[str | int]:
        # Try to get from challenge list first
        try:
            challenges = await self.fetch_challenges()
            return {ch.id for ch in challenges if ch.solved}
        except Exception:
            return set()

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    def _parse_response(self, data: Any) -> Any:
        """Normalize API responses that wrap data in a success/data envelope."""
        if isinstance(data, dict):
            # Common patterns: {"success": true, "data": [...]} or {"code": 0, "data": {...}}
            if "data" in data and ("success" in data or "code" in data):
                return data["data"]
            if "challenges" in data:
                return data["challenges"]
        return data
