"""Shared offline fixtures.

Nothing here touches the network: images are generated in memory, the fake
dataset root mimics the Oxford-IIIT Pet layout with tiny files, and the model
package is exported from a randomly initialised ResNet-18 (``weights=None``).
"""

from __future__ import annotations

import io
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
import torch
from PIL import Image

from pet_classifier.artifacts import write_package
from pet_classifier.config import (
    ARCHITECTURE,
    DATASET_NAME,
    NUM_CLASSES,
    PRETRAINED_WEIGHTS,
    TrainingConfig,
)
from pet_classifier.model import build_model, cpu_state_dict, preprocessing_spec

# The G3 cost-control tests import the scripts under scripts/g3 as modules.
G3_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts" / "g3"
if str(G3_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(G3_SCRIPTS))

# 37 fake breed names in the same ``Name_with_underscores`` style as the real data.
FAKE_CLASS_NAMES = sorted(f"breed_{index:02d}" for index in range(NUM_CLASSES))


def make_image(seed: int, size: tuple[int, int] = (32, 24), fmt: str = "PNG") -> bytes:
    """A small deterministic RGB image encoded as ``fmt``."""
    generator = torch.Generator().manual_seed(seed)
    pixels = torch.randint(0, 256, (size[1], size[0], 3), generator=generator, dtype=torch.uint8)
    image = Image.fromarray(pixels.numpy())
    buffer = io.BytesIO()
    image.save(buffer, format=fmt)
    return buffer.getvalue()


@pytest.fixture
def png_bytes() -> bytes:
    return make_image(1, fmt="PNG")


@pytest.fixture
def jpeg_bytes() -> bytes:
    return make_image(2, fmt="JPEG")


@pytest.fixture
def fake_dataset_root(tmp_path: Path) -> Path:
    """A tiny dataset root in the official layout.

    Every class has three trainval images and one test image, except the two
    classes that carry a planted defect, which have eight so that excluding one
    sample cannot empty their validation split. The defects exercise the
    manifest's exclusion rules: ``breed_00_3.jpg`` is not an image, and
    ``breed_01_2.jpg`` is a byte-for-byte copy of ``breed_01_1.jpg``.
    """
    root = tmp_path / "data"
    images = root / DATASET_NAME / "images"
    annotations = root / DATASET_NAME / "annotations"
    images.mkdir(parents=True)
    annotations.mkdir(parents=True)

    trainval_lines = ["#Image CLASS-ID SPECIES BREED ID"]
    test_lines: list[str] = []
    seed = 0
    for class_index, class_name in enumerate(FAKE_CLASS_NAMES):
        instances = 8 if class_index in (0, 1) else 3
        for instance in range(1, instances + 1):
            sample_id = f"{class_name}_{instance}"
            seed += 1
            (images / f"{sample_id}.jpg").write_bytes(make_image(seed, fmt="JPEG"))
            trainval_lines.append(f"{sample_id} {class_index + 1} 1 {class_index + 1}")
        sample_id = f"{class_name}_{instances + 1}"
        seed += 1
        (images / f"{sample_id}.jpg").write_bytes(make_image(seed, fmt="JPEG"))
        test_lines.append(f"{sample_id} {class_index + 1} 1 {class_index + 1}")

    (images / "breed_00_3.jpg").write_bytes(b"this is not an image")
    (images / "breed_01_2.jpg").write_bytes((images / "breed_01_1.jpg").read_bytes())

    (annotations / "trainval.txt").write_text("\n".join(trainval_lines) + "\n")
    (annotations / "test.txt").write_text("\n".join(test_lines) + "\n")
    return root


@pytest.fixture(scope="session")
def random_model() -> torch.nn.Module:
    """A ResNet-18 with a 37-class head and random weights; no download."""
    torch.manual_seed(0)
    model: torch.nn.Module = build_model(NUM_CLASSES, pretrained=False)
    model.eval()
    return model


@pytest.fixture
def fake_manifest() -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "dataset": DATASET_NAME,
        "seed": 42,
        "val_fraction": 0.2,
        "class_names": FAKE_CLASS_NAMES,
        "counts": {"train": 0, "val": 0, "test": 0},
        "exclusions": [],
        "samples": [],
    }


@pytest.fixture
def model_package(
    tmp_path: Path, random_model: torch.nn.Module, fake_manifest: dict[str, object]
) -> Path:
    """An exported model package built from ``random_model``."""
    return write_package(
        tmp_path / "artifacts" / "run-test",
        run_id="run-test",
        state_dict=cpu_state_dict(random_model),
        class_names=FAKE_CLASS_NAMES,
        preprocessing=preprocessing_spec(),
        metrics={"smoke": True, "validation": {"accuracy": 0.0, "macro_f1": 0.0}},
        data_manifest=fake_manifest,
        data_manifest_hash="0" * 64,
        config=TrainingConfig(profile="smoke"),
        architecture=ARCHITECTURE,
        pretrained_weights=PRETRAINED_WEIGHTS,
    )


@pytest.fixture
def no_model_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv("PET_CLASSIFIER_MODEL_DIR", raising=False)
    yield
