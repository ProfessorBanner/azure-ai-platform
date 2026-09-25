"""Watchdog eligibility and execution against a fake ARM client."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
import watchdog as wd

from tests.g3_helpers import SUBSCRIPTION

RG = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-aiplatform-aksmlops-session"
NODE_RG = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-aiplatform-aksmlops-session-nodes"
SID = "s20261001-1200-aaaaaa"
EXPIRY = "2026-10-01T15:00:00Z"
BEFORE = datetime(2026, 10, 1, 14, 59, tzinfo=UTC)
AFTER = datetime(2026, 10, 1, 15, 0, tzinfo=UTC)

ALLOW = wd.Allowlist(SUBSCRIPTION, "pet-classifier-aks-mlops", RG, NODE_RG, SID, EXPIRY)
GOOD_TAGS = {
    "project": "pet-classifier-aks-mlops",
    "lifecycle": "disposable",
    "session_id": SID,
    "expiry_utc": EXPIRY,
}


class FakeArm:
    def __init__(
        self,
        groups: dict[str, wd.ResourceGroup],
        resources: dict[str, list[wd.Resource]],
        subscription: str = SUBSCRIPTION,
        delete_takes_polls: int = 1,
        fail_ids: set[str] | None = None,
    ) -> None:
        self.groups = dict(groups)
        self.resources = {k: list(v) for k, v in resources.items()}
        self.subscription = subscription
        self.delete_takes_polls = delete_takes_polls
        self.fail_ids = fail_ids or set()
        self.deleted: list[str] = []
        self._pending: dict[str, int] = {}

    def current_subscription(self) -> str:
        return self.subscription

    def get_resource_group(self, rg_id: str) -> wd.ResourceGroup | None:
        return self.groups.get(rg_id)

    def list_resources(self, rg_id: str) -> list[wd.Resource]:
        return [r for r in self.resources.get(rg_id, []) if r.id not in self.deleted]

    def get_resource(self, resource_id: str) -> wd.Resource | None:
        if resource_id in self._pending:
            self._pending[resource_id] -= 1
            if self._pending[resource_id] <= 0:
                del self._pending[resource_id]
                self.deleted.append(resource_id)
                if resource_id.endswith("/aks"):
                    self.groups.pop(NODE_RG, None)
        if resource_id in self.deleted:
            return None
        for rs in self.resources.values():
            for r in rs:
                if r.id == resource_id:
                    return r
        return None

    def delete_resource(self, resource_id: str) -> None:
        if resource_id in self.fail_ids:
            raise RuntimeError("ARM: deletion stuck")
        self._pending[resource_id] = self.delete_takes_polls


def clock(start: datetime) -> Callable[[], datetime]:
    state = {"now": start}

    def now() -> datetime:
        state["now"] += timedelta(seconds=1)
        return state["now"]

    return now


def cluster() -> wd.Resource:
    return wd.Resource(
        f"{RG}/providers/Microsoft.ContainerService/managedClusters/aks",
        "Microsoft.ContainerService/managedClusters",
        GOOD_TAGS,
    )


def registry() -> wd.Resource:
    return wd.Resource(
        f"{RG}/providers/Microsoft.ContainerRegistry/registries/acr",
        "Microsoft.ContainerRegistry/registries",
        GOOD_TAGS,
    )


def test_eligible_only_when_every_rule_holds() -> None:
    group = wd.ResourceGroup(RG, GOOD_TAGS)
    assert wd.evaluate_eligibility(ALLOW, SUBSCRIPTION, group, AFTER) == []
    assert wd.evaluate_eligibility(ALLOW, SUBSCRIPTION, group, AFTER, schedule_session_id=SID) == []


@pytest.mark.parametrize(
    "mutate, expected",
    [
        (
            lambda a, g, s: (
                a._replace(subscription_id="99999999-9999-9999-9999-999999999999")
                if False
                else wd.Allowlist(
                    "99999999-9999-9999-9999-999999999999",
                    a.project,
                    a.session_rg_id,
                    a.node_rg_id,
                    a.armed_session_id,
                    a.armed_expiry_utc,
                ),
                g,
                s,
            ),
            "subscription mismatch",
        ),
        (lambda a, g, s: (a, wd.ResourceGroup(RG + "-other", GOOD_TAGS), s), "not the allowlisted"),
        (
            lambda a, g, s: (a, wd.ResourceGroup(RG, {**GOOD_TAGS, "project": "other"}), s),
            "project tag",
        ),
        (
            lambda a, g, s: (
                a,
                wd.ResourceGroup(RG, {**GOOD_TAGS, "session_id": "s20261001-1200-zzzzzz"}),
                s,
            ),
            "session_id tag",
        ),
        (
            lambda a, g, s: (a, wd.ResourceGroup(RG, {**GOOD_TAGS, "lifecycle": "control"}), s),
            "lifecycle=disposable",
        ),
        (lambda a, g, s: (a, g, "s20261001-1200-zzzzzz"), "schedule session id"),
        (
            lambda a, g, s: (
                wd.Allowlist(
                    a.subscription_id,
                    a.project,
                    a.session_rg_id,
                    a.node_rg_id,
                    "none",
                    a.armed_expiry_utc,
                ),
                g,
                s,
            ),
            "no session armed",
        ),
    ],
)
def test_wrong_subscription_group_project_session_never_eligible(
    mutate: Callable[..., tuple[wd.Allowlist, wd.ResourceGroup, str]], expected: str
) -> None:
    allow, group, sched = mutate(ALLOW, wd.ResourceGroup(RG, GOOD_TAGS), SID)
    reasons = wd.evaluate_eligibility(allow, SUBSCRIPTION, group, AFTER, sched)
    assert any(expected in r for r in reasons), reasons


def test_not_expired_versus_expired_with_injected_clock() -> None:
    group = wd.ResourceGroup(RG, GOOD_TAGS)
    assert any(
        "not expired" in r for r in wd.evaluate_eligibility(ALLOW, SUBSCRIPTION, group, BEFORE)
    )
    assert wd.evaluate_eligibility(ALLOW, SUBSCRIPTION, group, AFTER) == []


def test_report_mode_deletes_nothing() -> None:
    arm = FakeArm(
        {RG: wd.ResourceGroup(RG, GOOD_TAGS), NODE_RG: wd.ResourceGroup(NODE_RG, {})},
        {RG: [cluster(), registry()]},
    )
    result = wd.run(arm, ALLOW, "Report", clock(AFTER))
    assert result["outcome"] == "report-only" and result["eligible"] and arm.deleted == []
    assert len(result["inventory"]) == 2 and result["node_rg_state"] == "present"


def test_execute_deletes_cluster_first_polls_and_verifies_node_group_gone() -> None:
    arm = FakeArm(
        {RG: wd.ResourceGroup(RG, GOOD_TAGS), NODE_RG: wd.ResourceGroup(NODE_RG, {})},
        {RG: [registry(), cluster()]},
        delete_takes_polls=3,
    )
    result = wd.run(arm, ALLOW, "Execute", clock(AFTER))
    assert result["outcome"] == "clean" and result["unresolved"] == []
    assert arm.deleted[0].endswith("/aks") and len(arm.deleted) == 2
    assert result["node_rg_state"] == "absent"


def test_already_deleted_is_safe_and_idempotent() -> None:
    arm = FakeArm({RG: wd.ResourceGroup(RG, GOOD_TAGS)}, {RG: []})
    result = wd.run(arm, ALLOW, "Execute", clock(AFTER))
    assert result["outcome"] == "already-clean" and arm.deleted == []
    gone = FakeArm({}, {})
    assert wd.run(gone, ALLOW, "Execute", clock(AFTER))["outcome"] == "nothing-to-do"


def test_failed_deletion_remains_unresolved_and_is_reported() -> None:
    arm = FakeArm(
        {RG: wd.ResourceGroup(RG, GOOD_TAGS), NODE_RG: wd.ResourceGroup(NODE_RG, {})},
        {RG: [cluster(), registry()]},
        fail_ids={cluster().id},
    )
    result = wd.run(arm, ALLOW, "Execute", clock(AFTER), max_attempts=2)
    assert result["outcome"] == "unresolved"
    ids = {u["id"] for u in result["unresolved"]}
    assert cluster().id in ids and NODE_RG in ids
    assert registry().id in result["deleted"]


def test_foreign_tagged_resource_is_left_in_place_even_in_execute() -> None:
    foreign = wd.Resource(
        f"{RG}/providers/Microsoft.Storage/storageAccounts/keep",
        "Microsoft.Storage/storageAccounts",
        {"lifecycle": "retained"},
    )
    arm = FakeArm({RG: wd.ResourceGroup(RG, GOOD_TAGS)}, {RG: [foreign]})
    result = wd.run(arm, ALLOW, "Execute", clock(AFTER))
    assert arm.deleted == [] and result["outcome"] == "unresolved"
    assert result["unresolved"][0]["id"] == foreign.id


def test_not_expired_execute_does_nothing() -> None:
    arm = FakeArm({RG: wd.ResourceGroup(RG, GOOD_TAGS)}, {RG: [cluster()]})
    result = wd.run(arm, ALLOW, "Execute", clock(BEFORE))
    assert result["outcome"] == "not-eligible" and arm.deleted == []


def test_local_runner_refuses_execute_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(wd.EXECUTE_ENV, raising=False)
    from g3lib import G3Error

    with pytest.raises(G3Error, match="refused"):
        wd.main(
            [
                "--subscription",
                SUBSCRIPTION,
                "--armed-session-id",
                SID,
                "--armed-expiry-utc",
                EXPIRY,
                "--execute",
            ]
        )
