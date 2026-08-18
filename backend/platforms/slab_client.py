"""西湖论剑（slab-match）AI Agent 平台客户端。

按 AI Agent API 文档适配：
  Base:   {serverHost}/slab-match/api/v1/agent
  Auth:   Header `X-Agent-AccessKey`
  响应:   统一json {code, message, data}，code=="00000" 成功

题目模型是「分类(category) → 子题(corpus)」两级：
  exercise-list 返回 [{id, name, corpus:[{id, name, ...}]}]
  所有题目操作（详情/提交/环境）都使用 corpus.id 作为 exerciseId。

接口映射：
  match/notice/match-info        竞赛注意事项/规则
  answer-panel/overview          得分排名
  answer-panel/answer            提交 flag
  ctf/exercise-list              题目列表
  ctf/exercise                   题目详情（含附件/靶机）
  ctf/build-exercise-env         启动环境（异步）
  ctf/recover-exercise-env       回收环境
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from backend.flag_utils import normalize_flag
from backend.platforms.base import ChallengeInfo, PlatformClient, SubmitResult
from backend.platforms.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

USER_AGENT = "CTF-Agent/1.0"
OK_CODE = "00000"
_ENV_PATH = "/slab-match/api/v1/agent"


class SlabClient(PlatformClient):
    """西湖论剑 slab-match 平台客户端。"""

    def __init__(
        self,
        api_base_url: str,
        access_key: str = "",
        rate_limit_rps: int = 5,
    ) -> None:
        self.api_base_url = api_base_url.rstrip("/")
        self.access_key = access_key
        self.rate_limiter = RateLimiter(rps=rate_limit_rps)
        self._client: httpx.AsyncClient | None = None
        self._last_error = ""
        # 缓存：exerciseId(str) -> ChallengeInfo
        self._by_id: dict[str, ChallengeInfo] = {}

    @classmethod
    def from_settings(cls, settings: object) -> "SlabClient":
        return cls(
            api_base_url=getattr(settings, "platform_api_base_url", ""),
            access_key=getattr(settings, "slab_access_key", "")
            or getattr(settings, "platform_auth_credential", ""),
            rate_limit_rps=getattr(settings, "rate_limit_rps", 5),
        )

    # ── HTTP ────────────────────────────────────────────────────────────────

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            headers = {"User-Agent": USER_AGENT}
            if self.access_key:
                headers["X-Agent-AccessKey"] = self.access_key
            self._client = httpx.AsyncClient(
                base_url=self.api_base_url,
                headers=headers,
                timeout=30.0,
                verify=False,  # 自签证书兼容
                follow_redirects=True,
            )
        return self._client

    async def _request(
        self, method: str, path: str, *, json: dict | None = None, params: dict | None = None
    ) -> dict[str, Any]:
        """发起请求并解统一json。失败抛 RuntimeError(message)。"""
        async with self.rate_limiter:
            client = await self._ensure_client()
            resp = await client.request(method, path, json=json, params=params)

        if resp.status_code == 429:
            self._last_error = "rate limited"
            raise RuntimeError("slab API rate limited (HTTP 429)")
        try:
            payload = resp.json()
        except Exception:
            self._last_error = f"invalid JSON: {resp.text[:200]}"
            raise RuntimeError(self._last_error)

        if not isinstance(payload, dict):
            self._last_error = f"unexpected response: {payload}"
            raise RuntimeError(self._last_error)

        code = str(payload.get("code", ""))
        if code != OK_CODE:
            msg = str(payload.get("message") or payload.get("msg") or code)
            self._last_error = msg
            raise RuntimeError(f"slab API error {code}: {msg}")

        return payload.get("data", payload)

    # ── 题目列表（两级）────────────────────────────────────────────────────

    async def fetch_challenges(self) -> list[ChallengeInfo]:
        """拉取全部题目：分类 → corpus 子题，逐题补详情拿附件/靶机。"""
        self._by_id = {}
        all_challenges: list[ChallengeInfo] = []
        try:
            data = await self._request("GET", f"{_ENV_PATH}/ctf/exercise-list")
        except Exception as e:  # noqa: BLE001
            self._last_error = str(e)
            logger.warning("slab exercise-list failed: %s", e)
            return all_challenges

        categories = data if isinstance(data, list) else data.get("list", data.get("data", []))
        for cat in categories if isinstance(categories, list) else []:
            cat_name = str(cat.get("name") or "")
            for item in cat.get("corpus", []) if isinstance(cat, dict) else []:
                cid = str(item.get("id", ""))
                if not cid:
                    continue
                info = ChallengeInfo(
                    id=cid,
                    name=str(item.get("name", cid)),
                    category=cat_name,
                    solved=bool(item.get("hasSolved", False)),
                    raw=item,
                )
                # 补详情（附件/靶机/描述）
                try:
                    detail = await self.get_challenge_detail(cid)
                    info.description = detail.description
                    info.value = detail.value
                    info.files = detail.files
                    info.connection_info = detail.connection_info
                    info.tags = detail.tags
                    info.solved = info.solved or detail.solved
                    info.raw = detail.raw or item
                except Exception as e:  # noqa: BLE001
                    logger.warning("slab detail %s failed: %s", cid, e)
                self._by_id[cid] = info
                all_challenges.append(info)

        return all_challenges

    async def get_challenge_detail(self, challenge_id: str | int) -> ChallengeInfo:
        """题目详情：描述/分值/难度/附件/靶机 endpoints/环境状态。"""
        cid = str(challenge_id)
        cached = self._by_id.get(cid)
        try:
            data = await self._request(
                "GET", f"{_ENV_PATH}/ctf/exercise", params={"exerciseId": cid}
            )
        except Exception as e:  # noqa: BLE001
            self._last_error = str(e)
            return cached or ChallengeInfo(id=cid, name="Unknown (fetch failed)")

        if not isinstance(data, dict):
            return cached or ChallengeInfo(id=cid, name="Unknown")

        # 详情优先用自身 name；缺省时回退缓存名
        name = str(data.get("name") or "")
        if not name or name == "Unknown":
            if cached and cached.name and cached.name != "Unknown":
                name = cached.name
            else:
                name = cid

        info = ChallengeInfo(
            id=cid,
            name=name,
            description=str(data.get("description") or ""),
            value=int(float(str(data.get("score") or "0"))),
            tags=[str(data.get("difficulty", ""))] if data.get("difficulty") else [],
            solved=bool(data.get("hasSolved", False)),
            files=_files_of(data.get("attachment")),
            connection_info=_connection_of(data.get("endpoints")),
            raw=data,
        )
        self._by_id[cid] = info
        return info

    # ── 动态环境 ───────────────────────────────────────────────────────────

    async def start_environment(self, challenge_id: str | int) -> Any:
        """启动动态环境（build-exercise-env），随后轮询直到可用。

        build 是异步操作：POST 后轮询题目详情，直到 isNeedCheck=false 且
        endpoints 可用（或 expireTime 到 / 超时）。
        """
        cid = str(challenge_id)
        try:
            await self._request(
                "POST", f"{_ENV_PATH}/ctf/build-exercise-env", json={"exerciseId": int(cid)}
            )
        except Exception as e:  # noqa: BLE001
            self._last_error = f"build env failed: {e}"
            logger.warning("slab build env %s: %s", cid, e)
            return _SlabEnv("")

        # 轮询详情直到就绪（isNeedCheck=false 且 endpoints 可用）
        import asyncio

        deadline = 120.0  # 最多等 2 分钟
        waited = 0.0
        while waited < deadline:
            try:
                detail = await self.get_challenge_detail(cid)
                raw = detail.raw or {}
                if not raw.get("isNeedCheck") and detail.connection_info:
                    entry = detail.connection_info
                    logger.info("slab env %s ready: %s", cid, entry)
                    return _SlabEnv(entry, raw=raw)
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(3.0)
            waited += 3.0

        logger.warning("slab env %s not ready within timeout", cid)
        # 尽力返回已有 entry（若详情里已有）
        try:
            detail = await self.get_challenge_detail(cid)
            return _SlabEnv(detail.connection_info, raw=detail.raw or {})
        except Exception:  # noqa: BLE001
            return _SlabEnv("")

    async def recover_environment(self, challenge_id: str | int) -> bool:
        """回收动态环境。"""
        cid = str(challenge_id)
        try:
            await self._request(
                "POST", f"{_ENV_PATH}/ctf/recover-exercise-env", json={"exerciseId": int(cid)}
            )
            return True
        except Exception as e:  # noqa: BLE001
            self._last_error = f"recover env failed: {e}"
            logger.warning("slab recover env %s: %s", cid, e)
            return False

    # ── 附件下载 ───────────────────────────────────────────────────────────

    async def download_attachment(self, challenge_id: str | int, filename: str) -> bytes | None:
        """下载题目附件。

        ``filename`` 可为：
        - 附件显示名（从详情 files 反查 url/key/signature）
        - 直接的 http(s) URL
        - JSON 对象字符串 / 已解析 dict（含 url/name/key/signature）

        注意：资源站是绝对 URL（pro-resource），不能走带 base_url 的 client，
        否则路径拼接/鉴权行为不可控。下载结果会校验，拒绝 HTML 错误页。
        """
        cid = str(challenge_id)
        meta = _attachment_meta(filename)
        if meta.get("url") is None and meta.get("name"):
            try:
                detail = await self.get_challenge_detail(cid)
                hit = _match_file(detail.files, meta.get("name") or "")
                if hit:
                    meta = {**hit, **{k: v for k, v in meta.items() if v}}
            except Exception:  # noqa: BLE001
                pass
        if not meta.get("url") and not meta.get("key"):
            # 最后兜底：详情里只有一个附件时直接用它
            try:
                detail = await self.get_challenge_detail(cid)
                if len(detail.files) == 1:
                    meta = {**detail.files[0], **{k: v for k, v in meta.items() if v}}
            except Exception:  # noqa: BLE001
                pass

        candidates = _download_candidates(meta)
        if not candidates:
            logger.warning("slab no attachment url for %s/%s", cid, filename)
            return None

        headers = {"User-Agent": USER_AGENT}
        if self.access_key:
            headers["X-Agent-AccessKey"] = self.access_key
            headers["key"] = str(meta.get("key") or "")
            headers["signature"] = str(meta.get("signature") or "")

        last_err = ""
        async with self.rate_limiter:
            async with httpx.AsyncClient(
                headers={k: v for k, v in headers.items() if v},
                timeout=60.0,
                verify=False,
                follow_redirects=True,
                trust_env=False,
            ) as client:
                for url in candidates:
                    try:
                        resp = await client.get(url)
                    except Exception as e:  # noqa: BLE001
                        last_err = f"{url}: {e}"
                        logger.warning("slab download %s failed: %s", filename, e)
                        continue
                    if resp.status_code != 200:
                        last_err = f"{url}: HTTP {resp.status_code}"
                        logger.warning(
                            "slab download %s -> HTTP %d (%s)",
                            filename,
                            resp.status_code,
                            url[:120],
                        )
                        continue
                    data = resp.content or b""
                    if not _looks_like_attachment(data, resp.headers.get("content-type", "")):
                        last_err = f"{url}: rejected non-attachment body ({len(data)}B)"
                        logger.warning(
                            "slab download %s rejected error body from %s (%d bytes, ct=%s)",
                            filename,
                            url[:120],
                            len(data),
                            resp.headers.get("content-type"),
                        )
                        continue
                    return data

        logger.warning("slab download %s exhausted candidates; last=%s", filename, last_err)
        return None

    # ── Flag 提交 ───────────────────────────────────────────────────────────

    async def submit_flag(self, challenge_id: str | int, flag: str) -> SubmitResult:
        """提交 flag：POST answer-panel/answer → {isCorrect}。

        平台规则：flag 格式为 DASCTF{} / flag{}，提交时仅需提交 {} 内内容，
        故先归一化（剥掉外壳）再提交。若有特殊格式要求题目会注明，
        非 DASCTF/flag 外壳的 flag 原样保留。
        """
        cid = str(challenge_id)
        flag = normalize_flag(flag)
        if not flag:
            return SubmitResult("incorrect", "Empty flag", "Empty flag — nothing to submit.")

        try:
            data = await self._request(
                "POST", f"{_ENV_PATH}/answer-panel/answer",
                json={"exerciseId": int(cid), "flag": flag[:256]},
            )
        except Exception as e:  # noqa: BLE001
            return SubmitResult("unknown", str(e), f"Submit error: {e}")

        is_correct = bool(data.get("isCorrect", False)) if isinstance(data, dict) else False
        if is_correct:
            return SubmitResult("correct", "correct", "CORRECT — flag accepted.")
        return SubmitResult("incorrect", "wrong", "INCORRECT — flag rejected.")

    # ── 已解出 ─────────────────────────────────────────────────────────────

    async def fetch_solved(self) -> set[str | int]:
        """返回已解出题目 id 集合（从题目列表 hasSolved 判定）。"""
        solved: set[str | int] = set()
        try:
            challenges = await self.fetch_challenges()
            for c in challenges:
                if c.solved:
                    solved.add(c.id)
        except Exception as e:  # noqa: BLE001
            logger.warning("slab fetch_solved failed: %s", e)
        return solved

    # ── 竞赛信息 / 公告 ─────────────────────────────────────────────────────

    async def fetch_match_info(self) -> dict[str, str]:
        """竞赛注意事项与规则。"""
        try:
            data = await self._request("GET", f"{_ENV_PATH}/match/notice/match-info")
            return dict(data or {})
        except Exception as e:  # noqa: BLE001
            logger.warning("slab match-info failed: %s", e)
            return {}

    async def fetch_overview(self) -> dict[str, Any]:
        """得分与排名。"""
        try:
            return dict(await self._request("GET", f"{_ENV_PATH}/answer-panel/overview") or {})
        except Exception as e:  # noqa: BLE001
            logger.warning("slab overview failed: %s", e)
            return {}

    async def fetch_notices(self) -> list[dict[str, Any]]:
        """公告列表。"""
        try:
            data = await self._request("GET", f"{_ENV_PATH}/match/notice/now-list")
        except Exception as e:  # noqa: BLE001
            logger.warning("slab notice-list failed: %s", e)
            return []
        if isinstance(data, list):
            return [dict(x) for x in data if isinstance(x, dict)]
        return [dict(x) for x in (data.get("list", []) if isinstance(data, dict) else []) if isinstance(x, dict)]

    async def fetch_notice_detail(self, notice_id: int | str) -> dict[str, Any]:
        """公告详情。"""
        try:
            return dict(await self._request(
                "GET", f"{_ENV_PATH}/match/notice/detail", params={"id": notice_id}
            ) or {})
        except Exception as e:  # noqa: BLE001
            logger.warning("slab notice-detail %s failed: %s", notice_id, e)
            return {}

    # ── 诊断 / 关闭 ─────────────────────────────────────────────────────────

    def diagnostics(self) -> dict[str, Any]:
        return {
            "platform": "slab",
            "base_url": self.api_base_url,
            "access_key_set": bool(self.access_key),
            "cached_challenges": len(self._by_id),
            "last_error": self._last_error,
        }

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None


class _SlabEnv:
    """动态环境轻量对象（兼容引擎 getattr(env, 'entry')）。"""

    def __init__(self, entry: str, *, status: str = "running", raw: dict | None = None) -> None:
        self.entry = entry
        self.status = status
        self.raw = raw or {}


def _file_entry(item: dict[str, Any]) -> dict[str, str] | None:
    """规范化单个附件对象。"""
    url = str(item.get("url") or item.get("previewUrl") or item.get("preview_url") or "").strip()
    key = str(item.get("key") or "").strip()
    if not url and not key:
        return None
    name = str(item.get("name") or item.get("filename") or "").strip()
    if not name:
        if url:
            name = url.rstrip("/").rsplit("/", 1)[-1] or "attachment"
        else:
            name = f"attachment-{key[:8] or 'bin'}"
    ext = str(
        item.get("ext")
        or item.get("extension")
        or (name.rsplit(".", 1)[-1] if "." in name else "")
    ).strip()
    out = {
        "name": name,
        "url": url,
        "ext": ext,
    }
    if key:
        out["key"] = key
    sig = str(item.get("signature") or item.get("sign") or "").strip()
    if sig:
        out["signature"] = sig
    return out


def _files_of(attachment: Any) -> list[dict[str, str]]:
    """attachment → [{name, url, ext, key?, signature?}]。

    西湖论剑实际返回 **单个对象**：
      {key, signature, url, name, previewUrl, extension}
    也兼容：
      - list[dict]
      - {files: list[dict]}
      - 空 / null
    """
    if attachment is None or attachment == "" or attachment == []:
        return []

    items: list[Any]
    if isinstance(attachment, list):
        items = attachment
    elif isinstance(attachment, dict):
        nested = attachment.get("files")
        if isinstance(nested, list):
            items = nested
        elif attachment.get("url") or attachment.get("previewUrl") or attachment.get("key"):
            items = [attachment]
        else:
            items = []
    else:
        return []

    out: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        entry = _file_entry(item)
        if entry:
            out.append(entry)
    return out


def _attachment_meta(filename: Any) -> dict[str, str]:
    """把 download_attachment 的 filename 参数归一成 meta dict。"""
    if isinstance(filename, dict):
        entry = _file_entry(filename) or {}
        return entry
    if not isinstance(filename, str):
        return {"name": str(filename or "")}
    text = filename.strip()
    if not text:
        return {}
    if text.startswith("{") and text.endswith("}"):
        try:
            import json

            obj = json.loads(text)
            if isinstance(obj, dict):
                return _file_entry(obj) or {}
        except Exception:  # noqa: BLE001
            pass
    if text.startswith(("http://", "https://")):
        return {"url": text, "name": text.rstrip("/").rsplit("/", 1)[-1] or "attachment"}
    return {"name": text}


def _match_file(files: list[dict[str, str]], name: str) -> dict[str, str] | None:
    if not name:
        return None
    for f in files:
        if f.get("name") == name:
            return f
    # 宽松：后缀/包含匹配
    for f in files:
        fn = f.get("name") or ""
        if fn.endswith(name) or name.endswith(fn):
            return f
    return None


def _download_candidates(meta: dict[str, str]) -> list[str]:
    """生成附件下载候选 URL（去重保序）。"""
    url = (meta.get("url") or "").strip()
    key = (meta.get("key") or "").strip()
    sig = (meta.get("signature") or "").strip()
    preview = (meta.get("previewUrl") or meta.get("preview_url") or "").strip()
    out: list[str] = []

    def add(u: str) -> None:
        u = (u or "").strip()
        if u and u not in out:
            out.append(u)

    add(url)
    add(preview)
    if url and key and sig:
        sep = "&" if "?" in url else "?"
        add(f"{url}{sep}key={key}&signature={sig}")
        add(f"{url}{sep}signature={sig}")
        add(f"{url}{sep}key={key}&sign={sig}")
    if key and sig:
        add(f"https://pro-resource.dasctf.com/resource/oss/{key}?signature={sig}")
        add(f"https://pro-resource.dasctf.com/resource/download?key={key}&signature={sig}")
        add(f"https://pro-resource.dasctf.com/resource/download?key={key}&sign={sig}")
    return out


def _looks_like_attachment(data: bytes, content_type: str = "") -> bool:
    """拒绝明显的 HTML/JSON 错误页，避免把 404 页存成 .zip。"""
    if not data or len(data) < 4:
        return False
    head = data[:256].lstrip().lower()
    ct = (content_type or "").lower()
    if "text/html" in ct or head.startswith((b"<!doctype", b"<html", b"<head", b"<pre")):
        return False
    if head.startswith((b"cannot get", b"cannot post", b"not found", b"error")):
        return False
    # JSON error envelope
    if head[:1] == b"{" and (
        b"\"error\"" in head or b"\"status\"" in head or b"\"timestamp\"" in head
    ):
        # 允许极少数真正的 json 附件；错误页通常很短
        if len(data) < 4096 and (
            b"not found" in head or b"error" in head or b"message" in head
        ):
            return False
    return True


def _connection_of(endpoints: Any) -> str:
    """endpoints → 可连接的 host:port / URL 字符串。

    优先用代理连接（isProxy=true 时用 proxyIps + portMappings[].proxy），
    否则用 exposeIps + ports。多 endpoint 取第一个可用的。
    """
    if not isinstance(endpoints, list) or not endpoints:
        return ""
    ep = endpoints[0]
    if not isinstance(ep, dict):
        return ""

    # 代理连接：portMappings[].proxy + proxyIps
    mappings = ep.get("portMappings") or []
    proxy_ips = ep.get("proxyIps") or []
    if mappings and proxy_ips:
        m = mappings[0]
        if isinstance(m, dict) and m.get("proxy"):
            host = proxy_ips[0]
            return f"{host}:{m['proxy']}"

    # 直连：exposeIps + ports
    ips = ep.get("exposeIps") or []
    ports = ep.get("ports") or []
    if ips and ports:
        return f"{ips[0]}:{ports[0]}"

    return ""
