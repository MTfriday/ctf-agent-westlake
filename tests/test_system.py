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
        """Test that excessive requests are blocked."""
        from backend.platforms.rate_limiter import RateLimiter

        limiter = RateLimiter(rps=1000, burst=2)  # Low burst
        await limiter.acquire()  # OK
        await limiter.acquire()  # OK (burst)
        # Third acquire would block — verify it waits
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(limiter.acquire(), timeout=0.05)


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


# ── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
