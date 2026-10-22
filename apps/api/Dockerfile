# API and worker image (build context: repository root).
#   API:        docker run IMAGE
#   Worker:     docker run IMAGE python -m cloudoptics.worker
#   Migrations: docker run IMAGE alembic -c apps/api/alembic.ini upgrade head
FROM python:3.13-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
COPY pyproject.toml uv.lock ./
COPY apps/api/pyproject.toml apps/api/pyproject.toml
COPY tools/cur-fixture/pyproject.toml tools/cur-fixture/pyproject.toml
RUN uv sync --locked --no-dev --no-install-workspace --package cloudoptics-api
COPY apps/api apps/api
RUN uv sync --locked --no-dev --no-editable --package cloudoptics-api

FROM python:3.13-slim
RUN useradd --system --uid 10001 --home /app cloudoptics
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY --chown=root:root apps/api/alembic.ini apps/api/alembic.ini
COPY --chown=root:root apps/api/migrations apps/api/migrations
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1
USER 10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')"
CMD ["uvicorn", "cloudoptics.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
