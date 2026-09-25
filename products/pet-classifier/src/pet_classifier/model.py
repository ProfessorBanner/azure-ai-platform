"""Architecture, preprocessing and the frozen-backbone training setup.

G1 trains a linear head on top of a frozen ImageNet ResNet-18. The backbone's
weights and its BatchNorm running statistics must not move: only the new
37-class head learns. That is what makes a CPU run feasible, and it is asserted
by the tests rather than assumed.

Preprocessing is deterministic and identical for training, validation and
serving. It is described by a plain dictionary (``preprocessing_spec``) that is
written into every model package, so serving rebuilds the exact transform the
model was evaluated with instead of re-deriving it from a weights enum.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import torch
from PIL import Image, ImageOps
from torch import nn
from torch.utils.data import Dataset
from torchvision import transforms
from torchvision.models import ResNet, ResNet18_Weights, resnet18
from torchvision.transforms import InterpolationMode

from pet_classifier.config import ARCHITECTURE, PRETRAINED_WEIGHTS
from pet_classifier.data import Sample, image_path

_INTERPOLATION = {
    "bilinear": InterpolationMode.BILINEAR,
    "bicubic": InterpolationMode.BICUBIC,
    "nearest": InterpolationMode.NEAREST,
}


def _scalar(value: Any) -> int:
    """Unwrap torchvision's single-element size sequences."""
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return int(value[0])
    return int(value)


def preprocessing_spec() -> dict[str, Any]:
    """The documented inference transform for the selected pretrained weights.

    Read from the weights enum's own ``transforms()`` object — public metadata
    that does not download any weights — and flattened into JSON so it can be
    persisted alongside the model.
    """
    preset = ResNet18_Weights.IMAGENET1K_V1.transforms()
    return {
        "resize_size": _scalar(preset.resize_size),
        "crop_size": _scalar(preset.crop_size),
        "mean": [float(value) for value in preset.mean],
        "std": [float(value) for value in preset.std],
        "interpolation": str(preset.interpolation.value),
        "color_mode": "RGB",
    }


def build_transform(spec: dict[str, Any]) -> transforms.Compose:
    """Rebuild the preprocessing pipeline from a persisted specification."""
    interpolation = _INTERPOLATION.get(spec["interpolation"])
    if interpolation is None:
        raise ValueError(f"unsupported interpolation {spec['interpolation']!r}")
    return transforms.Compose(
        [
            transforms.Resize(spec["resize_size"], interpolation=interpolation),
            transforms.CenterCrop(spec["crop_size"]),
            transforms.ToTensor(),
            transforms.Normalize(mean=spec["mean"], std=spec["std"]),
        ]
    )


def check_preprocessing_compatible(expected: dict[str, Any], found: dict[str, Any]) -> None:
    """Reject a model package whose preprocessing this code cannot reproduce."""
    if expected != found:
        differences = sorted(
            key for key in set(expected) | set(found) if expected.get(key) != found.get(key)
        )
        raise ValueError(f"incompatible preprocessing specification; differs on {differences}")


def load_image(source: Any) -> Image.Image:
    """Decode an image to RGB, honouring EXIF orientation.

    Orientation is applied before anything else so a rotated phone photo is
    preprocessed the same way a correctly stored one is.
    """
    opened = Image.open(source)
    oriented = ImageOps.exif_transpose(opened) or opened
    return oriented.convert("RGB")


def build_model(num_classes: int, *, pretrained: bool) -> ResNet:
    """A ResNet-18 whose final linear layer is a ``num_classes`` head.

    ``pretrained=False`` constructs the architecture with ``weights=None`` and
    downloads nothing — this is the only path serving ever takes.
    """
    weights = ResNet18_Weights.IMAGENET1K_V1 if pretrained else None
    model: ResNet = resnet18(weights=weights)
    model.fc = nn.Linear(model.fc.in_features, num_classes)
    return model


def head(model: nn.Module) -> nn.Linear:
    """The classification head: the only part of the model that trains."""
    layer = getattr(model, "fc", None)
    if not isinstance(layer, nn.Linear):
        raise TypeError("model has no linear 'fc' head")
    return layer


def freeze_backbone(model: nn.Module) -> list[nn.Parameter]:
    """Freeze everything except the head and return the trainable parameters."""
    for parameter in model.parameters():
        parameter.requires_grad = False
    for parameter in head(model).parameters():
        parameter.requires_grad = True
    return [parameter for parameter in model.parameters() if parameter.requires_grad]


def set_train_mode(model: nn.Module) -> None:
    """Put the model in training mode with the frozen backbone still evaluating.

    ``model.eval()`` first, then the head back into training mode: the backbone's
    BatchNorm layers keep using their stored running statistics instead of
    updating them from the tiny batches a smoke run sees.
    """
    model.eval()
    head(model).train()


def cpu_state_dict(model: nn.Module) -> dict[str, torch.Tensor]:
    """An independent CPU copy of every parameter and buffer.

    ``clone().detach()`` matters: keeping a reference to the live state would
    mean the "best" checkpoint silently changed as training continued.
    """
    return {name: tensor.detach().to("cpu").clone() for name, tensor in model.state_dict().items()}


class ManifestDataset(Dataset[tuple[torch.Tensor, int]]):
    """Images named by the manifest, preprocessed identically to serving."""

    def __init__(self, root: Path, samples: Sequence[Sample], transform: transforms.Compose):
        self.root = Path(root)
        self.samples = list(samples)
        self.transform = transform

    def __len__(self) -> int:
        return len(self.samples)

    def __getitem__(self, index: int) -> tuple[torch.Tensor, int]:
        sample = self.samples[index]
        image = load_image(image_path(self.root, sample.sample_id))
        tensor: torch.Tensor = self.transform(image)
        return tensor, sample.class_index


def architecture_identifiers() -> dict[str, str]:
    """The identifiers recorded with every run and package."""
    return {"architecture": ARCHITECTURE, "pretrained_weights": PRETRAINED_WEIGHTS}
