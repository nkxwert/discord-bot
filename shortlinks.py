"""Short links through NKOTE (github.com/nkxwert/NKOTE), served at nnaco.kro.kr.

Used for the upload page link and the [원본 열기] buttons, so links stay short (Discord link buttons hold
512 characters) and carry no "#|" for Discord to rewrite. Every short link expires with what it points to.
If NKOTE isn't configured or doesn't answer, callers simply keep the long link."""
import hashlib
import os
import secrets
import time
from urllib.parse import urlsplit

import aiohttp
from dotenv import load_dotenv

import net

load_dotenv()  # bot.py imports this module before it loads .env itself

BASE_URL = os.getenv("NKOTE_URL", "").strip().rstrip("/")
API_KEY = os.getenv("NKOTE_API_KEY", "").strip()
# NKOTE's own alphabet (no 0/O/1/l/I). 10 characters (vs. its default 6) so a link holding an upload token
# can't be found by guessing.
ALPHABET = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
SLUG_LENGTH = 10
TIMEOUT = aiohttp.ClientTimeout(total=6)


def configured():
    return bool(BASE_URL and API_KEY)


def host():
    return urlsplit(BASE_URL).netloc


def _headers():
    return {"Authorization": f"Bearer {API_KEY}"}


async def shorten(url, expires_at):
    """Short URL for url that expires at expires_at (epoch seconds), or None (not configured, too close to
    expiring, or NKOTE failed: the caller keeps the long link)."""
    if not configured() or expires_at - time.time() < 120:
        return None
    for _ in range(2):  # a taken slug (409) is retried once with a new one
        slug = "".join(secrets.choice(ALPHABET) for _ in range(SLUG_LENGTH))
        try:
            async with net.session().post(
                f"{BASE_URL}/api/links", headers=_headers(), timeout=TIMEOUT,
                json={"url": url, "slug": slug, "expiresAt": int(expires_at * 1000)},
            ) as response:
                data = await response.json(content_type=None)
        except Exception as exc:
            print(f"단축 링크 만들기 실패: {type(exc).__name__}: {exc}")
            return None
        if response.status == 201:
            return data.get("shortUrl")
        if response.status != 409:
            error = (data or {}).get("error") or {}
            print(f"단축 링크 만들기 실패: HTTP {response.status} {error.get('code', '')} {error.get('message', '')}")
            return None
    return None


def slug_of(short_url):
    return urlsplit(short_url).path.strip("/") if short_url else ""


async def delete(short_url):
    """Remove a short link (best effort; it would expire on its own anyway)."""
    # By slug only: NKOTE_URL (where the API is called) may differ from the domain in the short links
    # (NKOTE's PUBLIC_BASE_URL), e.g. nkote.vercel.app vs nnaco.kro.kr.
    slug = slug_of(short_url)
    if not configured() or not slug:
        return
    try:
        async with net.session().delete(f"{BASE_URL}/api/links/{slug}", headers=_headers(), timeout=TIMEOUT):
            pass
    except Exception as exc:
        print(f"단축 링크 삭제 실패: {type(exc).__name__}: {exc}")


async def self_test():
    """[(label, ok, detail)] for £DB 테스트: make a short link, open it (without following), delete it."""
    if not configured():
        return [("단축 링크 (NKOTE)", True, "설정 안 됨 (NKOTE_URL / NKOTE_API_KEY) · 긴 링크를 씁니다")]
    target = f"https://example.com/maco-selftest#b2!x!y!files/1/z/upload!7%EC%9D%BC"
    short = await shorten(target, time.time() + 300)
    if not short:
        return [("단축 링크 만들기", False, "실패 (봇 로그에 이유가 남습니다)")]
    results = [("단축 링크 만들기", True, short)]
    try:
        async with net.session().get(short, allow_redirects=False, timeout=TIMEOUT) as response:
            location = response.headers.get("Location", "")
        ok = response.status in (301, 302, 307, 308) and location == target
        results.append(("단축 링크 열기", ok, f"HTTP {response.status}" + ("" if ok else f" → {location[:80]}")))
    except Exception as exc:
        results.append(("단축 링크 열기", False, f"{type(exc).__name__}: {exc}"))
    await delete(short)
    return results


# ---- upload signals: the upload page reports "started"/"done" to NKOTE; the bot reads them with its key,
# so it notices a finished upload within seconds without polling the file storage.

def signal_id(key):
    """Per-upload id, derived from the storage key and the bot's secret key: unguessable, and the same after
    a restart without being stored anywhere."""
    return hashlib.sha256(f"maco-signal:{API_KEY}:{key}".encode()).hexdigest()[:32]


def signal_url(key):
    return f"{BASE_URL}/api/signal/{signal_id(key)}" if configured() else None


async def get_signal(key):
    """"started", "done", None (nothing yet), or raises on a connection problem."""
    async with net.session().get(f"{BASE_URL}/api/signal/{signal_id(key)}", headers=_headers(),
                                 timeout=TIMEOUT) as response:
        if response.status == 404:
            return None
        if response.status != 200:
            raise RuntimeError(f"NKOTE 신호 확인 HTTP {response.status}")
        return (await response.json(content_type=None)).get("state")
