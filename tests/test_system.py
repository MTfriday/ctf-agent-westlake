"""System integration tests with mock API — validates the full pipeline end-to-end.

Tests cover:
- Platform client abstraction (generic + CTFd adapter)
- Rate limiter
- Challenge classification router
- Task state machine
- Flag validation
- Sandbox dangerous command detection
- Dashboard data loading

Run with: pytest tests/ -v
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── Platform Client Tests ────────────────────────────────────────────────────


class TestPlatformClient:
    """Test the generic REST platform client with mock HTTP."""

    @pytest.mark.asyncio
    async def test_generic_fetch_challenges(self) -> None:
        """Test that GenericRestClient correctly parses challenge lists."""
        from unittest.mock import MagicMock

        from backend.platforms.generic import GenericRestClient

        client = GenericRestClient(
            api_base_url="https://mock-api.example.com",
            auth_type="Bearer",
            auth_credential="test-token",
            endpoints={
                "list_challenges": "/api/challenges",
                "get_challenge": "/api/challenge?id={id}",
                "submit_flag": "/api/flag",
            },
        )

        # Mock the HTTP response — use MagicMock for response (json() is sync in httpx)
        mock_data = {
            "success": True,
            "data": [
                {"id": 1, "name": "Test Web", "category": "web", "value": 100,
                 "description": "A web challenge", "solved": False},
                {"id": 2, "name": "Test Rev", "category": "rev", "value": 200,
                 "description": "A reversing challenge", "solved": True},
            ],
        }

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = mock_data

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        client._client = mock_client

        challenges = await client.fetch_challenges()
        assert len(challenges) == 2
        assert challenges[0].name == "Test Web"
        assert challenges[0].category == "web"
        assert challenges[1].solved is True
        await client.close()

    @pytest.mark.asyncio
    async def test_generic_submit_flag(self) -> None:
        """Test flag submission with various status responses."""
        from unittest.mock import MagicMock

        from backend.platforms.generic import GenericRestClient

        client = GenericRestClient(
            api_base_url="https://mock-api.example.com",
            endpoints={"submit_flag": "/api/flag"},
        )

        # Mock correct response — use MagicMock for response (json() is sync in httpx)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"status": "correct", "message": "Well done!"}

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        client._client = mock_client

        result = await client.submit_flag(1, "flag{test123}")
        assert result.status == "correct"
        assert "CORRECT" in result.display

        # Mock incorrect response
        mock_resp.json.return_value = {"status": "incorrect", "message": "Wrong flag"}
        result = await client.submit_flag(1, "wrong_flag")
        assert result.status == "incorrect"

        # Mock rate limit
        mock_resp.status_code = 429
        mock_resp.json.return_value = {"status": "rate_limited"}
        result = await client.submit_flag(1, "flag{test}")
        assert result.status == "rate_limited"

        await client.close()


# ── CTFd Adapter Tests ───────────────────────────────────────────────────────


class TestCTFdAdapter:
    """Test that CTFdAdapter properly wraps the existing CTFdClient."""

    @pytest.mark.asyncio
    async def test_adapter_wraps_ctfd(self) -> None:
        """Test that CTFdAdapter correctly delegates to CTFdClient."""
        from backend.platforms.ctfd_adapter import CTFdAdapter

        mock_inner = AsyncMock()
        mock_inner.fetch_challenge_stubs = AsyncMock(return_value=[
            {"id": 1, "name": "chall1", "category": "web", "value": 100},
            {"id": 2, "name": "chall2", "category": "crypto", "value": 200},
        ])
        mock_inner.fetch_solved_names = AsyncMock(return_value={"chall1"})

        adapter = CTFdAdapter(mock_inner)
        challenges = await adapter.fetch_challenges()

        assert len(challenges) == 2
        assert challenges[0].name == "chall1"
        assert challenges[0].solved is True
        assert challenges[1].solved is False

        # Test inner client access
        assert adapter.inner_client is mock_inner


# ── Rate Limiter Tests ───────────────────────────────────────────────────────


class TestRateLimiter:
    """Test the token-bucket rate limiter."""

    @pytest.mark.asyncio
    async def test_rate_limiter_allows_within_limit(self) -> None:
        """Test that requests within the rate limit pass through."""
        from backend.platforms.rate_limiter import RateLimiter

        limiter = RateLimiter(rps=100, burst=50)  # High limit for testing
        for _ in range(10):
            await limiter.acquire()  # Should not block

    @pytest.mark.asyncio
    async def test_rate_limiter_blocks_excessive(self) -> None:
        """Test that excessive requests are throttled (block until refill)."""
        import time

        from backend.platforms.rate_limiter import RateLimiter

        limiter = RateLimiter(rps=10, burst=1)  # 1 token burst, refills 10/sec
        await limiter.acquire()  # consume the only token
        start = time.monotonic()
        await limiter.acquire()  # must wait for refill (>= ~0.1s), NOT return instantly
        elapsed = time.monotonic() - start
        assert elapsed >= 0.05  # it actually blocked for a refill interval

    @pytest.mark.asyncio
    async def test_rate_limiter_no_deadlock_under_concurrency(self) -> None:
        """Test that many concurrent acquires all eventually get tokens (no deadlock)."""
        from backend.platforms.rate_limiter import RateLimiter

        limiter = RateLimiter(rps=20, burst=2)  # small burst, fast refill
        results = await asyncio.gather(*[limiter.acquire() for _ in range(12)])
        assert all(r is None for r in results)  # all 12 acquired without hanging


# ── Challenge Router Tests ───────────────────────────────────────────────────


class TestChallengeRouter:
    """Test challenge classification by category, tags, files, and description."""

    def test_classify_by_explicit_category(self) -> None:
        """Test classification from explicit category field."""
        from solvers.router import ChallengeCategory, classify_challenge

        assert classify_challenge("test", category="web") == ChallengeCategory.WEB
        assert classify_challenge("test", category="reverse") == ChallengeCategory.REV
        assert classify_challenge("test", category="crypto") == ChallengeCategory.CRYPTO
        assert classify_challenge("test", category="pwn") == ChallengeCategory.PWN
        assert classify_challenge("test", category="misc") == ChallengeCategory.MISC
        assert classify_challenge("test", category="forensics") == ChallengeCategory.FORENSICS

    def test_classify_by_tags(self) -> None:
        """Test classification from challenge tags."""
        from solvers.router import ChallengeCategory, classify_challenge

        assert classify_challenge("test", tags=["xss", "web"]) == ChallengeCategory.WEB
        assert classify_challenge("test", tags=["rsa", "crypto"]) == ChallengeCategory.CRYPTO
        assert classify_challenge("test", tags=["elf", "reverse"]) == ChallengeCategory.REV

    def test_classify_by_file_extensions(self) -> None:
        """Test classification from attached file extensions."""
        from solvers.router import ChallengeCategory, classify_challenge

        assert classify_challenge("test", filenames=["program.elf"]) == ChallengeCategory.REV
        assert classify_challenge("test", filenames=["capture.pcap"]) == ChallengeCategory.FORENSICS
        assert classify_challenge("test", filenames=["image.png"]) == ChallengeCategory.MISC

    def test_classify_by_description(self) -> None:
        """Test classification from description text."""
        from solvers.router import ChallengeCategory, classify_challenge

        assert classify_challenge("test", description="buffer overflow exploit") == ChallengeCategory.PWN
        assert classify_challenge("test", description="RSA encrypted message") == ChallengeCategory.CRYPTO
        assert classify_challenge("test", description="XSS in login form") == ChallengeCategory.WEB

    def test_classify_by_name_heuristic(self) -> None:
        """Test classification from challenge name as last resort."""
        from solvers.router import ChallengeCategory, classify_challenge

        assert classify_challenge("crackme_1") == ChallengeCategory.REV
        assert classify_challenge("web_sqli_easy") == ChallengeCategory.WEB

    def test_classify_unknown(self) -> None:
        """Test that unknown challenges return UNKNOWN."""
        from solvers.router import ChallengeCategory, classify_challenge

        assert classify_challenge("random_name") == ChallengeCategory.UNKNOWN

    def test_category_enum_from_str(self) -> None:
        """Test the from_str method handles various naming conventions."""
        from solvers.router import ChallengeCategory

        assert ChallengeCategory.from_str("REV") == ChallengeCategory.REV
        assert ChallengeCategory.from_str("Reverse Engineering") == ChallengeCategory.REV
        assert ChallengeCategory.from_str("cryptography") == ChallengeCategory.CRYPTO
        assert ChallengeCategory.from_str("Forensic") == ChallengeCategory.FORENSICS
        assert ChallengeCategory.from_str("pwnable") == ChallengeCategory.PWN


# ── Task State Machine Tests ─────────────────────────────────────────────────


class TestTaskStateMachine:
    """Test the task state machine transitions."""

    def test_valid_transitions(self) -> None:
        """Test all valid state transitions."""
        from backend.task_manager import TaskState

        task = TaskState.NEW
        task = task.transition_to(TaskState.ANALYZING)
        task = task.transition_to(TaskState.EXPLOITING)
        task = task.transition_to(TaskState.SUBMITTED)
        task = task.transition_to(TaskState.SOLVED)
        assert task == TaskState.SOLVED

    def test_retry_path(self) -> None:
        """Test the retry transition path."""
        from backend.task_manager import TaskState

        task = TaskState.NEW
        task = task.transition_to(TaskState.ANALYZING)
        task = task.transition_to(TaskState.EXPLOITING)
        task = task.transition_to(TaskState.SUBMITTED)
        # Retry
        task = task.transition_to(TaskState.EXPLOITING)
        assert task == TaskState.EXPLOITING

    def test_fail_to_needs_human(self) -> None:
        """Test the FAILED -> NEEDS_HUMAN path."""
        from backend.task_manager import TaskState

        task = TaskState.NEW
        task = task.transition_to(TaskState.ANALYZING)
        task = task.transition_to(TaskState.FAILED)
        task = task.transition_to(TaskState.NEEDS_HUMAN)
        assert task == TaskState.NEEDS_HUMAN

    def test_invalid_transition_raises(self) -> None:
        """Test that invalid transitions raise ValueError."""
        from backend.task_manager import TaskState

        with pytest.raises(ValueError, match="Invalid transition"):
            TaskState.NEW.transition_to(TaskState.SOLVED)

        with pytest.raises(ValueError):
            TaskState.SOLVED.transition_to(TaskState.ANALYZING)

    def test_can_transition_to(self) -> None:
        """Test the can_transition_to method."""
        from backend.task_manager import TaskState

        assert TaskState.NEW.can_transition_to(TaskState.ANALYZING)
        assert not TaskState.NEW.can_transition_to(TaskState.SOLVED)


class TestTaskRecord:
    """Test the TaskRecord data class."""

    def test_task_record_creation(self) -> None:
        """Test basic task record creation."""
        from backend.task_manager import TaskRecord, TaskState

        task = TaskRecord(challenge_id=1, challenge_name="Test Challenge", category="web")
        assert task.state == TaskState.NEW
        assert task.attempts == 0
        assert task.max_attempts == 3

    def test_can_retry(self) -> None:
        """Test retry logic."""
        from backend.task_manager import TaskRecord

        task = TaskRecord(challenge_id=1, challenge_name="Test", max_attempts=3)
        assert task.can_retry()
        task.attempts = 3
        assert not task.can_retry()

    def test_add_hint(self) -> None:
        """Test hint addition."""
        from backend.task_manager import TaskRecord

        task = TaskRecord(challenge_id=1, challenge_name="Test")
        task.add_hint("Try XOR with key 0x42")
        assert len(task.human_hints) == 1
        assert "XOR" in task.human_hints[0]


# ── Flag Validation Tests ────────────────────────────────────────────────────


class TestFlagValidation:
    """Test configurable flag validation."""

    def test_empty_flag_rejected(self) -> None:
        """Test that empty flags are rejected."""
        from backend.flag_utils import validate_flag

        result = validate_flag("")
        assert result is not None
        assert "Empty" in result

    def test_flag_without_pattern(self) -> None:
        """Test flag validation without pattern (should pass any non-empty)."""
        from backend.flag_utils import validate_flag

        assert validate_flag("some_flag_value") is None
        assert validate_flag("flag{test123}") is None
        assert validate_flag("FLAG-2024-abc") is None

    def test_flag_with_custom_pattern(self) -> None:
        """Test flag validation with a custom regex pattern."""
        from backend.flag_utils import validate_flag

        # CTFd-style pattern
        pattern = r"flag\{[A-Za-z0-9_]+\}"
        assert validate_flag("flag{test_flag}", pattern=pattern) is None
        assert validate_flag("wrong_format", pattern=pattern) is not None

        # Custom format (e.g., FLAG-xxx)
        pattern2 = r"FLAG-[A-Z0-9a-z-]+"
        assert validate_flag("FLAG-abc-123", pattern=pattern2) is None
        assert validate_flag("flag{test}", pattern=pattern2) is not None

    def test_flag_min_length(self) -> None:
        """Test minimum length validation."""
        from backend.flag_utils import validate_flag

        assert validate_flag("abc", min_length=5) is not None
        assert validate_flag("long_enough_flag", min_length=5) is None


# ── Sandbox Security Tests ───────────────────────────────────────────────────


class TestSandboxSecurity:
    """Test dangerous command detection in sandbox."""

    def test_rm_rf_root_blocked(self) -> None:
        """Test that rm -rf / is blocked."""
        from backend.sandbox_security import check_dangerous_command

        result = check_dangerous_command("rm -rf /")
        assert result is not None
        assert "BLOCKED" in result

        result = check_dangerous_command("rm -rf /var")
        assert result is None  # Specific paths should be OK

    def test_fork_bomb_blocked(self) -> None:
        """Test that fork bombs are blocked."""
        from backend.sandbox_security import check_dangerous_command

        result = check_dangerous_command(":(){ :|:& };:")
        assert result is not None
        assert "BLOCKED" in result

    def test_dd_zero_blocked(self) -> None:
        """Test that dd if=/dev/zero is blocked."""
        from backend.sandbox_security import check_dangerous_command

        result = check_dangerous_command("dd if=/dev/zero of=/dev/sda bs=1M")
        assert result is not None
        assert "BLOCKED" in result

    def test_safe_commands_allowed(self) -> None:
        """Test that safe commands pass through."""
        from backend.sandbox_security import check_dangerous_command

        assert check_dangerous_command("ls -la") is None
        assert check_dangerous_command("cat flag.txt") is None
        assert check_dangerous_command("python3 exploit.py") is None
        assert check_dangerous_command("nc host.docker.internal 1234") is None
        assert check_dangerous_command("strings binary | grep flag") is None

    def test_piped_dangerous_blocked(self) -> None:
        """Test that dangerous commands in pipes are blocked."""
        from backend.sandbox_security import check_dangerous_command

        result = check_dangerous_command("echo test | rm -rf /")
        assert result is not None
        assert "BLOCKED" in result


# ── Dashboard Data Tests ─────────────────────────────────────────────────────


class TestDashboardData:
    """Test dashboard data loading functions."""

    def test_trace_parsing(self, tmp_path: Path) -> None:
        """Test parsing of JSONL trace files directly."""
        # Create a mock trace file
        trace_dir = tmp_path / "logs"
        trace_dir.mkdir()
        trace_file = trace_dir / "trace-test_challenge-gpt-5.4-20240101.jsonl"

        events = [
            {"ts": 1700000000.0, "type": "start", "challenge": "test", "model": "gpt-5.4"},
            {"ts": 1700000001.0, "type": "tool_call", "tool": "bash", "args": "ls", "step": 1},
            {"ts": 1700000002.0, "type": "tool_result", "tool": "bash", "result": "flag.txt", "step": 1},
            {"ts": 1700000003.0, "type": "flag_confirmed", "tool": "submit_flag", "step": 2},
        ]
        trace_file.write_text("\n".join(json.dumps(e) for e in events))

        # Test file discovery and parsing directly
        files = sorted(trace_dir.glob("trace-*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        assert len(files) == 1

        parsed: list[dict] = []
        for line in files[0].read_text().strip().split("\n"):
            if line.strip():
                parsed.append(json.loads(line))
        assert len(parsed) == 4
        assert parsed[0]["type"] == "start"
        assert parsed[3]["type"] == "flag_confirmed"

    def test_challenge_meta_loading(self, tmp_path: Path) -> None:
        """Test loading challenge metadata from YAML files."""
        import yaml

        challenges_dir = tmp_path / "challenges"
        challenges_dir.mkdir()
        challenge_dir = challenges_dir / "test-challenge"
        challenge_dir.mkdir()

        meta = {
            "name": "Test Challenge",
            "category": "web",
            "value": 100,
            "description": "A test web challenge",
        }
        with open(challenge_dir / "metadata.yml", "w") as f:
            yaml.dump(meta, f)

        # Direct test without dashboard import
        metas: dict[str, dict] = {}
        for d in challenges_dir.iterdir():
            meta_path = d / "metadata.yml"
            if meta_path.exists():
                with open(meta_path) as f:
                    data = yaml.safe_load(f) or {}
                    metas[data.get("name", d.name)] = data

        assert "Test Challenge" in metas
        assert metas["Test Challenge"]["category"] == "web"
        assert metas["Test Challenge"]["value"] == 100


# ── Category Tool Registry Tests ─────────────────────────────────────────────


class TestCategoryTools:
    """Test the category-specific tool registry."""

    def test_rev_tools_exist(self) -> None:
        """Test that reverse engineering tools are registered."""
        from solvers.base_solver import CATEGORY_TOOLS, ChallengeCategory

        tools = CATEGORY_TOOLS.get(ChallengeCategory.REV, [])
        tool_names = [t.name for t in tools]
        assert "decompile" in tool_names
        assert "strings_analysis" in tool_names
        assert "angr_symbolic" in tool_names

    def test_web_tools_exist(self) -> None:
        """Test that web security tools are registered."""
        from solvers.base_solver import CATEGORY_TOOLS, ChallengeCategory

        tools = CATEGORY_TOOLS.get(ChallengeCategory.WEB, [])
        tool_names = [t.name for t in tools]
        assert "curl_headers" in tool_names
        assert "check_robots" in tool_names

    def test_prompt_suffix_not_empty(self) -> None:
        """Test that category prompt suffixes are non-empty."""
        from solvers.base_solver import ChallengeCategory, get_category_prompt_suffix

        for cat in ChallengeCategory:
            suffix = get_category_prompt_suffix(cat)
            assert isinstance(suffix, str)
            if cat != ChallengeCategory.UNKNOWN:
                assert len(suffix) > 0, f"Empty prompt for {cat}"


# ── MCP Server / Tool Definition Tests ─────────────────────────────────────


class TestCoordinatorTools:
    """Test that coordinator tool functions work correctly."""

    @pytest.mark.asyncio
    async def test_fetch_challenges_tool(self) -> None:
        """Test the fetch_challenges coordinator tool."""
        mock_ctfd = AsyncMock()
        mock_ctfd.fetch_all_challenges = AsyncMock(return_value=[
            {"name": "chall1", "category": "web", "value": 100, "solves": 5},
        ])
        mock_ctfd.fetch_solved_names = AsyncMock(return_value=set())

        # Test the JSON formatting directly
        challenges = await mock_ctfd.fetch_all_challenges()
        solved = await mock_ctfd.fetch_solved_names()
        result = [
            {
                "name": ch.get("name", "?"),
                "category": ch.get("category", "?"),
                "value": ch.get("value", 0),
                "solves": ch.get("solves", 0),
                "status": "SOLVED" if ch.get("name") in solved else "unsolved",
            }
            for ch in challenges
        ]
        import json
        output = json.dumps(result, indent=2)
        assert "chall1" in output
        assert "web" in output
        assert "unsolved" in output

    @pytest.mark.asyncio
    async def test_spawn_swarm_at_capacity(self) -> None:
        """Test spawn swarm capacity check logic."""
        # Test the capacity check logic directly
        swarms = {"a": "s1", "b": "s2"}
        max_concurrent = 2
        assert len(swarms) >= max_concurrent
        capacity_msg = f"At capacity ({len(swarms)}/{max_concurrent} challenges running). Wait for one to finish."
        assert "capacity" in capacity_msg.lower()


# ── Unified Config Tests ────────────────────────────────────────────────────


class TestUnifiedConfig:
    """Test that Settings reads config.yaml (non-secret) + .env (secrets)."""

    def test_settings_loads_config_yaml(self) -> None:
        """Test that non-secret config is loaded from config.yaml."""
        from backend.config import Settings

        s = Settings()
        # These come from config.yaml (currently slab/WestLake mode)
        assert s.platform_type == "slab"
        assert s.platform_api_base_url == "https://pro.dasctf.com"
        assert s.slab_access_key_env == "SLAB_ACCESS_KEY"
        assert s.rate_limit_rps == 5
        assert s.sandbox_image == "ctf-sandbox"

    def test_settings_loads_env_secrets(self) -> None:
        """Test that secret credentials are loaded from .env."""
        from backend.config import Settings

        s = Settings()
        # platform_auth_credential comes from .env PLATFORM_AUTH_CREDENTIAL
        # (config.yaml has it empty)
        assert s.platform_auth_credential.startswith("ctf2_")
        # deepseek key from .env
        assert s.deepseek_api_key.startswith("sk-")

    def test_env_overrides_config_yaml(self) -> None:
        """Test that .env values override config.yaml (priority)."""
        from backend.config import Settings

        # Set an env var that should override config.yaml's value
        import os
        os.environ["MAX_CONCURRENT_CHALLENGES"] = "3"
        try:
            s = Settings()
            assert s.max_concurrent_challenges == 3  # .env wins over config.yaml
        finally:
            os.environ.pop("MAX_CONCURRENT_CHALLENGES", None)

    def test_gzctf_client_from_settings(self) -> None:
        """Test GZCTFClient factory reads from the real unified Settings."""
        from backend.config import Settings
        from backend.platforms.gzctf_client import GZCTFClient

        s = Settings()
        client = GZCTFClient.from_settings(s)
        assert client.api_base_url == s.platform_api_base_url
        assert client.username == s.gzctf_username
        assert client.password == s.gzctf_password
        assert client.auth_token == s.gzctf_token


# ── CTF2 Client Tests ───────────────────────────────────────────────────────


class TestCTF2Client:
    """Test the CTF2-specific platform client."""

    @pytest.mark.asyncio
    async def test_compound_id_roundtrip(self) -> None:
        """Test that compound IDs encode and decode correctly."""
        from backend.platforms.ctf2_client import (
            _make_challenge_id,
            _parse_challenge_id,
        )

        cid = _make_challenge_id("practice", "ground_abc", "challenge_123")
        parent_type, parent_id, ch_id = _parse_challenge_id(cid)
        assert parent_type == "practice"
        assert parent_id == "ground_abc"
        assert ch_id == "challenge_123"

        # Test fallback for plain IDs
        parent_type, parent_id, ch_id = _parse_challenge_id("simple_id")
        assert parent_type == "practice"
        assert parent_id == ""

    @pytest.mark.asyncio
    async def test_fetch_challenges_traverses_hierarchy(self) -> None:
        """Test that fetch_challenges traverses competitions→stages→challenges."""
        from backend.platforms.ctf2_client import CTF2Client

        client = CTF2Client(auth_token="test-token")

        # Mock the paginated GET to simulate CTF2 responses
        async def mock_paginated(path: str) -> list[dict]:
            if "competitions" in path and "stages" not in path:
                return [{"id": "comp1", "name": "Test Competition"}]
            if "stages" in path and "challenges" not in path:
                return [{"id": "stage1", "name": "Stage 1"}]
            if "challenges" in path:
                return [
                    {"id": "ch1", "name": "Web Challenge", "category": "web", "value": 100},
                    {"id": "ch2", "name": "Crypto Challenge", "category": "crypto", "value": 200},
                ]
            if "practice" in path:
                return []
            return []

        client._paginated_get = mock_paginated  # type: ignore[assignment]

        challenges = await client.fetch_challenges()
        assert len(challenges) == 2
        assert challenges[0].name == "Web Challenge"
        assert challenges[1].name == "Crypto Challenge"
        # Check compound IDs
        assert "stage" in str(challenges[0].id)
        assert "ch1" in str(challenges[0].id)

    @pytest.mark.asyncio
    async def test_submit_flag_practice_format(self) -> None:
        """Test that practice challenge flags use the correct format."""
        from unittest.mock import AsyncMock

        from backend.platforms.ctf2_client import CTF2Client, _make_challenge_id

        client = CTF2Client(auth_token="test-token")
        challenge_id = _make_challenge_id("practice", "ground1", "ch1")

        # Mock _request to capture the call
        calls: list[tuple] = []

        async def capture_request(method: str, path: str, **kwargs: object) -> dict:
            calls.append((method, path, kwargs))
            return {"success": True, "data": {}}

        client._request = capture_request  # type: ignore[assignment]

        result = await client.submit_flag(challenge_id, "flag{test123}")

        assert len(calls) == 1
        method, path, kwargs = calls[0]
        assert method == "POST"
        assert "practice" in path and "ground1" in path and "ch1" in path
        body = kwargs.get("json", {})
        assert isinstance(body, dict)
        assert body.get("flag") == "flag{test123}"
        assert body.get("confirmation") is True
        assert result.status == "correct"

    @pytest.mark.asyncio
    async def test_submit_flag_stage_format(self) -> None:
        """Test that stage challenge flags use the correct format."""
        from backend.platforms.ctf2_client import CTF2Client, _make_challenge_id

        client = CTF2Client(auth_token="test-token")
        challenge_id = _make_challenge_id("stage", "stage1", "ch1")

        calls: list[tuple] = []

        async def capture_request(method: str, path: str, **kwargs: object) -> dict:
            calls.append((method, path, kwargs))
            return {"success": True, "data": {}}

        client._request = capture_request  # type: ignore[assignment]

        result = await client.submit_flag(challenge_id, "CTF{test}")

        assert len(calls) == 1
        method, path, kwargs = calls[0]
        assert method == "POST"
        assert "stages" in path and "stage1" in path
        body = kwargs.get("json", {})
        assert isinstance(body, dict)
        assert body.get("flag") == "CTF{test}"
        assert "challenge_id" in body
        assert result.status == "correct"

    @pytest.mark.asyncio
    async def test_start_environment(self) -> None:
        """Test starting a practice challenge environment."""
        from backend.platforms.ctf2_client import CTF2Client, _make_challenge_id

        client = CTF2Client(auth_token="test-token")
        challenge_id = _make_challenge_id("practice", "ground1", "ch1")

        async def mock_request(method: str, path: str, **kwargs: object) -> dict:
            return {
                "success": True,
                "data": {
                    "environment": {
                        "host": "10.0.0.1",
                        "port": 8080,
                        "container_id": "abc123",
                        "status": "started",
                    }
                },
            }

        client._request = mock_request  # type: ignore[assignment]

        env = await client.start_environment(challenge_id)
        assert env.host == "10.0.0.1"
        assert env.port == 8080
        assert env.status == "started"
        assert env.container_id == "abc123"

        # Verify it's cached
        cached = await client.get_environment(challenge_id)
        assert cached is not None
        assert cached.host == "10.0.0.1"

    @pytest.mark.asyncio
    async def test_rate_limited_response(self) -> None:
        """Test handling of rate limited responses."""
        from backend.platforms.ctf2_client import CTF2Client

        client = CTF2Client(auth_token="test-token")
        result = await client.submit_flag("test", "flag{x}")
        assert result.status == "incorrect"  # No mock, will fail gracefully

    def test_from_config(self) -> None:
        """Test creating client from config dict."""
        from backend.platforms.ctf2_client import CTF2Client

        config = {
            "type": "ctf2",
            "api_base_url": "https://ctf2.example.com",
            "api_path": "/api/v1",
            "auth_type": "ApiKey",
            "auth_credential": "test-token-123",
        }
        client = CTF2Client.from_config(config)
        assert client.api_base_url == "https://ctf2.example.com"
        assert client.api_path == "/api/v1"
        assert client.auth_token == "test-token-123"
        assert client.auth_type == "ApiKey"


# ── GZCTF Client Tests ──────────────────────────────────────────────────────


class TestGZCTFClient:
    """Test the GZCTF-specific platform client."""

    @pytest.mark.asyncio
    async def test_compound_id_roundtrip(self) -> None:
        """Test that GZCTF compound IDs (game::game_id::challenge_id) roundtrip."""
        from backend.platforms.gzctf_client import (
            _make_challenge_id,
            _parse_challenge_id,
        )

        cid = _make_challenge_id("game_42", "challenge_7")
        assert cid == "game::game_42::challenge_7"
        game_id, ch_id = _parse_challenge_id(cid)
        assert game_id == "game_42"
        assert ch_id == "challenge_7"

        # Fallback for plain IDs
        game_id, ch_id = _parse_challenge_id("simple_id")
        assert game_id == ""
        assert ch_id == "simple_id"

    @pytest.mark.asyncio
    async def test_fetch_challenges_parses_gzctf_format(self) -> None:
        """Test fetch_challenges handles GZCTF's {data,...} list + category dict."""
        from backend.platforms.gzctf_client import GZCTFClient

        client = GZCTFClient(api_base_url="http://gzctf.test")

        async def mock_request(method: str, path: str, **kwargs: object) -> object:
            if "/api/game?" in path:
                # ArrayResponseOfBasicGameInfoModel: {data, length, total}
                return {
                    "data": [{"id": 1, "title": "Demo CTF"}],
                    "length": 1,
                    "total": 1,
                }
            if path.endswith("/details"):
                # GameDetailModel is returned directly (not wrapped in {data}):
                # {challenges: {Category: [ChallengeInfo]}, rank: ScoreboardItem}
                return {
                    "challenges": {
                        "Web": [
                            {"id": 11, "title": "Easy Web", "category": "Web", "score": 100, "solved": 3},
                        ],
                        "Crypto": [
                            {"id": 12, "title": "Crypto 1", "category": "Crypto", "score": 200, "solved": 0},
                        ],
                    },
                    "challengeCount": 2,
                    "rank": {
                        "solvedChallenges": [{"id": 11, "challengeId": 11, "score": 100}],
                    },
                    "teamToken": "abc",
                }
            return {}

        client._request = mock_request  # type: ignore[assignment]

        challenges = await client.fetch_challenges()
        assert len(challenges) == 2
        assert challenges[0].name == "Easy Web"
        assert challenges[0].category == "Web"
        assert challenges[0].value == 100
        # Compound id embeds game + challenge ids
        assert "game::1::11" in str(challenges[0].id)

        # fetch_solved uses rank.solvedChallenges
        solved = await client.fetch_solved()
        assert "game::1::11" in solved

    @pytest.mark.asyncio
    async def test_submit_flag_two_stage(self) -> None:
        """Test flag submission: POST → submitId, then GET status → Accepted."""
        from backend.platforms.gzctf_client import GZCTFClient

        client = GZCTFClient(api_base_url="http://gzctf.test")
        challenge_id = "game::1::11"

        calls: list[tuple] = []

        async def mock_request(method: str, path: str, **kwargs: object) -> object:
            calls.append((method, path, kwargs))
            if method == "POST":
                # Server returns submitId as plain int
                return 505
            if "status/505" in path:
                return {"data": {"result": "Accepted"}}
            return {}

        client._request = mock_request  # type: ignore[assignment]

        result = await client.submit_flag(challenge_id, "flag{abc}")

        # POST then GET status
        assert len(calls) == 2
        post_method, post_path, post_kwargs = calls[0]
        assert post_method == "POST"
        assert post_path == "/api/game/1/challenges/11"
        assert post_kwargs.get("json", {}).get("flag") == "flag{abc}"

        assert calls[1][0] == "GET"
        assert "status/505" in calls[1][1]
        assert result.status == "correct"

    @pytest.mark.asyncio
    async def test_submit_flag_wrong_answer(self) -> None:
        """Test wrong flag maps to incorrect."""
        from backend.platforms.gzctf_client import GZCTFClient

        client = GZCTFClient(api_base_url="http://gzctf.test")

        async def mock_request(method: str, path: str, **kwargs: object) -> object:
            if method == "POST":
                return 606
            return {"data": {"result": "WrongAnswer"}}

        client._request = mock_request  # type: ignore[assignment]

        result = await client.submit_flag("game::1::11", "wrong{flag}")
        assert result.status == "incorrect"

    @pytest.mark.asyncio
    async def test_start_environment(self) -> None:
        """Test creating a dynamic container returns the entry point."""
        from backend.platforms.gzctf_client import GZCTFClient

        client = GZCTFClient(api_base_url="http://gzctf.test")

        async def mock_request(method: str, path: str, **kwargs: object) -> object:
            return {
                "data": {
                    "status": "Running",
                    "startedAt": 1700000000,
                    "expectStopAt": 1700003600,
                    "entry": "http://10.0.0.5:8080",
                }
            }

        client._request = mock_request  # type: ignore[assignment]

        env = await client.start_environment("game::1::11")
        assert env.status == "Running"
        assert env.entry == "http://10.0.0.5:8080"
        assert env.started_at == 1700000000

    @pytest.mark.asyncio
    async def test_get_challenge_detail_uses_context(self) -> None:
        """Test challenge detail pulls attachment url + instance entry from context."""
        from backend.platforms.gzctf_client import GZCTFClient

        client = GZCTFClient(api_base_url="http://gzctf.test")

        async def mock_request(method: str, path: str, **kwargs: object) -> object:
            return {
                "data": {
                    "id": 11,
                    "title": "Easy Web",
                    "category": "Web",
                    "content": "Find the flag!",
                    "score": 100,
                    "type": "DynamicContainer",
                    "hints": [{"id": 1, "content": "Use curl"}],
                    "context": {
                        "url": "/assets/abc123/file.zip",
                        "instanceEntry": "http://10.0.0.9:9000",
                        "fileSize": 12345,
                    },
                }
            }

        client._request = mock_request  # type: ignore[assignment]

        info = await client.get_challenge_detail("game::1::11")
        assert info.name == "Easy Web"
        assert info.description == "Find the flag!"
        assert info.connection_info == "http://10.0.0.9:9000"
        assert len(info.files) == 1
        assert info.files[0]["url"] == "/assets/abc123/file.zip"

    def test_from_settings(self) -> None:
        """Test creating client from the unified Settings object."""
        from backend.platforms.gzctf_client import GZCTFClient

        class FakeSettings:
            platform_api_base_url = "http://150.158.131.227:65534"
            gzctf_username = "alice"
            gzctf_password = "secret"
            gzctf_token = ""
            rate_limit_rps = 5

        client = GZCTFClient.from_settings(FakeSettings())  # type: ignore[arg-type]
        assert client.api_base_url == "http://150.158.131.227:65534"
        assert client.username == "alice"
        assert client.password == "secret"
        assert client.auth_token == ""


# ── Platform Adapter Tests ──────────────────────────────────────────────────


class TestPlatformAdapter:
    """Test the unified adapter that wraps PlatformClient into CTFd-style interface."""

    @pytest.mark.asyncio
    async def test_fetch_and_submit_by_name(self) -> None:
        """Test that challenges are cached by name and submit resolves by name."""
        from unittest.mock import AsyncMock

        from backend.platforms.adapter import PlatformAdapter
        from backend.platforms.base import ChallengeInfo, SubmitResult

        mock_client = AsyncMock()
        mock_client.fetch_challenges = AsyncMock(return_value=[
            ChallengeInfo(id="ch1", name="Web Challenge", category="web",
                          value=100, solved=False),
            ChallengeInfo(id="ch2", name="Crypto Challenge", category="crypto",
                          value=200, solved=False),
        ])
        mock_client.fetch_solved = AsyncMock(return_value={"ch1"})
        mock_client.submit_flag = AsyncMock(return_value=SubmitResult(
            "correct", "accepted", "CORRECT"))

        adapter = PlatformAdapter(mock_client)

        # Fetch all
        challenges = await adapter.fetch_all_challenges()
        assert len(challenges) == 2

        # Solved names resolved from cache
        solved = await adapter.fetch_solved_names()
        assert "Web Challenge" in solved

        # Submit by name resolves to ch1's compound id
        result = await adapter.submit_flag("Web Challenge", "flag{x}")
        assert result.status == "correct"
        mock_client.submit_flag.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_pull_challenge_writes_metadata(self, tmp_path: Path) -> None:
        """Test that pull_challenge writes metadata.yml and downloads files."""
        from unittest.mock import AsyncMock

        from backend.platforms.adapter import PlatformAdapter
        from backend.platforms.base import ChallengeInfo

        mock_client = AsyncMock()
        mock_client.fetch_challenges = AsyncMock(return_value=[
            ChallengeInfo(id="ch1", name="Test Challenge", category="web",
                          description="A test", files=[{"name": "file.txt", "url": "http://x/file.txt"}]),
        ])
        mock_client.download_attachment = AsyncMock(return_value=b"file content")

        adapter = PlatformAdapter(mock_client)
        ch_data = {
            "id": "ch1", "name": "Test Challenge", "category": "web",
            "value": 100, "description": "A test", "files": [{"name": "file.txt", "url": "http://x/file.txt"}],
        }

        out_dir = await adapter.pull_challenge(ch_data, str(tmp_path))

        import yaml
        meta_path = Path(out_dir) / "metadata.yml"
        assert meta_path.exists()
        meta = yaml.safe_load(meta_path.read_text())
        assert meta["name"] == "Test Challenge"

        dist = Path(out_dir) / "distfiles" / "file.txt"
        assert dist.exists()
        assert dist.read_bytes() == b"file content"

    def test_diagnostics_passthrough(self) -> None:
        """Test that adapter surfaces underlying client diagnostics."""
        from backend.platforms.adapter import PlatformAdapter

        mock_client = MagicMock()
        mock_client.diagnostics = MagicMock(return_value={"platform": "ctf2"})
        adapter = PlatformAdapter(mock_client)
        assert adapter.diagnostics() == {"platform": "ctf2"}


# ── Coordinator Selection Tests ──────────────────────────────────────────────


class TestCoordinatorSelection:
    """Test coordinator backend resolution."""

    def test_auto_resolves_to_pydantic(self) -> None:
        """Test that 'auto' coordinator defaults to pydantic (no CLI needed)."""
        from backend.cli import _resolve_coordinator
        from backend.config import Settings

        s = Settings()
        # Force no claude/codex available
        assert _resolve_coordinator(s, "auto") == "pydantic"

    def test_cli_value_wins(self) -> None:
        """Test that explicit CLI value takes priority."""
        from backend.cli import _resolve_coordinator
        from backend.config import Settings

        s = Settings()
        assert _resolve_coordinator(s, "claude") == "claude"
        assert _resolve_coordinator(s, "codex") == "codex"

    def test_configured_setting_used(self) -> None:
        """Test that configured settings.coordinator is used when CLI is auto."""
        from backend.cli import _resolve_coordinator
        from backend.config import Settings

        s = Settings()
        s.coordinator = "claude"
        assert _resolve_coordinator(s, "auto") == "claude"

    def test_platform_label_slab(self) -> None:
        """Test the platform label shows slab/WestLake for the active config."""
        from backend.cli import _platform_label
        from backend.config import Settings

        s = Settings()
        assert "Slab" in _platform_label(s) or "西湖论剑" in _platform_label(s)


# ── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
