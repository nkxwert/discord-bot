"""Player stat lookups: Hypixel (via Mojang + Hypixel API) and Valorant (via HenrikDev API)."""
import os
import re
import time
import asyncio
from datetime import datetime, timezone
from urllib.parse import quote

import aiohttp

CACHE_SECONDS = 300
MAX_CACHE_ENTRIES = 500
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10)
USER_AGENT = "discord-minecraft-uptime-bot"
MOJANG_PROFILE_URL = "https://api.mojang.com/users/profiles/minecraft/"
HYPIXEL_API_URL = "https://api.hypixel.net/v2/"
HENRIK_API_URL = "https://api.henrikdev.xyz/valorant/"
SKIN_URLS = (
    "https://crafatar.com/renders/body/{uuid}?overlay&scale=4",
    "https://mc-heads.net/body/{uuid}/240",
)
VALORANT_REGIONS = ("kr", "ap", "na", "eu", "latam", "br")

_cache = {}


class StatsError(Exception):
    """An error whose message can be shown to Discord users as-is."""


async def _cached(key, loader):
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < CACHE_SECONDS:
        return hit[1]
    value = await loader()
    if len(_cache) >= MAX_CACHE_ENTRIES:
        for old_key in [k for k, (t, _) in _cache.items() if now - t >= CACHE_SECONDS]:
            del _cache[old_key]
    _cache[key] = (now, value)
    return value


async def _request(url, *, headers=None, params=None, raw=False):
    try:
        async with aiohttp.ClientSession(
            timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT}
        ) as session:
            async with session.get(url, headers=headers, params=params) as response:
                if raw:
                    return response.status, await response.read()
                if response.status == 204:
                    return response.status, None
                return response.status, await response.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
        print(f"외부 API 요청 실패 ({url.split('?')[0]}): {type(exc).__name__}: {exc}")
        raise StatsError("외부 서비스에 연결하지 못했습니다. 잠시 후 다시 시도해 주세요.") from exc


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
        raise StatsError("마인크래프트 계정 조회에 실패했습니다. 잠시 후 다시 시도해 주세요.")
    return data["id"], data["name"]


async def _hypixel(path, **params):
    key = os.getenv("HYPIXEL_API_KEY", "").strip()
    if not key:
        raise StatsError("Hypixel API 키가 설정되지 않았습니다. `.env`의 `HYPIXEL_API_KEY`를 확인해 주세요.")
    status, data = await _request(HYPIXEL_API_URL + path, headers={"API-Key": key}, params=params)
    if status == 403:
        raise StatsError("Hypixel API 키가 올바르지 않거나 만료되었습니다.")
    if status == 429:
        raise StatsError("Hypixel API 요청 한도를 넘었습니다. 잠시 후 다시 시도해 주세요.")
    if status != 200 or not (data or {}).get("success"):
        raise StatsError("Hypixel 조회에 실패했습니다. 잠시 후 다시 시도해 주세요.")
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


async def hypixel_profile(name):
    async def load():
        uuid, real_name = await minecraft_profile(name)
        player = (await _hypixel("player", uuid=uuid)).get("player")
        if not player:
            raise StatsError(f"`{real_name}` 님은 Hypixel에 접속한 기록이 없습니다.")
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
    """'이름#태그 [지역]' -> (name, tag, region)."""
    text = text.strip()
    region = "kr"
    parts = text.rsplit(maxsplit=1)
    if len(parts) == 2 and parts[1].lower() in VALORANT_REGIONS and "#" in parts[0]:
        text, region = parts[0], parts[1].lower()
    if "#" not in text:
        raise StatsError("Riot ID를 `이름#태그` 형식으로 입력해 주세요. (예: `!V전적 Hide on bush#KR1`)")
    name, tag = (s.strip() for s in text.rsplit("#", 1))
    if not name or not tag or len(name) > 16 or len(tag) > 5:
        raise StatsError("Riot ID 형식이 올바르지 않습니다. (이름 16자, 태그 5자 이하)")
    return name, tag, region


async def _henrik(path):
    key = os.getenv("HENRIK_API_KEY", "").strip()
    if not key:
        raise StatsError("HenrikDev API 키가 설정되지 않았습니다. `.env`의 `HENRIK_API_KEY`를 확인해 주세요.")
    status, data = await _request(HENRIK_API_URL + path, headers={"Authorization": key})
    if status == 404:
        raise StatsError("플레이어를 찾을 수 없습니다. Riot ID와 지역을 확인해 주세요.")
    if status in (401, 403):
        raise StatsError("HenrikDev API 키가 올바르지 않습니다.")
    if status == 429:
        raise StatsError("HenrikDev API 요청 한도를 넘었습니다. 잠시 후 다시 시도해 주세요.")
    if status != 200 or not isinstance(data, dict):
        raise StatsError("발로란트 전적 조회에 실패했습니다. 잠시 후 다시 시도해 주세요.")
    return data.get("data")


def match_summary(match):
    meta = match.get("meta") or {}
    stats = match.get("stats") or {}
    teams = match.get("teams") or {}
    team = (stats.get("team") or "").lower()
    result = "-"
    if team in ("red", "blue"):
        mine, theirs = teams.get(team), teams.get("blue" if team == "red" else "red")
        if isinstance(mine, int) and isinstance(theirs, int):
            result = "승" if mine > theirs else "패" if mine < theirs else "무"
            result += f" {mine}:{theirs}"
    return {
        "result": result,
        "agent": (stats.get("character") or {}).get("name") or "-",
        "map": (meta.get("map") or {}).get("name") or "-",
        "mode": meta.get("mode") or "-",
        "kda": f"{stats.get('kills', 0)}/{stats.get('deaths', 0)}/{stats.get('assists', 0)}",
    }


async def valorant_profile(name, tag, region):
    async def load():
        base = f"{region}/{quote(name, safe='')}/{quote(tag, safe='')}"
        mmr = await _henrik(f"v2/mmr/{base}") or {}
        try:
            matches = await _henrik(f"v1/stored-matches/{base}?size=5") or []
        except StatsError:
            matches = []
        current = mmr.get("current_data") or {}
        highest = mmr.get("highest_rank") or {}
        return {
            "name": mmr.get("name") or name,
            "tag": mmr.get("tag") or tag,
            "region": region,
            "rank": current.get("currenttierpatched") or "Unrated",
            "rr": current.get("ranking_in_tier"),
            "rr_change": current.get("mmr_change_to_last_game"),
            "elo": current.get("elo"),
            "peak": highest.get("patched_tier"),
            "peak_season": highest.get("season"),
            "matches": [match_summary(m) for m in matches[:5] if isinstance(m, dict)],
        }

    return await _cached(("valorant", region, name.lower(), tag.lower()), load)
