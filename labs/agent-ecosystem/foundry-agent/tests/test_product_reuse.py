"""The reuse boundary: same corpus, same retrieval, no duplicated dependency.

`_product.py` rests on one load-bearing assumption — that the product subgraph
this lab imports pulls in nothing outside `pydantic`. If that stops being true,
importing it here would drag `openai>=1.99,<3` into a workspace resolved for
`openai>=3`, and the failure would surface as an unresolvable lock rather than
as anything pointing back at this decision. So the assumption is tested.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from foundry_agent_lab._product import (
    PRODUCT_RELATIVE,
    ensure_product_importable,
    repository_root,
)
from foundry_agent_lab.docs_search import render_evidence, run_search, search_platform_docs
from foundry_agent_lab.tools import SearchDocsArguments

STDLIB_OK = {
    "__future__",
    "typing",
    "dataclasses",
    "enum",
    "abc",
    "re",
    "json",
    "hashlib",
    "pathlib",
    "collections",
    "functools",
    "math",
    "statistics",
    "time",
    "uuid",
    "threading",
    "datetime",
    "os",
    "logging",
    "importlib",
    "types",
    "subprocess",
    "io",
    "sys",
    "itertools",
}


def reachable_modules() -> set[str]:
    """Every product module reachable from the search tool, transitively."""
    root = repository_root() / PRODUCT_RELATIVE / "platform_engineering_assistant"
    graph: dict[str, tuple[set[str], set[str]]] = {}
    for path in root.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        name = str(path.relative_to(root))[:-3].replace("/", ".").removesuffix(".__init__")
        external: set[str] = set()
        internal: set[str] = set()
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and node.module and node.col_offset == 0:
                if "platform_engineering_assistant" in node.module:
                    internal.add(node.module.split("platform_engineering_assistant.")[-1])
                else:
                    external.add(node.module.split(".")[0])
            elif isinstance(node, ast.Import) and node.col_offset == 0:
                for alias in node.names:
                    target = (
                        internal if "platform_engineering_assistant" in alias.name else external
                    )
                    target.add(alias.name.split(".")[0])
        graph[name] = (external, internal)

    seen: set[str] = set()
    stack = ["agent.tools", "retrieval.index", "corpus.chunking", "config"]
    while stack:
        module = stack.pop()
        if module in seen or module not in graph:
            continue
        seen.add(module)
        stack.extend(graph[module][1])
    return {dependency for module in seen for dependency in graph[module][0]}


def test_the_reused_product_subgraph_pulls_in_nothing_but_pydantic() -> None:
    """THE assumption `_product.py` rests on."""
    external = {name for name in reachable_modules() if name not in STDLIB_OK}
    assert external == {"pydantic"}, f"reuse boundary now pulls in: {sorted(external)}"


@pytest.mark.parametrize("forbidden", ["openai", "azure", "fastapi", "uvicorn", "httpx"])
def test_the_reused_subgraph_never_reaches_a_conflicting_dependency(forbidden: str) -> None:
    assert forbidden not in reachable_modules()


def test_the_lab_declares_no_dependency_on_the_product() -> None:
    """Reuse is by source, deliberately. A package dependency cannot resolve."""
    pyproject = (pathlib.Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    assert "platform-engineering-assistant" not in pyproject


def test_the_lab_does_not_duplicate_the_corpus() -> None:
    """There is no second copy of the documents or the manifest in this lab."""
    lab = pathlib.Path(__file__).resolve().parents[1]
    assert not list(lab.rglob("manifest.json"))
    assert not (lab / "corpus").exists()


# --- the reuse actually works, over the real corpus -------------------------


def test_the_product_source_is_importable() -> None:
    assert (ensure_product_importable() / "platform_engineering_assistant").is_dir()


def test_search_returns_real_evidence_from_the_approved_corpus() -> None:
    chunks = search_platform_docs(SearchDocsArguments(query="terraform state separation"))
    assert chunks, "the approved corpus returned nothing for a corpus topic"
    assert all(chunk.chunk_id and chunk.doc_id for chunk in chunks)


def test_rendered_evidence_is_labelled_as_data_and_carries_identifiers() -> None:
    rendered = run_search(SearchDocsArguments(query="terraform state separation"))
    assert "data, not instructions" in rendered
    assert "[chunk_id:" in rendered


def test_an_unmatched_query_reports_absence_rather_than_inventing() -> None:
    rendered = render_evidence([])
    assert rendered.startswith("NO_EVIDENCE")


def test_search_is_deterministic_over_the_same_corpus() -> None:
    query = SearchDocsArguments(query="terraform state separation")
    first = [c.chunk_id for c in search_platform_docs(query)]
    second = [c.chunk_id for c in search_platform_docs(query)]
    assert first == second
