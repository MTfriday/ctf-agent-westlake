"""Type-specific CTF challenge solvers.

Each module provides category-specific tools and prompts for the solver backends.
The router classifies challenges by name/description/tags/files.
"""

from solvers.router import ChallengeCategory, classify_challenge

__all__ = ["ChallengeCategory", "classify_challenge"]
