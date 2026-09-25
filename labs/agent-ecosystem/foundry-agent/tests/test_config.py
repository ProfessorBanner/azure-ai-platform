"""Configuration: the project endpoint, and the refusal of key auth."""

from __future__ import annotations

import pytest

from foundry_agent_lab.config import (
    AGENT_NAME_VAR,
    DEFAULT_AGENT_NAME,
    DEFAULT_AUTH_SCOPE,
    FORBIDDEN_KEY_VARS,
    PROJECT_ENDPOINT_VAR,
    LabConfigurationError,
    load_config,
)

ENDPOINT = "https://aif-example-sandbox.services.ai.azure.com/api/projects/proj-capability-lab"


def test_a_valid_project_endpoint_is_accepted() -> None:
    config = load_config({PROJECT_ENDPOINT_VAR: ENDPOINT})
    assert config.project_endpoint == ENDPOINT
    assert config.agent_name == DEFAULT_AGENT_NAME
    assert config.model_deployment == "gpt-4-1-mini"
    assert config.auth_scope == DEFAULT_AUTH_SCOPE


def test_a_missing_endpoint_is_a_configuration_error() -> None:
    with pytest.raises(LabConfigurationError, match=PROJECT_ENDPOINT_VAR):
        load_config({})


def test_the_model_data_plane_endpoint_is_refused() -> None:
    """The two endpoints are different APIs, not two spellings of one."""
    with pytest.raises(LabConfigurationError, match="PROJECT endpoint"):
        load_config(
            {PROJECT_ENDPOINT_VAR: "https://aif-example-sandbox.openai.azure.com/openai/v1/"}
        )


def test_a_malformed_endpoint_is_refused() -> None:
    with pytest.raises(LabConfigurationError):
        load_config({PROJECT_ENDPOINT_VAR: "https://example.com/api/projects/p"})


@pytest.mark.parametrize("variable", FORBIDDEN_KEY_VARS)
def test_a_key_shaped_variable_is_a_configuration_error(variable: str) -> None:
    """Local auth is disabled on the account; a key signals a wrong belief."""
    with pytest.raises(LabConfigurationError, match="Key-based"):
        load_config({PROJECT_ENDPOINT_VAR: ENDPOINT, variable: "something"})


def test_a_malformed_agent_name_is_refused() -> None:
    with pytest.raises(LabConfigurationError, match=AGENT_NAME_VAR):
        load_config({PROJECT_ENDPOINT_VAR: ENDPOINT, AGENT_NAME_VAR: "not a valid name!"})


def test_no_configuration_error_ever_quotes_a_value() -> None:
    marker = "SuperSecretValue"
    with pytest.raises(LabConfigurationError) as error:
        load_config({PROJECT_ENDPOINT_VAR: ENDPOINT, "OPENAI_API_KEY": marker})
    assert marker not in str(error.value)
