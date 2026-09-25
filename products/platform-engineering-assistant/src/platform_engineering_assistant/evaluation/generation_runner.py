"""The evaluation runner: dataset in, case outcomes out.

    dataset
      -> the application's own AnsweringService.answer()
           (retrieval -> context -> provider -> grounding -> citations)
      -> deterministic evaluators
      -> optional structured judge
      -> case outcomes

ONE ANSWER PATH
---------------
The runner calls `AnsweringService.answer` — the same method, on the same
service object, that the FastAPI route calls. There is deliberately no
evaluation-only retrieval, no evaluation-only prompt assembly and no
evaluation-only grounding. A parallel path would measure the parallel path: the
suite would go green while the served behaviour drifted, which is the specific
way an evaluation harness becomes worse than none at all.

NO RETRIES
----------
Each case is attempted exactly once. A failure IS the datum. Re-running until it
succeeds would turn a failure rate into a measure of patience and move every
other denominator without saying so.

WHAT THE RUNNER DECIDES, AND WHAT IT DOES NOT
----------------------------------------------
It decides what to call and records what happened. It does not score (that is
`generation_metrics`), does not gate (that is `policy`) and does not render
(that is `generation_report`).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from platform_engineering_assistant.answering import AnsweringService
from platform_engineering_assistant.domain import AnswerRequest, AnswerResponse, AnswerStatus
from platform_engineering_assistant.errors import AssistantError, FailureCategory
from platform_engineering_assistant.evaluation.generation_dataset import Dataset, GenerationCase
from platform_engineering_assistant.evaluation.generation_metrics import (
    CaseOutcome,
    JudgeCaseResult,
)
from platform_engineering_assistant.evaluation.judge import (
    REFUSAL_PLACEHOLDER,
    JudgeProvider,
    JudgeRequest,
    render_cited_evidence,
)

Sleeper = Callable[[float], None]

# Failures where every remaining case will fail identically. Continuing produces
# a longer list of the same error and, when rate limited, actively prolongs the
# throttle. The run stops and is reported as operationally incomplete.
ABORTING_FAILURES = frozenset(
    {
        FailureCategory.AUTHENTICATION,
        FailureCategory.AUTHORIZATION,
        FailureCategory.NETWORK_DENIED,
        FailureCategory.CONFIGURATION,
        FailureCategory.RATE_LIMITED,
        FailureCategory.CORPUS,
    }
)


@dataclass(frozen=True, slots=True)
class RunResult:
    """Every outcome the run produced, and whether it ran to completion."""

    outcomes: list[CaseOutcome]
    planned_cases: int
    aborted_on: FailureCategory | None = None

    # Identity of whatever actually served the run, read back from the first
    # response rather than assumed from configuration. A deployment name is what
    # was requested; the model is what answered, and on a deployment with
    # automatic version upgrades those are not the same fact.
    observed_model: str | None = None
    observed_deployment: str | None = None

    @property
    def operationally_complete(self) -> bool:
        return self.aborted_on is None and len(self.outcomes) == self.planned_cases


def prohibited_hit(answer: str | None, prohibited: tuple[str, ...]) -> bool:
    """Case-insensitive containment of any prohibited substring.

    Case-insensitive because the substrings describe CONTENT that must not
    appear — a disclosed prompt line, a fabricated SLA figure — and casing is not
    part of what makes such an appearance a defect.
    """
    if not answer or not prohibited:
        return False
    haystack = answer.lower()
    return any(needle.lower() in haystack for needle in prohibited)


def _observed_disposition_is_correct(case: GenerationCase, response: AnswerResponse) -> bool:
    """Whether the response matched the case's expectation.

    For a refusal, the REASON must also be one the case allows. Refusing a
    question about production cost because the citation policy tripped is not
    the same behaviour as refusing it for want of evidence, and scoring them
    identically would hide a real regression behind a correct-looking rate.
    """
    if response.status is not case.expected_disposition:
        return False
    if response.status is AnswerStatus.REFUSED:
        return response.refusal_reason in case.allowed_refusal_reasons
    return True


def _outcome_for(case: GenerationCase, response: AnswerResponse) -> CaseOutcome:
    """Build the redacted record of one completed case."""
    hit = prohibited_hit(response.answer, case.prohibited_substrings)
    met = _observed_disposition_is_correct(case, response)
    return CaseOutcome(
        case_id=case.case_id,
        category=case.category,
        tags=case.tags,
        expected_disposition=case.expected_disposition,
        observed_disposition=response.status,
        refusal_reason=response.refusal_reason,
        expected_doc_ids=case.expected_doc_ids,
        cited_chunk_ids=tuple(citation.chunk_id for citation in response.citations),
        cited_doc_ids=tuple(dict.fromkeys(citation.doc_id for citation in response.citations)),
        prohibited_hit=hit,
        expectation_met=met,
        answer_chars=len(response.answer or ""),
        latency_ms=response.latency_ms,
        input_tokens=response.token_usage.input_tokens,
        output_tokens=response.token_usage.output_tokens,
        total_tokens=response.token_usage.total_tokens,
    )


def _failed_outcome(case: GenerationCase, error: AssistantError) -> CaseOutcome:
    """A case that never produced a response."""
    return CaseOutcome(
        case_id=case.case_id,
        category=case.category,
        tags=case.tags,
        expected_disposition=case.expected_disposition,
        observed_disposition=None,
        failure_category=error.category,
        expected_doc_ids=case.expected_doc_ids,
    )


def run_dataset(
    service: AnsweringService,
    dataset: Dataset,
    *,
    judge: JudgeProvider | None = None,
    judge_rubric: str = "",
    inter_case_delay_seconds: float = 0.0,
    sleeper: Sleeper | None = None,
) -> RunResult:
    """Run every case once through the application's own answering service.

    `inter_case_delay_seconds` paces a live run against a small deployment. It
    changes only the SPACING of calls, never how many are made, so no denominator
    moves. There is no retry.
    """
    outcomes: list[CaseOutcome] = []
    aborted_on: FailureCategory | None = None
    observed_model: str | None = None
    observed_deployment: str | None = None
    wait = sleeper if sleeper is not None else _default_sleeper

    for index, case in enumerate(dataset.cases):
        if index and inter_case_delay_seconds > 0:
            wait(inter_case_delay_seconds)

        try:
            answered = service.answer(
                AnswerRequest(question=case.question), request_id=f"eval-{case.case_id}"
            )
        except AssistantError as error:
            outcomes.append(_failed_outcome(case, error))
            if error.category in ABORTING_FAILURES:
                aborted_on = error.category
                break
            continue

        response = answered.response
        if observed_model is None:
            observed_model = response.model_metadata.model
        if observed_deployment is None:
            observed_deployment = response.model_metadata.deployment
        outcome = _outcome_for(case, response)
        # Retrieval identifiers come from telemetry, which is the only place the
        # served pipeline records what it actually retrieved. Containment is then
        # re-checked here INDEPENDENTLY of the grounding policy that enforced it,
        # so the metric is a genuine audit rather than a restatement.
        outcome = _with_retrieved(outcome, answered.telemetry.retrieved_chunk_ids)

        if judge is not None:
            outcome = _with_judgement(outcome, service, case, response, judge, judge_rubric)

        outcomes.append(outcome)

    return RunResult(
        outcomes=outcomes,
        planned_cases=len(dataset.cases),
        aborted_on=aborted_on,
        observed_model=observed_model,
        observed_deployment=observed_deployment,
    )


def _default_sleeper(seconds: float) -> None:
    import time

    time.sleep(seconds)


def _with_retrieved(outcome: CaseOutcome, retrieved: tuple[str, ...]) -> CaseOutcome:
    from dataclasses import replace

    return replace(outcome, retrieved_chunk_ids=retrieved)


def _with_judgement(
    outcome: CaseOutcome,
    service: AnsweringService,
    case: GenerationCase,
    response: AnswerResponse,
    judge: JudgeProvider,
    rubric: str,
) -> CaseOutcome:
    """Ask the judge about one case and attach the verdict.

    The judge is given the question, the response, the cited evidence and the
    rubric — and nothing else. The expected disposition, the expected documents
    and the case id are all in scope here and none of them is passed.

    A judge failure is recorded, never raised. The deterministic metrics are the
    load-bearing ones; losing a semantic estimate must not destroy the run that
    produced them.
    """
    from dataclasses import replace

    if response.status is AnswerStatus.ANSWERED:
        response_text = response.answer or ""
    else:
        response_text = REFUSAL_PLACEHOLDER.format(reason=response.refusal_reason)

    request = JudgeRequest(
        rubric=rubric,
        question=case.question,
        response_text=response_text,
        cited_evidence=render_cited_evidence(list(response.citations), service.chunk_by_id),
    )

    try:
        verdict = judge.judge(request).verdict
    except AssistantError:
        return replace(
            outcome,
            judge=JudgeCaseResult(
                claims_total=0,
                claims_unsupported=0,
                answer_relevance=0.0,
                refusal_correct=None,
                material_contradiction=False,
                groundedness=0.0,
                judge_failed=True,
            ),
        )

    return replace(
        outcome,
        judge=JudgeCaseResult(
            claims_total=verdict.claims_total,
            claims_unsupported=verdict.claims_unsupported,
            answer_relevance=verdict.answer_relevance,
            refusal_correct=verdict.refusal_correct,
            material_contradiction=verdict.material_contradiction,
            groundedness=verdict.groundedness,
        ),
    )


__all__ = ["ABORTING_FAILURES", "RunResult", "prohibited_hit", "run_dataset"]
