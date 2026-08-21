#!/usr/bin/env bash
# ============================================================================
# Aemeath CTF Agent — 一键启动脚本（Linux / bash 版）
#
# 启动后端 adapter（:12346）+ 前端驾驶舱（:3999）
#
# 用法（在仓库根目录）：
#   ./run.sh                    # 后端 + 前端（真实求解模式）
#   ./run.sh --mock             # 开发/联调（mock 引擎，无需平台/LLM/Docker）
#   ./run.sh --frontend-only    # 仅启动前端（复用已运行的后端）
#   ./run.sh --backend-only     # 仅启动后端 adapter
#
# 依赖：先执行 ./setup.sh（创建 .venv + 安装 Python/前端依赖 + 构建沙箱镜像）
# ============================================================================
set -euo pipefail

MOCK=false
BACKEND_ONLY=false
FRONTEND_ONLY=false
for arg in "$@"; do
    case "$arg" in
        --mock)          MOCK=true ;;
        --backend-only)  BACKEND_ONLY=true ;;
        --frontend-only) FRONTEND_ONLY=true ;;
        *) echo "未知参数: $arg" >&2; exit 1 ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; }

VenvPy="$SCRIPT_DIR/.venv/bin/python"
if [ ! -x "$VenvPy" ]; then
    error "未找到虚拟环境 python（$VenvPy）。请先运行 ./setup.sh"
    error "或手动创建：python3 -m venv .venv && .venv/bin/pip install -e ."
    exit 1
fi
info "使用虚拟环境: $VenvPy"

mkdir -p logs

# ── 后台进程管理（Ctrl+C 或退出时自动清理）──────────────────────────────────
BACKEND_PID=""
FRONTEND_PID=""
cleanup() {
    echo ""
    warn "正在停止服务..."
    [ -n "$FRONTEND_PID" ] && kill "$FRONTEND_PID" 2>/dev/null || true
    [ -n "$BACKEND_PID" ] && kill "$BACKEND_PID" 2>/dev/null || true
    wait 2>/dev/null || true
    info "已停止。"
}
trap cleanup EXIT INT TERM

# 端口占用检查（bash 内置 /dev/tcp）
port_busy() {
    (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null && { exec 3>&-; exec 3<&-; return 0; } || return 1
}

# ── 启动后端 adapter ────────────────────────────────────────────────────────
if [ "$FRONTEND_ONLY" = "false" ]; then
    if port_busy 12346; then
        warn "端口 12346 已被占用 — adapter 可能已在运行，跳过启动。"
    else
        if [ "$MOCK" = "true" ]; then
            info "模式: mock（开发/联调，无需平台/LLM/Docker）"
            BACKEND_CMD="$VenvPy scripts/run_muteki_dev.py"
        else
            info "模式: 真实（slab 平台 + 全自动求解，需 Docker + .env 凭据）"
            BACKEND_CMD="$VenvPy -m adapter"
        fi
        info "启动后端 adapter（:12346）... 日志: logs/adapter.log"
        # shellcheck disable=SC2086
        nohup $BACKEND_CMD > logs/adapter.log 2>&1 &
        BACKEND_PID=$!
        ready=false
        for _ in $(seq 1 30); do
            sleep 1
            if curl -sf --max-time 2 http://127.0.0.1:12346/api/health >/dev/null 2>&1; then
                ready=true; break
            fi
            if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
                warn "adapter 进程已退出 — 请查看 logs/adapter.log"
                break
            fi
        done
        if [ "$ready" = "true" ]; then
            info "✅ adapter 就绪: http://127.0.0.1:12346"
        else
            warn "adapter 启动超时 — 请查看 logs/adapter.log"
        fi
    fi
fi

# ── 启动前端驾驶舱 ──────────────────────────────────────────────────────────
if [ "$BACKEND_ONLY" = "false" ]; then
    if port_busy 3999; then
        warn "端口 3999 已被占用 — 前端可能已在运行，跳过启动。"
    else
        if [ ! -d "frontend/node_modules" ]; then
            error "frontend/node_modules 不存在。请先运行 ./setup.sh 或: npm --prefix frontend install"
            exit 1
        fi
        info "启动前端驾驶舱（:3999）... 日志: logs/frontend.log"
        # NEXT_PUBLIC_MUTEKI_API 留空 → 前端走相对路径 /api，经 Next rewrite 代理到 adapter
        nohup npm --prefix "$SCRIPT_DIR/frontend" run dev -- -H 0.0.0.0 > logs/frontend.log 2>&1 &
        FRONTEND_PID=$!
        ready=false
        for _ in $(seq 1 60); do
            sleep 1
            if curl -sf --max-time 2 http://127.0.0.1:3999/ >/dev/null 2>&1; then
                ready=true; break
            fi
            if ! kill -0 "$FRONTEND_PID" 2>/dev/null; then
                warn "前端进程已退出 — 请查看 logs/frontend.log"
                break
            fi
        done
        if [ "$ready" = "true" ]; then
            info "✅ 前端就绪: http://127.0.0.1:3999"
        else
            warn "前端启动超时 — 请查看 logs/frontend.log"
        fi
    fi
fi

# ── 完成 ────────────────────────────────────────────────────────────────────
echo ""
info "🚀 启动完成！打开 http://127.0.0.1:3999 使用驾驶舱"
echo ""
echo "常用命令:"
echo "  重启后端:  $VenvPy -m adapter"
echo "  mock 联调:  $VenvPy scripts/run_muteki_dev.py"
echo "  查看日志:  tail -f logs/adapter.log   /   logs/frontend.log"
echo "  停止:      Ctrl+C（自动清理后台进程）"

# 保持前台运行直到 Ctrl+C（由 trap 清理后台进程）
while true; do sleep 3600; done
