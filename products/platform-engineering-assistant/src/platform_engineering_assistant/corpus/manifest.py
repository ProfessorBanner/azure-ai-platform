"""The approved-corpus manifest.

Approval is explicit and enumerated. The loader never globs the repository:
a glob admits whatever a future commit happens to add, which is precisely how an
unreviewed, superseded or sensitive document becomes a cited authority. Adding a
document to the corpus is a reviewable diff, by design.

Exclusions are recorded in the manifest with reasons, so that a document's
absence reads as a decision rather than an oversight — and so that a reviewer
who wonders "why isn't the architecture doc in here?" finds the answer next to
the data rather than in a commit message.
"""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from platform_engineering_assistant.config import PRODUCT_ROOT
from platform_engineering_assistant.errors import CorpusError, CorpusRule

DEFAULT_MANIFEST_PATH = PRODUCT_ROOT / "corpus" / "manifest.json"


class Authority(StrEnum):
    """How much weight a document carries.

    Recorded now, used later: when two documents disagree, an ADR is a decision
    and a runbook is a procedure, and an answer should be able to say which it
    is relying on.
    """

    ADR = "adr"
    CURRENT_STATE = "current-state"
    STANDARD = "standard"
    RUNBOOK = "runbook"


class ManifestDocument(BaseModel):
    """One approved document."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    doc_id: str = Field(
        min_length=1,
        pattern=r"^[a-z0-9][a-z0-9-]*$",
        description="Stable, lowercase identifier. Appears in every chunk id and citation.",
    )
    path: str = Field(min_length=1, description="Repository-relative path.")
    title: str = Field(min_length=1)
    authority: Authority


class ExcludedDocument(BaseModel):
    """A document deliberately kept out of the corpus, and why."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(min_length=1)
    reason: str = Field(min_length=1, description="Why this document must not be cited.")


class CorpusManifest(BaseModel):
    """The full approved corpus."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    corpus_version: int = Field(ge=1)
    documents: list[ManifestDocument] = Field(min_length=1)
    excluded: list[ExcludedDocument] = Field(default_factory=list)

    def document_ids(self) -> set[str]:
        return {document.doc_id for document in self.documents}

    def excluded_paths(self) -> set[str]:
        return {document.path for document in self.excluded}


def load_manifest(path: Path | None = None) -> CorpusManifest:
    """Load and validate the manifest.

    Uniqueness of both `doc_id` and `path` is checked here rather than left to
    the loader: a duplicate id would make chunk ids ambiguous, and the same
    document listed twice would double its weight in retrieval.

    Raises:
        CorpusError: if the file is missing or malformed, or if any id or path
            is duplicated.
    """
    manifest_path = DEFAULT_MANIFEST_PATH if path is None else path

    try:
        raw = json.loads(manifest_path.read_text())
    except OSError as exc:
        raise CorpusError(
            CorpusRule.MISSING,
            f"Corpus manifest could not be read: {manifest_path.name}",
        ) from exc
    except json.JSONDecodeError as exc:
        raise CorpusError(
            CorpusRule.UNREADABLE,
            f"{manifest_path.name} is not valid JSON: {exc.msg} (line {exc.lineno})",
        ) from exc

    if isinstance(raw, dict):
        raw = {key: value for key, value in raw.items() if not key.startswith("$")}

    try:
        manifest = CorpusManifest.model_validate(raw)
    except Exception as exc:
        # The manifest body is repository documentation metadata, not secret,
        # but the message still names only the file: a validation dump would be
        # unreadable and would set the wrong precedent for error text.
        raise CorpusError(
            CorpusRule.UNREADABLE,
            f"{manifest_path.name} is not a valid corpus manifest.",
        ) from exc

    identifiers = [document.doc_id for document in manifest.documents]
    duplicate_ids = sorted({name for name in identifiers if identifiers.count(name) > 1})
    if duplicate_ids:
        raise CorpusError(
            CorpusRule.DUPLICATE_DOC_ID,
            f"Manifest contains duplicate doc_id value(s): {', '.join(duplicate_ids)}",
        )

    paths = [document.path for document in manifest.documents]
    duplicate_paths = sorted({name for name in paths if paths.count(name) > 1})
    if duplicate_paths:
        raise CorpusError(
            CorpusRule.DUPLICATE_PATH,
            f"Manifest lists the same path more than once: {', '.join(duplicate_paths)}",
        )

    overlap = sorted(set(paths) & manifest.excluded_paths())
    if overlap:
        raise CorpusError(
            CorpusRule.DUPLICATE_PATH,
            f"Path(s) both approved and excluded: {', '.join(overlap)}",
        )

    return manifest
