"""Developer API keys: creation, revocation and the lookup the routes run on every request.

A key is ``di_`` followed by 40 random URL-safe characters. Only its HMAC-SHA256 with
``settings.api_key_secret`` is stored (``api_keys.key_hash``), plus the first characters
(``prefix``) so the maintainer can tell keys apart; the plain key is shown once, at creation.

Serverless instances must not query Postgres on every request, so a looked-up key is cached in
Redis for ``CACHE_TTL`` seconds and in the instance for ``LOCAL_TTL``. Revoking a key overwrites
its Redis entry, so other instances stop accepting it within ``LOCAL_TTL`` seconds.
"""

import hashlib
import hmac
import json
import logging
import secrets
import time
from dataclasses import asdict, dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from dota_insights.collector.db import utc_now
from dota_insights.config import settings
from dota_insights.db.access import read
from dota_insights.db.models import ApiKey
from dota_insights.kv import KVError, get_kv

log = logging.getLogger(__name__)

KEY_PREFIX = "di_"
PREFIX_LENGTH = 10
CACHE_KEY = "di:api:key:{hash}"
CACHE_TTL = 60
LOCAL_TTL = 30.0
LOCAL_MAX = 10_000


class ApiNotConfiguredError(RuntimeError):
    """``DOTA_INSIGHTS_API_KEY_SECRET`` is not set."""


class KeyStoreUnavailableError(RuntimeError):
    """The database holding the keys is not configured or did not answer."""


@dataclass(frozen=True)
class KeyRecord:
    """What a request needs to know about its key. ``id`` is empty for an unknown key."""

    id: str
    revoked: bool
    per_minute: int | None = None
    per_day: int | None = None

    @property
    def valid(self) -> bool:
        return bool(self.id) and not self.revoked


UNKNOWN = KeyRecord(id="", revoked=True)

_local: dict[str, tuple[float, KeyRecord]] = {}


def _secret() -> bytes:
    if not settings.api_key_secret:
        raise ApiNotConfiguredError("DOTA_INSIGHTS_API_KEY_SECRET is not set")
    return settings.api_key_secret.encode()


def hash_key(key: str) -> str:
    """HMAC-SHA256 of ``key`` with the server secret, as stored in ``api_keys.key_hash``."""
    return hmac.new(_secret(), key.encode(), hashlib.sha256).hexdigest()


def new_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(30)


def record_of(row: ApiKey) -> KeyRecord:
    return KeyRecord(
        id=row.id,
        revoked=row.revoked_at is not None,
        per_minute=row.per_minute,
        per_day=row.per_day,
    )


def create_key(
    session: Session,
    name: str,
    *,
    per_minute: int | None = None,
    per_day: int | None = None,
    now: datetime | None = None,
) -> tuple[ApiKey, str]:
    """Store a new key; returns its row and the plain key, which is not stored anywhere."""
    key = new_key()
    row = ApiKey(
        id=secrets.token_hex(8),
        key_hash=hash_key(key),
        prefix=key[:PREFIX_LENGTH],
        name=name[:100],
        created_at=now or utc_now(),
        per_minute=per_minute,
        per_day=per_day,
    )
    session.add(row)
    session.flush()
    return row, key


def list_keys(session: Session) -> list[ApiKey]:
    return list(session.scalars(select(ApiKey).order_by(ApiKey.created_at)))


def revoke_key(session: Session, key_id: str, now: datetime | None = None) -> ApiKey | None:
    """Mark the key revoked (kept for the record); None if there is no such key."""
    row = session.get(ApiKey, key_id)
    if row is not None and row.revoked_at is None:
        row.revoked_at = now or utc_now()
        session.flush()
    return row


def _lookup(session: Session, key_hash: str) -> KeyRecord:
    row = session.scalar(select(ApiKey).where(ApiKey.key_hash == key_hash))
    return record_of(row) if row is not None else UNKNOWN


async def publish(key_hash: str, record: KeyRecord) -> None:
    """Put ``record`` in the shared cache, so every instance sees a revocation soon."""
    _local.pop(key_hash, None)
    kv = get_kv()
    if kv is None:
        return
    try:
        await kv.set(CACHE_KEY.format(hash=key_hash), json.dumps(asdict(record)), ex=CACHE_TTL)
    except KVError as exc:
        log.warning("api key cache write failed", extra={"error": type(exc).__name__})


async def _cached(key_hash: str) -> KeyRecord | None:
    kv = get_kv()
    if kv is None:
        return None
    try:
        raw = await kv.get(CACHE_KEY.format(hash=key_hash))
        return KeyRecord(**json.loads(raw)) if raw else None
    except (KVError, ValueError, TypeError):
        return None


def _remember(key_hash: str, record: KeyRecord) -> None:
    if len(_local) >= LOCAL_MAX:
        _local.clear()
    _local[key_hash] = (time.monotonic() + LOCAL_TTL, record)


async def authenticate(key: str) -> KeyRecord:
    """The record for ``key`` (``UNKNOWN`` if no key matches).

    Raises ``ApiNotConfiguredError`` without a secret and ``KeyStoreUnavailableError`` when
    the key is not cached and the database cannot be read.
    """
    key_hash = hash_key(key)
    found = _local.get(key_hash)
    if found is not None and found[0] > time.monotonic():
        return found[1]
    record = await _cached(key_hash)
    if record is None:
        record = await read(_lookup, key_hash)
        if record is None:
            raise KeyStoreUnavailableError("the API key database is not configured or unavailable")
        # Unknown keys are remembered only here: random guesses must not fill Redis.
        if record.id:
            await publish(key_hash, record)
    _remember(key_hash, record)
    return record


def clear_cache() -> None:
    _local.clear()
