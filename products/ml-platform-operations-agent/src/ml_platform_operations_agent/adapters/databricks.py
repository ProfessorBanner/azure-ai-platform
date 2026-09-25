"""Live read-only adapters. The ONLY module in this package that touches Azure.

WHAT THIS MODULE MAY DO
-----------------------
Read Unity Catalog metadata, read MLflow run metadata, and read repository
files. That is the entire surface.

WHAT IT CANNOT DO, STRUCTURALLY
--------------------------------
There is no method here that runs a job, writes a table, executes
caller-supplied SQL, registers a model, moves an alias, creates an experiment,
logs to MLflow or invokes an endpoint. `tests/test_live_adapters.py` walks this
module's AST and asserts that no such call appears — the read-only claim rests
on absent code, checkable without credentials, rather than on a promise.

NO SQL WAREHOUSE IS STARTED
---------------------------
Table EXISTENCE comes from Unity Catalog metadata (`w.tables.list`), which is a
control-plane call and needs no compute. Reading table ROWS would need the
stopped serverless warehouse, so `monitoring_history` returns a structured
absence describing exactly that, rather than starting compute to find out.

THE ABSENCE IS CITED TO THE SCHEMA, NOT THE MISSING TABLE
----------------------------------------------------------
When `monitoring_history` does not exist, the evidence is the SCHEMA'S TABLE
LISTING — the thing actually read. Citing `dev.ml_lifecycle_demo.monitoring_history`
would be a citation to something that does not exist, which an operator cannot
resolve and which is indistinguishable from a fabricated one.

THE CLI TAG-OMISSION TRAP
-------------------------
`databricks model-versions get` returns a payload with NO `tags` field, while
`GET /api/2.1/unity-catalog/models/{name}/versions/{v}` returns full provenance
plus `model_metrics`. An adapter built on the CLI shape sees `{}` and would
report complete provenance as missing. This module uses the REST endpoint and
sets `tags_available` from whether the key was present at all — which is why
`ModelVersionFacts` distinguishes "no tags" from "tags not returned".
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ml_platform_operations_agent.config import (
    RUNBOOK_DOCUMENTS,
    EvidenceScope,
    parse_model_name,
)
from ml_platform_operations_agent.domain import (
    AliasBinding,
    EvidenceAbsence,
    ModelVersionFacts,
    MonitoringObservation,
    PredictionSample,
    ResolvedModel,
    SourceType,
    TimeWindow,
)
from ml_platform_operations_agent.errors import ConfigurationError, EvidenceSourceError

#: The profile must be named explicitly. Falling back to DEFAULT would mean a
#: mistyped variable silently pointed a "DEV read-only proof" at whatever
#: workspace the default profile happens to name — possibly PROD.
PROFILE_VAR = "DATABRICKS_CONFIG_PROFILE"

#: Bounded result counts. An operations agent has no reason to enumerate a
#: registry, and an unbounded list is how a read-only call becomes an expensive
#: one.
MAX_VERSIONS = 25
MAX_RUNS = 20

#: Seconds. Applied where the SDK supports it so a hung control-plane call
#: fails rather than blocking a diagnosis indefinitely.
REQUEST_TIMEOUT_SECONDS = 30


def require_profile(environment: dict[str, str] | None = None) -> str:
    """Read the profile name, refusing to guess one."""
    env = os.environ if environment is None else environment
    profile = (env.get(PROFILE_VAR) or "").strip()
    if not profile:
        raise ConfigurationError(
            f"{PROFILE_VAR} must name a Databricks profile explicitly; "
            "this adapter never falls back to DEFAULT"
        )
    return profile


def _sanitise(error: Exception) -> str:
    """Reduce an SDK exception to a category name.

    THE ORIGINAL MESSAGE IS DISCARDED. An SDK error can carry the workspace
    URL, a request id, response headers, a fragment of the response body, or a
    filesystem path from the config chain. This value is returned to a caller
    and written to a log, so it carries the exception TYPE and nothing else.
    """
    return type(error).__name__


def _utc(value: Any) -> datetime:
    """Coerce an epoch-millis or datetime to an aware UTC datetime."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    return datetime.fromtimestamp(int(value) / 1000, tz=UTC)


# --- Registered model -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DatabricksModelRegistry:
    """Unity Catalog registered models, over the authoritative REST endpoint.

    `client` is a `databricks.sdk.WorkspaceClient`, injected rather than
    constructed here so tests never need credentials.
    """

    client: Any
    scope: EvidenceScope
    #: The window an absence record is scoped to. INJECTED, never read from a
    #: clock: an adapter that called `datetime.now()` would make two runs of
    #: the same question produce different evidence, and determinism is a gate.
    window: TimeWindow

    def resolve_model(self, full_name: str) -> ResolvedModel | EvidenceAbsence:
        parsed = parse_model_name(full_name)
        if parsed is None or parsed[0] != self.scope.catalog:
            # Validated against the scope BEFORE any call: an unvalidated name
            # would be interpolated into a REST path.
            return self._absent(full_name, "the model name is outside this agent's evidence scope")

        try:
            # `include_aliases=true` IS REQUIRED. Without it the endpoint
            # returns the model with an EMPTY alias list rather than an error —
            # a second lossy-default trap of the same family as the CLI's
            # omitted tags. An adapter that omitted it would conclude the model
            # has no Champion at all and quietly lose the only record of what
            # is serving.
            model = self._get(f"/api/2.1/unity-catalog/models/{full_name}?include_aliases=true")
        except Exception as error:  # noqa: BLE001 - sanitised below
            if self._is_not_found(error):
                return self._absent(full_name, "no registered model with this name exists")
            raise EvidenceSourceError(
                f"the Unity Catalog registry could not be read ({_sanitise(error)})"
            ) from None

        aliases = tuple(
            # The ORIGINAL casing is preserved. Unity Catalog returns
            # `champion`; Phase 15 writes `Champion`. `AliasBinding.matches`
            # compares case-insensitively, so both resolve, and evidence shows
            # what the workspace actually said.
            AliasBinding(name=str(a["alias_name"]), version=int(a["version_num"]))
            for a in (model.get("aliases") or [])
            if a.get("alias_name") and a.get("version_num") is not None
        )

        catalog, schema_name, name = parsed
        return ResolvedModel(
            catalog=catalog,
            schema_name=schema_name,
            name=name,
            aliases=aliases,
            versions=self._versions(full_name, aliases),
        )

    def _versions(
        self, full_name: str, aliases: tuple[AliasBinding, ...]
    ) -> tuple[ModelVersionFacts, ...]:
        """Fetch facts for the aliased versions only.

        BOUNDED BY THE ALIASES, not by enumerating the registry. The bounded
        question is about what is serving, and the aliases are what say so.
        """
        wanted = sorted({binding.version for binding in aliases})[:MAX_VERSIONS]
        facts: list[ModelVersionFacts] = []

        for version in wanted:
            try:
                payload = self._get(f"/api/2.1/unity-catalog/models/{full_name}/versions/{version}")
            except Exception as error:  # noqa: BLE001
                if self._is_not_found(error):
                    continue
                raise EvidenceSourceError(
                    f"a model version could not be read ({_sanitise(error)})"
                ) from None

            # THE TAG-OMISSION TRAP. `tags` absent from the payload is a
            # different fact from `tags` present and empty, and only the REST
            # endpoint distinguishes them.
            raw_tags = payload.get("tags")
            facts.append(
                ModelVersionFacts(
                    version=int(payload["version"]) if "version" in payload else version,
                    created_at=_utc(payload.get("created_at", 0)),
                    status=str(payload.get("status", "UNKNOWN")),
                    run_id=payload.get("run_id"),
                    tags={
                        str(t["key"]): str(t["value"])
                        for t in (raw_tags or [])
                        if "key" in t and "value" in t
                    },
                    tags_available=raw_tags is not None,
                    metrics={
                        str(m["key"]): float(m["value"])
                        for m in (payload.get("model_metrics") or [])
                        if "key" in m and m.get("value") is not None
                    },
                )
            )

        return tuple(facts)

    def _get(self, path: str) -> dict[str, Any]:
        result = self.client.api_client.do("GET", path)
        return result if isinstance(result, dict) else {}

    @staticmethod
    def _is_not_found(error: Exception) -> bool:
        return type(error).__name__ in {"NotFound", "ResourceDoesNotExist"}

    def _absent(self, full_name: str, reason: str) -> EvidenceAbsence:
        return EvidenceAbsence(
            source_type=SourceType.UNITY_CATALOG_SCHEMA,
            source_identifier=f"{self.scope.catalog}.{self.scope.schema_name}",
            reason=reason,
            query_window=self.window,
        )


# --- MLflow runs ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DatabricksRunHistory:
    """MLflow runs, reached ONLY through model versions.

    `mlflow_client` is an `mlflow.MlflowClient` configured against
    `databricks-uc`, injected for testability.

    There is deliberately no experiment search here. Searching all workspace
    experiments would let this agent read runs belonging to products it has no
    business inspecting, and the bounded question does not need it: a run is
    relevant precisely because a registered version points at it.
    """

    mlflow_client: Any
    registry: DatabricksModelRegistry

    def recent_runs(
        self,
        full_name: str,
        window: TimeWindow,
        limit: int = MAX_RUNS,
    ) -> tuple[tuple[str, dict[str, float]], ...] | EvidenceAbsence:
        resolved = self.registry.resolve_model(full_name)
        if isinstance(resolved, EvidenceAbsence):
            return resolved

        run_ids = [v.run_id for v in resolved.versions if v.run_id][: min(limit, MAX_RUNS)]
        if not run_ids:
            return EvidenceAbsence(
                source_type=SourceType.UNITY_CATALOG_MODEL,
                source_identifier=resolved.full_name,
                reason="no model version in scope references an MLflow run",
                query_window=window,
            )

        collected: list[tuple[str, dict[str, float]]] = []
        for run_id in run_ids:
            try:
                run = self.mlflow_client.get_run(run_id)
            except Exception as error:  # noqa: BLE001
                raise EvidenceSourceError(
                    f"an MLflow run could not be read ({_sanitise(error)})"
                ) from None

            started = _utc(run.info.start_time)
            if not window.covers(started):
                continue
            # Metrics only. No artifact is listed or downloaded: an operations
            # agent has no business pulling model binaries.
            collected.append(
                (run_id, {str(k): float(v) for k, v in dict(run.data.metrics).items()})
            )

        if not collected:
            return EvidenceAbsence(
                source_type=SourceType.UNITY_CATALOG_MODEL,
                source_identifier=resolved.full_name,
                reason="no MLflow run linked to a current model version falls inside the window",
                query_window=window,
            )
        return tuple(collected)


# --- Monitoring -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DatabricksMonitoring:
    """Monitoring tables, by Unity Catalog METADATA only.

    Existence is a control-plane question and is answered without compute.
    Reading rows is not, so it is reported as unavailable rather than performed.
    """

    client: Any
    scope: EvidenceScope

    def _existing_tables(self) -> frozenset[str]:
        try:
            listing = self.client.tables.list(
                catalog_name=self.scope.catalog, schema_name=self.scope.schema_name
            )
            return frozenset(str(t.name) for t in listing)
        except Exception as error:  # noqa: BLE001
            raise EvidenceSourceError(
                f"the schema table listing could not be read ({_sanitise(error)})"
            ) from None

    def monitoring_history(
        self,
        full_name: str,
        window: TimeWindow,
    ) -> tuple[MonitoringObservation, ...] | EvidenceAbsence:
        present = self._existing_tables()
        schema_id = f"{self.scope.catalog}.{self.scope.schema_name}"

        if "monitoring_history" not in present:
            # ABSENT, OR MERELY INVISIBLE? THE DIFFERENCE MATTERS.
            #
            # `tables.list` returns only what the CALLER may see. A table the
            # identity lacks privileges on is omitted exactly like one that
            # does not exist, and Unity Catalog does not distinguish them for
            # a non-privileged principal — by design, so existence is not
            # leaked. So "not in the listing" alone cannot support the claim
            # "this table does not exist".
            #
            # An empty listing is the tell. If the identity can see NOTHING in
            # the schema, its visibility is unproven and absence is not
            # assertable. If it can see at least one table, the listing is
            # demonstrably returning content, and a monitoring table missing
            # from it is genuinely missing.
            #
            # Reporting invisibility as absence would turn a permissions gap
            # into "no monitoring exists" — a confident, wrong answer about
            # the one thing this agent is for.
            if not present:
                return EvidenceAbsence(
                    source_type=SourceType.UNITY_CATALOG_SCHEMA,
                    source_identifier=schema_id,
                    reason=(
                        "visibility_unverified: this identity can see no table in the "
                        "schema, so a missing monitoring table cannot be distinguished "
                        "from one it lacks permission to see"
                    ),
                    query_window=window,
                )

            # Cited to the SCHEMA LISTING — the thing actually read. Citing the
            # missing table would be a citation to something that does not
            # exist.
            return EvidenceAbsence(
                source_type=SourceType.UNITY_CATALOG_SCHEMA,
                source_identifier=schema_id,
                reason=(
                    "table_absent: monitoring_history is not present in this schema's "
                    f"table listing (which returned {len(present)} other tables, so the "
                    "listing is visible to this identity); no live performance series "
                    "exists for any model here"
                ),
                query_window=window,
            )

        # The table exists but its ROWS need warehouse compute, which this
        # phase must not start. Reported as an explicit unavailability rather
        # than silently returning zero rows — zero rows would read as "measured
        # and found nothing", which is a different and much more dangerous claim.
        return EvidenceAbsence(
            source_type=SourceType.UNITY_CATALOG_SCHEMA,
            source_identifier=schema_id,
            reason=(
                "monitoring_history exists but reading its rows requires SQL warehouse "
                "compute, which this read-only phase does not start"
            ),
            query_window=window,
        )

    def predictions(
        self,
        full_name: str,
        window: TimeWindow,
        limit: int = 50,
    ) -> tuple[PredictionSample, ...] | EvidenceAbsence:
        return EvidenceAbsence(
            source_type=SourceType.UNITY_CATALOG_SCHEMA,
            source_identifier=f"{self.scope.catalog}.{self.scope.schema_name}",
            reason=(
                "reading scored rows requires SQL warehouse compute, which this "
                "read-only phase does not start"
            ),
            query_window=window,
        )


# --- Runbooks ---------------------------------------------------------------

_HEADING = re.compile(r"^(#{2,3})\s+(.*)$")


def _anchor(heading: str) -> str:
    """GitHub-style anchor. Deterministic, so a citation is stable."""
    slug = re.sub(r"[^\w\s-]", "", heading.strip().casefold())
    return re.sub(r"[\s_]+", "-", slug).strip("-")


@dataclass(frozen=True, slots=True)
class RepositoryRunbooks:
    """Approved runbooks, from the repository working tree.

    Repository-resident because Phase 19.2a found no UC volume exists. The path
    allow-list is `config.RUNBOOK_DOCUMENTS`; nothing else is readable, and no
    workspace or network call is made.

    THE CONTENT IS EVIDENCE, NEVER INSTRUCTION. Sections are scored by keyword
    overlap and returned as text. `policy.scan_untrusted_text` annotates
    instruction-shaped content, and nothing in the agent branches on it.
    """

    repository_root: Path
    #: Injected for the same reason as above.
    window: TimeWindow
    documents: tuple[str, ...] = RUNBOOK_DOCUMENTS

    def search(
        self,
        query: str,
        limit: int = 3,
    ) -> tuple[tuple[str, str, str], ...] | EvidenceAbsence:
        terms = {t for t in re.findall(r"[a-z0-9_]+", query.casefold()) if len(t) > 3}
        scored: list[tuple[int, str, str, str]] = []

        for relative in self.documents:
            path = (self.repository_root / relative).resolve()
            # Containment check: a document outside the repository is one
            # nobody reviewed. The allow-list already prevents this; the check
            # is here because a path allow-list plus a symlink is not a
            # containment boundary on its own.
            if not path.is_file() or self.repository_root.resolve() not in path.parents:
                continue
            for anchor, heading, body in self._sections(path):
                haystack = f"{heading} {body}".casefold()
                score = sum(1 for term in terms if term in haystack)
                if score:
                    scored.append((score, relative, anchor, body.strip()))

        if not scored:
            return EvidenceAbsence(
                source_type=SourceType.REPOSITORY_DOCUMENT,
                source_identifier=self.documents[0],
                reason="no approved runbook section matches this query",
                query_window=self.window,
            )

        # Sorted by score then by identifier: ties must not depend on
        # filesystem order, because determinism is a gate.
        scored.sort(key=lambda item: (-item[0], item[1], item[2]))
        return tuple((relative, anchor, body) for _s, relative, anchor, body in scored[:limit])

    @staticmethod
    def _sections(path: Path) -> list[tuple[str, str, str]]:
        text = path.read_text(encoding="utf-8")
        sections: list[tuple[str, str, str]] = []
        anchor = heading = ""
        buffer: list[str] = []

        for line in text.splitlines():
            match = _HEADING.match(line)
            if match:
                if heading:
                    sections.append((anchor, heading, "\n".join(buffer)))
                heading = match.group(2).strip()
                anchor = _anchor(heading)
                buffer = []
            else:
                buffer.append(line)

        if heading:
            sections.append((anchor, heading, "\n".join(buffer)))
        return sections


__all__ = [
    "MAX_RUNS",
    "MAX_VERSIONS",
    "PROFILE_VAR",
    "REQUEST_TIMEOUT_SECONDS",
    "DatabricksModelRegistry",
    "DatabricksMonitoring",
    "DatabricksRunHistory",
    "RepositoryRunbooks",
    "require_profile",
]
