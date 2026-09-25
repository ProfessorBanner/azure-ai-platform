"""Corpus loader: containment rules and credential safety.

Every credential-looking string here is SYNTHETIC and built inline. No fixture
reads a real key, and nothing in this file is a working secret.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from platform_engineering_assistant.corpus.loader import (
    load_corpus,
    read_document,
    repository_root,
    resolve_document_path,
    scan_for_credential_material,
)
from platform_engineering_assistant.corpus.manifest import (
    Authority,
    CorpusManifest,
    ManifestDocument,
    load_manifest,
)
from platform_engineering_assistant.errors import CorpusError, CorpusRule


def entry(path: str, doc_id: str = "d1") -> ManifestDocument:
    return ManifestDocument(doc_id=doc_id, path=path, title="Doc", authority=Authority.ADR)


# --- one test per containment rule ------------------------------------------


def test_absolute_path_is_rejected(fake_repo: Path) -> None:
    with pytest.raises(CorpusError) as caught:
        resolve_document_path("/etc/passwd.md", fake_repo)
    assert caught.value.rule is CorpusRule.ABSOLUTE_PATH


def test_parent_traversal_segment_is_rejected(fake_repo: Path) -> None:
    with pytest.raises(CorpusError) as caught:
        resolve_document_path("docs/../../outside.md", fake_repo)
    assert caught.value.rule is CorpusRule.PARENT_TRAVERSAL


def test_leading_parent_traversal_is_rejected(fake_repo: Path) -> None:
    with pytest.raises(CorpusError) as caught:
        resolve_document_path("../secrets.md", fake_repo)
    assert caught.value.rule is CorpusRule.PARENT_TRAVERSAL


def test_non_markdown_extension_is_rejected(fake_repo: Path) -> None:
    (fake_repo / "docs" / "config.yaml").write_text("a: 1\n")
    with pytest.raises(CorpusError) as caught:
        resolve_document_path("docs/config.yaml", fake_repo)
    assert caught.value.rule is CorpusRule.NOT_MARKDOWN


def test_symlink_is_rejected_even_when_it_points_inside_the_repository(
    fake_repo: Path,
) -> None:
    """resolve() would follow it, so the link must be caught before resolving.

    A link is an indirection the manifest never approved: the reviewed path and
    the file actually read would be different things.
    """
    link = fake_repo / "docs" / "link.md"
    link.symlink_to(fake_repo / "docs" / "good.md")
    with pytest.raises(CorpusError) as caught:
        resolve_document_path("docs/link.md", fake_repo)
    assert caught.value.rule is CorpusRule.SYMLINK


def test_symlink_escaping_the_repository_is_rejected(fake_repo: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text("# Outside\n")
    link = fake_repo / "docs" / "escape.md"
    link.symlink_to(outside)
    with pytest.raises(CorpusError) as caught:
        resolve_document_path("docs/escape.md", fake_repo)
    assert caught.value.rule is CorpusRule.SYMLINK


def test_directory_is_rejected_as_a_non_regular_file(fake_repo: Path) -> None:
    (fake_repo / "docs" / "folder.md").mkdir()
    with pytest.raises(CorpusError) as caught:
        resolve_document_path("docs/folder.md", fake_repo)
    assert caught.value.rule is CorpusRule.NOT_REGULAR_FILE


def test_missing_file_is_rejected(fake_repo: Path) -> None:
    with pytest.raises(CorpusError) as caught:
        resolve_document_path("docs/absent.md", fake_repo)
    assert caught.value.rule is CorpusRule.MISSING


def test_path_outside_the_repository_root_is_rejected(fake_repo: Path, tmp_path: Path) -> None:
    """Containment holds even when no '..' segment is present."""
    sibling = tmp_path / "sibling"
    sibling.mkdir(exist_ok=True)
    (sibling / "doc.md").write_text("# Doc\n")
    nested_root = fake_repo / "nested"
    nested_root.mkdir()
    with pytest.raises(CorpusError):
        resolve_document_path("nested/../../sibling/doc.md", fake_repo)


def test_a_valid_document_resolves(fake_repo: Path) -> None:
    resolved = resolve_document_path("docs/good.md", fake_repo)
    assert resolved.is_file()
    assert resolved.is_relative_to(fake_repo)


def test_duplicate_resolved_paths_are_rejected(fake_repo: Path) -> None:
    """Two textually different entries naming one file must not both load."""
    manifest = CorpusManifest(
        corpus_version=1,
        documents=[entry("docs/good.md", "a"), entry("docs/./good.md", "b")],
    )
    with pytest.raises(CorpusError) as caught:
        load_corpus(manifest, fake_repo)
    assert caught.value.rule is CorpusRule.DUPLICATE_PATH


# --- credential safety ------------------------------------------------------
#
# All synthetic. Constructed inline so no committed line is a plausible secret.

# Assembled at runtime rather than written as one literal. The repository's
# detect-private-key pre-commit hook scans file CONTENT, and a contiguous PEM
# header would trip it — correctly. Splitting the string keeps that repo-wide
# control intact while still giving the scanner the exact bytes to match.
SYNTHETIC_PRIVATE_KEY = "-----BEGIN " + "RSA PRIVATE" + " KEY-----"
SYNTHETIC_BEARER = "Authorization: Bearer " + "A" * 40
SYNTHETIC_JWT = "eyJhbGciOiJIUzI1NiJ9." + "b" * 20 + "." + "c" * 20
SYNTHETIC_API_KEY = 'api_key = "' + "k" * 32 + '"'
SYNTHETIC_CLIENT_SECRET = "client_secret: " + "s" * 40
SYNTHETIC_CONNECTION_STRING = (
    "DefaultEndpointsProtocol=https;AccountName=acct;AccountKey=" + "K" * 44 + ";"
)


@pytest.mark.parametrize(
    "payload",
    [
        SYNTHETIC_PRIVATE_KEY,
        SYNTHETIC_BEARER,
        SYNTHETIC_JWT,
        SYNTHETIC_API_KEY,
        SYNTHETIC_CLIENT_SECRET,
        SYNTHETIC_CONNECTION_STRING,
    ],
)
def test_credential_material_is_detected(payload: str) -> None:
    finding = scan_for_credential_material(f"# Doc\n\nSome text.\n{payload}\n")
    assert finding is not None
    description, line_number = finding
    assert line_number == 4
    assert description


def test_document_with_credential_material_is_rejected(fake_repo: Path) -> None:
    (fake_repo / "docs" / "leak.md").write_text(f"# Leak\n\n{SYNTHETIC_API_KEY}\n")
    with pytest.raises(CorpusError) as caught:
        read_document(entry("docs/leak.md"), fake_repo)
    assert caught.value.rule is CorpusRule.CREDENTIAL_MATERIAL


def test_the_secret_value_never_appears_in_the_exception(fake_repo: Path) -> None:
    """An error that quoted the secret would publish what it was protecting."""
    secret_value = "q" * 48
    (fake_repo / "docs" / "leak.md").write_text(f'# Leak\n\napi_key = "{secret_value}"\n')
    with pytest.raises(CorpusError) as caught:
        read_document(entry("docs/leak.md"), fake_repo)

    rendered = str(caught.value) + repr(caught.value)
    assert secret_value not in rendered
    # It reports the rule and the location instead.
    assert "line 3" in rendered
    assert "docs/leak.md" in rendered


def test_scanner_returns_a_description_and_location_not_the_value() -> None:
    secret_value = "z" * 50
    finding = scan_for_credential_material(f'client_secret = "{secret_value}"')
    assert finding is not None
    assert secret_value not in finding[0]


# --- the scanner must not fire on ordinary documentation ---------------------


@pytest.mark.parametrize(
    "prose",
    [
        "Do not create client secrets unless explicitly approved.",
        "Use workload identity federation; no PAT, client secret or storage key.",
        "The storage account is stexamplesandboxdata in rg-aiplatform-sandbox.",
        "Authenticate with a bearer token obtained from DefaultAzureCredential.",
        "Set the api key policy to disabled (local_auth_enabled = false).",
        "The Key Vault is kv-aiplatform-sandbox with RBAC authorization enabled.",
        'subscription_id = "00000000-0000-0000-0000-000000000000"',
        "Endpoint: https://<subdomain>.openai.azure.com/openai/v1/",
    ],
)
def test_ordinary_documentation_is_not_flagged(prose: str) -> None:
    """The corpus IS security documentation; prose mentions must not match."""
    assert scan_for_credential_material(prose) is None


def test_the_entire_real_corpus_passes_the_safety_scanner() -> None:
    """The strongest false-positive test available: the actual documents."""
    documents = load_corpus(load_manifest())
    assert len(documents) == 15


def test_loading_the_real_corpus_returns_text_in_manifest_order() -> None:
    manifest = load_manifest()
    documents = load_corpus(manifest)
    assert [d.doc_id for d in documents] == [d.doc_id for d in manifest.documents]
    assert all(document.text.strip() for document in documents)


# --- repository root --------------------------------------------------------


def test_repository_root_is_derived_from_the_git_directory(repo_root: Path) -> None:
    """Derived, never configured: an env var must not widen containment."""
    assert (repo_root / ".git").exists()
    assert (repo_root / "products" / "platform-engineering-assistant").is_dir()


def test_repository_root_fails_loudly_when_no_git_directory_exists(tmp_path: Path) -> None:
    with pytest.raises(CorpusError) as caught:
        repository_root(tmp_path / "nowhere")
    assert caught.value.rule is CorpusRule.OUTSIDE_REPOSITORY


# --- read-only --------------------------------------------------------------


def test_loading_the_corpus_writes_nothing(fake_repo: Path) -> None:
    before = {p: p.stat().st_mtime_ns for p in fake_repo.rglob("*") if p.is_file()}
    manifest = CorpusManifest(corpus_version=1, documents=[entry("docs/good.md")])
    load_corpus(manifest, fake_repo)
    after = {p: p.stat().st_mtime_ns for p in fake_repo.rglob("*") if p.is_file()}
    assert before == after


# --- placeholder handling applies to the VALUE, not the line (17.1b fix) -----
#
# The 17.1a scanner skipped a whole line containing "example" or "placeholder",
# so a trailing comment disarmed the check for the assignment beside it. All
# secrets below are synthetic.

REAL_LOOKING_SECRET = "a-real-plausible-secret-value-0123"


@pytest.mark.parametrize(
    "comment",
    ["# example", "# placeholder", "# redacted", "# see example above", "<!-- example -->"],
)
def test_a_real_secret_is_rejected_despite_a_placeholder_comment(comment: str) -> None:
    """A comment beside a live credential must not disarm the guard."""
    line = f'client_secret = "{REAL_LOOKING_SECRET}"  {comment}'
    finding = scan_for_credential_material(line)
    assert finding is not None, f"placeholder comment '{comment}' bypassed the scanner"


def test_a_real_api_key_is_rejected_despite_a_placeholder_comment() -> None:
    line = 'api_key = "AbCdEf0123456789AbCdEf0123456789"  # example'
    assert scan_for_credential_material(line) is not None


@pytest.mark.parametrize(
    "placeholder_line",
    [
        "client_secret = <your-client-secret>",
        'api_key = "xxxxxxxxxxxxxxxxxxxxxxxx"',
        'client_secret = "REDACTED-REDACTED-REDACTED"',
        'subscription_id = "00000000-0000-0000-0000-000000000000"',
        'access_key = "<REPLACE-WITH-YOUR-KEY>"',
        'api_key = "example-key-value-not-real-0000"',
    ],
)
def test_documentation_placeholder_values_remain_accepted(placeholder_line: str) -> None:
    """Recognition now applies to the captured value, which is what makes this safe."""
    assert scan_for_credential_material(placeholder_line) is None


def test_placeholder_comment_case_does_not_leak_the_secret(fake_repo: Path) -> None:
    """The corrected path must keep the same redaction guarantee."""
    (fake_repo / "docs" / "leak.md").write_text(
        f'# Leak\n\nclient_secret = "{REAL_LOOKING_SECRET}"  # example\n'
    )
    with pytest.raises(CorpusError) as caught:
        read_document(entry("docs/leak.md"), fake_repo)

    rendered = str(caught.value) + repr(caught.value)
    assert REAL_LOOKING_SECRET not in rendered
    assert caught.value.rule is CorpusRule.CREDENTIAL_MATERIAL


def test_scanner_reports_the_first_finding_only() -> None:
    text = f'api_key = "{REAL_LOOKING_SECRET}"\nclient_secret = "{REAL_LOOKING_SECRET}"\n'
    finding = scan_for_credential_material(text)
    assert finding is not None
    assert finding[1] == 1


# --- placeholder recognition is positive identification (17.1b hardening) ----
#
# The earlier version excused any value CONTAINING "<". Recognition must now
# match a known template shape end to end.

SECRET_WITH_ANGLE_BRACKET = "aB3xK9<mQ2vL7pZzR4tW8"


@pytest.mark.parametrize(
    "line",
    [
        f'client_secret = "{SECRET_WITH_ANGLE_BRACKET}"',
        'api_key = "Xy7<Qm2>Lp9Zr4Tw8Vb3Nk6"',
        'client_secret = "prefix<suffix>0123456789abcd"',
    ],
)
def test_a_plausible_secret_containing_angle_brackets_is_rejected(line: str) -> None:
    """Containing '<' proves nothing; it must MATCH a template shape."""
    assert scan_for_credential_material(line) is not None


@pytest.mark.parametrize(
    "line",
    [
        "client_secret = <your-client-secret>",
        "api_key = <your-key>",
        "access_key = <replace-with-your-key>",
        "api_key = <subdomain>",
        'client_secret = "<YOUR_CLIENT_SECRET>"',
    ],
)
def test_recognised_placeholder_templates_remain_accepted(line: str) -> None:
    assert scan_for_credential_material(line) is None


def test_angle_bracket_secret_value_never_appears_in_the_exception(fake_repo: Path) -> None:
    (fake_repo / "docs" / "leak.md").write_text(
        f'# Leak\n\nclient_secret = "{SECRET_WITH_ANGLE_BRACKET}"\n'
    )
    with pytest.raises(CorpusError) as caught:
        read_document(entry("docs/leak.md"), fake_repo)
    assert SECRET_WITH_ANGLE_BRACKET not in (str(caught.value) + repr(caught.value))
