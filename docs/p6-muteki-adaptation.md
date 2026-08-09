# P6 重设计：直接使用 Muteki 前端 + 适配层桥接

> 版本：2.0 · 2026-08-09
> 决策：放弃自研精简前端，**直接使用 muteki 完整前端**；重构 adapter 为 **muteki 契约桥接层**，把 Aemeath 后端能力（引擎/黑板/总控）映射为 muteki 前端消费的 API 与事件流。

---

## 1. 架构

```
frontend/  ←── muteki 完整版（恢复全部被裁剪组件，不重写）
   │  muteki API 契约
adapter/  ←── 重构为 muteki 兼容桥接层
   ├── auth      /api/auth/login|me|ticket   （单密码，本地默认禁用）
   ├── runs      GET/PATCH/DELETE /api/runs   （平台题目 → run；pin/archive/rename/folder）
   ├── folders   /api/folders                 （本地元数据，简化实现）
   ├── start     POST /api/runs/{id}/start    （启动 Aemeath 引擎 + 总控 OODA）
   ├── events    GET /api/runs/{id}/events    （每 run SSE：Aemeath 事件 → muteki 事件）
   ├── hitl      POST /api/runs/{id}/hitl     （→ 黑板 intent / 总控指令）
   └── (可选) terminal / btw / credentials / workers
```

**run 概念映射**：muteki 的 `run` = Aemeath 的一道题（challenge）。每 run 有独立的引擎任务 + 黑板 + 事件总线订阅。

## 2. 事件映射（Aemeath → muteki）

muteki 前端通过 `reduce()` 状态机消费 `run.started / text.delta / tool.* / blackboard.delta / reason.intent / insight.event / run.finished` 等。

| Aemeath 事件 | → muteki 事件 | 载荷桥接 |
|---|---|---|
| `run.started` | `RUN_STARTED` | run_id / challenge 名 / category |
| `engine.log` | `TEXT_MESSAGE_DELTA`（或 `TOOL_CALL_START/RESULT`） | 日志 → 聊天区 agent 消息 |
| `blackboard.delta`(discovery/deadend/partial) | `INSIGHT_BUS_EVENT`(Fact/DeadEnd) + `BLACKBOARD_DELTA` | 黑板事实（actor/verified） |
| `intent.proposed/claimed/concluded` | `REASON_INTENT` + `BLACKBOARD_DELTA` | 意图生命周期（goal/worker/status） |
| `mode.changed` | `GUIDANCE_INJECTED` | 模式切换提示 → 聊天 system |
| `flag.solved` / `run.finished` | `INSIGHT_BUS_EVENT`(FlagFound) + `RUN_FINISHED` | 找到 flag → 结算 |
| solver 人工/候选 | `TEXT_MESSAGE_DELTA`(human) | HITL 回显 |

**简化策略**：Aemeath 是单题单引擎（mock/swarm）模型，映射时把所有 agent 输出归到 1 个 solver（coordinator 或 engine 名）。

## 3. 前端恢复范围

恢复 `frontend/` 为 muteki 完整版：复制 `ref/muteki/apps/web/ui` 全部内容（含 Conversation/Blackboard/GraphView/WorkerLanes/Terminal 等），仅改 `next.config`（代理 → adapter :8001）+ 后端兼容。

## 4. adapter 桥接层实现（核心契约）

- `RunManager`（仿 muteki run_manager.py）：维护 run 注册表 + 每题的黑板/事件订阅 + 元数据（pin/archive/folder）
- `GET /api/runs`：从平台 `fetch_all_challenges()` 生成 run 摘要（RunSummary）
- `POST /api/runs/{id}/start`：拉取题目 → 启动 Aemeath 引擎（swarm/mock）+ 总控
- `GET /api/runs/{id}/events`：该 run 的 SSE（复用 Aemeath 事件 → muteki 事件桥接）
- `POST /api/runs/{id}/hitl`：解析 target/action/text → 写黑板 intent / 总控指令
- auth 默认禁用（本地）；folders 用简单 JSON 持久化

## 5. 实施顺序

1. 恢复 frontend/ 为 muteki 完整版（复制 + next.config 改代理）
2. adapter 重构：RunManager + run 注册 + 平台→run 映射
3. auth（禁用模式）+ folders（简化）
4. run start（桥接引擎 + 总控）
5. run events（事件桥接）
6. hitl（→ 黑板/总控）
7. 联调验证（浏览器全链路）

## 6. 待确认范围

- [ ] **前端**：恢复全部 muteki 组件（Conversation/Blackboard/GraphView/WorkerLanes/Terminal 等）
- [ ] **adapter**：核心契约（auth 禁用/runs/start/events/hitl/folders 简化）
- [ ] **跳过**：terminal(WS 沙箱终端)、btw(side-query)、credentials、workers 管理（这些依赖 muteki CLI/沙箱体系，与 Aemeath 引擎重叠，后续按需补）
