"""The evidence-source boundary. Pure typing; imports nothing outside this package.

WHY THIS FILE IS THE WHOLE POINT OF 19.2b
------------------------------------------
Everything above this line is controlled Python that can be reasoned about and
tested on a laptop. Everything below it is a workspace. Phase 19.2c implements
these same protocols with `MlflowClient`, Unity Catalog REST and the SQL
Statement Execution API, and nothing in `agent.py`, `policy.py` or
`evidence.py` changes when it does.

That is also what makes the offline gate structural rather than aspirational.
`mlflow` and `databricks-sdk` are not installed (see pyproject.toml), so there
is no way for a live call to appear anywhere except in a module that implements
one of these protocols.

ABSENCE IS A RETURN VALUE, NOT AN EXCEPTION
--------------------------------------------
Every method returns a result that can express "the data is not there".
`monitoring_history` in DEV genuinely does not exist (19.2a, gaps G1/G2), and
an interface that could only express absence by raising would force every
caller into a try/except whose except branch is the common case. A protocol
that makes the normal path exceptional is a protocol that will be got wrong.

`EvidenceSourceError` is reserved for a source that was expected to answer and
malfunctioned — a timeout, a permission failure, a malformed payload.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ml_platform_operations_agent.domain import (
    EvidenceAbsence,
    MonitoringObservation,
    PredictionSample,
    ResolvedModel,
    TimeWindow,
)


@runtime_checkable
class ModelRegistrySource(Protocol):
    """Unity Catalog registered models and versions.

    In 19.2c this is `MlflowClient` plus the Unity Catalog REST endpoint —
    NOT `databricks model-versions get`, which omits version tags entirely
    (19.2a). An adapter built on the CLI shape would report complete provenance
    as missing.
    """

    def resolve_model(self, full_name: str) -> ResolvedModel | EvidenceAbsence:
        """Resolve a three-part name to registry facts, or report it absent.

        Absence here means "no such registered model", which becomes a refusal
        with reason `unknown_model` rather than an error.
        """
        ...


@runtime_checkable
class RunHistorySource(Protocol):
    """MLflow experiments and runs.

    Bounded by construction: the window and a result ceiling are parameters, and
    there is no method that downloads an artifact. An operations agent has no
    business pulling model binaries.
    """

    def recent_runs(
        self,
        full_name: str,
        window: TimeWindow,
        limit: int = 20,
    ) -> tuple[tuple[str, dict[str, float]], ...] | EvidenceAbsence:
        """Return `(run_id, metrics)` pairs inside the window.

        NOTE FOR CALLERS: in the live DEV workspace every run carries identical
        metrics (19.2a), so this source cannot discriminate a degraded model
        from a healthy one. It is provenance, not a degradation signal, and
        `evidence.permits_degradation_finding` deliberately does not accept it
        as one.
        """
        ...


@runtime_checkable
class MonitoringSource(Protocol):
    """The `monitoring_history` / `monitoring_alerts` tables.

    The only source that can establish degradation. Returns `EvidenceAbsence`
    when the table does not exist — the ordinary DEV case.
    """

    def monitoring_history(
        self,
        full_name: str,
        window: TimeWindow,
    ) -> tuple[MonitoringObservation, ...] | EvidenceAbsence: ...

    def predictions(
        self,
        full_name: str,
        window: TimeWindow,
        limit: int = 50,
    ) -> tuple[PredictionSample, ...] | EvidenceAbsence:
        """Scored rows. `prediction` is array-typed in the live schema (19.2a)."""
        ...


@runtime_checkable
class RunbookSource(Protocol):
    """Approved operational documents.

    Repository-resident: Phase 19.2a found no UC volume exists. Returns
    `(document_path, section_anchor, excerpt)` triples so a citation can name a
    stable location inside a document rather than the whole file.

    The excerpt is EVIDENCE, never instruction. `policy.scan_untrusted_text`
    annotates it; nothing branches on it.
    """

    def search(
        self,
        query: str,
        limit: int = 3,
    ) -> tuple[tuple[str, str, str], ...] | EvidenceAbsence: ...


__all__ = [
    "ModelRegistrySource",
    "MonitoringSource",
    "RunHistorySource",
    "RunbookSource",
]
