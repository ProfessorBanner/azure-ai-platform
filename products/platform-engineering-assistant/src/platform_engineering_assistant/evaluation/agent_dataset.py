"""The versioned agent evaluation set: cases, stimuli and expectations.

WHAT AN AGENT CASE IS, AND WHY IT CARRIES A SCRIPTED DECISION
--------------------------------------------------------------
A generation case is a question and an expected disposition. An agent case needs
one more thing, and the reason is the honest core of this whole suite.

Two different properties are being measured here, and they belong to different
parts of the system:

  WHAT THE MODEL CHOOSES   which tool, or none, for a given question. A property
                           of the model. Measurable only against a real one.

  WHAT THE APPLICATION DOES   given a proposal — including a hostile or absurd
                           one — does policy rule correctly, does approval hold,
                           does anything execute that should not, is the
                           trajectory well formed. A property of the server.

The second is where every safety guarantee lives, and it must be provable
offline, on a laptop, with no model and no Azure. So each case carries a
`scripted_decision`: the proposal the model is TAKEN to have made. In fake mode
that proposal is injected directly, which lets the suite ask the question that
matters most — *if the model were fully compromised and proposed this, what would
the application do?* — and answer it deterministically, every commit.

In live mode the scripted decision is not used. The real model proposes, and the
model-side metrics (tool selection, unnecessary tool calls, task success) become
meaningful for the first time. The evaluation policy marks each threshold with
the modes it applies to, exactly as `evaluation_policy_v1.json` does.

This is deliberately NOT a benchmark of agent quality. It is a control suite with
a quality section that only runs live. See `Dataset.limitations`.

REDACTION
---------
Case questions and injected strings live in this file because a dataset must
contain its stimuli. Nothing derived from them — no outcome, no metric, no
report row — carries them back out. `AgentCaseOutcome` has no field for a
question, an answer or a tool argument.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from platform_engineering_assistant.agent.domain import (
    MAX_TOOL_ITERATIONS,
    AgentDecision,
    AgentOutcomeKind,
    DecisionKind,
    DenialReason,
    PolicyDecision,
    ToolArgument,
    ToolRiskLevel,
)
from platform_engineering_assistant.config import PRODUCT_ROOT
from platform_engineering_assistant.domain import RefusalReason
from platform_engineering_assistant.errors import ConfigurationError

DEFAULT_AGENT_DATASET_PATH = PRODUCT_ROOT / "evaluation" / "agent_v1.json"

# The composition the set promises, asserted on load. Phrased as exact counts
# for the reason the generation set gives: the adversarial coverage IS the
# reason the suite exists, and a well-meaning edit that replaced an injection
# case with another happy path would weaken it without failing anything.
REQUIRED_CASE_COUNT = 22
REQUIRED_BY_CATEGORY = {
    "tool-use": 4,
    "no-tool": 2,
    "refusal": 2,
    "approval": 3,
    "adversarial": 8,
    "reliability": 3,
}

# Every adversarial technique the phase brief requires, each pinned to at least
# one case by tag. A technique losing its last case is a load failure, not a
# quietly smaller suite.
REQUIRED_ADVERSARIAL_TAGS = (
    "prompt-injection",
    "tool-instruction-injection",
    "risk-level-tampering",
    "approval-bypass",
    "exfiltration",
    "conflicting-evidence",
    "scope-substitution",
    "argument-tampering",
)
REQUIRED_RELIABILITY_TAGS = ("tool-timeout", "malformed-tool-result", "tool-failure")


class ToolBehaviour(StrEnum):
    """How the tool is made to behave for a case.

    The reliability and evidence-shaped adversarial cases need a tool that
    misbehaves in a specific way. Injecting the behaviour at the TOOL boundary,
    rather than mocking the executor, means the real executor, the real timeout
    and the real retry classification all still run — which is the part being
    tested.
    """

    NORMAL = "normal"
    TIMEOUT = "timeout"
    MALFORMED = "malformed"
    PERMANENT_FAILURE = "permanent_failure"
    INJECTED_INSTRUCTIONS = "injected_instructions"
    CONFLICTING_EVIDENCE = "conflicting_evidence"


class ScriptedDecision(BaseModel):
    """The proposal a case takes the model to have made. Fake mode only."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: DecisionKind
    tool_name: str | None = None
    tool_arguments: dict[str, str] = Field(default_factory=dict)
    claimed_risk_level: ToolRiskLevel | None = None
    refusal_reason: RefusalReason | None = None

    def as_decision(self) -> AgentDecision:
        """Build the untrusted proposal.

        Deliberately routed through the real `AgentDecision` model, so a case
        cannot script something the wire schema would have rejected — that would
        be testing a proposal the application could never actually receive.
        """
        return AgentDecision(
            kind=self.kind,
            tool_name=self.tool_name,
            # The dataset keeps arguments as a readable MAP — it is
            # version-controlled server-side fixture data, not model output, and
            # a list of pairs would make every case harder to read for no gain.
            # The wire encoding is applied here, on the way in.
            tool_arguments=[
                ToolArgument(name=name, value=value) for name, value in self.tool_arguments.items()
            ],
            claimed_risk_level=self.claimed_risk_level,
            refusal_reason=self.refusal_reason,
        )


class AgentCase(BaseModel):
    """One labelled agent case: a question, a stimulus and its expectations."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    category: str = Field(min_length=1)
    tags: tuple[str, ...] = ()

    scripted_decision: ScriptedDecision
    tool_behaviour: ToolBehaviour = ToolBehaviour.NORMAL

    # --- what the application must do -------------------------------------
    expected_outcome: AgentOutcomeKind
    expected_policy_decision: PolicyDecision | None = None
    expected_denial_reason: DenialReason | None = None
    expected_refusal_reasons: tuple[RefusalReason, ...] = ()

    # `expected_tool` is the MODEL-side expectation and is scored only in live
    # mode. In fake mode the tool is whatever the case scripted, so scoring it
    # would be scoring the fixture against itself.
    expected_tool: str | None = None
    expects_tool_use: bool = Field(
        default=False, description="Whether a correct model would consult a tool at all."
    )

    expects_execution: bool = Field(
        default=False, description="Whether a tool may legitimately RUN for this case."
    )
    expects_citations: bool = False
    max_expected_iterations: int = Field(default=MAX_TOOL_ITERATIONS, ge=0, le=MAX_TOOL_ITERATIONS)

    prohibited_substrings: tuple[str, ...] = ()
    rationale: str = Field(min_length=1, description="Written provenance for this case.")

    @model_validator(mode="after")
    def expectations_must_be_internally_consistent(self) -> AgentCase:
        """Reject a case that could never be scored, or that scores itself.

        Each rule below exists because the alternative is a case that passes
        without testing anything.
        """
        if self.expected_outcome is AgentOutcomeKind.DENIED:
            if self.expected_denial_reason is None:
                raise ValueError("a denied case must name the denial reason it expects")
            if self.expects_execution:
                raise ValueError("a denied case must not expect execution")

        if self.expected_outcome is AgentOutcomeKind.REFUSED and not self.expected_refusal_reasons:
            raise ValueError("a refused case must allow at least one refusal reason")

        if self.expected_outcome is AgentOutcomeKind.APPROVAL_REQUIRED:
            if self.expects_execution:
                raise ValueError(
                    "an approval-required case must not expect execution: the whole "
                    "point is that nothing ran"
                )
            if self.expected_policy_decision is not PolicyDecision.REQUIRE_APPROVAL:
                raise ValueError("an approval-required case must expect a require_approval verdict")

        if self.expects_citations and self.expected_outcome is not AgentOutcomeKind.ANSWERED:
            raise ValueError("only an answered case may expect citations")

        if self.expects_tool_use and not self.expected_tool:
            raise ValueError("a case expecting tool use must name the tool a correct model picks")
        if self.expected_tool and not self.expects_tool_use:
            raise ValueError("a case naming an expected tool must expect tool use")

        return self

    @property
    def is_adversarial(self) -> bool:
        return self.category == "adversarial"


class AgentDatasetProvenance(BaseModel):
    """How the set was written, and what it does not prove."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    authored: str
    authoring_rule: str
    relationship_to_generation_set: str
    what_fake_mode_proves: str
    what_fake_mode_cannot_prove: str
    limitations: tuple[str, ...] = Field(min_length=1)


class AgentDataset(BaseModel):
    """A loaded, validated agent dataset and the hash of the bytes it came from."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_id: str = Field(min_length=1)
    dataset_version: int = Field(ge=1)
    corpus_version_at_authoring: int = Field(ge=1)
    provenance: AgentDatasetProvenance
    cases: tuple[AgentCase, ...] = Field(min_length=1)
    content_sha256: str = Field(min_length=64, max_length=64)

    @property
    def limitations(self) -> tuple[str, ...]:
        return self.provenance.limitations

    @property
    def adversarial_cases(self) -> tuple[AgentCase, ...]:
        return tuple(case for case in self.cases if case.is_adversarial)

    def cases_tagged(self, tag: str) -> tuple[AgentCase, ...]:
        return tuple(case for case in self.cases if tag in case.tags)


def _assert_composition(cases: tuple[AgentCase, ...]) -> None:
    """Enforce the coverage the set promises."""
    if len(cases) != REQUIRED_CASE_COUNT:
        raise ConfigurationError(
            f"The agent dataset must contain exactly {REQUIRED_CASE_COUNT} cases; "
            f"found {len(cases)}."
        )

    identifiers = [case.case_id for case in cases]
    if len(set(identifiers)) != len(identifiers):
        raise ConfigurationError("Agent case identifiers must be unique.")

    for category, required in sorted(REQUIRED_BY_CATEGORY.items()):
        actual = sum(1 for case in cases if case.category == category)
        if actual != required:
            raise ConfigurationError(
                f"The agent dataset must contain exactly {required} {category} case(s); "
                f"found {actual}."
            )

    tagged = {tag for case in cases for tag in case.tags}
    for tag in (*REQUIRED_ADVERSARIAL_TAGS, *REQUIRED_RELIABILITY_TAGS):
        if tag not in tagged:
            raise ConfigurationError(
                f"The agent dataset must retain coverage of '{tag}'; no case carries that tag."
            )


def load_agent_dataset(path: Path | None = None) -> AgentDataset:
    """Load, validate and hash the versioned agent dataset.

    Raises:
        ConfigurationError: if the file is missing, malformed, fails schema
            validation, or does not hold the required composition. Messages name
            the rule that failed and never quote a question or an argument.
    """
    dataset_path = DEFAULT_AGENT_DATASET_PATH if path is None else path

    try:
        raw_bytes = dataset_path.read_bytes()
    except OSError as exc:
        raise ConfigurationError(f"Agent dataset could not be read: {dataset_path.name}") from exc

    try:
        raw = json.loads(raw_bytes)
    except json.JSONDecodeError as exc:
        raise ConfigurationError(
            f"{dataset_path.name} is not valid JSON: {exc.msg} (line {exc.lineno})"
        ) from exc

    if not isinstance(raw, dict):
        raise ConfigurationError(f"{dataset_path.name} must contain a JSON object.")

    payload = {key: value for key, value in raw.items() if not key.startswith("$")}
    payload["content_sha256"] = hashlib.sha256(raw_bytes).hexdigest()

    try:
        dataset = AgentDataset.model_validate(payload)
    except ValidationError as exc:
        locations = sorted({".".join(str(part) for part in error["loc"]) for error in exc.errors()})
        raise ConfigurationError(
            f"{dataset_path.name} failed schema validation at: {', '.join(locations)}"
        ) from exc

    _assert_composition(dataset.cases)
    return dataset


__all__ = [
    "REQUIRED_ADVERSARIAL_TAGS",
    "REQUIRED_BY_CATEGORY",
    "REQUIRED_CASE_COUNT",
    "AgentCase",
    "AgentDataset",
    "AgentDatasetProvenance",
    "ScriptedDecision",
    "ToolBehaviour",
    "load_agent_dataset",
]
