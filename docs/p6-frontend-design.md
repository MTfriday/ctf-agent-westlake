# P6 设计：Aemeath Web 驾驶舱（模块五）

> 版本：1.0 · 2026-08-09
> 目标：基于 Muteki Next.js 基底，实现「共享黑板面板 + 总控指挥面板 + 求解控制台」的人机协同驾驶舱，对接 P3/P5 的 adapter `/api/*` 与 SSE 事件流。

---

## 1. 总原则

- **借鉴 Muteki 架构**：Next.js 14 + TypeScript + Tailwind + SSE 客户端 + 同源 `/api` 代理（`next.config.mjs` 的 `compress: false` 防 SSE gzip 缓冲是关键，必须保留）。
- **复制可复用基底**：`next.config.mjs`、`tsconfig.json`、`tailwind`、`globals.css`、基础 UI 组件（`Icon`/`Skeleton`/`Toast`/`ChipFilterBar`/`CopyText` 等）。
- **业务页面重写**：muteki 的 `Deck` 是面向 muteki 后端（run/events/HITL）的对话甲板；P6 重写为 **Aemeath 驾驶舱**（题目 → 详情：黑板 + 总控 + 求解），事件流改用 adapter 的 EventType。
- **BlackboardPanel / OrchestratorDashboard 严格按蓝皮书**：列表式面板（🟢Facts / 🔴Deadends / 🕐Pending Intents + 按钮），不复制 muteki 的 React Flow 自由画布。

## 2. 目录结构（工作区 `frontend/`）

```
frontend/
├── package.json            # 基于 muteki 精简（去掉 xterm/cytoscape/animejs 等用不到的）
├── next.config.mjs         # 复制 muteki：output=standalone、compress:false、/api → :8001
├── tsconfig.json / tailwind.config / postcss.config
├── app/
│   ├── globals.css         # 复制 muteki（含 CSS 变量主题）
│   ├── layout.tsx          # 顶层布局 + 导航
│   ├── page.tsx            # 首页：题目列表驾驶舱
│   └── problem/[id]/page.tsx   # 题目详情页（父页面，集成四面板）
├── lib/
│   ├── api.ts              # Aemeath REST 客户端（无认证，同源 /api）
│   ├── events.ts           # Aemeath EventType + Event 类型（与 adapter/events.py 对齐）+ SSE hook
│   └── format.ts           # 时间/时长/颜色工具
└── components/
    ├── BlackboardPanel.tsx        # 模块五·改造一：共享黑板面板
    ├── OrchestratorDashboard.tsx  # 模块五·改造二：总控指挥面板
    ├── ChallengeList.tsx          # 题目列表
    ├── ProblemHeader.tsx          # 题目信息 + Flag 提交（门禁）
    ├── SolverConsole.tsx          # 求解启动/停止 + 实时日志（SSE）
    └── ui/                        # 从 muteki 复制的基础组件
```

## 3. 页面与组件

### 3.1 首页 `/` — ChallengeList
- `GET /api/challenges` → 题目卡片（名称/分类/分值/solved 徽章）
- 点击卡片 → 跳转 `/problem/[id]`

### 3.2 详情页 `/problem/[id]`（父页面）
四面板布局（可折叠）：
1. **ProblemHeader**：题目信息 + Flag 提交框 → `POST /api/flag/submit`（展示门禁结果）
2. **BlackboardPanel**（模块五·改造一）
3. **OrchestratorDashboard**（模块五·改造二）
4. **SolverConsole**：启动求解（`POST /api/solver/start`，mode 可选）+ 状态轮询 + SSE 实时日志（`engine.log`）

### 3.3 BlackboardPanel.tsx（改造一）
- 数据源：`GET /api/blackboard/{problem_id}`（facts + intents + stats 快照），SSE `blackboard.delta` 实时增量
- 渲染（蓝皮书要求）：
  - 🟢 **Facts**（发现，type=discovery）
  - 🔴 **Deadends**（死路，type=deadend）
  - 🟡 **Partials**（部分/候选）
  - 🕐 **Pending Intents**（待认领/执行中，status=pending/claimed）
- 按钮：
  - **「强制注入上下文」** → `POST /api/blackboard/{problem_id}/inject`（把黑板摘要注入后续 solver 的 System Prompt）
  - **「重置此题黑板」** → `POST /api/blackboard/{problem_id}/reset`（清空记忆，确认弹窗）

### 3.4 OrchestratorDashboard.tsx（改造二）
- 数据源：`GET /api/orchestrator/status/{problem_id}` 轮询 + SSE（`mode.changed`/`intent.*`/`run.status`）
- 渲染（蓝皮书要求）：
  - **模式徽章**：`current_mode` + `mode_label`（🏎️竞速/🧠总控/🔀混合）+ `mode_reason`（LLM 决策理由）
  - **模式锁定开关**：Toggle → 下拉（swarm/orchestrated/hybrid）→ `POST /api/orchestrator/mode/lock`（null 解锁）
  - **控制按钮**：启动（`/start`）、暂停（`/pause`）、恢复（`/resume`）、停止（`/stop`）
  - **实时决策日志**：SSE 滚动（MODE_CHANGED / INTENT_PROPOSED / INTENT_CLAIMED / INTENT_CONCLUDED / RUN_STATUS）
  - **人工接管输入框**：文本框（action_type + 说明）→ `POST /api/orchestrator/intent/manual`

### 3.5 SolverConsole.tsx
- 启动求解（`POST /api/solver/start`，mode 下拉：auto/swarm/orchestrated/hybrid）
- 状态轮询 `GET /api/solver/status/{run_id}` + 日志尾部
- SSE `engine.log` 实时追加；求解完成显示候选 Flag（可一键提交）

## 4. API 契约（对接 adapter）

| 端点 | 用途 | 状态 |
|------|------|------|
| `GET /api/challenges` | 题目列表 | **P6 新增** |
| `POST /api/challenge/prepare` | 题目详情 + 黑板上下文 | 已有(P3) |
| `POST /api/solver/start` / `GET status/{run_id}` | 求解 | 已有(P3) |
| `POST /api/flag/submit` / `GET verify/{id}` | Flag 门禁 | 已有(P3) |
| `/api/orchestrator/*` | 总控控制 | 已有(P5) |
| `GET /api/blackboard/{problem_id}` | 黑板快照 | **P6 新增** |
| `POST /api/blackboard/{problem_id}/reset` | 重置黑板 | **P6 新增** |
| `POST /api/blackboard/{problem_id}/inject` | 注入上下文动作 | **P6 新增** |
| `GET /api/events/stream` | SSE 实时事件 | 已有(P3) |

**solver/start 增强**：启动时自动把 `blackboard.get_context(problem_id)` 拼入 `prompt`（Solver 执行前注入历史/死路），实现蓝皮书「执行前注入」闭环。

## 5. 运行方式

```bash
# 后端（:8001）
python -m adapter

# 前端（:3001，/api 同源代理到 :8001）
cd frontend && npm install && npm run dev
```

浏览器打开 `http://localhost:3001`。

## 6. 实施顺序

1. **后端小端点**（P6 配套）：`/api/challenges`、`/api/blackboard/*`、solver/start 注入黑板
2. **前端脚手架**：复制 muteki 基底 + 精简依赖 + `next.config` 代理到 8001
3. **lib**：`api.ts` + `events.ts`（Aemeath EventType + SSE hook）
4. **首页 ChallengeList**
5. **详情页骨架** + ProblemHeader + SolverConsole
6. **BlackboardPanel**（改造一）
7. **OrchestratorDashboard**（改造二）
8. **联调冒烟**：mock 引擎全链路（列表 → 详情 → 黑板 → 总控 → 求解 → SSE）

## 7. 待确认

- [ ] 前端目录用 `frontend/` 命名？
- [ ] 复制 muteki 基础组件范围（仅 ui/ 通用件，不搬 GraphView/WorkerLanes/Terminal 等 muteki 特定件）？
- [ ] 新增 4 个后端小端点 OK？
