"""开发用适配器启动器 — 注入 mock 平台 + mock 引擎（不依赖 GZCTF）。

用法: .venv\\Scripts\\python.exe scripts\\run_muteki_dev.py   # :8001
"""

from __future__ import annotations

import uvicorn

from adapter.engines import MockEngineBackend
from adapter.main import create_app
from adapter.runtime import SolverRuntime


class FakePlatform:
    async def fetch_all_challenges(self):
        return [
            {"name": "cscs", "title": "CSCS", "category": "crypto", "description": "CSCS"},
            {"name": "dsasignaturedata", "title": "DSASignatureData", "category": "crypto", "description": "DSA"},
        ]

    async def submit_flag(self, problem_id, flag):
        return {"correct": str(flag).endswith("}")}

    async def get_challenge_status(self, problem_id):
        return {"solved": False}

    async def fetch_challenge(self, name):
        return {"name": name, "title": name, "category": "crypto", "description": name}

    async def close(self):
        pass


def make_app():
    rt = SolverRuntime(platform=FakePlatform(), no_submit=True)
    rt.engine = MockEngineBackend(rt)
    return create_app(runtime=rt)


if __name__ == "__main__":
    uvicorn.run(make_app(), host="127.0.0.1", port=8001, log_level="info")
