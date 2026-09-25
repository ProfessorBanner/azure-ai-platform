"""Version and provenance capture for an evaluation report.

A score without a provenance record is an anecdote. "Groundedness improved" is
only a statement about the product if the corpus, the retrieval configuration,
the prompt, the rubric, the dataset, the policy and the code are all pinned to
identities that can be compared between two runs — which is why every one of
them is hashed here rather than merely named. A version string that someone
forgot to bump is invisible; a content hash is not.

THE DIRTY-TREE RULE
-------------------
A LIVE report produced from a dirty working tree cannot be attributed to any
commit, so nobody can ever reproduce it or use it as a baseline. Such a report is
still written — it is useful to the person who ran it, right now — but it is
marked and its status is forced to INCOMPLETE. It is never allowed to read as a
successful run.

Offline fake runs are not subject to that rule: they measure the working tree on
purpose, which is the whole point of running them during development.
"""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from platform_engineering_assistant.config import PRODUCT_ROOT

GIT_TIMEOUT_SECONDS = 10

UNKNOWN_SHA = "unknown"


def _git(*arguments: str) -> str | None:
    """Run a read-only git command, or return None if git cannot answer.

    Never raises. A missing git, a detached checkout or an export without
    history are all ordinary situations, and an evaluation run must not fail
    because provenance capture was inconvenient — it must record that the
    provenance is unknown, which is a materially different and much louder
    statement than recording nothing.
    """
    try:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=PRODUCT_ROOT,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def git_sha() -> str:
    """The current commit, or `unknown`."""
    return _git("rev-parse", "HEAD") or UNKNOWN_SHA


def working_tree_is_dirty() -> bool:
    """True when tracked files differ from HEAD, or provenance is unknown.

    Failing closed on purpose: if git cannot be consulted, the tree cannot be
    shown to be clean, and treating "we could not check" as "it was clean" is
    how an unattributable report becomes a baseline.
    """
    status = _git("status", "--porcelain")
    if status is None:
        return True
    return bool(status.strip())


def sha256_of(path: Path) -> str:
    """Hash a file's bytes, or return `unknown` if it cannot be read."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return UNKNOWN_SHA


@dataclass(frozen=True, slots=True)
class RunProvenance:
    """Everything needed to say which artefacts produced a number.

    Every field is an identifier, a version, a hash or a count. None of it is
    content, so the whole record is safe to publish alongside a report.
    """

    git_sha: str
    git_dirty: bool

    dataset_id: str
    dataset_version: int
    dataset_sha256: str

    corpus_version: int

    retrieval_config_version: str
    retrieval_config_sha256: str

    prompt_version: str
    prompt_sha256: str

    judge_prompt_version: str | None
    judge_prompt_sha256: str | None

    provider: str
    deployment: str | None
    model: str | None

    evaluation_policy_id: str
    evaluation_policy_version: int
    evaluation_policy_sha256: str

    generated_at: str
    case_count: int
    execution_mode: str
    judge_enabled: bool

    @property
    def unattributable_live_run(self) -> bool:
        """A live run whose result cannot be tied to a commit."""
        return self.execution_mode == "live" and (self.git_dirty or self.git_sha == UNKNOWN_SHA)

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def utc_now_iso() -> str:
    """Timestamp in UTC, to the second. Reports are compared, not raced."""
    return datetime.now(UTC).replace(microsecond=0).isoformat()


__all__ = [
    "RunProvenance",
    "UNKNOWN_SHA",
    "git_sha",
    "sha256_of",
    "utc_now_iso",
    "working_tree_is_dirty",
]
