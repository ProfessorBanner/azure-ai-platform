"""Retail price selection: pagination, exclusions, ambiguity, freshness."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import prices
import pytest

from tests.g3_helpers import NOW


def record(**overrides: Any) -> dict[str, Any]:
    base = {
        "currencyCode": "USD",
        "armRegionName": "uksouth",
        "armSkuName": "Standard_NC4as_T4_v3",
        "skuName": "NC4as T4 v3",
        "productName": "Virtual Machines NCasT4 v3 Series",
        "productId": "DZH318Z0BQ4L",
        "meterId": "m-1",
        "meterName": "NC4as T4 v3",
        "type": "Consumption",
        "unitOfMeasure": "1 Hour",
        "retailPrice": 0.615,
        "effectiveStartDate": "2021-11-01T00:00:00Z",
    }
    base.update(overrides)
    return base


T4_RECORDS = [
    record(),
    record(productName="Virtual Machines NCasT4 v3 Series Windows", retailPrice=0.799),
    record(skuName="NC4as T4 v3 Spot", meterName="NC4as T4 v3 Spot", retailPrice=0.18),
    record(
        skuName="NC4as T4 v3 Low Priority", meterName="NC4as T4 v3 Low Priority", retailPrice=0.123
    ),
    record(type="Reservation", reservationTerm="1 Year", retailPrice=3434.0),
    record(type="DevTestConsumption", retailPrice=0.5),
]


def test_linux_on_demand_selection_excludes_windows_spot_low_priority_reservations() -> None:
    chosen = prices.select_linux_vm(T4_RECORDS, "Standard_NC4as_T4_v3")
    assert chosen["retailPrice"] == 0.615 and chosen["type"] == "Consumption"


def test_missing_price_raises() -> None:
    with pytest.raises(prices.MissingPrice):
        prices.select_linux_vm(T4_RECORDS, "Standard_D4als_v6")


def test_ambiguous_price_raises() -> None:
    with pytest.raises(prices.AmbiguousPrice):
        prices.select_linux_vm([*T4_RECORDS, record(meterId="m-2")], "Standard_NC4as_T4_v3")


def test_pagination_follows_next_page_link() -> None:
    pages: dict[str, dict[str, Any]] = {
        "first": {"Items": [record(meterId="a")], "NextPageLink": "second"},
        "second": {"Items": [record(meterId="b")], "NextPageLink": None},
    }
    calls: list[str] = []

    def fetcher(url: str) -> dict[str, Any]:
        key = "first" if "prices.azure.com" in url else url
        calls.append(key)
        return pages[key]

    items = prices.fetch_all("armSkuName eq 'x'", fetcher)
    assert [i["meterId"] for i in items] == ["a", "b"] and calls == ["first", "second"]


def test_highest_tier_is_conservative_and_ignores_free_tier() -> None:
    tiers = [
        record(
            productName="Rtn Preference: MGN",
            skuName="Standard",
            meterName="Standard Data Transfer Out",
            unitOfMeasure="1 GB",
            retailPrice=p,
        )
        for p in (0.0, 0.05, 0.087, 0.083)
    ]
    chosen = prices.select_highest_tier(
        tiers, "Rtn Preference: MGN", "Standard", "Standard Data Transfer Out", "1 GB"
    )
    assert chosen["retailPrice"] == 0.087


def test_collect_builds_evidence_with_hash_and_raw(monkeypatch: pytest.MonkeyPatch) -> None:
    def fetcher(url: str) -> dict[str, Any]:
        # One matching record per catalogue entry, chosen by the filter text.
        if "Standard_D4als_v6" in url:
            return {
                "Items": [
                    record(
                        armSkuName="Standard_D4als_v6",
                        skuName="D4als v6",
                        productName="Virtual Machines Dalsv6 Series",
                        retailPrice=0.187,
                    )
                ]
            }
        if "Standard_NC4as_T4_v3" in url:
            return {"Items": T4_RECORDS}
        if "Premium+SSD" in url:
            return {
                "Items": [
                    record(
                        productName="Premium SSD Managed Disks",
                        skuName="P6 LRS",
                        meterName="P6 LRS Disk",
                        unitOfMeasure="1/Month",
                        retailPrice=12.3499,
                    )
                ]
            }
        if "Load+Balancer" in url:
            return {
                "Items": [
                    record(
                        armRegionName="Global",
                        productName="Load Balancer",
                        skuName="Standard",
                        meterName="Standard Included LB Rules and Outbound Rules",
                        unitOfMeasure="1 Hour",
                        retailPrice=0.025,
                    ),
                    record(
                        armRegionName="Global",
                        productName="Load Balancer",
                        skuName="Standard",
                        meterName="Standard Data Processed",
                        unitOfMeasure="1 GB",
                        retailPrice=0.005,
                    ),
                ]
            }
        if "IP+Addresses" in url:
            return {
                "Items": [
                    record(
                        productName="IP Addresses",
                        skuName="Standard",
                        meterName="Standard IPv4 Static Public IP",
                        retailPrice=0.005,
                    )
                ]
            }
        if "Bandwidth" in url:
            return {
                "Items": [
                    record(
                        productName="Rtn Preference: MGN",
                        skuName="Standard",
                        meterName="Standard Data Transfer Out",
                        unitOfMeasure="1 GB",
                        retailPrice=0.087,
                    )
                ]
            }
        if "Container+Registry" in url:
            return {
                "Items": [
                    record(
                        productName="Container Registry",
                        skuName="Basic",
                        meterName="Basic Registry Unit",
                        unitOfMeasure="1/Day",
                        retailPrice=0.1666,
                    ),
                    record(
                        productName="Container Registry",
                        skuName="Basic",
                        meterName="Data Stored",
                        unitOfMeasure="1 GB/Month",
                        retailPrice=0.1,
                    ),
                ]
            }
        if "Automation" in url:
            return {
                "Items": [
                    record(
                        productName="Process Automation",
                        skuName="Basic",
                        meterName="Basic Runtime",
                        unitOfMeasure="1 Minute",
                        retailPrice=0.002,
                    )
                ]
            }
        if "General+Block+Blob" in url:
            return {
                "Items": [
                    record(
                        productName="General Block Blob v2",
                        skuName="Hot LRS",
                        meterName="Hot LRS Data Stored",
                        unitOfMeasure="1 GB/Month",
                        retailPrice=0.0192,
                    )
                ]
            }
        raise AssertionError(url)

    evidence = prices.collect("uksouth", "Standard_D4als_v6", "Standard_NC4as_T4_v3", fetcher, NOW)
    assert evidence["currency"] == "USD" and set(evidence["entries"]) == set(
        prices.catalogue("uksouth", "a", "b")[i][0] for i in range(11)
    )
    assert evidence["entries"]["vm_gpu_hour"]["unit_price"] == "0.615"
    assert evidence["raw"]["vm_gpu_hour"]["meterId"] == "m-1"
    assert len(evidence["evidence_hash"]) == 64
    assert prices.check_evidence(evidence, NOW + timedelta(hours=1), 72, "uksouth") == []


def test_check_evidence_flags_stale_wrong_currency_and_region() -> None:
    evidence = {
        "currency": "GBP",
        "region": "ukwest",
        "retrieved_utc": "2026-09-01T00:00:00Z",
        "entries": {},
    }
    reasons = prices.check_evidence(evidence, datetime(2026, 10, 1, tzinfo=UTC), 72, "uksouth")
    assert any("not USD" in r for r in reasons)
    assert any("not 'uksouth'" in r for r in reasons)
    assert any("old" in r for r in reasons)
    assert any("no entries" in r for r in reasons)


def test_unit_price_missing_key_is_not_zero() -> None:
    with pytest.raises(prices.MissingPrice):
        prices.unit_price({"entries": {}}, "vm_gpu_hour")
