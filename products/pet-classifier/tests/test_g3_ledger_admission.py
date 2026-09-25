"""Ledger exposure, idempotent reconciliation and the admission decision."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import admission
import estimate as est_mod
import ledger as led
import pytest
from g3lib import G3Error

from tests.g3_helpers import NOW, REGION, SUBSCRIPTION, config_dict, discovery, evidence, policy

EXPIRY = NOW + timedelta(minutes=170)


def reserved_ledger(
    amount: str = "6.10", session_id: str = "s20261001-1200-aaaaaa"
) -> dict[str, Any]:
    book = led.empty_ledger("1")
    return led.reserve(book, session_id, NOW, EXPIRY, Decimal(amount), "cfg", "ev", "pol", ["rg-a"])


def test_reserve_is_idempotent_and_refuses_changed_content() -> None:
    book = reserved_ledger()
    again = led.reserve(
        book, "s20261001-1200-aaaaaa", NOW, EXPIRY, Decimal("6.10"), "cfg", "ev", "pol", ["rg-a"]
    )
    assert len(again["sessions"]) == 1
    with pytest.raises(G3Error, match="different content"):
        led.reserve(
            book,
            "s20261001-1200-aaaaaa",
            NOW,
            EXPIRY,
            Decimal("7.00"),
            "cfg",
            "ev",
            "pol",
            ["rg-a"],
        )


def test_second_concurrent_session_is_refused() -> None:
    book = reserved_ledger()
    with pytest.raises(G3Error, match="one session at a time"):
        led.reserve(book, "s20261001-1300-bbbbbb", NOW, EXPIRY, Decimal("1"), "c", "e", "p", [])


def test_unresolved_reservation_is_retained_after_session_ends() -> None:
    book = reserved_ledger("6.10")
    led.set_status(book, "s20261001-1200-aaaaaa", "ended")
    exp = led.exposure(book)
    assert exp.unresolved_usd == Decimal("6.10") and exp.settled_usd == Decimal("0")
    assert exp.unverified_teardowns == ["s20261001-1200-aaaaaa"]


def test_empty_billing_does_not_release_the_reservation() -> None:
    book = reserved_ledger("6.10")
    led.record_teardown(book, "s20261001-1200-aaaaaa", NOW + timedelta(hours=4), [], "result-1")
    led.reconcile(
        book,
        "s20261001-1200-aaaaaa",
        NOW + timedelta(hours=6),
        Decimal("0"),
        NOW - timedelta(hours=1),
        NOW + timedelta(hours=5),
        False,
        "cost-mgmt",
    )
    exp = led.exposure(book)
    assert exp.unresolved_usd == Decimal("6.10")
    assert book["sessions"]["s20261001-1200-aaaaaa"]["status"] != "settled"


def test_reconcile_is_idempotent_and_replaces_rather_than_sums() -> None:
    book = reserved_ledger("6.10")
    sid = "s20261001-1200-aaaaaa"
    led.record_teardown(book, sid, NOW + timedelta(hours=4), [], "result-1")
    args = (NOW - timedelta(hours=1), NOW + timedelta(days=2), True, "invoice")
    led.reconcile(book, sid, NOW + timedelta(days=3), Decimal("4.20"), *args)
    led.reconcile(book, sid, NOW + timedelta(days=3), Decimal("4.20"), *args)
    assert len(book["sessions"][sid]["reconciliation"]["events"]) == 2  # teardown + one reconcile
    exp = led.exposure(book)
    assert exp.settled_usd == Decimal("4.20") and exp.unresolved_usd == Decimal("0")


def test_actual_above_reservation_uses_actual() -> None:
    book = reserved_ledger("6.10")
    sid = "s20261001-1200-aaaaaa"
    led.record_teardown(book, sid, NOW + timedelta(hours=4), [], "r")
    led.reconcile(
        book,
        sid,
        NOW + timedelta(days=3),
        Decimal("9.99"),
        NOW - timedelta(hours=1),
        NOW + timedelta(days=2),
        True,
        "invoice",
    )
    assert led.exposure(book).settled_usd == Decimal("9.99")
    # partial coverage above the reservation also counts the actual, not the reservation
    book2 = reserved_ledger("6.10")
    led.reconcile(
        book2,
        sid,
        NOW + timedelta(days=1),
        Decimal("7.50"),
        NOW - timedelta(hours=1),
        NOW + timedelta(hours=8),
        False,
        "cost-mgmt",
    )
    assert led.exposure(book2).unresolved_usd == Decimal("7.50")


def test_failed_partial_teardown_stays_unresolved() -> None:
    book = reserved_ledger("6.10")
    sid = "s20261001-1200-aaaaaa"
    led.record_teardown(book, sid, NOW + timedelta(hours=4), ["/sub/rg/aks"], "r-partial")
    led.reconcile(
        book,
        sid,
        NOW + timedelta(days=3),
        Decimal("4.20"),
        NOW - timedelta(hours=1),
        NOW + timedelta(days=2),
        True,
        "invoice",
    )
    exp = led.exposure(book)
    assert exp.unresolved_usd == Decimal("6.10") and exp.settled_usd == Decimal("0")
    assert exp.unverified_teardowns == [sid]


def test_cancelled_session_with_resources_is_refused() -> None:
    book = reserved_ledger()
    led.record_resources(book, "s20261001-1200-aaaaaa", ["/sub/rg/x"])
    with pytest.raises(G3Error):
        led.set_status(book, "s20261001-1200-aaaaaa", "cancelled")


def _decide(
    book: dict[str, Any],
    *,
    watchdog_ok: bool = True,
    cfg_gpu: bool = True,
    disc: dict[str, Any] | None = None,
    ev: dict[str, Any] | None = None,
    bound: str | None = None,
    now: datetime = NOW,
) -> tuple[est_mod.Estimate, admission.AdmissionDecision]:
    p = policy()
    ev = ev or evidence()
    est = est_mod.estimate(est_mod.config_from_dict(config_dict(cfg_gpu)), p, ev)
    wd = (
        admission.WatchdogVerification(
            True, (NOW - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"), "Report", "job:1"
        )
        if watchdog_ok
        else admission.WatchdogVerification(False)
    )
    return est, admission.decide(
        p, led.exposure(book), est, ev, disc or discovery(), wd, now, SUBSCRIPTION, REGION, bound
    )


def settled_book(total: str) -> dict[str, Any]:
    book = led.empty_ledger("1")
    sid = "s20260901-0900-cccccc"
    led.reserve(
        book,
        sid,
        NOW - timedelta(days=30),
        NOW - timedelta(days=30) + timedelta(hours=3),
        Decimal(total),
        "c",
        "e",
        "p",
        [],
    )
    led.record_teardown(book, sid, NOW - timedelta(days=29), [], "r")
    led.reconcile(
        book,
        sid,
        NOW - timedelta(days=20),
        Decimal(total),
        NOW - timedelta(days=31),
        NOW - timedelta(days=25),
        True,
        "invoice",
    )
    return book


def test_admission_passes_when_everything_is_verified() -> None:
    est, decision = _decide(led.empty_ledger("1"))
    assert decision.cost_fits and decision.cloud_execution_allowed
    assert decision.projected_total_usd == est.reservation_usd


def test_boundary_at_60_equal_fits_and_one_cent_over_blocks() -> None:
    est, _ = _decide(led.empty_ledger("1"))
    exact = Decimal("60") - est.reservation_usd
    _, fits = _decide(settled_book(str(exact)))
    assert fits.cost_fits and fits.projected_total_usd == Decimal("60")
    _, over = _decide(settled_book(str(exact + Decimal("0.01"))))
    assert not over.cost_fits and not over.cloud_execution_allowed
    assert any("contingency" in r for r in over.cost_reasons)


def test_dry_run_reports_cost_fits_but_cloud_blocked_without_live_watchdog() -> None:
    _, decision = _decide(led.empty_ledger("1"), watchdog_ok=False)
    assert decision.cost_fits
    assert not decision.cloud_execution_allowed
    assert "cloud execution blocked: watchdog not live-verified" in decision.control_reasons


def test_live_session_blocks_admission() -> None:
    _, decision = _decide(reserved_ledger())
    assert decision.cost_fits is True or decision.cost_fits is False
    assert not decision.cloud_execution_allowed
    assert any("another session is live" in r for r in decision.control_reasons)


def test_stale_evidence_and_discovery_blockers_block() -> None:
    _, decision = _decide(led.empty_ledger("1"), ev=evidence(NOW - timedelta(hours=100)))
    assert decision.cost_fits and any("old" in r for r in decision.control_reasons)
    _, decision = _decide(
        led.empty_ledger("1"),
        disc=discovery(
            blockers=["gpu family Standard NCASv3_T4 Family quota 0/0 vCPU cannot fit 4 vCPU"]
        ),
    )
    assert not decision.cloud_execution_allowed
    assert any("quota" in r for r in decision.control_reasons)


def test_unverified_permissions_block() -> None:
    _, decision = _decide(led.empty_ledger("1"), disc=discovery(roles=["Reader"]))
    assert any("permissions" in r for r in decision.control_reasons)


def test_changed_configuration_invalidates_admission() -> None:
    other = est_mod.config_from_dict(config_dict(False)).config_hash
    _, decision = _decide(led.empty_ledger("1"), bound=other)
    assert any("configuration changed" in r for r in decision.control_reasons)


def test_ledger_store_round_trip_is_atomic(tmp_path: Path) -> None:
    store = led.LedgerStore(tmp_path / "ledger.json", tmp_path / "ledger.lock")
    with store.locked():
        book = store.load("1")
        led.reserve(book, "s20261001-1200-aaaaaa", NOW, EXPIRY, Decimal("6.10"), "c", "e", "p", [])
        store.save(book)
    assert store.load("1")["sessions"]["s20261001-1200-aaaaaa"]["reserved_usd"] == "6.10"
    assert not list(tmp_path.glob(".ledger.json.*"))
