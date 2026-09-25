"""Repository-level hygiene guarantees for the lab.

These read the lab's own source and documentation rather than exercising
behaviour. They exist because the properties they protect — no retired variable
names, no key-based authentication path, no token material in rendered output —
are the kind that decay silently during ordinary edits and would otherwise only
be caught by a human noticing.
"""

from __future__ import annotations

import pathlib

import pytest

from foundry_capability_lab.config import FORBIDDEN_KEY_VARS

LAB_ROOT = pathlib.Path(__file__).resolve().parent.parent

# Variable names retired in Phase 16.2. The lab is new and had no consumers, so
# they were renamed outright with no compatibility aliases; any reappearance is
# a regression, not a deprecation.
RETIRED_VARIABLE_NAMES = (
    "FOUNDRY_ENDPOINT",
    "FOUNDRY_DEPLOYMENT",
    "FOUNDRY_AUTH_SCOPE",
    "FOUNDRY_TIMEOUT_SECONDS",
)

# Role names that belong to classic Azure-OpenAI-only accounts and must not be
# RECOMMENDED for this Foundry resource. They may still be mentioned in order to
# warn against them, so matches are allowed only on lines that do so.
SUPERSEDED_ROLE_NAMES = (
    "Cognitive Services User",
    "Cognitive Services OpenAI User",
)

NEGATION_MARKERS = ("do not use", "neither", "nor ", "originally named", "instead of")


def lab_surface_files() -> list[pathlib.Path]:
    """The lab's SHIPPED surface: package source, README and project metadata.

    Test files are excluded on purpose. A test that proves a retired variable is
    rejected, or that a token is never rendered, has to name the very literal
    these checks forbid; scanning the tests would make the two mutually
    exclusive. The contract being enforced is about what the lab SHIPS and
    RECOMMENDS, which is exactly this set of files.
    """
    files = [path for path in (LAB_ROOT / "src").rglob("*.py") if "__pycache__" not in path.parts]
    files.append(LAB_ROOT / "README.md")
    files.append(LAB_ROOT / "pyproject.toml")
    return files


@pytest.mark.parametrize("retired", RETIRED_VARIABLE_NAMES)
def test_no_retired_variable_name_survives(retired: str) -> None:
    """`FOUNDRY_PROJECT_ENDPOINT` is reserved and must not trip this check.

    The retired names are all prefixes of nothing else, but the project variable
    deliberately keeps the FOUNDRY_ prefix, so matching is exact-token based.
    """
    offenders: list[str] = []
    for path in lab_surface_files():
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            if retired in line:
                offenders.append(f"{path.relative_to(LAB_ROOT)}:{number}")
    assert not offenders, f"retired variable {retired} still referenced at: {offenders}"


def test_reserved_project_endpoint_name_is_documented() -> None:
    """The reserved name must remain present and explained, not merely absent."""
    readme = (LAB_ROOT / "README.md").read_text()
    assert "FOUNDRY_PROJECT_ENDPOINT" in readme
    assert "reserved" in readme.lower()


def test_current_variable_names_are_documented() -> None:
    readme = (LAB_ROOT / "README.md").read_text()
    for name in (
        "AZURE_OPENAI_ENDPOINT",
        "AZURE_OPENAI_DEPLOYMENT",
        "AZURE_OPENAI_AUTH_SCOPE",
        "AZURE_OPENAI_TIMEOUT_SECONDS",
    ):
        assert name in readme, f"{name} is not documented in the README"


@pytest.mark.parametrize("role", SUPERSEDED_ROLE_NAMES)
def test_superseded_roles_are_never_recommended(role: str) -> None:
    """A mention is acceptable only when it sits inside a warning.

    Prose wraps, so the negation ("do not use ...") frequently lands on a
    neighbouring line. The check therefore reads a small window around the match
    rather than the single line, which is what a reader would do.
    """
    window = 2
    offenders: list[str] = []
    for path in lab_surface_files():
        lines = path.read_text().splitlines()
        for index, line in enumerate(lines):
            if role not in line:
                continue
            context = " ".join(lines[max(0, index - window) : index + window + 1]).lower()
            if not any(marker in context for marker in NEGATION_MARKERS):
                offenders.append(f"{path.relative_to(LAB_ROOT)}:{index + 1}")
    assert not offenders, f"'{role}' appears as guidance rather than a warning at: {offenders}"


def test_foundry_user_role_is_the_documented_grant() -> None:
    readme = (LAB_ROOT / "README.md").read_text()
    assert "Foundry User" in readme
    assert "53ca6127-db72-4b80-b1b0-d745d6d5456d" in readme
    # The control-plane/data-plane distinction must be spelled out, since an
    # inherited Owner grant is the most likely reason someone is confused.
    assert "dataActions" in readme


def test_no_api_key_is_ever_supplied_to_a_client() -> None:
    """The adapter must never pass key material to the SDK.

    `api_key=` as a keyword argument is the specific mistake this guards: it is
    how key auth would be reintroduced, and the account would reject it anyway.
    """
    for path in lab_surface_files():
        if path.suffix != ".py":
            continue
        source = path.read_text()
        assert "api_key=" not in source, f"{path.relative_to(LAB_ROOT)} passes an api_key argument"

    # And the refusal helper must actually be wired into the real client path.
    provider_source = (LAB_ROOT / "src/foundry_capability_lab/provider.py").read_text()
    assert "assert_no_api_key_configured()" in provider_source


def test_key_variables_are_declared_as_forbidden() -> None:
    """The refusal list must keep covering the obvious spellings."""
    for expected in ("AZURE_OPENAI_API_KEY", "OPENAI_API_KEY"):
        assert expected in FORBIDDEN_KEY_VARS


def test_no_hardcoded_credential_or_token_material() -> None:
    """No literal token, JWT or real endpoint host may be shipped."""
    for path in lab_surface_files():
        source = path.read_text()
        relative = path.relative_to(LAB_ROOT)
        # A real JWT begins "eyJ".
        assert "eyJ" not in source, f"{relative} contains JWT-like material"
        # An endpoint may appear only as a placeholder or documentation example.
        for line in source.splitlines():
            if "openai.azure.com" in line:
                assert "<" in line or "example" in line.lower(), (
                    f"{relative} contains what looks like a real endpoint host: "
                    f"endpoints belong in the environment, not the source"
                )
