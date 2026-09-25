"""Shared helpers for the G3 cost controls.

Standard library only. Currency is ``Decimal``; every timestamp is
timezone-aware UTC; every record is plain JSON written atomically. Runtime
state (evidence, ledger, session files) lives under ``.local/g3``, which is
git-ignored and survives cluster teardown because it is on the operator's
machine, not in the cluster.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import ROUND_UP, Decimal
from pathlib import Path
from typing import Any

PRODUCT_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIR = PRODUCT_ROOT / ".local" / "g3"
DISCOVERY_DIR = RUNTIME_DIR / "discovery"
PRICES_DIR = RUNTIME_DIR / "prices"
LEDGER_FILE = RUNTIME_DIR / "ledger" / "ledger.json"
LEDGER_LOCK = RUNTIME_DIR / "ledger" / "ledger.lock"
SESSIONS_DIR = RUNTIME_DIR / "sessions"
CONTROLS_STATE_FILE = RUNTIME_DIR / "controls-state.json"
POLICY_FILE = PRODUCT_ROOT / "deploy" / "g3" / "budget-policy.v1.json"

SUBSCRIPTION_ENV = "G3_SUBSCRIPTION_ID"
GUID_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
SESSION_ID_PATTERN = re.compile(r"^s[0-9]{8}-[0-9]{4}-[a-z0-9]{6}$")
RFC3339_UTC = "%Y-%m-%dT%H:%M:%SZ"

CENT = Decimal("0.01")
ZERO = Decimal("0")


class G3Error(Exception):
    """Base class for every deliberate refusal in the G3 tooling."""


def utc_now() -> datetime:
    return datetime.now(tz=UTC).replace(microsecond=0)


def format_utc(moment: datetime) -> str:
    if moment.tzinfo is None or moment.utcoffset() != UTC.utcoffset(None):
        raise G3Error(f"timestamp {moment!r} is not timezone-aware UTC")
    return moment.strftime(RFC3339_UTC)


def parse_utc(text: str) -> datetime:
    try:
        return datetime.strptime(text, RFC3339_UTC).replace(tzinfo=UTC)
    except ValueError as exc:
        raise G3Error(f"{text!r} is not an RFC 3339 UTC timestamp (YYYY-MM-DDTHH:MM:SSZ)") from exc


def money(value: Decimal | str | int) -> Decimal:
    """A currency amount as an exact Decimal. Floats are refused."""
    if isinstance(value, float):
        raise G3Error("currency values must be Decimal, str or int, never float")
    return Decimal(value)


def round_up_cents(value: Decimal) -> Decimal:
    """Conservative rounding: reservations round UP to the cent."""
    return value.quantize(CENT, rounding=ROUND_UP)


def validate_guid(value: str, what: str) -> str:
    if not GUID_PATTERN.fullmatch(value):
        raise G3Error(f"{what} {value!r} is not a lowercase GUID")
    return value


def validate_session_id(value: str) -> str:
    if not SESSION_ID_PATTERN.fullmatch(value):
        raise G3Error(f"session id {value!r} must look like sYYYYMMDD-HHMM-xxxxxx")
    return value


def new_session_id(now: datetime, token: str | None = None) -> str:
    """An immutable session id: reservation minute plus a random suffix."""
    suffix = token if token is not None else secrets.token_hex(3)
    return validate_session_id(f"s{now.strftime('%Y%m%d-%H%M')}-{suffix}")


def require_subscription() -> str:
    """The target subscription, always explicit; never the CLI default."""
    value = os.environ.get(SUBSCRIPTION_ENV, "").strip().lower()
    if not value:
        raise G3Error(
            f"set {SUBSCRIPTION_ENV} to the intended subscription id; "
            "the Azure CLI default subscription is never used"
        )
    return validate_guid(value, SUBSCRIPTION_ENV)


def _json_default(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return format_utc(value)
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"{type(value).__name__} is not JSON serialisable")


def json_dumps(obj: object) -> str:
    return json.dumps(obj, indent=2, sort_keys=True, default=_json_default) + "\n"


def canonical_hash(obj: object) -> str:
    """SHA-256 of the canonical JSON form (sorted keys, no whitespace)."""
    canonical = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=_json_default)
    return hashlib.sha256(canonical.encode()).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        loaded = json.load(handle)
    if not isinstance(loaded, dict):
        raise G3Error(f"{path} does not hold a JSON object")
    return loaded


def atomic_write_json(path: Path, obj: object) -> None:
    """Write via a temporary file in the same directory, then rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json_dumps(obj))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


@contextmanager
def file_lock(path: Path) -> Iterator[None]:
    """An exclusive advisory lock for the single-operator workflow."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise G3Error(f"{path} is locked by another process") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
