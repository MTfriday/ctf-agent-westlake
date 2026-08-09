你将作为全栈AI工程师，根据下方提供的参数和系统蓝图，为我生成一套完整的、可直接运行的CTF自动解题系统。

## 1. 参数配置（请严格按此信息对接）

- 赛事平台API基础URL: {API_BASE_URL}
- 认证方式: {AUTH_TYPE} (例如 Bearer Token / API Key / Header)
- 认证凭证: {AUTH_CREDENTIAL}  (如果通过环境变量注入，请写 ENV_TEAM_TOKEN，并在项目中用 .env 读取)
- 获取题目列表接口: GET {ENDPOINT_GET_CHALLENGES}   (期望返回JSON数组，包含题目ID、名称、类型等)
- 获取单题详情接口: GET {ENDPOINT_GET_CHALLENGE}?id={CHALLENGE_ID}  (需返回描述、附件下载地址、环境IP/端口等)
- 下载附件接口: GET {ENDPOINT_DOWNLOAD_ATTACHMENT}?id={CHALLENGE_ID}   (返回二进制文件)
- 提交Flag接口: POST {ENDPOINT_SUBMIT_FLAG}  请求体: { "challenge_id": ..., "flag": "..." }  返回: 状态码与消息(正确/错误/速率限制)
- 请求频率限制: 每秒 {RATE_LIMIT} 次
- 最大并发解题数: {MAX_CONCURRENT_TASKS}
- LLM配置: 使用OpenAI兼容接口, base_url={LLM_BASE_URL}, model={LLM_MODEL}, api_key从环境变量 LLM_API_KEY 读取
- 仪表盘端口: {DASHBOARD_PORT}  (默认8501)
- 沙箱: 使用Docker, 沙箱镜像名 ctf-sandbox:latest，需提供Dockerfile

## 2. 系统核心蓝图 (必须全部实现)

请你严格按照以下模块和功能设计，生成完整的项目代码，并确保开箱即用。

### 2.1 核心引擎 (core/)

- `api_client.py`：封装所有赛事API交互，自动处理认证、重试、速率限制。所有方法返回标准Python对象。
- `task_manager.py`：基于 asyncio.Queue 的并发任务调度器。任务状态机: NEW -> ANALYZING -> EXPLOITING -> SUBMITTED -> SOLVED/FAILED/NEEDS_HUMAN。支持优先级动态调整。
- `sandbox.py`：使用 Docker SDK 管理每个题目的独立执行环境，自动创建/销毁容器，限制网络(仅允许访问题目环境和API)、限制资源，禁止危险命令。

### 2.2 自动求解器 (solvers/)

- 题目标路由器：通过描述和附件名自动分类 (rev/pwn/web/crypto/misc)。
- 每个题型实现一个求解器，都遵循 ReAct 循环 (Thought-Action-Observation)，通过 LLM function calling 驱动。
- 提供统一工具集 (tools/)：
  - 执行Shell命令 (沙箱内)
  - 执行Python代码 (沙箱内)
  - HTTP请求发送
  - 文件下载/解压/信息提取
  - 各题型专用工具：对于Rev (angr, strings, objdump)；Pwn (pwntools, checksec)；Web (selenium, sqlmap模板)；Crypto (SageMath, 经典攻击脚本)；Misc (zsteg, binwalk, pcap分析等)
- 自我修正：Flag提交失败后，将错误信息注入LLM对话历史，自动重试最多3次。超过次数标记为 NEEDS_HUMAN。

### 2.3 人机协同仪表盘 (dashboard/)

- 使用 Next.js 实现 Web 驾驶舱（Aemeath 模块五，替代旧 Streamlit），通过 SSE 实时展示黑板与总控状态。
- 主页面：实时显示所有题目卡片（状态、类型、最近日志摘要），支持手动刷新。
- 题目详情页：展示完整解题对话历史/日志流。
- 人工干预：可对任一题目发送自然语言提示、上传脚本、调整优先级、暂停/继续/终止任务。
- 全局设置：修改并发数、模型温度、重试次数等，动态生效。

### 2.4 配置与启动

- 所有可变参数（上述API信息、LLM配置等）集中放在 `.env` 文件和 `config.yaml` 中，代码读取配置，不硬编码。
- 提供 `main.py` 启动整个系统（同时运行引擎和仪表盘）。
- 提供 `setup.sh` 或 `Makefile`，一键安装依赖、构建沙箱镜像、初始化数据库。

### 2.5 项目文件结构

ctf_autosolver/
├── core/
│ ├── api_client.py
│ ├── task_manager.py
│ └── sandbox.py
├── solvers/
│ ├── router.py
│ ├── base_solver.py
│ ├── rev_solver.py
│ ├── pwn_solver.py
│ ├── web_solver.py
│ ├── crypto_solver.py
│ └── misc_solver.py
├── tools/
│ ├── code_executor.py
│ ├── binary_tools.py
│ ├── web_tools.py
│ └── crypto_tools.py
├── dashboard/
│ └── app.py
├── sandbox/
│ └── Dockerfile
├── config.yaml
├── .env.example
├── main.py
├── setup.sh
├── requirements.txt
└── README.md

## 3. 交付要求

- 代码完整，逻辑严谨，关键函数有英文注释。
- 所有模块间通过清晰接口交互，解耦合。
- 异常处理：单个任务崩溃不影响整体系统，全部异常捕获并记录日志。
- 安全：Flag提交前用正则 `flag\{.*\}` 校验；沙箱内禁止 `rm -rf` 等操作，网络隔离。
- 提供 `test_system.py`，使用mock API验证全流程（并发、分类、求解、提交、仪表盘展示）。
- README.md 必须包含：如何配置参数、如何启动、如何访问仪表盘、如何自定义工具。

## 4. 执行步骤

请你遵循以下开发顺序，并在每个步骤完成后输出简短的进度说明：

1. 生成项目骨架、配置文件、Dockerfile和依赖清单。
2. 实现核心模块（api_client, task_manager, sandbox）。
3. 实现求解器基类和题型路由器，并与LLM集成。
4. 实现各题型求解器，至少集成2个专用工具。
5. 实现 Aemeath Web 驾驶舱（Next.js + SSE）。
6. 编写测试脚本并确保通过。
7. 输出最终的项目代码和说明文档。
