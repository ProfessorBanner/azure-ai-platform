"""Training configuration and the constants that describe the model contract.

The configuration is a frozen dataclass so a run's settings cannot drift after
the run has started: everything logged to MLflow and written into the model
package describes the values that were actually used.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path

# Bump when the on-disk model package layout or metadata contract changes in a
# way that an older Predictor cannot read.
ARTIFACT_SCHEMA_VERSION = "1.0"

ARCHITECTURE = "resnet18"
PRETRAINED_WEIGHTS = "ResNet18_Weights.IMAGENET1K_V1"
NUM_CLASSES = 37

DATASET_NAME = "oxford-iiit-pet"
DEFAULT_EXPERIMENT = "pet-classifier"
DEFAULT_TRACKING_URI = "http://127.0.0.1:5000"

PROFILES = ("smoke", "full")
DEVICE_TYPES = ("cpu", "cuda", "mps")


@dataclass(frozen=True)
class TrainingConfig:
    """One training run's settings.

    ``train_per_class``/``val_per_class`` cap how many already-split samples of
    each class are used. They select *within* the train and validation splits
    produced by :mod:`pet_classifier.data`; they never re-split the data, so a
    smoke run and a full run see the same split boundary.
    """

    profile: str = "smoke"
    epochs: int = 1
    batch_size: int = 8
    learning_rate: float = 1e-3
    weight_decay: float = 0.0
    seed: int = 42
    device: str = "cpu"
    num_workers: int = 0
    train_per_class: int | None = 4
    val_per_class: int | None = 2
    data_root: Path = field(default_factory=lambda: Path("data"))
    artifacts_root: Path = field(default_factory=lambda: Path("artifacts"))
    tracking_uri: str = DEFAULT_TRACKING_URI
    experiment: str = DEFAULT_EXPERIMENT

    def __post_init__(self) -> None:
        if self.profile not in PROFILES:
            raise ValueError(f"profile must be one of {PROFILES}, got {self.profile!r}")
        if self.epochs < 1:
            raise ValueError(f"epochs must be >= 1, got {self.epochs}")
        if self.batch_size < 1:
            raise ValueError(f"batch_size must be >= 1, got {self.batch_size}")
        if not 0.0 < self.learning_rate < 1.0:
            raise ValueError(f"learning_rate must be in (0, 1), got {self.learning_rate}")
        if self.weight_decay < 0.0:
            raise ValueError(f"weight_decay must be >= 0, got {self.weight_decay}")
        if self.num_workers < 0:
            raise ValueError(f"num_workers must be >= 0, got {self.num_workers}")
        if self.device.split(":")[0] not in DEVICE_TYPES:
            # CPU is the explicit default. A GPU is only ever used when named on
            # the command line, and never selected automatically; an unknown
            # device string is rejected here rather than silently falling back.
            raise ValueError(f"device must be one of {DEVICE_TYPES}, got {self.device!r}")
        for name in ("train_per_class", "val_per_class"):
            value: int | None = getattr(self, name)
            if value is not None and value < 1:
                raise ValueError(f"{name} must be >= 1 or None, got {value}")
        if self.is_smoke and (self.train_per_class is None or self.val_per_class is None):
            raise ValueError("the smoke profile must cap samples per class")

    @property
    def is_smoke(self) -> bool:
        """Whether this run is an integration smoke run rather than a real run."""
        return self.profile == "smoke"

    def as_params(self) -> dict[str, str]:
        """Flatten the configuration into MLflow-loggable string parameters."""
        return {key: str(value) for key, value in sorted(asdict(self).items())}


def config_for_profile(profile: str, **overrides: object) -> TrainingConfig:
    """Build a validated configuration for a named profile.

    The smoke profile is the contract from the phase brief: four training images
    per class, two validation images per class, one epoch, batch size eight,
    ``num_workers=0``. It proves the pipeline runs end to end; it does not
    produce a useful model.
    """
    if profile == "smoke":
        base: dict[str, object] = {
            "profile": "smoke",
            "epochs": 1,
            "batch_size": 8,
            "num_workers": 0,
            "train_per_class": 4,
            "val_per_class": 2,
        }
    elif profile == "full":
        base = {
            "profile": "full",
            "epochs": 5,
            "batch_size": 32,
            "num_workers": 0,
            "train_per_class": None,
            "val_per_class": None,
        }
    else:
        raise ValueError(f"profile must be one of {PROFILES}, got {profile!r}")
    base.update(overrides)
    return TrainingConfig(**base)  # type: ignore[arg-type]
