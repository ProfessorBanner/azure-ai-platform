import pytest
from transform_logic import (
    OUTPUT_TABLE,
    SOURCE_TABLE,
    qualified_table_name,
)


def test_source_table_is_dev_hello_databricks() -> None:
    assert SOURCE_TABLE == "dev.hello_databricks.uc_private_test"


def test_output_table_is_dev_hello_databricks() -> None:
    assert OUTPUT_TABLE == "dev.hello_databricks.bundle_test_output"


def test_qualified_table_name_uses_governed_dev_namespace() -> None:
    assert qualified_table_name("example") == "dev.hello_databricks.example"


def test_qualified_table_name_rejects_empty_name() -> None:
    with pytest.raises(ValueError, match="table_name must not be empty"):
        qualified_table_name("")
