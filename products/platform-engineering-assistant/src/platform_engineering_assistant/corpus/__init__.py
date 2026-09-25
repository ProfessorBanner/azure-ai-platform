"""Approved-corpus admission: what may be cited, and the rules for reading it."""

from platform_engineering_assistant.corpus.loader import (
    LoadedDocument,
    load_corpus,
    read_document,
    repository_root,
    scan_for_credential_material,
)
from platform_engineering_assistant.corpus.manifest import (
    CorpusManifest,
    ExcludedDocument,
    ManifestDocument,
    load_manifest,
)

__all__ = [
    "CorpusManifest",
    "ExcludedDocument",
    "LoadedDocument",
    "ManifestDocument",
    "load_corpus",
    "load_manifest",
    "read_document",
    "repository_root",
    "scan_for_credential_material",
]
