"""Builders for the Phase 17.2 evaluation tests. No network, no Azure, no credential."""

from __future__ import annotations

from platform_engineering_assistant.answering import AnsweringService
from platform_engineering_assistant.config import GenerationConfig
from platform_engineering_assistant.corpus.chunking import Chunk
from platform_engineering_assistant.domain import AnswerStatus, RefusalReason
from platform_engineering_assistant.evaluation.generation_dataset import (
    CaseEvidence,
    Dataset,
    DatasetProvenance,
    GenerationCase,
)
from platform_engineering_assistant.evaluation.generation_metrics import (
    CaseOutcome,
    JudgeCaseResult,
)
from platform_engineering_assistant.evaluation.judge import (
    JudgeOutcome,
    JudgeRequest,
    JudgeVerdict,
)
from platform_engineering_assistant.generation.protocol import GenerationProvider
from platform_engineering_assistant.prompts import LoadedPrompt
from platform_engineering_assistant.retrieval.index import build_index
from tests.generation_fakes import CONFIG, chunk

GENERATION_CONFIG = GenerationConfig(
    version="generation_v1",
    prompt_file="answer_v1.md",
    context_budget_chars=12000,
    max_answer_chars=4000,
)

PROMPT = LoadedPrompt(version="answer_v1", content_hash="a" * 64, text="test prompt")


def service_with(
    provider: GenerationProvider, chunks: list[Chunk] | None = None
) -> AnsweringService:
    """A real AnsweringService over a tiny synthetic corpus.

    The REAL service class, not a stand-in: the runner's whole contract is that
    it drives the same orchestration the API drives, so a test double here would
    remove the only thing worth asserting.
    """
    indexed = chunks or [
        chunk("doc-a::decision::0::0", "Terraform state is stored in Azure Blob Storage."),
        chunk(
            "doc-b::context::0::0",
            "The sandbox environment is disposable and is not a promotion stage.",
            doc_id="adr-0005",
            doc_path="docs/adr/0005-x.md",
        ),
    ]
    return AnsweringService(
        index=build_index(indexed, CONFIG),
        provider=provider,
        prompt=PROMPT,
        retrieval_config=CONFIG,
        generation_config=GENERATION_CONFIG,
        corpus_version=1,
    )


def case(
    case_id: str = "T-001",
    *,
    question: str = "Where is Terraform state stored for this platform?",
    disposition: AnswerStatus = AnswerStatus.ANSWERED,
    expected_doc_ids: tuple[str, ...] = ("adr-0001",),
    allowed_refusal_reasons: tuple[RefusalReason, ...] = (),
    prohibited_substrings: tuple[str, ...] = (),
    category: str = "test",
    tags: tuple[str, ...] = (),
) -> GenerationCase:
    answering = disposition is AnswerStatus.ANSWERED
    return GenerationCase(
        case_id=case_id,
        question=question,
        expected_disposition=disposition,
        expected_doc_ids=expected_doc_ids if answering else (),
        allowed_refusal_reasons=()
        if answering
        else (allowed_refusal_reasons or (RefusalReason.INSUFFICIENT_EVIDENCE,)),
        prohibited_substrings=prohibited_substrings,
        category=category,
        tags=tags,
        evidence=CaseEvidence(path="docs/x.md", section="Decision", why="it says so")
        if answering
        else None,
        rationale="a synthetic case built for a unit test",
    )


def dataset(*cases: GenerationCase) -> Dataset:
    """A Dataset built in memory, bypassing composition rules on purpose.

    `load_dataset` enforces the sixteen-case composition; these unit tests are
    about the runner and the metrics, and forcing every one of them to carry a
    full sixteen-case fixture would hide what each test is actually asserting.
    """
    return Dataset(
        dataset_id="test_dataset",
        dataset_version=1,
        corpus_version_at_authoring=1,
        provenance=DatasetProvenance(
            authored="unit test",
            authoring_rule="unit test",
            retrieval_was_not_tuned_to_this_set="unit test",
            relationship_to_other_fixtures="unit test",
            measured_retrieval_ceiling="unit test",
            limitations=("synthetic",),
        ),
        cases=cases or (case(),),
        content_sha256="b" * 64,
    )


def outcome(
    case_id: str = "T-001",
    *,
    expected: AnswerStatus = AnswerStatus.ANSWERED,
    observed: AnswerStatus | None = AnswerStatus.ANSWERED,
    expectation_met: bool = True,
    cited: tuple[str, ...] = ("doc-a::decision::0::0",),
    retrieved: tuple[str, ...] = ("doc-a::decision::0::0",),
    cited_docs: tuple[str, ...] = ("adr-0001",),
    expected_docs: tuple[str, ...] = ("adr-0001",),
    prohibited_hit: bool = False,
    latency_ms: float = 10.0,
    total_tokens: int | None = 100,
    failure: object = None,
    judge: JudgeCaseResult | None = None,
) -> CaseOutcome:
    return CaseOutcome(
        case_id=case_id,
        category="test",
        tags=(),
        expected_disposition=expected,
        observed_disposition=observed,
        failure_category=failure,  # type: ignore[arg-type]
        expected_doc_ids=expected_docs,
        cited_chunk_ids=cited,
        cited_doc_ids=cited_docs,
        retrieved_chunk_ids=retrieved,
        prohibited_hit=prohibited_hit,
        expectation_met=expectation_met,
        latency_ms=latency_ms,
        total_tokens=total_tokens,
        judge=judge,
    )


class RecordingJudge:
    """A judge that records exactly what it was shown and returns a fixed verdict."""

    def __init__(self, verdict: JudgeVerdict | None = None, error: Exception | None = None) -> None:
        self._verdict = verdict or JudgeVerdict(
            claims_total=4, claims_unsupported=1, answer_relevance=0.9
        )
        self._error = error
        self.requests: list[JudgeRequest] = []

    @property
    def name(self) -> str:
        return "recording-judge"

    def judge(self, request: JudgeRequest) -> JudgeOutcome:
        self.requests.append(request)
        if self._error is not None:
            raise self._error
        return JudgeOutcome(verdict=self._verdict, latency_ms=1.0)
