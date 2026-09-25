#!/usr/bin/env bash
#
# Input guard for the Foundry capability lab's data-plane IP allow-list.
#
# The Foundry account is created with network_acls.default_action = "Deny", so
# the value of `allowed_ip_cidrs` IS the account's perimeter. This guard runs
# BEFORE any terraform init/plan/apply so that a malformed, empty or placeholder
# allow-list fails the run rather than reaching Azure.
#
# It reads the queue-time value from FOUNDRY_ALLOWED_IP_CIDRS and requires:
#   - valid JSON;
#   - a JSON array;
#   - at least one element;
#   - every element a syntactically valid IPv4 CIDR (a.b.c.d/0-32, octets 0-255);
#   - no element inside an RFC 5737 documentation network. Those ranges exist
#     only for documentation and examples; the repository's own defaults and
#     examples use them precisely so that an unedited value can never be applied.
#
# PRIVACY: this script never prints the supplied addresses. A public egress
# address is not a secret, but pipeline logs are broadly readable and there is no
# reason to publish the operator's home or office address. Failures are reported
# by ELEMENT INDEX and reason only.
#
# On success it prints nothing but a count and re-emits the validated JSON to
# stdout ONLY when --emit is passed (used by local tooling, never by CI logs).
#
# Local use:
#   FOUNDRY_ALLOWED_IP_CIDRS='["203.0.113.10/32"]' scripts/ci/check-foundry-ip-allowlist.sh

set -euo pipefail

readonly VALUE="${FOUNDRY_ALLOWED_IP_CIDRS-}"

if [[ -z "${VALUE}" ]]; then
  echo "ERROR: FOUNDRY_ALLOWED_IP_CIDRS is empty or unset." >&2
  echo "       Supply a JSON array of IPv4 CIDRs at queue time, e.g. [\"203.0.113.10/32\"]." >&2
  exit 1
fi

# All parsing and validation happens in Python: bash cannot parse JSON safely,
# and a hand-rolled regex over the raw string would accept malformed input.
FOUNDRY_ALLOWED_IP_CIDRS="${VALUE}" python3 - "$@" <<'PY'
import ipaddress
import json
import os
import sys

raw = os.environ["FOUNDRY_ALLOWED_IP_CIDRS"]

# RFC 5737 networks reserved for documentation. Never routable, never valid here.
DOCUMENTATION_NETWORKS = [
    ipaddress.ip_network("192.0.2.0/24"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
]


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(1)


try:
    parsed = json.loads(raw)
except json.JSONDecodeError as exc:
    fail(f"FOUNDRY_ALLOWED_IP_CIDRS is not valid JSON ({exc.msg} at position {exc.pos}).")

if not isinstance(parsed, list):
    fail(f"FOUNDRY_ALLOWED_IP_CIDRS must be a JSON array, got {type(parsed).__name__}.")

if len(parsed) == 0:
    fail(
        "FOUNDRY_ALLOWED_IP_CIDRS is an empty array. The Foundry account is "
        "default-deny; an empty allow-list would make it unreachable."
    )

problems: list[str] = []

for index, element in enumerate(parsed):
    if not isinstance(element, str):
        problems.append(f"element {index}: expected a string, got {type(element).__name__}")
        continue

    if "/" not in element:
        problems.append(f"element {index}: missing a /prefix; supply CIDR notation")
        continue

    try:
        # strict=True rejects a prefix whose host bits are set (e.g. 10.0.0.1/24),
        # which is almost always an operator mistake rather than an intent.
        network = ipaddress.ip_network(element, strict=True)
    except ValueError as exc:
        problems.append(f"element {index}: not a valid IPv4 CIDR ({exc})")
        continue

    if network.version != 4:
        problems.append(f"element {index}: IPv6 is not supported by Cognitive Services IP rules")
        continue

    for documentation_network in DOCUMENTATION_NETWORKS:
        if network.subnet_of(documentation_network):
            problems.append(
                f"element {index}: inside RFC 5737 documentation network "
                f"{documentation_network} — placeholder values must not be applied"
            )
            break

if problems:
    print(
        "ERROR: the Foundry IP allow-list is invalid. Reported by index only; "
        "values are deliberately not printed.",
        file=sys.stderr,
    )
    for problem in problems:
        print(f"  - {problem}", file=sys.stderr)
    sys.exit(1)

print(f"OK: Foundry IP allow-list accepted ({len(parsed)} entr{'y' if len(parsed) == 1 else 'ies'}).")

# Re-emit only on explicit request, for local tooling. CI never passes --emit,
# so the validated value never reaches a pipeline log.
if "--emit" in sys.argv[1:]:
    print(json.dumps(parsed))
PY
