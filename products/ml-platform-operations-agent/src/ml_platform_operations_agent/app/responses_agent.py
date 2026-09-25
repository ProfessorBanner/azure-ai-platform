"""The `ResponsesAgent` implementation over the controlled core.

WHAT THE INSTALLED MLFLOW ACTUALLY PROVIDES
-------------------------------------------
MLflow 3.16.0 ships `mlflow.pyfunc.ResponsesAgent` — one abstract method,
`predict(ResponsesAgentRequest) -> ResponsesAgentResponse` — and the
`mlflow.types.responses` models. It does NOT ship an agent server: there is no
`mlflow.*agent_server` module in this install and no such package on PyPI for
this stack. So this file implements the genuine MLflow contract, and
`server.py` exposes it over HTTP because Databricks Apps needs a process bound
to `DATABRICKS_APP_PORT`.

NO RUNTIME MODEL
----------------
This agent calls no language model. Its orchestration is a fixed tool sequence
and its diagnosis is composed deterministically, which is the property the
whole product exists to demonstrate. The LLM judge belongs to evaluation and
has no runtime authority — deleting it changes nothing here.

STRUCTURED FACTS TRAVEL IN `custom_outputs`
--------------------------------------------
`ResponsesAgentResponse.custom_outputs` is the sanctioned place for
application-specific data. The prose output carries the summary; every
identifier, enum, citation and limitation travels as structured data, because a
caller must be able to branch on `degradation_status` without parsing English.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from mlflow.types.responses_helpers import OutputItem

from mlflow.pyfunc import ResponsesAgent  # type: ignore[attr-defined]
from mlflow.types.responses import ResponsesAgentRequest, ResponsesAgentResponse

from ml_platform_operations_agent.agent import (
    DiagnosisRequest,
    EvidenceSources,
    OperationsAgent,
)
from ml_platform_operations_agent.domain import (
    MAX_MODEL_NAME_CHARS,
    MAX_QUESTION_CHARS,
    Diagnosis,
    TimeWindow,
)
from ml_platform_operations_agent.errors import InvalidRequestError

#: The window the App answers about when a caller does not say otherwise.
DEFAULT_WINDOW_DAYS = 7

#: Bound on how many input items will be read. A Responses request can carry an
#: arbitrary conversation; this agent answers one bounded question and has no
#: use for history, so the bound is small and explicit.
MAX_INPUT_ITEMS = 20


def _text_of(item: Any) -> str:
    """Extract user text from one Responses input item.

    Returns "" for anything that is not user text — an image, a tool call, a
    function result. Those are not errors; they are simply not this agent's
    input, and silently ignoring them is better than failing a request that
    merely carried something extra.
    """
    if isinstance(item, str):
        return item

    # MLflow parses input items into pydantic models (`Message`), not dicts, so
    # both shapes are handled: the typed one the library produces, and the raw
    # one a test or a direct caller may pass.
    if not isinstance(item, dict):
        dumped = getattr(item, "model_dump", None)
        if dumped is None:
            return ""
        item = dumped(mode="python")

    if item.get("role") not in (None, "user"):
        return ""
    content = item.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") in {"input_text", "text"}
        ]
        return " ".join(p for p in parts if p)
    return ""


def extract_question(request: ResponsesAgentRequest) -> str:
    """The user's question, bounded and flattened.

    Raises `InvalidRequestError` when there is no user text at all: an empty
    request is not a question, and answering one would mean inventing it.
    """
    items = request.input if isinstance(request.input, list) else [request.input]
    if len(items) > MAX_INPUT_ITEMS:
        raise InvalidRequestError(f"the request carries more than {MAX_INPUT_ITEMS} input items")

    texts = [t for t in (_text_of(item) for item in items) if t.strip()]
    question = " ".join(texts).strip()

    if not question:
        raise InvalidRequestError("the request carries no user text")
    if len(question) > MAX_QUESTION_CHARS:
        raise InvalidRequestError(f"the question exceeds the {MAX_QUESTION_CHARS}-character limit")
    return question


def extract_model_name(request: ResponsesAgentRequest) -> str:
    """The governed model name, from `custom_inputs`.

    REQUIRED AND EXPLICIT. It is not parsed out of the question: guessing a
    three-part name from prose is how an agent ends up reading a model nobody
    asked about, and `config.parse_model_name` would reject a guess anyway.
    """
    custom = request.custom_inputs or {}
    name = str(custom.get("model_name") or "").strip()
    if not name:
        raise InvalidRequestError(
            "custom_inputs.model_name is required and must be a governed "
            "three-part Unity Catalog name"
        )
    if len(name) > MAX_MODEL_NAME_CHARS:
        raise InvalidRequestError("the model identifier exceeds its length limit")
    return name


def extract_window_days(request: ResponsesAgentRequest) -> int:
    custom = request.custom_inputs or {}
    raw = custom.get("window_days", DEFAULT_WINDOW_DAYS)
    try:
        days = int(raw)
    except (TypeError, ValueError):
        raise InvalidRequestError("custom_inputs.window_days must be a whole number") from None
    if not 1 <= days <= 90:
        raise InvalidRequestError("custom_inputs.window_days must be between 1 and 90")
    return days


def custom_outputs_for(diagnosis: Diagnosis) -> dict[str, Any]:
    """The structured answer a caller branches on.

    Identifiers, enums and counts — never a raw evidence payload. Citations
    carry their type and address so an operator can resolve them; they do not
    carry the rows behind them.
    """
    return {
        "requested_model": diagnosis.requested_model,
        "resolved_model": diagnosis.resolved_model,
        "resolved_version": diagnosis.resolved_version,
        "degradation_status": diagnosis.degradation_status.value,
        "outcome": diagnosis.outcome.value,
        "refusal_reason": diagnosis.refusal_reason.value if diagnosis.refusal_reason else None,
        "confidence": diagnosis.confidence,
        "selected_tools": list(diagnosis.selected_tools),
        "likely_causes": [
            {
                "statement": cause.statement,
                "support": cause.support.value,
                "evidence": [
                    {"source_type": r.source_type.value, "source_identifier": r.source_identifier}
                    for r in cause.evidence
                ],
                "mechanism": (cause.mechanism.source_identifier if cause.mechanism else None),
            }
            for cause in diagnosis.likely_causes
        ],
        "supporting_evidence": [
            {
                "source_type": r.source_type.value,
                "source_identifier": r.source_identifier,
                "relevant_fields": list(r.relevant_fields),
                "observed_at": r.observed_at.isoformat(),
            }
            for r in diagnosis.supporting_evidence
        ],
        "recommended_investigations": list(diagnosis.recommended_investigations),
        "limitations": list(diagnosis.limitations),
        "time_window": diagnosis.time_window.label() if diagnosis.time_window else None,
        "read_only": True,
    }


class MlPlatformOperationsResponsesAgent(ResponsesAgent):
    # `predict` intentionally narrows PythonModel.predict to the Responses
    # contract, which is what ResponsesAgent exists to do.
    """The App's agent. Holds no policy of its own.

    Takes a FACTORY, not a fixed `EvidenceSources`. The live adapters carry a
    window for the absence records they produce, and a long-running App that
    fixed it at startup would stamp every absence with its boot-time window —
    an evidence record claiming an interval nobody asked about. The factory
    rebinds the window per request while reusing the clients.
    """

    def __init__(self, sources: EvidenceSources | Callable[[TimeWindow], EvidenceSources]) -> None:
        if callable(sources):
            self._factory: Callable[[TimeWindow], EvidenceSources] = sources
        else:
            # A fixed set is accepted for tests, where the fakes ignore the
            # window anyway.
            self._factory = lambda _window: sources

    def predict(  # type: ignore[override]
        self, request: ResponsesAgentRequest
    ) -> ResponsesAgentResponse:
        question = extract_question(request)
        model_name = extract_model_name(request)
        days = extract_window_days(request)

        # The clock is read ONCE, here at the request boundary, and threaded
        # through. The agent and adapters never read one themselves.
        observed_at = datetime.now(UTC)
        window = TimeWindow(start=observed_at - timedelta(days=days), end=observed_at)

        diagnosis = OperationsAgent(self._factory(window)).run(
            DiagnosisRequest(
                model_name=model_name,
                window=window,
                observed_at=observed_at,
                question=question,
            )
        )

        return ResponsesAgentResponse(
            output=[
                cast(
                    "OutputItem",
                    self.create_text_output_item(text=diagnosis.summary, id="diagnosis"),
                )
            ],
            custom_outputs=custom_outputs_for(diagnosis),
        )


__all__ = [
    "DEFAULT_WINDOW_DAYS",
    "MAX_INPUT_ITEMS",
    "MlPlatformOperationsResponsesAgent",
    "custom_outputs_for",
    "extract_model_name",
    "extract_question",
    "extract_window_days",
]
