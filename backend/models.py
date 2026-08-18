"""Model resolution — Bedrock, Azure OpenAI, Zen, Google AI Studio."""

from __future__ import annotations

from typing import TYPE_CHECKING

import boto3
import httpx
from pydantic_ai.models import Model
from pydantic_ai.models.bedrock import BedrockConverseModel, BedrockModelSettings
from pydantic_ai.models.google import GoogleModel, GoogleModelSettings
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
from pydantic_ai.providers.bedrock import BedrockProvider
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings

if TYPE_CHECKING:
    from backend.config import Settings


class _TrimCompletionsTransport(httpx.AsyncBaseTransport):
    """去掉 openai SDK 自动拼的 `/chat/completions`，把请求转发到完整网关端点。

    西湖论剑 llm-gateway 代理给出的 base_url 本身就是完整的 Chat Completions
    端点（POST 到该 URL 即完成对话），openai SDK 却会在其后拼接 `/chat/completions`
    （`.../e/TOKEN/chat/completions` → 404）。此 transport 在转发前把拼接的路径段
    去掉，使请求命中正确的代理端点。
    """

    def __init__(self) -> None:
        self._transport = httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            request.url = request.url.copy_with(
                path=request.url.path[: -len("/chat/completions")]
            )
        return await self._transport.handle_async_request(request)

# Default model specs — claude-sdk and codex providers use the new solver backends
DEFAULT_MODELS: list[str] = [
    "claude-sdk/claude-opus-4-6/medium",
    "claude-sdk/claude-opus-4-6/max",
    "codex/gpt-5.4",
    "codex/gpt-5.4-mini",
    "codex/gpt-5.3-codex",
]

# Fallback models for environments without claude/codex CLIs — uses OpenAI-compatible APIs
# 性价比默认：千问 qwen3.7-plus + 智谱 glm-4.6 + DeepSeek v4-flash
FALLBACK_MODELS: list[str] = [
    "gateway-bailian/qwen3.7-plus",
    "gateway-bailian/glm-4.6",
    # DeepSeek 暂走平台「百炼」代理（关闭 gateway/deepseek 专属代理）
    "gateway-bailian/deepseek-v4-flash",
    "gateway-bailian/qwen3.7-flash",
    "gateway-bailian/glm-4.7",
    "gateway-bailian/deepseek-v4-pro",
    "gateway/deepseek-v4-flash",  # 备用：恢复 deepseek 专属代理时可用
    "deepseek/deepseek-v4-flash",
    "bailian/qwen3.7-plus",
    "bailian/qwen3.7-flash",
    "openai/gpt-4o",
    "openai/gpt-4o-mini",
]


def _cli_available(cmd: str) -> bool:
    """Check if a CLI command is available on PATH."""
    import shutil
    return shutil.which(cmd) is not None


def _provider_ready(spec: str, settings: Settings) -> bool:
    """Check if a model spec's provider is actually usable."""
    provider = provider_from_spec(spec)

    # CLI-based providers strictly require the CLI on PATH.
    # The claude-sdk and codex solver backends drive the external CLI,
    # so an API key alone is NOT sufficient.
    if provider == "claude-sdk":
        return _cli_available("claude")
    if provider == "codex":
        return _cli_available("codex")

    # API-based providers need their API key configured
    api_key_map: dict[str, str] = {
        "openai": settings.openai_api_key,
        "deepseek": settings.deepseek_api_key,
        "gateway": settings.gateway_api_key,
        "gateway-bailian": settings.bailian_api_key,
        "bailian": settings.bailian_api_key,
        "azure": settings.azure_openai_api_key,
        "zen": settings.opencode_zen_api_key,
        "google": settings.gemini_api_key,
        "bedrock": settings.aws_bearer_token,
    }
    key = api_key_map.get(provider, "")
    if key:
        return True
    # Some providers have default base URLs; if the model is the fallback
    # and no key is set, it still won't work — require a key.
    return False


def resolve_model_specs(
    settings: Settings | None = None,
    cli_models: list[str] | None = None,
    require_available: bool = True,
) -> list[str]:
    """Resolve the final list of model specs to use.

    Priority:
      1. cli_models (--models flag)
      2. settings.models (comma-separated from .env / config.yaml)
      3. Auto-detect: DEFAULT_MODELS filtered to available providers,
         falling back to FALLBACK_MODELS if none of the defaults are usable.

    Args:
        settings: Optional Settings instance for provider detection.
        cli_models: Model specs passed via CLI --models flag.
        require_available: If True, filter to providers that are actually usable.
            Set False to force all models regardless of availability.
    """
    if settings is None:
        # Import lazily to avoid circular imports
        from backend.config import Settings
        settings = Settings()

    # 1. CLI flag takes highest priority
    if cli_models:
        return list(cli_models)

    # 2. Configured models from settings
    if settings.models.strip():
        return [s.strip() for s in settings.models.split(",") if s.strip()]

    # 3. Auto-detect
    if require_available:
        # Try defaults first, keep only usable ones
        usable = [s for s in DEFAULT_MODELS if _provider_ready(s, settings)]
        if usable:
            return usable
        # Fallback: use FALLBACK_MODELS that are usable
        usable_fb = [s for s in FALLBACK_MODELS if _provider_ready(s, settings)]
        if usable_fb:
            return usable_fb
        # Nothing usable — return fallbacks anyway so user sees clear error
        return list(FALLBACK_MODELS)

    return list(DEFAULT_MODELS)

# Context window sizes (tokens)
CONTEXT_WINDOWS: dict[str, int] = {
    "us.anthropic.claude-opus-4-6-v1": 1_000_000,
    "claude-opus-4-6": 1_000_000,
    "gpt-5.4": 1_000_000,
    "gpt-5.4-mini": 400_000,
    "gpt-5.3-codex": 1_000_000,
    "gpt-5.3-codex-spark": 128_000,
    "gemini-3-flash-preview": 1_000_000,
    # DeepSeek 新版模型
    "deepseek-v4-flash": 1_000_000,
    "deepseek-v4-pro": 1_000_000,
    # 阿里百炼通义千问（现行推荐）
    "qwen3.8-max": 1_000_000,
    "qwen3.7-plus": 1_000_000,
    "qwen3.7-flash": 1_000_000,
    # 智谱 GLM（百炼/gateway-bailian）
    "glm-4.6": 202_752,
    "glm-4.7": 202_752,
    "glm-4.5-air": 131_072,
    "glm-5": 202_752,
}

# Models that support vision
VISION_MODELS: set[str] = {
    "us.anthropic.claude-opus-4-6-v1",
    "claude-opus-4-6",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gemini-3-flash-preview",
}


def _build_gateway_model(model_id: str, base_url: str, api_key: str) -> OpenAIChatModel:
    """构建"平台 LLM 网关代理"模型。

    网关代理给出的 base_url 本身就是完整 Chat Completions 端点，openai SDK 却会
    在其后拼 `/chat/completions`（→ 404），因此必须用 `_TrimCompletionsTransport`
    在转发前去掉该路径段。
    """
    return OpenAIChatModel(
        model_id,
        provider=OpenAIProvider(
            base_url=base_url,
            api_key=api_key,
            http_client=httpx.AsyncClient(transport=_TrimCompletionsTransport()),
        ),
    )


def resolve_model(spec: str, settings: Settings) -> Model:
    """Resolve a 'provider/model_id' spec to a Pydantic AI Model."""
    provider = provider_from_spec(spec)
    model_id = model_id_from_spec(spec)
    match provider:
        case "bedrock":
            if settings.aws_bearer_token:
                return BedrockConverseModel(
                    model_id,
                    provider=BedrockProvider(
                        api_key=settings.aws_bearer_token,
                        region_name=settings.aws_region,
                    ),
                )
            else:
                session = boto3.Session()
                client = session.client("bedrock-runtime", region_name=settings.aws_region)
                return BedrockConverseModel(
                    model_id,
                    provider=BedrockProvider(bedrock_client=client),
                )
        case "azure":
            return OpenAIChatModel(
                model_id,
                provider=OpenAIProvider(
                    base_url=settings.azure_openai_endpoint,
                    api_key=settings.azure_openai_api_key,
                ),
            )
        case "zen":
            return OpenAIChatModel(
                model_id,
                provider=OpenAIProvider(
                    base_url="https://opencode.ai/zen/v1",
                    api_key=settings.opencode_zen_api_key,
                ),
            )
        case "deepseek":
            # DeepSeek — OpenAI 兼容接口
            return OpenAIChatModel(
                model_id,
                provider=OpenAIProvider(
                    base_url=settings.deepseek_base_url,
                    api_key=settings.deepseek_api_key,
                ),
            )
        case "gateway":
            # 平台 LLM 网关代理（西湖论剑 llm-gateway 代理的 DeepSeek）
            # base_url 是"完整 Chat Completions 端点"，openai SDK 会在其后拼
            # /chat/completions → 404，必须用 _TrimCompletionsTransport 去掉该路径段。
            return _build_gateway_model(
                model_id, settings.gateway_base_url, settings.gateway_api_key
            )
        case "gateway-bailian":
            # 平台 LLM 网关代理（西湖论剑 llm-gateway 代理的百炼）
            # 与 gateway 同理：完整端点 + trim transport；认证复用 BAILIAN_API_KEY。
            return _build_gateway_model(
                model_id, settings.gateway_bailian_base_url, settings.bailian_api_key
            )
        case "bailian":
            # 阿里百炼 — OpenAI 兼容接口
            return OpenAIChatModel(
                model_id,
                provider=OpenAIProvider(
                    base_url=settings.bailian_base_url,
                    api_key=settings.bailian_api_key,
                ),
            )
        case "google":
            return GoogleModel(
                model_id,
                provider=GoogleProvider(api_key=settings.gemini_api_key),
            )
        case "claude-sdk" | "codex":
            raise ValueError(
                f"Provider '{provider}' uses its own solver backend, not Pydantic AI. "
                f"resolve_model() should not be called for {spec}."
            )
        case _:
            raise ValueError(f"Unknown provider: {provider}")


def resolve_model_settings(spec: str) -> ModelSettings:
    """Get provider-specific model settings with caching enabled."""
    provider = spec.split("/", 1)[0]
    match provider:
        case "bedrock":
            return BedrockModelSettings(
                max_tokens=128_000,
                bedrock_cache_instructions=True,
                bedrock_cache_tool_definitions=True,
                bedrock_cache_messages=True,
            )
        case "deepseek":
            # DeepSeek — OpenAI 兼容接口
            # 注意：DeepSeek v4 的思考模式与 tool_choice='required' 冲突（400
            # "Thinking mode does not support this tool_choice"）。我们的 coordinator
            # 和 solver 都是工具调用型 agent，pydantic-ai 在需要强制工具时会发
            # tool_choice='required'，因此必须通过 reasoning_effort='none' 关闭思考。
            return OpenAIChatModelSettings(
                max_tokens=128_000,
                openai_reasoning_effort="none",
            )
        case "gateway":
            # 平台网关代理的 DeepSeek 与直连 DeepSeek 行为一致：思考模式与
            # tool_choice='required' 冲突，需用 reasoning_effort='none' 关闭思考。
            return OpenAIChatModelSettings(
                max_tokens=128_000,
                openai_reasoning_effort="none",
            )
        case "gateway-bailian":
            # 平台「百炼」代理（qwen/glm/deepseek 均可能默认开思考）：
            # tool_choice=required 与思考模式冲突 → 统一 reasoning_effort=none。
            # deepseek 也必须关思考（实测不关会 400 InvalidParameter tool_choice）。
            return OpenAIChatModelSettings(
                max_tokens=128_000,
                openai_reasoning_effort="none",
            )
        case "azure" | "zen":
            # Azure/Zen use OpenAI chat completions — server-side
            # prompt caching is automatic, no explicit config needed. Set max_tokens
            # to avoid reserving the full context window.
            return OpenAIChatModelSettings(
                max_tokens=128_000,
            )
        case "bailian":
            # 阿里百炼 qwen — OpenAI 兼容接口。注意：qwen3.x 默认开启思考模式，
            # 与 pydantic-ai 在需要强制工具时发的 tool_choice='required' 冲突（400
            # "tool_choice does not support being set to required in thinking mode"）。
            # 与 DeepSeek v4 同理，必须通过 reasoning_effort='none' 关闭思考，
            # 否则求解器一启动就会因 400 直接失败。
            return OpenAIChatModelSettings(
                max_tokens=128_000,
                openai_reasoning_effort="none",
            )
        case "google":
            return GoogleModelSettings(
                max_tokens=64_000,
                google_thinking_config={
                    "thinking_level": "high",
                    "include_thoughts": True,
                },
            )
        case _:
            return ModelSettings(max_tokens=128_000)


def model_id_from_spec(spec: str) -> str:
    """Extract just the model ID from a spec (strips effort suffix)."""
    parts = spec.split("/")
    return parts[1] if len(parts) >= 2 else spec


def provider_from_spec(spec: str) -> str:
    """Extract the provider from a spec."""
    return spec.split("/", 1)[0]


def effort_from_spec(spec: str) -> str | None:
    """Extract effort level from a spec like 'claude-sdk/claude-opus-4-6/max'."""
    parts = spec.split("/")
    if len(parts) >= 3 and parts[2] in ("low", "medium", "high", "max"):
        return parts[2]
    return None


def supports_vision(spec: str) -> bool:
    """Check if a model spec supports vision."""
    return model_id_from_spec(spec) in VISION_MODELS


def context_window(spec: str) -> int:
    """Get context window size for a model spec."""
    return CONTEXT_WINDOWS.get(model_id_from_spec(spec), 200_000)
