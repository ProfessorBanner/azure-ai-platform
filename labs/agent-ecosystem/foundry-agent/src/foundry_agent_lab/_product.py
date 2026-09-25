"""The one place this lab reaches into the Phase 18 product.

WHY A PATH BOOTSTRAP RATHER THAN A DEPENDENCY
---------------------------------------------
The comparison this lab exists to make is only honest if both stacks answer from
the SAME corpus, through the SAME retrieval controls. Re-implementing search
here would compare this lab's retrieval against the product's, not Foundry's
orchestration against the application's.

A package dependency is not available. `azure-ai-projects==2.5.0` requires
`openai>=3` (and `httpx2`); `platform-engineering-assistant` pins
`openai>=1.99,<3`. The intersection is empty, so one resolution cannot hold
both, and forcing the product onto openai 3 to satisfy a lab would be exactly
backwards.

What makes the bootstrap safe rather than merely convenient: the product
subgraph reached from `agent.tools` — corpus loading, chunking, the BM25 index,
the retrieval config and the tool itself — imports NOTHING outside `pydantic` at
module level. No `openai`, no `azure`, no `fastapi`. So importing it here pulls
in no conflicting dependency; it is source reuse, not dependency reuse. A test
asserts that property, because it is the assumption this module rests on.

This is a lab-grade arrangement and is stated as such. The production fix, if
this comparison ever graduates, is to extract corpus+retrieval into a package
both sides depend on — see the README.
"""

from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path

PRODUCT_RELATIVE = Path("products/platform-engineering-assistant/src")


def repository_root(start: Path | None = None) -> Path:
    """Locate the repository root by walking up to the directory holding `.git`.

    Derived rather than configured, exactly as the product's corpus loader does,
    so the reuse boundary cannot be repointed by an environment variable.
    """
    current = (start or Path(__file__)).resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    raise RuntimeError("Repository root could not be located: no .git directory above this module.")


@lru_cache(maxsize=1)
def ensure_product_importable() -> Path:
    """Put the product's `src` on the import path. Idempotent.

    Raises:
        RuntimeError: when the product source is absent. Failing loudly matters:
            silently falling back to a lab-local search would make every
            comparison in this lab measure the wrong thing.
    """
    source = repository_root() / PRODUCT_RELATIVE
    if not (source / "platform_engineering_assistant").is_dir():
        raise RuntimeError(f"Product source not found at {source}.")
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))
    return source


__all__ = ["PRODUCT_RELATIVE", "ensure_product_importable", "repository_root"]
