#!/usr/bin/env bash
# REGRESSION GUARD for the container packaging contract.
#
#   ./scripts/container_smoke.sh [--tag <name:tag>] [--port <port>] [--keep]
#
# 19.1e shipped an image that built, pushed and deployed cleanly and then failed
# every Foundry session with:
#
#   /app/.../.venv/bin/python: No module named hosted_agent
#
# Nothing in the repository could have caught that, because every check ran
# against the workstation's virtualenv rather than the artefact. This script
# checks the artefact:
#
#   1. builds the image for linux/amd64 — the architecture Foundry runs;
#   2. starts it with its DECLARED CMD, not an overriding command, so the entry
#      point under test is the one Foundry invokes;
#   3. supplies PORT the way the platform does, and probes the port it gave;
#   4. requires the process to STAY ALIVE — a container that exits 3 on a
#      configuration or corpus failure is a failure here;
#   5. polls /readiness until HTTP 200;
#   6. fails if the container's stderr contains an import/module error, even if
#      readiness somehow passed.
#
# NO AZURE CALL IS MADE and nothing is pushed or deployed. The agent builds its
# providers at startup from environment configuration; constructing them does
# not contact Azure, and no request is sent to the model.
set -euo pipefail

cd "$(dirname "$0")/.."
REPO_ROOT="$(cd ../../.. && pwd)"

TAG="hosted-agent:container-smoke"
PORT=9099          # deliberately NOT 8088: proves the platform-supplied PORT
KEEP=0             # is honoured rather than a baked-in default being lucky.
READY_TIMEOUT=120

while [ $# -gt 0 ]; do
  case "$1" in
    --tag)  TAG="$2";  shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --keep) KEEP=1;    shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

CONTAINER="hosted-agent-smoke-$$"
WORK="$(mktemp -d)"

cleanup() {
  if [ "$KEEP" -eq 1 ]; then
    echo "==> kept container '$CONTAINER' and logs in $WORK"
    return
  fi
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT

fail() { echo; echo "FAILED: $*" >&2; exit 1; }

# --- 1. build ---------------------------------------------------------------
# From the repository root: the product is a path dependency and the corpus
# documents live under docs/, so a narrower context cannot build this image.
echo "==> build (linux/amd64)"
docker build --platform linux/amd64 \
  -f "$PWD/Dockerfile" \
  -t "$TAG" "$REPO_ROOT"

echo "==> the built image reports linux/amd64"
ARCH="$(docker image inspect --format '{{.Os}}/{{.Architecture}}' "$TAG")"
[ "$ARCH" = "linux/amd64" ] || fail "image architecture is $ARCH, not linux/amd64"

# --- 2. the module the entry point names is importable IN THE IMAGE ---------
# The exact failure that reached production, asserted directly.
echo "==> hosted_agent imports inside the image"
docker run --rm --platform linux/amd64 "$TAG" \
  python -c "import hosted_agent, hosted_agent.__main__; print(hosted_agent.__file__)" \
  || fail "hosted_agent is not importable in the image"

# --- 2b. it runs unprivileged -----------------------------------------------
# The Dockerfile sets `USER agent` (uid 10001), and nothing checked it. A root
# container is not a build failure, so this is exactly the kind of property that
# survives being silently dropped in a Dockerfile edit unless the artefact is
# asserted. Checked against the image's own default user, not an override.
echo "==> the image runs unprivileged as uid 10001"
RUN_UID="$(docker run --rm --platform linux/amd64 "$TAG" id -u | tr -d '[:space:]')"
[ "$RUN_UID" = "10001" ] || fail "the image runs as uid $RUN_UID, expected the non-root 10001"

# --- 2c. the approved corpus travels with the image -------------------------
# `docs/` and the corpus manifest are COPYed in, and the loader resolves manifest
# entries against the repository root marker at /app. A missing corpus does not
# fail the build — it fails the first session — so it is asserted here, in the
# artefact, rather than trusted to a COPY line.
echo "==> the approved corpus and its referenced documents ship in the image"
docker run --rm --platform linux/amd64 "$TAG" python -c "
from platform_engineering_assistant.corpus.loader import repository_root
root = repository_root()
manifest = root / 'products/platform-engineering-assistant/corpus/manifest.json'
assert manifest.is_file(), f'corpus manifest is missing at {manifest}'
import json
entries = json.loads(manifest.read_text())
docs = entries['documents'] if isinstance(entries, dict) else entries
missing = [d['path'] for d in docs if not (root / d['path']).is_file()]
assert not missing, f'corpus manifest references documents that are not in the image: {missing}'
print(f'corpus root={root} documents={len(docs)} all present')
" || fail "the approved corpus or a document it references is missing from the image"

# --- 3. start it the way Foundry does ---------------------------------------
# No command argument: the image's declared CMD is what runs.
echo "==> start with the declared CMD on the platform-supplied PORT=$PORT"
docker run -d --platform linux/amd64 \
  --name "$CONTAINER" \
  -e PORT="$PORT" \
  -e HOSTED_AGENT_NAME=phase19-hosted-controlled-agent \
  -e HOSTED_AGENT_MAX_INPUT_CHARS=4000 \
  -e AZURE_OPENAI_ENDPOINT="${AZURE_OPENAI_ENDPOINT:-https://aif-example-sandbox.openai.azure.com/openai/v1/}" \
  -e AZURE_OPENAI_DEPLOYMENT="${AZURE_OPENAI_DEPLOYMENT:-gpt-4-1-mini}" \
  -p "127.0.0.1:$PORT:$PORT" \
  "$TAG" >/dev/null

CMD_RUN="$(docker inspect --format '{{json .Config.Cmd}}' "$CONTAINER")"
ENTRY_RUN="$(docker inspect --format '{{json .Config.Entrypoint}}' "$CONTAINER")"
echo "    entrypoint: $ENTRY_RUN"
echo "    cmd:        $CMD_RUN"
[ "$CMD_RUN" = '["python","-m","hosted_agent"]' ] \
  || fail "the container is not running the declared CMD: $CMD_RUN"

# --- 4/5. stays alive, and becomes ready ------------------------------------
echo "==> the process stays alive and /readiness reaches 200 (timeout ${READY_TIMEOUT}s)"
READY=0
for _ in $(seq 1 "$READY_TIMEOUT"); do
  STATE="$(docker inspect --format '{{.State.Status}}' "$CONTAINER" 2>/dev/null || echo gone)"
  if [ "$STATE" != "running" ]; then
    CODE="$(docker inspect --format '{{.State.ExitCode}}' "$CONTAINER" 2>/dev/null || echo '?')"
    echo "--- container output ---" >&2
    docker logs "$CONTAINER" 2>&1 | tail -40 >&2
    fail "the container did not stay alive (state=$STATE exit=$CODE)"
  fi
  CODE="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/readiness" || true)"
  if [ "$CODE" = "200" ]; then READY=1; break; fi
  sleep 1
done
[ "$READY" -eq 1 ] || fail "/readiness never returned 200 within ${READY_TIMEOUT}s"
echo "    GET /readiness -> 200 $(curl -s "http://127.0.0.1:$PORT/readiness")"

# --- 6. no import/module error on stderr ------------------------------------
# Checked even though readiness passed: a broken import in a lazily loaded path
# would not stop the server coming up, and readiness is the library's, not the
# agent's. `docker logs` sends the container's stderr to ITS stderr, so the two
# streams are separable here.
echo "==> the container's stderr carries no import or module error"
docker logs "$CONTAINER" >"$WORK/stdout.log" 2>"$WORK/stderr.log"
PATTERN='No module named|ModuleNotFoundError|ImportError|cannot import name|Configuration error:|Agent could not be built'
if grep -nEi "$PATTERN" "$WORK/stderr.log"; then
  fail "an import/module error appeared on the container's stderr"
fi
# The same patterns are a failure on stdout too; the process logs to stdout.
if grep -nEi "$PATTERN" "$WORK/stdout.log"; then
  fail "an import/module error appeared in the container's output"
fi

echo
echo "OK - the image builds for linux/amd64, starts under its declared CMD,"
echo "     stays alive, imports hosted_agent, and serves /readiness on the"
echo "     platform-supplied port. Nothing was pushed and no Azure call was made."
