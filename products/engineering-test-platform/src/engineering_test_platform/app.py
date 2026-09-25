"""FastAPI application exposing the engineering surrogate model.

Liveness and readiness are deliberately separate. Liveness answers "is this
process running?" and must stay cheap and dependency-free; readiness answers
"can this process serve traffic?" and is false until the model is loaded. A
Kubernetes rollout depends on that distinction: restarting a live-but-not-ready
pod is wrong, and routing traffic to one is worse.
"""

import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import cast

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

from engineering_test_platform.model import (
    EngineeringSurrogate,
    PredictionInput,
    PredictionOutput,
)

PREDICTION_COUNTER = Counter(
    "etp_predictions_total",
    "Total number of predictions served.",
)

PREDICTION_LATENCY = Histogram(
    "etp_prediction_latency_seconds",
    "Latency of prediction requests in seconds.",
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Load the model on startup and mark the app ready only once it is loaded."""
    app.state.model = EngineeringSurrogate()
    app.state.ready = True

    yield

    # Stop advertising readiness before shutdown so in-flight rollouts drain
    # rather than race.
    app.state.ready = False
    app.state.model = None


app = FastAPI(
    title="Engineering Test Platform",
    description="Deterministic engineering surrogate-model inference service.",
    version="0.1.0",
    lifespan=lifespan,
)


def get_model(request: Request) -> EngineeringSurrogate:
    """Return the loaded model, or raise if the app is serving before startup."""
    model = getattr(request.app.state, "model", None)

    if model is None:
        raise RuntimeError("Model is not loaded; the application is not ready.")

    return cast(EngineeringSurrogate, model)


@app.get("/health/live")
def health_live() -> dict[str, str]:
    """Liveness: the process is running. Never depends on the model."""
    return {"status": "alive"}


@app.get("/health/ready")
def health_ready(request: Request) -> Response:
    """Readiness: the model is loaded and the service can answer predictions."""
    if not getattr(request.app.state, "ready", False):
        return JSONResponse(status_code=503, content={"status": "not_ready"})

    return JSONResponse(status_code=200, content={"status": "ready"})


@app.post("/predict")
def predict(features: PredictionInput, request: Request) -> PredictionOutput:
    """Score one set of process conditions."""
    model = get_model(request)

    start = time.perf_counter()
    prediction = model.predict(features)
    PREDICTION_LATENCY.observe(time.perf_counter() - start)

    PREDICTION_COUNTER.inc()

    return prediction


@app.get("/metrics")
def metrics() -> Response:
    """Prometheus metrics for scraping."""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
