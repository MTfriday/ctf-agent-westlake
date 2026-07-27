"""Sandbox command security — dangerous command detection.

Standalone module, no heavy Docker imports.
"""

from __future__ import annotations

import re

# Dangerous command patterns (regex) — blocked from execution in sandbox
DEFAULT_DANGEROUS_PATTERNS: list[re.Pattern] = [
    re.compile(r"^\s*rm\s+-rf\s+/\s*$"),          # rm -rf /
    re.compile(r"^\s*mkfs\."),                      # Format filesystems
    re.compile(r"^\s*dd\s+if=/dev/zero"),           # Overwrite with zeros
    re.compile(r"^\s*dd\s+if=/dev/random\s+of=/"),  # Overwrite with random
    re.compile(r"^\s*fdisk\s"),                     # Partition operations
    re.compile(r"^\s*:\(\s*\)\s*\{"),               # Fork bomb
    re.compile(r"^\s*chmod\s+-R?\s*0+\s+/"),        # chmod 0 /
    re.compile(r"^\s*>+\s+/dev/sda"),               # Direct block device write
    re.compile(r"^\s*mv\s+/\s+/dev/null"),           # Move root to null
]


def check_dangerous_command(command: str) -> str | None:
    """Check if a command matches dangerous patterns. Returns warning or None."""
    for pattern in DEFAULT_DANGEROUS_PATTERNS:
        if pattern.search(command):
            return (
                f"BLOCKED: Command matches dangerous pattern '{pattern.pattern}'. "
                "This command is not allowed in the sandbox."
            )
    # Check for nested dangerous patterns (within piped commands)
    for segment in command.split("|"):
        segment = segment.strip()
        for pattern in DEFAULT_DANGEROUS_PATTERNS:
            if pattern.search(segment):
                return (
                    f"BLOCKED: Sub-command '{segment[:50]}' matches dangerous pattern. "
                    "This command is not allowed in the sandbox."
                )
    return None
