"""Validation metrics.

Every metric is computed over *all* validation samples with the complete class
label list, so classes absent from a tiny smoke run still appear (with zeros)
instead of quietly shrinking the report. ``zero_division=0`` is explicit: a
class with no predictions scores zero rather than raising or producing NaN.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from typing import Any

import torch
from sklearn.metrics import (
    accuracy_score,
    classification_report,
    confusion_matrix,
    f1_score,
)
from torch import nn
from torch.utils.data import DataLoader


def compute_metrics(
    y_true: Sequence[int],
    y_pred: Sequence[int],
    class_names: Sequence[str],
) -> dict[str, Any]:
    """Accuracy, macro-F1, the per-class report and the confusion matrix."""
    labels = list(range(len(class_names)))
    report: dict[str, Any] = classification_report(
        y_true,
        y_pred,
        labels=labels,
        target_names=list(class_names),
        output_dict=True,
        zero_division=0,
    )
    matrix = confusion_matrix(y_true, y_pred, labels=labels)
    return {
        "accuracy": float(accuracy_score(y_true, y_pred)),
        "macro_f1": float(
            f1_score(y_true, y_pred, labels=labels, average="macro", zero_division=0)
        ),
        "n_samples": len(y_true),
        "per_class": report,
        "confusion_matrix": matrix.tolist(),
    }


def majority_baseline(
    train_labels: Sequence[int],
    val_labels: Sequence[int],
    class_names: Sequence[str],
) -> dict[str, Any]:
    """Metrics for always predicting the most common training class.

    This is the floor a trained model has to beat. Ties break on the lowest class
    index so the baseline is deterministic.
    """
    counts = Counter(train_labels)
    majority = min(counts.items(), key=lambda item: (-item[1], item[0]))[0] if counts else 0
    predictions = [majority] * len(val_labels)
    metrics = compute_metrics(val_labels, predictions, class_names)
    metrics["majority_class_index"] = int(majority)
    metrics["majority_class_name"] = class_names[majority]
    return metrics


@torch.inference_mode()
def predict_loader(
    model: nn.Module,
    loader: DataLoader[tuple[torch.Tensor, int]],
    device: torch.device,
) -> tuple[list[int], list[int]]:
    """Run the model over a loader and return ``(y_true, y_pred)``."""
    model.eval()
    y_true: list[int] = []
    y_pred: list[int] = []
    for images, labels in loader:
        logits = model(images.to(device))
        y_pred.extend(int(value) for value in logits.argmax(dim=1).tolist())
        y_true.extend(int(value) for value in labels.tolist())
    return y_true, y_pred


def scalar_metrics(metrics: dict[str, Any], prefix: str) -> dict[str, float]:
    """The subset of a metrics dict that MLflow can log as numbers."""
    return {
        f"{prefix}_accuracy": float(metrics["accuracy"]),
        f"{prefix}_macro_f1": float(metrics["macro_f1"]),
    }
