"""Building and (manually) deploying the Hosted Agent version.

WHY THIS LIVES HERE AND NOT IN THE PACKAGE
-------------------------------------------
Deployment needs `azure-ai-projects`; the hosted agent itself does not, because
Foundry calls IN to it. Keeping the SDK on this side is what lets the deployable
package depend on the product properly instead of through a source-path
bootstrap — the openai pins are incompatible, and only the deployment side needs
the newer one.

`--plan` builds the definition and prints it WITHOUT contacting Azure, so the
exact object that would be sent is reviewable before anything is created.
Deployment itself is a deliberate, human-invoked step.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from foundry_agent_lab._product import repository_root

MANIFEST_PATH = repository_root() / "labs/agent-ecosystem/hosted-agent/agent.manifest.json"
PACKAGE_ROOT = repository_root() / "labs/agent-ecosystem/hosted-agent"
PRODUCT_ROOT = repository_root() / "products/platform-engineering-assistant"

# What a code zip may contain. An allow-list rather than an ignore-list: a deny
# list silently ships whatever nobody thought to exclude, and a virtualenv or a
# .env in an uploaded artefact is the kind of mistake that is only found later.
CODE_INCLUDE = ("pyproject.toml", "uv.lock", "README.md")
CODE_INCLUDE_TREES = ("src",)
EXCLUDED_PARTS = frozenset({".venv", "__pycache__", ".git", ".mypy_cache", ".ruff_cache"})

# The Foundry Responses protocol version this agent serves. Pinned rather than
# read loosely: a mismatch between what the server implements and what the
# definition advertises is a runtime failure the platform cannot warn about.
PROTOCOL_VERSION = "1.0.0"

# Foundry runs Linux amd64. An image built on Apple Silicon without
# --platform pushes fine and then fails to start.
CONTAINER_ARCHITECTURE = "linux/amd64"

# THE SUPPORTED RESOURCE TIERS, AS PAIRS.
#
# Foundry does not accept an arbitrary CPU value with an arbitrary memory value.
# It accepts a fixed set of TIERS, and rejects anything else — as the live API
# established. `1` CPU and `4Gi` are each individually plausible and together
# invalid; so are `2` and `2Gi`. Validating the two fields independently is
# precisely how an unsupported pairing reached a deploy and was refused.
#
# Held as a tuple of pairs rather than two allow-lists so the shape of the
# constraint matches the shape of the rule.
RESOURCE_TIERS: tuple[tuple[str, str], ...] = (
    ("0.25", "0.5Gi"),
    ("0.5", "1Gi"),
    ("1", "2Gi"),
    ("2", "4Gi"),
)

# Variables the platform owns. Foundry reserves PORT and injects the port it
# wants the container to bind; `azure-ai-agentserver-core` resolves the bind
# port from it (defaulting to 8088) and binds 0.0.0.0. A manifest that declared
# PORT would pin the process to a port the platform is not routing to, and the
# session would fail readiness with no obvious cause.
PLATFORM_OWNED_VARIABLES = ("PORT",)


class DeploymentError(Exception):
    """The deployment inputs are not usable."""


@dataclass(frozen=True, slots=True)
class HostedManifest:
    """The reviewed deployment inputs, validated before anything is built."""

    agent_name: str
    description: str
    cpu: str
    memory: str
    protocol_versions: tuple[dict[str, str], ...]
    container_architecture: str
    container_image: str | None
    registry_connection_id: str | None
    environment_variables: dict[str, str]
    metadata: dict[str, str]

    @property
    def uses_container(self) -> bool:
        return bool(self.container_image) and "REPLACE_WITH" not in (self.container_image or "")


def load_manifest(path: Path | None = None) -> HostedManifest:
    """Load and validate the manifest.

    Raises:
        DeploymentError: on unreadable or malformed input, a missing protocol,
            or any secret-shaped environment variable. A manifest is committed,
            so a secret in one is a secret in the repository.
    """
    target = path or MANIFEST_PATH
    try:
        raw = json.loads(target.read_text())
    except OSError as exc:
        raise DeploymentError(f"manifest could not be read: {target.name}") from exc
    except json.JSONDecodeError as exc:
        raise DeploymentError(f"{target.name} is not valid JSON: {exc.msg}") from exc

    payload = {k: v for k, v in raw.items() if not k.startswith("$")}
    environment = dict(payload.get("environment_variables") or {})
    for name in environment:
        if any(token in name.upper() for token in ("KEY", "SECRET", "PASSWORD", "TOKEN")):
            raise DeploymentError(
                f"{target.name} declares a secret-shaped variable '{name}'; "
                "the hosted agent authenticates with a managed identity."
            )

    reserved = [name for name in environment if name.upper() in PLATFORM_OWNED_VARIABLES]
    if reserved:
        raise DeploymentError(
            f"{target.name} declares platform-reserved variable(s) "
            f"{', '.join(sorted(reserved))}; Foundry injects the port the container "
            "must bind, and the server reads it from the environment."
        )

    protocols = tuple(payload.get("protocol_versions") or ())
    if not protocols:
        raise DeploymentError("the manifest must declare at least one protocol version")
    responses = [p for p in protocols if p.get("protocol") == "responses"]
    if not responses:
        raise DeploymentError("this agent serves the 'responses' protocol; declare it")
    if responses[0].get("version") != PROTOCOL_VERSION:
        raise DeploymentError(f"the responses protocol version must be {PROTOCOL_VERSION}")

    cpu = str(payload.get("cpu", ""))
    memory = str(payload.get("memory", ""))
    if (cpu, memory) not in RESOURCE_TIERS:
        offered = ", ".join(f"{c} CPU / {m}" for c, m in RESOURCE_TIERS)
        raise DeploymentError(
            f"cpu and memory must name a supported tier, and '{cpu}' with '{memory}' "
            f"is not one; Foundry accepts these PAIRS and rejects every other "
            f"combination: {offered}"
        )

    architecture = str(payload.get("container_architecture", ""))
    if architecture != CONTAINER_ARCHITECTURE:
        raise DeploymentError(
            f"container_architecture must be {CONTAINER_ARCHITECTURE}: Foundry runs "
            "Linux amd64 nodes and an arm64 image fails at start with an exec format error"
        )

    container = payload.get("container_configuration") or {}
    return HostedManifest(
        agent_name=str(payload["agent_name"]),
        description=str(payload.get("description", "")),
        cpu=cpu,
        memory=memory,
        protocol_versions=protocols,
        container_architecture=architecture,
        container_image=container.get("image"),
        registry_connection_id=container.get("registry_connection_id"),
        environment_variables=environment,
        metadata={str(k): str(v) for k, v in (payload.get("metadata") or {}).items()},
    )


def build_code_zip(package_root: Path | None = None) -> tuple[bytes, str]:
    """Zip the deployable package. Returns (bytes, sha256).

    Deterministic: entries are sorted and timestamps fixed, so the same source
    produces the same hash. A deploy that cannot say which bytes it shipped is
    not reproducible.
    """
    root = package_root or PACKAGE_ROOT
    names: list[Path] = [root / name for name in CODE_INCLUDE if (root / name).is_file()]
    for tree in CODE_INCLUDE_TREES:
        for path in sorted((root / tree).rglob("*")):
            if path.is_file() and not EXCLUDED_PARTS & set(path.parts):
                names.append(path)

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(names):
            info = zipfile.ZipInfo(str(path.relative_to(root)), date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            archive.writestr(info, path.read_bytes())
    payload = buffer.getvalue()
    return payload, hashlib.sha256(payload).hexdigest()


def build_definition(manifest: HostedManifest) -> Any:
    """Build the `HostedAgentDefinition` that would be sent.

    Verified against the installed azure-ai-projects 2.5.0 model: `cpu`,
    `memory`, `environment_variables`, `container_configuration`,
    `protocol_versions` and `code_configuration` are the fields it declares.
    """
    from azure.ai.projects.models import (
        ContainerConfiguration,
        HostedAgentDefinition,
        ProtocolVersionRecord,
    )

    kwargs: dict[str, Any] = {
        "cpu": manifest.cpu,
        "memory": manifest.memory,
        "environment_variables": dict(manifest.environment_variables),
        "protocol_versions": [
            ProtocolVersionRecord(protocol=p["protocol"], version=p["version"])
            for p in manifest.protocol_versions
        ],
    }
    if manifest.uses_container:
        # `uses_container` already proved the image is a real reference rather
        # than a REPLACE_WITH placeholder; the assert keeps that fact visible to
        # the type checker instead of restating the check.
        assert manifest.container_image is not None
        kwargs["container_configuration"] = ContainerConfiguration(
            image=manifest.container_image,
            registry_connection_id=manifest.registry_connection_id,
        )
    return HostedAgentDefinition(**kwargs)


def describe(manifest: HostedManifest, code_sha: str) -> dict[str, Any]:
    """A reviewable plan. Contains no secret and contacts nothing."""
    return {
        "agent_name": manifest.agent_name,
        "kind": "hosted",
        "cpu": manifest.cpu,
        "memory": manifest.memory,
        "protocol_versions": [dict(p) for p in manifest.protocol_versions],
        "packaging": "container" if manifest.uses_container else "code",
        "container_architecture": manifest.container_architecture,
        "container_image": manifest.container_image if manifest.uses_container else None,
        "code_zip_sha256": code_sha,
        "environment_variable_names": sorted(manifest.environment_variables),
        "metadata": manifest.metadata,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hosted-agent-deploy",
        description="Build, and optionally create, the Hosted Agent version.",
    )
    parser.add_argument(
        "--plan",
        action="store_true",
        help="Build and print the definition without contacting Azure.",
    )
    parser.add_argument("--manifest", default=None)
    arguments = parser.parse_args(argv)

    try:
        manifest = load_manifest(Path(arguments.manifest) if arguments.manifest else None)
        payload, code_sha = build_code_zip()
    except DeploymentError as error:
        print(f"Deployment inputs are not usable: {error}", file=sys.stderr)
        return 3

    plan = describe(manifest, code_sha)
    plan["code_zip_bytes"] = len(payload)
    print(json.dumps(plan, indent=2))

    if arguments.plan:
        # Still builds the SDK object, so a schema mismatch surfaces in --plan
        # rather than at deploy time.
        build_definition(manifest)
        print("\nPlan only. Nothing was sent.")
        return 0

    print(
        "\nRefusing to deploy from this command. Creating a hosted version is a "
        "deliberate, human-invoked action; see the README for the manual steps.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
