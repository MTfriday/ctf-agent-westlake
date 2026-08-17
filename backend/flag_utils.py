"""Flag validation utilities — standalone, no heavy imports."""

from __future__ import annotations

import re


# 平台 flag 外壳（西湖论剑规则：flag 格式为 DASCTF{} 或 flag{}，提交时仅需提交 {} 内内容）
_FLAG_SHELL_RE = re.compile(r"^(?:DASCTF|flag)\{(.*)\}$", re.IGNORECASE | re.DOTALL)


def normalize_flag(flag: str) -> str:
    """从完整 flag 外壳提取内部内容（提交时仅需提交 {} 内内容）。

    例如 "DASCTF{abc123}" → "abc123"，"flag{xyz}" → "xyz"（大小写不敏感）。
    非 DASCTF/flag 外壳（如 CTF{...} 或题目自定义特殊格式）原样返回，不做改动。
    """
    flag = flag.strip()
    m = _FLAG_SHELL_RE.match(flag)
    if m:
        return m.group(1).strip()
    return flag


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
