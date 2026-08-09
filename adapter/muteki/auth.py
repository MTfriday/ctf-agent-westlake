"""muteki 契约 — 认证模块。

对应 muteki `apps/web/auth.py`：单密码门禁。默认关闭（loopback 单操作员），
设置 AEMEATH_WEB_PASSWORD 后启用。启用时：
  - POST /api/auth/login    交换密码 → 签名会话 token
  - GET  /api/auth/me       校验 token（gate 已挡，能到即有效）
  - POST /api/auth/ticket   铸造一次性 SSE/WS 票据（EventSource/WS 无法带头）
契约与前端 useRun.ts 完全一致（token → localStorage，SSE 用 ?ticket=）。
"""

from __future__ import annotations

import base64
import hmac
import hashlib
import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Optional

# 未纳入 auth 门的公开路径（登录本身、事件流由 handler 内自校验票据）
PUBLIC_API_PATHS: frozenset[str] = frozenset({"/api/auth/login"})


@dataclass
class AuthConfig:
    """从环境读取认证配置。"""

    enabled: bool = False
    password: str = ""
    token_secret: str = ""
    token_ttl: float = 24 * 3600.0  # 会话 token 有效期（秒）

    @classmethod
    def from_env(cls) -> "AuthConfig":
        password = os.environ.get("AEMEATH_WEB_PASSWORD", "")
        enabled = bool(password)
        return cls(
            enabled=enabled,
            password=password,
            token_secret=os.environ.get(
                "AEMEATH_TOKEN_SECRET",
                secrets.token_hex(32),  # 每次启动轮换 → 重启后需重新登录（可接受）
            ),
            token_ttl=float(os.environ.get("AEMEATH_TOKEN_TTL", "86400")),
        )

    def fail_fast_check(self) -> None:
        """非 loopback 绑定但未设密码时拒绝启动（安全护栏）。"""
        bind = os.environ.get("AEMEATH_WEB_BIND", "127.0.0.1")
        if self.enabled:
            return
        if bind not in ("127.0.0.1", "localhost", "::1"):
            raise RuntimeError(
                "AEMEATH_WEB_PASSWORD must be set when binding a non-loopback address"
            )


def _sign(cfg: AuthConfig, payload: str, exp: float) -> str:
    msg = f"{payload}.{exp}".encode()
    sig = hmac.new(cfg.token_secret.encode(), msg, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(sig).decode()


def issue_token(cfg: AuthConfig) -> str:
    """签发签名会话 token（payload.签名）。"""
    payload = secrets.token_urlsafe(18)
    exp = time.time() + cfg.token_ttl
    sig = _sign(cfg, payload, exp)
    return base64.urlsafe_b64encode(f"{payload}.{exp}.{sig}".encode()).decode()


def verify_token(cfg: AuthConfig, token: Optional[str]) -> bool:
    if not token:
        return False
    try:
        raw = base64.urlsafe_b64decode(token.encode()).decode()
        payload, exp_s, sig = raw.split(".")
        exp = float(exp_s)
        if time.time() > exp:
            return False
        return hmac.compare_digest(_sign(cfg, payload, exp), sig)
    except Exception:  # noqa: BLE001
        return False


def bearer_from_header(auth: Optional[str]) -> Optional[str]:
    """从 Authorization 头提取 Bearer token。"""
    if not auth:
        return None
    parts = auth.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


def check_password(cfg: AuthConfig, password: Optional[str]) -> bool:
    if not cfg.enabled:
        return True  # 未启用 → 任意/空密码通过（前端流程统一）
    if not password:
        return False
    return hmac.compare_digest(password.encode(), cfg.password.encode())


@dataclass
class TicketStore:
    """一次性 SSE/WS 票据存储（短 TTL）。"""

    _tickets: dict[str, float] = field(default_factory=dict)
    _ttl: float = 60.0

    def mint(self) -> str:
        ticket = secrets.token_urlsafe(24)
        self._tickets[ticket] = time.time() + self._ttl
        return ticket

    def consume(self, ticket: Optional[str]) -> bool:
        if not ticket:
            return False
        exp = self._tickets.pop(ticket, None)
        if exp is None:
            return False
        return time.time() <= exp
