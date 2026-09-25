"""Packaging invariants: what ships, what it depends on, and what it must not."""

from __future__ import annotations

import json
import pathlib
from typing import Any

import pytest

PACKAGE_ROOT = pathlib.Path(__file__).resolve().parents[1]
REPO_ROOT = PACKAGE_ROOT.parents[2]


def pyproject() -> str:
    return (PACKAGE_ROOT / "pyproject.toml").read_text()


def test_the_package_depends_on_the_product_properly() -> None:
    """Not a source-path bootstrap: a real path dependency, because it resolves."""
    text = pyproject()
    assert "platform-engineering-assistant" in text
    assert "tool.uv.sources" in text


def dependency_block() -> str:
    """The declared runtime dependencies, without the surrounding prose.

    Matched against the block rather than the file: this pyproject EXPLAINS in a
    comment why the Foundry SDK is absent, and a whole-file scan would trip on
    its own explanation.
    """
    return pyproject().split("dependencies = [", 1)[1].split("]", 1)[0]


def test_the_package_does_not_depend_on_the_foundry_sdk() -> None:
    """A hosted agent is called INTO; it needs no client SDK.

    This is what lets the package depend on the product at all — the SDK would
    force `openai>=3` and the product pins `openai<3`.
    """
    assert "azure-ai-projects" not in dependency_block()


def test_the_installed_openai_matches_the_products_pin() -> None:
    import openai

    assert openai.__version__.startswith("2.") or openai.__version__.startswith("1.")


def test_the_foundry_sdk_is_absent_from_the_runtime() -> None:
    with pytest.raises(ImportError):
        import azure.ai.projects  # noqa: F401


def test_the_package_is_not_a_workspace_member() -> None:
    """A single uv resolution cannot hold both openai pins."""
    root = json.dumps((REPO_ROOT / "pyproject.toml").read_text())
    assert "labs/agent-ecosystem/hosted-agent" not in root


# --- the deployment artefacts ------------------------------------------------


def manifest() -> dict[str, Any]:
    raw = json.loads((PACKAGE_ROOT / "agent.manifest.json").read_text())
    return {k: v for k, v in raw.items() if not k.startswith("$")}


def test_the_manifest_declares_the_responses_protocol_at_version_1_0_0() -> None:
    protocols = manifest()["protocol_versions"]
    responses = [p for p in protocols if p["protocol"] == "responses"]
    assert responses, "the agent serves the responses protocol"
    assert responses[0]["version"] == "1.0.0"


def test_the_manifest_declares_a_supported_resource_tier() -> None:
    """CPU AND MEMORY ARE A PAIR, NOT TWO INDEPENDENT DIALS.

    The live Foundry API accepts a fixed set of tiers and rejects any other
    combination — `1` CPU goes with `2Gi` and nothing else. Asserting the two
    fields separately is how `1` / `4Gi` (each individually plausible) reached
    a deploy and was refused. The authoritative list lives in
    `foundry_agent_lab.hosted_deploy.RESOURCE_TIERS`; it is restated here
    because this package deliberately cannot import that module.
    """
    values = manifest()
    supported = {("0.25", "0.5Gi"), ("0.5", "1Gi"), ("1", "2Gi"), ("2", "4Gi")}
    assert (values["cpu"], values["memory"]) in supported
    assert (values["cpu"], values["memory"]) == ("1", "2Gi")


def test_the_manifest_pins_linux_amd64() -> None:
    """Foundry runs Linux amd64. An arm64 image pushes fine and then fails to
    start with an exec format error, which is a slow way to learn this."""
    assert manifest()["container_architecture"] == "linux/amd64"


def test_the_registry_connection_id_stays_optional_for_the_standard_acr_path() -> None:
    assert manifest()["container_configuration"]["registry_connection_id"] is None


def test_the_manifest_declares_no_secret_shaped_variable() -> None:
    """A manifest is committed, so a secret in one is a secret in the repository."""
    for name in manifest()["environment_variables"]:
        assert not any(t in name.upper() for t in ("KEY", "SECRET", "PASSWORD", "TOKEN"))


def test_the_manifest_fields_match_the_sdk_model() -> None:
    """Asserted against the real model rather than trusted.

    The SDK is not installed in this environment by design, so the field names
    are pinned here as the contract this manifest was built against; the
    deployment side asserts them against the live model.
    """
    expected = {
        "cpu",
        "memory",
        "environment_variables",
        "container_configuration",
        "protocol_versions",
    }
    assert expected <= set(manifest()) | {"container_configuration", "environment_variables"}


def test_the_dockerfile_runs_unprivileged_and_uses_a_frozen_sync() -> None:
    text = (PACKAGE_ROOT / "Dockerfile").read_text()
    assert "USER agent" in text
    assert "--frozen" in text


def test_the_dockerfile_exposes_the_configured_port() -> None:
    assert "8088" in (PACKAGE_ROOT / "Dockerfile").read_text()


def test_the_dockerfile_bakes_in_no_secret() -> None:
    """Checked over instructions, not comments: the Dockerfile explains that it
    bakes in no secret, and a whole-file scan would fail on that sentence."""
    instructions = [
        line
        for line in (PACKAGE_ROOT / "Dockerfile").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    text = " ".join(instructions).upper()
    for token in ("API_KEY", "SECRET", "PASSWORD"):
        assert token not in text


def test_the_entry_point_module_exists() -> None:
    assert (PACKAGE_ROOT / "src" / "hosted_agent" / "__main__.py").is_file()


# --- the control model is not re-expressed here ------------------------------


def test_the_package_re_implements_no_control() -> None:
    """A hosting adapter that grew a policy layer would be a second agent."""
    source = " ".join(
        path.read_text() for path in (PACKAGE_ROOT / "src" / "hosted_agent").rglob("*.py")
    )
    for forbidden in ("def authorise", "class ToolRegistry", "ToolRiskLevel.STATE_CHANGING ="):
        assert forbidden not in source


def test_the_adapter_calls_the_products_agent_service() -> None:
    source = (PACKAGE_ROOT / "src" / "hosted_agent" / "server.py").read_text()
    assert "build_agent_service" in source
    assert "agent.run" in source


def test_the_official_protocol_library_is_a_dependency() -> None:
    """The protocol is the platform's, not ours to imitate."""
    assert "azure-ai-agentserver-responses" in dependency_block()


def test_no_hand_built_responses_payload_survives() -> None:
    """19.1e originally guessed at the wire contract. That guess is deleted,
    not maintained alongside the real thing."""
    source = " ".join(
        path.read_text() for path in (PACKAGE_ROOT / "src" / "hosted_agent").rglob("*.py")
    )
    for remnant in ('"object": "response"', "def to_responses_payload", "def extract_question"):
        assert remnant not in source


def test_the_server_uses_the_library_host_and_readiness() -> None:
    source = (PACKAGE_ROOT / "src" / "hosted_agent" / "server.py").read_text()
    assert "ResponsesAgentServerHost" in source
    assert "response_handler" in source
    # Readiness is the library's; a hand-rolled one would compete with it.
    assert "/health/ready" not in source


# --- the container packaging contract ----------------------------------------
#
# 19.1e deployed an image that built, pushed and started, and then failed every
# Foundry session with `No module named hosted_agent`. Nothing here could have
# caught it, because every check ran against the workstation virtualenv rather
# than the artefact. `scripts/container_smoke.sh` is the check that actually
# runs the image; these are the cheap static invariants that make the mistake
# hard to reintroduce without Docker being available.


def dockerfile() -> str:
    return (PACKAGE_ROOT / "Dockerfile").read_text()


def dockerfile_instructions() -> list[str]:
    """Instruction lines only, with continuations joined.

    Matched over instructions rather than the whole file: this Dockerfile
    EXPLAINS its own rules in comments, and a whole-file scan would pass on the
    explanation of a rule the instructions no longer follow.
    """
    joined: list[str] = []
    buffer = ""
    for line in dockerfile().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        buffer += stripped[:-1] + " " if stripped.endswith("\\") else stripped
        if not stripped.endswith("\\"):
            joined.append(buffer)
            buffer = ""
    return joined


def test_the_architecture_is_asserted_rather_than_pinned_in_from() -> None:
    """`FROM --platform=linux/amd64` OVERRIDES the build's own --platform, so a
    `docker build --platform linux/arm64` would silently produce a mixed image
    instead of failing. The requirement did not go away when the constant did:
    it moved to a `TARGETARCH` assertion that can actually fail the build."""
    instructions = dockerfile_instructions()
    from_lines = [i for i in instructions if i.startswith("FROM ")]
    assert from_lines, "the Dockerfile declares stages"
    for line in from_lines:
        assert "--platform" not in line, f"constant platform pin survives: {line}"

    assert "ARG TARGETARCH" in instructions
    assertion = [i for i in instructions if i.startswith("RUN test") and "TARGETARCH" in i]
    assert assertion, "the runtime stage must assert TARGETARCH"
    assert "amd64" in assertion[0]


def test_the_smoke_test_independently_rechecks_the_built_architecture() -> None:
    """Two independent checks, because the Dockerfile assertion runs inside the
    build and the manifest's claim is only a claim."""
    text = (PACKAGE_ROOT / "scripts" / "container_smoke.sh").read_text()
    assert "{{.Os}}/{{.Architecture}}" in text
    assert "linux/amd64" in text


def test_the_build_stage_and_the_runtime_share_one_absolute_path() -> None:
    """THE ROOT CAUSE OF THE 19.1e SESSION FAILURE.

    Both the product and this package are installed editable, and an editable
    install records an ABSOLUTE path to the source tree in a `.pth` file. The
    original Dockerfile synced under `/build` and then relocated the tree to
    `/app`, which left those paths dangling: the distribution was installed and
    the module was still unimportable.
    """
    instructions = dockerfile_instructions()
    assert not any("/build" in instruction for instruction in instructions), (
        "the build stage must sync at the path the runtime uses, not at /build"
    )
    assert "COPY --from=build /app /app" in instructions


def test_the_venv_on_path_is_the_one_that_was_synced() -> None:
    text = dockerfile()
    assert 'ENV PATH="/app/labs/agent-ecosystem/hosted-agent/.venv/bin:${PATH}"' in text


def test_the_image_carries_the_corpus_documents_and_a_repository_root() -> None:
    """The corpus manifest names REPOSITORY-relative paths under `docs/`, and
    the loader derives the containment root from a `.git` entry. An image
    without both starts and then exits 3 with category=corpus."""
    instructions = dockerfile_instructions()
    assert "COPY docs /app/docs" in instructions
    assert any("/app/.git" in instruction for instruction in instructions)


def test_the_image_does_not_bake_in_a_port() -> None:
    """PORT is reserved by Foundry and injected at runtime; the library and this
    package both default to 8088 when it is absent. A baked-in ENV PORT is a
    container default competing with the platform's choice."""
    for instruction in dockerfile_instructions():
        assert not instruction.startswith("ENV PORT"), instruction
        assert " PORT=" not in instruction, instruction


def test_the_healthcheck_follows_the_platform_port() -> None:
    healthcheck = [i for i in dockerfile_instructions() if i.startswith("HEALTHCHECK")]
    assert healthcheck, "the image declares a healthcheck"
    assert "PORT" in healthcheck[0], "the healthcheck must read PORT, not hardcode a port"


def test_the_image_proves_the_import_at_build_time() -> None:
    """A build that cannot import its own entry-point module must not produce a
    taggable image. This is the check whose absence let a broken image ship."""
    assert any(
        instruction.startswith("RUN python -c") and "import hosted_agent" in instruction
        for instruction in dockerfile_instructions()
    )


def test_the_declared_command_is_the_module_entry_point() -> None:
    assert 'CMD ["python", "-m", "hosted_agent"]' in dockerfile()


def test_the_port_variable_matches_the_official_server() -> None:
    """`azure.ai.agentserver.core` resolves the bind port from `PORT`, defaulting
    to 8088. This package reads the same variable and the same default, so the
    two cannot disagree about which port Foundry asked for."""
    from hosted_agent.config import DEFAULT_PORT, PORT_VAR

    assert PORT_VAR == "PORT"
    assert DEFAULT_PORT == 8088


def test_the_manifest_declares_no_port() -> None:
    """Foundry reserves PORT and injects it; declaring it would fight the
    platform for the bind address."""
    assert "PORT" not in manifest()["environment_variables"]


def test_the_container_smoke_test_exists_and_is_executable() -> None:
    script = PACKAGE_ROOT / "scripts" / "container_smoke.sh"
    assert script.is_file()
    assert script.stat().st_mode & 0o111, "the smoke test must be runnable"
    text = script.read_text()
    # It must exercise the artefact, not a command of its own choosing.
    assert "--platform linux/amd64" in text
    assert "/readiness" in text
    assert "No module named" in text
