"""External data source clients (OpenDota, STRATZ, Steam Web API).

Tools get clients through ``get_opendota()`` and ``get_stratz()`` so the whole server shares one
rate limiter per source. Tests replace them with ``use_clients(...)``.
"""

from dota_insights.sources.opendota import OpenDotaClient
from dota_insights.sources.stratz import StratzClient

_opendota: OpenDotaClient | None = None
_stratz: StratzClient | None = None


def get_opendota() -> OpenDotaClient:
    """The shared OpenDota client, created on first use."""
    global _opendota
    if _opendota is None:
        _opendota = OpenDotaClient()
    return _opendota


def get_stratz() -> StratzClient:
    """The shared STRATZ client, created on first use."""
    global _stratz
    if _stratz is None:
        _stratz = StratzClient()
    return _stratz


def use_clients(*, opendota: OpenDotaClient | None = None, stratz: StratzClient | None = None):
    """Replace the shared clients (for tests). Passing ``None`` resets that source."""
    global _opendota, _stratz
    _opendota, _stratz = opendota, stratz


async def close_clients() -> None:
    """Close any shared clients that were created."""
    global _opendota, _stratz
    for client in (_opendota, _stratz):
        if client is not None:
            await client.aclose()
    _opendota = _stratz = None
