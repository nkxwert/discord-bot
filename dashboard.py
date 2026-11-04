"""Connection to the bot dashboard on NKOTE (nnaco.kro.kr/admin).

The bot runs no web server (its host has 128MB): it asks NKOTE for waiting jobs, every 30 seconds while
nobody has the dashboard open and every 3 seconds while someone does, and posts back results and a small
snapshot of its state. Uses the same NKOTE_URL / NKOTE_API_KEY as the short links and the shared HTTP
session, so it adds no library and keeps nothing in memory between polls."""
import aiohttp

import net
import shortlinks

TIMEOUT = aiohttp.ClientTimeout(total=10)


class NotBound(Exception):
    """NKOTE says another key is connected to the dashboard."""


def configured():
    return shortlinks.configured()


async def sync():
    """{"wake": bool, "jobs": [...]}. Raises NotBound, or another exception on a connection problem."""
    async with net.session().get(f"{shortlinks.BASE_URL}/api/bot/sync", headers=shortlinks._headers(),
                                 timeout=TIMEOUT) as response:
        if response.status == 403:
            raise NotBound()
        if response.status == 404:  # an NKOTE without the dashboard yet
            return {"wake": False, "jobs": [], "missing": True}
        if response.status != 200:
            raise RuntimeError(f"NKOTE 대시보드 HTTP {response.status}")
        return await response.json(content_type=None)


async def report(snapshot=None, results=None):
    body = {}
    if snapshot is not None:
        body["snapshot"] = snapshot
    if results:
        body["results"] = results
    async with net.session().post(f"{shortlinks.BASE_URL}/api/bot/report", headers=shortlinks._headers(),
                                  json=body, timeout=TIMEOUT) as response:
        if response.status != 200:
            raise RuntimeError(f"NKOTE 대시보드 보고 HTTP {response.status}")
