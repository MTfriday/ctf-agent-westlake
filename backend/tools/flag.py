"""Flag submission tool with configurable validation."""

from pydantic_ai import RunContext

from backend.deps import SolverDeps
from backend.flag_utils import normalize_flag, validate_flag
from backend.tools.core import do_submit_flag


async def submit_flag(ctx: RunContext[SolverDeps], flag: str) -> str:
    """Submit a flag to the platform to verify it. Always call this before reporting a flag.

    Returns CORRECT, ALREADY SOLVED, or INCORRECT.
    Do NOT submit placeholder flags like CTF{flag} or CTF{placeholder}.

    Flag format validation is configurable via flag_pattern and flag_min_length settings.
    """
    # 平台规则：DASCTF{}/flag{} 只提交 {} 内内容 → 先归一化再校验/提交
    flag = normalize_flag(flag)

    # Configurable flag validation
    pattern = getattr(ctx.deps, "flag_pattern", "")
    error = validate_flag(flag, pattern=pattern)
    if error:
        return error

    if ctx.deps.no_submit:
        return f'DRY RUN — would submit "{flag}" but --no-submit is set.'

    # Use deduped submission via swarm if available, otherwise direct call
    if ctx.deps.submit_fn:
        display, is_confirmed = await ctx.deps.submit_fn(flag)
    else:
        display, is_confirmed = await do_submit_flag(ctx.deps.ctfd, ctx.deps.challenge_name, flag)
    if is_confirmed:
        ctx.deps.confirmed_flag = flag
    return display
