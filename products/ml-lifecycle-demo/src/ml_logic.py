"""Environment-neutral ML logic for ml-lifecycle-demo.

Everything in this module is pure Python plus NumPy and scikit-learn: no Spark
session, no MLflow client, no Databricks SDK, no Azure credential. That is the
point. The lifecycle's decisions — what the data is, what the model is called,
whether a trained model is good enough to promote — are the parts worth testing,
and they are testable on a laptop in milliseconds only if they never touch a
runtime. The task entrypoints (prepare_data, train_register, batch_inference)
hold the runtime I/O and delegate every decision here.

Environment-owned values (catalog, schema) arrive through the process
environment, injected by the Bundle target. They are never literals here.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray
from sklearn.datasets import make_regression
from sklearn.metrics import r2_score, root_mean_squared_error

# --- Governed naming --------------------------------------------------------

#: The one schema this product owns, in every catalog. Terraform creates it
#: (infrastructure/modules/databricks_product_uc); the product only fills it.
PRODUCT_SCHEMA = "ml_lifecycle_demo"

#: Catalogs this product is allowed to run against — one per governed
#: environment. Sandbox is deliberately absent: it is not a promotion stage.
GOVERNED_CATALOGS = ("dev", "stg", "prod")

TRAINING_TABLE = "training_data"
BATCH_INPUT_TABLE = "batch_input"
PREDICTIONS_TABLE = "predictions"

#: Candidate scoring output. Kept SEPARATE from PREDICTIONS_TABLE on purpose: a
#: candidate is an unapproved model, and letting it overwrite the table that
#: production consumers read would make "validated the candidate" and "shipped
#: the candidate" the same act.
CANDIDATE_PREDICTIONS_TABLE = "candidate_predictions"

#: Delayed ground truth for the batch rows — the labels that only become known
#: after the fact. Kept in its OWN table, holding row_id and label and no
#: features, so that it is structurally unusable as an inference input. Nothing
#: in the scoring path may read it: a model that saw its own answers would score
#: perfectly and monitor perfectly, and the monitoring would be worthless.
INFERENCE_ACTUALS_TABLE = "inference_actuals"

#: The training set a RETRAINING run fits on: the original training_data plus
#: the batch observations whose ground truth has since arrived. Kept separate
#: from training_data so the original set stays a stable, reproducible baseline
#: — a retrain that overwrote it would destroy the very thing cross-environment
#: metric comparison depends on.
RETRAINING_DATA_TABLE = "retraining_data"

#: Append-only monitoring record. One row per monitor run, so the history of a
#: model's behaviour survives the run that observed it.
MONITORING_HISTORY_TABLE = "monitoring_history"

#: Append-only alert record. For Phase 15 this IS the alert channel — see
#: docs/platform/ml-operations-runbook.md.
MONITORING_ALERTS_TABLE = "monitoring_alerts"

REGISTERED_MODEL_NAME = "linear_regression_model"
PRODUCT_NAME = "ml-lifecycle-demo"

# --- Release aliases --------------------------------------------------------
#
# Two aliases, two meanings, and the distinction is the whole of Phase 15:
#
#   Candidate — set by training, in every environment, on the version it just
#               registered. Says only "this version passed its metric gate".
#   Champion  — the release pointer. In DEV and STG it follows Candidate
#               automatically; in PROD it moves only after a human approves one
#               EXACT numbered version.
#
# ALIASES ARE AUTHORITATIVE MUTABLE STATE. They are the only record of what is
# current, and they are expected to move. Anything that resolves one must
# therefore resolve it ONCE and then work from the resulting version number —
# see ProductConfig.version_model_uri.
CANDIDATE_ALIAS = "Candidate"
CHAMPION_ALIAS = "Champion"

# --- Model-version tag keys -------------------------------------------------
#
# MODEL-VERSION TAGS ARE IMMUTABLE PROVENANCE. They record facts about how a
# version came to exist — which commit, which environment, which run, what it
# scored — and nothing may rewrite them afterwards. That is what makes them
# trustworthy evidence for a promotion decision.
#
# The division of labour is strict, and the two halves must not be confused:
#
#   alias  -> current state, mutable, authoritative  ("what is Champion now?")
#   tag    -> historical fact, immutable             ("what was this version?")
#
# `registered_as` is a tag, so it says what the version was registered AS at
# creation time — always "candidate". It is deliberately NOT a release-status
# field that later gets rewritten to "champion": that would make a tag mutable
# and give the registry two competing answers to "what is Champion?", one of
# which could silently go stale. The alias is the only answer to that question.
#
# A run tag describes an experiment; a model-version tag travels with the
# registered artefact and is what a promotion pipeline can independently verify.
PRODUCT_TAG = "product"
ENVIRONMENT_TAG = "environment"
GIT_SHA_TAG = "git_sha"
BUNDLE_TARGET_TAG = "bundle_target"
TRAINING_RUN_ID_TAG = "training_run_id"
EVALUATION_RMSE_TAG = "evaluation_rmse"
EVALUATION_R2_TAG = "evaluation_r2"
REGISTERED_AS_TAG = "registered_as"

REGISTERED_AS_CANDIDATE = "candidate"

# --- Dataset shape ----------------------------------------------------------

#: One seed for the whole product. Every source of randomness below derives from
#: it, so a run in DEV, STG and PROD at the same commit sees identical data and
#: trains an identical model. That is what makes cross-environment comparison of
#: RMSE and R2 meaningful rather than noise.
RANDOM_STATE = 42

TRAINING_ROWS = 500
BATCH_ROWS = 100
TOTAL_ROWS = TRAINING_ROWS + BATCH_ROWS

FEATURE_COLUMNS = ("feature_0", "feature_1", "feature_2")
LABEL_COLUMN = "label"
ID_COLUMN = "row_id"
PREDICTION_COLUMN = "prediction"

NOISE = 10.0
BIAS = 5.0

#: Held-out fraction used to evaluate the model. The split is seeded, so the
#: same rows are held out on every run.
TEST_FRACTION = 0.2

# --- Promotion threshold ----------------------------------------------------
#
# The threshold is encoded here, in versioned source, rather than configured per
# environment. A model that is not good enough for PROD is not good enough for
# DEV either, and a per-environment threshold would let a weaker model pass the
# early gates and fail late. Changing these numbers is a reviewed code change
# that flows through the same DEV -> STG -> PROD promotion as everything else.
#
# Calibrated against the generative process above: with NOISE=10.0 the
# irreducible error is ~10.0 RMSE, so a correctly fitted linear model lands just
# above it. These bounds catch a broken pipeline, not a marginally worse fit.
MAX_RMSE = 15.0
MIN_R2 = 0.90


class EvaluationThresholdError(RuntimeError):
    """Raised when a trained model fails the promotion threshold.

    Raising rather than returning is deliberate: an uncaught exception fails the
    Databricks task, which fails the job, which fails the deployment pipeline.
    A model that cannot clear the bar must never reach the registry or the
    Champion alias.
    """


class ProvenanceError(RuntimeError):
    """Raised when a model version is not the artefact it claims to be.

    Wrong environment, wrong commit, or a missing provenance tag. Every caller
    treats this as fatal: a promotion pipeline that cannot prove WHICH artefact
    it is about to make Champion must not make anything Champion.
    """


@dataclass(frozen=True)
class ProductConfig:
    """The environment-owned coordinates of one deployment of this product."""

    catalog: str
    schema: str

    def table(self, table_name: str) -> str:
        """Fully qualified Unity Catalog name of a product-owned table."""
        if not table_name:
            raise ValueError("table_name must not be empty")

        return f"{self.catalog}.{self.schema}.{table_name}"

    @property
    def training_table(self) -> str:
        return self.table(TRAINING_TABLE)

    @property
    def batch_input_table(self) -> str:
        return self.table(BATCH_INPUT_TABLE)

    @property
    def predictions_table(self) -> str:
        return self.table(PREDICTIONS_TABLE)

    @property
    def candidate_predictions_table(self) -> str:
        return self.table(CANDIDATE_PREDICTIONS_TABLE)

    @property
    def inference_actuals_table(self) -> str:
        return self.table(INFERENCE_ACTUALS_TABLE)

    @property
    def retraining_data_table(self) -> str:
        return self.table(RETRAINING_DATA_TABLE)

    @property
    def monitoring_history_table(self) -> str:
        return self.table(MONITORING_HISTORY_TABLE)

    @property
    def monitoring_alerts_table(self) -> str:
        return self.table(MONITORING_ALERTS_TABLE)

    @property
    def model_name(self) -> str:
        """Three-level Unity Catalog name of the registered model."""
        return f"{self.catalog}.{self.schema}.{REGISTERED_MODEL_NAME}"

    @property
    def champion_model_uri(self) -> str:
        """URI resolving to whichever version currently holds Champion.

        Use this to RESOLVE the alias once, never to score with. Scoring goes
        through version_model_uri with the resolved number — see below.
        """
        return f"models:/{self.model_name}@{CHAMPION_ALIAS}"

    @property
    def candidate_model_uri(self) -> str:
        """URI resolving to whichever version currently holds Candidate.

        Same rule as champion_model_uri: resolve once, then pin the number.
        """
        return f"models:/{self.model_name}@{CANDIDATE_ALIAS}"

    def version_model_uri(self, version: int | str) -> str:
        """URI of one EXACT numbered version.

        This is what scoring and promotion must use. An alias is a mutable
        pointer: between resolving `@Candidate` and scoring it, a concurrent
        training run can move it, and the job would then score — or a pipeline
        would promote — an artefact nobody evaluated. Resolving the alias once
        and pinning the integer closes that window, which is also why the
        Bundle jobs run with max_concurrent_runs: 1.
        """
        return f"models:/{self.model_name}/{parse_model_version(version)}"


#: The only tables training may fit on. An allowlist rather than a free-form
#: name: the training table arrives as a job parameter, and a parameter that
#: could name any table would let a job be pointed at another product's data —
#: or another environment's — by editing a string in the Databricks UI.
ALLOWED_TRAINING_TABLES = (TRAINING_TABLE, RETRAINING_DATA_TABLE)


def select_training_table(config: ProductConfig, table_name: str | None = None) -> str:
    """Resolve the requested training table to a governed, fully-qualified name.

    Takes a SHORT name and qualifies it with the environment's own catalog and
    schema, so the result is always inside this product's namespace in this
    environment. A caller cannot reach another catalog no matter what it passes,
    because the catalog is never taken from the argument.

    ``None`` keeps the governed default, which is what the ordinary candidate
    job uses; retraining passes ``retraining_data`` explicitly.
    """
    if table_name is None:
        return config.training_table

    requested = table_name.strip()

    if not requested:
        raise ValueError("training table name must not be empty")

    if requested not in ALLOWED_TRAINING_TABLES:
        raise ValueError(
            f"training table must be one of {', '.join(ALLOWED_TRAINING_TABLES)} "
            f"(got '{requested}')"
        )

    return config.table(requested)


def parse_model_version(version: int | str) -> int:
    """Coerce a model version to a positive integer, or fail closed.

    Version numbers reach Python and Bash from CLI JSON and from Azure Pipelines
    variables, both of which can hand over an empty string or an unresolved
    macro without erroring. Treating "" or "$(candidateVersion)" as a version
    would build a URI that resolves to nothing, or worse, to something else.
    """
    text = str(version).strip()

    if not text:
        raise ProvenanceError("model version is empty")

    if not text.isdigit():
        raise ProvenanceError(f"model version must be a positive integer (got '{text}')")

    number = int(text)

    if number < 1:
        raise ProvenanceError(f"model version must be a positive integer (got '{text}')")

    return number


def build_run_tags(
    config: ProductConfig,
    env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Tags for the MLflow RUN — the experiment-side record of the training.

    Recorded when available and omitted when not, so an interactive run is still
    possible without faking pipeline metadata.
    """
    source = os.environ if env is None else env

    tags = {
        ENVIRONMENT_TAG: config.catalog,
        PRODUCT_TAG: PRODUCT_NAME,
    }

    git_sha = source.get("GIT_SHA", "").strip()
    if git_sha:
        tags[GIT_SHA_TAG] = git_sha

    bundle_target = source.get("BUNDLE_TARGET", "").strip()
    if bundle_target:
        tags[BUNDLE_TARGET_TAG] = bundle_target

    return tags


def build_model_version_tags(
    config: ProductConfig,
    training_run_id: str,
    result: EvaluationResult,
    env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Tags for the MODEL VERSION — the artefact-side record of the training.

    A superset of the run tags. The run tags describe an experiment that may be
    deleted or hard to find; these travel with the registered version, which is
    what a promotion pipeline can fetch and verify on its own before deciding to
    move Champion. The metrics are duplicated here deliberately, so the evidence
    an approver reads and the artefact they approve are the same object.

    registered_as is 'candidate' for every version, in every environment, and is
    never rewritten. It is a statement about how the version was created, not a
    release status: which version is Champion is answered by the alias alone.
    """
    tags = dict(build_run_tags(config, env))

    run_id = training_run_id.strip()
    if not run_id:
        raise ValueError("training_run_id must not be empty")

    tags[TRAINING_RUN_ID_TAG] = run_id
    tags[EVALUATION_RMSE_TAG] = f"{result.rmse:.6f}"
    tags[EVALUATION_R2_TAG] = f"{result.r2:.6f}"
    tags[REGISTERED_AS_TAG] = REGISTERED_AS_CANDIDATE

    return tags


def verify_model_version_provenance(
    tags: Mapping[str, str],
    *,
    expected_environment: str,
    expected_git_sha: str | None = None,
) -> None:
    """Fail closed unless a model version's tags match what was expected.

    The environment check catches the serious mistake — promoting an artefact
    trained against a different catalog's data. The Git SHA check is applied
    only when a SHA was supplied, matching how training records it: when a
    bundle is deployed outside a Git checkout there is no commit to record, and
    demanding one would block a legitimate interactive run. When a SHA IS
    supplied, an absent tag is a failure, not a pass — otherwise the check would
    silently weaken exactly where provenance matters most.
    """
    actual_environment = tags.get(ENVIRONMENT_TAG, "").strip()

    if not actual_environment:
        raise ProvenanceError(f"model version carries no '{ENVIRONMENT_TAG}' tag")

    if actual_environment != expected_environment:
        raise ProvenanceError(
            f"model version was trained in '{actual_environment}', "
            f"expected '{expected_environment}'"
        )

    if expected_git_sha is None or not expected_git_sha.strip():
        return

    expected = expected_git_sha.strip()
    actual_git_sha = tags.get(GIT_SHA_TAG, "").strip()

    if not actual_git_sha:
        raise ProvenanceError(
            f"model version carries no '{GIT_SHA_TAG}' tag, but commit {expected} was expected"
        )

    if actual_git_sha != expected:
        raise ProvenanceError(
            f"model version was trained from commit {actual_git_sha}, expected {expected}"
        )


def resolve_config(env: Mapping[str, str] | None = None) -> ProductConfig:
    """Read and validate the environment-injected catalog and schema.

    Fails closed. An absent or unexpected value means the job is not running
    where it thinks it is, and the correct response is to stop before writing
    anything — not to fall back to a default catalog and quietly write DEV data
    into the wrong place.
    """
    source = os.environ if env is None else env

    catalog = source.get("CATALOG", "").strip()
    schema = source.get("SCHEMA", "").strip()

    if not catalog or not schema:
        raise ValueError("CATALOG and SCHEMA must be supplied by the job cluster environment")

    if catalog not in GOVERNED_CATALOGS:
        raise ValueError(f"CATALOG must be one of {', '.join(GOVERNED_CATALOGS)} (got '{catalog}')")

    if schema != PRODUCT_SCHEMA:
        raise ValueError(f"SCHEMA must be '{PRODUCT_SCHEMA}' (got '{schema}')")

    return ProductConfig(catalog=catalog, schema=schema)


# --- Deterministic synthetic data -------------------------------------------


def generate_regression_data(
    n_rows: int = TOTAL_ROWS,
    random_state: int = RANDOM_STATE,
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """Generate the product's synthetic regression dataset.

    Synthetic and seeded, not an external download: the lifecycle must be
    reproducible from the repository alone, with no network dependency and no
    dataset licence to reason about, and every environment must see byte-identical
    inputs.
    """
    if n_rows <= 0:
        raise ValueError("n_rows must be positive")

    features, labels = make_regression(
        n_samples=n_rows,
        n_features=len(FEATURE_COLUMNS),
        n_informative=len(FEATURE_COLUMNS),
        noise=NOISE,
        bias=BIAS,
        random_state=random_state,
    )

    return features.astype(np.float64), labels.astype(np.float64)


def build_records(
    features: NDArray[np.float64],
    labels: NDArray[np.float64] | None,
    start_id: int = 0,
) -> list[dict[str, float]]:
    """Turn arrays into row dicts, ready for a Spark DataFrame.

    ``labels=None`` produces unlabelled rows, which is what batch input is.
    """
    if labels is not None and len(features) != len(labels):
        raise ValueError("features and labels must have the same length")

    records: list[dict[str, float]] = []

    for offset, row in enumerate(features):
        record: dict[str, float] = {ID_COLUMN: float(start_id + offset)}
        record.update(
            {name: float(value) for name, value in zip(FEATURE_COLUMNS, row, strict=True)}
        )

        if labels is not None:
            record[LABEL_COLUMN] = float(labels[offset])

        records.append(record)

    return records


def training_records() -> list[dict[str, float]]:
    """Labelled rows written to the training_data table."""
    features, labels = generate_regression_data()

    return build_records(features[:TRAINING_ROWS], labels[:TRAINING_ROWS], start_id=0)


def actuals_records() -> list[dict[str, float]]:
    """Delayed ground truth for the batch rows: row_id and label only.

    These are the SAME labels the generator produced for the batch rows, held
    back from batch_input and surfaced separately, which is how real delayed
    ground truth behaves: the outcome exists, it simply is not known at scoring
    time. Monitoring joins them back by row_id once they are available.

    Deliberately carries no features. A table of answers that also carried the
    questions could be joined into the scoring path by mistake; this one cannot.
    """
    _, labels = generate_regression_data()

    return [
        {ID_COLUMN: float(TRAINING_ROWS + offset), LABEL_COLUMN: float(label)}
        for offset, label in enumerate(labels[TRAINING_ROWS:])
    ]


def batch_input_records() -> list[dict[str, float]]:
    """Unlabelled rows written to the batch_input table.

    Cut from the SAME generated dataset as the training rows, so batch input is
    drawn from the same generative process the model was fitted on. Generating it
    from a second, independently seeded call would silently change the underlying
    coefficients and make inference look broken when it is not.
    """
    features, _ = generate_regression_data()

    return build_records(features[TRAINING_ROWS:], None, start_id=TRAINING_ROWS)


# --- Retraining set assembly ------------------------------------------------
#
# Pure record-level functions, deliberately. The tables involved are bounded by
# construction — TRAINING_ROWS + BATCH_ROWS, a few hundred rows — so a Spark
# join would add untested distributed code to solve a problem this product does
# not have. If the dataset ever stopped being generated and started arriving
# from somewhere real, this is the first thing to move into Spark.


class RetrainingDataError(RuntimeError):
    """Raised when newly labelled observations cannot be trusted.

    Every case is fatal. A retraining set assembled from a partial or ambiguous
    join is worse than no retrain at all: it trains a model on a silently
    filtered slice of reality and then promotes it as a candidate.
    """


def join_labelled_observations(
    features: Sequence[Mapping[str, float]],
    actuals: Sequence[Mapping[str, float]],
) -> list[dict[str, float]]:
    """Join batch features to their delayed labels by row_id.

    This is the step that turns "predictions we made" into "observations we can
    learn from". It fails closed on three conditions, because each one means the
    join is not the join the caller thinks it is:

      * an empty result — no ground truth has arrived, so there is nothing new
        to learn and a retrain would just refit the original data;
      * duplicate row_ids on either side — a row_id that appears twice makes the
        join ambiguous and would silently weight one observation double;
      * a batch row with no label — the ground truth is incomplete, and training
        on the labelled subset alone is a biased sample nobody chose.
    """
    if not features:
        raise RetrainingDataError("no batch features supplied to join")

    feature_ids = [record[ID_COLUMN] for record in features]
    actual_ids = [record[ID_COLUMN] for record in actuals]

    for label, ids in (("batch feature", feature_ids), ("actuals", actual_ids)):
        duplicates = sorted({value for value in ids if ids.count(value) > 1})
        if duplicates:
            raise RetrainingDataError(
                f"duplicate row_id values in {label} rows: "
                + ", ".join(str(int(value)) for value in duplicates)
            )

    labels_by_id = {record[ID_COLUMN]: record[LABEL_COLUMN] for record in actuals}

    missing = sorted(value for value in feature_ids if value not in labels_by_id)
    if missing:
        raise RetrainingDataError(
            f"{len(missing)} batch row(s) have no label, starting at row_id {int(missing[0])}"
        )

    joined = [
        {
            ID_COLUMN: record[ID_COLUMN],
            **{name: record[name] for name in FEATURE_COLUMNS},
            LABEL_COLUMN: labels_by_id[record[ID_COLUMN]],
        }
        for record in features
    ]

    if not joined:
        raise RetrainingDataError("joining batch features to actuals produced no rows")

    return joined


def combine_training_sets(
    original: Sequence[Mapping[str, float]],
    newly_labelled: Sequence[Mapping[str, float]],
) -> list[dict[str, float]]:
    """Original training rows plus newly labelled observations, deduplicated.

    Deterministic in both respects that matter:

      * on a row_id collision the NEWLY LABELLED observation wins. Ground truth
        that arrived later is the more recent statement about that row, and
        preferring it means a corrected label actually takes effect.
      * the result is sorted by row_id, so the same inputs always produce a
        byte-identical table. Training splits on a seed, and an unstable row
        order would make the seed meaningless — two runs of the same commit
        would fit different models and their metrics would not be comparable.
    """
    combined: dict[float, dict[str, float]] = {
        record[ID_COLUMN]: dict(record) for record in original
    }

    for record in newly_labelled:
        combined[record[ID_COLUMN]] = dict(record)

    return [combined[key] for key in sorted(combined)]


# --- Evaluation -------------------------------------------------------------


@dataclass(frozen=True)
class EvaluationResult:
    """Held-out metrics for one trained model, and the promotion verdict."""

    rmse: float
    r2: float

    @property
    def passed(self) -> bool:
        return self.rmse <= MAX_RMSE and self.r2 >= MIN_R2

    def as_metrics(self) -> dict[str, float]:
        """Metric payload logged to the MLflow run."""
        return {"rmse": self.rmse, "r2": self.r2}


def evaluate_predictions(
    y_true: NDArray[np.float64],
    y_pred: NDArray[np.float64],
) -> EvaluationResult:
    """Score predictions against held-out labels."""
    if len(y_true) == 0:
        raise ValueError("cannot evaluate an empty prediction set")

    if len(y_true) != len(y_pred):
        raise ValueError("y_true and y_pred must have the same length")

    return EvaluationResult(
        rmse=float(root_mean_squared_error(y_true, y_pred)),
        r2=float(r2_score(y_true, y_pred)),
    )


def enforce_threshold(result: EvaluationResult) -> None:
    """Fail the task unless the model clears the encoded promotion threshold."""
    if result.passed:
        return

    raise EvaluationThresholdError(
        "Model failed the promotion threshold: "
        f"rmse={result.rmse:.4f} (max {MAX_RMSE}), "
        f"r2={result.r2:.4f} (min {MIN_R2}). "
        "Not registered, and the Champion alias is unchanged."
    )
