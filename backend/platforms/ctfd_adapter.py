"""CTFd platform adapter — wraps existing CTFdClient into PlatformClient interface."""

from __future__ import annotations

from typing import Any

from backend.ctfd import CTFdClient as CTFdClientImpl
from backend.ctfd import SubmitResult as CTFdSubmitResult
from backend.platforms.base import ChallengeInfo, PlatformClient, SubmitResult


class CTFdAdapter(PlatformClient):
    """Adapter that wraps the existing CTFdClient into the PlatformClient interface.

    This preserves all existing CTFd-specific logic while providing
    a uniform interface for the rest of the system.
    """

    def __init__(self, client: CTFdClientImpl) -> None:
        self._client = client

    @property
    def inner_client(self) -> CTFdClientImpl:
        """Access the underlying CTFdClient for advanced operations."""
        return self._client

    async def fetch_challenges(self) -> list[ChallengeInfo]:
        stubs = await self._client.fetch_challenge_stubs()
        solved = await self._client.fetch_solved_names()
        result: list[ChallengeInfo] = []
        for ch in stubs:
            result.append(ChallengeInfo(
                id=ch.get("id", 0),
                name=ch.get("name", "Unknown"),
                category=ch.get("category", ""),
                value=ch.get("value", 0),
                solved=ch.get("name") in solved,
                raw=ch,
            ))
        return result

    async def get_challenge_detail(self, challenge_id: str | int) -> ChallengeInfo:
        # CTFd detail requires name mapping; delegate to fetch_all_challenges
        challenges = await self._client.fetch_all_challenges()
        for ch in challenges:
            if ch.get("id") == challenge_id or ch.get("name") == challenge_id:
                return ChallengeInfo(
                    id=ch.get("id", 0),
                    name=ch.get("name", "Unknown"),
                    category=ch.get("category", ""),
                    description=ch.get("description", ""),
                    value=ch.get("value", 0),
                    tags=ch.get("tags", []),
                    connection_info=ch.get("connection_info", ""),
                    files=[{"name": f.get("name", ""), "url": f.get("url", "")} for f in ch.get("files", [])],
                    raw=ch,
                )
        raise ValueError(f"Challenge '{challenge_id}' not found")

    async def download_attachment(self, challenge_id: str | int, filename: str) -> bytes | None:
        # CTFd downloads are handled via pull_challenge mechanism
        # This is a placeholder — actual download goes through the CTFdClient
        return None

    async def submit_flag(self, challenge_id: str | int, flag: str) -> SubmitResult:
        # CTFd submit by challenge name (not ID in this implementation)
        name = challenge_id if isinstance(challenge_id, str) else str(challenge_id)
        result: CTFdSubmitResult = await self._client.submit_flag(name, flag)
        return SubmitResult(
            status=result.status,
            message=result.message,
            display=result.display,
        )

    async def fetch_solved(self) -> set[str | int]:
        return await self._client.fetch_solved_names()  # type: ignore[return-value]

    async def close(self) -> None:
        await self._client.close()
