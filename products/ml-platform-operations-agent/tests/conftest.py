"""Session-wide guards so no test can write an MLflow store into the repository.

WHY THIS IS NEEDED
------------------
MLflow 3.16 defaults its tracking URI to `sqlite:///<cwd>/mlflow.db`. Any store
operation from a test therefore creates a database inside the product
directory — which is not gitignored here, and is almost certainly how
`products/ml-lifecycle-demo/mlflow.db` came to exist in the first place.

Pointing the whole session at a temporary directory makes that impossible
rather than merely unlikely. `test_no_mlflow_tracking_store_is_left_in_the_repository`
still asserts the invariant afterwards; this fixture removes the ways to break
it accidentally.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterator

import pytest


@pytest.fixture(scope="session", autouse=True)
def isolate_mlflow_store() -> Iterator[None]:
    """Point MLflow at a temp directory for the whole session, then remove it."""
    temporary = tempfile.mkdtemp(prefix="mlpoa-session-")
    overrides = {
        "MLFLOW_TRACKING_URI": f"sqlite:///{temporary}/session.db",
        # ASYNC EXPORT OFF FOR THE SESSION.
        #
        # MLflow flushes its async trace queue at INTERPRETER EXIT — after this
        # fixture has torn down — and the flush then resolves a tracking
        # location afresh, landing in `./mlruns` inside the repository. A
        # synchronous exporter has nothing left to flush.
        "MLFLOW_ENABLE_ASYNC_TRACE_LOGGING": "false",
    }
    previous = {name: os.environ.get(name) for name in overrides}
    os.environ.update(overrides)
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        shutil.rmtree(temporary, ignore_errors=True)
