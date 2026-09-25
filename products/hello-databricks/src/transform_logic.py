import os

CATALOG = os.environ.get("CATALOG", "dev")
SCHEMA = os.environ.get("SCHEMA", "hello_databricks")

SOURCE_TABLE_NAME = "uc_private_test"
OUTPUT_TABLE_NAME = "bundle_test_output"


def qualified_table_name(table_name: str) -> str:
    if not table_name:
        raise ValueError("table_name must not be empty")

    return f"{CATALOG}.{SCHEMA}.{table_name}"


SOURCE_TABLE = qualified_table_name(SOURCE_TABLE_NAME)
OUTPUT_TABLE = qualified_table_name(OUTPUT_TABLE_NAME)
