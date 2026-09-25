import os

from pyspark.sql import SparkSession


def main() -> None:
    spark = SparkSession.builder.getOrCreate()

    catalog = os.environ.get("CATALOG")
    schema = os.environ.get("SCHEMA")

    if not catalog or not schema:
        raise ValueError("CATALOG and SCHEMA must be supplied by the job cluster environment")

    print(f"catalog={catalog} schema={schema}")

    spark.sql(f"USE CATALOG {catalog}")
    spark.sql(f"USE SCHEMA {schema}")

    print(f"Governed namespace {catalog}.{schema} is reachable.")


if __name__ == "__main__":
    main()
