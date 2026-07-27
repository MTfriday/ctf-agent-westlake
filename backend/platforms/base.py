"""Abstract base class for CTF competition platform clients."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ChallengeInfo:
    """Standardized challenge info returned by all platform clients."""

    id: str | int
    name: str
    category: str = ""
    description: str = ""
    value: int = 0
    tags: list[str] = field(default_factory=list)
    connection_info: str = ""  # host:port or URL for the challenge service
    files: list[dict[str, str]] = field(default_factory=list)  # [{"name": ..., "url": ...}, ...]
    solved: bool = False
    raw: dict[str, Any] = field(default_factory=dict)  # original platform data


@dataclass
class SubmitResult:
    """Result of a flag submission."""

    status: str  # "correct" | "already_solved" | "incorrect" | "rate_limited" | "unknown"
    message: str
    display: str  # human-readable message


class PlatformClient(ABC):
    """Abstract interface for CTF competition platforms.

    Implementations: CTFdAdapter, GenericRestClient
    """

    @abstractmethod
    async def fetch_challenges(self) -> list[ChallengeInfo]:
        """Fetch all visible challenges from the platform."""
        ...

    @abstractmethod
    async def get_challenge_detail(self, challenge_id: str | int) -> ChallengeInfo:
        """Fetch detailed info for a single challenge."""
        ...

    @abstractmethod
    async def download_attachment(self, challenge_id: str | int, filename: str) -> bytes | None:
        """Download a challenge attachment file. Returns raw bytes."""
        ...

    @abstractmethod
    async def submit_flag(self, challenge_id: str | int, flag: str) -> SubmitResult:
        """Submit a flag for a challenge."""
        ...

    @abstractmethod
    async def fetch_solved(self) -> set[str | int]:
        """Return set of solved challenge IDs or names."""
        ...

    async def close(self) -> None:
        """Cleanup resources. Override if needed."""
        pass
