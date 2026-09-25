"""Test configuration.

Two guarantees this file exists to provide:

  NO AZURE. Nothing in this suite makes a network call, mints a credential or
  reads an ambient Azure environment variable. The fixture below strips any that
  the developer's shell happens to carry, so the suite behaves identically with
  and without an active `az login`.

  NO REAL SECRETS. Every credential-looking string in these tests is synthetic
  and constructed inline. No fixture reads a real key, and none is committed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from platform_engineering_assistant.corpus.loader import repository_root

# Variables that could otherwise let a test pick up an ambient credential.
AMBIENT_CREDENTIAL_VARS = (
    "AZURE_CLIENT_ID",
    "AZURE_CLIENT_SECRET",
    "AZURE_TENANT_ID",
    "AZURE_USERNAME",
    "AZURE_PASSWORD",
    "AZURE_OPENAI_API_KEY",
    "OPENAI_API_KEY",
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_DEPLOYMENT",
)


@pytest.fixture(autouse=True)
def isolate_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip credential and endpoint variables from every test."""
    for name in AMBIENT_CREDENTIAL_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def repo_root() -> Path:
    """The real repository root, located the same way the loader locates it."""
    return repository_root()


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    """A synthetic repository root with a `.git` marker and a docs tree.

    Used for containment tests so they never depend on, or touch, the real
    repository layout.
    """
    (tmp_path / ".git").mkdir()
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "good.md").write_text("# Title\n\nSome ordinary documentation text.\n")
    return tmp_path
