"""The versioned, self-contained model package.

A package is a directory under ``artifacts/<run_id>/`` holding everything needed
to reproduce a prediction without MLflow, without the dataset and without
network access: CPU weights, the class list, the preprocessing definition, the
metrics that justified the checkpoint, the dataset manifest and the dependency
lockfile.

Packages are written into a temporary directory and published with a single
atomic rename, and an existing package is never silently overwritten — a
half-written package must never be loadable.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import torch

from pet_classifier.config import ARTIFACT_SCHEMA_VERSION, TrainingConfig

MODEL_FILENAME = "model.pt"
METADATA_FILENAME = "metadata.json"
METRICS_FILENAME = "metrics.json"
CLASS_NAMES_FILENAME = "class_names.json"
DATA_MANIFEST_FILENAME = "data_manifest.json"
LOCKFILE_FILENAME = "uv.lock"

REQUIRED_FILES = (
    MODEL_FILENAME,
    METADATA_FILENAME,
    METRICS_FILENAME,
    CLASS_NAMES_FILENAME,
    DATA_MANIFEST_FILENAME,
)


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def product_root() -> Path:
    """The product directory, i.e. the parent of ``src/``."""
    return Path(__file__).resolve().parents[2]


def git_state(repo: Path | None = None) -> dict[str, Any]:
    """Current commit and whether the working tree is dirty.

    Recorded so a package can be tied to source. A failure to read git is
    reported as ``"unknown"`` rather than aborting a training run.
    """
    cwd = str(repo or product_root())
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return {"git_sha": "unknown", "git_dirty": True}
    return {"git_sha": sha, "git_dirty": bool(status)}


def lockfile_path() -> Path:
    return product_root() / LOCKFILE_FILENAME


def lockfile_hash() -> str:
    path = lockfile_path()
    return sha256_path(path) if path.is_file() else "unknown"


def write_package(
    destination: Path,
    *,
    run_id: str,
    state_dict: dict[str, torch.Tensor],
    class_names: list[str],
    preprocessing: dict[str, Any],
    metrics: dict[str, Any],
    data_manifest: dict[str, Any],
    data_manifest_hash: str,
    config: TrainingConfig,
    architecture: str,
    pretrained_weights: str,
) -> Path:
    """Write one model package and publish it atomically.

    The weights are a ``state_dict`` of CPU tensors, not a pickled model object:
    loading must not require this package's Python classes to be importable, and
    must not execute arbitrary code.
    """
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError(f"model package {destination} already exists; refusing to overwrite")
    destination.parent.mkdir(parents=True, exist_ok=True)

    staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=str(destination.parent)))
    try:
        cpu_state = {name: tensor.detach().to("cpu").clone() for name, tensor in state_dict.items()}
        model_file = staging / MODEL_FILENAME
        torch.save(cpu_state, model_file)

        metadata = {
            "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
            "mlflow_run_id": run_id,
            "architecture": architecture,
            "pretrained_weights": pretrained_weights,
            "num_classes": len(class_names),
            "preprocessing": preprocessing,
            "data_manifest_hash": data_manifest_hash,
            "model_sha256": sha256_path(model_file),
            "device": config.device,
            "seed": config.seed,
            "profile": config.profile,
            "smoke": config.is_smoke,
            "lockfile_sha256": lockfile_hash(),
            **git_state(),
        }
        _write_json(staging / METADATA_FILENAME, metadata)
        _write_json(staging / METRICS_FILENAME, metrics)
        _write_json(staging / CLASS_NAMES_FILENAME, class_names)
        _write_json(staging / DATA_MANIFEST_FILENAME, data_manifest)

        lock = lockfile_path()
        if lock.is_file():
            shutil.copyfile(lock, staging / LOCKFILE_FILENAME)

        os.replace(staging, destination)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return destination


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))
