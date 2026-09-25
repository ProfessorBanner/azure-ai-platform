"""Phase 19 capability lab: a Microsoft Foundry managed agent, governed by Phase 18.

Built to compare Foundry-managed orchestration against the Phase 18
application-owned agent, not to replace it. See README.md for the responsibility
split this lab exists to measure.
"""

from foundry_agent_lab.config import FoundryLabConfig, LabConfigurationError, load_config
from foundry_agent_lab.correlation import TurnCorrelation, correlate, verify
from foundry_agent_lab.governance import GovernedOutcome, GovernedResult, GovernedToolGateway
from foundry_agent_lab.governed_runtime import GovernedFoundryLoop, GovernedTurn
from foundry_agent_lab.provisioning import ProvisionedAgent, provision_agent
from foundry_agent_lab.registry import build_registry
from foundry_agent_lab.resume import ApprovedActionRunner, ResumeResult
from foundry_agent_lab.runtime import FoundryFunctionLoop, TurnRecord
from foundry_agent_lab.workflow import InMemoryWorkflowStore, LabWorkflow, WorkflowState

__all__ = [
    "ApprovedActionRunner",
    "FoundryFunctionLoop",
    "FoundryLabConfig",
    "GovernedFoundryLoop",
    "GovernedOutcome",
    "GovernedResult",
    "GovernedToolGateway",
    "GovernedTurn",
    "InMemoryWorkflowStore",
    "LabConfigurationError",
    "LabWorkflow",
    "ProvisionedAgent",
    "ResumeResult",
    "TurnCorrelation",
    "TurnRecord",
    "WorkflowState",
    "build_registry",
    "correlate",
    "load_config",
    "provision_agent",
    "verify",
]
