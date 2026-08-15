# ============================================================================
# Aemeath CTF Agent — 一键启动脚本（PowerShell / Windows 版）
#
# 启动后端 adapter（:12345）+ 前端驾驶舱（:3999）
#
# 用法（在仓库根目录）：
#   powershell -ExecutionPolicy Bypass -File .\run.ps1
#   .\run.ps1 -Mock              # 开发/联调模式（mock 引擎，无需平台/LLM/Docker）
#   .\run.ps1 -FrontendOnly      # 仅启动前端（复用已运行的后端）
#   .\run.ps1 -BackendOnly       # 仅启动后端
# ============================================================================
[CmdletBinding()]
param(
    [switch]$Mock,               # 用 mock 引擎（scripts/run_muteki_dev.py），不连真实平台
    [switch]$BackendOnly,        # 仅启动后端 adapter
    [switch]$FrontendOnly        # 仅启动前端驾驶舱
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

function Info  { Write-Host "[INFO]  $args" -ForegroundColor Green }
function Warn  { Write-Host "[WARN]  $args" -ForegroundColor Yellow }
function Error { Write-Host "[ERROR] $args" -ForegroundColor Red }

$VenvPy = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPy)) {
    Error "未找到虚拟环境 python（$VenvPy）。请先运行 setup.ps1 或创建 venv。"
    exit 1
}

# ── 端口检查 ────────────────────────────────────────────────────────────────
function Test-Port([int]$Port) {
    $conn = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
    return [bool]$conn
}

# ── 启动后端 adapter ────────────────────────────────────────────────────────
if (-not $FrontendOnly) {
    if (Test-Port 12345) {
        Warn "端口 12345 已被占用 — adapter 可能已在运行，跳过启动。"
    } else {
        Info "启动后端 adapter（:12345）..."
        if ($Mock) {
            Info "模式: mock（开发/联调，无需平台/LLM/Docker）"
            $BackendCmd = "& '$VenvPy' scripts\run_muteki_dev.py"
        } else {
            Info "模式: 真实（GZCTF + 全自动求解，需 Docker + .env 凭据）"
            $BackendCmd = "& '$VenvPy' -m adapter"
        }
        Start-Process powershell -ArgumentList @(
            "-NoExit", "-ExecutionPolicy", "Bypass", "-Command", $BackendCmd
        )
        # 等 adapter 就绪（最多 30s）
        $ready = $false
        for ($i = 0; $i -lt 30; $i++) {
            Start-Sleep -Seconds 1
            try {
                $h = Invoke-RestMethod -Uri "http://127.0.0.1:12345/api/health" -TimeoutSec 2
                if ($h.status -eq "ok") { $ready = $true; break }
            } catch { }
        }
        if ($ready) { Info "✅ adapter 就绪: http://127.0.0.1:12345 (engine=$($h.engine))" }
        else { Warn "adapter 启动超时 — 请检查新开窗口中的日志。" }
    }
}

# ── 启动前端驾驶舱 ──────────────────────────────────────────────────────────
if (-not $BackendOnly) {
    if (Test-Port 3999) {
        Warn "端口 3999 已被占用 — 前端可能已在运行，跳过启动。"
    } else {
        if (-not (Test-Path "frontend\node_modules")) {
            Error "frontend/node_modules 不存在。请先运行 setup.ps1 或: npm --prefix frontend install"
            exit 1
        }
        Info "启动前端驾驶舱（:3999）..."
        $env:NEXT_PUBLIC_MUTEKI_API = "http://127.0.0.1:12345"
        $FrontendCmd = "npm --prefix '$Root\frontend' run dev"
        Start-Process powershell -ArgumentList @(
            "-NoExit", "-ExecutionPolicy", "Bypass", "-Command",
            "`$env:NEXT_PUBLIC_MUTEKI_API='http://127.0.0.1:12345'; $FrontendCmd"
        )
        # 等前端就绪（最多 60s）
        $ready = $false
        for ($i = 0; $i -lt 60; $i++) {
            Start-Sleep -Seconds 1
            try {
                $r = Invoke-WebRequest -Uri "http://127.0.0.1:3999/" -UseBasicParsing -TimeoutSec 2
                if ($r.StatusCode -eq 200) { $ready = $true; break }
            } catch { }
        }
        if ($ready) { Info "✅ 前端就绪: http://127.0.0.1:3999" }
        else { Warn "前端启动超时 — 请检查新开窗口中的日志。" }
    }
}

# ── 完成 ────────────────────────────────────────────────────────────────────
Write-Host ""
Info "🚀 启动完成！打开 http://127.0.0.1:3999 使用驾驶舱"
Write-Host ""
Write-Host "常用命令:" -ForegroundColor Cyan
Write-Host "  重启后端:  .\.venv\Scripts\python.exe -m adapter" -ForegroundColor Cyan
Write-Host "  mock 联调: .\.venv\Scripts\python.exe scripts\run_muteki_dev.py" -ForegroundColor Cyan
Write-Host "  停止:      关闭对应 PowerShell 窗口即可（或 Ctrl+C）" -ForegroundColor Cyan
