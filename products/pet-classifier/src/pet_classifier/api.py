"""Prediction API and the small web UI.

One Predictor is loaded once, at application start, from an explicitly selected
model directory. Restarting the app therefore serves exactly the same model —
nothing here searches for "the latest" package.

Uploads are treated as untrusted input: the request body is capped, the file is
decoded before it is trusted, only real JPEG/PNG images are accepted, the decoded
pixel count is capped (a small file can decompress into an enormous bitmap), and
the bytes are never written to disk.
"""

from __future__ import annotations

import io
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, Field

from pet_classifier.model import load_image
from pet_classifier.predict import DEFAULT_TOP_K, PredictionResult, Predictor

MODEL_DIR_ENV = "PET_CLASSIFIER_MODEL_DIR"
MLFLOW_UI_ENV = "PET_CLASSIFIER_MLFLOW_URL"
DEFAULT_MLFLOW_UI = "http://127.0.0.1:5000"

# 10 MB of request body, and 50 megapixels once decoded. The second limit is the
# one that matters: a few hundred kilobytes of PNG can expand into gigabytes of
# bitmap.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_IMAGE_PIXELS = 50_000_000
ALLOWED_FORMATS = {"JPEG", "PNG"}

_HERE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(_HERE / "templates"))


class ScoredClass(BaseModel):
    """One candidate breed and its model score (not a calibrated probability)."""

    class_name: str = Field(description="Predicted breed name.")
    score: float = Field(description="Softmax model score in [0, 1]; not a calibrated probability.")


class PredictionResponse(BaseModel):
    model_version: str = Field(description="MLflow run ID of the loaded model package.")
    smoke: bool = Field(description="True when the loaded model came from a smoke run.")
    predictions: list[ScoredClass] = Field(description="Top-k classes, highest score first.")


def _to_response(result: PredictionResult) -> PredictionResponse:
    return PredictionResponse(
        model_version=result.model_version,
        smoke=result.smoke,
        predictions=[
            ScoredClass(class_name=item.class_name, score=item.score) for item in result.predictions
        ],
    )


def decode_upload(payload: bytes) -> Image.Image:
    """Validate and decode an uploaded image, or raise a 4xx HTTPException.

    The declared content type is not trusted: the format comes from the decoded
    file itself.
    """
    if not payload:
        raise HTTPException(status_code=400, detail="The uploaded file is empty.")
    if len(payload) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Image exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB upload limit.",
        )
    try:
        with Image.open(io.BytesIO(payload)) as probe:
            image_format = probe.format
            width, height = probe.size
    except (UnidentifiedImageError, OSError, ValueError) as error:
        raise HTTPException(
            status_code=400, detail="The uploaded file is not a readable image."
        ) from error
    if image_format not in ALLOWED_FORMATS:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported image format {image_format!r}; upload a JPEG or PNG.",
        )
    if width * height > MAX_IMAGE_PIXELS:
        raise HTTPException(
            status_code=413,
            detail=f"Decoded image is too large ({width}x{height} pixels).",
        )
    try:
        return load_image(io.BytesIO(payload))
    except (UnidentifiedImageError, OSError, ValueError) as error:
        raise HTTPException(status_code=400, detail="The image could not be decoded.") from error


async def read_upload(upload: UploadFile) -> bytes:
    """Read an upload, refusing anything past the body limit.

    Read with a cap rather than trusting ``Content-Length``, which a client
    controls.
    """
    payload = await upload.read(MAX_UPLOAD_BYTES + 1)
    if len(payload) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Image exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB upload limit.",
        )
    return payload


def get_predictor(request: Request) -> Predictor:
    predictor = getattr(request.app.state, "predictor", None)
    if predictor is None:
        raise HTTPException(status_code=503, detail="No model is loaded.")
    return predictor  # type: ignore[no-any-return]


async def predict_upload(request: Request, upload: UploadFile) -> PredictionResult:
    """The single decode-and-predict path shared by both prediction routes."""
    payload = await read_upload(upload)
    image = decode_upload(payload)
    # The decoded image lives only for the duration of this call; nothing is
    # written to disk.
    return get_predictor(request).predict(image, top_k=DEFAULT_TOP_K)


def model_dir_from_env() -> Path | None:
    value = os.environ.get(MODEL_DIR_ENV)
    return Path(value) if value else None


def create_app(model_dir: Path | None = None) -> FastAPI:
    """Build the application around one explicitly selected model directory."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        selected = model_dir or model_dir_from_env()
        if selected is None:
            raise RuntimeError(
                f"No model selected. Set {MODEL_DIR_ENV} to an exported package "
                "directory, for example artifacts/<run_id>."
            )
        app.state.predictor = Predictor(selected)
        app.state.model_dir = Path(selected).resolve()
        try:
            yield
        finally:
            app.state.predictor = None

    app = FastAPI(
        title="Pet Classifier",
        version="0.1.0",
        description="Local pet breed classifier (G1).",
        lifespan=lifespan,
    )
    app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")

    @app.get("/health/live", tags=["health"])
    def live() -> dict[str, str]:
        """Liveness: the process is running. Says nothing about the model."""
        return {"status": "alive"}

    @app.get("/health/ready", tags=["health"])
    def ready(request: Request) -> JSONResponse:
        """Readiness: a model package is loaded and can answer requests."""
        predictor = getattr(request.app.state, "predictor", None)
        if predictor is None:
            return JSONResponse({"status": "not_ready"}, status_code=503)
        return JSONResponse(
            {
                "status": "ready",
                "model_version": predictor.model_version,
                "smoke": predictor.is_smoke,
                "num_classes": len(predictor.class_names),
            }
        )

    @app.get("/", response_class=HTMLResponse, tags=["ui"])
    def index(request: Request) -> Any:
        return templates.TemplateResponse(request, "index.html", _page_context(request))

    @app.post("/predict", response_class=HTMLResponse, tags=["ui"])
    async def predict_html(request: Request, file: Annotated[UploadFile, File()]) -> Any:
        try:
            result = await predict_upload(request, file)
        except HTTPException as error:
            context = _page_context(request) | {"error": error.detail}
            return templates.TemplateResponse(
                request, "index.html", context, status_code=error.status_code
            )
        context = _page_context(request) | {"result": _to_response(result)}
        return templates.TemplateResponse(request, "result.html", context)

    @app.post("/api/v1/predict", response_model=PredictionResponse, tags=["api"])
    async def predict_json(
        request: Request, file: Annotated[UploadFile, File()]
    ) -> PredictionResponse:
        return _to_response(await predict_upload(request, file))

    def _page_context(request: Request) -> dict[str, Any]:
        predictor = getattr(request.app.state, "predictor", None)
        return {
            "model_version": predictor.model_version if predictor else "none",
            "smoke": predictor.is_smoke if predictor else False,
            "num_classes": len(predictor.class_names) if predictor else 0,
            "mlflow_url": os.environ.get(MLFLOW_UI_ENV, DEFAULT_MLFLOW_UI),
            "max_upload_mb": MAX_UPLOAD_BYTES // (1024 * 1024),
        }

    return app


app = create_app()
