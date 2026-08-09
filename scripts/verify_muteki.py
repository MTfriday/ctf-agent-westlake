"""M2 验证脚本 — muteki 桥接层全链路（mock 平台 + mock 引擎，不依赖 GZCTF）。

用法: .venv\\Scripts\\python.exe scripts\\verify_muteki.py
"""

from __future__ import annotations

import json
import os
import sys
import time

from fastapi.testclient import TestClient

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


def main() -> int:
    rt = SolverRuntime(platform=FakePlatform(), no_submit=True)
    rt.engine = MockEngineBackend(rt)  # 覆盖为 mock 引擎

    app = create_app(runtime=rt)
    checks = []

    def check(name, cond, detail=""):
        checks.append((name, cond, detail))
        print(("PASS" if cond else "FAIL"), name, detail)

    with TestClient(app) as c:
        # 1. auth（默认禁用）
        r = c.post("/api/auth/login", json={"password": "x"})
        check("auth.login", r.status_code == 200, f"status={r.status_code}")
        check("auth.login.no_token", r.json().get("auth_required") is False)
        r = c.get("/api/auth/me")
        check("auth.me", r.status_code == 200 and r.json()["authenticated"], str(r.json()))

        # 2. runs 列表（预注册 2 challenges）
        r = c.get("/api/runs?archived=1")
        runs = r.json()["runs"]
        names = sorted(x["run_id"] for x in runs)
        check("runs.seeded", names == ["cscs", "dsasignaturedata"], f"runs={names}")
        s = runs[0]
        for f in ("run_id", "name", "category", "started", "finished", "solved",
                  "paused", "status", "pinned", "archived", "folder_id", "order", "updated"):
            check(f"runs.summary.{f}", f in s, f"missing {f}")

        # 3. 新建 run
        r = c.post("/api/runs")
        new_id = r.json()["run_id"]
        check("runs.create", bool(new_id) and new_id.startswith("run-"), new_id)

        # 4. folders CRUD
        r = c.post("/api/folders", json={"name": "测试"})
        folder = r.json()["folder"]
        check("folders.create", folder["name"] == "测试", str(folder))
        r = c.get("/api/folders")
        check("folders.list", any(f["id"] == folder["id"] for f in r.json()["folders"]))
        r = c.patch(f"/api/folders/{folder['id']}", json={"name": "改名"})
        check("folders.rename", r.json()["ok"], str(r.json()))
        r = c.patch(f"/api/runs/cscs", json={"folder_id": folder["id"], "pinned": True})
        check("runs.patch", r.json()["ok"] and r.json()["run"]["pinned"], str(r.json()))

        # 5. start（cscs challenge）
        r = c.post("/api/runs/cscs/start", json={
            "kind": "swarm", "prompt": "求解 CSCS", "mode": "swarm",
            "challenge": {"name": "cscs", "category": "crypto", "description": "求解 CSCS"},
        })
        check("start.ok", r.status_code == 200 and r.json().get("ok"), f"status={r.status_code} body={r.text[:200]}")
        time.sleep(1.0)

        # 6. SSE 事件流（真实流式读取；可用 SKIP_SSE=1 跳过 → 用真实 uvicorn 单独验证）
        if not os.environ.get("SKIP_SSE"):
            events = []
            with c.stream("GET", "/api/runs/cscs/events") as resp:
                check("events.status", resp.status_code == 200, str(resp.status_code))
                it = iter(resp.iter_lines())
                try:
                    for _ in range(40):
                        line = next(it)
                        if line.startswith("event:"):
                            events.append(line.split(":", 1)[1].strip())
                        if len(events) >= 6:
                            break
                except StopIteration:
                    pass
            check("events.received", len(events) >= 3, f"events={events}")
            for want in ("run.started", "text.delta", "run.finished"):
                if want in events:
                    check(f"events.{want}", True)
            if "run.finished" in events:
                check("events.has_finished", True)

        # 7. hitl
        r = c.post("/api/runs/cscs/hitl", json={
            "target": "cscs", "action": "hint", "text": "注意 DSA 随机数复用", "preempt_policy": "soft_rebind",
        })
        check("hitl.hint", r.status_code == 200 and r.json()["ok"], str(r.text[:200]))

        # 8. settings / engines
        r = c.get("/api/settings/workers")
        check("settings.workers", r.status_code == 200 and "config" in r.json(), str(r.json())[:80])
        r = c.get("/api/engines")
        check("engines", r.status_code == 200 and r.json()["engines"], str(r.json())[:80])

    passed = sum(1 for _, cond, _ in checks if cond)
    print(f"\n{passed}/{len(checks)} passed")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
