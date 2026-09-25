"""Deterministic tokenisation, exercised with real platform terminology."""

from __future__ import annotations

import pytest

from platform_engineering_assistant.retrieval.tokenize import tokenize


def test_lowercases() -> None:
    assert tokenize("Terraform AZURE Databricks") == ["terraform", "azure", "databricks"]


def test_drops_empty_tokens() -> None:
    assert tokenize("  ---  ///  ") == []
    assert tokenize("") == []


def test_preserves_order_and_duplicates() -> None:
    """Term frequency is counted from this, so duplicates must survive."""
    assert tokenize("state state backend") == ["state", "state", "backend"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("gpt-4-1-mini", ["gpt", "4", "1", "mini"]),
        ("gpt-4.1-mini", ["gpt", "4", "1", "mini"]),
        ("dev/stg/prod", ["dev", "stg", "prod"]),
        ("/openai/v1/", ["openai", "v1"]),
        ("azurerm_cognitive_account", ["azurerm", "cognitive", "account"]),
        ("azurerm_cognitive_deployment", ["azurerm", "cognitive", "deployment"]),
        ("rg-aiplatform-sandbox", ["rg", "aiplatform", "sandbox"]),
        ("platform/dev.tfstate", ["platform", "dev", "tfstate"]),
        ("Microsoft.CognitiveServices", ["microsoft", "cognitiveservices"]),
        ("sc-azure-terraform-apply-prod", ["sc", "azure", "terraform", "apply", "prod"]),
        ("kv-aiplatform-sandbox", ["kv", "aiplatform", "sandbox"]),
        ("10.10.0.0/24", ["10", "10", "0", "0", "24"]),
    ],
)
def test_platform_identifiers_split_on_non_alphanumeric(text: str, expected: list[str]) -> None:
    assert tokenize(text) == expected


def test_two_spellings_of_the_model_name_tokenise_identically() -> None:
    """A query for gpt-4-1-mini must match a chunk saying gpt-4.1-mini."""
    assert tokenize("gpt-4-1-mini") == tokenize("gpt-4.1-mini")


def test_environment_list_is_searchable_by_a_single_environment() -> None:
    assert "stg" in tokenize("the dev/stg/prod promotion chain")


def test_no_stemming_is_applied() -> None:
    """Deliberate: stemming is a second source of behaviour to pin and explain."""
    assert tokenize("environments") == ["environments"]
    assert tokenize("environment") != tokenize("environments")


def test_is_deterministic_across_calls() -> None:
    text = "Terraform state is stored in platform/dev.tfstate for the dev environment."
    assert tokenize(text) == tokenize(text)


def test_non_ascii_characters_are_separators_not_tokens() -> None:
    """ASCII-only classes keep behaviour independent of locale and Unicode flags."""
    assert tokenize("dev — stg — prod") == ["dev", "stg", "prod"]
    assert tokenize("café") == ["caf"]


def test_digits_are_retained_as_tokens() -> None:
    assert tokenize("ADR 0005 and phase 17") == ["adr", "0005", "and", "phase", "17"]
