# engineering-test-platform

A small, production-shaped inference service for a deterministic **engineering
surrogate model**. It predicts a quality score from three process conditions —
temperature, pressure and rotational speed.

The model is deliberately trivial: pure Python, no numerical dependencies, no
randomness and no I/O. This product exists to practise the operational surface
around a model — containerisation, Kubernetes health checking, releases,
monitoring and rollback — so the model must never be the interesting part of a
failure.

## Endpoints

| Category | Endpoint | Behaviour |
|---|---|---|
| **Liveness** | `GET /health/live` | `200` while the process is alive. Never touches the model, so it is safe as a restart signal. |
| **Readiness** | `GET /health/ready` | `200` only once the model is loaded; `503` otherwise. Readiness is dropped again on shutdown so rollouts drain instead of racing. |
| **Inference** | `POST /predict` | Accepts `PredictionInput`, returns `PredictionOutput` (`predicted_quality`, `model_version`). Invalid input returns `422`. |
| **Metrics** | `GET /metrics` | Prometheus exposition format, including `etp_predictions_total` and `etp_prediction_latency_seconds`. |

### Prediction contract

Input ranges are enforced by Pydantic:

| Field | Unit | Range |
|---|---|---|
| `temperature_c` | °C | -50 … 500 |
| `pressure_bar` | bar absolute | 0 … 300 |
| `rotational_speed_rpm` | rpm | 0 … 20000 |

`predicted_quality` is a percentage, always clamped to **0.0 … 100.0**.
`model_version` is read from the `MODEL_VERSION` environment variable and
defaults to `engineering-surrogate-v1` — which is what makes a version-labelled
rollout and rollback observable from the response itself.

## Local development

This product has its own `pyproject.toml` and `uv.lock`, independent of the
repository root project.

```bash
# Install the locked dependency set
uv sync --project products/engineering-test-platform

# Format, lint, type-check, test
uv run --project products/engineering-test-platform ruff format .
uv run --project products/engineering-test-platform ruff check .
uv run --project products/engineering-test-platform mypy src tests
uv run --project products/engineering-test-platform pytest

# Run the service locally
uv run --project products/engineering-test-platform \
  uvicorn engineering_test_platform.app:app --reload
```

Then:

```bash
curl localhost:8000/health/ready
curl -X POST localhost:8000/predict \
  -H 'content-type: application/json' \
  -d '{"temperature_c": 85, "pressure_bar": 5.5, "rotational_speed_rpm": 3100}'
curl localhost:8000/metrics
```

## Container

The image is a two-stage build: `uv` (pinned to an exact release) resolves the
locked production dependency set, and only the resulting virtual environment and
`src/` are carried into a `python:3.12-slim-bookworm` runtime. No tests, caches,
dev dependencies or build tools reach the runtime image, which runs as a
non-root user with UID/GID 10001.

```bash
cd products/engineering-test-platform

docker build -t engineering-test-platform:local .

# Run it directly
docker run --rm -p 8000:8000 -e MODEL_VERSION=engineering-surrogate-docker \
  engineering-test-platform:local

curl localhost:8000/health/ready
```

The image declares a `HEALTHCHECK` against `/health/ready` using standard-library
`urllib`, so no `curl` is needed inside the runtime image.

## Kubernetes (local, kind)

```bash
cd products/engineering-test-platform

# One control-plane node, one worker; no host ports (use port-forward)
kind create cluster --name etp --config deploy/kind-config.yaml

# Side-load the locally built image so the cluster never pulls from a registry
kind load docker-image engineering-test-platform:local --name etp

helm lint helm -f helm/values-local.yaml
helm template etp helm -f helm/values-local.yaml
helm upgrade --install etp helm -f helm/values-local.yaml

kubectl rollout status deployment/etp-engineering-test-platform
kubectl port-forward svc/etp-engineering-test-platform 8000:8000
```

The chart deploys a Deployment and a ClusterIP Service — no ServiceAccount,
Ingress, volumes or Secrets, because the application needs none of them.

### Probes

The three probes deliberately target different endpoints:

| Probe | Path | Purpose |
|---|---|---|
| `startupProbe` | `/health/ready` | Gates the other two, so a slow start is never mistaken for a crash loop. |
| `readinessProbe` | `/health/ready` | Controls traffic; false until the model is loaded. |
| `livenessProbe` | `/health/live` | Controls restarts; independent of the model, so an unready pod is not killed. |

### Rollout and rollback

`MODEL_VERSION` is surfaced in every `/predict` response, so which release is
serving is observable from the application output:

```bash
helm upgrade --install etp helm -f helm/values-local.yaml \
  --set image.tag=local --set modelVersion=engineering-surrogate-local-v2

kubectl rollout status deployment/etp-engineering-test-platform
kubectl rollout undo deployment/etp-engineering-test-platform
```

## Scope

Steps **K1** (service, tests, quality configuration) and **K2** (container, Helm
chart, local kind configuration) are in place.

**AKS and blue-green delivery are added in subsequent MVP steps.** The chart
currently uses a standard Kubernetes Deployment with a RollingUpdate strategy;
Argo Rollouts and the blue-green cutover come later.
