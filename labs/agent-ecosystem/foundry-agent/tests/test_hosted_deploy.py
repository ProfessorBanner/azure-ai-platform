"""Validation of the Hosted Agent deployment inputs.

The deployment side lives here rather than in the hosted package because it
needs `azure-ai-projects`, and the hosted package deliberately does not depend
on it. These cases guard the manifest contract that version 1 got wrong.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from foundry_agent_lab.hosted_deploy import (
    CONTAINER_ARCHITECTURE,
    MANIFEST_PATH,
    PLATFORM_OWNED_VARIABLES,
    PROTOCOL_VERSION,
    RESOURCE_TIERS,
    DeploymentError,
    describe,
    load_manifest,
)


def write(tmp_path: Path, payload: dict[str, Any]) -> Path:
    target = tmp_path / "agent.manifest.json"
    target.write_text(json.dumps(payload))
    return target


def valid_payload() -> dict[str, Any]:
    """The committed manifest, minus its `$comment` prose.

    Each guard mutates one field of the real manifest, so a case fails because
    of the field it changed rather than because its fixture drifted.
    """
    raw = json.loads(MANIFEST_PATH.read_text())
    return {k: v for k, v in raw.items() if not k.startswith("$")}


# --- the committed manifest is the thing under test --------------------------


def test_the_committed_manifest_loads() -> None:
    manifest = load_manifest()
    assert manifest.agent_name == "phase19-hosted-controlled-agent"
    assert manifest.container_architecture == CONTAINER_ARCHITECTURE
    assert manifest.protocol_versions[0]["version"] == PROTOCOL_VERSION


def test_the_committed_manifest_declares_no_platform_reserved_variable() -> None:
    """FOUNDRY RESERVES PORT.

    It injects the port the container must bind; `azure-ai-agentserver-core`
    resolves it from the environment (default 8088) and binds 0.0.0.0. Pinning
    it in the manifest would bind a port the platform is not routing to, and the
    session would fail readiness with nothing in the log to explain it.
    """
    declared = {name.upper() for name in load_manifest().environment_variables}
    assert declared.isdisjoint(PLATFORM_OWNED_VARIABLES)


def test_the_committed_manifest_uses_the_container_route() -> None:
    manifest = load_manifest()
    assert manifest.uses_container
    assert manifest.registry_connection_id is None


# --- the guards refuse what they say they refuse -----------------------------


def test_a_declared_port_is_refused(tmp_path: Path) -> None:
    payload = valid_payload()
    payload["environment_variables"] = {**payload["environment_variables"], "PORT": "8088"}
    with pytest.raises(DeploymentError, match="platform-reserved"):
        load_manifest(write(tmp_path, payload))


def test_a_lowercase_declared_port_is_refused_too(tmp_path: Path) -> None:
    """Environment variable names are case-sensitive on Linux, but a manifest
    that writes `port` meant `PORT` and should be corrected, not shipped."""
    payload = valid_payload()
    payload["environment_variables"] = {**payload["environment_variables"], "port": "8088"}
    with pytest.raises(DeploymentError, match="platform-reserved"):
        load_manifest(write(tmp_path, payload))


def test_a_secret_shaped_variable_is_refused(tmp_path: Path) -> None:
    payload = valid_payload()
    payload["environment_variables"] = {
        **payload["environment_variables"],
        "AZURE_OPENAI_API_KEY": "x",
    }
    with pytest.raises(DeploymentError, match="secret-shaped"):
        load_manifest(write(tmp_path, payload))


def test_a_non_amd64_architecture_is_refused(tmp_path: Path) -> None:
    payload = valid_payload()
    payload["container_architecture"] = "linux/arm64"
    with pytest.raises(DeploymentError, match="linux/amd64"):
        load_manifest(write(tmp_path, payload))


# --- CPU and memory are a PAIR --------------------------------------------
#
# Foundry accepts a fixed set of tiers and rejects every other combination. The
# fields were previously validated independently, which is how `1` CPU with
# `4Gi` — each value individually appearing in a valid tier — reached a deploy
# and was refused by the live API.


def test_the_committed_manifest_names_a_supported_tier() -> None:
    manifest = load_manifest()
    assert (manifest.cpu, manifest.memory) in RESOURCE_TIERS
    assert (manifest.cpu, manifest.memory) == ("1", "2Gi")


@pytest.mark.parametrize(("cpu", "memory"), RESOURCE_TIERS)
def test_every_supported_tier_is_accepted(tmp_path: Path, cpu: str, memory: str) -> None:
    payload = valid_payload()
    payload["cpu"], payload["memory"] = cpu, memory
    manifest = load_manifest(write(tmp_path, payload))
    assert (manifest.cpu, manifest.memory) == (cpu, memory)


@pytest.mark.parametrize(
    ("cpu", "memory"),
    [
        # THE PAIRING THAT WAS ACTUALLY REJECTED. Both halves appear in the
        # tier table; together they are not a tier.
        ("1", "4Gi"),
        # The mirror-image mistake: `2` belongs with `4Gi`, not `2Gi`.
        ("2", "2Gi"),
        # Every other cross-pairing of otherwise-valid halves.
        ("0.25", "1Gi"),
        ("0.25", "2Gi"),
        ("0.25", "4Gi"),
        ("0.5", "0.5Gi"),
        ("0.5", "2Gi"),
        ("0.5", "4Gi"),
        ("1", "0.5Gi"),
        ("1", "1Gi"),
        ("2", "0.5Gi"),
        ("2", "1Gi"),
        # Values that are in no tier at all.
        ("4", "8Gi"),
        ("1", "2048Mi"),
        ("1.0", "2Gi"),
        # Units omitted, the shape the old regex guard existed to catch.
        ("1", "2"),
        ("1", ""),
        ("", "2Gi"),
    ],
)
def test_an_unsupported_pairing_is_refused(tmp_path: Path, cpu: str, memory: str) -> None:
    payload = valid_payload()
    payload["cpu"], payload["memory"] = cpu, memory
    with pytest.raises(DeploymentError, match="supported tier"):
        load_manifest(write(tmp_path, payload))


def test_the_refusal_names_every_tier_the_platform_offers(tmp_path: Path) -> None:
    """A rejection that does not say what IS allowed sends the reader to the
    portal to guess, which is how the wrong pairing was chosen twice."""
    payload = valid_payload()
    payload["cpu"], payload["memory"] = "1", "4Gi"
    with pytest.raises(DeploymentError) as caught:
        load_manifest(write(tmp_path, payload))
    message = str(caught.value)
    for cpu, memory in RESOURCE_TIERS:
        assert f"{cpu} CPU / {memory}" in message


def test_a_missing_resource_field_is_refused(tmp_path: Path) -> None:
    payload = valid_payload()
    del payload["memory"]
    with pytest.raises(DeploymentError, match="supported tier"):
        load_manifest(write(tmp_path, payload))


# --- the plan is reviewable and carries no secret ----------------------------


def test_the_plan_names_variables_but_never_their_values() -> None:
    """A plan is printed to a terminal and pasted into reviews; it names what is
    configured without reproducing the configuration."""
    manifest = load_manifest()
    plan = describe(manifest, code_sha="deadbeef")
    assert plan["environment_variable_names"] == sorted(manifest.environment_variables)
    assert "environment_variables" not in plan


def test_the_plan_records_the_image_actually_being_deployed() -> None:
    manifest = load_manifest()
    plan = describe(manifest, code_sha="deadbeef")
    assert plan["packaging"] == "container"
    assert plan["container_image"] == manifest.container_image
