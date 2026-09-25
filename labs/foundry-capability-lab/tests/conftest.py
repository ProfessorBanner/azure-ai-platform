"""Test configuration.

Adds the repository-local package to the path via the installed project, and
guarantees the suite can never authenticate against Azure by accident.
"""

from __future__ import annotations

import pytest

from foundry_capability_lab.config import FORBIDDEN_KEY_VARS

# Azure SDK environment variables that could otherwise let a test pick up an
# ambient credential from the developer's shell.
AMBIENT_CREDENTIAL_VARS = (
    "AZURE_CLIENT_ID",
    "AZURE_CLIENT_SECRET",
    "AZURE_TENANT_ID",
    "AZURE_USERNAME",
    "AZURE_PASSWORD",
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_DEPLOYMENT",
)


@pytest.fixture(autouse=True)
def isolate_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip credential and endpoint variables from every test.

    The suite must behave identically on a developer laptop with `az login`
    active and on a clean CI agent. Any test needing configuration builds it
    explicitly.
    """
    for name in (*FORBIDDEN_KEY_VARS, *AMBIENT_CREDENTIAL_VARS):
        monkeypatch.delenv(name, raising=False)
