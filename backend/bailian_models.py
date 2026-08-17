"""百炼官方模型目录查询（GET /api/v1/models）。

从 .env 的 BAILIAN_BASE_URL 自动推导 host（去掉 /compatible-mode/v1），
认证用 BAILIAN_API_KEY（sk-ws-*，即 DASHSCOPE_API_KEY）。
列表查询不消耗推理 token，即使 compatible-mode 直连配额耗尽（403）仍可查询。

支持按模型作者(providers)、模态类型(capabilities)、模型能力(features)、
上下文长度(context_window)、部署模式(service_site)、应用场景(supports)等条件筛选，
并返回模型定价与上下文长度信息。

CLI 用法：
    python -m backend.bailian_models --capabilities TG --page_size 100
    python -m backend.bailian_models --providers qwen --capabilities TG Reasoning
    python -m backend.bailian_models --features function-calling --min-context 100000
"""

from __future__ import annotations

import argparse
import sys
from typing import Any
from urllib.parse import urlparse

import httpx

from backend.config import Settings

# capabilities 可选值
CAPABILITIES = [
    "Reasoning", "VU", "IG", "VG", "ASR", "TTS", "ME", "Realtime-Omni",
    "Multimodal-Omni", "Realtime-Text-to-Speech", "TG", "TR",
    "Realtime-ASR", "Realtime-Audio-Translate", "3D-generation", "Realtime-Chatting",
]
# features 可选值
FEATURES = [
    "model-experience", "function-calling", "structured-outputs", "web-search",
    "prefix-completion", "cache", "batch", "fine-tuning",
]
# service_site 可选值
SERVICE_SITES = ["global", "international", "asia-pacific-china", "cn-hongkong",
                 "european-union", "united-states", "japan"]


def models_url(base_url: str) -> str:
    """从 BAILIAN_BASE_URL（.../compatible-mode/v1）推导 /api/v1/models 端点。"""
    p = urlparse(base_url)
    return f"{p.scheme}://{p.netloc}/api/v1/models"


def list_models(
    api_key: str,
    base_url: str,
    *,
    name: str | None = None,
    model: str | None = None,
    language: str | None = None,
    providers: list[str] | None = None,
    inference_providers: list[str] | None = None,
    capabilities: list[str] | None = None,
    features: list[str] | None = None,
    context_window: int | None = None,
    service_site: str | None = None,
    supports: list[str] | None = None,
    deployment_methods: list[str] | None = None,
    page_size: int = 100,
) -> list[dict[str, Any]]:
    """查询百炼模型目录，自动翻页拉全，返回模型列表（含 model_info / prices）。"""
    url = models_url(base_url)
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    params: dict[str, Any] = {"page_size": page_size}
    if name:
        params["name"] = name
    if model:
        params["model"] = model
    if language:
        params["language"] = language
    # 列表型参数：httpx 可传同 key 多值
    for key, vals in (
        ("providers", providers),
        ("inference_providers", inference_providers),
        ("capabilities", capabilities),
        ("features", features),
        ("supports", supports),
        ("deployment_methods", deployment_methods),
    ):
        if vals:
            params[key] = vals
    if context_window is not None:
        params["context_window"] = context_window
    if service_site:
        params["service_site"] = service_site

    collected: list[dict[str, Any]] = []
    total = None
    page = 1
    with httpx.Client(timeout=60) as client:
        while True:
            params["page_no"] = page
            r = client.get(url, params=params, headers=headers)
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:400]}")
            data = r.json()
            out = data.get("output") or {}
            if total is None:
                total = out.get("total")
            models = out.get("models") or []
            if not models:
                break
            collected.extend(models)
            page_size_actual = out.get("page_size") or page_size
            if page * page_size_actual >= (total or 0):
                break
            page += 1
    return collected


def format_models(models: list[dict[str, Any]]) -> list[str]:
    """把模型列表格式化为可读行（模型名/上下文/输入输出/定价/能力/特性）。"""
    lines: list[str] = []
    for m in sorted(models, key=lambda x: x.get("model", "")):
        mi = m.get("model_info") or {}
        cw = mi.get("context_window")
        max_in = mi.get("max_input_tokens")
        max_out = mi.get("max_output_tokens")
        s = (f"{m.get('model', ''):30} "
             f"ctx={cw if cw is not None else '?':>9} "
             f"in={max_in if max_in is not None else '?':>8} "
             f"out={max_out if max_out is not None else '?':>7}")
        pr = ""
        for pr_ in m.get("prices") or []:
            for it in pr_.get("prices") or []:
                if it.get("type") in ("input_token", "output_token"):
                    pr += f" {it.get('price_name', '?')}={it.get('price', '?')}/M"
        caps = ",".join(m.get("capabilities") or [])
        feats = ",".join(m.get("features") or [])
        lines.append(f"{s}  {pr[:34]:34} caps={caps[:24]:24} [{feats[:40]}]")
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="查询百炼官方模型目录")
    ap.add_argument("--name", help="按模型名称模糊搜索，如 qwen")
    ap.add_argument("--model", help="按模型 ID 精确查询，如 qwen3-max")
    ap.add_argument("--providers", nargs="*", help="模型作者，如 qwen deepseek")
    ap.add_argument("--capabilities", nargs="*", choices=CAPABILITIES, default=["TG"],
                    help="模态类型，默认 TG（文本生成）")
    ap.add_argument("--features", nargs="*", choices=FEATURES, help="模型能力")
    ap.add_argument("--min-context", type=int, dest="context_window",
                    help="最小上下文长度")
    ap.add_argument("--service-site", choices=SERVICE_SITES, dest="service_site",
                    help="部署模式")
    ap.add_argument("--supports", nargs="*", help="应用场景，如 inference deploy")
    ap.add_argument("--page-size", type=int, default=100, dest="page_size")
    args = ap.parse_args(argv)

    settings = Settings()
    if not settings.bailian_api_key or not settings.bailian_base_url:
        print("错误：未配置 BAILIAN_API_KEY / BAILIAN_BASE_URL（.env）", file=sys.stderr)
        return 1

    print("查询端点:", models_url(settings.bailian_base_url))
    try:
        models = list_models(
            settings.bailian_api_key,
            settings.bailian_base_url,
            name=args.name,
            model=args.model,
            providers=args.providers,
            capabilities=args.capabilities,
            features=args.features,
            context_window=args.context_window,
            service_site=args.service_site,
            supports=args.supports,
            page_size=args.page_size,
        )
    except Exception as e:
        print(f"查询失败：{e}", file=sys.stderr)
        return 1

    print(f"共 {len(models)} 个模型：")
    for line in format_models(models):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
