"""Binary Exploitation solver — specializes in pwntools, ROP, and exploit development."""

from solvers.base_solver import (
    CATEGORY_TOOLS,
    ChallengeCategory,
    get_category_prompt_suffix,
    get_distfile_hints,
)

CATEGORY = ChallengeCategory.PWN

__all__ = ["CATEGORY", "category_prompt", "get_distfile_hints"]

category_prompt = get_category_prompt_suffix(CATEGORY)
category_tools = CATEGORY_TOOLS.get(CATEGORY, [])
