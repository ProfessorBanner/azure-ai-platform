"""Hosted process configuration: environment only, and no keys."""

from __future__ import annotations

import pytest

from hosted_agent.config import (
    AGENT_NAME_VAR,
    DEFAULT_AGENT_NAME,
    DEFAULT_PORT,
    FORBIDDEN_KEY_VARS,
    MAX_BODY_VAR,
    PORT_VAR,
    HostedConfigurationError,
    load_hosted_config,
)


def test_defaults_apply_when_nothing_is_set() -> None:
    config = load_hosted_config({})
    assert config.port == DEFAULT_PORT
    assert config.agent_name == DEFAULT_AGENT_NAME


def test_the_platform_supplied_port_is_honoured() -> None:
    """Foundry supplies PORT; a hosted process that ignored it would not serve."""
    assert load_hosted_config({PORT_VAR: "9000"}).port == 9000


@pytest.mark.parametrize("value", ["nope", "0", "70000", "-1"])
def test_a_meaningless_port_is_refused(value: str) -> None:
    with pytest.raises(HostedConfigurationError):
        load_hosted_config({PORT_VAR: value})


@pytest.mark.parametrize("variable", FORBIDDEN_KEY_VARS)
def test_a_key_shaped_variable_is_a_configuration_error(variable: str) -> None:
    """The container authenticates with a managed identity; a key is a wrong belief."""
    with pytest.raises(HostedConfigurationError, match="Key-based"):
        load_hosted_config({variable: "something"})


def test_no_configuration_error_ever_quotes_a_value() -> None:
    marker = "SuperSecretValue"
    with pytest.raises(HostedConfigurationError) as error:
        load_hosted_config({"OPENAI_API_KEY": marker})
    assert marker not in str(error.value)


def test_the_input_bound_is_configurable_and_must_be_positive() -> None:
    assert load_hosted_config({MAX_BODY_VAR: "50"}).max_input_chars == 50
    with pytest.raises(HostedConfigurationError):
        load_hosted_config({MAX_BODY_VAR: "0"})


def test_the_agent_name_is_configurable() -> None:
    assert load_hosted_config({AGENT_NAME_VAR: "custom"}).agent_name == "custom"
