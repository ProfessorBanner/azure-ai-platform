"""The lab's registry, built server-side at import of a run. One read-only tool."""

from __future__ import annotations

from foundry_agent_lab.docs_search import run_search
from foundry_agent_lab.tools import LabTool, LabToolRegistry, ToolRiskLevel

SEARCH_DESCRIPTION = (
    "Search the approved platform documentation corpus and return evidence "
    "chunks with their identifiers. Read-only."
)


def build_registry() -> LabToolRegistry:
    """The Phase 19.1b tool set: exactly one read-only search."""
    return LabToolRegistry(
        [
            LabTool(
                name="search_platform_docs",
                description=SEARCH_DESCRIPTION,
                risk=ToolRiskLevel.READ_ONLY,
                run=run_search,
            )
        ]
    )


__all__ = ["SEARCH_DESCRIPTION", "build_registry"]
