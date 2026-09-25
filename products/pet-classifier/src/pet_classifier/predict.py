"""Loading a model package and predicting from it.

The Predictor is the only thing that turns a directory on disk into
predictions. It is deliberately strict on load — required files, checksum,
schema version, class count and preprocessing must all agree — because a
mismatched package produces confident nonsense rather than an error.

Nothing here touches the network: the architecture is rebuilt with
``weights=None`` and the saved state dictionary is loaded with
``weights_only=True``, so no pretrained download and no pickle execution occur.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import torch
from PIL import Image

from pet_classifier.artifacts import (
    CLASS_NAMES_FILENAME,
    METADATA_FILENAME,
    METRICS_FILENAME,
    MODEL_FILENAME,
    REQUIRED_FILES,
    read_json,
    sha256_path,
)
from pet_classifier.config import ARCHITECTURE, ARTIFACT_SCHEMA_VERSION
from pet_classifier.model import (
    build_model,
    build_transform,
    check_preprocessing_compatible,
    preprocessing_spec,
)

DEFAULT_TOP_K = 3


@dataclass(frozen=True)
class Scored:
    """One candidate class and its model score.

    ``score`` is a softmax output. It is a model score, not a calibrated
    probability and not a confidence guarantee.
    """

    class_name: str
    score: float


@dataclass(frozen=True)
class PredictionResult:
    model_version: str
    smoke: bool
    predictions: list[Scored]


class Predictor:
    """A loaded model package, ready to score images on CPU."""

    def __init__(self, model_dir: Path):
        self.model_dir = Path(model_dir).resolve()
        self.metadata: dict[str, Any] = self._load_metadata()
        self.class_names: list[str] = self._load_class_names()
        self.preprocessing: dict[str, Any] = self.metadata["preprocessing"]
        self.transform = build_transform(self.preprocessing)
        self.model = self._load_model()

    # -- loading -----------------------------------------------------------

    def _load_metadata(self) -> dict[str, Any]:
        if not self.model_dir.is_dir():
            raise FileNotFoundError(f"model package {self.model_dir} does not exist")
        missing = [name for name in REQUIRED_FILES if not (self.model_dir / name).is_file()]
        if missing:
            raise ValueError(f"model package {self.model_dir} is missing {missing}")
        metadata: dict[str, Any] = read_json(self.model_dir / METADATA_FILENAME)
        version = metadata.get("artifact_schema_version")
        if version != ARTIFACT_SCHEMA_VERSION:
            raise ValueError(
                f"model package schema version {version!r} is not supported by this code "
                f"(expected {ARTIFACT_SCHEMA_VERSION!r})"
            )
        architecture = metadata.get("architecture")
        if architecture != ARCHITECTURE:
            raise ValueError(f"model package architecture {architecture!r} is not {ARCHITECTURE!r}")
        check_preprocessing_compatible(preprocessing_spec(), metadata["preprocessing"])
        return metadata

    def _load_class_names(self) -> list[str]:
        class_names: list[str] = read_json(self.model_dir / CLASS_NAMES_FILENAME)
        declared = int(self.metadata["num_classes"])
        if len(class_names) != declared:
            raise ValueError(
                f"metadata declares {declared} classes but class_names.json holds "
                f"{len(class_names)}"
            )
        return class_names

    def _load_model(self) -> torch.nn.Module:
        model_file = self.model_dir / MODEL_FILENAME
        expected = self.metadata["model_sha256"]
        actual = sha256_path(model_file)
        if actual != expected:
            raise ValueError(
                f"{MODEL_FILENAME} checksum mismatch: metadata says {expected}, file is {actual}"
            )
        state_dict = torch.load(model_file, map_location="cpu", weights_only=True)
        model: torch.nn.Module = build_model(len(self.class_names), pretrained=False)
        model.load_state_dict(state_dict)
        model.eval()
        return model

    # -- prediction --------------------------------------------------------

    @property
    def model_version(self) -> str:
        """The MLflow run ID the package was produced by."""
        return str(self.metadata["mlflow_run_id"])

    @property
    def is_smoke(self) -> bool:
        return bool(self.metadata.get("smoke", False))

    def metrics(self) -> dict[str, Any]:
        result: dict[str, Any] = read_json(self.model_dir / METRICS_FILENAME)
        return result

    @torch.inference_mode()
    def predict(self, image: Image.Image, *, top_k: int = DEFAULT_TOP_K) -> PredictionResult:
        """Score one already-decoded RGB image."""
        if top_k < 1:
            raise ValueError(f"top_k must be >= 1, got {top_k}")
        tensor: torch.Tensor = self.transform(image).unsqueeze(0)
        logits = self.model(tensor)
        scores = torch.softmax(logits, dim=1).squeeze(0)
        k = min(top_k, len(self.class_names))
        values, indices = torch.topk(scores, k)
        return PredictionResult(
            model_version=self.model_version,
            smoke=self.is_smoke,
            predictions=[
                Scored(class_name=self.class_names[int(index)], score=float(value))
                for value, index in zip(values.tolist(), indices.tolist(), strict=True)
            ],
        )
