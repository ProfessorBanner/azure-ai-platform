from __future__ import annotations

import json
from pathlib import Path

import pytest

from pet_classifier.config import NUM_CLASSES
from pet_classifier.data import (
    build_manifest,
    class_names_from,
    load_manifest,
    main,
    manifest_hash,
    read_annotations,
    samples_for_split,
    stratified_split,
    take_per_class,
    validate_manifest,
    write_manifest,
)
from tests.conftest import FAKE_CLASS_NAMES


def test_annotations_are_read_from_the_official_files(fake_dataset_root: Path) -> None:
    pairs = read_annotations(fake_dataset_root, "trainval")
    assert len(pairs) == (NUM_CLASSES - 2) * 3 + 2 * 8
    assert pairs[0] == ("breed_00_1", "breed_00")
    assert class_names_from(pairs) == FAKE_CLASS_NAMES


def test_stratified_split_is_deterministic_and_disjoint() -> None:
    pairs = [(f"{name}_{i}", name) for name in FAKE_CLASS_NAMES for i in range(10)]
    first = stratified_split(pairs, seed=42, val_fraction=0.2)
    second = stratified_split(list(reversed(pairs)), seed=42, val_fraction=0.2)
    assert first == second, "assignment must not depend on input order"

    other_seed = stratified_split(pairs, seed=7, val_fraction=0.2)
    assert other_seed != first, "a different seed should give a different split"

    train = {k for k, v in first.items() if v == "train"}
    val = {k for k, v in first.items() if v == "val"}
    assert train.isdisjoint(val)
    assert train | val == {sample_id for sample_id, _ in pairs}
    for name in FAKE_CLASS_NAMES:
        assert sum(1 for k, v in first.items() if v == "val" and k.startswith(name)) == 2


def test_manifest_excludes_defects_deterministically(fake_dataset_root: Path) -> None:
    manifest = build_manifest(fake_dataset_root)
    again = build_manifest(fake_dataset_root)
    assert manifest_hash(manifest) == manifest_hash(again)

    exclusions = {item["sample_id"]: item for item in manifest["exclusions"]}
    assert exclusions["breed_00_3"]["reason"] == "unreadable"
    assert len(exclusions) == 2

    # Exactly one of the identical pair survives: the one processed first in
    # (train, val, test) then identifier order. The exclusion names its twin.
    pair = {"breed_01_1", "breed_01_2"}
    dropped = pair & set(exclusions)
    assert len(dropped) == 1
    (dropped_id,) = dropped
    assert exclusions[dropped_id]["reason"] == "duplicate_content"
    assert exclusions[dropped_id]["detail"] == (pair - dropped).pop()

    ids = {record["sample_id"] for record in manifest["samples"]}
    assert "breed_00_3" not in ids and dropped_id not in ids
    assert (pair - dropped) <= ids


def test_manifest_keeps_test_split_untouched(fake_dataset_root: Path) -> None:
    manifest = build_manifest(fake_dataset_root)
    test_ids = {r["sample_id"] for r in manifest["samples"] if r["split"] == "test"}
    assert test_ids == {"breed_00_9", "breed_01_9"} | {f"{name}_4" for name in FAKE_CLASS_NAMES[2:]}
    for record in manifest["samples"]:
        assert (record["source_split"] == "test") == (record["split"] == "test")
    assert manifest["counts"]["test"] == NUM_CLASSES


def test_class_order_is_persisted_and_index_matches_name(fake_dataset_root: Path) -> None:
    manifest = build_manifest(fake_dataset_root)
    assert manifest["class_names"] == FAKE_CLASS_NAMES
    for record in manifest["samples"]:
        assert manifest["class_names"][record["class_index"]] == record["class_name"]
        assert len(record["sha256"]) == 64


def test_validate_manifest_catches_overlap_and_missing_classes(fake_dataset_root: Path) -> None:
    manifest = build_manifest(fake_dataset_root)

    overlapping = json.loads(json.dumps(manifest))
    train_row = next(r for r in overlapping["samples"] if r["split"] == "train")
    overlapping["samples"].append(dict(train_row, split="val"))
    with pytest.raises(ValueError, match="overlap"):
        validate_manifest(overlapping)

    thin = json.loads(json.dumps(manifest))
    thin["samples"] = [
        r for r in thin["samples"] if not (r["split"] == "val" and r["class_index"] == 3)
    ]
    with pytest.raises(ValueError, match="missing classes"):
        validate_manifest(thin)

    bad_index = json.loads(json.dumps(manifest))
    bad_index["samples"][0]["class_index"] = NUM_CLASSES
    with pytest.raises(ValueError, match="out of range"):
        validate_manifest(bad_index)


def test_take_per_class_selects_within_split(fake_dataset_root: Path) -> None:
    manifest = build_manifest(fake_dataset_root)
    train = samples_for_split(manifest, "train")
    capped = take_per_class(train, 1)
    assert len(capped) == NUM_CLASSES
    assert all(sample.split == "train" for sample in capped)
    assert capped == take_per_class(list(reversed(train)), 1)
    assert take_per_class(train, None) == train


def test_manifest_round_trips_and_refuses_silent_overwrite(fake_dataset_root: Path) -> None:
    manifest = build_manifest(fake_dataset_root)
    write_manifest(fake_dataset_root, manifest)
    assert manifest_hash(load_manifest(fake_dataset_root)) == manifest_hash(manifest)
    with pytest.raises(FileExistsError):
        write_manifest(fake_dataset_root, manifest)
    write_manifest(fake_dataset_root, manifest, force=True)


def test_prepare_cli_without_download(
    fake_dataset_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["prepare", "--root", str(fake_dataset_root), "--no-download"]) == 0
    out = capsys.readouterr().out
    assert "unreadable: 1" in out and "duplicate_content: 1" in out
    assert (fake_dataset_root / "manifest.json").is_file()
