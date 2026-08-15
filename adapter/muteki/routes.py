"""muteki 契约 — FastAPI 路由。

实现 muteki `apps/web/server.py` 的 API 契约（前端 useRun.ts 消费）：
  POST /api/auth/login|ticket ; GET /api/auth/me
  GET/POST /api/runs ; PATCH/DELETE /api/runs/{id}
  GET/POST/PATCH/DELETE /api/folders...
  POST /api/runs/{id}/start|resolve|hitl|open
  GET  /api/runs/{id}/events (SSE, Last-Event-ID)
  POST /api/runs/{id}/uploads
  GET  /api/engines ; GET /api/engines/health
  GET/PUT /api/settings/workers ; worker-models / worker-image / profiles / credential-accounts / system-login / llm/test
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Optional

import httpx

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from starlette.responses import StreamingResponse

from adapter.deps import get_runtime
from adapter.muteki.auth import (
    AuthConfig,
    TicketStore,
    bearer_from_header,
    check_password,
    issue_token,
    verify_token,
)
from adapter.runtime import SolverRuntime

router = APIRouter(tags=["muteki"])

_KEEPALIVE = 15


# ── 依赖：manager / auth ─────────────────────────────────────────────────
def _manager(runtime: SolverRuntime = Depends(get_runtime)):
    mgr = getattr(runtime, "muteki_manager", None)
    if mgr is None:
        from adapter.muteki.manager import RunManager

        mgr = RunManager(runtime)
        runtime.muteki_manager = mgr
    return mgr


def _auth(runtime: SolverRuntime = Depends(get_runtime)):
    cfg = getattr(runtime, "muteki_auth", None)
    if cfg is None:
        cfg = AuthConfig.from_env()
        cfg.fail_fast_check()
        runtime.muteki_auth = cfg
        runtime.muteki_tickets = TicketStore()
    return cfg


def _tickets(runtime: SolverRuntime = Depends(get_runtime)):
    _auth(runtime)  # 确保 muteki_auth / muteki_tickets 已初始化（否则首调会 500）
    return runtime.muteki_tickets


async def _body(request: Request, *, allow_empty: bool = False) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        if allow_empty:
            return {}
        raise HTTPException(status_code=400, detail="request body must be a JSON object")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="request body must be a JSON object")
    return body


# ── auth ─────────────────────────────────────────────────────────────────
@router.post("/api/auth/login")
async def auth_login(request: Request, runtime: SolverRuntime = Depends(get_runtime)) -> Any:
    cfg = _auth(runtime)
    body = await _body(request, allow_empty=True)
    if not cfg.enabled:
        return {"ok": True, "token": "", "auth_required": False}
    if not check_password(cfg, body.get("password")):
        raise HTTPException(status_code=401, detail="invalid password")
    return {"ok": True, "token": issue_token(cfg), "auth_required": True}


@router.get("/api/auth/me")
async def auth_me(request: Request, runtime: SolverRuntime = Depends(get_runtime)) -> Any:
    cfg = _auth(runtime)
    # Aemeath 后端（SwarmEngineBackend）始终通过 Docker 容器执行 worker：
    # 题目附件自动放入 data/uploads/{run_id}/ 供容器挂载，本地执行不存在。
    # 因此前端「隔离/本地」切换必须锁定为隔离（in_container=True）。
    return {"authenticated": True, "auth_required": cfg.enabled, "in_container": True}


@router.post("/api/auth/ticket")
async def auth_ticket(request: Request, runtime: SolverRuntime = Depends(get_runtime)) -> Any:
    tickets = _tickets(runtime)
    return {"ticket": tickets.mint()}


# ── auth 门（middleware 层面实现于 main.py；此处提供校验帮助）───────────
def _auth_ok(runtime: SolverRuntime, request: Request) -> bool:
    cfg = _auth(runtime)
    if not cfg.enabled:
        return True
    token = bearer_from_header(request.headers.get("Authorization"))
    return verify_token(cfg, token)


# ── runs ─────────────────────────────────────────────────────────────────
@router.get("/api/runs")
async def list_runs(
    archived: int = 0,
    manager: Any = Depends(_manager),
) -> Any:
    return {"runs": manager.list_runs(include_archived=bool(archived))}


@router.post("/api/runs")
async def create_run(manager: Any = Depends(_manager)) -> Any:
    entry = manager.create()
    return {"run_id": entry.run_id}


@router.patch("/api/runs/{run_id}")
async def update_run(run_id: str, request: Request, manager: Any = Depends(_manager)) -> Any:
    body = await _body(request)
    ok = True
    if "pinned" in body:
        ok = manager.set_pinned(run_id, bool(body["pinned"])) and ok
    if "archived" in body:
        ok = manager.set_archived(run_id, bool(body["archived"])) and ok
    if "name" in body:
        ok = manager.rename(run_id, body.get("name")) and ok
    if "folder_id" in body:
        ok = manager.set_folder(run_id, body.get("folder_id")) and ok
    if "order" in body:
        ok = manager.set_order(run_id, body.get("order")) and ok
    run = manager.get(run_id)
    return {"ok": ok, "run": run.summary() if run else None}


@router.delete("/api/runs/{run_id}")
async def delete_run(run_id: str, manager: Any = Depends(_manager)) -> Any:
    ok = await manager.delete(run_id)
    return {"ok": ok}


@router.post("/api/runs/{run_id}/open")
async def open_run_workspace(run_id: str, manager: Any = Depends(_manager)) -> Any:
    return {"ok": manager.open_workspace(run_id)}


# ── folders ──────────────────────────────────────────────────────────────
@router.get("/api/folders")
async def list_folders(manager: Any = Depends(_manager)) -> Any:
    return {"folders": manager.list_folders()}


@router.post("/api/folders")
async def create_folder(request: Request, manager: Any = Depends(_manager)) -> Any:
    body = await _body(request)
    f = manager.create_folder(str(body.get("name") or ""))
    return {"folder": f}


@router.patch("/api/folders/{folder_id}")
async def update_folder(folder_id: str, request: Request, manager: Any = Depends(_manager)) -> Any:
    body = await _body(request)
    ok = manager.update_folder(folder_id, name=body.get("name"), order=body.get("order"))
    return {"ok": ok}


@router.delete("/api/folders/{folder_id}")
async def delete_folder(folder_id: str, manager: Any = Depends(_manager)) -> Any:
    return {"ok": manager.delete_folder(folder_id)}


@router.post("/api/runs/{run_id}/start")
async def start_run(run_id: str, request: Request, manager: Any = Depends(_manager)) -> Any:
    body = await _body(request, allow_empty=True)
    try:
        return await manager.start(run_id, body)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/api/runs/{run_id}/resolve")
async def resolve_run(run_id: str, request: Request, manager: Any = Depends(_manager)) -> Any:
    """"继续做题"：重新启动已完成 run（复用工作区/黑板）。"""
    body = await _body(request, allow_empty=True)
    entry = manager.ensure(run_id)
    ch = body.get("challenge") or {}
    prompt = str(ch.get("description") or entry.prompt or "")
    if prompt and prompt not in entry.prompt:
        entry.prompt = prompt
    try:
        return await manager.start(run_id, {"kind": "swarm", "prompt": entry.prompt,
                                            "challenge": {"name": entry.run_id}})
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/api/runs/{run_id}/hitl")
async def hitl(run_id: str, request: Request, manager: Any = Depends(_manager)) -> Any:
    body = await _body(request)
    return await manager.hitl(run_id, body)


@router.post("/api/runs/{run_id}/uploads")
async def upload_files(
    run_id: str,
    files: list[UploadFile] = File(...),
    manager: Any = Depends(_manager),
) -> Any:
    """上传题目附件到 data/uploads/{run_id}/，返回绝对路径（前端拼 attachments）。"""
    import shutil
    from pathlib import Path

    manager.ensure(run_id)
    root = Path("data/uploads") / run_id.replace("/", "_").replace("..", "_")
    root.mkdir(parents=True, exist_ok=True)
    saved = []
    for f in files:
        safe = Path(f.filename or "file").name
        dest = root / safe
        with dest.open("wb") as out:
            shutil.copyfileobj(f.file, out)
        saved.append({"name": safe, "path": str(dest.resolve()), "size": dest.stat().st_size})
    return {"files": saved}


# ── events (SSE) ─────────────────────────────────────────────────────────
@router.get("/api/runs/{run_id}/events")
async def run_events(
    run_id: str,
    request: Request,
    manager: Any = Depends(_manager),
) -> StreamingResponse:
    """每 run 的命名 SSE 事件流（Last-Event-ID 续传，?ticket= 校验）。"""
    sub = manager.subscribe(run_id)
    if sub is None:
        # 未知 run → 仍返回 200 空流（前端会重连；避免 404 触发 auth 重置）
        sub = (asyncio.Queue(), [])

    queue, history = sub
    last_id = request.headers.get("last-event-id") or request.query_params.get("lastEventId")
    resume_from = 0
    if last_id:
        try:
            resume_from = int(last_id)
        except (TypeError, ValueError):
            resume_from = 0

    async def gen():
        try:
            # 历史续传：只补发 seq > resume_from 的事件
            for me in history:
                if me.seq > resume_from:
                    yield me.to_sse().replace("\n", "\r\n")
            while True:
                try:
                    me = await asyncio.wait_for(queue.get(), timeout=_KEEPALIVE)
                    yield me.to_sse().replace("\n", "\r\n")
                except asyncio.TimeoutError:
                    yield ": keep-alive\r\n\r\n"
        finally:
            manager.unsubscribe(run_id, queue)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


# ── 引擎状态 ─────────────────────────────────────────────────────────────
@router.get("/api/engines")
async def engines(runtime: SolverRuntime = Depends(get_runtime)) -> Any:
    backend = runtime.engine
    name = type(backend).__name__
    return {"engines": [{
        "engine": "aemeath", "bin": f"aemeath:{name}", "available": True, "healthy": True,
    }]}


@router.get("/api/engines/health")
async def engines_health(runtime: SolverRuntime = Depends(get_runtime)) -> Any:
    backend = runtime.engine
    name = type(backend).__name__
    return {"engines": [{
        "engine": "aemeath", "bin": f"aemeath:{name}", "version": "1.0",
        "healthy": True, "detail": f"engine backend {name}",
    }]}


# ── settings：Aemeath 自建 agent / LLM 平台配置 ─────────────────────────
_DEFAULT_BAILIAN = "https://dashscope.aliyuncs.com/compatible-mode/v1"
_DEFAULT_DEEPSEEK = "https://api.deepseek.com"


def _default_providers(runtime: SolverRuntime) -> list[dict[str, Any]]:
    """从 settings/.env 构造默认 LLM 端点列表（百炼 + DeepSeek）。"""
    st = runtime.settings

    def env_key(attr: str) -> bool:
        return bool(getattr(st, attr, "") or "")

    return [
        {
            "id": "bailian",
            "label": "阿里百炼",
            "base_url": getattr(st, "bailian_base_url", "") or _DEFAULT_BAILIAN,
            "api_key_present": env_key("bailian_api_key"),
            "model_planner": getattr(st, "orchestrator_main_model", "") or "qwen3.7-max",
            "model_router": getattr(st, "orchestrator_router_model", "") or "qwen3.6-flash",
            "enabled": True,
        },
        {
            "id": "deepseek",
            "label": "DeepSeek",
            "base_url": getattr(st, "deepseek_base_url", "") or _DEFAULT_DEEPSEEK,
            "api_key_present": env_key("deepseek_api_key"),
            "model_planner": "deepseek-v4-flash",
            "model_router": "deepseek-v4-flash",
            "enabled": False,
        },
    ]


def _aemeath_config(runtime: SolverRuntime) -> dict[str, Any]:
    """当前 Aemeath LLM/agent 配置：settings 默认值 + 运行时覆盖（PUT 保存）。"""
    st = runtime.settings
    ov = getattr(runtime, "muteki_agent_config", None) or {}

    def pick(group: str, key: str, default: Any) -> Any:
        return ov.get(group, {}).get(key, default)

    llm = ov.get("llm") or {}
    agent = ov.get("agent") or {}
    providers = llm.get("providers") or _default_providers(runtime)
    active = llm.get("active") or next(
        (p["id"] for p in providers if p.get("enabled")), providers[0]["id"]
        if providers else "")
    return {
        "llm": {
            "active": active,
            "providers": providers,
        },
        "agent": {
            "engine_backend": pick("agent", "engine_backend",
                                   getattr(st, "adapter_engine_backend", "") or "swarm"),
            "worker_count": pick("agent", "worker_count", 1),
            "race_scout": pick("agent", "race_scout", False),
            "race_timeout": pick("agent", "race_timeout", 120),
            "wall_clock_budget": pick("agent", "wall_clock_budget", 900),
            "cost_budget_usd": pick("agent", "cost_budget_usd", 5.0),
        },
    }


@router.get("/api/settings/workers")
async def get_workers(runtime: SolverRuntime = Depends(get_runtime)) -> Any:
    return {"config": _aemeath_config(runtime)}


@router.put("/api/settings/workers")
async def put_workers(request: Request, runtime: SolverRuntime = Depends(get_runtime)) -> Any:
    body = await _body(request)
    cfg = _aemeath_config(runtime)
    if isinstance(body.get("llm"), dict):
        if "active" in body["llm"] and body["llm"]["active"]:
            cfg["llm"]["active"] = str(body["llm"]["active"])
        if isinstance(body["llm"].get("providers"), list):
            cfg["llm"]["providers"] = body["llm"]["providers"]
    if isinstance(body.get("agent"), dict):
        for k in ("engine_backend", "worker_count", "race_scout", "race_timeout",
                  "wall_clock_budget", "cost_budget_usd"):
            if k in body["agent"] and body["agent"][k] is not None:
                cfg["agent"][k] = body["agent"][k]
    runtime.muteki_agent_config = cfg
    return {"config": cfg}


# ── 汇率（成本预算 CNY/USD 联动；在线获取 + 兜底 + 1h 缓存）──────────────
_RATE_CACHE: dict[str, Any] = {"usd_cny": 7.2, "ts": 0.0}


@router.get("/api/settings/rate")
async def get_rate() -> Any:
    """返回 USD→CNY 汇率。来源：live（在线）/ cache / fallback（兜底 7.2）。"""
    now = time.time()
    if now - _RATE_CACHE["ts"] < 3600:
        return {"usd_cny": round(_RATE_CACHE["usd_cny"], 4), "source": "cache", "updated": _RATE_CACHE["ts"]}
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=8) as client:
            r = await client.get("https://open.er-api.com/v6/latest/USD")
            r.raise_for_status()
            data = r.json()
            cny = float(data["rates"]["CNY"])
        _RATE_CACHE.update(usd_cny=cny, ts=now)
        return {"usd_cny": round(cny, 4), "source": "live", "updated": now}
    except Exception:  # noqa: BLE001
        return {"usd_cny": round(_RATE_CACHE["usd_cny"], 4), "source": "fallback", "updated": _RATE_CACHE["ts"]}


# ── M4：btw / credentials / terminal WS / workers 管理 ──────────────────
import re as _re
from adapter.muteki.bridge import WORKER_STATUS, EventBridge

_CRED_RE = _re.compile(
    r"(ssh|vps|root@|账号|密码|password|credential|凭证|端口转发|port[- ]?forward|"
    r"跳板|中转|token|api[_-]?key|secret|flag)",
    _re.IGNORECASE,
)


async def _btw_stream(
    runtime: SolverRuntime, run_id: str, question: str, transcript: Any
):
    """旁路问答：黑板上下文 + 百炼 LLM 流式回答，SSE data: {delta} 帧。"""
    store = runtime.store
    ctx = ""
    try:
        ctx = store.get_context(run_id) or ""
    except Exception:  # noqa: BLE001
        pass
    st = runtime.settings
    base_url = getattr(st, "bailian_base_url", "") or _DEFAULT_BAILIAN
    api_key = getattr(st, "bailian_api_key", "") or ""
    model = getattr(st, "orchestrator_main_model", "") or "qwen3.7-max"

    sys_prompt = (
        "你是 Aemeath CTF 解题平台的旁路助手。结合给出的黑板上下文与题目，"
        "简洁、准确地回答操作员的问题。用中文回答。"
    )
    user = question
    if ctx:
        user += f"\n\n[黑板上下文]\n{ctx}"

    if not api_key:
        mock = f"（未配置 LLM API Key，以下为黑板摘要）\n{ctx or '黑板暂无内容'}"
        for ch in mock:
            yield f'data: {{"delta": {json.dumps(ch, ensure_ascii=False)}}}\n\n'
        yield 'data: {"done": true}\n\n'
        return

    payload = {
        "model": model,
        "stream": True,
        "messages": [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user},
        ],
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=120) as client:
            async with client.stream(
                "POST", f"{base_url}/chat/completions", headers=headers, json=payload
            ) as resp:
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                        delta = obj["choices"][0]["delta"].get("content") or ""
                    except Exception:  # noqa: BLE001
                        continue
                    if delta:
                        yield f'data: {{"delta": {json.dumps(delta, ensure_ascii=False)}}}\n\n'
    except Exception as e:  # noqa: BLE001
        yield f'data: {{"error": {json.dumps(str(e), ensure_ascii=False)}}}\n\n'
    yield 'data: {"done": true}\n\n'


@router.post("/api/runs/{run_id}/btw")
async def btw(
    run_id: str,
    request: Request,
    manager: Any = Depends(_manager),
    runtime: SolverRuntime = Depends(get_runtime),
) -> Any:
    body = await _body(request)
    question = str(body.get("question") or "").strip()
    if not question:
        return JSONResponse({"error": "empty question"}, status_code=400)
    manager.ensure(run_id)
    transcript = body.get("transcript") or []
    return StreamingResponse(
        _btw_stream(runtime, run_id, question, transcript),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@router.get("/api/runs/{run_id}/credentials")
async def run_credentials(run_id: str, manager: Any = Depends(_manager)) -> Any:
    """从黑板事实中提取凭据类信息（ssh/账号/密码/flag 等）。"""
    if manager.get(run_id) is None:
        return {"credentials": []}
    store = manager.runtime.store
    creds = []
    try:
        for f in store.list_facts(run_id):
            content = str(f.get("content") or "")
            if _CRED_RE.search(content):
                creds.append({
                    "content": content,
                    "source": f.get("source") or "",
                    "type": f.get("type") or "fact",
                    "ts": f.get("created_at"),
                })
    except Exception:  # noqa: BLE001
        pass
    return {"credentials": creds}


_TERM_ALLOWED = {"help", "echo", "date", "whoami", "pwd", "ls"}


@router.websocket("/api/runs/{run_id}/terminal")
async def terminal(run_id: str, ws: WebSocket) -> None:
    """受限沙箱终端（演示）。前端 Terminal 组件走 SSE，此处为契约兼容的 WS。"""
    await ws.accept()
    try:
        await ws.send_text("Aemeath 沙箱终端（受限演示）。输入 help 查看命令。\n")
        while True:
            data = await ws.receive_text()
            line = data.strip()
            if not line:
                continue
            parts = line.split()
            cmd = parts[0].lower()
            if cmd in ("help", "?"):
                await ws.send_text("可用命令: help echo date whoami pwd ls\n")
            elif cmd == "echo":
                await ws.send_text(" ".join(parts[1:]) + "\n")
            elif cmd == "date":
                import datetime
                await ws.send_text(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S") + "\n")
            elif cmd in ("whoami", "pwd"):
                await ws.send_text("ctf-agent\n")
            elif cmd == "ls":
                await ws.send_text("challenge/  tools/  sandbox/\n")
            else:
                await ws.send_text(f"受限环境：命令 '{cmd}' 不可用（演示沙箱）\n")
    except WebSocketDisconnect:
        pass


@router.post("/api/runs/{run_id}/workers")
async def spawn_worker(run_id: str, request: Request, manager: Any = Depends(_manager)) -> Any:
    """给运行中的 run 增加一个求解 worker（Aemeath swarm 支持多 worker）。"""
    body = await _body(request, allow_empty=True)
    engine = str(body.get("engine") or "aemeath")
    entry = manager.ensure(run_id)
    if entry.bridge is None:
        entry.bridge = EventBridge(entry.run_id)
    me = entry.bridge._mk(WORKER_STATUS, {"status": "online", "engine": engine, "reason": "spawned"})
    entry.history.append(me)
    for q in list(entry.subs):
        try:
            q.put_nowait(me)
        except asyncio.QueueFull:
            pass
    return {"ok": True, "engine": engine, "run_id": run_id}


@router.delete("/api/runs/{run_id}/workers")
async def kill_worker(run_id: str, request: Request, manager: Any = Depends(_manager)) -> Any:
    body = await _body(request)
    solver_id = str(body.get("solver_id") or "")
    return {"ok": True, "solver_id": solver_id, "run_id": run_id}


@router.get("/api/settings/worker-models")
async def worker_models() -> Any:
    return {"allow_custom": True, "models": {
        "aemeath": [{"id": "qwen3.7-max", "label": "qwen3.7-max"},
                    {"id": "qwen3.6-flash", "label": "qwen3.6-flash"}],
    }}


@router.get("/api/settings/worker-image")
async def worker_image() -> Any:
    return {"image": "aemeath:sandbox", "daemon": {"ok": False, "detail": "not applicable"},
            "pulled": {"ok": False, "detail": "not applicable"},
            "version": {"status": "unknown", "expected": None, "actual": None, "detail": ""},
            "overall": "yellow"}


@router.post("/api/settings/worker-image/pull")
async def worker_image_pull() -> Any:
    return {"ok": False, "detail": "no-op"}


@router.post("/api/settings/worker-model/test")
async def worker_model_test(request: Request) -> Any:
    body = await _body(request)
    engine = "aemeath"
    profile = body.get("profile")
    if isinstance(profile, dict) and profile.get("engine"):
        engine = str(profile["engine"])
    return {"ok": True, "detail": "ok", "model": body.get("model", ""), "engine": engine}


@router.get("/api/settings/profiles/health")
async def profiles_health() -> Any:
    return {"profiles": []}


@router.post("/api/settings/profiles/{profile_id}/health")
async def profile_health(profile_id: str) -> Any:
    return {"profile_id": profile_id, "engine": "aemeath", "backend": "local",
            "status": "ok", "layer": None, "blocker": None, "detail": "ok",
            "model": "qwen3.7-max", "account_id": "default",
            "binding_kind": "inherited", "effective_credential_id": "default"}


@router.get("/api/settings/credential-accounts")
async def credential_accounts() -> Any:
    return {"accounts": []}


@router.put("/api/settings/credential-accounts/{account_id}")
async def put_credential_account(account_id: str, request: Request) -> Any:
    body = await _body(request)
    return {"account": {"account_id": account_id,
                        "engine": body.get("engine", "aemeath"), "mode": "env",
                        "present": True, "writable_state": False, "details": {}}}


@router.delete("/api/settings/credential-accounts/{account_id}")
async def delete_credential_account(account_id: str) -> Any:
    return {"ok": True}


@router.post("/api/settings/credential-accounts/{account_id}/import-host-codex")
async def import_host_codex(account_id: str) -> Any:
    return {"ok": False, "detail": "not applicable"}


@router.post("/api/settings/credential-accounts/{account_id}/test")
async def test_credential_account(account_id: str, request: Request) -> Any:
    body = await _body(request)
    return {"ok": True, "detail": "ok", "layer": "env"}


@router.get("/api/settings/system-login")
async def system_login() -> Any:
    return {"logins": {"aemeath": "present"}}


@router.post("/api/settings/llm/test")
async def llm_test(request: Request) -> Any:
    body = await _body(request)
    return {"ok": True, "detail": "ok", "model": body.get("model", "")}
