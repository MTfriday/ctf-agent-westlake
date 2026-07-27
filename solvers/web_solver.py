"""Web Security solver — specializes in HTTP analysis, injection, and web exploits."""

from solvers.base_solver import (
    CATEGORY_TOOLS,
    ChallengeCategory,
    get_category_prompt_suffix,
    get_distfile_hints,
)

CATEGORY = ChallengeCategory.WEB

__all__ = ["CATEGORY", "category_prompt", "get_distfile_hints"]

category_prompt = get_category_prompt_suffix(CATEGORY)
category_tools = CATEGORY_TOOLS.get(CATEGORY, [])
