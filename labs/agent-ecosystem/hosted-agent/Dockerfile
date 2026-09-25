# Container image for the Hosted Agent route.
#
# Two packaging routes exist and this repository supports both:
#   * CODE  — zip the source, `CodeConfiguration(runtime, entry_point, ...)`,
#             Foundry resolves dependencies and runs it;
#   * IMAGE — this Dockerfile, `ContainerConfiguration(image, registry_connection_id)`.
#
# The image route is the one to prefer here, because the agent needs the
# product's corpus at a pinned commit and a reproducible dependency set. A code
# zip with `remote_build` would resolve dependencies at deploy time, which is
# exactly the drift the product's `uv sync --frozen` discipline exists to prevent.
#
# NO SECRETS ARE BAKED IN. The container authenticates with the managed identity
# Foundry assigns it; the product refuses key-shaped variables outright.

# ARCHITECTURE IS ASSERTED, NOT PINNED IN THE `FROM`.
#
# `FROM --platform=linux/amd64` looks like a safety belt and is the opposite of
# one: it OVERRIDES the build's own --platform, so `docker build --platform
# linux/arm64` would silently produce a mixed image instead of failing. It is
# also what BuildKit warns about (FromPlatformFlagConstDisallowed).
#
# The architecture requirement has not been relaxed — it has moved somewhere it
# can actually fail. The build inherits the platform from `docker build
# --platform linux/amd64`, and the runtime stage ASSERTS `TARGETARCH` below, so
# a build on Apple Silicon that forgets the flag stops with a clear message
# rather than pushing an arm64 image that Foundry rejects with an exec format
# error. `scripts/container_smoke.sh` re-checks the built image independently.
FROM python:3.12-slim AS build

# uv, pinned. The toolchain that resolves a locked dependency set must itself be
# reproducible — the same rule the product CI follows.
ARG UV_VERSION=0.12.0
RUN pip install --no-cache-dir "uv==${UV_VERSION}"

# BUILT AT THE PATH IT WILL RUN AT, DELIBERATELY.
#
# Both the product and this package are installed EDITABLE (see pyproject.toml
# for why the product must be). An editable install is recorded as a `.pth`
# file in site-packages holding an ABSOLUTE path to the source tree. Building
# under /build and then relocating the tree to /app leaves those `.pth` files
# pointing at /build, which no longer exists — and the runtime fails with
# `No module named hosted_agent` even though the distribution is installed.
#
# Building under /app keeps the recorded paths true. Do not change these
# directories independently of the runtime WORKDIR below.
WORKDIR /app

# The product is a path dependency, so its source must be present to resolve.
COPY products/platform-engineering-assistant /app/products/platform-engineering-assistant
COPY labs/agent-ecosystem/hosted-agent /app/labs/agent-ecosystem/hosted-agent

WORKDIR /app/labs/agent-ecosystem/hosted-agent
# --frozen: fail rather than silently re-resolve.
RUN uv sync --frozen --no-dev

# THE APPROVED CORPUS TRAVELS WITH THE IMAGE.
#
# `corpus/manifest.json` names its documents REPOSITORY-relative — every entry
# is currently under `docs/` — and the loader resolves them against the
# repository root, refusing anything outside it. So the image is a repository
# export, not a package install: without `docs/` the process starts, fails to
# build the agent and exits 3 with category=corpus.
#
# Copied after the sync so a documentation edit does not invalidate the
# dependency layer.
COPY docs /app/docs

# THE REPOSITORY-ROOT MARKER.
#
# `corpus.loader.repository_root` walks up for a `.git` entry, deliberately
# derived rather than configured so the containment boundary cannot be widened
# by an environment variable. The real `.git` is excluded from the build context
# (history is not deployable and carries no runtime value), so the export
# declares its own root the same way a git worktree does — with a `.git` entry
# at the top of the tree. This BOUNDS the corpus to /app; it does not widen it.
RUN printf '%s\n' \
  "This is a source export of the azure-ai-platform repository, not a git" \
  "repository. It marks /app as the corpus containment root for" \
  "platform_engineering_assistant.corpus.loader.repository_root." \
  > /app/.git

FROM python:3.12-slim AS runtime

# The corpus is loaded from the repository at runtime, so the documents and the
# manifest travel with the image. They are approved documentation, not secrets.
WORKDIR /app
# Same path in both stages: the editable-install `.pth` files copied with the
# venv hold absolute paths, and only an identical layout keeps them resolvable.
COPY --from=build /app /app

# PORT IS NOT SET HERE. Foundry reserves it and injects the port the platform
# wants the process to bind; `azure.ai.agentserver.core` resolves it from the
# environment with a default of 8088, and so does this package's config. Baking
# a value in would be a container-level default competing with a platform one.
ENV PATH="/app/labs/agent-ecosystem/hosted-agent/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1

# Runs unprivileged. A hosted agent has no reason to be root.
RUN useradd --create-home --uid 10001 agent && chown -R agent:agent /app
USER agent

# THE ARCHITECTURE ASSERTION. Populated by BuildKit from the build platform;
# `docker build --platform linux/amd64` sets it to amd64. Anything else is a
# build that would push an image Foundry cannot execute.
ARG TARGETARCH
RUN test "${TARGETARCH}" = "amd64" || { \
      echo "FATAL: TARGETARCH=${TARGETARCH:-unset}, but Foundry runs linux/amd64 nodes." >&2; \
      echo "Rebuild with: docker build --platform linux/amd64 ..." >&2; \
      exit 1; \
    }

# BUILD-TIME PROOF OF THE PACKAGING CONTRACT. If the module the CMD runs is not
# importable as the user that runs it, the build fails here rather than at the
# first Foundry session. This is the check whose absence let a broken image be
# tagged, pushed and deployed.
RUN python -c "import hosted_agent, platform_engineering_assistant; print('import ok:', hosted_agent.__file__)" \
 && python -c "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('hosted_agent.__main__') else 1)"

# The documented default. The platform may bind a different port via PORT.
EXPOSE 8088

# Readiness is served by azure-ai-agentserver-responses at /readiness. The
# container does not define its own: two competing health surfaces is one more
# than anything can act on, and the library's is the one Foundry knows about.
# The port is read from the environment so the check follows the platform's
# choice rather than a hardcoded guess.
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
  CMD python -c "import os,urllib.request,sys; p=os.environ.get('PORT','8088'); sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{p}/readiness').status==200 else 1)"

CMD ["python", "-m", "hosted_agent"]
