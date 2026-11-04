"""Operator alerts over Telegram: source quota near exhaustion and tool error-rate spikes.

Enabled when ``DOTA_INSIGHTS_TELEGRAM_BOT_TOKEN`` and ``DOTA_INSIGHTS_TELEGRAM_CHAT_ID`` are set.
Each alert kind is sent at most once per ``alert_throttle_seconds`` across all instances (a Redis
``SET NX`` marker, or this instance's memory without Redis). Sending never fails a tool call:
Telegram errors are logged (without the token) and dropped.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

import httpx

from dota_insights.config import settings
from dota_insights.kv import KVError, get_kv
from dota_insights.sources.quota import QuotaUsage, observe

logger = logging.getLogger(__name__)

API_URL = "https://api.telegram.org"
ERROR_WINDOW = 300  # seconds per error-rate window


class Alerter:
    def __init__(
        self,
        token: str,
        chat_id: str,
        *,
        throttle: int = 1800,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.time,
        base_url: str = API_URL,
    ) -> None:
        self._token = token
        self.chat_id = chat_id
        self.throttle = throttle
        self._transport = transport
        self._clock = clock
        self._base_url = base_url
        self._sent: dict[str, float] = {}  # key -> when, without Redis
        self._local_counts: dict[str, int] = {}
        self._quota_keys: set[str] = set()  # per-day quota alerts this instance already tried

    async def _claim(self, key: str) -> bool:
        """True if no alert with ``key`` went out within the throttle window."""
        kv = get_kv()
        if kv is not None:
            try:
                return await kv.set_nx(f"di:alert:{key}", "1", ex=self.throttle)
            except KVError:
                pass
        now = self._clock()
        last = self._sent.get(key)
        if last is not None and now - last < self.throttle:
            return False
        self._sent[key] = now
        return True

    async def alert(self, key: str, text: str) -> bool:
        """Send ``text`` unless an alert with ``key`` was sent recently; True if sent."""
        if not await self._claim(key):
            return False
        logger.warning("alert", extra={"alert": key, "text": text})
        try:
            async with httpx.AsyncClient(timeout=3.0, transport=self._transport) as client:
                response = await client.post(
                    f"{self._base_url}/bot{self._token}/sendMessage",
                    json={"chat_id": self.chat_id, "text": f"[dota-insights] {text}"},
                )
            if response.status_code >= 400:
                logger.warning("telegram alert failed", extra={"status": response.status_code})
                return False
        except httpx.HTTPError as exc:
            logger.warning("telegram alert failed", extra={"error": type(exc).__name__})
            return False
        return True

    async def _count(self, windows: list[tuple[str, int]]) -> list[int]:
        kv = get_kv()
        if kv is not None:
            try:
                return await kv.incr_windows(windows)
            except KVError:
                pass
        current = {key for key, _ in windows}
        for key in [k for k in self._local_counts if k not in current]:
            del self._local_counts[key]
        for key, _ in windows:
            self._local_counts[key] = self._local_counts.get(key, 0) + 1
        return [self._local_counts[key] for key, _ in windows]

    async def record_call(self, failed: bool) -> None:
        """Count a tool call; alert when the window's error rate crosses the threshold."""
        bucket = int(self._clock() // ERROR_WINDOW)
        windows = [(f"di:calls:{bucket}", 2 * ERROR_WINDOW)]
        if failed:
            windows.append((f"di:errors:{bucket}", 2 * ERROR_WINDOW))
        counts = await self._count(windows)
        if not failed:
            return
        calls, errors = counts
        if calls >= settings.alert_error_min_calls and errors / calls >= settings.alert_error_rate:
            await self.alert(
                "error-rate",
                f"{errors} of the last {calls} tool calls failed (in the current "
                f"{ERROR_WINDOW // 60}-minute window). Check the logs and /status.",
            )

    async def on_quota(self, usage: QuotaUsage) -> None:
        if usage.day_used < settings.alert_quota_ratio * usage.day_limit:
            return
        day = int(self._clock() // 86400)
        if usage.day_used >= usage.day_limit:
            key, state = f"quota:{usage.source}:{day}:spent", "is spent"
        else:
            key, state = f"quota:{usage.source}:{day}", "is nearly spent"
        if key in self._quota_keys:
            return
        self._quota_keys.add(key)
        await self.alert(
            key,
            f"The server's daily {usage.source} quota {state}: {usage.day_used} of "
            f"{usage.day_limit} calls used today (UTC). Calls over it fail until midnight UTC.",
        )


_UNSET: object = object()
_alerter: Alerter | None | object = _UNSET


def get_alerter() -> Alerter | None:
    """The configured alerter, or None when Telegram is not set up."""
    global _alerter
    if _alerter is _UNSET:
        if settings.telegram_bot_token and settings.telegram_chat_id:
            _alerter = Alerter(
                settings.telegram_bot_token,
                settings.telegram_chat_id,
                throttle=settings.alert_throttle_seconds,
            )
        else:
            _alerter = None
    return _alerter  # type: ignore[return-value]


def use_alerter(alerter: Alerter | None) -> None:
    """Replace the alerter (for tests); ``None`` disables alerts."""
    global _alerter
    _alerter = alerter


async def _quota_observer(usage: QuotaUsage) -> None:
    alerter = get_alerter()
    if alerter is not None:
        await alerter.on_quota(usage)


observe(_quota_observer)
