"""Reading approved documents, safely.

Two independent jobs, both of which must pass before a document is admitted:

  CONTAINMENT   a manifest path must name a Markdown file inside the repository
                and nothing else. Every rule below is enforced separately and
                has its own error, because the remedies differ: an absolute path
                is an authoring slip, a `..` segment is a mistake worth
                understanding, and a symlink is a containment breach.

  SAFETY        no document may carry credential material. The corpus is public
                repository documentation and should contain none, so a match is
                a finding worth stopping for rather than filtering out.

READ ONLY. Nothing in this module writes, creates, moves or deletes anything.

REDACTION. A safety finding reports the rule, the path and the line number, and
NEVER the matching text. An error that quoted the secret it found would publish
the secret it was protecting — and error strings end up in logs, tickets and
terminal scrollback.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from platform_engineering_assistant.corpus.manifest import CorpusManifest, ManifestDocument
from platform_engineering_assistant.errors import CorpusError, CorpusRule

MARKDOWN_SUFFIX = ".md"


@dataclass(frozen=True, slots=True)
class LoadedDocument:
    """An admitted document and its text."""

    doc_id: str
    path: str
    title: str
    authority: str
    text: str


def repository_root(start: Path | None = None) -> Path:
    """Locate the repository root by walking up to the directory holding `.git`.

    Derived rather than configured so the containment boundary cannot be widened
    by an environment variable. If `.git` is ever absent (a source export, say),
    that is a hard failure rather than a silent fallback to `/`.
    """
    current = (start or Path(__file__)).resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    raise CorpusError(
        CorpusRule.OUTSIDE_REPOSITORY,
        "Repository root could not be located: no .git directory above this module.",
    )


# --- credential material ----------------------------------------------------
#
# Every pattern requires an actual VALUE, not a mention. The corpus is security
# documentation: it says things like "no PAT, client secret or storage key" and
# "do not create client secrets unless explicitly approved" in ordinary prose,
# and a scanner that fired on the word "secret" would reject the very documents
# it is meant to protect. Matching therefore demands an assignment or a
# structural marker plus a plausible secret-length value.
_CREDENTIAL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # PEM blocks are unambiguous: no prose contains one.
    (
        "private-key block",
        re.compile(r"(?P<value>-----BEGIN [A-Z ]*PRIVATE KEY-----)"),
    ),
    # A bearer token literal: the scheme followed by a long opaque value.
    # "bearer token" as prose has no such value and does not match.
    (
        "bearer-token literal",
        re.compile(r"\bBearer\s+(?P<value>[A-Za-z0-9\-._~+/<>]{16,}={0,2})"),
    ),
    # JWTs are self-identifying by their header segment.
    (
        "JSON Web Token",
        re.compile(r"(?P<value>\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})"),
    ),
    # key = "value" / key: value, with a value long enough to be real.
    (
        "API key assignment",
        re.compile(
            r"\b(?:api[_-]?key|apikey|subscription[_-]?key|access[_-]?key)\b\s*[=:]\s*"
            r"[\"']?(?P<value>[A-Za-z0-9/+_\-<>.]{16,})[\"']?"
        ),
    ),
    (
        "client-secret assignment",
        re.compile(
            r"\b(?:client[_-]?secret|clientsecret)\b\s*[=:]\s*"
            r"[\"']?(?P<value>[A-Za-z0-9~._\-<>]{16,})[\"']?"
        ),
    ),
    # Credential-bearing connection strings, matched on the credential field.
    (
        "credential-bearing connection string",
        re.compile(
            r"\b(?:AccountKey|SharedAccessSignature|Password|Pwd)\s*=\s*"
            r"(?P<value>[^;\s\"']{12,})"
        ),
    ),
)

# Patterns that identify a CANDIDATE VALUE as a documentation placeholder.
#
# Each pattern is ANCHORED and matches the WHOLE value. That is the point of
# this design. An earlier version tested for substrings, which meant any value
# merely CONTAINING "<" was excused — so a real credential that happened to
# include an angle bracket walked straight past the guard. Recognition must be
# positive identification of a known template shape, never the presence of a
# suspicious character.
#
# Matched against the extracted value only, never the surrounding line: a
# trailing "# example" comment must not disarm the check for the assignment
# beside it.
_PLACEHOLDER_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Angle-bracket templates: <subdomain>, <your-client-secret>, <YOUR_KEY>,
    # <replace-with-your-key>. The INSIDE must itself look like a template
    # word-list, so "<" alone proves nothing.
    re.compile(r"^<[a-z0-9]+(?:[-_.][a-z0-9]+)*>$"),
    # Repeated-character masking: xxxxxxxx, ********, ........
    re.compile(r"^(?:x{4,}|\*{4,}|\.{4,})$"),
    # Zero-filled identifiers, including the all-zero GUID.
    re.compile(r"^[0-]+$"),
    # A hyphen/underscore word-list containing at least one placeholder word:
    # redacted, example-key-value-not-real-0000, your-client-secret,
    # changeme-please, dummy_value.
    re.compile(
        r"^(?:[a-z0-9]+[-_])*"
        r"(?:redacted|example|placeholder|dummy|sample|changeme|todo|your|fake|notreal)"
        r"(?:[-_][a-z0-9]+)*$"
    ),
)


def _is_placeholder_value(value: str) -> bool:
    """True only when a captured candidate matches a known template shape.

    Positive identification, not heuristic suspicion: anything this does not
    recognise is treated as a real credential, which is the safe default for a
    tripwire.
    """
    candidate = value.strip().strip("\"'").lower()
    if not candidate:
        return True
    return any(pattern.fullmatch(candidate) for pattern in _PLACEHOLDER_VALUE_PATTERNS)


def scan_for_credential_material(text: str) -> tuple[str, int] | None:
    """Return (description, line_number) for the first credential match, else None.

    Deliberately returns a DESCRIPTION and a LOCATION, never the matched text.
    Callers cannot leak what they are not given.
    """
    for line_number, line in enumerate(text.splitlines(), start=1):
        for description, pattern in _CREDENTIAL_PATTERNS:
            for match in pattern.finditer(line):
                if _is_placeholder_value(match.group("value")):
                    continue
                return description, line_number
    return None


# --- containment ------------------------------------------------------------


def resolve_document_path(relative_path: str, root: Path) -> Path:
    """Validate a manifest path and return its resolved location.

    Each rule is checked separately, in the order a reader would reason about
    them, so a failure names one cause rather than a compound one.

    Raises:
        CorpusError: with the specific CorpusRule that failed.
    """
    candidate = Path(relative_path)

    if candidate.is_absolute():
        raise CorpusError(
            CorpusRule.ABSOLUTE_PATH,
            "Corpus paths must be repository-relative, not absolute.",
            path=relative_path,
        )

    if ".." in candidate.parts:
        raise CorpusError(
            CorpusRule.PARENT_TRAVERSAL,
            "Corpus paths must not contain a '..' segment.",
            path=relative_path,
        )

    if candidate.suffix != MARKDOWN_SUFFIX:
        raise CorpusError(
            CorpusRule.NOT_MARKDOWN,
            f"Corpus documents must be Markdown ('{MARKDOWN_SUFFIX}').",
            path=relative_path,
        )

    target = root / candidate

    # A symlink is checked BEFORE resolve(): resolve() follows the link, so a
    # link pointing inside the repository would otherwise pass containment while
    # still being an indirection the manifest never approved.
    if target.is_symlink():
        raise CorpusError(
            CorpusRule.SYMLINK,
            "Corpus documents must not be symlinks; a link is an unapproved indirection.",
            path=relative_path,
        )

    if not target.exists():
        raise CorpusError(
            CorpusRule.MISSING,
            "Corpus document does not exist.",
            path=relative_path,
        )

    resolved = target.resolve()

    if not resolved.is_relative_to(root.resolve()):
        raise CorpusError(
            CorpusRule.OUTSIDE_REPOSITORY,
            "Corpus document resolves outside the repository root.",
            path=relative_path,
        )

    if not resolved.is_file():
        raise CorpusError(
            CorpusRule.NOT_REGULAR_FILE,
            "Corpus documents must be regular files.",
            path=relative_path,
        )

    return resolved


def read_document(document: ManifestDocument, root: Path) -> LoadedDocument:
    """Validate, read and safety-scan one approved document.

    Raises:
        CorpusError: on any containment or safety failure.
    """
    resolved = resolve_document_path(document.path, root)

    try:
        text = resolved.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise CorpusError(
            CorpusRule.UNREADABLE,
            "Corpus document could not be read as UTF-8 text.",
            path=document.path,
        ) from exc

    finding = scan_for_credential_material(text)
    if finding is not None:
        description, line_number = finding
        raise CorpusError(
            CorpusRule.CREDENTIAL_MATERIAL,
            f"Corpus document appears to contain credential material "
            f"({description}) at line {line_number}. The value is deliberately "
            f"not reported. Remove it from the document before approving it.",
            path=document.path,
        )

    return LoadedDocument(
        doc_id=document.doc_id,
        path=document.path,
        title=document.title,
        authority=str(document.authority),
        text=text,
    )


def load_corpus(manifest: CorpusManifest, root: Path | None = None) -> list[LoadedDocument]:
    """Load every approved document, in manifest order.

    Manifest order is preserved so downstream chunk ordinals are deterministic.

    Raises:
        CorpusError: on the first document that fails any rule. Loading is
            all-or-nothing: a corpus missing a document it claims to have would
            answer from a silently smaller evidence set.
    """
    repository = repository_root() if root is None else root

    documents: list[LoadedDocument] = []
    seen_resolved: dict[Path, str] = {}

    for entry in manifest.documents:
        loaded = read_document(entry, repository)

        # Two manifest entries may differ textually and still name one file.
        resolved = (repository / entry.path).resolve()
        previous = seen_resolved.get(resolved)
        if previous is not None:
            raise CorpusError(
                CorpusRule.DUPLICATE_PATH,
                f"Two manifest entries resolve to the same file (also '{previous}').",
                path=entry.path,
            )
        seen_resolved[resolved] = entry.path

        documents.append(loaded)

    return documents
