from fastapi.testclient import TestClient

from engineering_test_platform.app import app
from engineering_test_platform.model import DEFAULT_MODEL_VERSION

VALID_PAYLOAD = {
    "temperature_c": 85.0,
    "pressure_bar": 5.5,
    "rotational_speed_rpm": 3100.0,
}


def test_liveness_does_not_require_the_model() -> None:
    # No `with` block: lifespan never runs, so the model is not loaded. Liveness
    # must still succeed — that is what makes it safe as a restart signal.
    client = TestClient(app)

    response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


def test_readiness_is_false_before_startup() -> None:
    client = TestClient(app)

    response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}


def test_readiness_is_true_after_startup() -> None:
    with TestClient(app) as client:
        response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_predict_returns_quality_and_model_version() -> None:
    with TestClient(app) as client:
        response = client.post("/predict", json=VALID_PAYLOAD)

    assert response.status_code == 200

    body = response.json()
    assert set(body) == {"predicted_quality", "model_version"}
    assert 0.0 <= body["predicted_quality"] <= 100.0
    assert body["model_version"] == DEFAULT_MODEL_VERSION


def test_predict_rejects_invalid_input_with_422() -> None:
    with TestClient(app) as client:
        response = client.post(
            "/predict",
            json={
                "temperature_c": 9_000.0,
                "pressure_bar": 5.0,
                "rotational_speed_rpm": 3000.0,
            },
        )

    assert response.status_code == 422


def test_predict_rejects_missing_field_with_422() -> None:
    with TestClient(app) as client:
        response = client.post("/predict", json={"temperature_c": 80.0})

    assert response.status_code == 422


def test_metrics_exposes_the_prediction_counter() -> None:
    with TestClient(app) as client:
        client.post("/predict", json=VALID_PAYLOAD)
        response = client.get("/metrics")

    assert response.status_code == 200
    assert "etp_predictions_total" in response.text
    assert "etp_prediction_latency_seconds" in response.text
