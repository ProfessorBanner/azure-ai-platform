"""Server-owned, versioned prompt loading.

The prompt is part of the trusted computing base: it is what tells the model
that the question and the evidence are data rather than instructions. It is
therefore loaded from a reviewed file inside `prompts/`, named by server-owned
configuration, and never selected, overridden or inspected by a caller.

Its content hash travels with every response and telemetry record. A version
identifier alone is not enough — an edit that forgets to bump the version would
otherwise be invisible, and "which instructions produced this answer" is exactly
the question an incident asks.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from platform_engineering_assistant.config import PROMPTS_DIR, GenerationConfig
from platform_engineering_assistant.errors import ConfigurationError

_VERSION_PATTERN = re.compile(r"^prompt_version:\s*(?P<version>[a-z0-9_]+)\s*$", re.MULTILINE)

MINIMUM_PROMPT_CHARS = 200


@dataclass(frozen=True, slots=True)
class LoadedPrompt:
    """A reviewed prompt, its declared version and the hash of its bytes."""

    version: str
    content_hash: str
    text: str

    @property
    def short_hash(self) -> str:
        """First 12 hex characters — enough to correlate, short enough to log."""
        return self.content_hash[:12]


def load_prompt(config: GenerationConfig, prompts_dir: Path | None = None) -> LoadedPrompt:
    """Load the answering prompt named by the generation configuration.

    Raises:
        ConfigurationError: if the file is missing, unreadable, implausibly
            short, or does not declare a `prompt_version` in its front matter.
    """
    return load_named_prompt(config.prompt_file, prompts_dir)


def load_named_prompt(file_name: str, prompts_dir: Path | None = None) -> LoadedPrompt:
    """Load any reviewed prompt from `prompts/` by bare file name.

    Extracted from `load_prompt` in Phase 17.2 so that the evaluation judge's
    rubric is loaded, versioned and hashed by exactly the same code as the
    answering prompt. A second, slightly different loader would eventually drift,
    and "which rubric produced this score" is the same question as "which
    instructions produced this answer".

    The caller supplies a bare file name, never a path: the containment check
    below is what keeps an arbitrary file from becoming a system prompt.

    Raises:
        ConfigurationError: if the name is not a bare Markdown file name, or the
            file is missing, unreadable, implausibly short, or does not declare a
            `prompt_version` in its front matter.
    """
    if not file_name.endswith(".md"):
        raise ConfigurationError("A prompt file name must name a Markdown file.")
    if "/" in file_name or "\\" in file_name or ".." in file_name:
        raise ConfigurationError("A prompt file name must be a bare name inside prompts/.")

    directory = PROMPTS_DIR if prompts_dir is None else prompts_dir
    path = directory / file_name

    # Defence in depth: the configuration loader already rejects a path, but the
    # containment check is cheap and the consequence of getting it wrong is that
    # an arbitrary file becomes the system prompt.
    resolved = path.resolve()
    if not resolved.is_relative_to(directory.resolve()):
        raise ConfigurationError("prompt_file resolves outside the prompts directory.")

    try:
        text = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigurationError(f"Prompt could not be read: {file_name}") from exc

    if len(text) < MINIMUM_PROMPT_CHARS:
        raise ConfigurationError(f"{file_name} is too short to be the reviewed system prompt.")

    match = _VERSION_PATTERN.search(text)
    if match is None:
        raise ConfigurationError(
            f"{file_name} does not declare a prompt_version in its front matter."
        )

    return LoadedPrompt(
        version=match.group("version"),
        content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        text=text,
    )
