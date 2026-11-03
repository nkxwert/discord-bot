"""Player stat lookups: Hypixel (via Mojang + Hypixel API) and Valorant (via HenrikDev API)."""
import os
import re
import time
import asyncio
from datetime import datetime, timezone
from urllib.parse import quote

import aiohttp

import net

CACHE_SECONDS = 300
PARTIAL_CACHE_SECONDS = 30  # a Valorant result missing its matches is retried soon
MATCHES_TIMEOUT_SECONDS = 20  # stored-matches is often slower than the other endpoints
MATCHES_RETRY_TIMEOUT_SECONDS = 15
MAX_CACHE_ENTRIES = 150  # profiles hold icon images; keep the cache small on a 128MB host
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10)
MOJANG_PROFILE_URL = "https://api.mojang.com/users/profiles/minecraft/"
HYPIXEL_API_URL = "https://api.hypixel.net/v2/"
HENRIK_API_URL = "https://api.henrikdev.xyz/valorant/"
SKIN_URLS = (
    "https://crafatar.com/renders/body/{uuid}?overlay&scale=4",
    "https://mc-heads.net/body/{uuid}/240",
)
VALORANT_REGIONS = ("kr", "ap", "na", "eu", "latam", "br")
MATCH_COUNT = 10  # matches used for K/D and other averages
MATCHES_SHOWN = 5
CONFUSABLE = {"i": "l1", "l": "i1", "1": "li", "o": "0", "0": "o"}
MAX_VARIANT_LOOKUPS = 6
MAX_SUGGESTIONS = 3
AGENT_ICON_URL = "https://media.valorant-api.com/agents/{id}/displayicon.png"
MAX_IMAGE_CACHE = 100

_cache = {}
_image_cache = {}  # static game assets (rank and agent icons), keyed by URL


class StatsError(Exception):
    """An error whose message can be shown to Discord users as-is."""


class PlayerNotFound(StatsError):
    """The API answered 404 for the requested player."""


class ServiceError(StatsError):
    """A missing or expired API key, a rate limit or an outage: not the user's fault, so the bot admin
    should be told. str() is the message for the user; detail says what actually went wrong."""

    def __init__(self, message, detail):
        super().__init__(message)
        self.detail = detail


def _api_cause(data):
    """The error text an API put in its JSON body, if any (Hypixel: cause, HenrikDev: errors[].message)."""
    if not isinstance(data, dict):
        return ""
    if data.get("cause"):
        return str(data["cause"])
    errors = data.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        return str(errors[0].get("message") or "")
    return ""


async def _cached(key, loader, ttl=None):
    """Cache loader()'s value; ttl(value) may shorten how long (in seconds) a given value is kept."""
    now = time.time()
    hit = _cache.get(key)
    if hit and now < hit[0]:
        return hit[1]
    value = await loader()
    if len(_cache) >= MAX_CACHE_ENTRIES:
        for old_key in [k for k, (expires, _) in _cache.items() if now >= expires]:
            del _cache[old_key]
        # Still full of fresh entries: drop the ones closest to expiring so the cap really holds.
        for old_key in sorted(_cache, key=lambda k: _cache[k][0])[:len(_cache) - MAX_CACHE_ENTRIES + 1]:
            del _cache[old_key]
    _cache[key] = (now + (ttl(value) if ttl else CACHE_SECONDS), value)
    return value


async def _request(url, *, headers=None, params=None, raw=False, timeout=None):
    timeout = timeout or REQUEST_TIMEOUT
    try:
        # One shared session (net.py) instead of a new one per request.
        async with net.session().get(url, headers=headers, params=params, timeout=timeout) as response:
            if raw:
                return response.status, await response.read()
            if response.status == 204:
                return response.status, None
            return response.status, await response.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
        host = url.split("/")[2] if "://" in url else url
        if isinstance(exc, asyncio.TimeoutError):
            reason = f"응답 시간 초과 ({timeout.total:.0f}초)"  # TimeoutError has no message of its own
        else:
            reason = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
        print(f"외부 API 요청 실패 ({url.split('?')[0]}): {reason}")
        error = ServiceError(
            "외부 서비스에 연결하지 못했습니다. 잠시 후 다시 시도해 주세요.",
            f"{host} 연결 실패: {reason}",
        )
        error.timed_out = isinstance(exc, asyncio.TimeoutError)
        raise error from exc


async def _report(progress, step):
    """Tell the caller which step a lookup is on; progress is an optional async callable."""
    if progress:
        await progress(step)


def ratio(a, b):
    return f"{a / b:.2f}" if b else f"{a:.2f}"


# ---------------------------------------------------------------- Hypixel

MC_COLOR_NAMES = {
    "BLACK": "0", "DARK_BLUE": "1", "DARK_GREEN": "2", "DARK_AQUA": "3",
    "DARK_RED": "4", "DARK_PURPLE": "5", "GOLD": "6", "GRAY": "7",
    "DARK_GRAY": "8", "BLUE": "9", "GREEN": "a", "AQUA": "b",
    "RED": "c", "LIGHT_PURPLE": "d", "YELLOW": "e", "WHITE": "f",
}
STAFF_RANKS = {
    "OWNER": "§c[OWNER]",
    "ADMIN": "§c[ADMIN]",
    "GAME_MASTER": "§2[GM]",
    "MODERATOR": "§2[MOD]",
    "HELPER": "§9[HELPER]",
    "YOUTUBER": "§c[§fYOUTUBE§c]",
}


def rank_prefix(player):
    """Hypixel rank as a §-color-coded string such as '§b[MVP§c+§b]', or '' for no rank."""
    if player.get("prefix"):
        return player["prefix"]
    if player.get("rank") in STAFF_RANKS:
        return STAFF_RANKS[player["rank"]]
    plus = "§" + MC_COLOR_NAMES.get(player.get("rankPlusColor"), "c")
    if player.get("monthlyPackageRank") == "SUPERSTAR":
        base = "§" + MC_COLOR_NAMES.get(player.get("monthlyRankColor"), "6")
        return f"{base}[MVP{plus}++{base}]"
    package = player.get("newPackageRank") or player.get("packageRank")
    return {
        "MVP_PLUS": f"§b[MVP{plus}+§b]",
        "MVP": "§b[MVP]",
        "VIP_PLUS": "§a[VIP§6+§a]",
        "VIP": "§a[VIP]",
    }.get(package, "")


def display_name(player, name):
    prefix = rank_prefix(player)
    if not prefix:
        return f"§7{name}"
    # The name takes the colour the prefix starts with.
    match = re.match(r"§([0-9a-f])", prefix)
    return f"{prefix} §{match.group(1) if match else '7'}{name}"


def network_level(exp):
    return int(1 + (-8750.0 + (8750 ** 2 + 5000 * exp) ** 0.5) / 2500)


def login_time(ms):
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def game_stats(player):
    stats = player.get("stats") or {}
    bw = stats.get("Bedwars") or {}
    sw = stats.get("SkyWars") or {}
    duels = stats.get("Duels") or {}
    star = (player.get("achievements") or {}).get("bedwars_level", 0)

    def g(source, key):
        return source.get(key, 0) or 0

    return {
        "bedwars": {
            "title": "Bed Wars",
            "wins": g(bw, "wins_bedwars"),
            "rows": [
                ("Star", star),
                ("Wins", g(bw, "wins_bedwars")),
                ("Losses", g(bw, "losses_bedwars")),
                ("WLR", ratio(g(bw, "wins_bedwars"), g(bw, "losses_bedwars"))),
                ("Final Kills", g(bw, "final_kills_bedwars")),
                ("Final Deaths", g(bw, "final_deaths_bedwars")),
                ("FKDR", ratio(g(bw, "final_kills_bedwars"), g(bw, "final_deaths_bedwars"))),
                ("Beds Broken", g(bw, "beds_broken_bedwars")),
                ("Kills", g(bw, "kills_bedwars")),
                ("Deaths", g(bw, "deaths_bedwars")),
                ("KDR", ratio(g(bw, "kills_bedwars"), g(bw, "deaths_bedwars"))),
                ("Winstreak", g(bw, "winstreak")),
            ],
        },
        "skywars": {
            "title": "SkyWars",
            "wins": g(sw, "wins"),
            "rows": [
                ("Level", sw.get("levelFormatted") or "-"),
                ("Wins", g(sw, "wins")),
                ("Losses", g(sw, "losses")),
                ("WLR", ratio(g(sw, "wins"), g(sw, "losses"))),
                ("Kills", g(sw, "kills")),
                ("Deaths", g(sw, "deaths")),
                ("KDR", ratio(g(sw, "kills"), g(sw, "deaths"))),
                ("Coins", g(sw, "coins")),
                ("Souls", g(sw, "souls")),
            ],
        },
        "duels": {
            "title": "Duels",
            "wins": g(duels, "wins"),
            "rows": [
                ("Wins", g(duels, "wins")),
                ("Losses", g(duels, "losses")),
                ("WLR", ratio(g(duels, "wins"), g(duels, "losses"))),
                ("Kills", g(duels, "kills")),
                ("Deaths", g(duels, "deaths")),
                ("KDR", ratio(g(duels, "kills"), g(duels, "deaths"))),
                ("Winstreak", g(duels, "current_winstreak")),
                ("Best Streak", g(duels, "best_overall_winstreak")),
                ("Coins", g(duels, "coins")),
            ],
        },
    }


def guild_stats(guild, uuid):
    if not guild:
        return None
    member = next((m for m in guild.get("members") or [] if m.get("uuid") == uuid), {})
    # expHistory is keyed by date (YYYY-MM-DD); the newest day is "today".
    history = [exp for _, exp in sorted((member.get("expHistory") or {}).items(), reverse=True)]
    return {
        "name": guild.get("name") or "-",
        "quests": member.get("questParticipation", 0) or 0,
        "daily_gexp": history[0] if history else 0,
        "weekly_gexp": sum(history[:7]),
    }


async def minecraft_profile(name):
    if not re.fullmatch(r"[A-Za-z0-9_]{1,16}", name):
        raise StatsError("마인크래프트 닉네임 형식이 올바르지 않습니다. (영문, 숫자, _ 1~16자)")
    status, data = await _request(MOJANG_PROFILE_URL + name)
    if status in (204, 404) or (status == 200 and not (data or {}).get("id")):
        raise StatsError(f"`{name}` 플레이어를 찾을 수 없습니다.")
    if status != 200:
        raise ServiceError(
            "마인크래프트 계정 조회에 실패했습니다. 잠시 후 다시 시도해 주세요.",
            f"Mojang API HTTP {status} {_api_cause(data)}".strip(),
        )
    return data["id"], data["name"]


async def _hypixel(path, **params):
    key = os.getenv("HYPIXEL_API_KEY", "").strip()
    if not key:
        raise ServiceError(
            "Hypixel API 키가 설정되지 않았습니다. 봇 관리자에게 알렸습니다.",
            "HYPIXEL_API_KEY가 설정되지 않음",
        )
    status, data = await _request(HYPIXEL_API_URL + path, headers={"API-Key": key}, params=params)
    cause = _api_cause(data)
    if status == 403:
        raise ServiceError(
            "Hypixel API 키가 올바르지 않거나 만료되었습니다. 봇 관리자에게 알렸습니다.",
            f"Hypixel API 403 (키 무효 또는 만료) {cause}".strip(),
        )
    if status == 429:
        raise ServiceError(
            "Hypixel API 요청 한도를 넘었습니다. 잠시 후 다시 시도해 주세요.",
            f"Hypixel API 429 (요청 한도 초과) {cause}".strip(),
        )
    if status != 200 or not (data or {}).get("success"):
        raise ServiceError(
            "Hypixel 조회에 실패했습니다. 잠시 후 다시 시도해 주세요.",
            f"Hypixel API /{path} HTTP {status} {cause}".strip(),
        )
    return data


async def fetch_skin(uuid):
    for template in SKIN_URLS:
        try:
            status, body = await _request(template.format(uuid=uuid), raw=True)
        except StatsError:
            continue
        if status == 200 and body:
            return body
    return None


async def fetch_image(url):
    if not url:
        return None
    if url in _image_cache:
        return _image_cache[url]
    try:
        status, body = await _request(url, raw=True)
    except StatsError:
        return None
    data = body if status == 200 and body else None
    if data and len(_image_cache) < MAX_IMAGE_CACHE:
        _image_cache[url] = data
    return data


async def hypixel_profile(name, progress=None):
    """Steps reported to progress: "account", "player", "extra"."""
    async def load():
        await _report(progress, "account")
        uuid, real_name = await minecraft_profile(name)
        await _report(progress, "player")
        player = (await _hypixel("player", uuid=uuid)).get("player")
        if not player:
            raise StatsError(f"`{real_name}` 님은 Hypixel에 접속한 기록이 없습니다.")
        await _report(progress, "extra")
        guild_data, status_data, skin = await asyncio.gather(
            _hypixel("guild", player=uuid),
            _hypixel("status", uuid=uuid),
            fetch_skin(uuid),
            return_exceptions=True,
        )
        guild = guild_data.get("guild") if isinstance(guild_data, dict) else None
        session = status_data.get("session") if isinstance(status_data, dict) else None
        quests = player.get("quests") or {}
        challenges = (player.get("challenges") or {}).get("all_time") or {}
        gifting = player.get("giftingMeta") or {}
        return {
            "uuid": uuid,
            "name": player.get("displayname") or real_name,
            "display_name": display_name(player, player.get("displayname") or real_name),
            "level": network_level(player.get("networkExp", 0) or 0),
            "achievement_points": player.get("achievementPoints", 0) or 0,
            "karma": player.get("karma", 0) or 0,
            "reward_streak": player.get("rewardStreak", 0) or 0,
            "quests": sum(len(q.get("completions") or []) for q in quests.values() if isinstance(q, dict)),
            "challenges": sum(v for v in challenges.values() if isinstance(v, int)),
            "gifts_sent": gifting.get("giftsGiven", 0) or 0,
            "ranks_gifted": gifting.get("realBundlesGiven", 0) or 0,
            "first_login": login_time(player.get("firstLogin")),
            "last_login": login_time(player.get("lastLogin")),
            "online": session.get("online") if session else None,
            "guild": guild_stats(guild, uuid),
            "games": game_stats(player),
            "skin": skin if isinstance(skin, bytes) else None,
        }

    return await _cached(("hypixel", name.lower()), load)


# ---------------------------------------------------------------- Valorant

def parse_riot_id(text):
    """'이름#태그 [지역]' -> (name, tag, region). region is None when it should be detected."""
    text = text.strip()
    region = None
    parts = text.rsplit(maxsplit=1)
    if len(parts) == 2 and parts[1].lower() in VALORANT_REGIONS and "#" in parts[0]:
        text, region = parts[0], parts[1].lower()
    if "#" not in text:
        raise StatsError("Riot ID를 `이름#태그` 형식으로 입력해 주세요. (예: `!V전적 Hide on bush#KR1`)")
    # Keep spaces in the name as typed: a space before '#' can be part of a real Riot ID.
    name, tag = text.rsplit("#", 1)
    tag = tag.strip()
    if not name.strip() or not tag or len(name) > 16 or len(tag) > 5:
        raise StatsError("Riot ID 형식이 올바르지 않습니다. (이름 16자, 태그 5자 이하)")
    return name, tag, region


async def _henrik(path, timeout=None):
    key = os.getenv("HENRIK_API_KEY", "").strip()
    if not key:
        raise ServiceError(
            "HenrikDev API 키가 설정되지 않았습니다. 봇 관리자에게 알렸습니다.",
            "HENRIK_API_KEY가 설정되지 않음",
        )
    status, data = await _request(HENRIK_API_URL + path, headers={"Authorization": key}, timeout=timeout)
    cause = _api_cause(data)
    if status == 404:
        raise PlayerNotFound("플레이어를 찾을 수 없습니다. Riot ID를 확인해 주세요.")
    if status in (401, 403):
        raise ServiceError(
            "HenrikDev API 키가 올바르지 않습니다. 봇 관리자에게 알렸습니다.",
            f"HenrikDev API {status} (키 무효 또는 만료) {cause}".strip(),
        )
    if status == 429:
        raise ServiceError(
            "HenrikDev API 요청 한도를 넘었습니다. 잠시 후 다시 시도해 주세요.",
            f"HenrikDev API 429 (요청 한도 초과) {cause}".strip(),
        )
    if status != 200 or not isinstance(data, dict):
        endpoint = path.split("/")[0] + "/" + path.split("/")[1] if "/" in path else path
        raise ServiceError(
            "발로란트 전적 조회에 실패했습니다. 잠시 후 다시 시도해 주세요.",
            f"HenrikDev API {endpoint} HTTP {status} {cause}".strip(),
        )
    return data.get("data")


def match_summary(match):
    meta = match.get("meta") or {}
    stats = match.get("stats") or {}
    teams = match.get("teams") or {}
    shots = stats.get("shots") or {}
    team = (stats.get("team") or "").lower()
    result, won, rounds = "-", None, 0
    if team in ("red", "blue"):
        mine, theirs = teams.get(team), teams.get("blue" if team == "red" else "red")
        if isinstance(mine, int) and isinstance(theirs, int):
            result = "승" if mine > theirs else "패" if mine < theirs else "무"
            result += f" {mine}:{theirs}"
            won = mine > theirs if mine != theirs else None
            rounds = mine + theirs
    kills, deaths, assists = (stats.get(k, 0) or 0 for k in ("kills", "deaths", "assists"))
    return {
        "result": result,
        "won": won,
        "agent": (stats.get("character") or {}).get("name") or "-",
        "agent_id": (stats.get("character") or {}).get("id"),
        "kd": ratio(kills, deaths),
        "map": (meta.get("map") or {}).get("name") or "-",
        "mode": meta.get("mode") or "-",
        "kills": kills,
        "deaths": deaths,
        "assists": assists,
        "kda": f"{kills}/{deaths}/{assists}",
        "headshots": shots.get("head", 0) or 0,
        "shots": sum(shots.get(k, 0) or 0 for k in ("head", "body", "leg")),
        "damage": (stats.get("damage") or {}).get("made", 0) or 0,
        "rounds": rounds,
    }


def match_totals(matches):
    """Win rate, K/D, average KDA, headshot % and ADR over matches, skipping Deathmatch modes."""
    counted = [m for m in matches if "deathmatch" not in m["mode"].lower()]
    if not counted:
        return None
    n = len(counted)
    kills = sum(m["kills"] for m in counted)
    deaths = sum(m["deaths"] for m in counted)
    assists = sum(m["assists"] for m in counted)
    decided = [m["won"] for m in counted if m["won"] is not None]
    shots = sum(m["shots"] for m in counted)
    rounds = sum(m["rounds"] for m in counted)
    return {
        "games": n,
        "win_rate": f"{sum(decided) / len(decided) * 100:.0f}%" if decided else "-",
        "record": f"{sum(decided)}승 {len(decided) - sum(decided)}패" if decided else "",
        "kd": ratio(kills, deaths),
        "kda": f"{kills / n:.1f} / {deaths / n:.1f} / {assists / n:.1f}",
        "headshot": f"{sum(m['headshots'] for m in counted) / shots * 100:.1f}%" if shots else "-",
        "adr": f"{sum(m['damage'] for m in counted) / rounds:.0f}" if rounds else "-",
    }


async def valorant_account(name, tag):
    """Canonical name/tag and home region of a Riot ID. Raises PlayerNotFound if it doesn't exist."""
    async def load():
        data = await _henrik(f"v1/account/{quote(name, safe='')}/{quote(tag, safe='')}") or {}
        return {
            "name": data.get("name") or name,
            "tag": data.get("tag") or tag,
            "region": (data.get("region") or "").lower() or None,
        }

    return await _cached(("valorant-account", name.lower(), tag.lower()), load)


def riot_id_variants(name, tag):
    """Riot IDs one small slip away from name#tag: spacing first, then one confused character (I/l/1, O/0)."""
    variants = []

    def add(n, t):
        if n.strip() and len(n) <= 16 and (n, t) != (name, tag) and (n, t) not in variants:
            variants.append((n, t))

    # A missing or stray space before '#', or spaces typed inside the name.
    add(name.rstrip() if name != name.rstrip() else name + " ", tag)
    add(name.replace(" ", ""), tag)
    for in_tag in (True, False):
        text = tag if in_tag else name
        for i, char in enumerate(text):
            for replacement in CONFUSABLE.get(char.lower(), ""):
                changed = text[:i] + replacement + text[i + 1:]
                add(*((name, changed) if in_tag else (changed, tag)))
    return variants[:MAX_VARIANT_LOOKUPS]


async def similar_accounts(name, tag):
    """Existing accounts one small slip away from name#tag, as (accounts, failure).

    Variants are tried one at a time so a burst can't eat the API's rate limit. If a lookup fails for a
    reason other than "not found" (rate limit, outage), the search stops and that error is returned as
    failure, so callers don't claim "no similar account" when they simply couldn't check.
    Each account carries the Riot ID that found it (query_name/query_tag), which is what to look up again.
    """
    found, seen = [], set()
    for query_name, query_tag in riot_id_variants(name, tag):
        try:
            account = await valorant_account(query_name, query_tag)
        except PlayerNotFound:
            continue
        except StatsError as exc:
            return found, exc
        key = (account["name"].lower(), account["tag"].lower())
        if key not in seen:
            seen.add(key)
            found.append(dict(account, query_name=query_name, query_tag=query_tag))
            if len(found) >= MAX_SUGGESTIONS:
                break
    return found, None


async def valorant_profile(name, tag, region=None, progress=None):
    """Steps reported to progress: "account", "data", "icons"."""
    await _report(progress, "account")
    account = await valorant_account(name, tag)
    name, tag = account["name"], account["tag"]
    region = region or account["region"] or "kr"

    async def load():
        await _report(progress, "data")
        base = f"{region}/{quote(name, safe='')}/{quote(tag, safe='')}"
        mmr, matches = await asyncio.gather(
            _henrik(f"v2/mmr/{base}"),
            _recent_matches(base),
            return_exceptions=True,
        )
        if isinstance(mmr, PlayerNotFound):
            mmr = {}  # the account exists but has no ranked data in this region
        elif isinstance(mmr, BaseException):
            raise mmr
        mmr = mmr or {}
        # Recent matches are optional: the card still shows the rank, and says the matches couldn't be loaded.
        matches_error = None
        if isinstance(matches, PlayerNotFound):
            matches = []  # no stored matches: really nothing to show
        elif isinstance(matches, BaseException):
            matches_error = matches if isinstance(matches, ServiceError) else ServiceError(
                "최근 경기를 불러오지 못했습니다.", f"HenrikDev stored-matches 오류: {type(matches).__name__}")
            matches = []
        matches = matches or []
        summaries = [match_summary(m) for m in matches[:MATCH_COUNT] if isinstance(m, dict)]
        shown = summaries[:MATCHES_SHOWN]
        current = mmr.get("current_data") or {}
        highest = mmr.get("highest_rank") or {}

        images = current.get("images") or {}
        rank_icon_url = images.get("large") or images.get("small")
        # Rank icons share one URL pattern (.../competitivetiers/<set>/<tier>/...); swap the tier for the peak.
        peak_icon_url = None
        if rank_icon_url and current.get("currenttier") is not None and highest.get("tier") is not None:
            peak_icon_url = rank_icon_url.replace(f"/{current['currenttier']}/", f"/{highest['tier']}/", 1)
        agent_ids = list(dict.fromkeys(m["agent_id"] for m in shown if m["agent_id"]))
        await _report(progress, "icons")
        rank_icon, peak_icon, *agent_icons = await asyncio.gather(
            fetch_image(rank_icon_url),
            fetch_image(peak_icon_url),
            *(fetch_image(AGENT_ICON_URL.format(id=quote(agent_id, safe=""))) for agent_id in agent_ids),
        )
        return {
            "name": mmr.get("name") or name,
            "tag": mmr.get("tag") or tag,
            "region": region,
            "rank": current.get("currenttierpatched") or "Unrated",
            "rank_icon": rank_icon,
            "peak_icon": peak_icon,
            "agent_icons": dict(zip(agent_ids, agent_icons)),
            "rr": current.get("ranking_in_tier"),
            "rr_change": current.get("mmr_change_to_last_game"),
            "elo": current.get("elo"),
            "peak": highest.get("patched_tier"),
            "peak_season": highest.get("season"),
            "matches": shown,
            "totals": match_totals(summaries),
            "matches_error": matches_error,  # set when the matches failed to load (not when there are none)
        }

    return await _cached(
        ("valorant", region, name.lower(), tag.lower()), load,
        ttl=lambda profile: PARTIAL_CACHE_SECONDS if profile.get("matches_error") else CACHE_SECONDS,
    )


async def _recent_matches(base):
    """stored-matches with a longer timeout and one retry when it times out (HenrikDev is often slow here)."""
    path = f"v1/stored-matches/{base}?size={MATCH_COUNT}"
    try:
        return await _henrik(path, aiohttp.ClientTimeout(total=MATCHES_TIMEOUT_SECONDS))
    except ServiceError as exc:
        if not getattr(exc, "timed_out", False):
            raise
    return await _henrik(path, aiohttp.ClientTimeout(total=MATCHES_RETRY_TIMEOUT_SECONDS))
