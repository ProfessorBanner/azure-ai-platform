"""Prompt versioning/hashing and Azure provider configuration."""

from __future__ import annotations

from pathlib import Path

import pytest

from platform_engineering_assistant.config import (
    load_generation_config,
    load_retrieval_config,
)
from platform_engineering_assistant.errors import ConfigurationError
from platform_engineering_assistant.prompts import load_prompt
from platform_engineering_assistant.provider_config import (
    DEFAULT_AUTH_SCOPE,
    DEFAULT_TIMEOUT_SECONDS,
    FORBIDDEN_KEY_VARS,
    PROJECT_ENDPOINT_VAR,
    load_provider_config,
    validate_endpoint,
)

VALID_ENV = {
    "AZURE_OPENAI_ENDPOINT": "https://example-resource.openai.azure.com/openai/v1/",
    "AZURE_OPENAI_DEPLOYMENT": "gpt-4-1-mini",
}


# --- prompt versioning ------------------------------------------------------


def test_the_shipped_prompt_loads_with_a_version_and_a_hash() -> None:
    prompt = load_prompt(load_generation_config())
    assert prompt.version == "answer_v1"
    assert len(prompt.content_hash) == 64
    assert len(prompt.short_hash) == 12


def test_the_prompt_hash_changes_when_the_content_changes(tmp_path: Path) -> None:
    """A version bump can be forgotten; a hash cannot."""
    config = load_generation_config()
    original = load_prompt(config)

    edited = tmp_path / config.prompt_file
    edited.write_text(original.text + "\n\nAn additional sentence.\n")
    changed = load_prompt(config, tmp_path)

    assert changed.version == original.version
    assert changed.content_hash != original.content_hash


@pytest.mark.parametrize(
    "requirement",
    [
        "untrusted",
        "instructions",
        "general knowledge",
        "chunk identifier",
        "Refuse",
        "authority",
        "conflict",
        "reveal",
    ],
)
def test_the_prompt_states_each_required_rule(requirement: str) -> None:
    text = load_prompt(load_generation_config()).text.lower()
    assert requirement.lower() in text


def test_a_prompt_without_a_version_is_rejected(tmp_path: Path) -> None:
    config = load_generation_config()
    (tmp_path / config.prompt_file).write_text("x" * 500)
    with pytest.raises(ConfigurationError):
        load_prompt(config, tmp_path)


def test_a_missing_prompt_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError):
        load_prompt(load_generation_config(), tmp_path)


def test_an_implausibly_short_prompt_is_rejected(tmp_path: Path) -> None:
    config = load_generation_config()
    (tmp_path / config.prompt_file).write_text("prompt_version: answer_v1\n")
    with pytest.raises(ConfigurationError):
        load_prompt(config, tmp_path)


# --- generation configuration ----------------------------------------------


def test_the_shipped_generation_configuration_loads() -> None:
    config = load_generation_config()
    assert config.version == "generation_v1"
    assert config.context_budget_chars > 0
    assert config.prompt_file.endswith(".md")


def test_a_prompt_file_containing_a_path_is_rejected(tmp_path: Path) -> None:
    """The prompt is server-owned; a path would point the app outside prompts/."""
    import json

    path = tmp_path / "generation.json"
    path.write_text(
        json.dumps(
            {
                "version": "generation_v1",
                "prompt_file": "../../../etc/passwd.md",
                "context_budget_chars": 12000,
                "max_answer_chars": 4000,
            }
        )
    )
    with pytest.raises(ConfigurationError):
        load_generation_config(path)


def test_retrieval_and_generation_configuration_are_separate_versions() -> None:
    assert load_retrieval_config().version.startswith("retrieval_v")
    assert load_generation_config().version.startswith("generation_v")


# --- provider configuration -------------------------------------------------


def test_valid_provider_configuration_loads() -> None:
    config = load_provider_config(dict(VALID_ENV))
    assert config.deployment == "gpt-4-1-mini"
    assert config.auth_scope == DEFAULT_AUTH_SCOPE
    assert config.timeout_seconds == DEFAULT_TIMEOUT_SECONDS


def test_the_default_auth_scope_is_the_proven_contract() -> None:
    """Stated explicitly; there is no automatic audience fallback anywhere."""
    assert DEFAULT_AUTH_SCOPE == "https://ai.azure.com/.default"


def test_the_auth_scope_is_overridable_for_diagnostics() -> None:
    config = load_provider_config(
        {**VALID_ENV, "AZURE_OPENAI_AUTH_SCOPE": "https://cognitiveservices.azure.com/.default"}
    )
    assert config.auth_scope == "https://cognitiveservices.azure.com/.default"


@pytest.mark.parametrize("key_var", FORBIDDEN_KEY_VARS)
def test_any_configured_api_key_is_rejected(key_var: str) -> None:
    with pytest.raises(ConfigurationError) as caught:
        load_provider_config({**VALID_ENV, key_var: "a-real-looking-key-value-0123"})
    assert key_var in str(caught.value)


@pytest.mark.parametrize("key_var", FORBIDDEN_KEY_VARS)
def test_the_key_value_is_never_echoed(key_var: str) -> None:
    secret = "sk-do-not-leak-this-value-please"
    with pytest.raises(ConfigurationError) as caught:
        load_provider_config({**VALID_ENV, key_var: secret})
    assert secret not in str(caught.value)


def test_the_generic_account_endpoint_is_rejected() -> None:
    with pytest.raises(ConfigurationError) as caught:
        load_provider_config(
            {**VALID_ENV, "AZURE_OPENAI_ENDPOINT": "https://x.cognitiveservices.azure.com/"}
        )
    message = str(caught.value)
    # Names the generic host (a constant, not the tenant's subdomain) and says
    # what to use instead, so the error is actionable without leaking anything.
    assert "cognitiveservices.azure.com" in message
    assert "/openai/v1/" in message


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://x.openai.azure.com/",
        "https://x.openai.azure.com/openai/",
        "https://x.openai.azure.com/openai/v1",
        "http://x.openai.azure.com/openai/v1/",
    ],
)
def test_a_non_canonical_endpoint_is_rejected(endpoint: str) -> None:
    with pytest.raises(ConfigurationError):
        validate_endpoint(endpoint)


def test_endpoint_errors_do_not_echo_the_value() -> None:
    private = "https://very-private-name.cognitiveservices.azure.com/"
    with pytest.raises(ConfigurationError) as caught:
        validate_endpoint(private)
    assert "very-private-name" not in str(caught.value)


def test_a_missing_deployment_is_rejected() -> None:
    with pytest.raises(ConfigurationError):
        load_provider_config({"AZURE_OPENAI_ENDPOINT": VALID_ENV["AZURE_OPENAI_ENDPOINT"]})


@pytest.mark.parametrize("timeout", ["nonsense", "0", "-5"])
def test_an_invalid_timeout_is_rejected(timeout: str) -> None:
    with pytest.raises(ConfigurationError):
        load_provider_config({**VALID_ENV, "AZURE_OPENAI_TIMEOUT_SECONDS": timeout})


def test_the_project_endpoint_name_is_reserved_and_unread() -> None:
    config = load_provider_config(
        {**VALID_ENV, PROJECT_ENDPOINT_VAR: "https://x.services.ai.azure.com/api/projects/p"}
    )
    assert config.endpoint == VALID_ENV["AZURE_OPENAI_ENDPOINT"]
