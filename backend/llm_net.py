"""LLM 网络层韧性：共享连接池、分阶段超时、并发信号量、指数退避重试。

背景问题：pydantic-ai 多模型并发求解时，每个 provider 各自创建 httpx 客户端
（`httpx.AsyncClient(transport=...)`），连接池互不共享、keep-alive 无法跨调用
复用；多个 solver × 多个模型并发启动会突发新建大量 TCP 连接，在 NAT/防火墙
处堆积，导致 `httpx.ConnectTimeout`（TCP 建连超时，而非读取超时）。

本模块提供：
- `get_shared_http_client()`：进程内共享的 httpx.AsyncClient（直连 API 用），
  连接池复用 + 分阶段超时（connect 单独拉长）。
- `get_shared_gateway_client()`：同上，但带网关端点路径裁剪 transport。
- `get_llm_semaphore()`：限制同时进行的 LLM 请求数。
- `classify_timeout()`：沿异常链区分连接超时 / 读取超时 / 写超时 / 连接池超时。
- `run_with_retry()`：捕获 ModelAPIError 的指数退避重试包装（agent.run 替换品）。

所有参数均可通过环境变量覆盖（括号内为默认值）：
  LLM_CONNECT_TIMEOUT (30)   TCP 建连超时（秒）——httpx/openai SDK 原默认 5s，
                            NAT 拥塞时太短，单独拉长
  LLM_READ_TIMEOUT (600)     读取/首字节超时（秒）——深度思考模型响应慢，需要长值
  LLM_WRITE_TIMEOUT (60)     写请求体超时（秒）
  LLM_POOL_TIMEOUT (30)      等待连接池空位超时（秒）
  LLM_MAX_CONNECTIONS (64)   连接池总连接上限
  LLM_MAX_KEEPALIVE (16)     每 host keep-alive 连接数
  LLM_MAX_CONCURRENT (8)     同时进行的 LLM 请求上限（信号量）
  LLM_RETRIES (3)            传输层失败重试次数（共 retries+1 次尝试）
  LLM_RETRY_BASE_DELAY (2.0) 指数退避基数（秒）
  LLM_RETRY_MAX_DELAY (30.0) 单次退避上限（秒）
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
from typing import Any

import httpx

logger = logging.getLogger(__name__)

__all__ = [
    "classify_timeout",
    "close_llm_clients",
    "get_llm_semaphore",
    "get_shared_gateway_client",
    "get_shared_http_client",
    "run_with_retry",
]


# ── 配置（环境变量可覆盖）──────────────────────────────────────────────

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


CONNECT_TIMEOUT = _env_float("LLM_CONNECT_TIMEOUT", 30.0)
READ_TIMEOUT = _env_float("LLM_READ_TIMEOUT", 600.0)
WRITE_TIMEOUT = _env_float("LLM_WRITE_TIMEOUT", 60.0)
POOL_TIMEOUT = _env_float("LLM_POOL_TIMEOUT", 30.0)
MAX_CONNECTIONS = _env_int("LLM_MAX_CONNECTIONS", 64)
MAX_KEEPALIVE = _env_int("LLM_MAX_KEEPALIVE", 16)
MAX_CONCURRENT = _env_int("LLM_MAX_CONCURRENT", 8)
RETRIES = _env_int("LLM_RETRIES", 3)
RETRY_BASE_DELAY = _env_float("LLM_RETRY_BASE_DELAY", 2.0)
RETRY_MAX_DELAY = _env_float("LLM_RETRY_MAX_DELAY", 30.0)


# ── 共享 httpx 客户端 ──────────────────────────────────────────────────

_http_client: httpx.AsyncClient | None = None
_gateway_client: httpx.AsyncClient | None = None
_semaphore: asyncio.Semaphore | None = None


def _make_timeout() -> httpx.Timeout:
    """分阶段超时：connect 单独拉长（NAT 拥塞场景），read 覆盖长思考响应。"""
    return httpx.Timeout(
        connect=CONNECT_TIMEOUT,
        read=READ_TIMEOUT,
        write=WRITE_TIMEOUT,
        pool=POOL_TIMEOUT,
    )


def _make_limits() -> httpx.Limits:
    return httpx.Limits(
        max_connections=MAX_CONNECTIONS,
        max_keepalive_connections=MAX_KEEPALIVE,
    )


def get_shared_http_client() -> httpx.AsyncClient:
    """进程内共享的直连 API 客户端（deepseek/bailian/openai/azure/zen 共用）。

    所有 OpenAI 兼容 provider 共用同一个 httpx.AsyncClient —— openai SDK 内部
    按 base_url 拆 host 各自维护连接池，共享 client 才能让 keep-alive 跨调用、
    跨模型复用，避免每次冷启动都新建 TCP + TLS 连接。
    """
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=_make_timeout(),
            limits=_make_limits(),
        )
    return _http_client


class _TrimCompletionsTransport(httpx.AsyncBaseTransport):
    """去掉 openai SDK 自动拼的 `/chat/completions`，把请求转发到完整网关端点。

    西湖论剑 llm-gateway 代理给出的 base_url 本身就是完整的 Chat Completions
    端点（POST 到该 URL 即完成对话），openai SDK 却会在其后拼接 `/chat/completions`
    （`.../e/TOKEN/chat/completions` → 404）。此 transport 在转发前把拼接的路径段
    去掉，使请求命中正确的代理端点。
    """

    def __init__(self) -> None:
        # 自定义 transport 下 client 层的 limits 不生效，需在这里显式传入
        self._transport = httpx.AsyncHTTPTransport(limits=_make_limits())

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            request.url = request.url.copy_with(
                path=request.url.path[: -len("/chat/completions")]
            )
        return await self._transport.handle_async_request(request)


def get_shared_gateway_client() -> httpx.AsyncClient:
    """进程内共享的网关客户端（gateway / gateway-bailian 用，带路径裁剪）。"""
    global _gateway_client
    if _gateway_client is None or _gateway_client.is_closed:
        _gateway_client = httpx.AsyncClient(
            timeout=_make_timeout(),
            transport=_TrimCompletionsTransport(),
        )
    return _gateway_client


async def close_llm_clients() -> None:
    """程序退出前关闭共享客户端（测试/优雅退出用）。"""
    global _http_client, _gateway_client
    for client in (_http_client, _gateway_client):
        if client is not None and not client.is_closed:
            await client.aclose()
    _http_client = _gateway_client = None


# ── 并发信号量 ─────────────────────────────────────────────────────────

def get_llm_semaphore() -> asyncio.Semaphore:
    """限制同时进行的 LLM 请求数，防止并发突发耗尽 NAT 连接/出口带宽。"""
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    return _semaphore


# ── 超时分类 ───────────────────────────────────────────────────────────

def classify_timeout(exc: BaseException) -> str | None:
    """沿异常链向下分类超时类型。

    返回 'connect' / 'read' / 'write' / 'pool' / 'connect-error'；非网络超时
    返回 None。用于日志区分"连接超时"与"读取超时"（排查方向完全不同：
    前者是出口网络/NAT/防火墙问题，后者是服务端慢响应）。
    """
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, httpx.ConnectTimeout):
            return "connect"
        if isinstance(cur, httpx.ReadTimeout):
            return "read"
        if isinstance(cur, httpx.WriteTimeout):
            return "write"
        if isinstance(cur, httpx.PoolTimeout):
            return "pool"
        if isinstance(cur, httpx.ConnectError):
            return "connect-error"
        cur = cur.__cause__ or cur.__context__
    return None


# ── 带指数退避重试的 agent.run ─────────────────────────────────────────

def _is_retryable(exc: Exception) -> tuple[bool, str | None]:
    """判断 ModelAPIError 是否值得重试，返回 (是否重试, 原因)。

    重试策略：
    - 传输层错误（ModelAPIError 非 ModelHTTPError 子类：ConnectTimeout /
      ReadTimeout / ConnectionError 等）→ 重试；
    - HTTP 429 / 5xx（ModelHTTPError 带 status_code）→ 重试（openai SDK
      内部已重试过 2 次仍失败，外层退避再兜底）；
    - 其余 HTTP 错误（400/401/403…业务错误）→ 不重试，立即抛出。
    """
    from pydantic_ai.exceptions import ModelHTTPError

    if isinstance(exc, ModelHTTPError):
        status = getattr(exc, "status_code", None)
        if status == 429 or (isinstance(status, int) and status >= 500):
            return True, "http-%s" % status
        return False, "http-%s" % status
    # 非 HTTP 的 ModelAPIError：传输层错误
    return True, classify_timeout(exc) or "transport"


async def run_with_retry(
    agent: Any,
    prompt: str | None,
    *,
    retries: int = RETRIES,
    base_delay: float = RETRY_BASE_DELAY,
    max_delay: float = RETRY_MAX_DELAY,
    **run_kwargs: Any,
) -> Any:
    """带指数退避重试的 agent.run（签名兼容 `Agent.run(prompt, **run_kwargs)`）。

    捕获 `pydantic_ai.exceptions.ModelAPIError`：
    - 传输层错误（连接超时/读取超时等）→ 指数退避重试；
    - HTTP 429/5xx → 指数退避重试；
    - 其余 HTTP 错误（400/401/403…）与其它异常 → 立即抛出，不重试。

    每次尝试都受全局信号量（LLM_MAX_CONCURRENT）约束；退避等待期间释放
    信号量，不占用并发槽位。message_history 由 pydantic-ai 在 run 开始时
    拷贝进内部 graph state（`_agent_graph.py`: `messages[:] =
    _clean_message_history(...)`），失败的尝试不会污染调用方传入的 history，
    因此重试无需快照恢复，也不会叠加重复消息。

    注意：信号量包裹整个 agent.run（含工具执行），长工具执行期间会占用
    一个并发槽位；solver 级并发通常只有个位数，默认 8 个槽位足够。
    """
    from pydantic_ai.exceptions import ModelAPIError

    semaphore = get_llm_semaphore()
    model_name = (
        getattr(agent, "name", None)
        or getattr(getattr(agent, "model", None), "model_name", None)
        or getattr(agent, "__class__", agent).__name__
    )

    for attempt in range(retries + 1):
        try:
            async with semaphore:
                return await agent.run(prompt, **run_kwargs)
        except ModelAPIError as exc:
            retryable, reason = _is_retryable(exc)
            if not retryable:
                logger.warning(
                    "LLM request [%s] failed (%s), no retry: %s",
                    model_name, reason, exc,
                )
                raise
            if attempt >= retries:
                logger.error(
                    "LLM request [%s] failed %d times (%s), giving up: %s",
                    model_name, retries + 1, reason, exc,
                )
                raise
            delay = min(max_delay, base_delay * (2 ** attempt))
            delay = random.uniform(delay / 2, delay) if delay > 0 else 0.0
            # 连接超时与读取超时分开记录：前者指向出口网络/NAT/防火墙问题，
            # 后者指向 API 服务端慢响应——排查方向完全不同。
            if reason == "connect":
                logger.warning(
                    "LLM CONNECT-TIMEOUT [%s] attempt %d/%d: %s; retrying in %.1fs",
                    model_name, attempt + 1, retries + 1, exc, delay,
                )
            elif reason in ("connect-error", "pool"):
                logger.warning(
                    "LLM CONNECTION-ERROR(%s) [%s] attempt %d/%d: %s; retrying in %.1fs",
                    reason, model_name, attempt + 1, retries + 1, exc, delay,
                )
            elif reason == "read":
                logger.warning(
                    "LLM READ-TIMEOUT [%s] attempt %d/%d: %s; retrying in %.1fs",
                    model_name, attempt + 1, retries + 1, exc, delay,
                )
            else:
                logger.warning(
                    "LLM request [%s] attempt %d/%d failed (%s): %s; retrying in %.1fs",
                    model_name, attempt + 1, retries + 1, reason, exc, delay,
                )
            await asyncio.sleep(delay)
    raise RuntimeError("unreachable")  # pragma: no cover
