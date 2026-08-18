"""轻量 OpenAI 兼容 LLM 客户端（阿里百炼 qwen）— 供 mode_router / planner 调用。

与 pydantic-ai 解耦，用 httpx 直调 /chat/completions，便于控制超时与 JSON 模式。
未配置 API Key 或网络失败时抛 LLMError，调用方回退到规则逻辑（离线可用）。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"


class LLMError(RuntimeError):
    """LLM 调用失败（未配 key / 网络错误 / 非 JSON 响应）。"""


def extract_json(text: str) -> dict[str, Any]:
    """从 LLM 输出中提取首个 JSON 对象（容错 markdown 代码块/前后缀文本）。"""
    cleaned = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise LLMError(f"no JSON object in LLM output: {text[:200]!r}")
    try:
        return json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError as e:
        raise LLMError(f"invalid JSON from LLM: {e}") from e


class OpenAICompatLLM:
    """OpenAI 兼容接口的轻量异步客户端。"""

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: str = "qwen3.8-max",
        timeout: float = 30.0,
        full_endpoint: bool = False,
    ) -> None:
        """full_endpoint=True：base_url 本身即完整 Chat Completions 端点
        （如西湖论剑 llm-gateway 代理），不再拼接 /chat/completions。
        """
        self.base_url = (base_url or _DEFAULT_BASE_URL).rstrip("/")
        self.api_key = api_key or ""
        self.model = model
        self.timeout = timeout
        self.full_endpoint = full_endpoint

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    async def chat(
        self,
        system: str,
        user: str,
        *,
        temperature: float = 0.2,
        json_mode: bool = False,
        max_tokens: Optional[int] = None,
    ) -> str:
        if not self.api_key:
            raise LLMError("no API key configured")
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if max_tokens:
            payload["max_tokens"] = max_tokens
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        try:
            # full_endpoint：base_url 本身即完整端点（llm-gateway 代理），不拼路径
            url = self.base_url if self.full_endpoint else f"{self.base_url}/chat/completions"
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
            return data["choices"][0]["message"]["content"]
        except LLMError:
            raise
        except Exception as e:  # noqa: BLE001
            raise LLMError(f"LLM request failed: {e}") from e

    async def chat_json(
        self, system: str, user: str, *, temperature: float = 0.0
    ) -> dict[str, Any]:
        text = await self.chat(system, user, temperature=temperature, json_mode=True)
        return extract_json(text)


def llm_from_settings(settings: Any, model: str) -> Optional[OpenAICompatLLM]:
    """从 Settings 构建 LLM；未配 key 返回 None（调用方走规则兜底）。

    优先走 gateway-bailian 代理通道（西湖论剑 llm-gateway，绕过 MaaS 直连配额），
    未配置时回退到百炼直连。
    """
    key = getattr(settings, "bailian_api_key", "") or getattr(settings, "openai_api_key", "")
    gateway = getattr(settings, "gateway_bailian_base_url", "") or ""
    bailian = getattr(settings, "bailian_base_url", "") or _DEFAULT_BASE_URL
    if gateway:
        # gateway-bailian base_url 是完整 Chat Completions 端点，不拼 /chat/completions
        llm = OpenAICompatLLM(base_url=gateway, api_key=key, model=model, full_endpoint=True)
    else:
        llm = OpenAICompatLLM(base_url=bailian, api_key=key, model=model)
    return llm if llm.available else None
