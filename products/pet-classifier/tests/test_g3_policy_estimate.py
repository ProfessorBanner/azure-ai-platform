"""Policy loading, Decimal estimate arithmetic and evidence rejection."""

from __future__ import annotations

from decimal import Decimal

import estimate as est_mod
import pytest
from g3lib import G3Error
from policy import policy_from_dict
from prices import MissingPrice

from tests.g3_helpers import NOW, config_dict, evidence, policy


def test_policy_v1_numbers() -> None:
    p = policy()
    assert (p.project_limit_usd, p.admission_limit_usd, p.contingency_usd) == (
        Decimal("100"),
        Decimal("60"),
        Decimal("40"),
    )
    assert p.max_concurrent_sessions == 1 and p.max_gpu_nodes == 1
    assert p.active_target_minutes <= 120 and p.training_job_deadline_seconds <= 1800
    assert p.expiry_minutes_after_reservation == 120 + 20 + 10 + 20
    assert p.total_session_minutes == 230
    assert p.billing_currency == "GBP" and p.pricing_currency == "USD"


def test_policy_rejects_inconsistent_limits() -> None:
    from g3lib import read_json

    from tests.g3_helpers import POLICY_PATH

    raw = read_json(POLICY_PATH)
    raw["limits"]["contingency_usd"] = "50"
    with pytest.raises(G3Error):
        policy_from_dict(raw)


def test_estimate_uses_decimal_and_rounds_up() -> None:
    p = policy()
    config = est_mod.config_from_dict(config_dict(gpu=True))
    result = est_mod.estimate(config, p, evidence())
    assert result.billable_hours == 4  # ceil(230 / 60)
    by_key = {line.key: line for line in result.lines}
    assert by_key["cpu_compute"].amount == Decimal("0.187") * 4
    assert by_key["gpu_compute"].amount == Decimal("0.615") * 4
    disk_hour = (Decimal("12.3499") / Decimal("730")).quantize(
        Decimal("0.000001"), rounding="ROUND_UP"
    )
    assert disk_hour == Decimal("0.016918")
    assert by_key["os_disks"].amount == disk_hour * 8
    assert all(isinstance(line.amount, Decimal) for line in result.lines)
    subtotal = sum((line.amount for line in result.lines), Decimal("0"))
    assert result.subtotal == subtotal
    assert result.fx_allowance == subtotal * Decimal("0.05")
    assert result.tax_allowance == subtotal * Decimal("0.20")
    expected = (subtotal * Decimal("1.25")).quantize(Decimal("0.01"), rounding="ROUND_UP")
    assert result.reservation_usd == expected
    # Sanity: a four-hour T4 session is a few dollars, not tens.
    assert Decimal("4") < result.reservation_usd < Decimal("10")


def test_estimate_without_gpu_is_cheaper_and_has_no_gpu_line() -> None:
    p = policy()
    with_gpu = est_mod.estimate(est_mod.config_from_dict(config_dict(True)), p, evidence())
    without = est_mod.estimate(est_mod.config_from_dict(config_dict(False)), p, evidence())
    assert "gpu_compute" not in {line.key for line in without.lines}
    assert without.reservation_usd < with_gpu.reservation_usd


def test_missing_price_is_an_error_not_zero() -> None:
    p = policy()
    config = est_mod.config_from_dict(config_dict())
    with pytest.raises(MissingPrice):
        est_mod.estimate(config, p, evidence(drop="vm_gpu_hour"))


def test_wrong_currency_is_rejected_without_conversion() -> None:
    p = policy()
    config = est_mod.config_from_dict(config_dict())
    with pytest.raises(G3Error, match="no silent conversion"):
        est_mod.estimate(config, p, evidence(currency="GBP"))


def test_wrong_region_is_rejected() -> None:
    p = policy()
    config = est_mod.config_from_dict(config_dict())
    with pytest.raises(G3Error, match="different region"):
        est_mod.estimate(config, p, evidence(region="ukwest"))


def test_config_allowlists() -> None:
    bad = config_dict()
    bad["gpu_node_count"] = 2
    with pytest.raises(G3Error):
        est_mod.config_from_dict(bad)
    bad = config_dict()
    bad["system_node_vm_size"] = "Standard_B2s"
    with pytest.raises(G3Error):
        est_mod.config_from_dict(bad)


def test_config_hash_changes_with_config() -> None:
    a = est_mod.config_from_dict(config_dict(True)).config_hash
    b = est_mod.config_from_dict(config_dict(False)).config_hash
    assert a != b and len(a) == 64


def test_evidence_hash_is_stable() -> None:
    assert evidence(NOW)["evidence_hash"] == evidence(NOW)["evidence_hash"]
