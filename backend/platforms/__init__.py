"""Platform API client abstraction layer.

Provides a unified interface for interacting with different CTF competition platforms.
- CTFdAdapter: wraps existing CTFdClient
- GenericRestClient: configurable for any REST-based platform
- CTF2Client: specialized for ctf2.dasctf.com Open API
"""

from backend.platforms.base import PlatformClient, ChallengeInfo, SubmitResult

__all__ = ["PlatformClient", "ChallengeInfo", "SubmitResult"]
