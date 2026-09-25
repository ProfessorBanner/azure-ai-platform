"""The orchestrator: question in, grounded AnswerResponse out.

    question
      -> retrieve (server-owned top-k)
      -> build bounded context
      -> generate draft
      -> enforce grounding OUTSIDE the model
      -> construct server-owned citations
      -> AnswerResponse, or a fail-closed refusal

Plain typed Python on purpose. Every step is a function call whose inputs and
outputs are visible, which is what makes the grounding guarantee reviewable. A
framework would hide exactly the part that matters.

The service is built once at startup and is immutable afterwards. Nothing a
caller sends can change the prompt, the model, the deployment, the corpus,
top-k, the authority labels or the citations.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

from platform_engineering_assistant.config import (
    GenerationConfig,
    RetrievalConfig,
    load_generation_config,
    load_retrieval_config,
)
from platform_engineering_assistant.context import build_context
from platform_engineering_assistant.corpus import load_corpus, load_manifest
from platform_engineering_assistant.corpus.chunking import Chunk, chunk_corpus
from platform_engineering_assistant.domain import (
    AnswerRequest,
    AnswerResponse,
    AnswerStatus,
    ModelMetadata,
    RefusalReason,
    TokenUsage,
)
from platform_engineering_assistant.errors import AssistantError
from platform_engineering_assistant.generation.protocol import (
    GenerationProvider,
    GenerationRequest,
)
from platform_engineering_assistant.grounding import enforce_grounding
from platform_engineering_assistant.prompts import LoadedPrompt, load_prompt
from platform_engineering_assistant.retrieval.index import BM25Index, build_index
from platform_engineering_assistant.telemetry import RequestTelemetry


@dataclass(frozen=True, slots=True)
class AnsweredRequest:
    """The public response plus the telemetry describing how it was produced."""

    response: AnswerResponse
    telemetry: RequestTelemetry


class AnsweringService:
    """Immutable, fully-configured answering pipeline."""

    def __init__(
        self,
        index: BM25Index,
        provider: GenerationProvider,
        prompt: LoadedPrompt,
        retrieval_config: RetrievalConfig,
        generation_config: GenerationConfig,
        corpus_version: int,
    ) -> None:
        self._index = index
        self._provider = provider
        self._prompt = prompt
        self._retrieval_config = retrieval_config
        self._generation_config = generation_config
        self._corpus_version = corpus_version

    # --- introspection used by readiness and telemetry ----------------------

    @property
    def prompt(self) -> LoadedPrompt:
        return self._prompt

    @property
    def chunk_count(self) -> int:
        return self._index.size

    @property
    def provider_name(self) -> str:
        return self._provider.name

    @property
    def corpus_version(self) -> int:
        return self._corpus_version

    # Read-only accessors added in Phase 18.1 so the agent layer can reuse this
    # service's already-built index, provider and configuration rather than
    # constructing a second, divergent set. Additive and read-only on purpose:
    # the service stays immutable and there is still exactly one answering path.

    @property
    def index(self) -> BM25Index:
        return self._index

    @property
    def provider(self) -> GenerationProvider:
        return self._provider

    @property
    def retrieval_config(self) -> RetrievalConfig:
        return self._retrieval_config

    @property
    def generation_config(self) -> GenerationConfig:
        return self._generation_config

    def chunk_by_id(self, chunk_id: str) -> Chunk | None:
        """Look up an indexed chunk by identifier, or None if it is unknown.

        Added in Phase 17.2 for the evaluation judge, which must show the judge
        the TEXT of the chunks an answer cited — an `AnswerResponse` carries
        citation metadata only, by design, so the text has to be resolved from
        the index the answer was actually served from.

        Read-only and additive on purpose. The alternative was to have the
        evaluation runner build its own index and its own context, which would
        be a second answer path measuring something other than the product.
        """
        for chunk in self._index.chunks:
            if chunk.chunk_id == chunk_id:
                return chunk
        return None

    def answer(self, request: AnswerRequest, request_id: str | None = None) -> AnsweredRequest:
        """Answer one question, or refuse.

        Raises:
            AssistantError: only for provider and configuration failures, which
                the API layer maps to status codes. Grounding failures never
                raise — they become refusals, because a refusal is a legitimate
                answer and a 500 is not.
        """
        correlation_id = request_id or f"req-{uuid.uuid4().hex[:16]}"
        started = time.perf_counter()

        # --- retrieve --------------------------------------------------------
        retrieval_started = time.perf_counter()
        results = self._index.search(request.question)
        retrieval_ms = (time.perf_counter() - retrieval_started) * 1000.0

        if not results:
            # Nothing cleared the no-signal floor. There is nothing to ground an
            # answer in, so there is no reason to spend a model call.
            return self._refusal(
                RefusalReason.INSUFFICIENT_EVIDENCE,
                correlation_id=correlation_id,
                question_chars=len(request.question),
                retrieval_ms=retrieval_ms,
                total_ms=(time.perf_counter() - started) * 1000.0,
            )

        # --- build the bounded, delimited evidence block ---------------------
        context = build_context(list(results), self._generation_config.context_budget_chars)

        # --- generate --------------------------------------------------------
        generation_started = time.perf_counter()
        outcome = self._provider.generate(
            GenerationRequest(
                system_prompt=self._prompt.text,
                context=context.text,
                question=request.question,
                max_answer_chars=self._generation_config.max_answer_chars,
            )
        )
        generation_ms = (time.perf_counter() - generation_started) * 1000.0

        # --- enforce grounding outside the model -----------------------------
        # Only the chunks actually shown to the model are citable, so a chunk
        # dropped for budget cannot be cited even though it was retrieved.
        decision = enforce_grounding(outcome.draft, list(context.included))

        total_ms = (time.perf_counter() - started) * 1000.0
        model_metadata = ModelMetadata(
            provider=outcome.telemetry.provider,
            deployment=outcome.telemetry.deployment,
            model=outcome.telemetry.model,
        )
        token_usage = TokenUsage(
            input_tokens=outcome.telemetry.input_tokens,
            output_tokens=outcome.telemetry.output_tokens,
            total_tokens=outcome.telemetry.total_tokens,
        )

        base_telemetry = {
            "request_id": correlation_id,
            "retrieval_ms": retrieval_ms,
            "generation_ms": generation_ms,
            "total_ms": total_ms,
            "retrieved_chunk_count": len(context.included),
            "context_chars": len(context.text),
            "context_chunks_dropped": context.dropped_for_budget,
            "question_chars": len(request.question),
            "prompt_version": self._prompt.version,
            "prompt_hash": self._prompt.content_hash,
            "retrieval_config_version": self._retrieval_config.version,
            "generation_config_version": self._generation_config.version,
            "corpus_version": self._corpus_version,
            "provider": outcome.telemetry.provider,
            "model": outcome.telemetry.model,
            "deployment": outcome.telemetry.deployment,
            "input_tokens": outcome.telemetry.input_tokens,
            "output_tokens": outcome.telemetry.output_tokens,
            "total_tokens": outcome.telemetry.total_tokens,
            "provider_request_id": outcome.telemetry.request_id,
            "retrieved_chunk_ids": context.chunk_ids,
        }

        if not decision.accepted:
            reason = decision.refusal_reason or RefusalReason.INSUFFICIENT_EVIDENCE
            return AnsweredRequest(
                response=AnswerResponse(
                    status=AnswerStatus.REFUSED,
                    refusal_reason=reason,
                    request_id=correlation_id,
                    prompt_version=self._prompt.version,
                    retrieval_config_version=self._retrieval_config.version,
                    corpus_version=self._corpus_version,
                    model_metadata=model_metadata,
                    latency_ms=total_ms,
                    token_usage=token_usage,
                ),
                telemetry=RequestTelemetry(
                    status=AnswerStatus.REFUSED,
                    refusal_reason=reason,
                    grounding_violation=decision.violation,
                    citation_count=0,
                    **base_telemetry,  # type: ignore[arg-type]
                ),
            )

        answer_text = (outcome.draft.answer or "").strip()[
            : self._generation_config.max_answer_chars
        ]

        return AnsweredRequest(
            response=AnswerResponse(
                status=AnswerStatus.ANSWERED,
                answer=answer_text,
                citations=list(decision.citations),
                request_id=correlation_id,
                prompt_version=self._prompt.version,
                retrieval_config_version=self._retrieval_config.version,
                corpus_version=self._corpus_version,
                model_metadata=model_metadata,
                latency_ms=total_ms,
                token_usage=token_usage,
            ),
            telemetry=RequestTelemetry(
                status=AnswerStatus.ANSWERED,
                citation_count=len(decision.citations),
                **base_telemetry,  # type: ignore[arg-type]
            ),
        )

    def _refusal(
        self,
        reason: RefusalReason,
        *,
        correlation_id: str,
        question_chars: int,
        retrieval_ms: float,
        total_ms: float,
    ) -> AnsweredRequest:
        """A refusal produced without calling the model at all."""
        return AnsweredRequest(
            response=AnswerResponse(
                status=AnswerStatus.REFUSED,
                refusal_reason=reason,
                request_id=correlation_id,
                prompt_version=self._prompt.version,
                retrieval_config_version=self._retrieval_config.version,
                corpus_version=self._corpus_version,
                model_metadata=ModelMetadata(provider=self._provider.name),
                latency_ms=total_ms,
            ),
            telemetry=RequestTelemetry(
                request_id=correlation_id,
                retrieval_ms=retrieval_ms,
                total_ms=total_ms,
                question_chars=question_chars,
                prompt_version=self._prompt.version,
                prompt_hash=self._prompt.content_hash,
                retrieval_config_version=self._retrieval_config.version,
                generation_config_version=self._generation_config.version,
                corpus_version=self._corpus_version,
                provider=self._provider.name,
                status=AnswerStatus.REFUSED,
                refusal_reason=reason,
            ),
        )


def build_service(provider: GenerationProvider) -> AnsweringService:
    """Load corpus, chunk, index and prompt once. Raises AssistantError on failure."""
    retrieval_config = load_retrieval_config()
    generation_config = load_generation_config()
    manifest = load_manifest()
    chunks = chunk_corpus(load_corpus(manifest), retrieval_config.chunk_budget_chars)

    return AnsweringService(
        index=build_index(chunks, retrieval_config),
        provider=provider,
        prompt=load_prompt(generation_config),
        retrieval_config=retrieval_config,
        generation_config=generation_config,
        corpus_version=manifest.corpus_version,
    )


__all__ = ["AnsweredRequest", "AnsweringService", "AssistantError", "build_service"]
