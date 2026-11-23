# syntax=docker/dockerfile:1.7
# Platform image: API, agent worker, tool gateway, migrations and admin tasks.
# Build from the repository root:
#   docker build -f infrastructure/docker/api.Dockerfile -t aieos-platform:local .
# REPLACE_WITH_DIGEST_PIN: pin both base images by digest before publishing to ECR.

FROM python:3.12-slim AS build
ENV UV_PROJECT_ENVIRONMENT=/app/.venv UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
RUN pip install --no-cache-dir "uv>=0.5,<1"
WORKDIR /src
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

FROM python:3.12-slim AS runtime
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin aieos
COPY --from=build /app/.venv /app/.venv
WORKDIR /app
# Agent contracts, prompts, the tool catalog and model routing are read at runtime; migrations run
# from this image as a one-off task.
COPY engineering ./engineering
COPY migrations ./migrations
COPY alembic.ini ./
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    API_HOST=0.0.0.0 \
    API_PORT=8000
USER 10001
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status == 200 else 1)"]
ENTRYPOINT ["aieos-api"]
