# syntax=docker/dockerfile:1

# Multi-stage build for the engineering surrogate inference service.
#
# Stage 1 resolves the locked production dependency set with uv; stage 2 carries
# only the resulting virtual environment and the application source. Build tools,
# tests, caches and dev dependencies never reach the runtime image.

# uv is pinned to an exact release rather than installed from an unversioned
# script, so the dependency resolver itself is reproducible.
FROM ghcr.io/astral-sh/uv:0.12.0 AS uv

FROM python:3.12-slim-bookworm AS builder

COPY --from=uv /uv /usr/local/bin/uv

ENV UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Only the dependency manifests are copied here, so this layer is rebuilt when
# dependencies change rather than on every source edit.
COPY pyproject.toml uv.lock ./

# --frozen: fail rather than silently re-resolve; the lock file is the contract.
# --no-dev: pytest, ruff and mypy must not exist in a runtime image.
# --no-install-project: the application is added to the runtime image as source
#   on PYTHONPATH, so the build stage installs dependencies only and needs no
#   copy of src/ to resolve them.
RUN uv sync --frozen --no-dev --no-install-project

FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    PATH="/app/.venv/bin:${PATH}"

# Dedicated non-root identity with a stable numeric UID/GID. The Helm chart pins
# runAsUser/runAsGroup to the same 10001, so the container's filesystem
# ownership and the pod security context cannot drift apart.
RUN groupadd --gid 10001 app \
  && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin app

WORKDIR /app

COPY --from=builder --chown=10001:10001 /app/.venv /app/.venv
COPY --chown=10001:10001 src /app/src

USER 10001:10001

EXPOSE 8000

# urllib comes from the standard library, so the runtime image needs no curl or
# wget. A non-200 status or any exception exits non-zero and marks the container
# unhealthy.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=2).status == 200 else 1)"]

CMD ["uvicorn", "engineering_test_platform.app:app", "--host", "0.0.0.0", "--port", "8000"]
