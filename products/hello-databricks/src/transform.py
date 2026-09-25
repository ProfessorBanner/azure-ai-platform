from pyspark.sql import SparkSession
from pyspark.sql import functions as F
from transform_logic import OUTPUT_TABLE, SOURCE_TABLE


def main() -> None:
    spark = SparkSession.builder.getOrCreate()

    source = spark.table(SOURCE_TABLE)

    result = source.withColumn(
        "processed_at",
        F.current_timestamp(),
    ).withColumn(
        "source",
        F.lit("phase10_bundle"),
    )

    result.write.mode("overwrite").saveAsTable(OUTPUT_TABLE)

    spark.table(OUTPUT_TABLE).show(truncate=False)


if __name__ == "__main__":
    main()
