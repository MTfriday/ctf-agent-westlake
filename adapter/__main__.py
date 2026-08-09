"""`python -m adapter` — 启动 Aemeath 执行层适配器。"""

from __future__ import annotations

import logging

import uvicorn


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    from backend.config import Settings

    s = Settings()
    host = getattr(s, "adapter_host", "127.0.0.1")
    port = getattr(s, "adapter_port", 8001)

    from adapter.main import create_app

    print(f"🚀 Aemeath Adapter: http://{host}:{port}")
    print(f"   SSE 事件流:      http://{host}:{port}/api/events/stream")
    uvicorn.run(create_app(), host=host, port=port)


if __name__ == "__main__":
    main()
