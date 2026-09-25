"""Public retail price evidence from the Azure Retail Prices API.

Read-only. Every needed meter is selected by an explicit filter and must
match EXACTLY one on-demand Linux record; zero or several matches is an
error, never a zero price. The evidence file stores the raw selected records
plus a normalised view, the filters, the retrieval time and a hash that the
estimate, the reservation and the deployment binding all carry.

Retail prices are public list prices: estimates, not account-specific
guaranteed charges.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from g3lib import (
    PRICES_DIR,
    G3Error,
    atomic_write_json,
    canonical_hash,
    format_utc,
    money,
    parse_utc,
    read_json,
    utc_now,
)

RETAIL_API = "https://prices.azure.com/api/retail/prices"
API_VERSION = "2023-01-01-preview"
MAX_PAGES = 50
# Standard Load Balancer meters are published once, under armRegionName "Global".
LB_FILTER = "armRegionName eq 'Global' and serviceName eq 'Load Balancer' and skuName eq 'Standard'"

Fetcher = Callable[[str], dict[str, Any]]
Record = dict[str, Any]


class MissingPrice(G3Error):
    """No record matched the filter; the meter is unpriced, not free."""


class AmbiguousPrice(G3Error):
    """More than one record matched; the quote would be a guess."""


def default_fetcher(url: str) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310 - fixed https host
        loaded = json.load(response)
    if not isinstance(loaded, dict):
        raise G3Error("retail API returned a non-object page")
    return loaded


def fetch_all(filter_expr: str, fetcher: Fetcher = default_fetcher) -> list[Record]:
    """All items for an OData filter, following NextPageLink."""
    query = urllib.parse.urlencode(
        {"api-version": API_VERSION, "currencyCode": "USD", "$filter": filter_expr}
    )
    url: str | None = f"{RETAIL_API}?{query}"
    items: list[Record] = []
    pages = 0
    while url:
        if pages >= MAX_PAGES:
            raise G3Error(f"more than {MAX_PAGES} pages for filter {filter_expr!r}")
        page = fetcher(url)
        page_items = page.get("Items")
        if not isinstance(page_items, list):
            raise G3Error("retail API page without an Items list")
        items.extend(page_items)
        url = page.get("NextPageLink") or None
        pages += 1
    return items


def _is_linux_on_demand(record: Record) -> bool:
    sku, meter, product = record["skuName"], record["meterName"], record["productName"]
    excluded = ("Spot", "Low Priority")
    return (
        record["type"] == "Consumption"
        and record.get("reservationTerm") in (None, "")
        and not any(word in sku for word in excluded)
        and not any(word in meter for word in excluded)
        and "Windows" not in product
        and "Cloud Services" not in product
        and record["unitOfMeasure"] == "1 Hour"
    )


def select_exactly_one(
    records: Iterable[Record], predicate: Callable[[Record], bool], what: str
) -> Record:
    matches = [record for record in records if predicate(record)]
    if not matches:
        raise MissingPrice(f"no retail price record for {what}")
    if len(matches) > 1:
        raise AmbiguousPrice(f"{len(matches)} retail price records for {what}; refusing to guess")
    return matches[0]


def select_linux_vm(records: Iterable[Record], arm_sku: str) -> Record:
    return select_exactly_one(
        records,
        lambda r: r["armSkuName"] == arm_sku and _is_linux_on_demand(r),
        f"Linux on-demand {arm_sku}",
    )


def select_meter(
    records: Iterable[Record], product: str, sku: str, meter: str, unit: str
) -> Record:
    return select_exactly_one(
        records,
        lambda r: (
            r["type"] == "Consumption"
            and r["productName"] == product
            and r["skuName"] == sku
            and r["meterName"] == meter
            and r["unitOfMeasure"] == unit
        ),
        f"{product} / {sku} / {meter} ({unit})",
    )


def select_highest_tier(
    records: Iterable[Record], product: str, sku: str, meter: str, unit: str
) -> Record:
    """Tiered meters repeat the same name with different prices; take the DEAREST tier.

    Conservative by construction: a small project sits in the first (most
    expensive) tier and any free allowance is not assumed.
    """
    matches = [
        r
        for r in records
        if r["type"] == "Consumption"
        and r["productName"] == product
        and r["skuName"] == sku
        and r["meterName"] == meter
        and r["unitOfMeasure"] == unit
        and money(str(r["retailPrice"])) > 0
    ]
    if not matches:
        raise MissingPrice(f"no non-zero retail price record for {product} / {sku} / {meter}")
    return max(matches, key=lambda r: money(str(r["retailPrice"])))


def normalise(record: Record, key: str, filter_expr: str, selection: str) -> dict[str, Any]:
    return {
        "key": key,
        "currency": record["currencyCode"],
        "arm_region": record["armRegionName"],
        "arm_sku": record.get("armSkuName", ""),
        "sku": record["skuName"],
        "product": record["productName"],
        "product_id": record["productId"],
        "meter_id": record["meterId"],
        "meter": record["meterName"],
        "price_type": record["type"],
        "unit": record["unitOfMeasure"],
        "unit_price": str(money(str(record["retailPrice"]))),
        "effective_date": record["effectiveStartDate"],
        "filter": filter_expr,
        "selection": selection,
        "source": RETAIL_API,
    }


def region_filter(region: str, extra: str) -> str:
    return f"armRegionName eq '{region}' and {extra}"


def catalogue(
    region: str, cpu_sku: str, gpu_sku: str
) -> list[tuple[str, str, Callable[[list[Record]], Record], str]]:
    """(key, filter, selector, selection description) for every meter the estimate needs."""
    return [
        (
            "vm_cpu_hour",
            region_filter(region, f"armSkuName eq '{cpu_sku}'"),
            lambda rs: select_linux_vm(rs, cpu_sku),
            "Consumption, not Windows/Spot/Low Priority/reservation, 1 Hour",
        ),
        (
            "vm_gpu_hour",
            region_filter(region, f"armSkuName eq '{gpu_sku}'"),
            lambda rs: select_linux_vm(rs, gpu_sku),
            "Consumption, not Windows/Spot/Low Priority/reservation, 1 Hour",
        ),
        (
            "os_disk_p6_month",
            region_filter(
                region, "serviceName eq 'Storage' and productName eq 'Premium SSD Managed Disks'"
            ),
            lambda rs: select_meter(
                rs, "Premium SSD Managed Disks", "P6 LRS", "P6 LRS Disk", "1/Month"
            ),
            "P6 LRS Disk, 1/Month",
        ),
        (
            "lb_rules_hour",
            LB_FILTER,
            lambda rs: select_meter(
                rs,
                "Load Balancer",
                "Standard",
                "Standard Included LB Rules and Outbound Rules",
                "1 Hour",
            ),
            "Standard Included LB Rules and Outbound Rules, 1 Hour (armRegionName Global)",
        ),
        (
            "lb_data_gb",
            LB_FILTER,
            lambda rs: select_meter(
                rs, "Load Balancer", "Standard", "Standard Data Processed", "1 GB"
            ),
            "Standard Data Processed, 1 GB (published under armRegionName Global)",
        ),
        (
            "public_ip_hour",
            region_filter(
                region,
                "serviceName eq 'Virtual Network' and productName eq 'IP Addresses' "
                "and skuName eq 'Standard'",
            ),
            lambda rs: select_meter(
                rs, "IP Addresses", "Standard", "Standard IPv4 Static Public IP", "1 Hour"
            ),
            "Standard IPv4 Static Public IP, 1 Hour",
        ),
        (
            "egress_gb",
            region_filter(
                region,
                "serviceName eq 'Bandwidth' and productName eq 'Rtn Preference: MGN' "
                "and skuName eq 'Standard'",
            ),
            lambda rs: select_highest_tier(
                rs, "Rtn Preference: MGN", "Standard", "Standard Data Transfer Out", "1 GB"
            ),
            "Standard Data Transfer Out, dearest non-zero tier, 1 GB",
        ),
        (
            "acr_basic_day",
            region_filter(region, "serviceName eq 'Container Registry' and skuName eq 'Basic'"),
            lambda rs: select_meter(
                rs, "Container Registry", "Basic", "Basic Registry Unit", "1/Day"
            ),
            "Basic Registry Unit, 1/Day",
        ),
        (
            "acr_storage_gb_month",
            region_filter(region, "serviceName eq 'Container Registry' and skuName eq 'Basic'"),
            lambda rs: select_meter(rs, "Container Registry", "Basic", "Data Stored", "1 GB/Month"),
            "Data Stored, 1 GB/Month",
        ),
        (
            "automation_minute",
            region_filter(
                region,
                "serviceName eq 'Automation' and productName eq 'Process Automation' "
                "and skuName eq 'Basic'",
            ),
            lambda rs: select_highest_tier(
                rs, "Process Automation", "Basic", "Basic Runtime", "1 Minute"
            ),
            "Basic Runtime, dearest non-zero tier (no free minutes assumed), 1 Minute",
        ),
        (
            "blob_hot_gb_month",
            region_filter(
                region,
                "serviceName eq 'Storage' and productName eq 'General Block Blob v2' "
                "and skuName eq 'Hot LRS'",
            ),
            lambda rs: select_highest_tier(
                rs, "General Block Blob v2", "Hot LRS", "Hot LRS Data Stored", "1 GB/Month"
            ),
            "Hot LRS Data Stored, dearest tier, 1 GB/Month",
        ),
    ]


def collect(
    region: str,
    cpu_sku: str,
    gpu_sku: str,
    fetcher: Fetcher = default_fetcher,
    now: datetime | None = None,
) -> dict[str, Any]:
    retrieved = now or utc_now()
    entries: dict[str, Any] = {}
    raw: dict[str, Any] = {}
    for key, filter_expr, selector, selection in catalogue(region, cpu_sku, gpu_sku):
        records = fetch_all(filter_expr, fetcher)
        chosen = selector(records)
        if chosen["currencyCode"] != "USD":
            raise G3Error(f"{key}: retail record currency {chosen['currencyCode']} is not USD")
        entries[key] = normalise(chosen, key, filter_expr, selection)
        raw[key] = chosen
    evidence: dict[str, Any] = {
        "schema_version": "1",
        "source": RETAIL_API,
        "api_version": API_VERSION,
        "currency": "USD",
        "region": region,
        "retrieved_utc": format_utc(retrieved),
        "note": "Public retail list prices: estimates, not account-specific guaranteed charges.",
        "entries": entries,
        "raw": raw,
    }
    evidence["evidence_hash"] = canonical_hash({"entries": entries, "region": region})
    return evidence


def unit_price(evidence: dict[str, Any], key: str) -> Decimal:
    entry = evidence["entries"].get(key)
    if entry is None:
        raise MissingPrice(f"price evidence has no entry {key!r}; a missing price is not zero")
    if entry["currency"] != "USD":
        raise G3Error(f"{key}: evidence currency {entry['currency']} is not USD")
    return money(entry["unit_price"])


def check_evidence(
    evidence: dict[str, Any], now: datetime, max_age_hours: int, region: str
) -> list[str]:
    """Reasons the evidence is unusable; empty when it is fresh, USD and for the region."""
    reasons: list[str] = []
    if evidence.get("currency") != "USD":
        reasons.append(f"price evidence currency {evidence.get('currency')!r} is not USD")
    if evidence.get("region") != region:
        reasons.append(f"price evidence region {evidence.get('region')!r} is not {region!r}")
    retrieved = evidence.get("retrieved_utc")
    if not isinstance(retrieved, str):
        reasons.append("price evidence has no retrieval timestamp")
    else:
        age = now - parse_utc(retrieved)
        if age < timedelta(0):
            reasons.append("price evidence is from the future")
        elif age > timedelta(hours=max_age_hours):
            reasons.append(f"price evidence is {age} old; maximum {max_age_hours} h")
    if not evidence.get("entries"):
        reasons.append("price evidence has no entries")
    return reasons


def latest_evidence_path() -> Path:
    return PRICES_DIR / "latest.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch retail price evidence (read-only).")
    parser.add_argument("--region", default="uksouth")
    parser.add_argument("--cpu-sku", default="Standard_D4als_v6")
    parser.add_argument("--gpu-sku", default="Standard_NC4as_T4_v3")
    args = parser.parse_args(argv)

    evidence = collect(args.region, args.cpu_sku, args.gpu_sku)
    stamp = evidence["retrieved_utc"].replace(":", "").replace("-", "")
    path = PRICES_DIR / f"{stamp}-{args.region}.json"
    atomic_write_json(path, evidence)
    atomic_write_json(latest_evidence_path(), evidence)
    print(f"evidence {evidence['evidence_hash'][:12]} written to {path}")
    for key, entry in evidence["entries"].items():
        print(f"  {key:22s} {entry['unit_price']:>10} USD per {entry['unit']:10s} {entry['meter']}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except G3Error as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)


def load_latest_evidence() -> dict[str, Any]:
    path = latest_evidence_path()
    if not path.is_file():
        raise MissingPrice(f"no price evidence at {path}; run scripts/g3/prices.py first")
    return read_json(path)
