"""The approved-corpus manifest: content, uniqueness and exclusions."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from platform_engineering_assistant.corpus.manifest import (
    DEFAULT_MANIFEST_PATH,
    Authority,
    load_manifest,
)
from platform_engineering_assistant.errors import CorpusError, CorpusRule

# Documents the inspection established as stale, recorded in
# docs/platform/current-state.md. Citing any of them would answer "how is this
# built?" confidently and wrongly.
STALE_DOCUMENTS = (
    "docs/architecture.md",
    "README.md",
    "docs/implementation-backlog.md",
)

DOC = {
    "doc_id": "adr-0001",
    "path": "docs/adr/0001-platform-scope.md",
    "title": "ADR 0001",
    "authority": "adr",
}


def write(tmp_path: Path, payload: object) -> Path:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(payload))
    return path


# --- the shipped manifest ---------------------------------------------------


def test_shipped_manifest_loads() -> None:
    manifest = load_manifest()
    assert manifest.corpus_version >= 1
    assert len(manifest.documents) == 15


def test_every_approved_document_exists_in_the_repository(repo_root: Path) -> None:
    for document in load_manifest().documents:
        assert (repo_root / document.path).is_file(), f"{document.path} is missing"


def test_doc_ids_are_unique() -> None:
    identifiers = [document.doc_id for document in load_manifest().documents]
    assert len(identifiers) == len(set(identifiers))


def test_paths_are_unique() -> None:
    paths = [document.path for document in load_manifest().documents]
    assert len(paths) == len(set(paths))


def test_doc_ids_are_stable_slugs() -> None:
    """Ids appear in every chunk id and citation, so they must not churn."""
    for document in load_manifest().documents:
        assert document.doc_id == document.doc_id.lower()
        assert " " not in document.doc_id


def test_every_document_declares_a_known_authority() -> None:
    for document in load_manifest().documents:
        assert isinstance(document.authority, Authority)


def test_manifest_covers_the_adrs_and_the_current_state_document() -> None:
    ids = load_manifest().document_ids()
    assert {f"adr-000{n}" for n in range(1, 7)} <= ids
    assert "platform-current-state" in ids


def test_no_approved_path_escapes_the_docs_tree() -> None:
    for document in load_manifest().documents:
        assert document.path.startswith("docs/")


# --- exclusions -------------------------------------------------------------


@pytest.mark.parametrize("stale_path", STALE_DOCUMENTS)
def test_stale_document_is_not_approved(stale_path: str) -> None:
    approved = {document.path for document in load_manifest().documents}
    assert stale_path not in approved


@pytest.mark.parametrize("stale_path", STALE_DOCUMENTS)
def test_stale_document_is_explicitly_excluded_with_a_reason(stale_path: str) -> None:
    """Absence must read as a decision, not an oversight."""
    excluded = {entry.path: entry.reason for entry in load_manifest().excluded}
    assert stale_path in excluded
    assert "stale" in excluded[stale_path].lower()
    assert len(excluded[stale_path]) > 40


def test_exclusion_reasons_cite_where_the_staleness_is_recorded() -> None:
    for entry in load_manifest().excluded:
        assert "current-state.md" in entry.reason


# --- structural failures ----------------------------------------------------


def test_missing_manifest_is_a_corpus_error(tmp_path: Path) -> None:
    with pytest.raises(CorpusError) as caught:
        load_manifest(tmp_path / "absent.json")
    assert caught.value.rule is CorpusRule.MISSING


def test_malformed_json_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text("{not json")
    with pytest.raises(CorpusError) as caught:
        load_manifest(path)
    assert caught.value.rule is CorpusRule.UNREADABLE


def test_duplicate_doc_id_is_rejected(tmp_path: Path) -> None:
    """A duplicate id would make chunk ids ambiguous."""
    payload: dict[str, Any] = {
        "corpus_version": 1,
        "documents": [DOC, {**DOC, "path": "docs/adr/0002-terraform-state-backend.md"}],
    }
    with pytest.raises(CorpusError) as caught:
        load_manifest(write(tmp_path, payload))
    assert caught.value.rule is CorpusRule.DUPLICATE_DOC_ID
    assert "adr-0001" in str(caught.value)


def test_duplicate_path_is_rejected(tmp_path: Path) -> None:
    """The same document listed twice would double its weight in retrieval."""
    payload: dict[str, Any] = {
        "corpus_version": 1,
        "documents": [DOC, {**DOC, "doc_id": "adr-0001-again"}],
    }
    with pytest.raises(CorpusError) as caught:
        load_manifest(write(tmp_path, payload))
    assert caught.value.rule is CorpusRule.DUPLICATE_PATH


def test_a_path_cannot_be_both_approved_and_excluded(tmp_path: Path) -> None:
    payload: dict[str, Any] = {
        "corpus_version": 1,
        "documents": [DOC],
        "excluded": [{"path": DOC["path"], "reason": "contradictory"}],
    }
    with pytest.raises(CorpusError) as caught:
        load_manifest(write(tmp_path, payload))
    assert caught.value.rule is CorpusRule.DUPLICATE_PATH


def test_empty_document_list_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(CorpusError):
        load_manifest(write(tmp_path, {"corpus_version": 1, "documents": []}))


def test_unknown_manifest_field_is_rejected(tmp_path: Path) -> None:
    payload: dict[str, Any] = {"corpus_version": 1, "documents": [DOC], "glob": "docs/**/*.md"}
    with pytest.raises(CorpusError):
        load_manifest(write(tmp_path, payload))


def test_unknown_authority_is_rejected(tmp_path: Path) -> None:
    payload: dict[str, Any] = {
        "corpus_version": 1,
        "documents": [{**DOC, "authority": "blog-post"}],
    }
    with pytest.raises(CorpusError):
        load_manifest(write(tmp_path, payload))


def test_documentation_keys_are_ignored(tmp_path: Path) -> None:
    payload: dict[str, Any] = {"$comment": ["notes"], "corpus_version": 2, "documents": [DOC]}
    assert load_manifest(write(tmp_path, payload)).corpus_version == 2


def test_manifest_does_not_contain_a_glob_pattern() -> None:
    """Enumeration is the point: a glob would admit unreviewed documents."""
    raw = DEFAULT_MANIFEST_PATH.read_text()
    assert "*" not in json.dumps(json.loads(raw)["documents"])
