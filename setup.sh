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

# ── 2. Install Python dependencies ──────────────────────────────────────────
info "Installing Python dependencies..."
if [ "$PKG_MANAGER" = "uv" ]; then
    $INSTALL_CMD
else
    # Generate requirements.txt from pyproject.toml if needed
    if [ ! -f "requirements.txt" ]; then
        warn "No requirements.txt found — generating from pyproject.toml..."
        if command -v pip-compile &>/dev/null; then
            pip-compile pyproject.toml -o requirements.txt
        else
            warn "Skipping requirements.txt generation (pip-compile not available)."
            warn "Using pyproject.toml directly with pip:"
            pip install -e .
        fi
    fi
    if [ -f "requirements.txt" ]; then
        $INSTALL_CMD
    fi
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

# ── 6. Install dashboard dependencies ───────────────────────────────────────
info "Installing dashboard dependencies..."
if command -v streamlit &>/dev/null; then
    info "Streamlit already installed."
else
    if [ "$PKG_MANAGER" = "uv" ]; then
        uv pip install streamlit pyyaml httpx 2>/dev/null || true
    else
        pip install streamlit pyyaml httpx 2>/dev/null || true
    fi
    info "Dashboard dependencies installed."
fi

# ── Done ────────────────────────────────────────────────────────────────────
echo ""
info "${GREEN}✅ Setup complete!${NC}"
echo ""
echo "Quick start:"
echo "  1. Edit .env with your API keys"
echo "  2. Run:  python main.py --dashboard"
echo "  3. Open http://localhost:8501 for the dashboard"
echo ""
echo "Or run without dashboard:"
echo "  python main.py"
echo ""
echo "Run tests:"
echo "  pytest tests/ -v"
