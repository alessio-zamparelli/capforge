# syntax=docker/dockerfile:1

# Keep this tag in sync with the uv version pinned in
# .github/workflows/deploy.yml and the one used to generate uv.lock.
FROM ghcr.io/astral-sh/uv:0.13.6 AS uv

# The web console is built with bun and shipped as static files.
FROM oven/bun:1 AS ui
WORKDIR /src
COPY ui/package.json ui/bun.lock ./
RUN bun install --frozen-lockfile
COPY ui/ ./
RUN bun run build

FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/home/capforge \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# git serves the capability receive/upload ingress and `list_tags` diffs.
# util-linux provides `setpriv` and `prlimit`; the entrypoint drops privileges
# after applying PUID/PGID.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git util-linux \
 && rm -rf /var/lib/apt/lists/*

COPY --from=uv /uv /uvx /bin/

WORKDIR /app

# Dependency layer: only invalidated by pyproject.toml or uv.lock changes.
# `--no-dev` drops the dev group (pytest, ruff, mypy); httpx is a runtime
# dependency. --locked makes a stale lockfile a build failure.
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

COPY capforge ./capforge
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# Default identity. PUID/PGID at runtime remap this user and its group; the
# entrypoint starts as root only long enough to chown the mounts and drop.
RUN useradd --uid 10001 --user-group --create-home --shell /usr/sbin/nologin capforge \
 && mkdir -p /var/lib/capforge/caps /var/lib/capforge/state \
 && chown -R capforge:capforge /home/capforge /var/lib/capforge

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod 0755 /usr/local/bin/docker-entrypoint.sh

# The console bundle (token-gated). CAPFORGE_UI_DIR=0 disables serving it.
COPY --from=ui /src/dist /app/ui/dist

# The venv is uv-managed; running the console script directly avoids a second
# uv process (and signal-forwarding layer) as PID 1.
ENV PATH="/app/.venv/bin:$PATH" \
    PUID=10001 \
    PGID=10001 \
    CAPFORGE_CAPS_DIR=/var/lib/capforge/caps \
    CAPFORGE_STATE_DIR=/var/lib/capforge/state \
    CAPFORGE_SANDBOX=require \
    CAPFORGE_HOST=0.0.0.0 \
    CAPFORGE_PORT=8099 \
    CAPFORGE_PATH=/mcp \
    CAPFORGE_TRANSPORT=http \
    CAPFORGE_WEB_HOST=0.0.0.0 \
    CAPFORGE_WEB_PORT=8080 \
    CAPFORGE_UI_DIR=/app/ui/dist

EXPOSE 8099 8080

# MCP requires a POST with protocol headers; use the web listener's
# unauthenticated readiness probe instead.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/readyz', timeout=3)"

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["capforge", "serve"]
