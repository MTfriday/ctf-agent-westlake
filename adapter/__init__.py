"""Aemeath 执行层适配器（FastAPI）。

实现蓝皮书「模块一」：平台工具函数、引擎调度、统一提交门禁、SSE 日志推送。
所有平台交互均封装为 LLM 可调用的工具函数（认知层 system_prompt.md 仅声明）。

启动::

    python -m adapter                 # 读取 config.yaml / .env 配置
    uvicorn adapter.main:app --port 8001
"""

from adapter.main import create_app

__all__ = ["create_app"]
