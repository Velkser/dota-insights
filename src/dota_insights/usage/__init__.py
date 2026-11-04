"""Usage analytics (p4-08), client feedback (p5-02) and unmet questions (p5-03).

- ``events``: what is stored about a call: argument shape, hashed client, client name.
- ``store``: the Redis queue the server writes to, and the flush/retention the collector runs.
- ``report``: calls, clients, success rates, latency, failures and feedback over a window.
- ``cli``: ``dota-insights usage report|flush|prune`` and ``dota-insights privacy ...``.

Recording never slows or fails a tool call: it is bounded by ``usage_write_timeout`` and every
error is swallowed (see ``record_call``).
"""

from dota_insights.usage.events import record_call

__all__ = ["record_call"]
