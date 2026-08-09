"""Aemeath 总控 OODA 循环 + 自适应模式路由（决策层）。

组件:
- mode_router   模式路由（swarm / orchestrated / hybrid）
- planner       主力 LLM 生成 ≤4 个 Intent
- dispatcher    Intent 认领入口 + 超时/死路处理
- scheduler     OODA 主循环 + 容灾恢复
- controller    组合层（供 P5 的 /api/orchestrator/* 端点调用）

用法::

    from src.orchestrator import OrchestratorController

    ctl = OrchestratorController(runtime)     # 复用 adapter 的 SolverRuntime
    await ctl.start("crypto-1")
    await ctl.status("crypto-1")
"""

from src.orchestrator.controller import OrchestratorController, orchestrator_settings
from src.orchestrator.dispatcher import Dispatcher
from src.orchestrator.mode_router import MODES, ModeRouter
from src.orchestrator.planner import Planner
from src.orchestrator.scheduler import OrchestratorScheduler

__all__ = [
    "OrchestratorController",
    "orchestrator_settings",
    "ModeRouter",
    "Planner",
    "Dispatcher",
    "OrchestratorScheduler",
    "MODES",
]
