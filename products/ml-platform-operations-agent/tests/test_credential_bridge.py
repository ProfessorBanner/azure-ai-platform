"""Security review of the SDK-to-MLflow OAuth credential bridge.

Every property is asserted against a FAKE SDK client, so no test needs
credentials and none can leak a real one. The token values used here are
obvious fakes.

WHY THIS FILE EXISTS
--------------------
The bridge hands a live credential to a third-party library. That is the
highest-risk code in this product, and "it looked fine" is not a control.
"""

from __future__ import annotations

import json
import os
import sys
import types
from collections.abc import Iterator
from typing import Any

import pytest

from ml_platform_operations_agent import tracing
from ml_platform_operations_agent.errors import ConfigurationError
from ml_platform_operations_agent.tracing import (
    ALLOWED_METADATA_KEYS,
    REQUIRED_PROFILE,
    REQUIRED_WORKSPACE_HOST,
    TracingConfig,
    TracingMode,
    bridged_credentials,
    sanitise_metadata,
)

AUTHORISED_HOST = f"https://{REQUIRED_WORKSPACE_HOST}.azuredatabricks.net"
FAKE_OAUTH = "eyJfake.oauth.token-not-real"
FAKE_PAT = "dapi00000000000000000000000000000000"


def _config(profile: str = REQUIRED_PROFILE) -> TracingConfig:
    return TracingConfig(
        mode=TracingMode.MANAGED,
        experiment="/Shared/x",
        tracking_uri="databricks",
        profile=profile,
    )


class FakeClient:
    def __init__(self, host: str, header: str | None, raises: Exception | None = None) -> None:
        outer = self

        class _Config:
            host = ""

            @staticmethod
            def authenticate() -> dict[str, str]:
                if outer._raises:
                    raise outer._raises
                return {} if outer._header is None else {"Authorization": outer._header}

        self._raises = raises
        self._header = header
        _Config.host = host
        self.config = _Config


@pytest.fixture
def fake_sdk(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Install a fake `databricks.sdk` so no real credential is ever touched."""
    state: dict[str, Any] = {"client": None, "profiles": []}

    def factory(profile: str) -> Any:
        state["profiles"].append(profile)
        client = state["client"]
        if isinstance(client, Exception):
            raise client
        return client

    module = types.ModuleType("databricks.sdk")
    module.WorkspaceClient = factory  # type: ignore[attr-defined]
    parent = types.ModuleType("databricks")
    parent.sdk = module  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "databricks", parent)
    monkeypatch.setitem(sys.modules, "databricks.sdk", module)
    yield state


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("DATABRICKS_HOST", "DATABRICKS_TOKEN", "DATABRICKS_CONFIG_PROFILE"):
        monkeypatch.delenv(name, raising=False)


# --- 1. the token comes only from the authenticated dev SDK client ----------


def test_token_is_taken_from_the_sdk_client_for_the_required_profile(fake_sdk: Any) -> None:
    fake_sdk["client"] = FakeClient(AUTHORISED_HOST, f"Bearer {FAKE_OAUTH}")
    with bridged_credentials(_config()):
        assert os.environ["DATABRICKS_TOKEN"] == FAKE_OAUTH
    assert fake_sdk["profiles"] == [REQUIRED_PROFILE]


def test_any_other_profile_is_refused(fake_sdk: Any) -> None:
    """The bridge hands out a credential, so it validates its own inputs
    rather than trusting the caller."""
    fake_sdk["client"] = FakeClient(AUTHORISED_HOST, f"Bearer {FAKE_OAUTH}")
    for profile in ("aiplatform-prod", "aiplatform-sandbox", "DEFAULT", ""):
        with pytest.raises(ConfigurationError, match="only permitted"):
            with bridged_credentials(_config(profile)):
                pass
    assert fake_sdk["profiles"] == []


# --- 2. the workspace host is asserted before bridging ----------------------


def test_an_unauthorised_workspace_host_is_refused(fake_sdk: Any) -> None:
    fake_sdk["client"] = FakeClient(
        "https://adb-9999999999999999.9.azuredatabricks.net", f"Bearer {FAKE_OAUTH}"
    )
    with pytest.raises(ConfigurationError, match="authorised"):
        with bridged_credentials(_config()):
            pass


def test_no_environment_variable_is_set_when_the_host_check_fails(fake_sdk: Any) -> None:
    """The check must happen BEFORE the credential reaches the environment."""
    fake_sdk["client"] = FakeClient(
        "https://adb-9999999999999999.9.azuredatabricks.net", f"Bearer {FAKE_OAUTH}"
    )
    with pytest.raises(ConfigurationError):
        with bridged_credentials(_config()):
            pass
    assert "DATABRICKS_TOKEN" not in os.environ
    assert "DATABRICKS_HOST" not in os.environ


# --- 3. short-lived OAuth material, not a PAT -------------------------------


def test_a_personal_access_token_is_refused(fake_sdk: Any) -> None:
    """A PAT is long-lived. This phase must not depend on one existing, nor
    quietly start working because somebody created one."""
    fake_sdk["client"] = FakeClient(AUTHORISED_HOST, f"Bearer {FAKE_PAT}")
    with pytest.raises(ConfigurationError, match="personal access token"):
        with bridged_credentials(_config()):
            pass
    assert "DATABRICKS_TOKEN" not in os.environ


def test_a_missing_or_malformed_authorization_header_is_refused(fake_sdk: Any) -> None:
    for header in (None, "", "Basic abc", "Token abc"):
        fake_sdk["client"] = FakeClient(AUTHORISED_HOST, header)
        with pytest.raises(ConfigurationError, match="bearer token"):
            with bridged_credentials(_config()):
                pass


# --- 4/5. memory only, never written to disk -------------------------------


def test_the_token_is_never_written_to_disk(fake_sdk: Any, tmp_path: Any) -> None:
    """Asserted by watching the filesystem, not by reading the code."""
    fake_sdk["client"] = FakeClient(AUTHORISED_HOST, f"Bearer {FAKE_OAUTH}")
    before = set(tmp_path.rglob("*"))
    cwd_before = set(os.listdir("."))
    with bridged_credentials(_config()):
        pass
    assert set(tmp_path.rglob("*")) == before
    assert set(os.listdir(".")) == cwd_before


def test_the_bridge_module_performs_no_file_write() -> None:
    import ast
    from pathlib import Path

    source = Path(tracing.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    called = {
        node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    for forbidden in ("open", "write_text", "write_bytes", "mkdtemp", "dump", "print"):
        assert forbidden not in called, f"tracing.py calls {forbidden}"


# --- 6. never in logs, exceptions, traces or output -------------------------


def test_no_error_message_contains_the_token(fake_sdk: Any) -> None:
    for header, host in (
        (f"Bearer {FAKE_PAT}", AUTHORISED_HOST),
        (f"Bearer {FAKE_OAUTH}", "https://adb-9999999999999999.9.azuredatabricks.net"),
    ):
        fake_sdk["client"] = FakeClient(host, header)
        with pytest.raises(ConfigurationError) as caught:
            with bridged_credentials(_config()):
                pass
        message = str(caught.value)
        assert FAKE_OAUTH not in message
        assert FAKE_PAT not in message


def test_an_sdk_failure_is_sanitised_and_fails_closed(fake_sdk: Any) -> None:
    """FAILS CLOSED: no credential is set, and the original message — which can
    carry a host, a request id or a config path — is discarded."""
    secret = f"host={AUTHORISED_HOST} token={FAKE_OAUTH} path=/Users/someone/.databrickscfg"
    fake_sdk["client"] = FakeClient(AUTHORISED_HOST, None, raises=RuntimeError(secret))
    with pytest.raises(ConfigurationError) as caught:
        with bridged_credentials(_config()):
            pass
    message = str(caught.value)
    assert FAKE_OAUTH not in message
    assert "/Users/someone" not in message
    assert "RuntimeError" in message
    assert "DATABRICKS_TOKEN" not in os.environ


def test_the_token_cannot_reach_a_trace(fake_sdk: Any) -> None:
    """No allow-listed key could carry it, and sanitisation drops the rest."""
    for name in ("DATABRICKS_TOKEN", "token", "Authorization", "bearer", "host"):
        assert name not in ALLOWED_METADATA_KEYS
    clean = sanitise_metadata({"Authorization": f"Bearer {FAKE_OAUTH}", "case_id": "x"})
    assert json.dumps(clean).find(FAKE_OAUTH) == -1
    assert clean == {"case_id": "x"}


# --- 7/8. the environment is restored exactly -------------------------------


def test_previous_values_are_restored_on_success(fake_sdk: Any, monkeypatch: Any) -> None:
    monkeypatch.setenv("DATABRICKS_HOST", "https://previous.example")
    monkeypatch.setenv("DATABRICKS_TOKEN", "previous-token")
    monkeypatch.setenv("DATABRICKS_CONFIG_PROFILE", "previous-profile")
    fake_sdk["client"] = FakeClient(AUTHORISED_HOST, f"Bearer {FAKE_OAUTH}")

    with bridged_credentials(_config()):
        assert os.environ["DATABRICKS_TOKEN"] == FAKE_OAUTH
        assert "DATABRICKS_CONFIG_PROFILE" not in os.environ

    assert os.environ["DATABRICKS_HOST"] == "https://previous.example"
    assert os.environ["DATABRICKS_TOKEN"] == "previous-token"
    assert os.environ["DATABRICKS_CONFIG_PROFILE"] == "previous-profile"


def test_previous_values_are_restored_on_exception(fake_sdk: Any, monkeypatch: Any) -> None:
    monkeypatch.setenv("DATABRICKS_TOKEN", "previous-token")
    fake_sdk["client"] = FakeClient(AUTHORISED_HOST, f"Bearer {FAKE_OAUTH}")

    with pytest.raises(ValueError):
        with bridged_credentials(_config()):
            raise ValueError("the evaluation blew up")

    assert os.environ["DATABRICKS_TOKEN"] == "previous-token"


def test_variables_that_did_not_exist_are_removed_not_blanked(fake_sdk: Any) -> None:
    """A naive save/restore writes "" back and leaves a different environment
    than the one it found."""
    fake_sdk["client"] = FakeClient(AUTHORISED_HOST, f"Bearer {FAKE_OAUTH}")
    with bridged_credentials(_config()):
        assert os.environ["DATABRICKS_TOKEN"] == FAKE_OAUTH
    for name in ("DATABRICKS_HOST", "DATABRICKS_TOKEN", "DATABRICKS_CONFIG_PROFILE"):
        assert name not in os.environ, f"{name} was left behind as {os.environ.get(name)!r}"


def test_the_token_does_not_outlive_the_block(fake_sdk: Any) -> None:
    fake_sdk["client"] = FakeClient(AUTHORISED_HOST, f"Bearer {FAKE_OAUTH}")
    with bridged_credentials(_config()):
        pass
    assert FAKE_OAUTH not in json.dumps(dict(os.environ))


# --- 9. the profile cannot silently redirect the destination ----------------


def test_the_profile_variable_is_removed_inside_the_block(fake_sdk: Any, monkeypatch: Any) -> None:
    """Left set, MLflow could fall back to the broken CLI path — the very
    failure this bridge exists to avoid."""
    monkeypatch.setenv("DATABRICKS_CONFIG_PROFILE", REQUIRED_PROFILE)
    fake_sdk["client"] = FakeClient(AUTHORISED_HOST, f"Bearer {FAKE_OAUTH}")
    with bridged_credentials(_config()):
        assert "DATABRICKS_CONFIG_PROFILE" not in os.environ


# --- 10. async trace export stays disabled for a controlled run -------------


def test_managed_activation_requires_synchronous_trace_export(monkeypatch: Any) -> None:
    """Background export threads each fetch their own token; that concurrency
    is what turned the keychain failure into silently dropped traces."""
    config = _config()
    for value in ("true", "1", None):
        if value is None:
            monkeypatch.delenv("MLFLOW_ENABLE_ASYNC_TRACE_LOGGING", raising=False)
        else:
            monkeypatch.setenv("MLFLOW_ENABLE_ASYNC_TRACE_LOGGING", value)
        with pytest.raises(ConfigurationError, match="synchronous"):
            tracing.activate(config)


def test_async_detection_treats_absence_as_enabled() -> None:
    """MLflow defaults it on, so a missing variable must not read as off."""
    assert tracing._async_trace_logging_enabled({}) is True
    assert (
        tracing._async_trace_logging_enabled({"MLFLOW_ENABLE_ASYNC_TRACE_LOGGING": "false"})
        is False
    )
    assert tracing._async_trace_logging_enabled({"MLFLOW_ENABLE_ASYNC_TRACE_LOGGING": "0"}) is False


# --- 11. disabled and local modes need no credential at all -----------------


def test_disabled_and_local_modes_never_touch_the_sdk(fake_sdk: Any) -> None:
    tracing.activate(TracingConfig(mode=TracingMode.DISABLED))
    assert fake_sdk["profiles"] == []


# --- deployed App mode is a different world ---------------------------------


def test_app_runtime_is_detected_from_the_injected_variables() -> None:
    """Verified against the Apps runtime contract, not guessed."""
    from ml_platform_operations_agent.tracing import (
        APP_RUNTIME_VARS,
        running_in_databricks_app,
    )

    assert running_in_databricks_app({}) is False
    assert running_in_databricks_app({"DATABRICKS_CONFIG_PROFILE": REQUIRED_PROFILE}) is False
    for name in APP_RUNTIME_VARS:
        assert running_in_databricks_app({name: "something"}) is True
    # A partially injected environment is still an App environment.
    assert running_in_databricks_app({"DATABRICKS_CLIENT_ID": "x"}) is True


@pytest.mark.parametrize(
    "app_env",
    [
        {"DATABRICKS_CLIENT_ID": "app-sp-id"},
        {"DATABRICKS_CLIENT_SECRET": "app-secret"},
        {"DATABRICKS_APP_NAME": "phase19-ml-platform-ops"},
    ],
)
def test_the_bridge_refuses_to_run_inside_a_deployed_app(
    fake_sdk: Any, monkeypatch: pytest.MonkeyPatch, app_env: dict[str, str]
) -> None:
    """The bridge is a LOCAL workaround for a macOS Keychain failure. A
    deployed App has no keychain, no profile and no developer credential —
    reaching for one from inside a container is a serious enough mistake to
    stop the process."""
    for name, value in app_env.items():
        monkeypatch.setenv(name, value)
    fake_sdk["client"] = FakeClient(AUTHORISED_HOST, f"Bearer {FAKE_OAUTH}")

    with pytest.raises(ConfigurationError, match="deployed Databricks App"):
        with bridged_credentials(_config()):
            pass

    # It refused BEFORE constructing a client or setting anything.
    assert fake_sdk["profiles"] == []
    assert "DATABRICKS_TOKEN" not in os.environ


def test_the_app_server_names_no_profile_and_supplies_no_token() -> None:
    """`WorkspaceClient()` with no arguments is the whole contract: there is no
    path from a deployed container to a developer's ~/.databrickscfg."""
    import ast
    from pathlib import Path

    source = (Path(tracing.__file__).parent / "app/server.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "WorkspaceClient":
            assert not node.args and not node.keywords, (
                "the App must construct WorkspaceClient with no arguments"
            )

    for forbidden in ("DATABRICKS_CONFIG_PROFILE", "DATABRICKS_TOKEN", "bridged_credentials"):
        assert forbidden not in source, f"app/server.py references {forbidden}"


def test_local_mode_still_requires_an_explicit_profile() -> None:
    """The two modes are distinct: local demands a named profile, deployed
    forbids one."""
    from ml_platform_operations_agent.adapters.databricks import require_profile

    with pytest.raises(ConfigurationError):
        require_profile({})
    assert require_profile({"DATABRICKS_CONFIG_PROFILE": REQUIRED_PROFILE}) == REQUIRED_PROFILE
