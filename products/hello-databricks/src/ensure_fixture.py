from pyspark.sql import SparkSession
from transform_logic import SOURCE_TABLE


def main() -> None:
    spark = SparkSession.builder.getOrCreate()

    spark.sql(
        f"CREATE TABLE IF NOT EXISTS {SOURCE_TABLE} (id BIGINT, created_at TIMESTAMP) USING DELTA"
    )

    if spark.table(SOURCE_TABLE).isEmpty():
        spark.sql(f"INSERT INTO {SOURCE_TABLE} VALUES (1, current_timestamp())")

    spark.table(SOURCE_TABLE).show(truncate=False)


if __name__ == "__main__":
    main()
