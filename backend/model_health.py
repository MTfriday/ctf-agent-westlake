"""模型健康注册表 — 运行中实时感知模型不可用并自动剔除/更换。

问题：model_specs 在 SolverRuntime 启动时一次性解析固定，运行中某个模型
失效（API key 失效 / 模型下线 / 配额用尽 / 限流 / 持续 4xx/5xx）不会被
实时感知，solver 只能白白失败 3 次后放弃，其余模型不顶上。

方案：每个 model_spec 维护健康状态，solver 失败时上报；根据错误类型分类：
  - 致命错误（401/403/404/invalid model/not found 等）→ 长时间禁用
  - 配额/限流（429/quota/rate/capacity 等）→ 冷却一段时间后自动重试
  - 普通错误 → 连续多次才禁用（冷却后自动重试）
被禁用的模型从活跃池剔除（自动更换到其余模型），冷却期过后自动恢复尝试。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

# 连续普通错误达到该次数 → 禁用
_MAX_CONSECUTIVE_ERRORS = 3
# 配额/限流类错误达到该次数 → 禁用（冷却）
_MAX_QUOTA_HITS = 2
# 普通/配额禁用冷却时间（秒），冷却后自动重新尝试
_DISABLE_COOLDOWN = 600  # 10 分钟
# 致命错误禁用时间（秒），这类通常不会自动恢复
_FATAL_COOLDOWN = 3600  # 1 小时

_FATAL_MARKERS = (
    "401", "403", "404", "invalid model", "model not found", "invalid_api_key",
    "access denied", "insufficient permissions", "not found", "authentication",
    "unauthorized", "invalid_api", "does not exist", "unknown model",
)
_QUOTA_MARKERS = (
    "429", "quota", "rate limit", "rate_limit", "capacity", "overloaded",
    "throttl", "insufficient_quota", "usage limit", "too many requests",
)


class ModelHealth:
    """单个 model_spec 的健康状态。"""

    def __init__(self, spec: str) -> None:
        self.spec = spec
        self.consecutive_errors = 0
        self.quota_hits = 0
        self.fatal = False
        self.last_error: Optional[str] = None
        self.disabled_until: float = 0.0  # epoch 秒；0 = 未禁用
        self.success_count = 0
        self.last_checked = 0.0

    @property
    def disabled(self) -> bool:
        if self.disabled_until <= 0:
            return False
        return time.time() < self.disabled_until

    @property
    def cooldown_remaining(self) -> int:
        if not self.disabled or self.disabled_until <= 0:
            return 0
        return max(0, int(self.disabled_until - time.time()))

    def status_dict(self) -> dict:
        return {
            "spec": self.spec,
            "disabled": self.disabled,
            "fatal": self.fatal,
            "consecutive_errors": self.consecutive_errors,
            "quota_hits": self.quota_hits,
            "last_error": self.last_error,
            "cooldown_remaining": self.cooldown_remaining,
            "success_count": self.success_count,
        }


class ModelHealthRegistry:
    """线程安全的模型健康注册表。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._models: dict[str, ModelHealth] = {}

    def _get(self, spec: str) -> ModelHealth:
        with self._lock:
            if spec not in self._models:
                self._models[spec] = ModelHealth(spec)
            return self._models[spec]

    def report_success(self, spec: str) -> None:
        """solver 有实质产出/正常结束 → 重置失败计数。

        注意：若模型当前处于禁用期（冷却中），不解除禁用、也不清 fatal 标记——
        保留诊断信息，冷却结束后自然恢复；只在非禁用期清理错误记录。
        """
        h = self._get(spec)
        with self._lock:
            if h.disabled:
                # 禁用期间的成功不解锁（避免反复横跳），仅保留信息
                return
            h.consecutive_errors = 0
            h.quota_hits = 0
            h.fatal = False
            h.last_error = None
            h.success_count += 1

    def report_failure(self, spec: str, error: str) -> bool:
        """上报一次失败。返回 True 表示本次调用后该模型进入禁用状态（可供外部告警）。

        错误分类：
          - 致命（401/403/404/模型不存在…）→ 立即长时间禁用
          - 配额/限流（429/quota/rate…）→ 计次，达阈值后冷却禁用
          - 普通错误 → 连续达阈值后冷却禁用
        """
        h = self._get(spec)
        err_lower = (error or "").lower()
        is_fatal = any(m in err_lower for m in _FATAL_MARKERS)
        is_quota = any(m in err_lower for m in _QUOTA_MARKERS)
        newly_disabled = False
        with self._lock:
            h.last_error = error or ""
            h.last_checked = time.time()
            if is_fatal:
                h.fatal = True
                h.disabled_until = time.time() + _FATAL_COOLDOWN
                newly_disabled = True
                logger.warning(
                    "[model-health] %s 致命错误 → 禁用 %ds: %s",
                    spec, _FATAL_COOLDOWN, (error or "")[:120],
                )
            elif is_quota:
                h.quota_hits += 1
                if h.quota_hits >= _MAX_QUOTA_HITS:
                    h.disabled_until = time.time() + _DISABLE_COOLDOWN
                    newly_disabled = True
                    logger.warning(
                        "[model-health] %s 配额/限流 x%d → 冷却 %ds",
                        spec, h.quota_hits, _DISABLE_COOLDOWN,
                    )
                else:
                    # 未达阈值也记一次普通错误（趋势可见）
                    h.consecutive_errors += 1
            else:
                h.consecutive_errors += 1
                if h.consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
                    h.disabled_until = time.time() + _DISABLE_COOLDOWN
                    newly_disabled = True
                    logger.warning(
                        "[model-health] %s 连续 %d 次错误 → 冷却 %ds",
                        spec, h.consecutive_errors, _DISABLE_COOLDOWN,
                    )
        return newly_disabled

    def active_specs(self, specs: list[str]) -> list[str]:
        """过滤掉当前被禁用的模型。"""
        return [s for s in specs if not self._get(s).disabled]

    def status(self) -> dict[str, dict]:
        with self._lock:
            return {spec: h.status_dict() for spec, h in self._models.items()}

    def reset(self, spec: str) -> None:
        """手动恢复（前端/运维干预）。"""
        h = self._get(spec)
        with self._lock:
            h.disabled_until = 0.0
            h.consecutive_errors = 0
            h.quota_hits = 0
            h.fatal = False
            h.last_error = None
            logger.info("[model-health] %s 手动恢复", spec)
