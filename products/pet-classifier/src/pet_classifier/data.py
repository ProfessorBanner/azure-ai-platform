"""Dataset preparation, splitting, validation and the manifest.

The manifest is the data contract for the rest of the product. It records, for
every usable image: a stable identifier, the split it came from officially, the
split this product assigned it to, its class index and name, and a SHA-256 of
the image bytes. Training, evaluation and every model package refer to the
manifest by hash, so a model can always be traced back to the exact sample set
that produced it.

Class labels come from the dataset's own annotation files
(``annotations/trainval.txt`` and ``annotations/test.txt``), which are a
documented part of the Oxford-IIIT Pet release, rather than from private
attributes of the torchvision dataset object.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageFile

from pet_classifier.config import DATASET_NAME, NUM_CLASSES

MANIFEST_SCHEMA_VERSION = "1.0"
MANIFEST_FILENAME = "manifest.json"
DEFAULT_VAL_FRACTION = 0.2
DEFAULT_SEED = 42

# Truncated files should fail validation rather than be silently padded.
ImageFile.LOAD_TRUNCATED_IMAGES = False


@dataclass(frozen=True)
class Sample:
    """One usable image in the manifest."""

    sample_id: str
    source_split: str
    split: str
    class_index: int
    class_name: str
    sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "sample_id": self.sample_id,
            "source_split": self.source_split,
            "split": self.split,
            "class_index": self.class_index,
            "class_name": self.class_name,
            "sha256": self.sha256,
        }


def dataset_dir(root: Path) -> Path:
    """Directory torchvision extracts the Oxford-IIIT Pet release into."""
    return Path(root) / DATASET_NAME


def image_path(root: Path, sample_id: str) -> Path:
    """Absolute path of one image, given its stable identifier."""
    return dataset_dir(root) / "images" / f"{sample_id}.jpg"


def manifest_path(root: Path) -> Path:
    return Path(root) / MANIFEST_FILENAME


def sha256_file(path: Path) -> str:
    """SHA-256 of a file's bytes, read in chunks so large files stay cheap."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_hash(payload: Any) -> str:
    """Stable SHA-256 of a JSON-serialisable object.

    Keys are sorted and separators fixed so the same content always hashes to
    the same value, whatever order it was built in.
    """
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def download(root: Path) -> None:
    """Fetch the Oxford-IIIT Pet release through torchvision's public API.

    Both official splits are requested so the test split exists on disk and stays
    untouched. The download is the full ~790 MB archive set (~1.6 GB on disk once
    extracted) regardless of how few images a smoke run later uses.
    """
    from torchvision.datasets import OxfordIIITPet

    Path(root).mkdir(parents=True, exist_ok=True)
    for split in ("trainval", "test"):
        OxfordIIITPet(root=str(root), split=split, download=True)


def read_annotations(root: Path, split: str) -> list[tuple[str, str]]:
    """Read ``(sample_id, class_name)`` pairs from an official annotation file.

    Lines look like ``Abyssinian_100 1 1 1``: image id, 1-based class id, species
    and breed id. The class name is the image id with its trailing instance
    number removed, which is how the release encodes breed names.
    """
    path = dataset_dir(root) / "annotations" / f"{split}.txt"
    if not path.is_file():
        raise FileNotFoundError(
            f"annotation file {path} not found; run 'prepare' to download the dataset first"
        )
    pairs: list[tuple[str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        sample_id = stripped.split(" ")[0]
        pairs.append((sample_id, sample_id.rsplit("_", 1)[0]))
    return sorted(pairs)


def class_names_from(pairs: Iterable[tuple[str, str]]) -> list[str]:
    """The ordered class list: unique class names, sorted, index == position.

    Sorting makes the order a property of the data rather than of file order, so
    it is reproducible; the resulting list is persisted and carried into every
    model package so an index never silently changes meaning.
    """
    return sorted({class_name for _, class_name in pairs})


def stratified_split(
    pairs: Sequence[tuple[str, str]],
    *,
    seed: int,
    val_fraction: float,
) -> dict[str, str]:
    """Assign each sample to ``"train"`` or ``"val"``, stratified by class.

    Deterministic for a given seed: samples are sorted before shuffling, and each
    class is shuffled with its own seeded generator, so the assignment does not
    depend on dictionary or filesystem ordering.
    """
    if not 0.0 < val_fraction < 1.0:
        raise ValueError(f"val_fraction must be in (0, 1), got {val_fraction}")
    by_class: dict[str, list[str]] = defaultdict(list)
    for sample_id, class_name in sorted(pairs):
        by_class[class_name].append(sample_id)

    assignment: dict[str, str] = {}
    for class_name in sorted(by_class):
        ids = sorted(by_class[class_name])
        rng = random.Random(f"{seed}:{class_name}")
        rng.shuffle(ids)
        # At least one validation and one training sample per class whenever the
        # class has two or more samples, so no class disappears from a split.
        n_val = int(round(len(ids) * val_fraction))
        n_val = max(1, min(n_val, len(ids) - 1)) if len(ids) > 1 else 0
        for position, sample_id in enumerate(ids):
            assignment[sample_id] = "val" if position < n_val else "train"
    return assignment


def _readable(path: Path) -> bool:
    """Whether an image can actually be decoded, not merely opened."""
    try:
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            image.convert("RGB").load()
    except Exception:
        return False
    return True


def build_manifest(
    root: Path,
    *,
    seed: int = DEFAULT_SEED,
    val_fraction: float = DEFAULT_VAL_FRACTION,
) -> dict[str, Any]:
    """Build the manifest from the official splits on disk.

    Exclusions are explicit and deterministic: an image is dropped if it is
    missing, if it cannot be decoded, or if its content duplicates an image
    already kept. Duplicate resolution keeps the first sample in
    ``(split, sample_id)`` order so the same input always yields the same output.
    """
    trainval = read_annotations(root, "trainval")
    test = read_annotations(root, "test")
    class_names = class_names_from(trainval + test)
    class_index = {name: index for index, name in enumerate(class_names)}

    assignment = stratified_split(trainval, seed=seed, val_fraction=val_fraction)
    candidates: list[tuple[str, str, str]] = [
        (sample_id, "trainval", assignment[sample_id]) for sample_id, _ in trainval
    ]
    # The official test split is recorded but never assigned to train or val: it
    # stays untouched during training and model selection.
    candidates += [(sample_id, "test", "test") for sample_id, _ in test]

    samples: list[Sample] = []
    exclusions: list[dict[str, str]] = []
    seen_by_checksum: dict[str, str] = {}

    split_order = {"train": 0, "val": 1, "test": 2}
    for sample_id, source_split, split in sorted(
        candidates, key=lambda item: (split_order[item[2]], item[0])
    ):
        path = image_path(root, sample_id)
        class_name = sample_id.rsplit("_", 1)[0]
        if not path.is_file():
            exclusions.append({"sample_id": sample_id, "reason": "missing", "detail": str(path)})
            continue
        if not _readable(path):
            exclusions.append({"sample_id": sample_id, "reason": "unreadable", "detail": str(path)})
            continue
        checksum = sha256_file(path)
        previous = seen_by_checksum.get(checksum)
        if previous is not None:
            exclusions.append(
                {"sample_id": sample_id, "reason": "duplicate_content", "detail": previous}
            )
            continue
        seen_by_checksum[checksum] = sample_id
        samples.append(
            Sample(
                sample_id=sample_id,
                source_split=source_split,
                split=split,
                class_index=class_index[class_name],
                class_name=class_name,
                sha256=checksum,
            )
        )

    samples.sort(key=lambda sample: sample.sample_id)
    counts = Counter(sample.split for sample in samples)
    manifest: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "dataset": DATASET_NAME,
        "seed": seed,
        "val_fraction": val_fraction,
        "class_names": class_names,
        "counts": {split: counts.get(split, 0) for split in ("train", "val", "test")},
        "exclusions": sorted(exclusions, key=lambda item: item["sample_id"]),
        "samples": [sample.as_dict() for sample in samples],
    }
    validate_manifest(manifest)
    return manifest


def validate_manifest(manifest: dict[str, Any]) -> None:
    """Fail loudly if the manifest violates the data contract."""
    class_names = manifest["class_names"]
    if len(class_names) != NUM_CLASSES:
        raise ValueError(f"expected {NUM_CLASSES} classes, found {len(class_names)}")
    if len(set(class_names)) != len(class_names):
        raise ValueError("class names are not unique")

    by_split: dict[str, set[str]] = defaultdict(set)
    covered: dict[str, set[int]] = defaultdict(set)
    for record in manifest["samples"]:
        split = record["split"]
        index = record["class_index"]
        if not 0 <= index < NUM_CLASSES:
            raise ValueError(f"class_index {index} out of range for {record['sample_id']}")
        if class_names[index] != record["class_name"]:
            raise ValueError(
                f"class_index {index} does not match class_name {record['class_name']!r}"
            )
        by_split[split].add(record["sample_id"])
        covered[split].add(index)

    train, val, test = by_split["train"], by_split["val"], by_split["test"]
    for left_name, left, right_name, right in (
        ("train", train, "val", val),
        ("train", train, "test", test),
        ("val", val, "test", test),
    ):
        overlap = left & right
        if overlap:
            raise ValueError(
                f"{left_name}/{right_name} splits overlap on {len(overlap)} samples: "
                f"{sorted(overlap)[:5]}"
            )
    for split in ("train", "val"):
        missing = set(range(NUM_CLASSES)) - covered[split]
        if missing:
            raise ValueError(f"{split} split is missing classes {sorted(missing)}")


def manifest_hash(manifest: dict[str, Any]) -> str:
    """Hash of the manifest exactly as written to disk."""
    return canonical_hash(manifest)


def write_manifest(root: Path, manifest: dict[str, Any], *, force: bool = False) -> Path:
    path = manifest_path(root)
    if path.exists() and not force:
        raise FileExistsError(f"{path} already exists; pass --force to rebuild it")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def load_manifest(root: Path) -> dict[str, Any]:
    path = manifest_path(root)
    if not path.is_file():
        raise FileNotFoundError(
            f"{path} not found; run 'python -m pet_classifier.data prepare --root {root}' first"
        )
    manifest: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return manifest


def samples_for_split(manifest: dict[str, Any], split: str) -> list[Sample]:
    """Manifest rows for one split, in stable identifier order."""
    return [
        Sample(**record)
        for record in sorted(manifest["samples"], key=lambda item: item["sample_id"])
        if record["split"] == split
    ]


def take_per_class(samples: Sequence[Sample], limit: int | None) -> list[Sample]:
    """First ``limit`` samples of each class, deterministically.

    Selection happens inside an already-assigned split, so capping samples never
    moves an image across the train/validation boundary.
    """
    if limit is None:
        return list(samples)
    kept: list[Sample] = []
    per_class: Counter[int] = Counter()
    for sample in sorted(samples, key=lambda item: (item.class_index, item.sample_id)):
        if per_class[sample.class_index] < limit:
            kept.append(sample)
            per_class[sample.class_index] += 1
    return kept


def summarise(manifest: dict[str, Any]) -> str:
    """Human-readable summary printed by the prepare command."""
    counts = manifest["counts"]
    reasons = Counter(item["reason"] for item in manifest["exclusions"])
    lines = [
        f"dataset:        {manifest['dataset']}",
        f"classes:        {len(manifest['class_names'])}",
        f"train samples:  {counts['train']}",
        f"val samples:    {counts['val']}",
        f"test samples:   {counts['test']} (held out, untouched by training)",
        f"manifest hash:  {manifest_hash(manifest)}",
    ]
    if reasons:
        lines.append("exclusions:")
        for reason, count in sorted(reasons.items()):
            lines.append(f"  {reason}: {count}")
        for item in manifest["exclusions"]:
            lines.append(f"    {item['sample_id']} ({item['reason']}: {item['detail']})")
    else:
        lines.append("exclusions:     none")
    return "\n".join(lines)


def _prepare(args: argparse.Namespace) -> int:
    root = Path(args.root)
    if args.download:
        print(
            "Downloading Oxford-IIIT Pet (~790 MB of archives, ~1.6 GB on disk after "
            "extraction with the archives kept). The full archive is fetched even if "
            "training later uses a subset."
        )
        download(root)
    manifest = build_manifest(root, seed=args.seed, val_fraction=args.val_fraction)
    path = write_manifest(root, manifest, force=args.force)
    print(summarise(manifest))
    print(f"manifest written: {path}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m pet_classifier.data",
        description="Prepare the Oxford-IIIT Pet dataset and build the manifest.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare", help="download the dataset and build the manifest")
    prepare.add_argument("--root", default="data", help="dataset root directory (default: data)")
    prepare.add_argument("--seed", type=int, default=DEFAULT_SEED, help="split seed (default: 42)")
    prepare.add_argument(
        "--val-fraction",
        type=float,
        default=DEFAULT_VAL_FRACTION,
        help="fraction of the official trainval split held out for validation (default: 0.2)",
    )
    prepare.add_argument(
        "--no-download",
        dest="download",
        action="store_false",
        help="skip the download and build the manifest from files already on disk",
    )
    prepare.add_argument("--force", action="store_true", help="overwrite an existing manifest")
    prepare.set_defaults(func=_prepare, download=True)

    args = parser.parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    sys.exit(main())
