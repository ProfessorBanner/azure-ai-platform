"""Versioned, server-owned configuration.

Retrieval parameters are loaded from a version-controlled JSON file rather than
hard-coded or read from the environment. Two reasons, and the second is the one
that matters: they decide which evidence an answer may rest on, so a change is a
reviewable diff; and a runtime flag that widened retrieval until an answer
appeared would quietly destroy the grounding guarantee.

Nothing here is a secret, and nothing here comes from a client request.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from platform_engineering_assistant.errors import ConfigurationError

# Package root is src/platform_engineering_assistant/, so the product directory
# is two levels up. Resolved once so callers need not know the layout.
PRODUCT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RETRIEVAL_CONFIG_PATH = PRODUCT_ROOT / "config" / "retrieval_v1.json"
DEFAULT_GENERATION_CONFIG_PATH = PRODUCT_ROOT / "config" / "generation_v1.json"
PROMPTS_DIR = PRODUCT_ROOT / "prompts"

VERSION_PATTERN = re.compile(r"^retrieval_v[1-9][0-9]*$")

# Bounds are guardrails against configuration that would be pointless or
# expensive, not tuning targets.
MIN_TOP_K = 1
MAX_TOP_K = 20
MIN_CHUNK_BUDGET_CHARS = 200
MAX_CHUNK_BUDGET_CHARS = 8000

_REQUIRED_FIELDS = (
    "version",
    "top_k",
    "minimum_score",
    "bm25_k1",
    "bm25_b",
    "chunk_budget_chars",
    "title_weight",
    "heading_weight",
)


@dataclass(frozen=True, slots=True)
class RetrievalConfig:
    """Validated retrieval parameters for one configuration version."""

    version: str
    top_k: int
    minimum_score: float
    bm25_k1: float
    bm25_b: float
    chunk_budget_chars: int
    # Field-aware weights. Zero for both reproduces pure body BM25.
    title_weight: float
    heading_weight: float


def _require_number(payload: dict[str, object], key: str) -> float:
    value = payload[key]
    # bool is a subclass of int; `"top_k": true` must not silently become 1.
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigurationError(f"{key} must be a number.")
    return float(value)


def _require_int(payload: dict[str, object], key: str) -> int:
    value = payload[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigurationError(f"{key} must be an integer.")
    return value


def load_retrieval_config(path: Path | None = None) -> RetrievalConfig:
    """Load and validate a retrieval configuration file.

    Raises:
        ConfigurationError: if the file is missing or malformed, if a required
            field is absent, or if any value falls outside its permitted range.
            Messages name the field and the rule, never the surrounding content.
    """
    config_path = DEFAULT_RETRIEVAL_CONFIG_PATH if path is None else path

    try:
        raw = json.loads(config_path.read_text())
    except OSError as exc:
        raise ConfigurationError(
            f"Retrieval configuration could not be read: {config_path.name}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise ConfigurationError(
            f"{config_path.name} is not valid JSON: {exc.msg} (line {exc.lineno})"
        ) from exc

    if not isinstance(raw, dict):
        raise ConfigurationError(f"{config_path.name} must contain a JSON object.")

    # Keys beginning with `$` are documentation for humans reading the file.
    payload: dict[str, object] = {
        key: value for key, value in raw.items() if not key.startswith("$")
    }

    missing = sorted(field for field in _REQUIRED_FIELDS if field not in payload)
    if missing:
        raise ConfigurationError(
            f"{config_path.name} is missing required field(s): {', '.join(missing)}"
        )

    unexpected = sorted(set(payload) - set(_REQUIRED_FIELDS))
    if unexpected:
        # A closed schema: a typo must be an error, not an ignored key that
        # leaves the intended setting silently at its old value.
        raise ConfigurationError(
            f"{config_path.name} contains unknown field(s): {', '.join(unexpected)}"
        )

    version = payload["version"]
    if not isinstance(version, str) or not VERSION_PATTERN.match(version):
        raise ConfigurationError(
            "version must look like 'retrieval_v<n>' with n >= 1, so a configuration "
            "change is always a new, citable version."
        )

    top_k = _require_int(payload, "top_k")
    if not MIN_TOP_K <= top_k <= MAX_TOP_K:
        raise ConfigurationError(f"top_k must be between {MIN_TOP_K} and {MAX_TOP_K}.")

    minimum_score = _require_number(payload, "minimum_score")
    if minimum_score < 0:
        raise ConfigurationError("minimum_score must not be negative.")

    bm25_k1 = _require_number(payload, "bm25_k1")
    if bm25_k1 <= 0:
        raise ConfigurationError("bm25_k1 must be greater than zero.")

    bm25_b = _require_number(payload, "bm25_b")
    if not 0.0 <= bm25_b <= 1.0:
        raise ConfigurationError("bm25_b must be between 0 and 1 inclusive.")

    chunk_budget_chars = _require_int(payload, "chunk_budget_chars")
    if not MIN_CHUNK_BUDGET_CHARS <= chunk_budget_chars <= MAX_CHUNK_BUDGET_CHARS:
        raise ConfigurationError(
            f"chunk_budget_chars must be between {MIN_CHUNK_BUDGET_CHARS} and "
            f"{MAX_CHUNK_BUDGET_CHARS}."
        )

    title_weight = _require_number(payload, "title_weight")
    if title_weight < 0:
        raise ConfigurationError("title_weight must not be negative.")

    heading_weight = _require_number(payload, "heading_weight")
    if heading_weight < 0:
        raise ConfigurationError("heading_weight must not be negative.")

    return RetrievalConfig(
        version=version,
        top_k=top_k,
        minimum_score=minimum_score,
        bm25_k1=bm25_k1,
        bm25_b=bm25_b,
        chunk_budget_chars=chunk_budget_chars,
        title_weight=title_weight,
        heading_weight=heading_weight,
    )


# --- generation configuration -----------------------------------------------

GENERATION_VERSION_PATTERN = re.compile(r"^generation_v[1-9][0-9]*$")

MIN_CONTEXT_BUDGET_CHARS = 1000
MAX_CONTEXT_BUDGET_CHARS = 200_000
MIN_ANSWER_CHARS = 100
MAX_ANSWER_CHARS = 20_000

_REQUIRED_GENERATION_FIELDS = (
    "version",
    "prompt_file",
    "context_budget_chars",
    "max_answer_chars",
)


@dataclass(frozen=True, slots=True)
class GenerationConfig:
    """Validated, server-owned generation parameters."""

    version: str
    prompt_file: str
    context_budget_chars: int
    max_answer_chars: int


def load_generation_config(path: Path | None = None) -> GenerationConfig:
    """Load and validate the generation configuration.

    Raises:
        ConfigurationError: on a missing or malformed file, an unknown or
            missing field, or a value outside its permitted range.
    """
    config_path = DEFAULT_GENERATION_CONFIG_PATH if path is None else path

    try:
        raw = json.loads(config_path.read_text())
    except OSError as exc:
        raise ConfigurationError(
            f"Generation configuration could not be read: {config_path.name}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise ConfigurationError(
            f"{config_path.name} is not valid JSON: {exc.msg} (line {exc.lineno})"
        ) from exc

    if not isinstance(raw, dict):
        raise ConfigurationError(f"{config_path.name} must contain a JSON object.")

    payload: dict[str, object] = {
        key: value for key, value in raw.items() if not key.startswith("$")
    }

    missing = sorted(field for field in _REQUIRED_GENERATION_FIELDS if field not in payload)
    if missing:
        raise ConfigurationError(
            f"{config_path.name} is missing required field(s): {', '.join(missing)}"
        )
    unexpected = sorted(set(payload) - set(_REQUIRED_GENERATION_FIELDS))
    if unexpected:
        raise ConfigurationError(
            f"{config_path.name} contains unknown field(s): {', '.join(unexpected)}"
        )

    version = payload["version"]
    if not isinstance(version, str) or not GENERATION_VERSION_PATTERN.match(version):
        raise ConfigurationError("version must look like 'generation_v<n>' with n >= 1.")

    prompt_file = payload["prompt_file"]
    if not isinstance(prompt_file, str) or not prompt_file.endswith(".md"):
        raise ConfigurationError("prompt_file must name a Markdown file.")
    if "/" in prompt_file or "\\" in prompt_file or ".." in prompt_file:
        # The prompt is server-owned; a path here would be a way to point the
        # application at a file outside the reviewed prompts directory.
        raise ConfigurationError("prompt_file must be a bare file name inside prompts/.")

    context_budget_chars = _require_int(payload, "context_budget_chars")
    if not MIN_CONTEXT_BUDGET_CHARS <= context_budget_chars <= MAX_CONTEXT_BUDGET_CHARS:
        raise ConfigurationError(
            f"context_budget_chars must be between {MIN_CONTEXT_BUDGET_CHARS} and "
            f"{MAX_CONTEXT_BUDGET_CHARS}."
        )

    max_answer_chars = _require_int(payload, "max_answer_chars")
    if not MIN_ANSWER_CHARS <= max_answer_chars <= MAX_ANSWER_CHARS:
        raise ConfigurationError(
            f"max_answer_chars must be between {MIN_ANSWER_CHARS} and {MAX_ANSWER_CHARS}."
        )

    return GenerationConfig(
        version=version,
        prompt_file=prompt_file,
        context_budget_chars=context_budget_chars,
        max_answer_chars=max_answer_chars,
    )
