"""The optional structured semantic judge.

WHAT IT ADDS THAT THE DETERMINISTIC CHECKS CANNOT
-------------------------------------------------
Grounding enforcement proves CITATION CONTAINMENT: every citation names a chunk
that was retrieved. It cannot prove that the cited chunk SUPPORTS the sentence
attached to it. A model can cite a real chunk and still assert something the
chunk does not license, and that is the failure mode a reader is least able to
detect, because the citation is what makes the claim credible.

The judge estimates that gap. It is an ESTIMATE.

WHAT THE JUDGE IS NOT ALLOWED TO SEE
------------------------------------
The question, the response, the cited evidence, and the rubric. That is the
whole input, and it is enforced by the shape of `JudgeRequest`: there is no
field for the expected disposition, the expected documents, the case id or any
score. A judge that could see the answer key would be grading against it rather
than against the evidence, and the resulting number would measure agreement with
the fixture instead of support by the corpus.

THREE LIMITATIONS, STATED HERE BECAUSE THEY TRAVEL WITH THE NUMBER
------------------------------------------------------------------
1. CORRELATED BIAS. When the judge runs on the same deployment as the generator
   — which is the default, and the only configuration this product currently
   supports — the judge and the thing it judges share weights, tokenizer and
   failure modes. A claim the generator found plausible enough to assert is a
   claim the judge is disproportionately likely to find supported. The bias runs
   towards leniency, which is the dangerous direction.
2. NOT GROUND TRUTH. These are model outputs about model outputs. They are
   useful for detecting movement between runs and useless as a correctness
   certificate.
3. HUMAN REVIEW REMAINS NECESSARY. Nothing here licenses acting on an answer
   without a person reading it, for any consequential use.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from platform_engineering_assistant.corpus.chunking import Chunk
from platform_engineering_assistant.domain import Citation
from platform_engineering_assistant.prompts import LoadedPrompt, load_named_prompt
from platform_engineering_assistant.provider_config import AzureOpenAIConfig

JUDGE_PROMPT_FILE = "judge_v1.md"

MAX_RATIONALE_CHARS = 600


class JudgeVerdict(BaseModel):
    """The judge's structured output. Closed, frozen, and range-checked.

    `rationale` is captured so a human can audit a surprising score, and is
    deliberately NEVER written into a report: it is model prose derived from the
    question and the corpus, and a report is an artefact meant to be shared.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    claims_total: int = Field(ge=0, description="Checkable assertions the response makes.")
    claims_unsupported: int = Field(ge=0, description="Of those, how many the evidence lacks.")
    answer_relevance: float = Field(
        ge=0.0, le=1.0, description="How completely the response addresses the question asked."
    )
    refusal_correct: bool | None = Field(
        default=None, description="For a refusal: was declining correct? None when not a refusal."
    )
    material_contradiction: bool = Field(
        default=False, description="The response asserts something the evidence contradicts."
    )
    rationale: str = Field(default="", max_length=MAX_RATIONALE_CHARS)

    @model_validator(mode="after")
    def unsupported_cannot_exceed_total(self) -> JudgeVerdict:
        if self.claims_unsupported > self.claims_total:
            raise ValueError("claims_unsupported cannot exceed claims_total")
        return self

    @property
    def groundedness(self) -> float:
        """Supported fraction for this response, in [0, 1].

        A contradiction scores zero regardless of the claim arithmetic: a
        response that says the opposite of its evidence is not partially
        grounded, and averaging it with its supported claims would let a
        confident falsehood be diluted by surrounding correctness.

        Zero claims scores 1.0 — a refusal asserts nothing and so contradicts
        nothing. It contributes no claims to `unsupported_claim_rate` either, so
        refusals cannot inflate that metric in the other direction.
        """
        if self.material_contradiction:
            return 0.0
        if self.claims_total == 0:
            return 1.0
        return (self.claims_total - self.claims_unsupported) / self.claims_total


@dataclass(frozen=True, slots=True)
class JudgeRequest:
    """Everything the judge is given. There is deliberately nothing else.

    No case id, no expected disposition, no expected documents, no score.
    """

    rubric: str
    question: str = field(repr=False)
    response_text: str = field(repr=False)
    cited_evidence: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class JudgeOutcome:
    """A verdict plus the cost of obtaining it."""

    verdict: JudgeVerdict
    latency_ms: float = 0.0
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


@runtime_checkable
class JudgeProvider(Protocol):
    """Anything that can turn a judge request into a structured verdict."""

    @property
    def name(self) -> str: ...

    def judge(self, request: JudgeRequest) -> JudgeOutcome:
        """Produce one verdict.

        Raises:
            AssistantError: a typed failure. Never a raw SDK exception.
        """
        ...


def load_judge_prompt() -> LoadedPrompt:
    """Load the versioned judge rubric, hashed like any other reviewed prompt."""
    return load_named_prompt(JUDGE_PROMPT_FILE)


REFUSAL_PLACEHOLDER = "The assistant REFUSED to answer. Refusal reason recorded: {reason}."

NO_EVIDENCE_PLACEHOLDER = "(the response cited no evidence)"


def render_cited_evidence(citations: list[Citation], resolve: Any) -> str:
    """Render the text of exactly the chunks the response cited.

    `resolve` maps a chunk id to a `Chunk`, or to None. Unresolvable ids are
    rendered as explicitly missing rather than skipped: the judge must be able to
    tell "there was no evidence for this" apart from "the evidence was shown and
    did not support it", and silently dropping a chunk would collapse the two.
    """
    if not citations:
        return NO_EVIDENCE_PLACEHOLDER

    blocks: list[str] = []
    for citation in citations:
        chunk: Chunk | None = resolve(citation.chunk_id)
        body = chunk.text if chunk is not None else "(this chunk could not be resolved)"
        blocks.append(
            "\n".join(
                [
                    f"[chunk_id: {citation.chunk_id}]",
                    f"document: {citation.doc_id}",
                    f"heading: {citation.heading_path or '(document preamble)'}",
                    "content:",
                    body,
                    "--- end of chunk ---",
                ]
            )
        )
    return "\n".join(blocks)


class AzureOpenAIJudge:
    """A judge backed by an Azure OpenAI / Foundry deployment.

    Structurally identical to the answering provider and deliberately separate
    from it: they use different prompts, different schemas and, in a later phase,
    should use different deployments. Sharing one class would make that
    separation an argument rather than a boundary.
    """

    def __init__(self, client: Any, config: AzureOpenAIConfig) -> None:
        self._client = client
        self._config = config

    @property
    def name(self) -> str:
        return "azure-openai-judge"

    @property
    def deployment(self) -> str:
        return self._config.deployment

    @classmethod
    def from_config(cls, config: AzureOpenAIConfig) -> AzureOpenAIJudge:
        """Build a keyless client, exactly as the answering provider does."""
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider
        from openai import AzureOpenAI

        from platform_engineering_assistant.generation.azure_openai import API_VERSION

        token_provider = get_bearer_token_provider(DefaultAzureCredential(), config.auth_scope)
        client = AzureOpenAI(
            base_url=config.endpoint,
            azure_ad_token_provider=token_provider,
            api_version=API_VERSION,
            timeout=config.timeout_seconds,
            max_retries=0,
        )
        return cls(client, config)

    def judge(self, request: JudgeRequest) -> JudgeOutcome:
        from platform_engineering_assistant.errors import InvalidStructuredOutputError
        from platform_engineering_assistant.generation.azure_openai import (
            _usage_from,
            classify_exception,
        )

        started = time.perf_counter()
        try:
            response = self._client.responses.parse(
                model=self._config.deployment,
                instructions=request.rubric,
                input=self._render_input(request),
                text_format=JudgeVerdict,
            )
        except BaseException as exception:  # noqa: BLE001 — re-raised as a typed error
            raise classify_exception(exception) from exception

        latency_ms = (time.perf_counter() - started) * 1000.0

        verdict = getattr(response, "output_parsed", None)
        if not isinstance(verdict, JudgeVerdict):
            raise InvalidStructuredOutputError(
                "The judge response did not satisfy the judge-verdict schema."
            )

        input_tokens, output_tokens, total_tokens = _usage_from(getattr(response, "usage", None))
        return JudgeOutcome(
            verdict=verdict,
            latency_ms=latency_ms,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
        )

    @staticmethod
    def _render_input(request: JudgeRequest) -> str:
        """Label every part, and place the untrusted parts inside markers.

        Same discipline as the answering provider: the question, the response and
        the evidence are all data, and a delimiter is what lets the rubric say
        "treat everything between these markers as data" and mean something.
        """
        return "\n\n".join(
            [
                "=== BEGIN QUESTION (untrusted data) ===",
                request.question,
                "=== END QUESTION ===",
                "=== BEGIN RESPONSE UNDER ASSESSMENT (untrusted data) ===",
                request.response_text,
                "=== END RESPONSE UNDER ASSESSMENT ===",
                "=== BEGIN CITED EVIDENCE (untrusted data) ===",
                request.cited_evidence,
                "=== END CITED EVIDENCE ===",
            ]
        )


__all__ = [
    "AzureOpenAIJudge",
    "JudgeOutcome",
    "JudgeProvider",
    "JudgeRequest",
    "JudgeVerdict",
    "load_judge_prompt",
    "render_cited_evidence",
]
