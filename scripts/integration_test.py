"""P7 集成测试 — Aemeath muteki 桥接层全链路验证（mock 平台 + mock 引擎）。

覆盖：health / runs 预注册 / 新建 run / folders CRUD / settings(workers+rate) /
     start → SSE 事件流(含 flag) / hitl / credentials / workers / btw(SSE) /
     terminal(WS) / auth。

自包含：若 12346 空闲则自行启动 uvicorn（scripts/run_muteki_dev.py），测完关闭；
      若已被占用（dev 服务在跑）则直接复用，不关闭。

用法: .venv\\Scripts\\python.exe scripts\\integration_test.py
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time

import httpx
import websockets

BASE = "http://127.0.0.1:12346"
WS_BASE = "ws://127.0.0.1:12346"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _port_free(port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


class Reporter:
    def __init__(self) -> None:
        self.checks: list[tuple[str, bool, str]] = []

    def check(self, name: str, cond: bool, detail: str = "") -> None:
        self.checks.append((name, bool(cond), detail))
        print(("PASS" if cond else "FAIL"), name, detail)

    def summary(self) -> int:
        passed = sum(1 for _, ok, _ in self.checks if ok)
        print(f"\n{passed}/{len(self.checks)} passed")
        return 0 if passed == len(self.checks) else 1


def _event_names(events: list[str]) -> list[str]:
    return [e.split(":", 1)[1].strip() for e in events if e.startswith("event:")]


def main() -> int:
    rep = Reporter()
    proc: subprocess.Popen | None = None
    started_server = False

    if _port_free(12346):
        # 自行启动 uvicorn
        proc = subprocess.Popen(
            [sys.executable, os.path.join("scripts", "run_muteki_dev.py")],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        started_server = True

    cli = httpx.Client(trust_env=False, timeout=30)
    ready = False
    for _ in range(40):
        try:
            r = cli.get(BASE + "/api/health")
            if r.status_code == 200:
                ready = True
                break
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.5)
    rep.check("server.ready", ready)

    try:
        if not ready:
            return 1

        # ── auth ─────────────────────────────────────────────────────────
        r = cli.post(BASE + "/api/auth/login", json={"password": "x"})
        rep.check("auth.login", r.status_code == 200 and r.json().get("auth_required") is False)
        r = cli.get(BASE + "/api/auth/me")
        rep.check("auth.me", r.status_code == 200 and r.json()["authenticated"])

        # ── runs 预注册 ─────────────────────────────────────────────────
        r = cli.get(BASE + "/api/runs?archived=1")
        runs = r.json()["runs"]
        names = sorted(x["run_id"] for x in runs)
        rep.check("runs.seeded", "cscs" in names and "dsasignaturedata" in names, f"runs={names}")
        s = runs[0]
        fields = ("run_id", "name", "category", "started", "finished", "solved",
                  "paused", "status", "pinned", "archived", "folder_id", "order", "updated")
        rep.check("runs.summary", all(f in s for f in fields), f"missing={[f for f in fields if f not in s]}")

        # ── 新建 run + folders ──────────────────────────────────────────
        r = cli.post(BASE + "/api/runs")
        new_id = r.json().get("run_id", "")
        rep.check("runs.create", bool(new_id) and new_id.startswith("run-"), new_id)
        r = cli.post(BASE + "/api/folders", json={"name": "测试"})
        folder = r.json().get("folder") or {}
        rep.check("folders.create", bool(folder.get("id")), str(folder))
        r = cli.get(BASE + "/api/folders")
        rep.check("folders.list", any(f["id"] == folder.get("id") for f in r.json()["folders"]))
        r = cli.patch(BASE + f"/api/folders/{folder.get('id')}", json={"name": "改名"})
        rep.check("folders.rename", r.json().get("ok") is True)
        r = cli.patch(BASE + "/api/runs/cscs", json={"pinned": True})
        rep.check("runs.patch", r.json().get("ok") is True and r.json()["run"]["pinned"])

        # ── settings ────────────────────────────────────────────────────
        r = cli.get(BASE + "/api/settings/workers")
        cfg = r.json().get("config") or {}
        rep.check("settings.workers", "llm" in cfg and "agent" in cfg)
        rep.check("settings.llm.providers", len(cfg.get("llm", {}).get("providers", [])) >= 1)
        r = cli.get(BASE + "/api/settings/rate")
        rate = r.json().get("usd_cny", 0)
        rep.check("settings.rate", rate > 0, f"usd_cny={rate}")

        # ── start ───────────────────────────────────────────────────────
        r = cli.post(BASE + "/api/runs/cscs/start", json={
            "kind": "swarm", "prompt": "求解 CSCS", "challenge": {"name": "cscs", "description": "求解"},
        })
        rep.check("start", r.status_code == 200 and r.json().get("ok"), r.text[:120])
        time.sleep(2.2)

        # ── SSE 事件流 ─────────────────────────────────────────────────
        evs = []
        with cli.stream("GET", BASE + "/api/runs/cscs/events", timeout=20) as resp:
            rep.check("sse.status", resp.status_code == 200)
            for line in resp.iter_lines():
                if line.startswith("event:"):
                    evs.append(line)
                    if len(evs) >= 8:
                        break
                if line.startswith("data:") and '"run.finished"' in line:
                    evs.append("event: run.finished")
                    break
        e_names = _event_names(evs)
        for want in ("run.started", "text.delta", "run.finished"):
            rep.check(f"sse.{want}", want in e_names, f"events={e_names}")

        # ── hitl ────────────────────────────────────────────────────────
        r = cli.post(BASE + "/api/runs/cscs/hitl", json={
            "target": "cscs", "action": "hint", "text": "注意 DSA 随机数复用", "preempt_policy": "soft_rebind",
        })
        rep.check("hitl", r.status_code == 200 and r.json().get("ok"))

        # ── credentials ─────────────────────────────────────────────────
        r = cli.get(BASE + "/api/runs/cscs/credentials")
        creds = r.json().get("credentials") or []
        rep.check("credentials", r.status_code == 200 and any("flag" in (c.get("content") or "").lower() for c in creds), f"n={len(creds)}")

        # ── workers ─────────────────────────────────────────────────────
        r = cli.post(BASE + "/api/runs/cscs/workers", json={})
        rep.check("workers.spawn", r.status_code == 200 and r.json().get("ok"))
        r = cli.request("DELETE", BASE + "/api/runs/cscs/workers",
                        headers={"Content-Type": "application/json"},
                        content=json.dumps({"solver_id": "s1"}))
        rep.check("workers.kill", r.status_code == 200 and r.json().get("ok"))

        # ── btw（SSE 流式）──────────────────────────────────────────────
        deltas = []
        with cli.stream("POST", BASE + "/api/runs/cscs/btw",
                        json={"question": "DSA 签名是什么？"}, timeout=30) as resp:
            rep.check("btw.status", resp.status_code == 200)
            for line in resp.iter_lines():
                if line.startswith("data:"):
                    try:
                        o = json.loads(line[5:].strip())
                    except Exception:  # noqa: BLE001
                        continue
                    if "delta" in o:
                        deltas.append(o["delta"])
                    if "done" in o or "error" in o:
                        break
        rep.check("btw.stream", len("".join(deltas)) > 5, f"chars={len(''.join(deltas))}")

        # ── terminal WS ─────────────────────────────────────────────────
        async def _ws():
            async with websockets.connect(WS_BASE + "/api/runs/cscs/terminal") as ws:
                banner = await ws.recv()
                await ws.send("echo hello")
                echo = await ws.recv()
                await ws.send("rm -rf /")
                restr = await ws.recv()
                return banner, echo, restr
        banner, echo, restr = asyncio.run(_ws())
        rep.check("terminal.banner", "沙箱终端" in banner, banner.strip())
        rep.check("terminal.echo", echo.strip() == "hello", echo.strip())
        rep.check("terminal.restricted", "受限" in restr, restr.strip())

    except Exception as e:  # noqa: BLE001
        rep.check("unexpected", False, f"{type(e).__name__}: {e}")
    finally:
        cli.close()
        if started_server and proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    return rep.summary()


if __name__ == "__main__":
    sys.exit(main())
