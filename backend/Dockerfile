# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.13.0 AS uv

FROM python:3.14-slim AS base
COPY --from=uv /uv /uvx /bin/
ENV PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH"
RUN groupadd --system --gid 1000 app \
    && useradd --system --uid 1000 --gid app --create-home app \
    && mkdir /app && chown app:app /app
WORKDIR /app
USER app

# --- dev: full dependency set (incl. dev group), source bind-mounted by compose, auto-reload ---
FROM base AS dev
COPY --chown=app:app pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/home/app/.cache/uv,uid=1000,gid=1000 \
    uv sync --locked --no-install-project
COPY --chown=app:app . .
RUN --mount=type=cache,target=/home/app/.cache/uv,uid=1000,gid=1000 \
    uv sync --locked
EXPOSE 8000
CMD ["uvicorn", "financial_hub.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload", "--reload-dir", "src"]

# --- build: production dependencies only, project installed non-editable ---
FROM base AS build
COPY --chown=app:app pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/home/app/.cache/uv,uid=1000,gid=1000 \
    uv sync --locked --no-dev --no-install-project --no-editable
COPY --chown=app:app src ./src
RUN --mount=type=cache,target=/home/app/.cache/uv,uid=1000,gid=1000 \
    uv sync --locked --no-dev --no-editable

# --- runtime: no uv, no dev tools, no source tree ---
FROM python:3.14-slim AS runtime
ENV PYTHONUNBUFFERED=1 PATH="/app/.venv/bin:$PATH"
# pip is not needed at runtime and vendors its own (often outdated) urllib3/msgpack/setuptools.
RUN python -m pip uninstall --yes --disable-pip-version-check pip \
    && groupadd --system --gid 1000 app \
    && useradd --system --uid 1000 --gid app --create-home app
WORKDIR /app
COPY --from=build --chown=app:app /app/.venv ./.venv
COPY --chown=app:app alembic.ini ./
COPY --chown=app:app migrations ./migrations
USER app
EXPOSE 8000
CMD ["uvicorn", "financial_hub.main:app", "--host", "0.0.0.0", "--port", "8000"]
