"""The typed tool contract, and the three tools Phase 18.1 ships.

WHY TYPED INPUTS AND OUTPUTS
----------------------------
A `dict[str, Any]` tool interface pushes validation to whoever remembers to do
it, and "whoever remembers" is not a control. Each tool here declares a Pydantic
input model and a Pydantic output model. The model's stringly-typed proposal is
parsed into the input model, and anything that does not fit is a DENIAL — not a
best-effort coercion.

SCOPE IS PART OF THE RESULT, NOT AN INFERENCE
---------------------------------------------
Where a tool returns environment- or scope-specific information, the scope is a
FIELD, not something a reader is expected to deduce from prose. `EvidenceScope`
carries the environment, the component, the authority level and the source
documents that a fact came from.

The reason is a general one about agent design. A fact detached from its
qualifier reads as universally true: "capacity is 10" is a different statement
from "sandbox capacity is 10", and if the qualifier lives somewhere else, a
model asked about production will answer with the sandbox number and cite a real
source while doing it. Making scope structural means the qualifier travels with
the fact and cannot be lost in transit.

It also lets a tool refuse rather than substitute. `lookup_platform_component`
asked for an environment it has no evidence for returns `found=False` and names
the environments it DOES have. Silently answering from a different environment
is the failure this design exists to prevent, and no amount of prompting is a
substitute for the tool simply not doing it.

TOOLS ARE NOT TRUSTED CONTEXT
-----------------------------
A tool result is structured evidence that the application labels and bounds
before any of it reaches the model again. It is never spliced into a prompt as
free-form text.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from platform_engineering_assistant.agent.domain import ToolRiskLevel
from platform_engineering_assistant.corpus.chunking import Chunk
from platform_engineering_assistant.errors import AssistantError, FailureCategory
from platform_engineering_assistant.retrieval.index import BM25Index

# The platform's four-environment vocabulary, from ADR 0005. A closed set on
# purpose: an environment is a governance boundary here, not free text.
KNOWN_ENVIRONMENTS = ("sandbox", "dev", "stg", "prod")

MAX_EVIDENCE_ITEMS = 6
MAX_QUERY_LENGTH = 500


class ToolError(AssistantError):
    """A tool failed to execute. Distinct from being denied by policy."""

    category = FailureCategory.PROVIDER_ERROR


class ToolInput(BaseModel):
    """Base for every tool input. Closed and frozen."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class EvidenceScope(BaseModel):
    """What a piece of evidence is ABOUT, carried alongside what it says.

    `environment` is None when the evidence is genuinely environment-neutral —
    which is a real and common case, and materially different from "we do not
    know". A None here means the source made no environment claim, so no
    environment claim may be built on it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    environment: str | None = Field(
        default=None,
        description="sandbox/dev/stg/prod when the source names one; None when it does not.",
    )
    component: str | None = Field(
        default=None, description="Platform component the evidence describes, when known."
    )
    authority: str = Field(
        description="Corpus authority level: adr, current-state, standard or runbook."
    )
    source_doc_ids: tuple[str, ...] = Field(
        default=(), description="Approved documents this evidence came from."
    )


class ToolOutput(BaseModel):
    """Base for every tool output. Closed and frozen."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class Tool[InputT: ToolInput, OutputT: ToolOutput](ABC):
    """A first-class tool.

    `risk` is declared here, in server-side code, and is read by the policy
    layer from the registry. It is never negotiable at runtime and never
    supplied by a model.
    """

    name: str
    description: str
    risk: ToolRiskLevel
    input_model: type[InputT]
    # Declared so the executor can check what a tool actually returned. A tool
    # that returns the wrong type has failed, and the alternative to noticing is
    # passing an unvalidated object into the grounding step as if it were
    # evidence. Phase 18.6 added this after the malformed-result case found that
    # nothing checked it.
    output_model: type[OutputT]

    def parse(self, arguments: dict[str, str]) -> InputT:
        """Parse untrusted arguments into the typed input.

        Raises:
            ValidationError: on anything the input model rejects. The caller
                turns this into a DENIAL; it is never coerced into a default.
        """
        return self.input_model.model_validate(arguments)

    @abstractmethod
    def run(self, payload: InputT) -> OutputT:
        """Execute. Raises ToolError on failure; never returns a partial result."""


# --- A. search_platform_docs (READ_ONLY) ------------------------------------


class SearchDocsInput(ToolInput):
    query: str = Field(min_length=3, max_length=MAX_QUERY_LENGTH)


class EvidenceItem(BaseModel):
    """One retrieved chunk, with its scope attached."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    chunk_id: str
    doc_id: str
    doc_path: str
    heading_path: str
    score: float
    scope: EvidenceScope


class SearchDocsOutput(ToolOutput):
    evidence: tuple[EvidenceItem, ...] = ()
    result_count: int = 0
    # True when retrieval returned nothing above the no-signal floor. Named for
    # what it is, so a caller cannot mistake "no evidence" for "no answer".
    empty: bool = False


def _environment_of(chunk: Chunk) -> str | None:
    """The environment a chunk actually names, or None.

    Word-boundary matching against the closed platform vocabulary. Returns None
    unless EXACTLY ONE environment is named: a chunk comparing all four
    environments is not evidence about any one of them, and claiming otherwise
    would reintroduce the very substitution this module exists to prevent.
    """
    import re

    blob = f"{chunk.title} {chunk.heading_path} {chunk.text}".lower()
    patterns = {
        "sandbox": r"\bsandbox\b",
        "dev": r"\bdev\b",
        "stg": r"\bstg\b|\bstaging\b",
        "prod": r"\bprod\b|\bproduction\b",
    }
    found = [name for name, pattern in patterns.items() if re.search(pattern, blob)]
    return found[0] if len(found) == 1 else None


def _scope_of(chunk: Chunk) -> EvidenceScope:
    return EvidenceScope(
        environment=_environment_of(chunk),
        component=None,
        authority=chunk.authority,
        source_doc_ids=(chunk.doc_id,),
    )


class SearchPlatformDocsTool(Tool[SearchDocsInput, SearchDocsOutput]):
    """Retrieve approved documentation evidence for a query.

    Delegates to the SAME BM25 index the answering path uses. Nothing about
    retrieval is re-implemented, re-tuned or widened here: top-k and the
    no-signal floor remain server-owned configuration, so an agent cannot
    enlarge the evidence set until an answer appears.
    """

    name = "search_platform_docs"
    description = (
        "Search the approved platform documentation corpus and return structured "
        "evidence with explicit scope. Read-only."
    )
    risk = ToolRiskLevel.READ_ONLY
    input_model = SearchDocsInput
    output_model = SearchDocsOutput

    def __init__(self, index: BM25Index) -> None:
        self._index = index

    def run(self, payload: SearchDocsInput) -> SearchDocsOutput:
        try:
            results = self._index.search(payload.query)
        except AssistantError:
            raise
        except Exception as exception:  # noqa: BLE001 — re-raised as a typed error
            raise ToolError("Documentation search failed.") from exception

        items = tuple(
            EvidenceItem(
                chunk_id=result.chunk.chunk_id,
                doc_id=result.chunk.doc_id,
                doc_path=result.chunk.doc_path,
                heading_path=result.chunk.heading_path,
                score=result.score,
                scope=_scope_of(result.chunk),
            )
            for result in results[:MAX_EVIDENCE_ITEMS]
        )
        return SearchDocsOutput(evidence=items, result_count=len(items), empty=not items)

    def chunks_for(self, payload: SearchDocsInput) -> list[Chunk]:
        """The chunks behind the evidence, for grounding the final answer."""
        return [result.chunk for result in self._index.search(payload.query)[:MAX_EVIDENCE_ITEMS]]


# --- B. lookup_platform_component (READ_ONLY) -------------------------------


class ComponentLookupInput(ToolInput):
    component: str = Field(min_length=2, max_length=100)
    environment: str | None = Field(
        default=None,
        description="One of sandbox, dev, stg, prod. Omit when the question is not scoped.",
    )


class ComponentLookupOutput(ToolOutput):
    component: str
    environment_requested: str | None = None
    found: bool = False
    evidence: tuple[EvidenceItem, ...] = ()
    # Which environments the corpus actually has evidence for. Returned even on
    # a miss, so the caller learns what exists rather than guessing.
    environments_available: tuple[str, ...] = ()
    unsupported_environment: bool = False


class LookupPlatformComponentTool(Tool[ComponentLookupInput, ComponentLookupOutput]):
    """Look up a platform component, honouring the environment asked for.

    THE SCOPE RULE, ENFORCED IN CODE
    --------------------------------
    When an environment is requested and no retrieved evidence names that
    environment, this returns `found=False` and `unsupported_environment=True`.
    It does NOT fall back to another environment's evidence.

    That substitution is the specific failure being designed out: evidence about
    one environment answering a question about another, carrying a real citation
    and therefore reading as verified. A tool that refuses is a control; a prompt
    asking a model to be careful is a preference.
    """

    name = "lookup_platform_component"
    description = (
        "Look up structured information about a known platform component, optionally "
        "scoped to an environment. Returns no result rather than substituting a "
        "different environment's information. Read-only."
    )
    risk = ToolRiskLevel.READ_ONLY
    input_model = ComponentLookupInput
    output_model = ComponentLookupOutput

    def __init__(self, index: BM25Index) -> None:
        self._index = index

    def _retrieve(self, payload: ComponentLookupInput) -> list[Chunk]:
        query = payload.component
        if payload.environment:
            query = f"{payload.environment} {query}"
        return [result.chunk for result in self._index.search(query)[:MAX_EVIDENCE_ITEMS]]

    def run(self, payload: ComponentLookupInput) -> ComponentLookupOutput:
        if payload.environment and payload.environment not in KNOWN_ENVIRONMENTS:
            return ComponentLookupOutput(
                component=payload.component,
                environment_requested=payload.environment,
                found=False,
                unsupported_environment=True,
                environments_available=KNOWN_ENVIRONMENTS,
            )

        try:
            chunks = self._retrieve(payload)
        except AssistantError:
            raise
        except Exception as exception:  # noqa: BLE001 — re-raised as a typed error
            raise ToolError("Component lookup failed.") from exception

        scoped = [chunk for chunk in chunks if _environment_of(chunk) == payload.environment]
        selected = scoped if payload.environment else chunks

        available = tuple(
            sorted({env for chunk in chunks if (env := _environment_of(chunk)) is not None})
        )

        if payload.environment and not scoped:
            # Evidence exists, but none of it is about the environment asked
            # for. Returning the unscoped evidence anyway is the substitution.
            return ComponentLookupOutput(
                component=payload.component,
                environment_requested=payload.environment,
                found=False,
                unsupported_environment=True,
                environments_available=available,
            )

        items = tuple(
            EvidenceItem(
                chunk_id=chunk.chunk_id,
                doc_id=chunk.doc_id,
                doc_path=chunk.doc_path,
                heading_path=chunk.heading_path,
                score=0.0,
                scope=_scope_of(chunk),
            )
            for chunk in selected
        )
        return ComponentLookupOutput(
            component=payload.component,
            environment_requested=payload.environment,
            found=bool(items),
            evidence=items,
            environments_available=available,
        )

    def chunks_for(self, payload: ComponentLookupInput) -> list[Chunk]:
        chunks = self._retrieve(payload)
        if payload.environment:
            return [chunk for chunk in chunks if _environment_of(chunk) == payload.environment]
        return chunks


# --- C. propose_change_request (STATE_CHANGING) -----------------------------


class ProposeChangeInput(ToolInput):
    title: str = Field(min_length=5, max_length=200)
    rationale: str = Field(min_length=10, max_length=2000)
    environment: str | None = Field(default=None)


class ProposeChangeOutput(ToolOutput):
    """Deliberately describes a proposal, never a performed action."""

    accepted: bool = False
    summary: str = ""


class ProposeChangeRequestTool(Tool[ProposeChangeInput, ProposeChangeOutput]):
    """Represent a proposed consequential change. PERFORMS NO MUTATION.

    This tool exists to prove the APPROVAL PATH, not to change anything. It
    writes nothing, calls nothing and has no side effect of any kind — and in
    Phase 18.1 `run` is never reached, because policy stops a state-changing
    tool before execution.

    `run` is nonetheless implemented as a hard failure rather than left absent.
    If a future refactor ever routes past the policy layer, this raises instead
    of quietly doing something, and a test asserts it is never called.
    """

    name = "propose_change_request"
    description = (
        "Record a proposed change to the platform for human review. Performs no "
        "mutation and always requires human approval."
    )
    risk = ToolRiskLevel.STATE_CHANGING
    input_model = ProposeChangeInput
    output_model = ProposeChangeOutput

    def run(self, payload: ProposeChangeInput) -> ProposeChangeOutput:
        raise ToolError(
            "propose_change_request must never execute automatically; it requires approval."
        )

    @staticmethod
    def summarise(payload: ProposeChangeInput) -> str:
        """The human-facing approval summary, built by the SERVER.

        Assembled from the validated typed input, not from anything the model
        wrote in prose, so the text a person approves is text the application
        controls.
        """
        scope = f" [{payload.environment}]" if payload.environment else ""
        return f"Proposed change{scope}: {payload.title}"


def parse_or_none[InputT: ToolInput, OutputT: ToolOutput](
    tool: Tool[InputT, OutputT], arguments: dict[str, str]
) -> InputT | None:
    """Parse arguments, returning None when they do not satisfy the contract."""
    try:
        return tool.parse(arguments)
    except ValidationError:
        return None


def timed_run[InputT: ToolInput, OutputT: ToolOutput](
    tool: Tool[InputT, OutputT], payload: InputT
) -> tuple[OutputT, float]:
    """Execute a tool and measure it. Exceptions propagate as typed errors."""
    started = time.perf_counter()
    output = tool.run(payload)
    return output, (time.perf_counter() - started) * 1000.0


__all__ = [
    "KNOWN_ENVIRONMENTS",
    "ComponentLookupInput",
    "ComponentLookupOutput",
    "EvidenceItem",
    "EvidenceScope",
    "LookupPlatformComponentTool",
    "ProposeChangeInput",
    "ProposeChangeOutput",
    "ProposeChangeRequestTool",
    "SearchDocsInput",
    "SearchDocsOutput",
    "SearchPlatformDocsTool",
    "Tool",
    "ToolError",
    "ToolInput",
    "ToolOutput",
    "parse_or_none",
    "timed_run",
]
