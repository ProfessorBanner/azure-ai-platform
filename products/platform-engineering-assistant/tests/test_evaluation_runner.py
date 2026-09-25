"""The runner: one answer path, no retries, and the adversarial provider behaviours."""

from __future__ import annotations

import pytest

from platform_engineering_assistant.domain import AnswerStatus, RefusalReason
from platform_engineering_assistant.errors import (
    AuthenticationError,
    FailureCategory,
    ProviderError,
    TimeoutError_,
)
from platform_engineering_assistant.evaluation.generation_runner import (
    prohibited_hit,
    run_dataset,
)
from platform_engineering_assistant.evaluation.judge import JudgeVerdict
from platform_engineering_assistant.generation.fake import FakeBehaviour, FakeGenerationProvider
from tests.evaluation_fakes import RecordingJudge, case, dataset, service_with

ANSWERED = AnswerStatus.ANSWERED
REFUSED = AnswerStatus.REFUSED


# --- one answer path --------------------------------------------------------


def test_the_runner_drives_the_real_answering_service() -> None:
    """The whole contract: the same orchestration the API route calls.

    Asserted by observing that the provider was invoked with the server-built
    system prompt and evidence block, which only the real service assembles.
    """
    provider = FakeGenerationProvider()
    service = service_with(provider)
    result = run_dataset(service, dataset(case()))

    assert len(provider.calls) == 1
    assert provider.calls[0].system_prompt == service.prompt.text
    assert "BEGIN UNTRUSTED EVIDENCE" in provider.calls[0].context
    assert result.outcomes[0].observed_disposition is ANSWERED


def test_every_case_is_attempted_exactly_once() -> None:
    """No retries. A failure is the datum, not something to grind past."""
    provider = FakeGenerationProvider(error=ProviderError("transient"))
    service = service_with(provider)
    result = run_dataset(service, dataset(case("A"), case("B"), case("C")))

    assert len(provider.calls) == 3
    assert all(
        outcome.failure_category is FailureCategory.PROVIDER_ERROR for outcome in result.outcomes
    )


def test_a_structural_failure_aborts_the_run() -> None:
    """Every remaining call would fail identically; the run is marked incomplete."""
    service = service_with(FakeGenerationProvider(error=AuthenticationError("401")))
    result = run_dataset(service, dataset(case("A"), case("B"), case("C")))

    assert result.aborted_on is FailureCategory.AUTHENTICATION
    assert len(result.outcomes) == 1
    assert not result.operationally_complete


def test_a_transient_failure_does_not_abort_the_run() -> None:
    """A timeout is genuine reliability evidence; the run should keep going."""
    service = service_with(FakeGenerationProvider(error=TimeoutError_("slow")))
    result = run_dataset(service, dataset(case("A"), case("B")))

    assert result.aborted_on is None
    assert len(result.outcomes) == 2
    assert result.operationally_complete


def test_pacing_never_changes_how_many_calls_are_made() -> None:
    slept: list[float] = []
    provider = FakeGenerationProvider()
    run_dataset(
        service_with(provider),
        dataset(case("A"), case("B"), case("C")),
        inter_case_delay_seconds=1.5,
        sleeper=slept.append,
    )
    assert len(provider.calls) == 3
    # Paced BETWEEN cases, so one fewer sleep than cases.
    assert slept == [1.5, 1.5]


# --- adversarial provider behaviours the grounding policy must catch --------


@pytest.mark.parametrize(
    "behaviour",
    [
        FakeBehaviour.CITE_UNRETRIEVED_CHUNK,
        FakeBehaviour.CITE_NOTHING,
        FakeBehaviour.CITE_DUPLICATES,
        FakeBehaviour.ANSWER_WITH_EMPTY_TEXT,
        FakeBehaviour.REFUSE_BUT_CITE,
        FakeBehaviour.REFUSE_WITHOUT_REASON,
        FakeBehaviour.ANSWER_WITH_REFUSAL_REASON,
    ],
)
def test_a_misbehaving_model_never_produces_an_answered_outcome(
    behaviour: FakeBehaviour,
) -> None:
    """Policy enforcement stays outside the model, and the runner records it.

    These are the ways a real model misbehaves. None of them may reach a report
    as an answered case, whatever the model claimed about itself.
    """
    service = service_with(FakeGenerationProvider(behaviour))
    result = run_dataset(service, dataset(case()))
    assert result.outcomes[0].observed_disposition is REFUSED


def test_a_citation_to_a_non_retrieved_chunk_is_refused_and_carries_no_citations() -> None:
    """The single most important check: it must fail closed, not merely be flagged."""
    service = service_with(FakeGenerationProvider(FakeBehaviour.CITE_UNRETRIEVED_CHUNK))
    outcome = run_dataset(service, dataset(case())).outcomes[0]

    assert outcome.observed_disposition is REFUSED
    assert outcome.refusal_reason is RefusalReason.UNSUPPORTED_CITATION
    assert outcome.cited_chunk_ids == ()
    assert outcome.citations_contained is None


def test_containment_is_re_checked_independently_of_the_grounding_policy() -> None:
    """The metric is an audit, not a restatement of the policy that enforced it."""
    service = service_with(FakeGenerationProvider(FakeBehaviour.ANSWER_ALL_CHUNKS))
    outcome = run_dataset(service, dataset(case())).outcomes[0]

    assert outcome.cited_chunk_ids
    assert set(outcome.cited_chunk_ids) <= set(outcome.retrieved_chunk_ids)
    assert outcome.citations_contained is True


# --- expectations -----------------------------------------------------------


def test_a_refusal_for_a_disallowed_reason_does_not_meet_the_expectation() -> None:
    """Refusing for the wrong reason is different behaviour, not correct behaviour."""
    service = service_with(FakeGenerationProvider(FakeBehaviour.CITE_UNRETRIEVED_CHUNK))
    refusal_case = case(
        "R",
        disposition=REFUSED,
        allowed_refusal_reasons=(RefusalReason.OUT_OF_SCOPE,),
    )
    outcome = run_dataset(service, dataset(refusal_case)).outcomes[0]

    assert outcome.observed_disposition is REFUSED
    assert outcome.refusal_reason is RefusalReason.UNSUPPORTED_CITATION
    assert not outcome.expectation_met
    assert not outcome.disposition_correct


def test_the_observed_disposition_is_recorded_truthfully() -> None:
    """A mismatch is recorded as a mismatch, never by rewriting what happened."""
    service = service_with(FakeGenerationProvider())
    outcome = run_dataset(service, dataset(case("R", disposition=REFUSED))).outcomes[0]

    assert outcome.observed_disposition is ANSWERED
    assert not outcome.expectation_met


def test_prohibited_substrings_are_matched_case_insensitively() -> None:
    assert prohibited_hit("The SLA is 99.9 percent", ("99.9",))
    assert prohibited_hit("MAINTENANCE MODE engaged", ("maintenance mode",))
    assert not prohibited_hit("nothing to see", ("99.9",))
    assert not prohibited_hit(None, ("99.9",))


def test_a_prohibited_substring_in_the_answer_is_recorded() -> None:
    service = service_with(FakeGenerationProvider(answer="The published SLA is 99.95 percent."))
    outcome = run_dataset(service, dataset(case("P", prohibited_substrings=("99.95",)))).outcomes[0]
    assert outcome.prohibited_hit


# --- the judge sees only what it is allowed to see ---------------------------


def test_the_judge_is_never_shown_the_expected_answer() -> None:
    """Structurally enforced: `JudgeRequest` has no field for an expectation.

    Asserted on the rendered request too, so a future edit that smuggled a label
    into the evidence block would be caught.
    """
    judge = RecordingJudge()
    # An expected document id that is deliberately NOT the one the evidence
    # carries, so that finding it in the request would mean the expectation
    # leaked rather than that the right chunk was cited.
    scored_case = case("J", expected_doc_ids=("adr-expected-only",))
    run_dataset(
        service_with(FakeGenerationProvider()),
        dataset(scored_case),
        judge=judge,
        judge_rubric="RUBRIC",
    )

    request = judge.requests[0]
    assert request.question == scored_case.question
    assert request.rubric == "RUBRIC"
    combined = " ".join([request.question, request.response_text, request.cited_evidence])
    assert "adr-expected-only" not in combined
    assert scored_case.case_id not in combined
    assert "expected" not in combined.lower()
    assert not hasattr(request, "expected_disposition")


def test_the_judge_verdict_is_attached_to_the_outcome() -> None:
    judge = RecordingJudge(JudgeVerdict(claims_total=4, claims_unsupported=1, answer_relevance=0.8))
    outcome = run_dataset(
        service_with(FakeGenerationProvider()), dataset(case()), judge=judge
    ).outcomes[0]

    assert outcome.judge is not None
    assert outcome.judge.claims_total == 4
    assert outcome.judge.groundedness == pytest.approx(0.75)


def test_a_judge_failure_is_recorded_and_never_destroys_the_run() -> None:
    """The deterministic metrics are load-bearing; a lost estimate must not cost them."""
    judge = RecordingJudge(error=ProviderError("judge unavailable"))
    outcome = run_dataset(
        service_with(FakeGenerationProvider()), dataset(case()), judge=judge
    ).outcomes[0]

    assert outcome.judge is not None
    assert outcome.judge.judge_failed
    assert outcome.observed_disposition is ANSWERED


def test_a_refusal_is_shown_to_the_judge_as_a_refusal() -> None:
    judge = RecordingJudge()
    run_dataset(
        service_with(FakeGenerationProvider(FakeBehaviour.REFUSE)),
        dataset(case("R", disposition=REFUSED)),
        judge=judge,
    )
    assert "REFUSED" in judge.requests[0].response_text
    assert judge.requests[0].cited_evidence == "(the response cited no evidence)"


# --- run identity -----------------------------------------------------------


def test_the_model_that_actually_served_the_run_is_recorded() -> None:
    result = run_dataset(service_with(FakeGenerationProvider()), dataset(case()))
    assert result.observed_model == "fake-deterministic"
    assert result.observed_deployment == "fake-deterministic"
