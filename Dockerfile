# ---------- Build stage ----------
FROM python:3.12-slim AS builder

# Optional dependency groups from pyproject.toml; cloud-providers holds the
# Anthropic and Google SDKs the default preset needs (e.g. "cloud-providers,e2b").
ARG EXTRAS=cloud-providers

WORKDIR /build

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends git && \
    rm -rf /var/lib/apt/lists/*

# Copy project files (prompts/ and templates/ are force-included in the wheel)
COPY pyproject.toml README.md ./
COPY src/ src/
COPY prompts/ prompts/
COPY templates/ templates/

# Install the package and dependencies into a virtual env
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --no-cache-dir ".[${EXTRAS}]"

# ---------- Runtime stage ----------
FROM python:3.12-slim AS runtime

WORKDIR /app

# git: every run works in a git workspace, and git-URL workspaces are cloned
RUN apt-get update && apt-get install -y --no-install-recommends git && \
    rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN groupadd --gid 1000 delegatron && \
    useradd --uid 1000 --gid delegatron --create-home delegatron

# Copy installed virtual env from builder
COPY --from=builder /opt/venv /opt/venv

# The server reads these at startup (uvicorn never runs `delegatron serve`):
# runs work under the mounted workspace, artifacts and the database live on volumes.
ENV PATH="/opt/venv/bin:$PATH" \
    DELEGATRON_WORKSPACE=/app/workspace \
    DELEGATRON_ARTIFACTS_DIR=/app/runs \
    DELEGATRON_DATA_DIR=/app/data \
    DELEGATRON_LOG_DIR=/app/logs

# Copy prompts (a fallback: the installed package already includes prompts/)
COPY prompts/ /app/prompts/

# Create directories for workspace, run artifacts, server data and logs
# (the default log dir would resolve inside the root-owned /opt/venv)
RUN mkdir -p /app/workspace /app/runs /app/data /app/logs && \
    chown -R delegatron:delegatron /app

USER delegatron

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health')" || exit 1

CMD ["uvicorn", "delegatron.server.app:app", "--host", "0.0.0.0", "--port", "8000"]
