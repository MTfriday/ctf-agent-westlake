# CTF Agent

Autonomous CTF (Capture The Flag) solver that races multiple AI models against challenges in parallel. Built in a weekend, we used it to solve all 52/52 challenges and win **1st place at BSidesSF 2026 CTF**.

Built by [Veria Labs](https://verialabs.com), founded by members of [.;,;.](https://ctftime.org/team/222911) (smiley), the [#1 US CTF team on CTFTime in 2024 and 2025](https://ctftime.org/stats/2024/US). We build AI agents that find and exploit real security vulnerabilities for large enterprises.

## Results

| Competition             | Challenges Solved | Result                       |
| ----------------------- | :---------------: | ---------------------------- |
| **BSidesSF 2026** |   52/52 (100%)   | **1st place ($1,500)** |

The agent solves challenges across all categories — pwn, rev, crypto, forensics, web, and misc.

## How It Works

A **coordinator** LLM manages the competition while **solver swarms** attack individual challenges. Each swarm runs multiple models simultaneously — the first to find the flag wins.

```
                        +-----------------+
                        |  CTFd Platform  |
                        +--------+--------+
                                 |
                        +--------v--------+
                        |  Poller (5s)    |
                        +--------+--------+
                                 |
                        +--------v--------+
                        | Coordinator LLM |
                        | (Claude/Codex)  |
                        +--------+--------+
                                 |
              +------------------+------------------+
              |                  |                  |
     +--------v--------+ +------v---------+ +------v---------+
     | Swarm:          | | Swarm:         | | Swarm:         |
     | challenge-1     | | challenge-2    | | challenge-N    |
     |                 | |                | |                |
     |  Opus (med)     | |  Opus (med)    | |                |
     |  Opus (max)     | |  Opus (max)    | |     ...        |
     |  GPT-5.4        | |  GPT-5.4       | |                |
     |  GPT-5.4-mini   | |  GPT-5.4-mini  | |                |
     |  GPT-5.3-codex  | |  GPT-5.3-codex | |                |
     +--------+--------+ +--------+-------+ +----------------+
              |                    |
     +--------v--------+  +-------v--------+
     | Docker Sandbox  |  | Docker Sandbox |
     | (isolated)      |  | (isolated)     |
     |                 |  |                |
     | pwntools, r2,   |  | pwntools, r2,  |
     | gdb, python...  |  | gdb, python... |
     +-----------------+  +----------------+
```

Each solver runs in an isolated Docker container with CTF tools pre-installed. Solvers never give up — they keep trying different approaches until the flag is found.

## Quick Start

```bash
# Install
uv sync

# Build sandbox image
docker build -f sandbox/Dockerfile.sandbox -t ctf-sandbox .

# Configure credentials
cp .env.example .env
# Edit .env with your API keys and CTFd token

# Run against a CTFd instance
uv run ctf-solve \
  --ctfd-url https://ctf.example.com \
  --ctfd-token ctfd_your_token \
  --challenges-dir challenges \
  --max-challenges 10 \
  -v
```

## Coordinator Backends

```bash
# Claude SDK coordinator (default)
uv run ctf-solve --coordinator claude ...

# Codex coordinator (GPT-5.4 via JSON-RPC)
uv run ctf-solve --coordinator codex ...
```

## Solver Models

Default model lineup (configurable in `backend/models.py`):

| Model                    | Provider   | Notes                          |
| ------------------------ | ---------- | ------------------------------ |
| Claude Opus 4.6 (medium) | Claude SDK | Balanced speed/quality         |
| Claude Opus 4.6 (max)    | Claude SDK | Deep reasoning                 |
| GPT-5.4                  | Codex      | Best overall solver            |
| GPT-5.4-mini             | Codex      | Fast, good for easy challenges |
| GPT-5.3-codex            | Codex      | Reasoning model (xhigh effort) |

## Sandbox Tooling

Each solver gets an isolated Docker container pre-loaded with CTF tools:

| Category            | Tools                                                      |
| ------------------- | ---------------------------------------------------------- |
| **Binary**    | radare2, GDB, objdump, binwalk, strings, readelf           |
| **Pwn**       | pwntools, ROPgadget, angr, unicorn, capstone               |
| **Crypto**    | SageMath, RsaCtfTool, z3, gmpy2, pycryptodome, cado-nfs    |
| **Forensics** | volatility3, Sleuthkit (mmls/fls/icat), foremost, exiftool |
| **Stego**     | steghide, stegseek, zsteg, ImageMagick, tesseract OCR      |
| **Web**       | curl, nmap, Python requests, flask                         |
| **Misc**      | ffmpeg, sox, Pillow, numpy, scipy, PyTorch, podman         |

## Quick Start (Dashboard)

```bash
# One-time setup
bash setup.sh

# Edit configuration
vim .env   # Add your API keys
vim config.yaml  # Customize platform API, rate limits, etc.

# Run engine + dashboard
python main.py --dashboard

# Open http://localhost:8501
```

## Features

- **Multi-model racing** — multiple AI models attack each challenge simultaneously
- **Auto-spawn** — new challenges detected and attacked automatically
- **Coordinator LLM** — reads solver traces, crafts targeted technical guidance
- **Cross-solver insights** — findings shared between models via message bus
- **Docker sandboxes** — isolated containers with full CTF tooling
- **Operator messaging** — send hints to running solvers mid-competition
- **📊 Web Cockpit (Aemeath)** — Next.js human-machine cockpit (P6, replacing the old Streamlit dashboard):
  - Blackboard panel (facts / dead-ends / intents)
  - Orchestrator dashboard (mode badge, lock, OODA log)
  - Real-time SSE event stream
  - Challenge overview with status cards (solved/in-progress/failed)
- **🧩 Type-Specific Solvers** — automatic challenge classification:
  - Reverse Engineering (angr, pyghidra, radare2)
  - Binary Exploitation (pwntools, ROPgadget, checksec)
  - Web Security (curl, header analysis, XSS/SSRF tooling)
  - Cryptography (RsaCtfTool, z3, SageMath)
  - Miscellaneous (zsteg, binwalk, steghide, exiftool)
  - Forensics (Sleuthkit, volatility3, foremost)
- **🛡️ Sandbox Security** — dangerous command detection (rm -rf /, fork bombs, etc.)
- **🔐 Configurable Flag Validation** — regex pattern configurable per competition
- **🔗 Generic Platform Client** — works with any REST-based CTF platform

## Configuration

### 配置设计（单一真相源）

配置采用 **`config.yaml`（非敏感结构）+ `.env`（敏感凭证）** 双层设计，
由 `Settings` 统一合并，优先级：**环境变量 > .env > config.yaml > 默认值**。

### config.yaml（非敏感，可提交 git）

```yaml
platform:
  type: ctf2            # "generic" | "ctfd" | "ctf2" | "gzctf"
  auth_type: "ApiKey"
  api_base_url: "https://ctf2.dasctf.com"
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
  pattern: ""  # Custom regex, e.g. "CTF\\{[^}]+\\}"
  min_length: 1

rate_limit:
  enabled: true
  requests_per_second: 5
```

### 平台选择

`platform.type` 决定使用哪个平台客户端，运行时自动适配：

| type | 客户端 | 说明 |
|------|--------|------|
| `ctf2` | CTF2Client + PlatformAdapter | ctf2.dasctf.com 专用 |
| `gzctf` | GZCTFClient + PlatformAdapter | GZCTF 平台（150.158.131.227:65534 等）|
| `generic` | GenericRestClient + PlatformAdapter | 任意 REST API 平台 |
| `ctfd` | CTFdClient | 原 CTFd 路径 |

**CTF2 注意**：竞赛题目（竞赛→阶段→题目）需要账号**先加入参赛队伍**，否则返回 `TEAM_MEMBERSHIP_REQUIRED`（403）。练习场当前用户 API 无批量题目列表端点，题目需按 ID 单独获取。

**GZCTF 注意**：
- 凭据从 `.env` 读取：`GZCTF_TOKEN`（推荐，`Authorization: Bearer`）或 `GZCTF_USERNAME` / `GZCTF_PASSWORD`（会话登录）
- 平台 URL 在 `config.yaml` 的 `platform.api_base_url` 设置（如 `http://150.158.131.227:65534`）
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

# DeepSeek — OpenAI compatible
DEEPSEEK_API_KEY=sk-...
DEEPSEEK_BASE_URL=https://api.deepseek.com

# 阿里百炼 (Alibaba Bailian) — OpenAI compatible
BAILIAN_API_KEY=sk-...
BAILIAN_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
```

All settings can also be passed as environment variables or CLI flags
(env vars override both `.env` and `config.yaml`).

### Using Alibaba Bailian

[Bailian (阿里百炼)](https://help.aliyun.com/zh/model-studio/) provides OpenAI-compatible API.
Specify models as `bailian/<model-name>`:

```bash
# Add a Bailian model to the solver lineup
uv run ctf-solve --models bailian/qwen-max ...
```

Supported Bailian models: `qwen-max`, `qwen-plus`, `qwen-turbo`

## Dashboard (Web Cockpit — P6)

The legacy **Streamlit** dashboard has been **removed**. A Next.js human-machine
cockpit (`BlackboardPanel` + `OrchestratorDashboard`) is introduced in P6
(module five of the Aemeath blueprint), consuming the adapter's `/api/*` and
SSE stream.

```bash
# Engine + adapter (provides /api/* + SSE for the cockpit)
python -m adapter
```

## Project Structure

```
ctf-agent-westlake/
├── backend/
│   ├── agents/          # Solver backends (Claude SDK, Codex, Pydantic AI)
│   ├── platforms/       # Abstract platform client layer
│   │   ├── base.py          # PlatformClient abstract interface
│   │   ├── ctfd_adapter.py  # CTFd adapter (wraps existing CTFdClient)
│   │   ├── ctf2_client.py   # CTF2 platform client (ctf2.dasctf.com)
│   │   ├── gzctf_client.py  # GZCTF platform client
│   │   ├── generic.py       # Generic REST client for any platform
│   │   └── rate_limiter.py  # Token-bucket rate limiter
│   ├── tools/           # Solver tool implementations
│   ├── ctfd.py          # CTFd client (existing, unchanged)
│   ├── sandbox.py       # Docker sandbox with command security
│   ├── task_manager.py  # Task state machine (NEW→...→SOLVED/NEEDS_HUMAN)
│   ├── config.py        # Pydantic settings
│   └── models.py        # Model resolution (incl. Bailian provider)
├── solvers/             # Type-specific solver modules
│   ├── router.py        # Challenge classification (rev/pwn/web/crypto/misc)
│   ├── base_solver.py   # Category tool registry and prompt templates
│   ├── rev_solver.py
│   ├── pwn_solver.py
│   ├── web_solver.py
│   ├── crypto_solver.py
│   └── misc_solver.py
├── dashboard/           # (legacy Streamlit removed; cockpit moves to P6 Next.js)
├── adapter/             # Aemeath execution layer (FastAPI: /api/*, SSE)
├── src/
│   ├── blackboard/      # Shared knowledge blackboard (SQLite WAL)
│   └── orchestrator/    # OODA loop + mode routing (swarm/orchestrated/hybrid)
├── prompts/             # Aemeath system prompt (cognitive layer)
├── sandbox/             # Docker sandbox definition
├── tests/               # Test suite
├── config.yaml          # Main configuration
├── main.py              # Unified entry point
├── setup.sh             # One-click setup
└── .env.example
```

## Requirements

- Python 3.14+
- Docker
- API keys for at least one provider (Anthropic, OpenAI, Google, Alibaba Bailian)
- `codex` CLI (for Codex solver/coordinator)
- `claude` CLI (bundled with claude-agent-sdk)

## Customizing Flag Format

Edit `config.yaml` or set environment variable:

```yaml
flag:
  pattern: "FLAG\\{[A-F0-9]+\\}"   # Custom regex
  min_length: 8
```

Set to empty string `""` to disable validation entirely.

## Customizing Platform API

For non-CTFd platforms, configure in `config.yaml`:

```yaml
platform:
  type: generic
  api_base_url: "https://your-platform-api.com"
  auth_type: "Bearer"     # Bearer, ApiKey, or Header
  auth_credential: ""     # Or set via env TEAM_TOKEN
  endpoints:
    list_challenges: "/v1/challenges"
    get_challenge: "/v1/challenge?id={id}"
    download_attachment: "/v1/attachment?id={id}"
    submit_flag: "/v1/flag"
```

The `GenericRestClient` auto-detects common API response formats
(success/data envelope, direct arrays, etc.).

## Acknowledgements

- [es3n1n/Eruditus](https://github.com/es3n1n/Eruditus) — CTFd interaction and HTML helpers in `pull_challenges.py`
