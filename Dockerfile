# syntax=docker/dockerfile:1

# Keep this tag in sync with the uv version pinned in
# .github/workflows/deploy.yml and the one used to generate uv.lock.
FROM ghcr.io/astral-sh/uv:0.12.24 AS uv

FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/home/capforge \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# git powers the best-effort audit trail (`git log` per promotion) and
# `list_tags(diff_against_active=true)`. util-linux provides `setpriv`, which
# the entrypoint uses to drop privileges after applying PUID/PGID.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git util-linux \
 && rm -rf /var/lib/apt/lists/*

COPY --from=uv /uv /uvx /bin/

WORKDIR /app

# Dependency layer: only invalidated by pyproject.toml or uv.lock changes.
# `--no-dev` keeps pytest/httpx out of the runtime image. --locked makes a
# stale lockfile a build failure.
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

COPY capforge ./capforge
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# Default identity. PUID/PGID at runtime remap this user and its group; the
# entrypoint starts as root only long enough to chown the mounts and drop.
RUN useradd --uid 10001 --user-group --create-home --shell /usr/sbin/nologin capforge \
 && mkdir -p /home/capforge/.agents/skills /var/lib/capforge \
 && chown -R capforge:capforge /home/capforge /var/lib/capforge

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod 0755 /usr/local/bin/docker-entrypoint.sh

# The venv is uv-managed; running the console script directly avoids a second
# uv process (and signal-forwarding layer) as PID 1.
ENV PATH="/app/.venv/bin:$PATH" \
    PUID=10001 \
    PGID=10001 \
    CAPFORGE_CAPS_DIR=/home/capforge/.agents/skills \
    CAPFORGE_STATE_DIR=/var/lib/capforge \
    CAPFORGE_HOST=0.0.0.0 \
    CAPFORGE_PORT=8099 \
    CAPFORGE_PATH=/mcp \
    CAPFORGE_TRANSPORT=http

EXPOSE 8099

# TCP check only: MCP requires a POST with protocol headers, so a plain HTTP
# probe would report a healthy server as down.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import os, socket; socket.create_connection(('127.0.0.1', int(os.environ['CAPFORGE_PORT'])), 3).close()"

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["capforge", "serve"]
