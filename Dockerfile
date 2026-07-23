# Pinned uv distribution image; provides /uv and /uvx only.
# Digests are refreshed automatically by Dependabot (see .github/dependabot.yml).
FROM ghcr.io/astral-sh/uv:0.11.31@sha256:ecd4de2f060c64bea0ff8ecb182ddf46ba3fcccdc8a60cfdbaf20d1a047d7437 AS uv

FROM python:3.14-slim@sha256:cea0e6040540fb2b965b6e7fb5ffa00871e632eef63719f0ea54bca189ce14a6 AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY --from=uv /uv /uvx /bin/

# Install locked third-party dependencies first so this layer caches
# independently of application source changes. --no-cache keeps uv's
# package cache out of the final image.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project --no-cache

# Install the project itself from the same lockfile resolution.
COPY README.md ./
COPY bridge ./bridge
RUN uv sync --locked --no-dev --no-cache

# Run as a dedicated non-root user. /data holds the SQLite state file
# (SCREENARR_DATA_PATH, default /data/screenarr.db), so it must be writable.
# /app stays root-owned: the runtime user gets no write access to the
# application code or virtual environment.
RUN groupadd --system screenarr \
    && useradd --system --gid screenarr --no-create-home screenarr \
    && mkdir -p /data \
    && chown -R screenarr:screenarr /data

USER screenarr

EXPOSE 7879

# Slim images ship no curl; probe the health endpoint with Python stdlib.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7879/healthz', timeout=3)"]

CMD ["uvicorn", "bridge.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "7879"]
