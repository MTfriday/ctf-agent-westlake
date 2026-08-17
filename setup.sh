#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# CTF Agent — 一键安装脚本
# 安装依赖、构建沙箱镜像、创建必要目录
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; }

# ── 1. Check prerequisites ──────────────────────────────────────────────────
info "Checking prerequisites..."

# Python 3.14+
PYTHON=$(command -v python3 || command -v python || echo "")
if [ -z "$PYTHON" ]; then
    error "Python 3 is required. Install Python 3.14+ first."
    exit 1
fi

PY_VERSION=$("$PYTHON" --version 2>&1 | awk '{print $2}')
info "Found Python: $PY_VERSION"

# Check for uv (preferred) or pip
UV=$(command -v uv || echo "")
if [ -n "$UV" ]; then
    PKG_MANAGER="uv"
    INSTALL_CMD="uv sync"
    info "Using uv (recommended)"
else
    PIP=$(command -v pip3 || command -v pip || echo "")
    if [ -z "$PIP" ]; then
        error "pip is required. Install pip first."
        exit 1
    fi
    PKG_MANAGER="pip"
    INSTALL_CMD="pip install -r requirements.txt"
    info "Using pip"
fi

# Docker
DOCKER=$(command -v docker || echo "")
if [ -z "$DOCKER" ]; then
    error "Docker is required. Install Docker first."
    exit 1
fi
info "Found Docker: $("$DOCKER" --version)"

# ── 1.5 Create/use virtual environment ──────────────────────────────────────
if [ -x ".venv/bin/python" ]; then
    info "Using existing virtual environment: .venv"
else
    info "Creating virtual environment (.venv)..."
    "$PYTHON" -m venv .venv
fi
VENV_PY="$SCRIPT_DIR/.venv/bin/python"
VENV_PIP="$SCRIPT_DIR/.venv/bin/pip"

# ── 2. Install Python dependencies (into .venv) ─────────────────────────────
info "Installing Python dependencies (into .venv)..."
if [ "$PKG_MANAGER" = "uv" ]; then
    # uv sync 自动使用/创建 .venv（pyproject.toml [tool.uv]）
    uv sync
else
    "$VENV_PIP" install --upgrade pip
    "$VENV_PIP" install -e .
fi
info "Dependencies installed."

# ── 3. Build Docker sandbox image ────────────────────────────────────────────
SANDBOX_IMAGE="${SANDBOX_IMAGE:-ctf-sandbox}"
info "Building Docker sandbox image: $SANDBOX_IMAGE..."
if "$DOCKER" image inspect "$SANDBOX_IMAGE" &>/dev/null; then
    warn "Image '$SANDBOX_IMAGE' already exists. Skipping build."
    warn "To rebuild: docker build -f sandbox/Dockerfile.sandbox -t $SANDBOX_IMAGE ."
else
    "$DOCKER" build -f sandbox/Dockerfile.sandbox -t "$SANDBOX_IMAGE" .
    info "Sandbox image built."
fi

# ── 3.5 Install frontend dependencies ───────────────────────────────────────
if [ -f "frontend/package.json" ]; then
    if command -v npm &>/dev/null; then
        if [ ! -d "frontend/node_modules" ]; then
            info "Installing frontend dependencies (frontend/)..."
            npm --prefix frontend install
        else
            info "frontend/node_modules already exists. Skip (reinstall: rm -rf frontend/node_modules)."
        fi
    else
        warn "npm not found — skip frontend install (run later: npm --prefix frontend install)."
    fi
else
    warn "frontend/package.json not found — skip frontend install."
fi

# ── 4. Create necessary directories ─────────────────────────────────────────
info "Creating directories..."
mkdir -p challenges logs

# ── 5. Setup .env if not exists ─────────────────────────────────────────────
if [ ! -f ".env" ]; then
    warn "No .env file found. Copying from .env.example..."
    cp .env.example .env
    info "Edit .env with your API keys and credentials."
else
    info ".env already exists."
fi

# ── 6. Dashboard ────────────────────────────────────────────────────────────
# dashboard/server.py 基于 stdlib http.server，无额外依赖；驾驶舱是 frontend/（Next.js）
info "Dashboard uses stdlib http.server — no extra Python deps needed."

# ── Done ────────────────────────────────────────────────────────────────────
echo ""
info "${GREEN}✅ Setup complete!${NC}"
echo ""
echo "Quick start:"
echo "  1. Edit .env with your API keys (SLAB_ACCESS_KEY / DEEPSEEK_API_KEY / BAILIAN_API_KEY)"
echo "  2. Run:  ./run.sh"
echo "     (开发/联调 mock:  ./run.sh --mock)"
echo "  3. Open http://127.0.0.1:3999 for the cockpit"
echo ""
echo "Run tests:"
echo "  .venv/bin/python -m pytest tests/ -v"
