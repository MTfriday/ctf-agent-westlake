# Aemeath — CTF 自动解题平台

<p align="center">
  <a href="https://www.gnu.org/licenses/agpl-3.0.html"><img src="https://img.shields.io/badge/license-AGPL--3.0-blue.svg" alt="License"></a>
  <img src="https://img.shields.io/badge/built%20with-vibe%20coding-ff69b4" alt="Vibe Coding">
</p>

一个基于 LLM 的 CTF（Capture The Flag）自动解题平台。它把多个 AI 模型组织成**求解蜂群（solver swarm）**并发攻击各道题目，由一个**总控 LLM（coordinator）**统一调度，并通过 muteki 契约桥接层接入一个 **Next.js 人机驾驶舱（Web Cockpit）**。

> **Vibe Coding 项目**：本项目主要由 AI 编程助手（GitHub Copilot）以 vibe coding 方式辅助开发迭代，人类开发者负责需求定义、审查与集成。代码质量与结构随迭代演进，欢迎 issue 与 PR 共同打磨。

## 系统架构

一个**总控 LLM（coordinator）**统筹整场比赛，多个**求解器（solver swarm）**并发攻击各道题目——每个求解器同时跑多个模型，先找到 Flag 者胜出。

```
                    +-----------------------+
                    |  平台客户端 (Platform) |
                    |  gzctf / ctf2 / ctfd  |
                    |  / generic            |
                    +----------+------------+
                               |
                    +----------v------------+
                    |  轮询器 (Poller)       |
                    +----------+------------+
                               |
                    +----------v------------+
                    |  总控 LLM (Coordinator)|
                    |  pydantic/claude/codex |
                    +----------+------------+
                               |
         +---------------------+---------------------+
         |                     |                     |
   +-----v------+       +------v-----+       +------v-----+
   | Swarm:     |       | Swarm:     |       | Swarm:     |
   | 题目 1     |       | 题目 2     |       | 题目 N     |
   | qwen-max   |       | qwen-max   |       |   ...      |
   | qwen-plus  |       | qwen-plus  |       |            |
   +-----+------+       +------+-----+       +------+-----+
         |                     |                     |
   +-----v------+       +------v-----+       +------v-----+
   | Docker     |       | Docker     |       | Docker     |
   | Sandbox    |       | Sandbox    |       | Sandbox    |
   | (隔离)     |       | (隔离)     |       | (隔离)     |
   +------------+       +------------+       +------------+

   ── 人机协同 (P6) ─────────────────────────────────────
   adapter/muteki 桥接层 (FastAPI :8001)
        ⇄ SSE 事件流 / REST / WebSocket 终端
        ⇄ Next.js 驾驶舱 (frontend/, :3999)
```

每个求解器运行在预装 CTF 工具的**隔离 Docker 容器**中。求解器永不放弃——它们会不断尝试不同思路，直到找到 Flag。

## 快速开始

### 1. 安装依赖

```bash
uv sync
```

### 2. 构建沙箱镜像

```bash
docker build -f sandbox/Dockerfile.sandbox -t ctf-sandbox .
```

### 3. 配置凭证

```bash
cp .env.example .env
# 编辑 .env 填入 LLM API Key 与平台凭证
# 编辑 config.yaml 选择平台类型（platform.type）
```

### 4. 命令行直接解题（CLI）

```bash
# 对接 CTFd 实例
uv run ctf-solve \
  --ctfd-url https://ctf.example.com \
  --ctfd-token ctfd_your_token \
  --challenges-dir challenges \
  --max-challenges 10 \
  -v

# 仅求解单题（跳过 coordinator）
uv run ctf-solve --challenge single_challenge
```

### 5. 启动 Web 驾驶舱（推荐）

见下方「驾驶舱（Web Cockpit — P6）」一节。

## 总控（Coordinator）后端

```bash
# pydantic（默认回退）—— 任意 OpenAI 兼容模型，无需 claude/codex CLI
uv run ctf-solve --coordinator pydantic --coordinator-model deepseek/deepseek-v4-flash ...

# Claude SDK
uv run ctf-solve --coordinator claude ...

# Codex CLI
uv run ctf-solve --coordinator codex ...
```

`--coordinator auto`（默认）会自动探测：无 claude/codex CLI 时回退到 pydantic。

## 求解器模型

默认模型阵容（可在 `backend/models.py` 配置，或通过 `--models` / `MODELS` 指定）：

| 模型              | Provider | 说明                  |
| ----------------- | -------- | --------------------- |
| qwen-max          | 阿里百炼 | 综合最优（总控/求解） |
| qwen-plus         | 阿里百炼 | 平衡速度与质量        |
| qwen-turbo        | 阿里百炼 | 快速，适合简单题      |
| deepseek-v4-flash | DeepSeek | 快速，适合简单题      |
| deepseek-v4-pro   | DeepSeek | 深度推理              |

未指定时自动检测可用 provider；支持 `bailian/<model>`、`deepseek/<model>`、`gateway/<model>`、`gateway-bailian/<model>` 等前缀。

- `gateway/deepseek-v4-flash`：西湖论剑比赛平台 llm-gateway 代理的 DeepSeek（`GATEWAY_BASE_URL` + `GATEWAY_API_KEY`）。
- `gateway-bailian/qwen3.7-plus` 等：西湖论剑比赛平台 llm-gateway 代理的百炼（`GATEWAY_BAILIAN_BASE_URL`，认证复用 `BAILIAN_API_KEY`），可绕过 MaaS 直连配额。
- 以上代理的 base_url 都是**完整 Chat Completions 端点**，系统会自动去掉 openai SDK 拼接的 `/chat/completions` 后缀。

示例：`MODELS=gateway/deepseek-v4-flash,gateway-bailian/qwen3.7-flash,gateway-bailian/qwen3.7-plus,gateway-bailian/qwen3.8-max`

### 查询百炼官方模型目录

`backend/bailian_models.py` 调用百炼官方 `GET /api/v1/models` 拉取可用模型列表
（含定价、上下文长度），认证用 `BAILIAN_API_KEY`，host 从 `BAILIAN_BASE_URL` 自动推导。
列表查询不消耗推理 token，配额耗尽时仍可用。

```bash
# 所有文本生成模型
python -m backend.bailian_models --capabilities TG
# Qwen 推理模型（作者 + 模态筛选）
python -m backend.bailian_models --providers qwen --capabilities TG Reasoning
# 按模型 ID 精确查询
python -m backend.bailian_models --model qwen3.7-plus
# 支持 --features / --min-context / --service-site / --supports 等筛选
```

## 沙箱工具链

每个求解器运行在预装 CTF 工具的隔离 Docker 容器中：

| 分类             | 工具                                                       |
| ---------------- | ---------------------------------------------------------- |
| **二进制** | radare2, GDB, objdump, binwalk, strings, readelf           |
| **Pwn**    | pwntools, ROPgadget, angr, unicorn, capstone               |
| **密码学** | SageMath, RsaCtfTool, z3, gmpy2, pycryptodome, cado-nfs    |
| **取证**   | volatility3, Sleuthkit (mmls/fls/icat), foremost, exiftool |
| **隐写**   | steghide, stegseek, zsteg, ImageMagick, tesseract OCR      |
| **Web**    | curl, nmap, Python requests, flask                         |
| **杂项**   | ffmpeg, sox, Pillow, numpy, scipy, PyTorch, podman         |

## 驾驶舱快速开始（P6）

```bash
# 1) 一次性安装依赖（Python + 前端）
bash setup.sh

# 2) 配置
vim .env             # 填入 API Key 与平台凭证
vim config.yaml      # 平台类型 / 速率限制 / 引擎后端等

# 3) 启动后端 adapter（:8001）
.venv/bin/python scripts/run_muteki_dev.py   # 开发模式（mock 引擎，无需平台/LLM）
# 或：python -m adapter                       # 真实模式（对接平台 + swarm 引擎）

# 4) 启动前端驾驶舱（:3999）
export NEXT_PUBLIC_MUTEKI_API=http://127.0.0.1:8001
npm --prefix frontend run dev

# 5) 打开 http://127.0.0.1:3999
```

> Windows 下使用 `set` 或 PowerShell 的 `$env:` 设置环境变量；前端端口固定为 `3999`（3000–3101 被系统保留）。

## 特性

- **多模型竞速** — 多个 AI 模型同时攻击每道题目，先解出者胜出
- **自动派发** — 检测到新题目自动开始攻击
- **总控 LLM** — 读取求解器轨迹，生成针对性的技术指导
- **跨求解器洞察** — 通过消息总线在模型间共享发现
- **Docker 沙箱** — 隔离容器，内置完整 CTF 工具链
- **操作员消息** — 比赛中可向运行中的求解器发送提示
- **📊 Web 驾驶舱（Aemeath）** — Next.js 人机驾驶舱（P6，替代旧 Streamlit 仪表盘）：
  - 黑板面板（已验证事实 / 死路 / 意图）
  - 总控台（模式徽标、锁定、OODA 日志）
  - 实时 SSE 事件流
  - 题目总览（已解 / 进行中 / 失败状态卡片）
  - 会话式对话 + 事实图（Cytoscape DAG）+ React Flow 黑板画布
  - xterm.js WebSocket 终端
  - Worker 设置（多 LLM 端点、引擎后端、CNY/USD 成本预算）
- **🧩 题型专用求解器** — 自动题目分类：
  - 逆向工程（angr、pyghidra、radare2）
  - 二进制利用（pwntools、ROPgadget、checksec）
  - Web 安全（curl、header 分析、XSS/SSRF 工具）
  - 密码学（RsaCtfTool、z3、SageMath）
  - 杂项（zsteg、binwalk、steghide、exiftool）
  - 取证（Sleuthkit、volatility3、foremost）
- **🛡️ 沙箱安全** — 危险命令检测（rm -rf /、fork bomb 等）
- **🔐 可配置 Flag 校验** — 按赛事自定义正则
- **🔗 通用平台客户端** — 兼容任意 REST 风格 CTF 平台

## 配置

### 配置设计（单一真相源）

配置采用 **`config.yaml`（非敏感结构）+ `.env`（敏感凭证）** 双层设计，
由 `Settings` 统一合并，优先级：**环境变量 > .env > config.yaml > 默认值**。

### config.yaml（非敏感，可提交 git）

```yaml
platform:
  type: ctf2            # "generic" | "ctfd" | "ctf2" | "gzctf"
  auth_type: "ApiKey"
  api_base_url: "https://ctf.example.com"
  api_path: "/api/open/v1"
  # auth_credential: "" # SECRET — 不要写这里！从 .env 的 PLATFORM_AUTH_CREDENTIAL 读取

solver:
  max_concurrent_challenges: 10
  max_attempts_per_challenge: 3
  models: ""            # 逗号分隔模型规格，留空=自动检测
  # Coordinator 后端: "claude" | "codex" | "pydantic" | "auto"
  # pydantic 可用任意 OpenAI 兼容模型（DeepSeek/百炼），无需 claude/codex CLI
  coordinator: "auto"
  coordinator_model: "" # 如 "deepseek/deepseek-v4-flash"（仅 pydantic 后端生效）

flag:
  pattern: ""  # 自定义正则，例如 "CTF\\{[^}]+\\}"
  min_length: 1

rate_limit:
  enabled: true
  requests_per_second: 5
```

### 平台选择

`platform.type` 决定使用哪个平台客户端，运行时自动适配：

| type        | 客户端                              | 说明                                   |
| ----------- | ----------------------------------- | -------------------------------------- |
| `ctf2`    | CTF2Client + PlatformAdapter        | ctf2.dasctf.com 专用                   |
| `gzctf`   | GZCTFClient + PlatformAdapter       | GZCTF 平台（150.158.131.227:65534 等） |
| `generic` | GenericRestClient + PlatformAdapter | 任意 REST API 平台                     |
| `ctfd`    | CTFdClient                          | 原 CTFd 路径                           |

**CTF2 注意**：竞赛题目（竞赛→阶段→题目）需要账号**先加入参赛队伍**，否则返回 `TEAM_MEMBERSHIP_REQUIRED`（403）。练习场当前用户 API 无批量题目列表端点，题目需按 ID 单独获取。

**GZCTF 注意**：

- 凭据从 `.env` 读取：`GZCTF_TOKEN`（推荐，`Authorization: Bearer`）或 `GZCTF_USERNAME` / `GZCTF_PASSWORD`（会话登录）
- 平台 URL 在 `config.yaml` 的 `platform.api_base_url` 设置（如 `https://ctf.example.com`）
- 题目列表（`GET /api/game/{id}/details`）要求账号**先加入比赛队伍**，否则返回 403
- 动态容器题目通过 `POST /api/game/{id}/container/{cid}` 创建实例（`instanceEntry` 为连接信息）
- Flag 提交为两段式：提交后拿 submitId 再查询状态，`Accepted` 即正确

### Coordinator 选择

`--coordinator` / `solver.coordinator` 支持：

- `pydantic`：用任意 OpenAI 兼容模型（DeepSeek/百炼），**无需 claude/codex CLI**
- `claude`：Claude Agent SDK
- `codex`：Codex CLI
- `auto`（默认）：自动选择，无 claude/codex 时回退到 pydantic

### .env（敏感凭证，不提交 git）

```env
# 平台认证凭证（根据 config.yaml 的 platform.type 使用）
PLATFORM_AUTH_CREDENTIAL=ctf2_your_token_here   # CTF2 / 通用平台
GZCTF_TOKEN=your_gzctf_token_here               # GZCTF 模式（推荐）
GZCTF_USERNAME=your_username                    # GZCTF 会话登录（可选）
GZCTF_PASSWORD=your_password                    # GZCTF 会话登录（可选）
CTFD_URL=https://ctf.example.com                # CTFd 模式
CTFD_TOKEN=ctfd_your_token

ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
GEMINI_API_KEY=...

# DeepSeek — OpenAI 兼容接口
DEEPSEEK_API_KEY=sk-...
DEEPSEEK_BASE_URL=https://api.deepseek.com

# 阿里百炼 (Alibaba Bailian) — OpenAI 兼容接口
BAILIAN_API_KEY=sk-...
BAILIAN_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
```

所有设置也可通过环境变量或 CLI 参数传入（环境变量同时覆盖 `.env` 与 `config.yaml`）。

### 使用阿里百炼（Alibaba Bailian）

[百炼（阿里云模型服务）](https://help.aliyun.com/zh/model-studio/) 提供 OpenAI 兼容接口。模型以 `bailian/<model-name>` 指定：

```bash
# 添加百炼模型到求解阵容
uv run ctf-solve --models bailian/qwen-max ...
```

支持的百炼模型：`qwen-max`、`qwen-plus`、`qwen-turbo`

## 驾驶舱（Web Cockpit）

复用 **muteki Web UI**（`frontend/`，Next.js 14）作为 Aemeath 人机驾驶舱，通过 `adapter/muteki/` 桥接层把 Aemeath 执行层包装为 muteki 前端契约（REST + SSE + WebSocket）。

```bash
# 1) 启动后端 adapter（:8001，提供 /api/* + SSE）
python -m adapter                                   # 真实引擎（adapter_engine_backend=swarm）
.venv/bin/python scripts/run_muteki_dev.py         # 开发模式（mock 引擎，无需平台/LLM）

# 2) 启动前端（:3999，直连 adapter SSE）
export NEXT_PUBLIC_MUTEKI_API=http://127.0.0.1:8001
npm --prefix frontend run dev

# 3) 打开 http://127.0.0.1:3999
```

驾驶舱功能（`frontend/components/`）：`Conversation` 会话式主控台、`GraphView` 事实图、`Blackboard` 黑板、`SolverRace` 竞速、`Terminal` 终端、`WorkerSettings` 智能体配置、`BtwPanel` 实时问答。

桥接层（`adapter/muteki/`）：`bridge.py` 事件桥（Aemeath Event → muteki 事件）、`manager.py` RunManager（题目自动注册为 run）、`routes.py` 全部契约端点、`auth.py` 单密码门禁。

## 项目结构

```
ctf-agent-westlake/
├── backend/                  # 求解引擎后端（平台客户端 / agent / 沙箱 / CLI）
│   ├── agents/               # 求解后端（Claude SDK、Codex、Pydantic AI）+ coordinator
│   ├── platforms/            # 抽象平台客户端层
│   │   ├── base.py               # PlatformClient 抽象接口
│   │   ├── ctfd_adapter.py       # CTFd 适配器
│   │   ├── ctf2_client.py        # CTF2 平台客户端（ctf2.dasctf.com）
│   │   ├── gzctf_client.py       # GZCTF 平台客户端
│   │   ├── generic.py            # 任意 REST 平台客户端
│   │   └── rate_limiter.py       # 令牌桶速率限制
│   ├── tools/               # 求解器工具实现
│   ├── sandbox.py           # Docker 沙箱 + 危险命令检测
│   ├── task_manager.py      # 任务状态机（NEW→…→SOLVED/NEEDS_HUMAN）
│   ├── config.py            # Pydantic Settings
│   └── models.py            # 模型解析（含 Bailian provider）
├── solvers/                 # 题型专用求解器
│   ├── router.py            # 题目分类（rev/pwn/web/crypto/misc）
│   ├── base_solver.py       # 工具注册表 + 提示词模板
│   ├── rev_solver.py / pwn_solver.py / web_solver.py / crypto_solver.py / misc_solver.py
├── adapter/                 # Aemeath 执行层（FastAPI：/api/*、SSE，:8001）
│   ├── main.py              # 应用入口（挂载全部路由）
│   ├── runtime.py           # SolverRuntime + EventBus
│   ├── engines.py           # 可插拔引擎后端（Mock / Swarm）
│   ├── events.py            # Aemeath 类型化事件
│   ├── routes/              # challenge / blackboard / solver / flag / orchestrator / stream
│   └── muteki/              # muteki 契约桥接层（auth / bridge / manager / routes）
├── frontend/                # muteki Web UI 复用（Next.js 14，:3999）
│   ├── app/                 # 页面（主控台 / run 详情）
│   ├── components/          # 驾驶舱组件（Conversation / GraphView / Blackboard / …）
│   ├── lib/                 # API 客户端、i18n、平台工具（跨平台快捷键）
│   └── package.json
├── src/                     # Aemeath 认知层
│   ├── blackboard/          # 共享知识黑板（SQLite WAL）
│   └── orchestrator/        # OODA 循环 + 模式路由（swarm/orchestrated/hybrid）
├── scripts/                 # 开发/测试脚本
│   ├── run_muteki_dev.py    # mock 引擎启动器（:8001）
│   ├── integration_test.py  # P7 全链路集成测试（27 项断言）
│   └── verify_muteki.py
├── prompts/                 # Aemeath 系统提示词（认知层）
├── sandbox/                 # Docker 沙箱定义（Dockerfile.sandbox）
├── dashboard/               # （遗留）旧 Streamlit 仪表盘，已被 P6 驾驶舱取代
├── challenges/              # 本地题目附件目录
├── tests/                   # 测试套件（test_system.py 等）
├── config.yaml              # 主配置
├── main.py                  # 统一入口
├── pull_challenges.py       # 从平台拉取题目附件
├── setup.sh                 # 一键安装
└── .env.example
```

## 环境要求

- Python 3.14+
- Docker
- 至少一个 LLM Provider 的 API Key（Anthropic / OpenAI / Google / 阿里百炼 / DeepSeek）
- `codex` CLI（可选，用于 Codex 求解器/总控）
- `claude` CLI（可选，随 claude-agent-sdk 提供）
- Node.js 18+（仅运行 Web 驾驶舱时需要）

## 自定义 Flag 格式

编辑 `config.yaml` 或设置环境变量：

```yaml
flag:
  pattern: "FLAG\\{[A-F0-9]+\\}"   # 自定义正则
  min_length: 8
  max_submit: 50                  # 每题最大提交次数（0=不限制）
```

设为空字符串 `""` 可完全禁用校验。

### 西湖论剑 flag 规则（slab 平台自动处理）

- flag 格式为 `DASCTF{...}` 或 `flag{...}`，提交时**仅需提交 `{}` 内内容**。
- 系统提交前会自动归一化（`normalize_flag`）剥掉 `DASCTF{}` / `flag{}` 外壳；`CTF{...}` 等其它外壳或题目自定义特殊格式原样保留。
- 每题 flag **最大提交 50 次**，达到上限后系统会停止提交并提示求解器深入分析（`FLAG_MAX_SUBMIT` 可调，0=不限制）。

### 动态靶机并发限制（slab 平台）

- 平台同时最多可开 **3 台**动态靶机（西湖论剑限制），超限会被平台拒绝。
- 系统用信号量限制同时运行的靶机环境数 ≤ `max_platform_env`（默认 3；`config.yaml platform.max_env` / `.env MAX_PLATFORM_ENV`，0=不限制）。
- 环境名额在启动时占用、该题求解结束自动释放（引擎层 `_start_platform_env` / `_run_swarm`）。

## 自定义平台 API

对非 CTFd 平台，在 `config.yaml` 中配置：

```yaml
platform:
  type: generic
  api_base_url: "https://your-platform-api.com"
  auth_type: "Bearer"     # Bearer、ApiKey 或 Header
  auth_credential: ""     # 或通过环境变量 TEAM_TOKEN 设置
  endpoints:
    list_challenges: "/v1/challenges"
    get_challenge: "/v1/challenge?id={id}"
    download_attachment: "/v1/attachment?id={id}"
    submit_flag: "/v1/flag"
```

`GenericRestClient` 会自动识别常见 API 响应格式（success/data 信封、直接数组等）。

## 致谢

- [es3n1n/Eruditus](https://github.com/es3n1n/Eruditus) — `pull_challenges.py` 中的 CTFd 交互与 HTML 辅助
- [FishCodeTech/muteki](https://github.com/FishCodeTech/muteki) — 前端借鉴

## License

本项目基于 **[GNU AGPL v3.0](https://www.gnu.org/licenses/agpl-3.0.html)** 开源（与上游 [FishCodeTech/muteki](https://github.com/FishCodeTech/muteki) 一致的协议）。详见 [LICENSE](./LICENSE)。
