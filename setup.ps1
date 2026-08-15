# ============================================================================
# Aemeath CTF Agent — 一键安装脚本（PowerShell / Windows 版）
# 对应 setup.sh：安装 Python 依赖 + 前端依赖 + 构建沙箱镜像 + 准备 .env
#
# 用法（在仓库根目录）：
#   powershell -ExecutionPolicy Bypass -File .\setup.ps1
# 或直接： .\setup.ps1
# ============================================================================
[CmdletBinding()]
param(
    [switch]$SkipSandbox,        # 跳过 Docker 沙箱镜像构建
    [switch]$SkipFrontend,       # 跳过前端 npm 依赖安装
    [switch]$SkipEnv             # 跳过 .env 生成
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

function Info  { Write-Host "[INFO]  $args" -ForegroundColor Green }
function Warn  { Write-Host "[WARN]  $args" -ForegroundColor Yellow }
function Error { Write-Host "[ERROR] $args" -ForegroundColor Red }

# ── 1. 前置检查 ────────────────────────────────────────────────────────────
Info "检查前置环境..."

# Python：优先用仓库 venv（.venv\Scripts\python.exe），否则系统 python
$VenvPy = Join-Path $Root ".venv\Scripts\python.exe"
$VenvPip = Join-Path $Root ".venv\Scripts\pip.exe"
if (Test-Path $VenvPy) {
    Info "使用仓库虚拟环境: $VenvPy"
    $PyPath = $VenvPy
    $PipPath = $VenvPip
} else {
    $Py = Get-Command python -ErrorAction SilentlyContinue
    if (-not $Py) { $Py = Get-Command python3 -ErrorAction SilentlyContinue }
    if (-not $Py) { Error "需要 Python 3.14+。请先安装 Python。"; exit 1 }
    $PyPath = $Py.Source
    $PipPath = (Get-Command pip -ErrorAction SilentlyContinue).Source
    if (-not $PipPath) { $PipPath = (Get-Command pip3 -ErrorAction SilentlyContinue).Source }
    if (-not $PipPath) { Error "需要 pip 或 uv。请先安装。"; exit 1 }
}
$PyVer = (& $PyPath --version 2>&1).Trim()
Info "Python: $PyVer"
if ($PyVer -notmatch "3\.1[4-9]") {
    Warn "建议使用 Python 3.14+（当前 $PyVer）。更高版本可能不受依赖支持。"
}

# uv（优先）或 pip
$Uv = Get-Command uv -ErrorAction SilentlyContinue
if ($Uv) {
    Info "使用 uv（推荐）"
    $UseUv = $true
} else {
    Info "使用 pip"
    $UseUv = $false
}

# Docker
$Docker = Get-Command docker -ErrorAction SilentlyContinue
if (-not $Docker) {
    Error "需要 Docker。请先安装并启动 Docker Desktop。"
    Warn  "然后运行: docker info 确认 daemon 已就绪"
    exit 1
}
Info "找到 Docker: $(& $Docker.Source --version)"

# Node.js / npm（前端需要）
if (-not $SkipFrontend) {
    $Npm = Get-Command npm -ErrorAction SilentlyContinue
    if (-not $Npm) { Warn "未找到 npm — 跳过前端依赖安装（可稍后手动 npm install）"; $SkipFrontend = $true }
    else { Info "找到 npm: $(& $Npm.Source --version)" }
}

# ── 2. 安装 Python 依赖 ────────────────────────────────────────────────────
Info "安装 Python 依赖..."
if ($UseUv) {
    uv sync
} else {
    # 无 requirements.txt 时从 pyproject.toml 安装（editable）
    if (Test-Path "requirements.txt") {
        & $PipPath install -r requirements.txt
    } else {
        Warn "无 requirements.txt — 直接从 pyproject.toml 安装（editable）"
        & $PipPath install -e .
    }
    if ($LASTEXITCODE -ne 0) { Error "Python 依赖安装失败。"; exit 1 }
}
Info "Python 依赖安装完成。"

# ── 3. 构建 Docker 沙箱镜像 ────────────────────────────────────────────────
$SandboxImage = if ($env:SANDBOX_IMAGE) { $env:SANDBOX_IMAGE } else { "ctf-sandbox" }
if (-not $SkipSandbox) {
    Info "构建 Docker 沙箱镜像: $SandboxImage ..."
    # 注意：用 $LASTEXITCODE 判断（$ErrorActionPreference=Stop 会把 docker 非零退出当错误抛出）
    $ErrorActionPreference = "Continue"
    docker image inspect $SandboxImage 2>$null | Out-Null
    $exists = ($LASTEXITCODE -eq 0)
    $ErrorActionPreference = "Stop"
    if ($exists) {
        Warn "镜像 '$SandboxImage' 已存在，跳过构建。"
        Warn "如需重建: docker build -f sandbox/Dockerfile.sandbox -t $SandboxImage ."
    } else {
        docker build -f sandbox/Dockerfile.sandbox -t $SandboxImage .
        if ($LASTEXITCODE -ne 0) { Error "沙箱镜像构建失败。"; exit 1 }
        Info "沙箱镜像构建完成。"
    }
} else {
    Warn "已跳过沙箱镜像构建（-SkipSandbox）。真实求解需要它。"
}

# ── 4. 创建必要目录 ────────────────────────────────────────────────────────
Info "创建目录..."
New-Item -ItemType Directory -Force -Path "challenges", "logs", "data" | Out-Null

# ── 5. 准备 .env ───────────────────────────────────────────────────────────
if (-not $SkipEnv) {
    if (-not (Test-Path ".env")) {
        Warn "未找到 .env，从 .env.example 复制..."
        Copy-Item ".env.example" ".env"
        Info "已生成 .env — 请编辑填入 API Key 与平台凭证。"
    } else {
        Info ".env 已存在。"
    }
} else {
    Warn "已跳过 .env 生成（-SkipEnv）。"
}

# ── 6. 安装前端依赖 ────────────────────────────────────────────────────────
if (-not $SkipFrontend) {
    if (Test-Path "frontend/package.json") {
        Info "安装前端依赖（frontend/）..."
        if (-not (Test-Path "frontend/node_modules")) {
            npm --prefix frontend install
            if ($LASTEXITCODE -ne 0) { Error "前端依赖安装失败。"; exit 1 }
            Info "前端依赖安装完成。"
        } else {
            Info "frontend/node_modules 已存在，跳过。如需重装: Remove-Item frontend/node_modules -Recurse -Force"
        }
    } else {
        Warn "未找到 frontend/package.json — 跳过前端依赖。"
    }
} else {
    Warn "已跳过前端依赖（-SkipFrontend）。"
}

# ── 完成 ────────────────────────────────────────────────────────────────────
Write-Host ""
Info "✅ 安装完成！"
Write-Host ""
Write-Host "快速开始（Windows / PowerShell）:" -ForegroundColor Cyan
Write-Host "  1. 编辑 .env 填入 API Key 与平台凭证" -ForegroundColor Cyan
Write-Host "  2. 启动后端 adapter:  .\.venv\Scripts\python.exe -m adapter" -ForegroundColor Cyan
Write-Host "     （开发/联调 mock 模式: .\.venv\Scripts\python.exe scripts\run_muteki_dev.py）" -ForegroundColor Cyan
Write-Host "  3. 启动前端驾驶舱:   .\run.ps1（一键启动后端+前端）" -ForegroundColor Cyan
Write-Host "  4. 打开 http://127.0.0.1:3999" -ForegroundColor Cyan
Write-Host ""
Write-Host "运行测试:" -ForegroundColor Cyan
Write-Host "  .\.venv\Scripts\python.exe -m pytest tests -v" -ForegroundColor Cyan
