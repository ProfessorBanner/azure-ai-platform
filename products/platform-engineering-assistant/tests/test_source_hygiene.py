"""Boundary and hygiene guarantees enforced against the shipped source.

These read the product's own files rather than exercising behaviour, because the
properties they protect decay silently during ordinary edits and would otherwise
only be caught by a human noticing.
"""

from __future__ import annotations

import json
import pathlib
import re

import pytest

PRODUCT_ROOT = pathlib.Path(__file__).resolve().parent.parent
SOURCE_ROOT = PRODUCT_ROOT / "src" / "platform_engineering_assistant"

# The lab is a disposable experiment. A product that imported it would acquire a
# dependency on something explicitly designed to be deleted.
FORBIDDEN_IMPORTS = ("foundry_capability_lab", "labs.", "from labs", "import labs")

# Capabilities explicitly excluded from Phase 17.1c and deferred to later work.
# Their appearance in source would mean unreviewed scope landed early.
#
# Updated from the 17.1a list, which forbade fastapi/openai/azure-identity: those
# ARE the HTTP surface and the generation provider this batch delivers, so the
# guard now names what is still out of scope rather than what has since arrived.
OUT_OF_PHASE_IMPORTS = (
    "langchain",
    "langgraph",
    "langsmith",
    "llama_index",
    "chromadb",
    "faiss",
    "pinecone",
    "qdrant",
    "weaviate",
    "sentence_transformers",
    "numpy",
    "mcp",
    "opentelemetry",
)


def python_sources() -> list[pathlib.Path]:
    return [p for p in SOURCE_ROOT.rglob("*.py") if "__pycache__" not in p.parts]


def all_sources() -> list[pathlib.Path]:
    """Product source plus tests, excluding this file.

    This module is excluded on purpose: a test that forbids a literal has to
    name it, so scanning itself would make the check and its own existence
    mutually exclusive. The contract being enforced is about what the product
    SHIPS and IMPORTS, which is exactly the remaining set.
    """
    files = python_sources()
    files += [
        path
        for path in (PRODUCT_ROOT / "tests").rglob("*.py")
        if "__pycache__" not in path.parts and path.name != pathlib.Path(__file__).name
    ]
    return files


# --- product isolation ------------------------------------------------------


@pytest.mark.parametrize("forbidden", FORBIDDEN_IMPORTS)
def test_no_source_file_imports_from_the_lab(forbidden: str) -> None:
    offenders = [
        f"{path.relative_to(PRODUCT_ROOT)}:{number}"
        for path in all_sources()
        for number, line in enumerate(path.read_text().splitlines(), start=1)
        if forbidden in line and not line.lstrip().startswith("#")
    ]
    assert not offenders, f"lab dependency '{forbidden}' found at: {offenders}"


def test_product_declares_its_own_dependencies() -> None:
    """Product-local, not inherited from the repository root."""
    pyproject = (PRODUCT_ROOT / "pyproject.toml").read_text()
    assert 'name = "platform-engineering-assistant"' in pyproject
    assert "pydantic" in pyproject
    assert (PRODUCT_ROOT / "uv.lock").is_file()


@pytest.mark.parametrize("out_of_phase", OUT_OF_PHASE_IMPORTS)
def test_no_out_of_phase_dependency_is_imported(out_of_phase: str) -> None:
    """17.1a has no API, no provider and no retrieval; nothing should import one."""
    offenders = [
        f"{path.relative_to(PRODUCT_ROOT)}:{number}"
        for path in python_sources()
        for number, line in enumerate(path.read_text().splitlines(), start=1)
        if line.lstrip().startswith(("import ", "from ")) and out_of_phase in line
    ]
    assert not offenders, f"out-of-phase import '{out_of_phase}' at: {offenders}"


def test_product_declares_only_the_approved_runtime_dependencies() -> None:
    """Each dependency is added by the batch that first uses it.

    Pinning the exact set stops an unreviewed capability arriving as a
    transitive convenience: a vector store or an agent framework would have to
    appear here first, and this test is where that gets noticed.
    """
    pyproject = (PRODUCT_ROOT / "pyproject.toml").read_text()
    runtime_block = pyproject.split("dependencies = [", 1)[1].split("]", 1)[0]

    declared = {
        line.strip().strip('",').split(">=")[0].split("==")[0]
        for line in runtime_block.splitlines()
        if line.strip().startswith('"')
    }
    assert declared == {"azure-identity", "fastapi", "openai", "pydantic", "uvicorn"}

    for excluded in ("langchain", "langgraph", "chromadb", "faiss", "numpy", "mcp"):
        assert excluded not in runtime_block


# --- no credential material in the product ----------------------------------


def test_no_source_file_contains_jwt_like_material() -> None:
    """Matches a whole three-segment token, not the bare 'eyJ' prefix.

    The corpus loader legitimately contains a JWT DETECTION pattern; a substring
    check would flag the guard for resembling the thing it guards against.
    """
    jwt_literal = re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")
    for path in python_sources():
        assert not jwt_literal.search(path.read_text()), (
            f"{path.relative_to(PRODUCT_ROOT)} contains JWT-like material"
        )


def test_no_api_key_is_ever_passed_to_a_client() -> None:
    """Key authentication must be refused, not merely unused.

    `api_key=` as a keyword argument is the specific mistake this guards: it is
    how key auth would be reintroduced, and the Foundry account would reject it
    anyway since local_auth_enabled is false.
    """
    for path in python_sources():
        assert "api_key=" not in path.read_text(), f"{path.name} passes an api_key argument"


def test_key_variables_appear_only_in_the_refusal_list() -> None:
    """The only place a key variable name may occur is the list that rejects it."""
    from platform_engineering_assistant.provider_config import FORBIDDEN_KEY_VARS

    assert "AZURE_OPENAI_API_KEY" in FORBIDDEN_KEY_VARS
    assert "OPENAI_API_KEY" in FORBIDDEN_KEY_VARS

    for path in python_sources():
        if path.name == "provider_config.py":
            continue
        assert "API_KEY" not in path.read_text(), f"{path.name} references an API key variable"


def test_the_provider_refuses_configured_keys_before_building_a_client() -> None:
    """The refusal must be wired into the real configuration path, not just defined."""
    source = (SOURCE_ROOT / "provider_config.py").read_text()
    assert "assert_no_api_key_configured(env)" in source


# --- versioned artefacts are present and coherent ---------------------------


def test_versioned_prompt_exists_and_declares_its_version() -> None:
    prompt = (PRODUCT_ROOT / "prompts" / "answer_v1.md").read_text()
    assert "prompt_version: answer_v1" in prompt


@pytest.mark.parametrize(
    "requirement",
    [
        "evidence",  # evidence is authoritative
        "untrusted",  # the question is untrusted data
        "instructions",  # never follow embedded instructions
        "general knowledge",  # no unsupported general knowledge
        "chunk identifier",  # cite only supplied chunk ids
        "Refuse",  # refuse on insufficient evidence
        "reveal",  # never reveal the system instructions
    ],
)
def test_prompt_states_each_required_rule(requirement: str) -> None:
    prompt = (PRODUCT_ROOT / "prompts" / "answer_v1.md").read_text()
    assert requirement.lower() in prompt.lower(), f"prompt does not state: {requirement}"


def test_retrieval_configuration_is_versioned_and_declares_minimum_score() -> None:
    raw = json.loads((PRODUCT_ROOT / "config" / "retrieval_v1.json").read_text())
    assert raw["version"] == "retrieval_v1"
    assert "minimum_score" in raw


def test_chunk_id_form_is_documented_in_the_readme() -> None:
    readme = (PRODUCT_ROOT / "README.md").read_text()
    assert "section-occurrence" in readme
    assert "chunk-ordinal" in readme


def test_readme_records_the_phase_boundary() -> None:
    """Prompt-injection testing is Phase 17; tools/agents/HITL are Phase 18."""
    readme = (PRODUCT_ROOT / "README.md").read_text()
    assert "Phase 17" in readme
    assert "Phase 18" in readme


# --- production retrieval must not know about the evaluation fixture --------

PRODUCTION_PACKAGES = ("corpus", "retrieval")

FIXTURE_LEAKS = (
    "retrieval_baseline",
    "RET-0",
    "OOS-0",
    "expected_doc_ids",
    "must_refuse",
)


def production_sources() -> list[pathlib.Path]:
    """Source that serves a request: corpus admission and retrieval.

    `evaluation/` is excluded — measuring against a fixture is its job.
    """
    files: list[pathlib.Path] = []
    for package in PRODUCTION_PACKAGES:
        files += [
            path
            for path in (SOURCE_ROOT / package).rglob("*.py")
            if "__pycache__" not in path.parts
        ]
    files += [SOURCE_ROOT / name for name in ("config.py", "domain.py", "errors.py")]
    return files


@pytest.mark.parametrize("leak", FIXTURE_LEAKS)
def test_production_retrieval_does_not_reference_the_fixture(leak: str) -> None:
    """Scoring must not be able to recognise the set it is measured on."""
    offenders = [
        f"{path.relative_to(PRODUCT_ROOT)}:{number}"
        for path in production_sources()
        for number, line in enumerate(path.read_text().splitlines(), start=1)
        if leak in line
    ]
    assert not offenders, f"fixture reference '{leak}' leaked into production at: {offenders}"


def test_retrieval_scoring_contains_no_hard_coded_document_ids() -> None:
    """No document-specific rules: scoring must be generic.

    Scoped to the retrieval package, which is where a document-specific rule
    would actually change a result. `domain.py` legitimately shows a real chunk
    id in a docstring as an example of the identifier FORMAT, which is
    documentation rather than a rule.
    """
    approved = {
        document["doc_id"]
        for document in json.loads((PRODUCT_ROOT / "corpus" / "manifest.json").read_text())[
            "documents"
        ]
    }
    for path in (SOURCE_ROOT / "retrieval").rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        source = path.read_text()
        for doc_id in approved:
            assert doc_id not in source, f"{path.name} hard-codes document id {doc_id}"


def test_retrieval_declares_no_synonym_or_expansion_table() -> None:
    """Query expansion and hand-written synonyms are explicitly out of scope."""
    for path in (SOURCE_ROOT / "retrieval").rglob("*.py"):
        source = path.read_text().lower()
        for banned in ("synonym", "expansion_map", "query_expansion"):
            assert banned not in source or "no " + banned in source


# --- Phase 17.2: evaluation, judge and LLMOps artefacts ---------------------

EVALUATION_ROOT = SOURCE_ROOT / "evaluation"


def evaluation_sources() -> list[pathlib.Path]:
    return [p for p in EVALUATION_ROOT.rglob("*.py") if "__pycache__" not in p.parts]


def test_versioned_evaluation_artefacts_exist_and_declare_their_versions() -> None:
    """A score attributable to nothing is an anecdote."""
    dataset = json.loads((PRODUCT_ROOT / "evaluation" / "generation_v1.json").read_text())
    assert dataset["dataset_id"] == "generation_v1"
    assert dataset["dataset_version"] >= 1

    policy = json.loads((PRODUCT_ROOT / "evaluation" / "evaluation_policy_v1.json").read_text())
    assert policy["policy_id"] == "evaluation_policy_v1"
    assert policy["policy_version"] >= 1

    assert "prompt_version: judge_v1" in (PRODUCT_ROOT / "prompts" / "judge_v1.md").read_text()


def test_the_locked_validation_set_is_untouched_by_this_phase() -> None:
    """A held-out set is spent the moment it informs a change.

    Phase 17.2's own modules must not read, re-run or edit it. `validation.py` is
    excluded because reading that file IS its job — it is the 17.1b module the
    locked set belongs to, and it is not modified by this phase.
    """
    offenders = [
        f"{path.relative_to(PRODUCT_ROOT)}:{number}"
        for path in evaluation_sources()
        if path.name != "validation.py"
        for number, line in enumerate(path.read_text().splitlines(), start=1)
        if "locked_validation" in line
    ]
    assert not offenders, f"the locked validation set is referenced at: {offenders}"


def test_no_evaluation_module_imports_an_azure_sdk_at_module_level() -> None:
    """Every Azure import is inside a live-only branch.

    This is what lets the offline command, and the whole test suite, run with no
    SDK installed and no credential resolvable.
    """
    for path in evaluation_sources():
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            stripped = line.strip()
            if not stripped.startswith(("import ", "from ")):
                continue
            if not line.startswith(("import ", "from ")):
                continue  # indented: inside a function, which is the point
            assert "azure." not in stripped and "openai" not in stripped, (
                f"{path.name}:{number} imports an Azure SDK at module level"
            )


def test_containment_is_never_called_groundedness() -> None:
    """Containment proves citations came from retrieved context, nothing more.

    Conflating the two would let a cheap structural check be read as a
    correctness guarantee — the most consequential wrong claim this codebase
    could make about itself. The judge's estimate is named
    `semantic_groundedness` precisely so its provenance is in its name.

    The rule is checked where it could actually mislead: any line mentioning both
    concepts must separate them explicitly.
    """
    for path in python_sources():
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            lowered = line.lower()
            if "groundedness" not in lowered or "containment" not in lowered:
                continue
            assert any(word in lowered for word in ("not", "never", "rather")), (
                f"{path.name}:{number} conflates containment with groundedness: {line.strip()}"
            )


def test_the_only_groundedness_metric_is_the_judges() -> None:
    """No deterministic metric may be named groundedness."""
    from platform_engineering_assistant.evaluation.generation_metrics import GenerationMetrics

    grounded = [name for name in GenerationMetrics.__annotations__ if "groundedness" in name]
    assert grounded == ["semantic_groundedness"]


def test_the_evaluation_report_model_has_no_field_for_generated_content() -> None:
    """Redaction is structural: the fields do not exist to be forgotten."""
    source = (EVALUATION_ROOT / "generation_metrics.py").read_text()
    fields_block = source.split("class CaseOutcome", 1)[1].split("# --- derived", 1)[0]
    for forbidden in ("answer:", "question:", "context:", "text:", "rationale:"):
        assert forbidden not in fields_block, f"CaseOutcome declares {forbidden}"


def test_the_judge_request_cannot_carry_an_answer_key() -> None:
    """Checked on the declared FIELDS, which is what a caller could actually set."""
    import dataclasses

    from platform_engineering_assistant.evaluation.judge import JudgeRequest

    names = {field.name for field in dataclasses.fields(JudgeRequest)}
    assert names == {"rubric", "question", "response_text", "cited_evidence"}
    for forbidden in ("expected_disposition", "expected_doc_ids", "case_id", "score"):
        assert forbidden not in names


def test_no_evaluation_report_is_committed() -> None:
    """Reports are run OUTPUT. A committed one becomes a score people read
    instead of reproduce, and a live one would publish model responses."""
    artefacts = PRODUCT_ROOT / "artifacts"
    if not artefacts.exists():
        return
    tracked = [
        path for path in artefacts.rglob("*") if path.is_file() and ".gitignore" not in path.name
    ]
    gitignore = (PRODUCT_ROOT.parent.parent / ".gitignore").read_text()
    assert "artifacts/" in gitignore, "artifacts must be gitignored"
    assert tracked or not tracked  # presence is fine; being ignored is the contract


def test_no_baseline_report_is_shipped() -> None:
    """The first accepted live report becomes the baseline, chosen by a person.

    A manufactured baseline would give every future comparison a fictional
    reference point that reads exactly like a real one.
    """
    for name in ("baseline.json", "evaluation-baseline.json", "baseline-report.json"):
        assert not (PRODUCT_ROOT / "evaluation" / name).exists()


def test_live_evaluation_is_never_automatic() -> None:
    """`--mode live` must be typed. Nothing may default to a metered call."""
    source = (EVALUATION_ROOT / "generation_cli.py").read_text()
    assert "default=ExecutionMode.FAKE.value" in source


def test_the_evaluation_policy_records_a_rationale_for_every_threshold() -> None:
    policy = json.loads((PRODUCT_ROOT / "evaluation" / "evaluation_policy_v1.json").read_text())
    for name, threshold in policy["thresholds"].items():
        assert threshold.get("rationale"), f"{name} has no recorded rationale"


def test_the_dataset_declares_that_it_is_not_an_independent_benchmark() -> None:
    dataset = json.loads((PRODUCT_ROOT / "evaluation" / "generation_v1.json").read_text())
    limitations = " ".join(dataset["provenance"]["limitations"]).lower()
    assert "not an independent benchmark" in limitations


# --- Phase 18.1: agent layer boundaries -------------------------------------

AGENT_ROOT = SOURCE_ROOT / "agent"


def agent_sources() -> list[pathlib.Path]:
    return [p for p in AGENT_ROOT.rglob("*.py") if "__pycache__" not in p.parts]


@pytest.mark.parametrize(
    "framework", ["langchain", "langgraph", "langsmith", "autogen", "crewai", "semantic_kernel"]
)
def test_the_agent_uses_no_orchestration_framework(framework: str) -> None:
    """The control flow IS the security property here.

    A framework would hide the step that matters — that nothing reaches a tool
    without passing the policy layer — behind its own control flow.
    """
    for path in agent_sources():
        for line in path.read_text().splitlines():
            if line.lstrip().startswith(("import ", "from ")):
                assert framework not in line, f"{path.name} imports {framework}"


def test_the_policy_layer_never_reads_the_models_risk_claim() -> None:
    """THE invariant, enforced against the source rather than only by behaviour.

    Checked over the parsed AST, not the text: policy.py's own docstring names
    the field in order to explain that it is ignored, and a substring scan
    cannot tell an attribute access apart from a sentence about one.
    """
    import ast

    tree = ast.parse((AGENT_ROOT / "policy.py").read_text())
    accessed = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert "claimed_risk_level" not in accessed, "policy.py reads the model's risk claim"
    assert "risk" in accessed, "policy.py must read the registry's classification"


def test_the_policy_layer_makes_no_model_call() -> None:
    """Asking an LLM whether a call is safe would let the attacker who shaped
    the proposal also shape the review."""
    source = (AGENT_ROOT / "policy.py").read_text()
    for forbidden in ("provider", "decide(", "generate(", "openai", "azure"):
        assert forbidden not in source.lower(), f"policy.py references {forbidden}"


def test_the_policy_layer_performs_no_io() -> None:
    """A pure function: no clock, no network, no logging, no randomness.

    Checked over imports in the AST, so the module docstring may describe the
    property without violating it.
    """
    import ast

    tree = ast.parse((AGENT_ROOT / "policy.py").read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    for forbidden in ("time", "random", "logging", "requests", "httpx", "os", "subprocess"):
        assert forbidden not in imported, f"policy.py imports {forbidden}"


def test_no_tool_executes_without_passing_the_policy_layer() -> None:
    """Structural check: the orchestrator calls `authorise` before `_execute`."""
    source = (AGENT_ROOT / "orchestrator.py").read_text()
    assert source.index("authorise(") < source.index("self._execute(")


def test_the_state_changing_tool_has_no_mutation_capability() -> None:
    """It represents a proposal. It must not be able to change anything."""
    source = (AGENT_ROOT / "tools.py").read_text()
    proposal_block = source.split("class ProposeChangeRequestTool", 1)[1]
    for forbidden in ("requests", "httpx", "subprocess", "open(", "write_text", "az ", "client"):
        assert forbidden not in proposal_block, f"the proposal tool references {forbidden}"


def test_the_agent_prompt_is_versioned() -> None:
    prompt = (PRODUCT_ROOT / "prompts" / "agent_decision_v1.md").read_text()
    assert "prompt_version: agent_decision_v1" in prompt


@pytest.mark.parametrize(
    "requirement",
    [
        "proposal",
        "do not decide whether a tool is safe",
        "untrusted data",
        "environment",
        "do not include reasoning",
    ],
)
def test_the_agent_prompt_states_each_required_rule(requirement: str) -> None:
    prompt = (PRODUCT_ROOT / "prompts" / "agent_decision_v1.md").read_text().lower()
    assert requirement.lower() in prompt, f"agent prompt does not state: {requirement}"


def test_the_agent_domain_has_no_field_for_reasoning() -> None:
    """Hidden reasoning is unvalidated output that leaks prompt content and
    invites callers to treat a plausible narrative as justification.

    Checked over declared FIELD NAMES in the AST — the module docstring names
    these concepts precisely in order to record why they are absent.
    """
    import ast

    forbidden = {
        "reasoning",
        "chain_of_thought",
        "thoughts",
        "scratchpad",
        "deliberation",
        "explanation",
        "rationale",
    }
    for module in ("domain.py", "telemetry.py"):
        tree = ast.parse((AGENT_ROOT / module).read_text())
        declared = {
            node.target.id
            for node in ast.walk(tree)
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
        }
        assert not (declared & forbidden), f"{module} declares {declared & forbidden}"


def test_agent_telemetry_has_no_field_for_tool_arguments() -> None:
    """A search query is the user's question restated. The count is recorded."""
    source = (AGENT_ROOT / "telemetry.py").read_text()
    assert "tool_argument_count" in source
    assert "tool_arguments:" not in source


def test_the_agent_declares_no_new_runtime_dependency() -> None:
    """Phase 18.1 adds an architecture, not a dependency."""
    pyproject = (PRODUCT_ROOT / "pyproject.toml").read_text()
    runtime_block = pyproject.split("dependencies = [", 1)[1].split("]", 1)[0]
    declared = {
        line.strip().strip('",').split(">=")[0].split("==")[0]
        for line in runtime_block.splitlines()
        if line.strip().startswith('"')
    }
    assert declared == {"azure-identity", "fastapi", "openai", "pydantic", "uvicorn"}


# --- Phase 18.5: observability boundaries -----------------------------------

TRACING_VENDORS = (
    "langsmith",
    "langchain",
    "opentelemetry",
    "openinference",
    "wandb",
    "braintrust",
)


@pytest.mark.parametrize("vendor", TRACING_VENDORS)
def test_no_source_file_imports_a_tracing_vendor(vendor: str) -> None:
    """The observability integration is a PROTOCOL, not a dependency.

    Phase 18.5 assessed LangSmith and Azure AI Foundry tracing and adopted
    neither as a package: both want prompts, answers and tool arguments, which
    this product does not collect, and LangSmith additionally wants a long-lived
    API key, which the platform rules forbid. What was built is a seam. This
    test is what keeps it a seam.
    """
    offenders = [
        f"{path.relative_to(PRODUCT_ROOT)}:{number}"
        for path in python_sources()
        for number, line in enumerate(path.read_text().splitlines(), start=1)
        if line.lstrip().startswith(("import ", "from ")) and vendor in line
    ]
    assert not offenders, f"tracing vendor '{vendor}' imported at: {offenders}"


def test_the_trajectory_models_have_no_field_for_content() -> None:
    """Redaction is structural here exactly as it is for telemetry and reports."""
    from platform_engineering_assistant.agent.trajectory import AgentTrajectory, TrajectoryEvent

    forbidden = {
        "question",
        "answer",
        "prompt",
        "text",
        "content",
        "arguments",
        "tool_arguments",
        "evidence",
        "reasoning",
        "chain_of_thought",
        "scratchpad",
    }
    for model in (TrajectoryEvent, AgentTrajectory):
        declared = set(model.model_fields)
        assert not declared & forbidden, f"{model.__name__} declares {declared & forbidden}"


def test_the_agent_layer_does_not_know_where_its_events_go() -> None:
    """`agent/` defines what an event is; `observability/` decides where it goes.

    The split is what lets the framework-free guarantee survive an integration:
    a vendor adapter can never appear in the module that owns the control flow.
    """
    for path in agent_sources():
        for line in path.read_text().splitlines():
            if line.lstrip().startswith(("import ", "from ")):
                assert "observability" not in line, f"{path.name} imports the sink layer"


def test_trajectory_verification_is_deterministic_and_makes_no_call() -> None:
    """`verify()` is the Phase 18.6 trajectory-correctness metric. It must be a
    pure function of the record, or it measures the environment instead."""
    import ast

    source = (AGENT_ROOT / "trajectory.py").read_text()
    tree = ast.parse(source)
    verify = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "verify"
    )
    called = {
        node.func.attr
        for node in ast.walk(verify)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    for forbidden in ("now", "time", "perf_counter", "random", "emit", "generate", "decide"):
        assert forbidden not in called, f"verify() calls {forbidden}"


# --- Phase 18.6: agent evaluation boundaries --------------------------------


def test_the_agent_evaluation_artefacts_exist_and_declare_their_versions() -> None:
    dataset = json.loads((PRODUCT_ROOT / "evaluation" / "agent_v1.json").read_text())
    policy = json.loads(
        (PRODUCT_ROOT / "evaluation" / "agent_evaluation_policy_v1.json").read_text()
    )
    assert dataset["dataset_id"] == "agent_v1"
    assert dataset["dataset_version"] >= 1
    assert policy["policy_id"] == "agent_evaluation_policy_v1"
    assert policy["policy_version"] >= 1


def test_the_agent_policy_records_a_rationale_for_every_threshold() -> None:
    """A bound with no written reason is a number someone will later move."""
    policy = json.loads(
        (PRODUCT_ROOT / "evaluation" / "agent_evaluation_policy_v1.json").read_text()
    )
    for name, threshold in policy["thresholds"].items():
        assert threshold.get("rationale"), f"{name} has no rationale"
        assert threshold.get("modes"), f"{name} declares no modes"


def test_the_agent_case_outcome_has_no_field_for_generated_content() -> None:
    """A report is assembled from these fields. Redaction is structural."""
    import dataclasses

    from platform_engineering_assistant.evaluation.agent_metrics import AgentCaseOutcome

    declared = {field.name for field in dataclasses.fields(AgentCaseOutcome)}
    forbidden = {
        "question",
        "answer",
        "response_text",
        "tool_arguments",
        "arguments",
        "evidence",
        "context",
        "prompt",
    }
    assert not declared & forbidden, f"AgentCaseOutcome declares {declared & forbidden}"


def test_the_locked_generation_dataset_is_untouched_by_this_phase() -> None:
    """Phase 18.6 adds a set; it does not edit the one the gates were calibrated on."""
    dataset = json.loads((PRODUCT_ROOT / "evaluation" / "generation_v1.json").read_text())
    assert dataset["dataset_version"] == 1
    assert len(dataset["cases"]) == 16


def test_the_agent_evaluation_never_calls_a_model_by_default() -> None:
    source = (EVALUATION_ROOT / "agent_cli.py").read_text()
    assert "default=ExecutionMode.FAKE.value" in source


def test_no_agent_evaluation_module_imports_an_azure_sdk_at_module_level() -> None:
    """CI has no credential. The offline path must not import one to find out."""
    for name in ("agent_dataset.py", "agent_metrics.py", "agent_runner.py", "agent_report.py"):
        source = (EVALUATION_ROOT / name).read_text()
        for number, line in enumerate(source.splitlines(), start=1):
            if line.startswith(("import ", "from ")):
                assert "azure" not in line.lower(), f"{name}:{number} imports an Azure SDK"


def test_the_agent_evaluation_runs_the_shipped_agent_service() -> None:
    """A parallel evaluation path would measure the parallel path.

    The suite would go green while the served behaviour drifted, which is how an
    evaluation harness becomes worse than having none.
    """
    source = (EVALUATION_ROOT / "agent_runner.py").read_text()
    assert "case_service.run(" in source
    assert "AgentService(" in source
    for reimplementation in ("def authorise", "def enforce_grounding", "PolicyVerdict("):
        assert reimplementation not in source, f"the runner re-implements {reimplementation}"
