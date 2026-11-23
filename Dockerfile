# syntax=docker/dockerfile:1.7
# One image runs everything: `projectx api` (serves the API and the built cockpit) and `projectx worker`.

# ---------- 1. the cockpit (static files)
FROM node:24-bookworm-slim AS web
WORKDIR /app/web
COPY web/package.json web/pnpm-lock.yaml web/pnpm-workspace.yaml ./
RUN npm install -g "pnpm@$(node -p "require('./package.json').packageManager.split('@')[1]")" \
 && pnpm install --frozen-lockfile
COPY web/ ./
RUN pnpm run build

# ---------- 2. Python dependencies
FROM ghcr.io/astral-sh/uv:0.9-python3.13-bookworm-slim AS deps
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app/backend
COPY backend/pyproject.toml backend/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project
COPY backend/ ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev

# ---------- 3. runtime: small, non-root, read-only friendly
FROM python:3.13-slim-bookworm AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH="/app/backend/.venv/bin:$PATH" \
    PX_ENV=prod PX_LOG_JSON=true PX_BLOB_DIR=/data/blobs
RUN groupadd --system --gid 10001 app && useradd --system --uid 10001 --gid app --home /app app \
 && mkdir -p /data/blobs && chown app:app /data/blobs
WORKDIR /app
COPY --from=deps --chown=app:app /app/backend /app/backend
COPY --from=web --chown=app:app /app/web/dist /app/web/dist
USER app
EXPOSE 8010
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8010/healthz', timeout=2).status == 200 else 1)"
CMD ["projectx", "api", "--host", "0.0.0.0", "--port", "8010"]
