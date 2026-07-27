"""Flag validation utilities — standalone, no heavy imports."""

from __future__ import annotations

import re


def validate_flag(flag: str, pattern: str = "", min_length: int = 1) -> str | None:
    """Validate flag format. Returns error message or None if valid.

    This is a standalone function that can be imported without triggering
    the full dependency chain (Docker SDK, etc.).

    Args:
        flag: The flag string to validate.
        pattern: Optional regex pattern. Empty string = no pattern validation.
        min_length: Minimum flag length.

    Returns:
        Error message string if invalid, None if valid.
    """
    flag = flag.strip()
    if not flag:
        return "Empty flag — nothing to submit."
    if len(flag) < min_length:
        return f"Flag too short ({len(flag)} < {min_length} chars)."
    if pattern:
        try:
            if not re.match(pattern, flag):
                return f"Flag does not match expected pattern: {pattern}"
        except re.error:
            pass  # invalid pattern, skip validation
    return None
