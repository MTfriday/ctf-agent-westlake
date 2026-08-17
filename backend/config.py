"""统一配置 — 单一真相源。

优先级（高 → 低）:
  1. 环境变量 / .env 文件   (敏感凭证 + 本地覆盖)
  2. config.yaml            (非敏感结构配置，默认值)
  3. 代码内默认值

设计原则:
  - config.yaml: 提交到 git 的非敏感配置（平台端点、参数、开关）
  - .env:        不提交的敏感凭证（API Key、Token）
  - 敏感字段留空时由 .env 提供；config.yaml 中不要写密钥
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict


class YamlConfigSource(PydanticBaseSettingsSource):
    """从 config.yaml 加载非敏感配置，优先级低于 .env。

    config.yaml 的嵌套键通过 _SECTION_MAP 映射到 Settings 字段名。
    """

    # config.yaml 段 → (yaml键, settings字段名)
    _SECTION_MAP: dict[str, dict[str, str]] = {
        "platform": {
            "type": "platform_type",
            "auth_type": "platform_auth_type",
            "auth_credential": "platform_auth_credential",
            "api_base_url": "platform_api_base_url",
            "api_path": "platform_api_path",
            "endpoints": "platform_endpoints",
        },
        "solver": {
            "max_concurrent_challenges": "max_concurrent_challenges",
            "max_attempts_per_challenge": "max_attempts_per_challenge",
            "model_temperature": "model_temperature",
            "retry_delay_seconds": "retry_delay_seconds",
            "models": "models",
            "coordinator": "coordinator",
            "coordinator_model": "coordinator_model",
            "operator_msg_port": "operator_msg_port",
        },
        "flag": {
            "pattern": "flag_pattern",
            "min_length": "flag_min_length",
        },
        "rate_limit": {
            "enabled": "rate_limit_enabled",
            "requests_per_second": "rate_limit_rps",
            "burst_size": "rate_limit_burst",
        },
        "sandbox": {
            "image": "sandbox_image",
            "memory_limit": "container_memory_limit",
            "cpu_limit": "sandbox_cpu_limit",
            "dangerous_commands": "dangerous_commands",
            "network_whitelist": "network_whitelist",
        },
        "dashboard": {
            "enabled": "dashboard_enabled",
            "port": "dashboard_port",
            "refresh_interval_seconds": "dashboard_refresh_interval",
        },
        "logging": {
            "level": "logging_level",
            "trace_enabled": "trace_enabled",
        },
        "blackboard": {
            "db_path": "blackboard_db_path",
        },
        "adapter": {
            "host": "adapter_host",
            "port": "adapter_port",
            "engine_backend": "adapter_engine_backend",
        },
        "orchestrator": {
            "enabled": "orchestrator_enabled",
            "main_model": "orchestrator_main_model",
            "router_model": "orchestrator_router_model",
            "max_intents": "orchestrator_max_intents",
            "intent_timeout_seconds": "orchestrator_intent_timeout_seconds",
            "observe_interval_seconds": "orchestrator_observe_interval_seconds",
            "max_rounds": "orchestrator_max_rounds",
        },
    }

    def __init__(self, settings_cls: type[BaseSettings], yaml_path: str = "config.yaml") -> None:
        super().__init__(settings_cls)
        self.yaml_path = Path(yaml_path)
        self._data: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        """读取 config.yaml 并扁平化为 Settings 字段名。"""
        if not self.yaml_path.exists():
            return {}
        try:
            with open(self.yaml_path) as f:
                cfg = yaml.safe_load(f) or {}
        except Exception as e:
            logging.getLogger(__name__).warning("Failed to load %s: %s", self.yaml_path, e)
            return {}

        result: dict[str, Any] = {}
        for section, mapping in self._SECTION_MAP.items():
            section_data = cfg.get(section, {})
            if not isinstance(section_data, dict):
                continue
            for yaml_key, field_name in mapping.items():
                if yaml_key in section_data:
                    result[field_name] = section_data[yaml_key]
        return result

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        value = self._data.get(field_name)
        return value, field_name, False

    def __call__(self) -> dict[str, Any]:
        return {k: v for k, v in self._data.items() if v is not None}


class Settings(BaseSettings):
    """统一配置入口 — 读取 .env（凭证，高优先）+ config.yaml（结构，低优先）。"""

    # ========== 比赛平台 ==========
    # CTFd 模式（保留，供 CTFdClient 使用）
    ctfd_url: str = "http://localhost:8000"
    ctfd_user: str = "admin"
    ctfd_pass: str = "admin"
    ctfd_token: str = ""

    # 通用平台模式（config.yaml platform.*，secret 从 .env 提供）
    platform_type: str = "generic"          # "ctfd" | "generic" | "ctf2" | "gzctf"
    platform_auth_type: str = "Bearer"      # "Bearer" | "ApiKey" | "Header"
    platform_auth_credential: str = ""      # SECRET — 从 .env PLATFORM_AUTH_CREDENTIAL 读取
    platform_api_base_url: str = ""
    platform_api_path: str = "/api/open/v1"
    platform_endpoints: dict[str, str] = {}  # 仅 generic 模式使用

    # GZCTF 凭据（SECRET，来自 .env）
    gzctf_username: str = ""
    gzctf_password: str = ""
    gzctf_token: str = ""

    # ========== LLM API Keys（SECRET，来自 .env）==========
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    gemini_api_key: str = ""

    # DeepSeek (OpenAI 兼容)
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"

    # 阿里百炼 (OpenAI 兼容)
    bailian_api_key: str = ""
    bailian_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"

    # Provider-specific (optional, for Bedrock/Azure/Zen fallback)
    aws_region: str = "us-east-1"
    aws_bearer_token: str = ""
    azure_openai_endpoint: str = ""
    azure_openai_api_key: str = ""
    opencode_zen_api_key: str = ""

    # ========== 求解器（config.yaml solver.*）==========
    max_concurrent_challenges: int = 10
    max_attempts_per_challenge: int = 3
    model_temperature: float = 0.7
    retry_delay_seconds: int = 30
    # 模型选择（逗号分隔，如 "deepseek/deepseek-v4-flash,bailian/qwen-max"）
    # 留空 = 自动检测可用 provider
    models: str = ""

    # Coordinator 后端: "claude" | "codex" | "pydantic" | "auto"
    # pydantic 可用任意 OpenAI 兼容模型（DeepSeek/百炼），无需 claude/codex CLI
    coordinator: str = "auto"
    # Coordinator 使用的模型（仅 pydantic 后端生效），如 "deepseek/deepseek-v4-flash"
    coordinator_model: str = ""
    # 操作员消息 HTTP 端口（仪表盘 ↔ 引擎通信，两端必须一致）
    operator_msg_port: int = 9400

    # ========== Flag 配置（config.yaml flag.*）==========
    flag_pattern: str = ""  # Flag 校验正则（空=不校验）
    flag_min_length: int = 1

    # ========== 速率限制（config.yaml rate_limit.*）==========
    rate_limit_enabled: bool = True
    rate_limit_rps: int = 5
    rate_limit_burst: int = 10

    # ========== 沙箱（config.yaml sandbox.*）==========
    sandbox_image: str = "ctf-sandbox"
    container_memory_limit: str = "16g"
    sandbox_cpu_limit: int = 2
    dangerous_commands: list[str] = []
    network_whitelist: list[str] = []

    # ========== 仪表盘（config.yaml dashboard.*）==========
    dashboard_enabled: bool = True
    dashboard_port: int = 8501
    dashboard_refresh_interval: int = 3

    # ========== 日志（config.yaml logging.*）==========
    logging_level: str = "INFO"  # DEBUG | INFO | WARNING | ERROR
    trace_enabled: bool = True

    # ========== Aemeath 共享黑板（config.yaml blackboard.*）==========
    blackboard_db_path: str = "data/blackboard.db"

    # ========== Aemeath 执行层适配器（config.yaml adapter.*）==========
    adapter_host: str = "127.0.0.1"
    # 注意：8001/12345 在部分 Windows 上落在 Hyper-V/WSL 排除端口段（WinError 10013）无法 bind，
    # 故默认用 12346（可用端口）。如需其他端口改 config.yaml adapter.port。
    adapter_port: int = 12346
    adapter_engine_backend: str = "swarm"  # "swarm" | "mock"
    # 全自动求解：启动后自动扫描未解出题目并发起求解（跳过已解出，避免浪费 token）
    adapter_auto_solve: bool = True
    # 自动求解轮询间隔（秒）——定期检测平台新题/解出状态
    adapter_auto_poll_interval: float = 20.0

    # ========== Aemeath 总控（config.yaml orchestrator.*）==========
    orchestrator_enabled: bool = True
    orchestrator_main_model: str = "qwen3.8-max"
    orchestrator_router_model: str = "qwen3.7-flash"
    orchestrator_max_intents: int = 4
    orchestrator_intent_timeout_seconds: int = 120
    orchestrator_observe_interval_seconds: int = 10
    orchestrator_max_rounds: int = 20

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """优先级: init > 环境变量 > .env > config.yaml > file secrets"""
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            YamlConfigSource(settings_cls),
            file_secret_settings,
        )
