"""A deterministic provider for tests and offline development.

WHY IT EXISTS
-------------
Every automated test in this product uses it. The grounding policy, the API, the
error mapping and the redaction guarantees must all be provable on a laptop with
no `az login`, no role assignment and no network — otherwise they could only be
debugged during a live call, which is the worst possible time.

WHAT IT IS NOT
--------------
It is not a model. Its "answers" are fixed strings and its citations are chosen
by rule. It proves the plumbing, never the quality of generation.

It is also the adversary: the behaviours below deliberately include the ways a
real model misbehaves — citing something it was not given, citing nothing,
citing the same chunk twice, and claiming to have answered while returning
nothing. Those are what the grounding policy exists to catch.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from enum import StrEnum

from platform_engineering_assistant.domain import RefusalReason
from platform_engineering_assistant.errors import AssistantError
from platform_engineering_assistant.generation.draft import DraftDisposition, GroundedDraft
from platform_engineering_assistant.generation.protocol import (
    GenerationOutcome,
    GenerationRequest,
    ProviderTelemetry,
)

PROVIDER_NAME = "fake-deterministic"

_CHUNK_ID_PATTERN = re.compile(r"\[chunk_id:\s*(?P<chunk_id>[^\]]+)\]")


class FakeBehaviour(StrEnum):
    """How the fake should misbehave, if at all."""

    ANSWER_FIRST_CHUNK = "answer_first_chunk"
    ANSWER_ALL_CHUNKS = "answer_all_chunks"
    REFUSE = "refuse"
    # --- the adversarial cases the grounding policy must catch ---------------
    CITE_UNRETRIEVED_CHUNK = "cite_unretrieved_chunk"
    CITE_NOTHING = "cite_nothing"
    CITE_DUPLICATES = "cite_duplicates"
    ANSWER_WITH_EMPTY_TEXT = "answer_with_empty_text"
    REFUSE_BUT_CITE = "refuse_but_cite"
    REFUSE_WITHOUT_REASON = "refuse_without_reason"
    ANSWER_WITH_REFUSAL_REASON = "answer_with_refusal_reason"


FIXED_ANSWER = "The supplied evidence describes the platform behaviour asked about."


class FakeGenerationProvider:
    """Deterministic provider. Same request in, same draft out, always."""

    def __init__(
        self,
        behaviour: FakeBehaviour = FakeBehaviour.ANSWER_FIRST_CHUNK,
        *,
        error: AssistantError | None = None,
        answer: str = FIXED_ANSWER,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._behaviour = behaviour
        self._error = error
        self._answer = answer
        self._clock = clock
        self.calls: list[GenerationRequest] = []

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    @staticmethod
    def chunk_ids_in(context: str) -> list[str]:
        """Read back the chunk ids the context actually offered.

        The fake parses the rendered context rather than being handed the
        retrieved chunks, which means the tests exercise the real contract: a
        model can only cite what the context showed it.
        """
        return _CHUNK_ID_PATTERN.findall(context)

    def generate(self, request: GenerationRequest) -> GenerationOutcome:
        self.calls.append(request)

        if self._error is not None:
            raise self._error

        started = self._clock()
        offered = self.chunk_ids_in(request.context)
        first = offered[0] if offered else "no-chunk-was-offered"

        draft = self._draft_for(offered, first)
        latency_ms = max((self._clock() - started) * 1000.0, 0.0)

        return GenerationOutcome(
            draft=draft,
            telemetry=ProviderTelemetry(
                provider=PROVIDER_NAME,
                model=PROVIDER_NAME,
                deployment=PROVIDER_NAME,
                latency_ms=latency_ms,
                input_tokens=len(request.context) // 4,
                output_tokens=len(self._answer) // 4,
                total_tokens=(len(request.context) + len(self._answer)) // 4,
                request_id="fake-request-0001",
            ),
        )

    def _draft_for(self, offered: list[str], first: str) -> GroundedDraft:
        behaviour = self._behaviour

        if behaviour is FakeBehaviour.REFUSE:
            return GroundedDraft(
                disposition=DraftDisposition.REFUSED,
                refusal_reason=RefusalReason.INSUFFICIENT_EVIDENCE,
            )
        if behaviour is FakeBehaviour.REFUSE_BUT_CITE:
            return GroundedDraft(
                disposition=DraftDisposition.REFUSED,
                cited_chunk_ids=[first],
                refusal_reason=RefusalReason.INSUFFICIENT_EVIDENCE,
            )
        if behaviour is FakeBehaviour.REFUSE_WITHOUT_REASON:
            return GroundedDraft(disposition=DraftDisposition.REFUSED)
        if behaviour is FakeBehaviour.CITE_UNRETRIEVED_CHUNK:
            return GroundedDraft(
                disposition=DraftDisposition.ANSWERED,
                answer=self._answer,
                cited_chunk_ids=["fabricated-doc::invented-heading::0::0"],
            )
        if behaviour is FakeBehaviour.CITE_NOTHING:
            return GroundedDraft(disposition=DraftDisposition.ANSWERED, answer=self._answer)
        if behaviour is FakeBehaviour.CITE_DUPLICATES:
            return GroundedDraft(
                disposition=DraftDisposition.ANSWERED,
                answer=self._answer,
                cited_chunk_ids=[first, first],
            )
        if behaviour is FakeBehaviour.ANSWER_WITH_EMPTY_TEXT:
            return GroundedDraft(
                disposition=DraftDisposition.ANSWERED,
                answer="   ",
                cited_chunk_ids=[first],
            )
        if behaviour is FakeBehaviour.ANSWER_WITH_REFUSAL_REASON:
            return GroundedDraft(
                disposition=DraftDisposition.ANSWERED,
                answer=self._answer,
                cited_chunk_ids=[first],
                refusal_reason=RefusalReason.OUT_OF_SCOPE,
            )
        if behaviour is FakeBehaviour.ANSWER_ALL_CHUNKS:
            return GroundedDraft(
                disposition=DraftDisposition.ANSWERED,
                answer=self._answer,
                cited_chunk_ids=list(offered),
            )

        return GroundedDraft(
            disposition=DraftDisposition.ANSWERED,
            answer=self._answer,
            cited_chunk_ids=[first],
        )
