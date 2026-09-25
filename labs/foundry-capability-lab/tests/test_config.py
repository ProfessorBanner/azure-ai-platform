"""Configuration loading, and the refusal to accept API-key authentication."""

from __future__ import annotations

import pytest

from foundry_capability_lab.config import (
    DEFAULT_AUTH_SCOPE,
    DEFAULT_TIMEOUT_SECONDS,
    DEPLOYMENT_VAR,
    ENDPOINT_VAR,
    FORBIDDEN_KEY_VARS,
    PROJECT_ENDPOINT_VAR,
    SCOPE_VAR,
    TIMEOUT_VAR,
    assert_no_api_key_configured,
    load_config,
    validate_endpoint,
)
from foundry_capability_lab.errors import ConfigurationError, FailureCategory

VALID_ENV = {
    "AZURE_OPENAI_ENDPOINT": "https://example-resource.openai.azure.com/openai/v1/",
    "AZURE_OPENAI_DEPLOYMENT": "gpt-4-1-mini",
}


def test_loads_valid_configuration() -> None:
    config = load_config(dict(VALID_ENV))
    assert config.deployment == "gpt-4-1-mini"
    assert config.timeout_seconds == DEFAULT_TIMEOUT_SECONDS
    assert config.auth_scope == DEFAULT_AUTH_SCOPE


def test_optional_timeout_is_honoured() -> None:
    config = load_config({**VALID_ENV, "AZURE_OPENAI_TIMEOUT_SECONDS": "12.5"})
    assert config.timeout_seconds == 12.5


@pytest.mark.parametrize("bad_timeout", ["nonsense", "0", "-5"])
def test_invalid_timeout_is_rejected(bad_timeout: str) -> None:
    with pytest.raises(ConfigurationError):
        load_config({**VALID_ENV, "AZURE_OPENAI_TIMEOUT_SECONDS": bad_timeout})


@pytest.mark.parametrize("missing", ["AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_DEPLOYMENT"])
def test_missing_required_variable_is_rejected(missing: str) -> None:
    env = dict(VALID_ENV)
    del env[missing]
    with pytest.raises(ConfigurationError) as caught:
        load_config(env)
    assert missing in str(caught.value)


def test_non_https_endpoint_is_rejected() -> None:
    with pytest.raises(ConfigurationError):
        load_config({**VALID_ENV, "AZURE_OPENAI_ENDPOINT": "http://insecure.example/openai/v1/"})


@pytest.mark.parametrize("key_var", FORBIDDEN_KEY_VARS)
def test_any_api_key_variable_is_rejected(key_var: str) -> None:
    """The account disables key auth; a stray key variable must fail loudly."""
    with pytest.raises(ConfigurationError) as caught:
        load_config({**VALID_ENV, key_var: "some-key-value"})
    error = caught.value
    assert error.category is FailureCategory.CONFIGURATION
    assert key_var in str(error)


@pytest.mark.parametrize("key_var", FORBIDDEN_KEY_VARS)
def test_api_key_value_is_never_echoed(key_var: str) -> None:
    """The error names the offending variable but must not reveal its value."""
    secret = "sk-do-not-leak-this-value"
    with pytest.raises(ConfigurationError) as caught:
        assert_no_api_key_configured({**VALID_ENV, key_var: secret})
    assert secret not in str(caught.value)


def test_empty_key_variable_is_not_treated_as_configured() -> None:
    assert_no_api_key_configured({**VALID_ENV, "AZURE_OPENAI_API_KEY": ""})


# --- endpoint-semantics naming contract -------------------------------------


def test_variable_names_are_azure_openai_prefixed() -> None:
    """The model data plane must be named apart from the project endpoint.

    Pinning the literal names stops a rename drifting away from the documented
    contract, and stops the model endpoint quietly reacquiring a `FOUNDRY_`
    prefix that would blur it with the project endpoint.
    """
    assert ENDPOINT_VAR == "AZURE_OPENAI_ENDPOINT"
    assert DEPLOYMENT_VAR == "AZURE_OPENAI_DEPLOYMENT"
    assert SCOPE_VAR == "AZURE_OPENAI_AUTH_SCOPE"
    assert TIMEOUT_VAR == "AZURE_OPENAI_TIMEOUT_SECONDS"


def test_project_endpoint_name_is_reserved_but_unused() -> None:
    """FOUNDRY_PROJECT_ENDPOINT is terminology for later phases, not an input."""
    assert PROJECT_ENDPOINT_VAR == "FOUNDRY_PROJECT_ENDPOINT"

    config = load_config(
        {**VALID_ENV, PROJECT_ENDPOINT_VAR: "https://example.services.ai.azure.com/api/projects/p"}
    )
    # Setting it must neither fail nor influence the data-plane configuration.
    assert config.endpoint == VALID_ENV[ENDPOINT_VAR]


def test_retired_variable_names_are_not_honoured() -> None:
    """The old names were renamed with no compatibility aliases.

    Supplying only the retired names must fail, rather than silently working and
    leaving two spellings in circulation.
    """
    retired = {
        "FOUNDRY_ENDPOINT": "https://example-resource.openai.azure.com/openai/v1/",
        "FOUNDRY_DEPLOYMENT": "gpt-4-1-mini",
    }
    with pytest.raises(ConfigurationError) as caught:
        load_config(retired)
    assert ENDPOINT_VAR in str(caught.value)


def test_retired_timeout_and_scope_names_are_ignored() -> None:
    config = load_config(
        {
            **VALID_ENV,
            "FOUNDRY_TIMEOUT_SECONDS": "999",
            "FOUNDRY_AUTH_SCOPE": "https://retired.example/.default",
        }
    )
    assert config.timeout_seconds == DEFAULT_TIMEOUT_SECONDS
    assert config.auth_scope == DEFAULT_AUTH_SCOPE


# --- authentication audience ------------------------------------------------


def test_default_auth_scope_is_explicit_and_pinned() -> None:
    """The audience is endpoint/contract specific, so it is stated, not inferred."""
    assert DEFAULT_AUTH_SCOPE == "https://ai.azure.com/.default"
    assert load_config(dict(VALID_ENV)).auth_scope == "https://ai.azure.com/.default"


def test_auth_scope_is_overridable_for_diagnostics() -> None:
    override = "https://cognitiveservices.azure.com/.default"
    config = load_config({**VALID_ENV, SCOPE_VAR: override})
    assert config.auth_scope == override


def test_blank_auth_scope_falls_back_to_the_explicit_default() -> None:
    """Blank means "unset", not "no audience"."""
    assert load_config({**VALID_ENV, SCOPE_VAR: "   "}).auth_scope == DEFAULT_AUTH_SCOPE


# --- canonical endpoint validation (Phase 16.3A) -----------------------------


def test_canonical_data_plane_url_is_accepted() -> None:
    config = load_config(dict(VALID_ENV))
    assert config.endpoint.endswith("/openai/v1/")


def test_generic_cognitive_services_account_endpoint_is_rejected() -> None:
    """The most obvious value in the portal is the wrong one.

    `properties.endpoint` answers for every Cognitive Services API on the
    resource but does not serve the OpenAI Responses API, so it 404s at call
    time — after a token has been minted and a request sent.
    """
    with pytest.raises(ConfigurationError) as caught:
        load_config(
            {**VALID_ENV, ENDPOINT_VAR: "https://example-resource.cognitiveservices.azure.com/"}
        )
    message = str(caught.value)
    assert "cognitiveservices.azure.com" in message
    assert "llm_endpoint" in message


def test_generic_host_is_rejected_even_with_the_v1_path_appended() -> None:
    with pytest.raises(ConfigurationError):
        load_config(
            {
                **VALID_ENV,
                ENDPOINT_VAR: "https://example.cognitiveservices.azure.com/openai/v1/",
            }
        )


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://example-resource.openai.azure.com/",
        "https://example-resource.openai.azure.com/openai/",
        "https://example-resource.openai.azure.com/openai/v1",
        "https://example-resource.openai.azure.com/openai/v2/",
    ],
)
def test_endpoint_must_end_with_the_canonical_suffix(endpoint: str) -> None:
    """A bare OpenAI host is not enough; the v1 surface lives beneath the path."""
    with pytest.raises(ConfigurationError) as caught:
        load_config({**VALID_ENV, ENDPOINT_VAR: endpoint})
    assert "/openai/v1/" in str(caught.value)


def test_endpoint_errors_never_echo_the_supplied_value() -> None:
    """An endpoint identifies the tenant's resource; keep it out of logs."""
    private = "https://very-private-resource-name.cognitiveservices.azure.com/"
    with pytest.raises(ConfigurationError) as caught:
        load_config({**VALID_ENV, ENDPOINT_VAR: private})
    assert "very-private-resource-name" not in str(caught.value)


def test_validate_endpoint_is_callable_independently() -> None:
    """Validation must be usable before any client or credential is built."""
    validate_endpoint("https://example.openai.azure.com/openai/v1/")
    with pytest.raises(ConfigurationError):
        validate_endpoint("https://example.cognitiveservices.azure.com/")
