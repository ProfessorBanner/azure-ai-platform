"""CPU training of the 37-class head, tracked in MLflow.

One run: load the manifest, build deterministic loaders, train only the new
head on top of the frozen backbone, evaluate every epoch on the full validation
subset, keep an independent copy of the best checkpoint by macro-F1, and export
a self-contained model package.

A smoke run proves the pipeline works end to end. It does not produce a useful
model, and nothing here pretends otherwise: there is no quality threshold, and
the majority-class baseline is logged next to the model's own metrics so a weak
result is visible rather than flattering.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import mlflow
import numpy as np
import torch
from torch import nn
from torch.utils.data import DataLoader

from pet_classifier.artifacts import git_state, lockfile_hash, write_package
from pet_classifier.config import (
    ARCHITECTURE,
    DEFAULT_EXPERIMENT,
    DEFAULT_TRACKING_URI,
    NUM_CLASSES,
    PRETRAINED_WEIGHTS,
    PROFILES,
    TrainingConfig,
    config_for_profile,
)
from pet_classifier.data import (
    Sample,
    load_manifest,
    manifest_hash,
    samples_for_split,
    take_per_class,
)
from pet_classifier.evaluate import (
    compute_metrics,
    majority_baseline,
    predict_loader,
    scalar_metrics,
)
from pet_classifier.model import (
    ManifestDataset,
    build_model,
    build_transform,
    cpu_state_dict,
    freeze_backbone,
    preprocessing_spec,
    set_train_mode,
)


def set_seeds(seed: int) -> None:
    """Seed every generator this run uses.

    Reproducibility here is best effort: the same seed on the same machine and
    the same library versions should reproduce a run, but results are not
    promised to be bit-for-bit identical across platforms or hardware.
    """
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.use_deterministic_algorithms(True, warn_only=True)


def resolve_device(name: str) -> torch.device:
    """The device the user asked for, or an error. Never a silent fallback."""
    device = torch.device(name)
    if device.type == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("device 'cuda' was requested but CUDA is not available")
    if device.type == "mps" and not torch.backends.mps.is_available():
        raise RuntimeError("device 'mps' was requested but MPS is not available")
    return device


def build_loaders(
    config: TrainingConfig,
    train_samples: Sequence[Sample],
    val_samples: Sequence[Sample],
) -> tuple[DataLoader[tuple[torch.Tensor, int]], DataLoader[tuple[torch.Tensor, int]]]:
    """Loaders using one deterministic transform for both splits."""
    transform = build_transform(preprocessing_spec())
    generator = torch.Generator()
    generator.manual_seed(config.seed)
    train_loader = DataLoader(
        ManifestDataset(config.data_root, train_samples, transform),
        batch_size=config.batch_size,
        shuffle=True,
        num_workers=config.num_workers,
        generator=generator,
    )
    val_loader = DataLoader(
        ManifestDataset(config.data_root, val_samples, transform),
        batch_size=config.batch_size,
        shuffle=False,
        num_workers=config.num_workers,
    )
    return train_loader, val_loader


def train_one_epoch(
    model: nn.Module,
    loader: DataLoader[tuple[torch.Tensor, int]],
    criterion: nn.Module,
    optimizer: torch.optim.Optimizer,
    device: torch.device,
) -> float:
    """One pass over the training set; returns the mean loss per sample."""
    set_train_mode(model)
    total_loss = 0.0
    total_samples = 0
    for images, labels in loader:
        images = images.to(device)
        labels = labels.to(device)
        optimizer.zero_grad(set_to_none=True)
        logits = model(images)
        loss = criterion(logits, labels)
        loss.backward()
        optimizer.step()
        total_loss += float(loss.item()) * labels.size(0)
        total_samples += int(labels.size(0))
    return total_loss / max(total_samples, 1)


def run(config: TrainingConfig) -> dict[str, Any]:
    """Execute one training run and return its summary."""
    started = time.monotonic()
    set_seeds(config.seed)
    device = resolve_device(config.device)

    manifest = load_manifest(config.data_root)
    digest = manifest_hash(manifest)
    class_names: list[str] = list(manifest["class_names"])
    if len(class_names) != NUM_CLASSES:
        raise ValueError(f"manifest declares {len(class_names)} classes, expected {NUM_CLASSES}")

    train_samples = take_per_class(samples_for_split(manifest, "train"), config.train_per_class)
    val_samples = take_per_class(samples_for_split(manifest, "val"), config.val_per_class)
    if not train_samples or not val_samples:
        raise ValueError("the manifest yielded an empty train or validation subset")

    train_loader, val_loader = build_loaders(config, train_samples, val_samples)

    model = build_model(NUM_CLASSES, pretrained=True).to(device)
    head_parameters = freeze_backbone(model)
    criterion = nn.CrossEntropyLoss()
    optimizer = torch.optim.Adam(
        head_parameters, lr=config.learning_rate, weight_decay=config.weight_decay
    )

    baseline = majority_baseline(
        [sample.class_index for sample in train_samples],
        [sample.class_index for sample in val_samples],
        class_names,
    )
    preprocessing = preprocessing_spec()
    source = git_state()

    mlflow.set_tracking_uri(config.tracking_uri)
    mlflow.set_experiment(config.experiment)
    with mlflow.start_run() as active_run:
        run_id = active_run.info.run_id
        mlflow.log_params(config.as_params())
        mlflow.log_params(
            {
                "architecture": ARCHITECTURE,
                "pretrained_weights": PRETRAINED_WEIGHTS,
                "num_classes": NUM_CLASSES,
                "data_manifest_hash": digest,
                "lockfile_sha256": lockfile_hash(),
                "n_train_samples": len(train_samples),
                "n_val_samples": len(val_samples),
                **{key: str(value) for key, value in source.items()},
            }
        )
        mlflow.set_tags(
            {
                "smoke_run": str(config.is_smoke).lower(),
                "profile": config.profile,
                "device": config.device,
                "git_dirty": str(source["git_dirty"]).lower(),
            }
        )
        mlflow.log_dict({"class_names": class_names}, "class_names.json")
        mlflow.log_dict(preprocessing, "preprocessing.json")
        mlflow.log_dict(baseline, "baseline_majority_class.json")
        mlflow.log_metrics(scalar_metrics(baseline, "baseline"))

        best_macro_f1 = -1.0
        best_state: dict[str, torch.Tensor] | None = None
        best_metrics: dict[str, Any] | None = None
        best_epoch = -1

        for epoch in range(1, config.epochs + 1):
            loss = train_one_epoch(model, train_loader, criterion, optimizer, device)
            y_true, y_pred = predict_loader(model, val_loader, device)
            metrics = compute_metrics(y_true, y_pred, class_names)
            mlflow.log_metric("train_loss", loss, step=epoch)
            mlflow.log_metrics(scalar_metrics(metrics, "val"), step=epoch)
            print(
                f"epoch {epoch}/{config.epochs}  train_loss={loss:.4f}  "
                f"val_accuracy={metrics['accuracy']:.4f}  val_macro_f1={metrics['macro_f1']:.4f}"
            )
            if metrics["macro_f1"] > best_macro_f1:
                best_macro_f1 = float(metrics["macro_f1"])
                # An independent copy: keeping a reference to the live state
                # would let the "best" checkpoint change as training continues.
                best_state = cpu_state_dict(model)
                best_metrics = metrics
                best_epoch = epoch

        if best_state is None or best_metrics is None:
            raise RuntimeError("training produced no checkpoint")

        mlflow.log_metric("best_epoch", best_epoch)
        mlflow.log_metrics(
            {
                "best_val_accuracy": float(best_metrics["accuracy"]),
                "best_val_macro_f1": float(best_metrics["macro_f1"]),
            }
        )
        mlflow.log_dict(best_metrics["per_class"], "per_class_report.json")
        mlflow.log_dict(
            {"labels": class_names, "matrix": best_metrics["confusion_matrix"]},
            "confusion_matrix.json",
        )

        package_metrics = {
            "best_epoch": best_epoch,
            "validation": {
                "accuracy": best_metrics["accuracy"],
                "macro_f1": best_metrics["macro_f1"],
                "n_samples": best_metrics["n_samples"],
                "per_class": best_metrics["per_class"],
                "confusion_matrix": best_metrics["confusion_matrix"],
            },
            "baseline_majority_class": {
                "accuracy": baseline["accuracy"],
                "macro_f1": baseline["macro_f1"],
                "class_name": baseline["majority_class_name"],
            },
            "smoke": config.is_smoke,
        }
        package = write_package(
            Path(config.artifacts_root) / run_id,
            run_id=run_id,
            state_dict=best_state,
            class_names=class_names,
            preprocessing=preprocessing,
            metrics=package_metrics,
            data_manifest=manifest,
            data_manifest_hash=digest,
            config=config,
            architecture=ARCHITECTURE,
            pretrained_weights=PRETRAINED_WEIGHTS,
        )
        elapsed = time.monotonic() - started
        mlflow.log_metric("elapsed_seconds", elapsed)
        mlflow.log_params({"model_package": str(package.resolve())})
        mlflow.log_artifacts(str(package), artifact_path="model_package")

    summary = {
        "run_id": run_id,
        "package": str(package.resolve()),
        "elapsed_seconds": round(elapsed, 2),
        "device": str(device),
        "smoke": config.is_smoke,
        "best_epoch": best_epoch,
        "val_accuracy": best_metrics["accuracy"],
        "val_macro_f1": best_metrics["macro_f1"],
        "baseline_accuracy": baseline["accuracy"],
        "baseline_macro_f1": baseline["macro_f1"],
        "n_train_samples": len(train_samples),
        "n_val_samples": len(val_samples),
        "data_manifest_hash": digest,
    }
    print(json.dumps(summary, indent=2))
    if config.is_smoke:
        print(
            "\nThis was a SMOKE run: 4 train / 2 val images per class for one epoch. "
            "It demonstrates that the pipeline is wired together correctly. "
            "The reported accuracy is not evidence of a useful classifier."
        )
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m pet_classifier.train",
        description="Train the pet classifier head on CPU and export a model package.",
    )
    parser.add_argument("--profile", choices=PROFILES, default="smoke")
    parser.add_argument(
        "--device",
        default="cpu",
        help="compute device; G1 is CPU-only and never selects a GPU automatically",
    )
    parser.add_argument("--data-root", default="data", help="dataset root (default: data)")
    parser.add_argument(
        "--artifacts-root", default="artifacts", help="model package root (default: artifacts)"
    )
    parser.add_argument("--epochs", type=int, default=None)
    parser.add_argument("--batch-size", type=int, default=None)
    parser.add_argument("--learning-rate", type=float, default=None)
    parser.add_argument("--num-workers", type=int, default=None)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--tracking-uri", default=DEFAULT_TRACKING_URI)
    parser.add_argument("--experiment", default=DEFAULT_EXPERIMENT)
    args = parser.parse_args(argv)

    overrides: dict[str, object] = {
        "device": args.device,
        "data_root": Path(args.data_root),
        "artifacts_root": Path(args.artifacts_root),
        "tracking_uri": args.tracking_uri,
        "experiment": args.experiment,
    }
    for name, value in (
        ("epochs", args.epochs),
        ("batch_size", args.batch_size),
        ("learning_rate", args.learning_rate),
        ("num_workers", args.num_workers),
        ("seed", args.seed),
    ):
        if value is not None:
            overrides[name] = value

    config = config_for_profile(args.profile, **overrides)
    run(config)
    return 0


if __name__ == "__main__":
    sys.exit(main())
