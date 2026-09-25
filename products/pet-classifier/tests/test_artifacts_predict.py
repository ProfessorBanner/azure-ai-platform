from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
import torch

from pet_classifier.artifacts import (
    LOCKFILE_FILENAME,
    METADATA_FILENAME,
    MODEL_FILENAME,
    REQUIRED_FILES,
    read_json,
    write_package,
)
from pet_classifier.config import (
    ARCHITECTURE,
    ARTIFACT_SCHEMA_VERSION,
    NUM_CLASSES,
    PRETRAINED_WEIGHTS,
    TrainingConfig,
)
from pet_classifier.model import (
    build_transform,
    cpu_state_dict,
    head,
    load_image,
    preprocessing_spec,
)
from pet_classifier.predict import Predictor
from tests.conftest import FAKE_CLASS_NAMES, make_image


def test_package_layout_and_metadata(model_package: Path) -> None:
    for name in REQUIRED_FILES:
        assert (model_package / name).is_file()
    assert (model_package / LOCKFILE_FILENAME).is_file()
    metadata = read_json(model_package / METADATA_FILENAME)
    for key in (
        "artifact_schema_version",
        "mlflow_run_id",
        "architecture",
        "num_classes",
        "preprocessing",
        "pretrained_weights",
        "data_manifest_hash",
        "model_sha256",
        "git_sha",
        "git_dirty",
        "device",
        "seed",
        "smoke",
    ):
        assert key in metadata, key
    assert metadata["artifact_schema_version"] == ARTIFACT_SCHEMA_VERSION
    assert metadata["num_classes"] == NUM_CLASSES and metadata["smoke"] is True
    assert not [p for p in model_package.parent.iterdir() if p.name.startswith(".staging")]


def test_saved_weights_are_cpu_tensors_in_a_plain_state_dict(model_package: Path) -> None:
    state = torch.load(model_package / MODEL_FILENAME, map_location="cpu", weights_only=True)
    assert isinstance(state, dict)
    assert all(tensor.device.type == "cpu" for tensor in state.values())
    assert "fc.weight" in state and "bn1.running_mean" in state


def test_existing_package_is_never_overwritten(
    model_package: Path, random_model: torch.nn.Module, fake_manifest: dict[str, object]
) -> None:
    with pytest.raises(FileExistsError):
        write_package(
            model_package,
            run_id="run-test",
            state_dict=cpu_state_dict(random_model),
            class_names=FAKE_CLASS_NAMES,
            preprocessing=preprocessing_spec(),
            metrics={},
            data_manifest=fake_manifest,
            data_manifest_hash="0" * 64,
            config=TrainingConfig(profile="smoke"),
            architecture=ARCHITECTURE,
            pretrained_weights=PRETRAINED_WEIGHTS,
        )


def test_save_load_prediction_equivalence(
    model_package: Path, random_model: torch.nn.Module
) -> None:
    predictor = Predictor(model_package)
    image = load_image(io.BytesIO(make_image(11, size=(120, 90))))
    with torch.inference_mode():
        expected = torch.softmax(
            random_model(build_transform(preprocessing_spec())(image)[None]), 1
        )
    result = predictor.predict(image)
    assert result.model_version == "run-test" and result.smoke is True
    assert len(result.predictions) == 3
    top = torch.topk(expected.squeeze(0), 3)
    for item, value, index in zip(result.predictions, top.values, top.indices, strict=True):
        assert item.class_name == FAKE_CLASS_NAMES[int(index)]
        assert item.score == pytest.approx(float(value), abs=1e-5)


def test_top_k_labels_follow_the_head(model_package: Path) -> None:
    predictor = Predictor(model_package)
    layer = head(predictor.model)
    with torch.no_grad():
        layer.weight.zero_()
        layer.bias.zero_()
        layer.bias[5] = 10.0
        layer.bias[17] = 5.0
        layer.bias[30] = 2.0
    result = predictor.predict(load_image(io.BytesIO(make_image(12))))
    assert [p.class_name for p in result.predictions] == [
        FAKE_CLASS_NAMES[5],
        FAKE_CLASS_NAMES[17],
        FAKE_CLASS_NAMES[30],
    ]
    assert result.predictions[0].score > result.predictions[1].score > result.predictions[2].score
    assert sum(p.score for p in result.predictions) <= 1.0 + 1e-6


def _edit_metadata(package: Path, **changes: object) -> None:
    metadata = read_json(package / METADATA_FILENAME)
    metadata.update(changes)
    (package / METADATA_FILENAME).write_text(json.dumps(metadata))


def test_tampered_weights_are_rejected(model_package: Path) -> None:
    with open(model_package / MODEL_FILENAME, "r+b") as handle:
        handle.seek(-1, 2)
        last = handle.read(1)
        handle.seek(-1, 2)
        handle.write(bytes([last[0] ^ 0xFF]))
    with pytest.raises(ValueError, match="checksum"):
        Predictor(model_package)


def test_wrong_schema_version_is_rejected(model_package: Path) -> None:
    _edit_metadata(model_package, artifact_schema_version="0.9")
    with pytest.raises(ValueError, match="schema version"):
        Predictor(model_package)


def test_wrong_architecture_is_rejected(model_package: Path) -> None:
    _edit_metadata(model_package, architecture="resnet50")
    with pytest.raises(ValueError, match="architecture"):
        Predictor(model_package)


def test_class_count_mismatch_is_rejected(model_package: Path) -> None:
    _edit_metadata(model_package, num_classes=36)
    with pytest.raises(ValueError, match="classes"):
        Predictor(model_package)


def test_incompatible_preprocessing_is_rejected(model_package: Path) -> None:
    spec = dict(preprocessing_spec(), crop_size=200)
    _edit_metadata(model_package, preprocessing=spec)
    with pytest.raises(ValueError, match="preprocessing"):
        Predictor(model_package)


def test_missing_file_is_rejected(model_package: Path) -> None:
    (model_package / "class_names.json").unlink()
    with pytest.raises(ValueError, match="missing"):
        Predictor(model_package)


def test_missing_directory_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        Predictor(tmp_path / "nowhere")
