from __future__ import annotations

import io
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from pet_classifier import api
from pet_classifier.api import MAX_UPLOAD_BYTES, PredictionResponse, create_app
from tests.conftest import make_image


@pytest.fixture
def client(model_package: Path) -> Iterator[TestClient]:
    with TestClient(create_app(model_dir=model_package)) as client:
        yield client


def _upload(
    payload: bytes, name: str = "pet.png", content_type: str = "image/png"
) -> dict[str, tuple[str, io.BytesIO, str]]:
    return {"file": (name, io.BytesIO(payload), content_type)}


def test_health_endpoints(client: TestClient) -> None:
    assert client.get("/health/live").json() == {"status": "alive"}
    ready = client.get("/health/ready")
    assert ready.status_code == 200
    assert ready.json()["model_version"] == "run-test" and ready.json()["smoke"] is True


def test_index_shows_model_version_and_smoke_flag(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    body = response.text
    assert "run-test" in body and "Smoke model" in body and "127.0.0.1:5000" in body
    assert 'enctype="multipart/form-data"' in body
    assert client.get("/static/style.css").status_code == 200


def test_json_prediction_matches_schema(client: TestClient, png_bytes: bytes) -> None:
    response = client.post("/api/v1/predict", files=_upload(png_bytes))
    assert response.status_code == 200, response.text
    parsed = PredictionResponse.model_validate(response.json())
    assert parsed.model_version == "run-test" and parsed.smoke is True
    assert len(parsed.predictions) == 3
    scores = [p.score for p in parsed.predictions]
    assert scores == sorted(scores, reverse=True)
    assert all(0.0 <= s <= 1.0 for s in scores)


def test_jpeg_upload_with_misleading_content_type_is_still_accepted(
    client: TestClient, jpeg_bytes: bytes
) -> None:
    response = client.post(
        "/api/v1/predict", files=_upload(jpeg_bytes, "x.bin", "application/octet-stream")
    )
    assert response.status_code == 200


def test_html_and_json_routes_agree(client: TestClient, png_bytes: bytes) -> None:
    json_result = client.post("/api/v1/predict", files=_upload(png_bytes)).json()
    html = client.post("/predict", files=_upload(png_bytes))
    assert html.status_code == 200
    for item in json_result["predictions"]:
        assert item["class_name"].replace("_", " ") in html.text
    assert "run-test" in html.text


def test_non_image_is_rejected(client: TestClient) -> None:
    response = client.post("/api/v1/predict", files=_upload(b"not an image", "a.png"))
    assert response.status_code == 400
    html = client.post("/predict", files=_upload(b"not an image", "a.png"))
    assert html.status_code == 400 and "not a readable image" in html.text


def test_empty_upload_is_rejected(client: TestClient) -> None:
    assert client.post("/api/v1/predict", files=_upload(b"")).status_code == 400


def test_unsupported_format_is_rejected(client: TestClient) -> None:
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8)).save(buffer, format="GIF")
    response = client.post(
        "/api/v1/predict", files=_upload(buffer.getvalue(), "a.gif", "image/gif")
    )
    assert response.status_code == 415


def test_oversized_body_is_rejected(client: TestClient) -> None:
    payload = b"\x00" * (MAX_UPLOAD_BYTES + 1)
    response = client.post("/api/v1/predict", files=_upload(payload))
    assert response.status_code == 413


def test_decoded_pixel_limit_is_enforced(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api, "MAX_IMAGE_PIXELS", 100)
    response = client.post("/api/v1/predict", files=_upload(make_image(4, size=(20, 20))))
    assert response.status_code == 413
    assert "too large" in response.json()["detail"]


def test_missing_file_field_is_a_client_error(client: TestClient) -> None:
    assert client.post("/api/v1/predict").status_code == 422


def test_app_refuses_to_start_without_a_selected_model(no_model_env: None) -> None:
    with pytest.raises(RuntimeError, match="No model selected"):
        with TestClient(create_app()):
            pass


def test_restart_serves_the_same_explicit_model(
    model_package: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PET_CLASSIFIER_MODEL_DIR", str(model_package))
    versions = []
    for _ in range(2):
        with TestClient(create_app()) as client:
            versions.append(client.get("/health/ready").json()["model_version"])
    assert versions == ["run-test", "run-test"]


def test_ready_is_503_when_no_model_is_loaded(model_package: Path) -> None:
    app = create_app(model_dir=model_package)
    # Outside the lifespan the predictor is not loaded.
    client = TestClient(app)
    assert client.get("/health/ready").status_code == 503
    assert client.post("/api/v1/predict", files=_upload(make_image(5))).status_code == 503
