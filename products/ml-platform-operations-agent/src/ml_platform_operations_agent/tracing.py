"""MLflow tracing behind an explicit mode boundary.

TRACING OBSERVES; IT DOES NOT AUTHORIZE
----------------------------------------
Nothing in this module makes a decision. The policy layer decides what may be
asked and called, `evidence.py` decides what counts as degraded, and this module
records that those decisions happened. A trace is not evidence for a diagnosis
and is not an authority over one — deleting every trace would change nothing
about what the agent concludes or refuses.

THREE MODES, AND `DISABLED` IS THE DEFAULT
-------------------------------------------
    disabled  no MLflow import, no network, no files. Unit tests and the
              offline CLI run here, which is why `import mlflow` cannot happen
              by accident.
    local     a temporary directory OUTSIDE the repository, for instrumentation
              tests. Never `mlruns/` in the working tree — a stray tracking
              store is exactly the artefact this repository already had to
              gitignore once.
    managed   Databricks. Requires an explicit profile AND an explicit
              tracking URI, and fails if either is implicit. A silent default
              could point a "DEV evaluation" at another workspace.

MLflow is imported LAZILY, inside the enabling functions. `disabled` therefore
costs nothing and the offline hygiene test still passes for every module that
merely imports this one.

WHAT IS NEVER TRACED
--------------------
`sanitise_metadata` is an ALLOW-LIST, not a deny-list. A deny-list of secret
patterns fails the first time a new field appears; an allow-list fails closed.
Anything not named in `ALLOWED_METADATA_KEYS` is dropped, values are coerced to
short strings, and a correlation id is hashed rather than carried.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ml_platform_operations_agent.config import (
    EVIDENCE_CONTRACT_VERSION,
    POLICY_VERSION,
)
from ml_platform_operations_agent.errors import ConfigurationError

#: Identifies this implementation in the Phase 19.7 cross-platform benchmark.
IMPLEMENTATION = "direct_python_databricks"
APPLICATION_VERSION = "0.1.0"

#: The runbook set the agent may cite. Bumped when the approved set changes, so
#: a stored trace stays interpretable.
RUNBOOK_VERSION = "phase15-ml-operations-runbook-v1"

TRACING_MODE_VAR = "ML_PLATFORM_AGENT_TRACING"
PROFILE_VAR = "DATABRICKS_CONFIG_PROFILE"
REQUIRED_PROFILE = "aiplatform-dev"
REQUIRED_TRACKING_URI = "databricks"

#: The one workspace this phase is authorised to write to. Asserted before any
#: token is handed to MLflow.
REQUIRED_WORKSPACE_HOST = "adb-1000000000000002.12"

#: The ONLY keys that may reach a trace. Everything else is dropped.
ALLOWED_METADATA_KEYS: frozenset[str] = frozenset(
    {
        "implementation",
        "application_version",
        "evidence_contract_version",
        "policy_version",
        "runbook_version",
        "case_id",
        "selected_tools",
        "degradation_status",
        "outcome",
        "refusal_reason",
        "latency_ms",
        "synthetic",
        "correlation_hash",
        "phase",
        "environment",
        "evidence",
        "base_commit",
        "working_tree_dirty",
    }
)

#: Hard cap on any traced value. A long value is a payload trying to become a
#: trace field.
MAX_METADATA_VALUE_CHARS = 200


class TracingMode(StrEnum):
    DISABLED = "disabled"
    LOCAL = "local"
    MANAGED = "managed"


@dataclass(frozen=True, slots=True)
class TracingConfig:
    """Resolved tracing configuration. Never holds a credential."""

    mode: TracingMode
    experiment: str | None = None
    tracking_uri: str | None = None
    profile: str | None = None

    @property
    def enabled(self) -> bool:
        return self.mode is not TracingMode.DISABLED


def correlation_hash(value: str) -> str:
    """A stable, non-reversible correlation id.

    The raw request id may embed a question. A hash correlates a trace with a
    log line without carrying the content into either.
    """
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def sanitise_metadata(values: Mapping[str, Any]) -> dict[str, str]:
    """Reduce arbitrary values to the allow-listed, bounded, stringified subset.

    ALLOW-LIST, NOT DENY-LIST. A deny-list of secret-shaped patterns fails the
    first time somebody adds a field it does not know about; this fails closed.
    """
    clean: dict[str, str] = {}
    for key, value in values.items():
        if key not in ALLOWED_METADATA_KEYS:
            continue
        if isinstance(value, list | tuple):
            rendered = ",".join(str(item) for item in value)
        elif isinstance(value, bool):
            rendered = "true" if value else "false"
        else:
            rendered = str(value)
        rendered = rendered.replace("\n", " ").strip()
        clean[key] = rendered[:MAX_METADATA_VALUE_CHARS]
    return clean


def base_metadata() -> dict[str, str]:
    """The version stamps every trace carries."""
    return {
        "implementation": IMPLEMENTATION,
        "application_version": APPLICATION_VERSION,
        "evidence_contract_version": EVIDENCE_CONTRACT_VERSION,
        "policy_version": POLICY_VERSION,
        "runbook_version": RUNBOOK_VERSION,
    }


def resolve_config(
    environment: Mapping[str, str] | None = None,
    experiment: str | None = None,
) -> TracingConfig:
    """Read the mode, validating what `managed` requires.

    `managed` refuses to infer anything. A tracking URI or profile that is
    merely "probably right" is how a DEV evaluation writes somewhere else.
    """
    env = os.environ if environment is None else environment
    raw = (env.get(TRACING_MODE_VAR) or "disabled").strip().casefold()

    try:
        mode = TracingMode(raw)
    except ValueError:
        raise ConfigurationError(
            f"{TRACING_MODE_VAR} must be one of: {', '.join(m.value for m in TracingMode)}"
        ) from None

    if mode is not TracingMode.MANAGED:
        return TracingConfig(mode=mode, experiment=experiment)

    profile = (env.get(PROFILE_VAR) or "").strip()
    if profile != REQUIRED_PROFILE:
        raise ConfigurationError(
            f"managed tracing requires {PROFILE_VAR}={REQUIRED_PROFILE}; "
            "an implicit or mismatched profile is refused"
        )
    if not experiment:
        raise ConfigurationError("managed tracing requires an explicit experiment path")

    return TracingConfig(
        mode=mode,
        experiment=experiment,
        tracking_uri=REQUIRED_TRACKING_URI,
        profile=profile,
    )


def _async_trace_logging_enabled(environment: Mapping[str, str] | None = None) -> bool:
    """MLflow defaults this to on, so absence means enabled."""
    env = os.environ if environment is None else environment
    return (env.get("MLFLOW_ENABLE_ASYNC_TRACE_LOGGING") or "true").strip().casefold() not in {
        "false",
        "0",
    }


def activate(config: TracingConfig) -> None:
    """Point MLflow at the resolved destination. No-op when disabled.

    Imports MLflow LAZILY so `disabled` never loads it.
    """
    if not config.enabled:
        return

    import mlflow

    if config.mode is TracingMode.LOCAL:
        if not config.tracking_uri:
            raise ConfigurationError("local tracing requires a temporary tracking URI")
        mlflow.set_tracking_uri(config.tracking_uri)
        if config.experiment:
            mlflow.set_experiment(config.experiment)
        return

    # ASYNC TRACE EXPORT MUST BE OFF for a controlled run. Export happens on
    # background threads that each fetch their own token; that concurrency is
    # what turned the keychain failure into silently dropped traces, and an
    # export failure on a background thread is one nobody sees.
    if _async_trace_logging_enabled():
        raise ConfigurationError(
            "managed tracing requires MLFLOW_ENABLE_ASYNC_TRACE_LOGGING=false so "
            "trace export is synchronous and an export failure is visible"
        )

    mlflow.set_tracking_uri(REQUIRED_TRACKING_URI)
    # Asserted AFTER setting it: if MLflow resolved something else — a stale
    # env var, a config file — this catches it before anything is written.
    actual = mlflow.get_tracking_uri()
    if actual != REQUIRED_TRACKING_URI:
        raise ConfigurationError("the resolved MLflow tracking URI is not the required managed one")
    assert config.experiment is not None
    mlflow.set_experiment(config.experiment)


#: Environment variables the bridge sets. Restored exactly on exit.
_BRIDGED_VARS = ("DATABRICKS_HOST", "DATABRICKS_TOKEN", PROFILE_VAR)

#: Environment variables Databricks Apps injects for the app's service
#: identity. Their presence is how "am I running inside an App?" is answered —
#: verified against the Apps runtime contract, not guessed.
APP_RUNTIME_VARS = ("DATABRICKS_CLIENT_ID", "DATABRICKS_CLIENT_SECRET", "DATABRICKS_APP_NAME")


def running_in_databricks_app(environment: Mapping[str, str] | None = None) -> bool:
    """True when the process is running as a deployed Databricks App.

    Any of the injected variables is enough: a partially-injected environment
    is still an App environment, and treating it as local would be exactly the
    wrong inference — it would reach for a developer profile inside a deployed
    container.
    """
    env = os.environ if environment is None else environment
    return any((env.get(name) or "").strip() for name in APP_RUNTIME_VARS)


#: A Databricks personal access token starts with this. The bridge refuses one:
#: a PAT is a long-lived credential, and this phase must not depend on one
#: existing — nor quietly start working because somebody created one.
_PAT_PREFIX = "dapi"


@contextmanager
def bridged_credentials(config: TracingConfig) -> Iterator[None]:
    """Scope the SDK's OAuth token to a block, then restore the environment.

    WHY THIS EXISTS — a real defect, observed 2026-09-04
    -----------------------------------------------------
    MLflow's Databricks auth path FORCES a token refresh, and the refresh must
    write the new token back to the CLI's cache. With `auth_storage = secure`
    that cache is the macOS Keychain, and the write fails from a
    non-interactive subprocess:

        forced token refresh: cache update: exit status 45
        -> Falling back to legacy authentication
        -> 401: Credential was not sent

    MLflow then sends NO credential, and the 401 surfaces on a background
    trace-export thread. During the first managed run this silently dropped
    traces mid-evaluation — the run reported progress while its evidence went
    nowhere. `databricks auth login` does not fix it: the CLI and
    `WorkspaceClient` both work, because neither forces a refresh.

    WHY A CONTEXT MANAGER RATHER THAN A SETTER
    -------------------------------------------
    A credential with no end is a credential that leaks into whatever the
    process does next. This narrows the token's lifetime to the block that
    needs it and restores the prior environment on BOTH success and exception —
    including deleting a variable that did not exist before, which a naive
    save/restore gets wrong by writing an empty string back.

    WHAT IT DOES NOT DO
    -------------------
    No personal access token is created or accepted, nothing is written to
    disk, nothing outlives the block, and no value here can reach a trace:
    `ALLOWED_METADATA_KEYS` has no key that could carry one.
    """
    from databricks.sdk import WorkspaceClient

    # THE BRIDGE IS A LOCAL DEVELOPMENT WORKAROUND AND NOTHING ELSE.
    #
    # It exists because MLflow forces a token refresh that cannot write to a
    # developer's macOS Keychain. A deployed App has no keychain, no CLI
    # profile and no developer credential — it authenticates with the identity
    # Databricks injects, which `WorkspaceClient()` picks up unaided.
    #
    # Refused rather than skipped: reaching a developer profile from inside a
    # deployed container is a serious enough mistake that it should stop the
    # process, not be silently worked around.
    if running_in_databricks_app():
        raise ConfigurationError(
            "the credential bridge is a local development workaround and must not "
            "run inside a deployed Databricks App, which authenticates with its "
            "own injected service identity"
        )

    # The profile is re-asserted here, not merely trusted from resolve_config:
    # this function hands out a credential, so it validates its own inputs.
    if config.profile != REQUIRED_PROFILE:
        raise ConfigurationError(
            f"the credential bridge is only permitted for the {REQUIRED_PROFILE} profile"
        )

    try:
        client = WorkspaceClient(profile=config.profile)
        header = client.config.authenticate().get("Authorization", "")
    except ConfigurationError:
        raise
    except Exception as error:
        # FAILS CLOSED, and without the original message: an SDK auth error can
        # carry a host, a request id or a config file path.
        raise ConfigurationError(
            f"the Databricks SDK could not authenticate this profile ({type(error).__name__})"
        ) from None

    if not header.startswith("Bearer "):
        raise ConfigurationError(
            "the Databricks SDK did not return a bearer token for this profile"
        )

    token = header.removeprefix("Bearer ")
    if token.startswith(_PAT_PREFIX):
        raise ConfigurationError(
            "the resolved credential is a personal access token; this phase "
            "bridges short-lived OAuth material only"
        )

    host = str(client.config.host)
    if REQUIRED_WORKSPACE_HOST not in host:
        # A token for a different workspace would write this evaluation
        # somewhere nobody approved.
        raise ConfigurationError(
            "the resolved workspace host is not the authorised one for this phase"
        )

    previous = {name: os.environ.get(name) for name in _BRIDGED_VARS}
    os.environ["DATABRICKS_HOST"] = host
    os.environ["DATABRICKS_TOKEN"] = token
    # Removed so MLflow cannot fall back to the broken CLI path; host and token
    # now fully determine the destination.
    os.environ.pop(PROFILE_VAR, None)

    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                # It did not exist before. Writing "" back would leave a
                # different environment than the one we found.
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


@contextmanager
def span(config: TracingConfig, name: str, **attributes: Any) -> Iterator[None]:
    """One span, or nothing at all when tracing is disabled.

    Attributes are sanitised on the way in, so a caller cannot place an
    unallow-listed value on a span by passing it here.
    """
    if not config.enabled:
        yield
        return

    import mlflow

    # THE EXCEPTION MUST NOT PROPAGATE THROUGH `start_span`.
    #
    # MLflow's own context manager records a propagating exception as an
    # OpenTelemetry `exception` event carrying `exception.stacktrace` — the
    # full traceback with absolute local paths and the exception message.
    # Catching and re-raising INSIDE the `with` does not help: the re-raise
    # still exits through MLflow's manager. So the failure is recorded on the
    # span, the span is allowed to close cleanly, and the exception is
    # re-raised AFTER the block.
    failure: BaseException | None = None

    with mlflow.start_span(name=name, attributes=sanitise_metadata(attributes)) as active:
        try:
            yield
        except BaseException as error:
            # THE EXCEPTION IS RECORDED AS A CATEGORY, NEVER AS A STACKTRACE.
            #
            # Left alone, `start_span` records a propagating exception on the
            # span as an OpenTelemetry `exception` event carrying
            # `exception.stacktrace` — the full traceback, with absolute local
            # paths and the exception message. Observed on 2026-09-04: the
            # `source_failure` case published a stacktrace containing
            # /Users/<name>/... into a durable managed trace.
            #
            # A traceback in a trace is worse than one in a log: a trace is a
            # shared, durable record read by people who were not present, and
            # an adapter's exception message can carry a connection string, a
            # row of monitoring data or a workspace path.
            #
            # Re-raised afterwards so control flow is unchanged — `agent.run`
            # still catches it and returns a FAILED diagnosis.
            active.set_status("ERROR")
            active.set_attribute("error_type", type(error).__name__)
            failure = error

    if failure is not None:
        # Control flow is unchanged: `agent.run` still catches this and returns
        # a FAILED diagnosis. Only the traceback is withheld from the trace.
        raise failure


def annotate_trace(config: TracingConfig, **metadata: Any) -> None:
    """Attach sanitised metadata to the active trace. No-op when disabled."""
    if not config.enabled:
        return

    import mlflow

    clean = sanitise_metadata({**base_metadata(), **metadata})
    if clean:
        mlflow.update_current_trace(metadata=clean)


__all__ = [
    "ALLOWED_METADATA_KEYS",
    "APPLICATION_VERSION",
    "IMPLEMENTATION",
    "MAX_METADATA_VALUE_CHARS",
    "REQUIRED_PROFILE",
    "REQUIRED_TRACKING_URI",
    "REQUIRED_WORKSPACE_HOST",
    "RUNBOOK_VERSION",
    "TRACING_MODE_VAR",
    "TracingConfig",
    "TracingMode",
    "activate",
    "APP_RUNTIME_VARS",
    "annotate_trace",
    "bridged_credentials",
    "base_metadata",
    "correlation_hash",
    "resolve_config",
    "running_in_databricks_app",
    "sanitise_metadata",
    "span",
]
