"""Task — score the production batch with the CHAMPION model.

Separate job from the candidate lifecycle (see resources/job.yml). This is the
only task that writes the `predictions` table, and it runs only against whatever
version currently holds Champion — in PROD, that is a version a human approved.

Champion is resolved ONCE and then pinned to its version number, for the same
reason validate_candidate pins Candidate: an alias is mutable, and scoring
through the alias would let a promotion landing mid-run silently split one
output table across two models.

THIS TASK MUTATES NO REGISTRY METADATA. It sets no alias and writes no tag. It
is a reader of registry state and a writer of data, and keeping that line clean
matters: aliases are the authoritative record of what is current, and the
promotion pipeline is the only thing allowed to move them. An inference job that
also edited the registry would make "what is Champion" answerable by something
other than the promotion decision.
"""

from __future__ import annotations

import mlflow
from mlflow.tracking import MlflowClient
from pyspark.sql import SparkSession

from ml_logic import (
    CHAMPION_ALIAS,
    parse_model_version,
    resolve_config,
)
from validate_candidate import score


def main() -> None:
    config = resolve_config()
    spark = SparkSession.builder.getOrCreate()

    mlflow.set_tracking_uri("databricks")
    mlflow.set_registry_uri("databricks-uc")

    client = MlflowClient()

    # RESOLVE ONCE, then pin the number.
    champion = client.get_model_version_by_alias(config.model_name, CHAMPION_ALIAS)
    version = parse_model_version(champion.version)

    print(f"Scoring with {config.model_name} version {version} (@{CHAMPION_ALIAS})")

    score(
        spark,
        config,
        model_uri=config.version_model_uri(version),
        version=version,
        target_table=config.predictions_table,
    )


if __name__ == "__main__":
    main()
