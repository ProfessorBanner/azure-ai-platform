from __future__ import annotations

import io

import torch
from PIL import Image
from torch import nn

from pet_classifier.config import NUM_CLASSES
from pet_classifier.model import (
    build_model,
    build_transform,
    check_preprocessing_compatible,
    cpu_state_dict,
    freeze_backbone,
    load_image,
    preprocessing_spec,
    set_train_mode,
)
from tests.conftest import make_image


def test_preprocessing_spec_is_the_documented_imagenet_transform() -> None:
    spec = preprocessing_spec()
    assert spec["crop_size"] == 224 and spec["resize_size"] == 256
    assert spec["interpolation"] == "bilinear" and spec["color_mode"] == "RGB"
    transform = build_transform(spec)
    tensor = transform(load_image(io.BytesIO(make_image(3, size=(300, 200)))))
    assert tuple(tensor.shape) == (3, 224, 224)


def test_incompatible_preprocessing_is_rejected() -> None:
    spec = preprocessing_spec()
    check_preprocessing_compatible(spec, dict(spec))
    try:
        check_preprocessing_compatible(spec, dict(spec, crop_size=200))
    except ValueError as error:
        assert "crop_size" in str(error)
    else:
        raise AssertionError("expected a ValueError")


def test_load_image_applies_exif_orientation_and_converts_to_rgb() -> None:
    image = Image.new("L", (40, 20), color=128)
    exif = image.getexif()
    exif[0x0112] = 6  # rotate 90 degrees clockwise
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", exif=exif.tobytes())
    loaded = load_image(io.BytesIO(buffer.getvalue()))
    assert loaded.mode == "RGB"
    assert loaded.size == (20, 40)


def test_only_the_head_trains_and_batchnorm_buffers_stay_fixed() -> None:
    torch.manual_seed(0)
    model = build_model(NUM_CLASSES, pretrained=False)
    trainable = freeze_backbone(model)
    assert {id(p) for p in trainable} == {id(p) for p in model.fc.parameters()}

    before = cpu_state_dict(model)
    optimizer = torch.optim.SGD(trainable, lr=0.5)
    set_train_mode(model)
    assert not model.bn1.training and model.fc.training

    images = torch.randn(4, 3, 224, 224)
    labels = torch.tensor([0, 1, 2, 3])
    optimizer.zero_grad()
    nn.CrossEntropyLoss()(model(images), labels).backward()
    optimizer.step()

    after = cpu_state_dict(model)
    for name, tensor in before.items():
        if name.startswith("fc."):
            assert not torch.equal(tensor, after[name]), f"{name} should have trained"
        else:
            assert torch.equal(tensor, after[name]), f"{name} changed but is frozen"
    # Running statistics are buffers, not parameters: check them explicitly.
    assert torch.equal(before["bn1.running_mean"], after["bn1.running_mean"])
    assert torch.equal(before["bn1.num_batches_tracked"], after["bn1.num_batches_tracked"])


def test_cpu_state_dict_is_an_independent_copy() -> None:
    model = build_model(NUM_CLASSES, pretrained=False)
    snapshot = cpu_state_dict(model)
    with torch.no_grad():
        model.fc.bias.add_(1.0)
    assert not torch.equal(snapshot["fc.bias"], model.fc.bias)
