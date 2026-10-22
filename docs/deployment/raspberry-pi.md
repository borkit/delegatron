# Deploying Delegatron on Raspberry Pi

Delegatron runs well on Raspberry Pi as a lightweight, always-on coding automation server. The orchestrator itself is just Python making API calls — the heavy compute happens on the LLM provider side.

## Minimum Requirements

| | Minimum | Recommended |
|---|---|---|
| **Board** | Raspberry Pi 4 (4GB) | Raspberry Pi 5 (8GB) |
| **OS** | Raspberry Pi OS 64-bit (Bookworm) | Raspberry Pi OS 64-bit (Bookworm) |
| **Python** | 3.11+ | 3.11+ |
| **Storage** | 16GB SD card | 32GB+ SSD (USB boot) |
| **Network** | Required (API calls) | Ethernet recommended for stability |

Pi 3 and earlier are **not recommended** — insufficient RAM for Docker sandbox.

## Setup

```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Install Python 3.11+ (Bookworm ships with 3.11)
sudo apt install -y python3 python3-pip python3-venv git

# Install Docker (for sandbox)
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
# Log out and back in for group change to take effect

# Clone and install Delegatron
git clone https://github.com/borkit/delegatron.git
cd delegatron
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[cloud-providers]"   # Anthropic and Google SDKs for the config below

# Configure
cp .env.example .env
nano .env  # Add your API keys
```

## Recommended Configuration

Use cloud LLM APIs — the Pi orchestrates, providers do the heavy lifting:

```toml
# delegatron.toml

[roles.architect]
provider = "anthropic"
model = "claude-sonnet-5-5"

[roles.implementer]
provider = "openai"
model = "gpt-4.1"

[roles.reviewer]
provider = "google"
model = "gemini-2.5-flash"    # Flash is cheaper, fast enough for review

[pipeline]
sandbox = "docker"
max_wall_time_minutes = 90     # Default is 60; allow more — Docker is slower on ARM
```

## Running as a Service

### API Server Mode (Recommended)

Run the Delegatron API server for dashboard access and remote triggering:

```bash
sudo tee /etc/systemd/system/delegatron.service > /dev/null <<EOF
[Unit]
Description=Delegatron API Server
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=/home/$USER/delegatron
Environment=PATH=/home/$USER/delegatron/.venv/bin:/usr/bin:/bin
EnvironmentFile=/home/$USER/delegatron/.env
ExecStart=/home/$USER/delegatron/.venv/bin/delegatron serve --host 0.0.0.0 --port 8000
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable delegatron
sudo systemctl start delegatron
```

Without `--workspace`, `delegatron serve` uses its working directory as the
workspace; since that is the Delegatron checkout here, the server falls back to
`~/delegatron-workspace`. Add `--workspace /path/to/repos` to `ExecStart` to
choose another.

Access the API at `http://<pi-ip>:8000`. Connect the dashboard from any device on your network:
the API accepts browser requests only from the origins in `DELEGATRON_CORS_ORIGINS`
(default: `localhost` ports 3000, 8001, 8002 and 8081), so add the dashboard's origin
(e.g. `DELEGATRON_CORS_ORIGINS=http://<pi-ip>:8001` in `.env`). The server is
unauthenticated unless `DELEGATRON_AUTH_ENABLED=true` (which also requires
`DELEGATRON_STREAM_SECRET`).

### CLI Mode

For direct CLI usage without the server:

```bash
# Run tasks
delegatron run "Add a /health endpoint" --config delegatron.toml

# Read-only code review (no sandbox needed — lightweight on Pi)
delegatron review --workspace /path/to/repo
delegatron review --focus security

# Analyze log files
delegatron review --logs /var/log/app.log --focus bugs
```

## Performance Notes

### What's fast
- API calls to Claude/GPT/Gemini — network I/O, Pi handles this with zero issues
- Git operations — native, fast on Pi 4/5
- Orchestrator logic — minimal CPU, mostly waiting on API responses

### What's slower
- **Docker sandbox** — ARM containers are ~2-3x slower than x86 for builds and test execution. Budget extra wall time.
- **Large repos** — Git operations on repos with 10K+ files may be noticeably slower on SD card. Use SSD boot for better I/O.
- **pip install** — First-time dependency installation is slow on Pi. Use a venv and it only happens once.

### Memory usage
- Delegatron orchestrator: ~50-100MB RSS
- Docker overhead: ~200-300MB
- Test container: varies (Python test suite typically 100-300MB); the sandbox caps each container at 512MB and 1 CPU
- **Total: ~500MB-1GB** — fits comfortably on a 4GB Pi 4

## Local Models on Pi (Experimental)

Running local LLMs on Pi is possible but limited:

| Model size | Pi 4 (4GB) | Pi 4 (8GB) | Pi 5 (8GB) |
|---|---|---|---|
| 1-3B params | Slow but works | Works | Usable |
| 7B params (Q4) | Won't fit | Very slow | Slow but works |
| 13B+ params | No | No | No |

If you want local models, use Ollama:

```bash
# Install Ollama on Pi
curl -fsSL https://ollama.com/install.sh | sh

# Pull a small model
ollama pull phi3:mini    # 3.8B params, fits on 4GB Pi

# Configure Delegatron to use it
# delegatron.toml
# [roles.reviewer]
# provider = "openai-compatible"
# model = "phi3:mini"
# base_url = "http://localhost:11434/v1"
```

**Recommendation:** Use cloud APIs for architect and implementer (quality matters there), and optionally a local model for reviewer if you want to save on API costs. But cloud APIs for all three roles is the most reliable setup on Pi.

## Troubleshooting

### Docker fails to start containers
```bash
# Ensure Docker is running
sudo systemctl status docker

# Check ARM image availability — some images are x86-only
docker run --rm arm64v8/python:3.12-slim python --version
```

### Out of memory
```bash
# Add swap if needed (not ideal on SD card, fine on SSD)
sudo dphys-swapfile swapoff
sudo sed -i 's/CONF_SWAPSIZE=.*/CONF_SWAPSIZE=2048/' /etc/dphys-swapfile
sudo dphys-swapfile setup
sudo dphys-swapfile swapon
```

### Slow git operations
Use SSD boot instead of SD card. USB 3.0 SSD on Pi 4/5 gives 10x better I/O.
