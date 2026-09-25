"""Configuration: what this deployment is allowed to look at.

EVERY ADDRESS IS AN ALLOW-LIST ENTRY, NOT A TEMPLATE
----------------------------------------------------
The evidence tables are named in full, per catalog, and built by this module —
never assembled from a user-supplied string. A `f"{catalog}.{schema}.{table}"`
built from request input is a table-name injection waiting to happen, and the
whole point of the read-only claim is that the set of things this agent can
read is fixed at startup by code nobody can talk round.

`GOVERNED_CATALOGS` matches Phase 15's own list and deliberately excludes
`sandbox`: sandbox is not a promotion stage, and a model there is not something
an operations agent should be diagnosing.

THRESHOLDS ARE MIRRORED, WITH THEIR SOURCE
------------------------------------------
`monitoring_thresholds` returns thresholds that each cite
`products/ml-lifecycle-demo/src/monitoring_logic.py`. The values are duplicated
here rather than imported because this product must not depend on
ml-lifecycle-demo's package — but the duplication is dangerous, so
`tests/test_config.py` reads the real constants out of that file and asserts
they still agree. A silent divergence would make every verdict wrong in a way
nothing else would catch.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

from ml_platform_operations_agent.domain import (
    EvidenceReference,
    MetricThreshold,
    SourceType,
    TimeWindow,
)
from ml_platform_operations_agent.errors import ConfigurationError

#: Catalogs this agent may diagnose models in. Mirrors
#: `ml_logic.GOVERNED_CATALOGS`; sandbox is excluded deliberately.
GOVERNED_CATALOGS: tuple[str, ...] = ("dev", "stg", "prod")

#: The one schema Phase 15 owns.
PRODUCT_SCHEMA = "ml_lifecycle_demo"

#: Evidence tables, by unqualified name. Anything not here cannot be read at
#: all — there is no code path that reaches a table outside this tuple.
EVIDENCE_TABLES: tuple[str, ...] = (
    "monitoring_history",
    "monitoring_alerts",
    "predictions",
    "inference_actuals",
)

#: The approved runbook set. Repository-resident because Phase 19.2a found no
#: UC volume exists (`volumes list` returned empty), so there is nowhere else
#: authoritative to read them from.
RUNBOOK_DOCUMENTS: tuple[str, ...] = ("docs/platform/ml-operations-runbook.md",)

#: The file the thresholds are defined in. Cited by every threshold so an
#: operator can check the number rather than take the agent's word for it.
THRESHOLD_SOURCE = "products/ml-lifecycle-demo/src/monitoring_logic.py"

#: Version stamps carried into traces and diagnoses in later subphases. Bumped
#: when the contract they name changes, so a stored diagnosis stays
#: interpretable.
EVIDENCE_CONTRACT_VERSION = "19.2-e1-e11"
POLICY_VERSION = "19.2b-1"

_MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9_]+\.[A-Za-z0-9_]+\.[A-Za-z0-9_]+$")


@dataclass(frozen=True, slots=True)
class EvidenceScope:
    """The addresses one catalog's evidence lives at.

    Frozen and built by `scope_for`, so a scope cannot be widened after
    construction by anything holding a reference to it.
    """

    catalog: str
    schema_name: str = PRODUCT_SCHEMA
    tables: tuple[str, ...] = field(default=EVIDENCE_TABLES)

    def table(self, name: str) -> str:
        """Fully qualified name for an allow-listed table.

        Raises rather than returning a best guess: a caller asking for a table
        outside the allow-list has a bug or is being driven by injected text,
        and neither deserves a working address.
        """
        if name not in self.tables:
            raise ConfigurationError(f"table '{name}' is not an allow-listed evidence table")
        return f"{self.catalog}.{self.schema_name}.{name}"

    @property
    def allowed_table_names(self) -> frozenset[str]:
        return frozenset(self.table(name) for name in self.tables)


def scope_for(catalog: str) -> EvidenceScope:
    """Build the evidence scope for a governed catalog."""
    if catalog not in GOVERNED_CATALOGS:
        raise ConfigurationError(
            "catalog is not one of the governed catalogs this agent may diagnose"
        )
    return EvidenceScope(catalog=catalog)


def parse_model_name(raw: str) -> tuple[str, str, str] | None:
    """Split a governed three-part model name, or None.

    None rather than an exception: an unrecognised model name is a REFUSAL with
    reason `unknown_model`, which is an answer the caller gets, not a fault.
    """
    candidate = raw.strip()
    if not _MODEL_NAME_RE.match(candidate):
        return None
    catalog, schema_name, name = candidate.split(".")
    if catalog not in GOVERNED_CATALOGS:
        return None
    return catalog, schema_name, name


def _threshold_reference(window: TimeWindow, observed_at: datetime) -> EvidenceReference:
    return EvidenceReference(
        source_type=SourceType.REPOSITORY_SOURCE,
        source_identifier=THRESHOLD_SOURCE,
        observed_at=observed_at,
        query_window=window,
        relevant_fields=("MONITOR_MAX_RMSE", "MONITOR_MIN_R2", "MAX_FEATURE_DRIFT"),
        limitations=(
            "Thresholds are version-controlled in the Phase 15 product and are "
            "mirrored here; they are not read from the workspace at runtime.",
        ),
    )


#: The live monitoring bounds, mirrored from Phase 15 `monitoring_logic.py`.
#: Kept as plain numbers so `tests/test_config.py` can compare them against the
#: real source file.
MONITOR_MAX_RMSE = 18.0
MONITOR_MIN_R2 = 0.85
MAX_FEATURE_DRIFT = 0.2
MAX_NULL_RATE = 0.01


def monitoring_thresholds(
    window: TimeWindow,
    observed_at: datetime | None = None,
) -> tuple[MetricThreshold, ...]:
    """The thresholds in force, each citing where it is defined.

    `observed_at` is passed in rather than read from a clock so a diagnosis is
    reproducible; it defaults to the window end, which is the moment the
    question is about.
    """
    moment = observed_at or window.end
    if moment.tzinfo is None:
        raise ConfigurationError("observed_at must be timezone-aware")
    reference = _threshold_reference(window, moment.astimezone(UTC))
    return (
        MetricThreshold(
            metric="rmse", breach_above=True, value=MONITOR_MAX_RMSE, defined_in=reference
        ),
        MetricThreshold(
            metric="r2", breach_above=False, value=MONITOR_MIN_R2, defined_in=reference
        ),
        MetricThreshold(
            metric="drift_score",
            breach_above=True,
            value=MAX_FEATURE_DRIFT,
            defined_in=reference,
        ),
        MetricThreshold(
            metric="null_rate", breach_above=True, value=MAX_NULL_RATE, defined_in=reference
        ),
    )


__all__ = [
    "EVIDENCE_CONTRACT_VERSION",
    "EVIDENCE_TABLES",
    "GOVERNED_CATALOGS",
    "MAX_FEATURE_DRIFT",
    "MAX_NULL_RATE",
    "MONITOR_MAX_RMSE",
    "MONITOR_MIN_R2",
    "POLICY_VERSION",
    "PRODUCT_SCHEMA",
    "RUNBOOK_DOCUMENTS",
    "THRESHOLD_SOURCE",
    "EvidenceScope",
    "monitoring_thresholds",
    "parse_model_name",
    "scope_for",
]
