#!/usr/bin/env bash
set -euo pipefail

echo "============================================"
echo "  Delegatron Setup"
echo "  StoriaBrevi LLC"
echo "============================================"
echo

# ---------- Python ----------
if ! command -v python3 &>/dev/null; then
    echo "[ERROR] Python 3 not found."
    echo "  macOS:  brew install python@3.11"
    echo "  Ubuntu: sudo apt install python3.11 python3.11-venv"
    exit 1
fi

PYVER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PYMAJOR=${PYVER%%.*}
PYMINOR=${PYVER##*.}

if [ "$PYMAJOR" -lt 3 ] || { [ "$PYMAJOR" -eq 3 ] && [ "$PYMINOR" -lt 11 ]; }; then
    echo "[ERROR] Python 3.11+ required. Found Python $PYVER"
    exit 1
fi
echo "[OK] Python $PYVER"

# ---------- Git ----------
if ! command -v git &>/dev/null; then
    echo "[ERROR] Git not found."
    echo "  macOS:  xcode-select --install"
    echo "  Ubuntu: sudo apt install git"
    exit 1
fi
echo "[OK] Git found"

# Check Docker
if ! command -v docker &>/dev/null; then
    echo "[WARN] Docker not found."
    echo "       Delegatron needs Docker to run code in a sandbox."
    echo "       Install: https://docs.docker.com/engine/install/"
    echo "       Or use E2B sandbox instead: export DELEGATRON_SANDBOX=e2b"
    echo
elif ! docker info &>/dev/null; then
    echo "[WARN] Docker is installed but not running."
    echo "       Start Docker Desktop or the Docker daemon, then re-run setup."
    echo "       Or use E2B sandbox instead: export DELEGATRON_SANDBOX=e2b"
    echo
else
    echo "[OK] Docker found and running"
fi

# ---------- Ollama ----------
OLLAMA_OK=false
OLLAMA_MODELS=()
if ! command -v ollama &>/dev/null; then
    echo "[INFO] Ollama not found — install for free local LLM support."
    echo "       Install: https://ollama.com/download"
elif ! curl -sf http://localhost:11434/api/tags &>/dev/null; then
    echo "[WARN] Ollama installed but not running. Start with: ollama serve"
else
    echo "[OK] Ollama running"
    OLLAMA_OK=true
    # Check which recommended models are available
    AVAILABLE=$(curl -sf http://localhost:11434/api/tags | python3 -c "
import sys, json
data = json.load(sys.stdin)
for m in data.get('models', []):
    print(m.get('name', ''))
" 2>/dev/null || true)
    for MODEL in "phi4:14b" "qwen2.5-coder:14b" "gpt-oss:20b"; do
        SHORT=${MODEL%%:*}
        if echo "$AVAILABLE" | grep -q "$SHORT"; then
            echo "  [OK] $MODEL available"
            OLLAMA_MODELS+=("$MODEL")
        else
            echo "  [--] $MODEL not pulled (optional: ollama pull $MODEL)"
        fi
    done
fi
echo

# ---------- Virtual environment ----------
if [ -d .venv ]; then
    echo "[OK] Virtual environment already exists"
else
    echo "Creating virtual environment..."
    python3 -m venv .venv
    echo "[OK] Virtual environment created"
fi

# shellcheck disable=SC1091
source .venv/bin/activate

echo "Upgrading pip..."
python -m pip install --upgrade pip --quiet

echo "Installing Delegatron..."
pip install -e ".[dev,cloud-providers]" --quiet
echo "[OK] Delegatron installed"

# ---------- .env ----------
if [ ! -f .env ]; then
    cat > .env << 'ENVEOF'
# Delegatron Environment Configuration
#
# LOCAL MODELS (Ollama) — no API keys needed!
# Just run: delegatron run --preset local "your task"
#
# CLOUD MODELS — uncomment the keys you want to use:
# ANTHROPIC_API_KEY=sk-ant-...
# OPENAI_API_KEY=sk-...
# GEMINI_API_KEY=...

# Pipeline settings
# DELEGATRON_MAX_ITERATIONS=5
# DELEGATRON_MAX_COST_USD=5.00
# DELEGATRON_SANDBOX=docker
# DELEGATRON_LOG_LEVEL=INFO
ENVEOF
    echo "[OK] Created .env"
else
    echo "[OK] .env already exists"
fi

# ---------- Verify ----------
echo
echo "Verifying installation..."
if delegatron --version &>/dev/null; then
    echo "[OK] $(delegatron --version 2>&1)"
elif python -m delegatron --version &>/dev/null; then
    echo "[OK] CLI available via: python -m delegatron"
else
    echo "[WARN] CLI not on PATH. Use: python -m delegatron"
fi

# ---------- Summary ----------
echo
echo "============================================"
echo "  Setup complete!"
echo "============================================"
echo

if [ "$OLLAMA_OK" = true ] && [ ${#OLLAMA_MODELS[@]} -gt 0 ]; then
    echo "  Ready to go with LOCAL models (free, no API keys)!"
    echo
    echo "  Quick start:"
    echo "    delegatron run --preset local \"Add a hello world endpoint\""
    echo
    echo "  Other local presets (see: delegatron list-presets):"
    echo "    --preset local-oss          gpt-oss:20b implementer"
    echo "    --preset local-oss-review   gpt-oss:20b reviewer (stronger review)"
    echo "    --preset local-phi4         phi4:14b for every role (fastest)"
    echo "    --preset local-heavy        qwen3-coder:30b implementer (24 GB+ VRAM)"
elif [ "$OLLAMA_OK" = true ]; then
    echo "  Ollama is running but no recommended models found."
    echo
    echo "  Pull the models the 'local' preset uses:"
    echo "    ollama pull phi4:14b"
    echo "    ollama pull qwen2.5-coder:14b"
    echo
    echo "  Then run:"
    echo "    delegatron run --preset local \"Add a hello world endpoint\""
else
    echo "  Next steps:"
    echo
    echo "  Option A — Local models (recommended, free):"
    echo "    1. Install Ollama: https://ollama.com/download"
    echo "    2. ollama pull phi4:14b && ollama pull qwen2.5-coder:14b"
    echo "    3. delegatron run --preset local \"your task\""
    echo
    echo "  Option B — Cloud models:"
    echo "    1. Edit .env and add an API key"
    echo "    2. delegatron run \"your task\""
fi
echo
echo "  Dashboard:  delegatron serve"
echo "  Docs:       docs/install.md"
echo

# Keep the venv active in the caller's shell
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    echo "--------------------------------------------"
    echo "  TIP: Run with 'source setup.sh' to keep"
    echo "  the venv active in your current shell."
    echo "  Starting a subshell with venv activated..."
    echo "--------------------------------------------"
    # Use bash explicitly for --init-file support (zsh/fish don't support it)
    exec bash --init-file <(cat <<'INITEOF'
source .venv/bin/activate
echo "Delegatron venv active. Type 'exit' to leave."
INITEOF
    )
fi
