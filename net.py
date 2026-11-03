"""One shared aiohttp session for every outside request (stats APIs, server status fallback, file storage).

Opening a session per request loads the TLS certificate bundle and a fresh connection pool each time,
which costs memory and time; a single session keeps connections alive and reuses them."""
import aiohttp

USER_AGENT = "maco-discord-bot"
DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=30)

_session = None


def session():
    """The shared session, created on first use (it must be made inside the running event loop)."""
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(
            headers={"User-Agent": USER_AGENT},
            timeout=DEFAULT_TIMEOUT,
            connector=aiohttp.TCPConnector(limit=20, ttl_dns_cache=300),
        )
    return _session


async def close():
    global _session
    if _session is not None and not _session.closed:
        await _session.close()
    _session = None
