import os
import random
import re
import math
import time
import sqlite3
import asyncio
import hashlib
import logging
from collections import deque
import io
import json
import shutil
import tempfile
import ipaddress
import traceback
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
from mcstatus import JavaServer
from dotenv import load_dotenv

import audio_cover
import cards
import dashboard
import dday
import net
import easter_eggs
import nara
import quiz
import shortlinks
import stats
import storage
import textcon

load_dotenv()


def limit_malloc_arenas():
    """Tune glibc's allocator for a 128MB host: two arenas instead of one per thread (MALLOC_ARENA_MAX=2),
    and freed large buffers handed back to the system."""
    try:
        import ctypes
        libc = ctypes.CDLL("libc.so.6")
        libc.mallopt(-8, 2)  # M_ARENA_MAX
        # A fixed threshold stops glibc from raising it after big buffers are freed, which otherwise
        # keeps tens of MB of freed upload/card buffers in the process instead of returning them.
        libc.mallopt(-3, 128 * 1024)  # M_MMAP_THRESHOLD: blocks this big get their own mapping
        libc.mallopt(-1, 256 * 1024)  # M_TRIM_THRESHOLD: give free heap memory back above this
    except (OSError, AttributeError):
        pass  # not glibc (Windows, macOS, musl): nothing to do


limit_malloc_arenas()


def env_int(name, default, minimum):
    """Integer setting from .env; a typo falls back to the default instead of stopping the bot."""
    raw = os.getenv(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        print(f"{name} 값 '{raw}'이(가) 숫자가 아니라서 기본값 {default}을(를) 씁니다.")
        value = default
    return max(minimum, value)


TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
DB_PATH = os.getenv("DATABASE_PATH", "uptime.db")
# Uptime: how often the background check runs and how long its samples are kept. Set in the NKOTE admin
# panel (settings "uptime_interval" / "uptime_days"); these are the values until then.
UPTIME_INTERVAL_DEFAULT = 30
UPTIME_INTERVAL_MIN, UPTIME_INTERVAL_MAX = 10, 3600
UPTIME_DAYS_CHOICES = (7, 14, 30)
PRUNE_INTERVAL_SECONDS = 600
MAX_SERVERS_PER_GUILD = 10
MAX_NAME_LENGTH = 30
NO_SERVER_MESSAGE = "등록된 서버가 없습니다. `!주소추가 <주소> [이름]`으로 추가해 주세요."
SERVER_ONLY_MESSAGE = "디스코드 서버 채널에서만 사용할 수 있습니다."
# Last resort when the direct ping fails; only domain names are ever sent out, never IPs.
EXTERNAL_FALLBACK = os.getenv("EXTERNAL_FALLBACK", "true").strip().lower() not in ("0", "false", "no", "off")
EXTERNAL_API_URL = "https://api.mcsrvstat.us/3/"
EXTERNAL_TIMEOUT_SECONDS = 8
EXTERNAL_COOLDOWN_SECONDS = 60
SLOW_NOTICE_SECONDS = 2
PROGRESS_EDIT_INTERVAL = 0.8  # seconds between progress message edits, to stay clear of rate limits
STATS_COOLDOWN_SECONDS = 5
HYPIXEL_GAMES = {
    "bw": "bedwars", "bedwars": "bedwars", "베드워즈": "bedwars", "베워": "bedwars",
    "sw": "skywars", "skywars": "skywars", "스카이워즈": "skywars", "스카": "skywars",
    "duels": "duels", "duel": "duels", "듀얼": "duels",
}
# Progress checklist steps; keys match the steps stats.py reports, plus "card" for rendering.
HYPIXEL_STEPS = [
    ("account", "마인크래프트 계정 확인하는 중"),
    ("player", "하이픽셀 전적 불러오는 중"),
    ("extra", "길드와 스킨 불러오는 중"),
    ("card", "카드 만드는 중"),
]
VALORANT_STEPS = [
    ("account", "계정 찾는 중"),
    ("data", "랭크와 최근 경기 불러오는 중"),
    ("icons", "랭크·요원 아이콘 불러오는 중"),
    ("card", "카드 만드는 중"),
]
external_cache = {}
last_prune = 0.0

OWNER_IDS = {int(i) for i in re.findall(r"\d+", os.getenv("BOT_OWNER_IDS", ""))}

intents = discord.Intents.default()
intents.message_content = True
# Channel permissions the bot needs, with the names Discord shows in the channel settings.
PERMISSION_NAMES = {
    "send_messages": "메시지 보내기", "embed_links": "링크 첨부", "attach_files": "파일 첨부",
    "read_message_history": "메시지 기록 보기", "add_reactions": "반응 추가하기", "manage_webhooks": "웹후크 관리하기",
}
# Nearly every answer is an embed or a picture, so ! commands need these three to work at all.
CORE_PERMISSIONS = ("send_messages", "embed_links", "attach_files")


def missing_permissions(channel, names=CORE_PERMISSIONS):
    """Which of these permissions the bot lacks in this channel (none in DMs)."""
    guild = getattr(channel, "guild", None)
    if guild is None or not hasattr(channel, "permissions_for"):
        return []
    perms = channel.permissions_for(guild.me)
    missing = []
    for name in names:
        if name == "send_messages" and isinstance(channel, discord.Thread):
            ok = perms.send_messages_in_threads
        else:
            ok = getattr(perms, name)
        if not ok:
            missing.append(name)
    return missing


def can_reply_in(channel):
    """Replies quote the command message, which Discord allows only with 메시지 기록 보기."""
    return not missing_permissions(channel, ("read_message_history",))


async def reply_to(message, content=None, **kwargs):
    """message.reply that still answers (as a plain message) where the bot can't read the history."""
    if can_reply_in(message.channel):
        return await message.reply(content, **kwargs)
    kwargs.pop("mention_author", None)
    return await message.channel.send(content, **kwargs)


class BotMissingPermissions(commands.CheckFailure):
    def __init__(self, missing):
        self.missing = missing
        super().__init__("봇 권한 부족: " + ", ".join(missing))


class MacoContext(commands.Context):
    async def reply(self, content=None, **kwargs):
        """Reply to the command message; if it was deleted meanwhile (stats take seconds), the answer still
        arrives as a plain message instead of failing with an error. Without 메시지 기록 보기 it is a plain
        message from the start."""
        if can_reply_in(self.channel):
            kwargs.setdefault("reference", self.message.to_reference(fail_if_not_exists=False))
        else:
            kwargs.pop("reference", None)
            kwargs.pop("mention_author", None)
        return await self.send(content, **kwargs)


class MacoBot(commands.Bot):
    async def get_context(self, origin, *, cls=MacoContext):
        return await super().get_context(origin, cls=cls)


# Replies echo user input (server names, Riot IDs); never let that ping @everyone, roles or users.
bot = MacoBot(
    command_prefix="!",
    intents=intents,
    help_command=None,
    case_insensitive=True,
    max_messages=None,  # the bot never looks up old messages; the default keeps 1,000 in memory
    allowed_mentions=discord.AllowedMentions.none(),
    # Owner-only commands (£servers). Empty means "the account that owns the app in the Developer Portal".
    owner_ids=OWNER_IDS or None,
    # The status line under the bot's name tells people how to find the commands.
    activity=discord.Game("!도움말 · /도움말로 명령어 보기"),
)


# Spam guard for every ! command: a person may use RATE_COUNT commands in RATE_SECONDS (game answers too).
RATE_COUNT, RATE_SECONDS = 5, 5
_command_rate = commands.CooldownMapping.from_cooldown(RATE_COUNT, RATE_SECONDS, commands.BucketType.user)
RATE_MESSAGE = "⏳ 명령어 도배 방지 - 잠시 기다려주세요"
_rate_warned = {}  # user id -> when they were last told to slow down (told once per burst)


class TooFast(commands.CheckFailure):
    def __init__(self, retry_after):
        self.retry_after = retry_after
        super().__init__("명령어를 너무 빠르게 사용함")


@bot.check
async def bot_can_answer(ctx):
    """Stop a ! command before it starts when the bot couldn't post its answer here anyway, or when the
    person is sending commands faster than RATE_COUNT in RATE_SECONDS."""
    retry_after = _command_rate.update_rate_limit(ctx.message)
    if retry_after:
        raise TooFast(retry_after)
    missing = missing_permissions(ctx.channel)
    if ctx.command and ctx.command.name == "도움말" and "send_messages" not in missing:
        return True  # the command list also works as plain text, so reviewers always see it
    if missing:
        raise BotMissingPermissions(missing)
    return True


async def tell_too_fast(ctx, error):
    """One short notice per burst; further commands in the same burst are ignored silently."""
    now = time.time()
    if now - _rate_warned.get(ctx.author.id, 0) < RATE_SECONDS:
        return
    if len(_rate_warned) >= 500:
        for user_id in [u for u, t in _rate_warned.items() if now - t >= RATE_SECONDS]:
            del _rate_warned[user_id]
    _rate_warned[ctx.author.id] = now
    # Only the person should see it: a ! command can't answer privately, so it goes by DM. With DMs closed
    # it is posted in the channel and removed again after a few seconds.
    try:
        await ctx.author.send(RATE_MESSAGE)
    except (discord.HTTPException, AttributeError):
        try:
            await ctx.channel.send(f"{ctx.author.mention} {RATE_MESSAGE}", delete_after=RATE_SECONDS)
        except discord.HTTPException:
            pass

# Per-server activity for £servers: counts only (no content, no authors), by hour. Kept in the DB so a
# restart doesn't lose them; anything older than TRAFFIC_KEEP_HOURS (a week) is deleted.
# Messages are counted in memory (kind -> guild id -> {hour: count}) and written once a minute, so a busy
# server doesn't mean a DB write per message.
TRAFFIC_KEEP_HOURS = 7 * 24
traffic = {"messages": {}, "commands": {}}  # counts not yet written to the DB


def record_traffic(kind, guild_id):
    hour = int(time.time() // 3600)
    buckets = traffic[kind].setdefault(guild_id, {})
    buckets[hour] = buckets.get(hour, 0) + 1


def flush_traffic():
    """Write the pending counts and delete hours older than a week."""
    rows = [(kind, gid, hour, n) for kind, guilds in traffic.items() for gid, hours in guilds.items()
            for hour, n in hours.items()]
    for guilds in traffic.values():
        guilds.clear()
    oldest = int(time.time() // 3600) - TRAFFIC_KEEP_HOURS
    with connect_db() as conn:
        if rows:
            conn.executemany("INSERT INTO traffic(kind, guild_id, hour, count) VALUES (?, ?, ?, ?) "
                             "ON CONFLICT(kind, guild_id, hour) DO UPDATE SET count = count + excluded.count", rows)
        conn.execute("DELETE FROM traffic WHERE hour <= ?", (oldest,))
        conn.commit()


def traffic_totals(hours):
    """{(kind, guild id): count} over the last `hours` hours, every server at once (DB + not yet written)."""
    since = int(time.time() // 3600) - hours
    with connect_db() as conn:
        totals = {(r[0], r[1]): r[2] for r in conn.execute(
            "SELECT kind, guild_id, SUM(count) FROM traffic WHERE hour > ? GROUP BY kind, guild_id", (since,))}
    for kind, guilds in traffic.items():
        for gid, by_hour in guilds.items():
            add = sum(n for h, n in by_hour.items() if h > since)
            if add:
                totals[(kind, gid)] = totals.get((kind, gid), 0) + add
    return totals


# ---------------------------------------------------------------- admin error DMs

ADMIN_NOTIFY_COOLDOWN = 600  # the same error is DMed at most once per 10 minutes
_admin_notified = {}  # (title, detail) -> [last sent time, times suppressed since]
_background_tasks = set()  # keep references so pending DM tasks aren't garbage-collected


def notify_admin(title, detail, where="", trace=""):
    """DM the bot admins about an error in the background. Never raises, never delays the caller."""
    now = time.time()
    key = (title, detail)
    entry = _admin_notified.get(key)
    if entry and now - entry[0] < ADMIN_NOTIFY_COOLDOWN:
        entry[1] += 1
        return
    repeats = entry[1] if entry else 0
    if len(_admin_notified) > 200:
        for old in [k for k, (sent, _) in _admin_notified.items() if now - sent >= ADMIN_NOTIFY_COOLDOWN]:
            del _admin_notified[old]
    _admin_notified[key] = [now, 0]
    task = asyncio.get_running_loop().create_task(_send_admin_dm(title, detail, where, trace, repeats))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def notify_service_error(exc, where=""):
    if isinstance(exc, stats.ServiceError):
        notify_admin("외부 API 문제", exc.detail, where)


async def admin_user_ids():
    """BOT_OWNER_IDS if set, otherwise the app's owner (or team admins/developers) from the Developer Portal."""
    if not (bot.owner_id or bot.owner_ids):
        await bot.is_owner(discord.Object(id=0))  # makes discord.py look up and remember the owner(s)
    return set(bot.owner_ids) if bot.owner_ids else {bot.owner_id}


async def _send_admin_dm(title, detail, where, trace, repeats):
    await bot.wait_until_ready()
    description = detail[:1500]
    if trace:
        description += "\n```\n" + trace[-2000:].replace("```", "'''") + "\n```"
    embed = discord.Embed(
        title=f"⚠️ {title}", description=description, color=discord.Color.red(),
        timestamp=datetime.now(timezone.utc),
    )
    if where:
        embed.add_field(name="위치", value=where[:1024], inline=False)
    if repeats:
        embed.set_footer(text=f"지난 알림 이후 같은 오류가 {repeats}번 더 있었습니다")
    try:
        ids = await admin_user_ids()
    except discord.HTTPException as exc:
        print(f"관리자 확인 실패: {type(exc).__name__}: {exc}")
        return
    for user_id in ids:
        try:
            user = bot.get_user(user_id) or await bot.fetch_user(user_id)
            await user.send(embed=embed)
        except discord.HTTPException as exc:
            print(f"관리자 DM 실패 ({user_id}): {type(exc).__name__}: {exc}")


def describe_ctx(ctx):
    place = f"{ctx.guild.name} ({ctx.guild.id})" if ctx.guild else "DM"
    return f"명령어: {ctx.message.content[:200]}\n서버: {place}\n사용자: {ctx.author} ({ctx.author.id})"


def describe_interaction(interaction, what):
    place = f"{interaction.guild.name} ({interaction.guild.id})" if interaction.guild else "DM"
    return f"{what}\n서버: {place}\n사용자: {interaction.user} ({interaction.user.id})"


@contextmanager
def connect_db():
    """`with connect_db() as conn:` commits (or rolls back on error) and then closes the connection.
    A plain sqlite3 connection used in a with-block is never closed, and on this host each one left
    behind tens of KB: about 29MB per 500 queries, which pushed the bot towards its 128MB limit."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db():
    with connect_db() as conn:
        # Several named servers per Discord server (guild), visible to all members.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS servers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                address TEXT NOT NULL,
                registered_by INTEGER NOT NULL,
                registered_at REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                checked_at REAL NOT NULL,
                online INTEGER NOT NULL,
                latency_ms REAL
            )
        """)
        columns = [row["name"] for row in conn.execute("PRAGMA table_info(samples)")]
        if "server_id" not in columns:
            conn.execute("ALTER TABLE samples ADD COLUMN server_id INTEGER")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_samples_server_time ON samples(server_id, checked_at)"
        )
        # For deleting old samples by time; without it every cleanup read the whole table.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_samples_time ON samples(checked_at)")

        # Move the old one-address-per-guild table into the new one, keeping its history.
        legacy = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'shared_servers'"
        ).fetchone()
        if legacy:
            for row in conn.execute(
                "SELECT guild_id, address, registered_by, registered_at FROM shared_servers"
            ).fetchall():
                cursor = conn.execute(
                    "INSERT INTO servers(guild_id, name, address, registered_by, registered_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (row["guild_id"], row["address"], row["address"],
                     row["registered_by"], row["registered_at"]),
                )
                conn.execute(
                    "UPDATE samples SET server_id = ? WHERE guild_id = ? AND server_id IS NULL",
                    (cursor.lastrowid, row["guild_id"]),
                )
            conn.execute("DROP TABLE shared_servers")
        conn.execute("DELETE FROM samples WHERE server_id IS NULL")
        # Files uploaded with !올리기 (kept in the S3-compatible storage). status: 'pending' while a browser upload is unfinished.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER,
                channel_id INTEGER,
                name TEXT NOT NULL,
                key TEXT NOT NULL,
                url TEXT NOT NULL,
                content_type TEXT NOT NULL,
                size INTEGER NOT NULL,
                uploaded_by INTEGER NOT NULL,
                uploaded_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                status TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_files_guild ON files(guild_id, status)")
        # A song's MP4 player (its sound + a still picture), so Discord can play it in the chat.
        columns = [row["name"] for row in conn.execute("PRAGMA table_info(files)")]
        if "player_key" not in columns:
            conn.execute("ALTER TABLE files ADD COLUMN player_key TEXT")
        # A song posted as a plain attachment (Discord's own audio bar): that message, deleted with the file.
        if "bar_channel_id" not in columns:
            conn.execute("ALTER TABLE files ADD COLUMN bar_channel_id INTEGER")
            conn.execute("ALTER TABLE files ADD COLUMN bar_message_id INTEGER")
        # Virus check (VirusTotal hash lookup): the file's SHA-256 and, for a file it flagged, the result.
        # status 'blocked': the file was flagged and moved out of reach, kept as evidence for the admin.
        if "link_at" not in columns:  # when the file's short link was last pointed at a fresh storage link
            conn.execute("ALTER TABLE files ADD COLUMN link_at REAL")
        if "sha256" not in columns:
            conn.execute("ALTER TABLE files ADD COLUMN sha256 TEXT")
            conn.execute("ALTER TABLE files ADD COLUMN scan TEXT")
        # People who may not upload: until NULL = for good (영구), else until that time (시간 차단).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS upload_bans (
                user_id INTEGER PRIMARY KEY,
                until REAL,
                reason TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL
            )
        """)
        # D-Days: per server, visible to everyone, edited only by the person who made them.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ddays (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                target REAL NOT NULL,
                has_time INTEGER NOT NULL DEFAULT 0,
                template TEXT,
                alert_time TEXT,
                channel_id INTEGER,
                after TEXT NOT NULL DEFAULT 'stop',
                count_up INTEGER NOT NULL DEFAULT 0,
                created_by INTEGER NOT NULL,
                created_at REAL NOT NULL,
                next_alert REAL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ddays_guild ON ddays(guild_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ddays_alert ON ddays(next_alert)")
        if "celebrate" not in [row["name"] for row in conn.execute("PRAGMA table_info(ddays)")]:
            conn.execute("ALTER TABLE ddays ADD COLUMN celebrate TEXT")  # the message on the day itself
        # 나라 맞히기: one game per channel (kept over restarts) and each server's winners.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS nara_games (
                channel_id INTEGER PRIMARY KEY,
                guild_id INTEGER,
                answer TEXT NOT NULL,
                guesses TEXT NOT NULL DEFAULT '[]',
                started_by INTEGER NOT NULL,
                started_at REAL NOT NULL,
                board_message_id INTEGER
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS nara_scores (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                wins INTEGER NOT NULL DEFAULT 0,
                best INTEGER,
                PRIMARY KEY (guild_id, user_id)
            )
        """)
        if "level" not in [row["name"] for row in conn.execute("PRAGMA table_info(nara_games)")]:
            conn.execute("ALTER TABLE nara_games ADD COLUMN level TEXT NOT NULL DEFAULT 'normal'")
        if "hard_wins" not in [row["name"] for row in conn.execute("PRAGMA table_info(nara_scores)")]:
            conn.execute("ALTER TABLE nara_scores ADD COLUMN hard_wins INTEGER NOT NULL DEFAULT 0")
        if "setter" not in [row["name"] for row in conn.execute("PRAGMA table_info(nara_games)")]:
            conn.execute("ALTER TABLE nara_games ADD COLUMN setter INTEGER")  # who chose the answer (!나라출제)
        if "easy_wins" not in [row["name"] for row in conn.execute("PRAGMA table_info(nara_scores)")]:
            conn.execute("ALTER TABLE nara_scores ADD COLUMN easy_wins INTEGER NOT NULL DEFAULT 0")
        # 국기 맞히기 / 수도 맞히기: one quiz of each per channel and how many each person has got, per difficulty.
        for table in ("flag", "capital", "map", "anime", "song"):
            conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {table}_games (
                    channel_id INTEGER PRIMARY KEY,
                    guild_id INTEGER,
                    level TEXT NOT NULL,
                    answer TEXT NOT NULL,
                    number INTEGER NOT NULL,
                    total INTEGER NOT NULL,
                    recent TEXT NOT NULL DEFAULT '[]',
                    scores TEXT NOT NULL DEFAULT '{{}}',
                    hinted INTEGER NOT NULL DEFAULT 0,
                    started_by INTEGER NOT NULL,
                    started_at REAL NOT NULL
                )
            """)
            conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {table}_scores (
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    easy INTEGER NOT NULL DEFAULT 0,
                    normal INTEGER NOT NULL DEFAULT 0,
                    hard INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (guild_id, user_id)
                )
            """)
        # 노래 맞히기: the genre a quiz was started with, and the songs each server added (!노래추가).
        song_columns = [row["name"] for row in conn.execute("PRAGMA table_info(song_games)")]
        if "genre" not in song_columns:
            conn.execute("ALTER TABLE song_games ADD COLUMN genre TEXT")
        if "upcoming" not in song_columns:  # the next question's song, chosen early so it can be prepared
            conn.execute("ALTER TABLE song_games ADD COLUMN upcoming TEXT")
        conn.execute("CREATE TABLE IF NOT EXISTS song_apple (id TEXT PRIMARY KEY, apple TEXT NOT NULL)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS song_custom (
                guild_id INTEGER NOT NULL,
                id TEXT NOT NULL,
                title TEXT NOT NULL,
                artist TEXT NOT NULL,
                names TEXT NOT NULL,
                cover TEXT,
                added_by INTEGER NOT NULL,
                added_at REAL NOT NULL,
                PRIMARY KEY (guild_id, id)
            )
        """)
        # 애니캐 맞히기: the works each server chose on the 작품 고르기 page (no row: all works).
        conn.execute("CREATE TABLE IF NOT EXISTS anime_works (guild_id INTEGER PRIMARY KEY, works TEXT NOT NULL)")
        # Small key/value settings changed with owner commands (e.g. £DB 기간).
        conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        # XP 배팅: one row per bet game in progress (who bet how much), and the XP each settled bet moved
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bet_rounds (
                game TEXT NOT NULL,
                channel_id INTEGER NOT NULL,
                guild_id INTEGER NOT NULL,
                open_until REAL NOT NULL,
                bets TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY (game, channel_id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS xp_bets (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                delta REAL NOT NULL,
                game TEXT NOT NULL,
                at REAL NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_xp_bets_user ON xp_bets(guild_id, user_id)")
        # £servers activity: message / command counts per server per hour, a week at most (no content, no authors)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS traffic (
                kind TEXT NOT NULL,
                guild_id INTEGER NOT NULL,
                hour INTEGER NOT NULL,
                count INTEGER NOT NULL,
                PRIMARY KEY (kind, guild_id, hour)
            )
        """)
        conn.commit()


def get_setting(key, default=None):
    with connect_db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    with connect_db() as conn:
        conn.execute("INSERT INTO settings(key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))
        conn.commit()
    _switch_cache.clear()


def delete_setting(key):
    with connect_db() as conn:
        conn.execute("DELETE FROM settings WHERE key = ?", (key,))
        conn.commit()
    _switch_cache.clear()


# On/off switches the NKOTE admin panel sets, kept as settings rows ("game_off:anime:all",
# "game_off:song:<server id>", "upload_block:all", "upload_block:<server id>"). Read on every command, so
# they are cached in memory and the cache is dropped whenever a setting changes.
GAME_SWITCHES = {"anime": "애니캐 맞히기", "song": "노래 맞히기"}
_switch_cache = {}


def switch_keys():
    if "keys" not in _switch_cache:
        with connect_db() as conn:
            _switch_cache["keys"] = {r["key"] for r in conn.execute(
                "SELECT key FROM settings WHERE key LIKE 'game_off:%' OR key LIKE 'upload_block:%' OR key LIKE 'upload_allow:%' "
                "OR key LIKE 'uptime_off:%'")}
    return _switch_cache["keys"]


def set_switch(key, on):
    if on:
        set_setting(key, "1")
    else:
        delete_setting(key)


def game_enabled(game, guild_id=None):
    keys = switch_keys()
    return f"game_off:{game}:all" not in keys and (not guild_id or f"game_off:{game}:{guild_id}" not in keys)


def uptime_interval():
    value = get_setting("uptime_interval")
    return int(value) if value and value.isdigit() else UPTIME_INTERVAL_DEFAULT


def history_days():
    value = get_setting("uptime_days")
    return int(value) if value and value.isdigit() and int(value) in UPTIME_DAYS_CHOICES else 30


def uptime_background_on():
    """False when the admin turned the automatic checks off: !업타임 then only checks on the spot."""
    return "uptime_off:all" not in switch_keys()


def uptime_enabled(guild_id):
    """False when the admin switched the uptime feature off for this server (commands and buttons go)."""
    return not guild_id or f"uptime_off:{guild_id}" not in switch_keys()


UPTIME_OFF_MESSAGE = "지금은 이 서버에서 서버 업타임 기능을 쓸 수 없습니다. (관리자가 꺼 두었습니다)"


def game_off_message(game, guild):
    """None while the game may be used here, else what to tell the person."""
    if game not in GAME_SWITCHES or game_enabled(game, guild.id if guild else None):
        return None
    return f"지금은 {GAME_SWITCHES[game]}을(를) 쓸 수 없습니다. (관리자가 꺼 두었습니다)"


def validate_address(raw: str) -> str:
    address = raw.strip()
    if not address or len(address) > 253:
        raise ValueError("서버 주소를 확인해 주세요.")
    if any(ch in address for ch in ("://", "/", "\\", "@", " ")):
        raise ValueError(
            "도메인 또는 IP와 선택적인 포트만 입력해 주세요. "
            "예: `play.example.com:25565`"
        )

    host = address
    port = None
    if address.startswith("["):
        match = re.fullmatch(r"\[([0-9a-fA-F:]+)\](?::(\d{1,5}))?", address)
        if not match:
            raise ValueError("IPv6 주소 형식이 올바르지 않습니다. 예: `[2001:db8::1]:25565`")
        host, port = match.group(1), match.group(2)
    elif address.count(":") == 1:
        host, possible_port = address.rsplit(":", 1)
        if not possible_port.isdigit():
            raise ValueError("포트 번호는 숫자로 입력해 주세요.")
        port = possible_port
    elif address.count(":") > 1:
        raise ValueError("IPv6 주소는 대괄호로 감싸 주세요. 예: `[2001:db8::1]:25565`")

    if port is not None and not (1 <= int(port) <= 65535):
        raise ValueError("포트 번호는 1~65535 범위여야 합니다.")
    if not host:
        raise ValueError("서버 주소가 비어 있습니다.")

    try:
        ip = ipaddress.ip_address(host.strip("[]"))
        if not ip.is_global:
            raise ValueError("공인 IP 주소만 등록할 수 있습니다.")
    except ValueError as exc:
        if str(exc) == "공인 IP 주소만 등록할 수 있습니다.":
            raise
        hostname_pattern = (
            r"(?=.{1,253}$)(?:[A-Za-z0-9]"
            r"(?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)"
            r"(?:\.(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?))*\.?"
        )
        if not re.fullmatch(hostname_pattern, host):
            raise ValueError("올바른 도메인 또는 공인 IP 주소를 입력해 주세요.")
        # Single-label names (localhost, LAN hostnames) would make the bot probe its own network.
        if "." not in host.rstrip("."):
            raise ValueError("도메인 전체를 입력해 주세요. 예: `play.example.com`")

    return address


def address_host(address: str) -> str:
    if address.startswith("["):
        return address[1:address.index("]")]
    if address.count(":") == 1:
        return address.rsplit(":", 1)[0]
    return address


def can_use_external(address: str) -> bool:
    host = address_host(address).strip(".")
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        pass
    return "." in host


async def query_external(address: str):
    now = time.time()
    cached = external_cache.get(address.lower())
    if cached and now - cached[0] < EXTERNAL_COOLDOWN_SECONDS:
        return cached[1]

    result = (False, None, None, None)
    try:
        timeout = aiohttp.ClientTimeout(total=EXTERNAL_TIMEOUT_SECONDS)
        async with net.session().get(EXTERNAL_API_URL + quote(address, safe=":"), timeout=timeout) as response:
            response.raise_for_status()
            data = await response.json()
        players = data.get("players") or {}
        result = (bool(data.get("online")), None, players.get("online"), players.get("max"))
    except Exception as exc:
        print(f"외부 조회 실패 ({address}): {type(exc).__name__}: {exc}")
        # Keyed by error kind, not address, so an mcsrvstat outage is one DM rather than one per server.
        kind = f"HTTP {exc.status}" if isinstance(exc, aiohttp.ClientResponseError) else type(exc).__name__
        notify_admin("외부 상태 조회(mcsrvstat) 실패", kind, f"주소: {address}\n{type(exc).__name__}: {exc}")
    if len(external_cache) >= 300:
        for key in [k for k, (at, _) in external_cache.items() if now - at >= EXTERNAL_COOLDOWN_SECONDS]:
            del external_cache[key]
    external_cache[address.lower()] = (now, result)
    return result


INSIDE_ADDRESS_MESSAGE = ("이 주소는 내부 네트워크 주소(사설 IP, localhost 등)를 가리켜서 확인할 수 없습니다. "
                          "인터넷에서 접속할 수 있는 주소(공인 IP나 그 IP를 가리키는 도메인)를 입력해 주세요.")


# The background check asks every 10 seconds; one DNS answer is reused for a few minutes.
INSIDE_CACHE_SECONDS = 300
_inside_cache = {}  # host -> (when looked up, points inside)


class InsideAddress(Exception):
    """The server name resolves to a private/local IP: checking it would probe the bot host's own network."""


async def points_inside(host):
    """True if the host is, or resolves to, an address that isn't on the public internet. A domain
    pointing at 127.0.0.1 or 192.168.x.x would otherwise let anyone probe the bot host's own network.
    Lookup failures count as outside: the connection then simply fails."""
    host = host.strip("[]").rstrip(".")
    try:
        return not ipaddress.ip_address(host).is_global
    except ValueError:
        pass
    cached = _inside_cache.get(host.lower())
    if cached and time.monotonic() - cached[0] < INSIDE_CACHE_SECONDS:
        return cached[1]
    try:
        infos = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(host, None), timeout=5)
    except (OSError, asyncio.TimeoutError, UnicodeError):
        return False
    inside = False
    for info in infos:
        try:
            if not ipaddress.ip_address(info[4][0].split("%")[0]).is_global:
                inside = True
                break
        except ValueError:
            continue
    if len(_inside_cache) >= 500:
        _inside_cache.clear()
    _inside_cache[host.lower()] = (time.monotonic(), inside)
    return inside


async def query_server(address: str, quiet=False, strict=False):
    """Returns (online, latency_ms, players_online, players_max, via_external). quiet: no log line on failure
    (the background check logs only when a server goes down). An internal address counts as offline, or
    raises InsideAddress when strict (adding a server)."""
    try:
        # async_lookup: the plain lookup() resolves SRV records synchronously and freezes the whole bot.
        server = await asyncio.wait_for(JavaServer.async_lookup(address), timeout=5)
        # Checked on every query, not only when added: the domain's IP can be changed afterwards.
        if await points_inside(server.address.host) or await points_inside(address_host(address)):
            raise InsideAddress(address)
        status = await asyncio.wait_for(server.async_status(), timeout=5)
        latency = float(status.latency) if status.latency is not None else None
        return True, latency, status.players.online, status.players.max, False
    except InsideAddress:
        if strict:
            raise
        if not quiet:
            print(f"내부 주소라서 확인하지 않음: {address}")
        return False, None, None, None, False
    except Exception as exc:
        if not quiet:
            print(f"상태 확인 실패 ({address}): {type(exc).__name__}: {exc}")
    if EXTERNAL_FALLBACK and can_use_external(address):
        return (*await query_external(address), True)
    return False, None, None, None, False


def guild_servers(guild_id: int):
    with connect_db() as conn:
        return conn.execute(
            "SELECT id, name, address FROM servers WHERE guild_id = ? ORDER BY id",
            (guild_id,),
        ).fetchall()


def find_server(guild_id: int, key: str):
    key = key.strip().lower()
    servers = guild_servers(guild_id)
    for server in servers:
        if server["name"].lower() == key:
            return server
    for server in servers:
        if server["address"].lower() == key:
            return server
    return None


def all_servers():
    with connect_db() as conn:
        return conn.execute("SELECT id, guild_id, address FROM servers").fetchall()


def save_samples(samples):
    """[(server id, guild id, online, latency)] in one transaction (the background check saves all at once)."""
    global last_prune
    now = time.time()
    with connect_db() as conn:
        conn.executemany(
            "INSERT INTO samples(server_id, guild_id, checked_at, online, latency_ms) "
            "VALUES (?, ?, ?, ?, ?)",
            [(server_id, guild_id, now, int(online), latency) for server_id, guild_id, online, latency in samples],
        )
        if now - last_prune >= PRUNE_INTERVAL_SECONDS:
            conn.execute(
                "DELETE FROM samples WHERE checked_at < ?",
                (now - history_days() * 24 * 60 * 60,),
            )
            last_prune = now
        conn.commit()


def uptime_stats_many(server_ids, now):
    """{server id: {"day": (percent, samples, avg latency), "week": (percent, samples, None)}} in one query.
    A week of checks is ~20,000 rows a server, so !업타임 runs this in a worker thread (asyncio.to_thread):
    the bot keeps answering everyone else meanwhile."""
    out = {sid: {"day": (None, 0, None), "week": (None, 0, None)} for sid in server_ids}
    if not server_ids:
        return out
    day, week = now - 86400, now - 7 * 86400
    with connect_db() as conn:
        rows = conn.execute(
            "SELECT server_id, SUM(checked_at >= ?), SUM(CASE WHEN checked_at >= ? THEN online ELSE 0 END), "
            "COUNT(*), SUM(online), "
            "AVG(CASE WHEN checked_at >= ? AND online AND latency_ms IS NOT NULL THEN latency_ms END) "
            f"FROM samples WHERE server_id IN ({','.join('?' * len(server_ids))}) AND checked_at >= ? "
            "GROUP BY server_id", (day, day, day, *server_ids, week)).fetchall()
    for sid, day_n, day_up, week_n, week_up, latency in rows:
        out[sid] = {"day": (day_up / day_n * 100 if day_n else None, day_n or 0, latency),
                    "week": (week_up / week_n * 100 if week_n else None, week_n or 0, None)}
    return out


def uptime_stats(server_id: int, since: float):
    """(percentage, samples, average latency) since a time. Counted by SQLite: a week of checks every 10
    seconds is ~60,000 rows per server, which used to be loaded into Python on every !업타임."""
    with connect_db() as conn:
        count, online_count, average_latency = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(online), 0), "
            "AVG(CASE WHEN online AND latency_ms IS NOT NULL THEN latency_ms END) "
            "FROM samples WHERE server_id = ? AND checked_at >= ?",
            (server_id, since),
        ).fetchone()
    if not count:
        return None, 0, None
    return online_count / count * 100, count, average_latency


def percent_text(percentage):
    return f"{percentage:.2f}%" if percentage is not None else "수집 중"


# ---------------------------------------------------------------- connection health
# "Shard ID None has successfully RESUMED session" means the connection to Discord dropped and was picked
# up again. Usually the network, or the bot was too busy to answer Discord's heartbeat in time (0.25 vCPU).
# The lines are counted instead of printed one by one, and the event loop's delays are measured so the
# admin panel can tell which it was.
_resumes = deque(maxlen=500)  # when each resume / fresh reconnect happened
_loop_lags = deque(maxlen=500)  # (when, seconds) for each time the event loop was late by over a second
_health_hour = {"resumes": 0, "lag": 0.0}


# Per-feature health for the public status page (nnaco.kro.kr/status): each outside call a feature makes
# is marked ok or failed; the last 30 minutes decide 정상 / 일부 오류 / 장애.
HEALTH_WINDOW_SECONDS = 30 * 60
_health = {}  # service -> deque of (when, ok, failure reason)


def mark_health(service, ok, reason=None):
    """reason: a short cause for a failure (error kind, status code), shown only in the admin panel.
    Never anything about servers or people, never addresses or keys."""
    entry = (time.time(), bool(ok), None if ok else health_reason_text(reason))
    _health.setdefault(service, deque(maxlen=60)).append(entry)


def health_reason_text(reason):
    if isinstance(reason, BaseException):
        reason = f"{type(reason).__name__}: {reason}" if str(reason) else type(reason).__name__
    text = re.sub(r"https?://\S+", "(주소)", str(reason or "알 수 없는 오류"))
    text = re.sub(r"\b\d{1,3}(\.\d{1,3}){3}\b", "(IP)", text)  # never an IP address
    text = re.sub(r"[A-Za-z0-9_\-]{32,}", "(…)", text)  # nor anything that could be a key or token
    return text[:120]


def health_last_reason(service):
    now = time.time()
    for at, ok, reason in reversed(_health.get(service, ())):
        if now - at < HEALTH_WINDOW_SECONDS and not ok:
            return reason
    return None


def health_level(service):
    """ok / warn / down from the recent marks (no recent use counts as ok)."""
    now = time.time()
    marks = [ok for at, ok, _ in _health.get(service, ()) if now - at < HEALTH_WINDOW_SECONDS]
    fails = marks.count(False)
    if not fails:
        return "ok"
    if fails >= 3 and fails / len(marks) >= 0.5:
        return "down"
    return "warn"


def status_services():
    """What the status page shows for each feature: ok / warn / down / off (switched off or not set up)."""
    return {
        # (the first beat can go out a moment before on_ready starts the check loop)
        "uptime": ("ok" if monitor_servers.is_running() or not uptime_background_on()
                   or time.time() - STARTED_AT < 180 else "down"),
        "hypixel": health_level("hypixel") if os.getenv("HYPIXEL_API_KEY", "").strip() else "off",
        "valorant": health_level("valorant") if os.getenv("HENRIK_API_KEY", "").strip() else "off",
        "song": health_level("song") if game_enabled("song") else "off",
        "anime": health_level("anime") if game_enabled("anime") else "off",
        "files": (("off" if "upload_block:all" in switch_keys() else health_level("files"))
                  if storage.configured() else "off"),
        "links": health_level("links") if shortlinks.configured() else "off",
        "ranking": health_level("ranking"),
    }


def status_reasons(services):
    """The last failure cause of each feature that isn't fine right now (for the admin panel only)."""
    reasons = {}
    for name, level in services.items():
        if level in ("warn", "down"):
            reasons[name] = ("자동 확인 반복이 멈춤" if name == "uptime" else health_last_reason(name)) or "원인 기록 없음"
    return reasons


_resume_count = [0]  # resumes since start, so NKOTE can tell a dropped connection from a restart


STATUS_HEARTBEAT_SECONDS = 300
STATUS_PAGE_URL = os.getenv("STATUS_PAGE_URL", "https://nnaco.kro.kr/status").strip()
SUPPORT_SERVER_URL = os.getenv("SUPPORT_SERVER_URL", "https://discord.gg/JRmaf2CV5W").strip()  # 마코 서포트 센터


async def status_heartbeat():
    """Every 5 minutes: tell NKOTE the bot is alive, with each feature's state (no server names or users)."""
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            latency = bot.latency * 1000
            services = status_services()
            await dashboard.status({
                "guilds": len(bot.guilds), "latencyMs": round(latency) if latency == latency else None,
                "services": services, "reasons": status_reasons(services),
                "startedAt": int(STARTED_AT * 1000), "resumes": _resume_count[0],
            })
            mark_health("links", True)
        except dashboard.NotBound:
            pass
        except Exception as exc:
            mark_health("links", False, exc)
            print(f"상태 페이지 신호 실패: {type(exc).__name__}: {exc}")
        await asyncio.sleep(STATUS_HEARTBEAT_SECONDS)


class _ResumeLogFilter(logging.Filter):
    def filter(self, record):
        return "RESUMED session" not in record.getMessage()


logging.getLogger("discord.gateway").addFilter(_ResumeLogFilter())


@bot.listen("on_resumed")
async def count_resume():
    _resumes.append(time.time())
    _resume_count[0] += 1
    _health_hour["resumes"] += 1


def connection_health():
    now = time.time()
    lags = [lag for at, lag in _loop_lags if now - at < 86400]
    return {"resumes24h": sum(1 for at in _resumes if now - at < 86400), "lags24h": len(lags),
            "maxLag": round(max(lags), 1) if lags else 0.0}


async def watch_loop_lag():
    """Sleep a second at a time and note how late the wake-up was: a late wake-up is the bot being
    blocked or starved of CPU, which is what makes Discord drop the connection."""
    last_report = time.monotonic()
    while True:
        start = time.monotonic()
        await asyncio.sleep(1)
        lag = time.monotonic() - start - 1
        if lag > 1:
            _loop_lags.append((time.time(), lag))
            _health_hour["lag"] = max(_health_hour["lag"], lag)
        if time.monotonic() - last_report >= 3600:
            last_report = time.monotonic()
            if _health_hour["resumes"] or _health_hour["lag"] > 1:
                print(f"연결 상태 (지난 1시간): 재연결 {_health_hour['resumes']}번 · 봇이 가장 오래 멈춘 시간 "
                      f"{_health_hour['lag']:.1f}초")
            _health_hour.update(resumes=0, lag=0.0)


@bot.event
async def on_ready():
    init_db()
    if storage.configured() and not clean_files.is_running():
        clean_files.start()
    if not monitor_servers.is_running():
        monitor_servers.start()
    if not dday_alerts.is_running():
        dday_alerts.start()
    if not tidy_memory.is_running():
        tidy_memory.start()
    if not save_traffic.is_running():
        save_traffic.start()
    print(f"로그인 완료: {bot.user} (ID: {bot.user.id})")


@bot.listen("on_message")
async def easter_egg(message):
    # Easter eggs: "if 김치 then say 맛있다" saves one for this server, "€김치" says it, and any other
    # "€이름" gets "바보". A listener, so the bot's own on_message still runs commands.
    if message.author.bot:
        return
    content = message.content.strip()

    registration = easter_eggs.parse_registration(content)
    if registration:
        if message.guild is None:
            await reply_to(message, "이스터에그는 디스코드 서버에서만 등록할 수 있습니다.", mention_author=False)
            return
        try:
            key = easter_eggs.register(message.guild.id, message.guild.name, *registration)
        except ValueError as exc:
            await reply_to(message, str(exc), mention_author=False)
            return
        except OSError as exc:
            print(f"이스터에그 저장 실패: {type(exc).__name__}: {exc}")
            await reply_to(message, "저장 실패", mention_author=False)
            return
        await reply_to(message, f"굿: `€{key}`", mention_author=False)
        return

    if content.startswith("€") and content[1:].strip():
        text = None
        if message.guild:
            try:
                text = easter_eggs.find(message.guild.id, content[1:].strip())
            except OSError as exc:
                print(f"이스터에그 파일 읽기 실패: {type(exc).__name__}: {exc}")
        await reply_to(message, text or "바보", mention_author=False)


@bot.listen("on_message")
async def count_messages(message):
    if message.guild and message.author != bot.user:
        record_traffic("messages", message.guild.id)


def welcome_message(guild):
    """The DM to a server's owner when Maco is added: how to start, and where to ask."""
    text = (f"👋 마코를 {discord.utils.escape_markdown(guild.name)}에 추가해 주셔서 감사합니다!\n"
            "`/도움말`이나 `!도움말`로 모든 기능을 볼 수 있어요\n"
            "궁금한 점, 오류 신고, 건의는 마코 서포트 센터로 와 주세요!")
    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="서포트 센터", emoji="💬", url=SUPPORT_SERVER_URL))
    view.add_item(discord.ui.Button(label="서비스 상태", emoji="🟢", url=STATUS_PAGE_URL))
    return text, view


@bot.listen("on_guild_join")
async def welcome_owner(guild):
    """Every time Maco is added to a server, its owner gets the welcome DM. Only a DM: if the owner's DMs
    are closed nothing is posted in the server instead. Nothing is stored."""
    try:
        owner = guild.owner or await bot.fetch_user(guild.owner_id)
        text, view = welcome_message(guild)
        await owner.send(text, view=view)
    except (discord.HTTPException, AttributeError, TypeError) as exc:
        print(f"환영 DM을 보내지 못함 ({guild.id}): {type(exc).__name__}")


@bot.listen("on_guild_remove")
async def forget_guild_traffic(guild):
    """The bot left: its activity counts for that server go too."""
    for counts in traffic.values():
        counts.pop(guild.id, None)
    with connect_db() as conn:
        conn.execute("DELETE FROM traffic WHERE guild_id = ?", (guild.id,))
        conn.commit()


@bot.event
async def on_error(event, *args, **kwargs):
    # Errors in event listeners (easter eggs, £servers, ...). Replacing discord.py's default handler,
    # so print the traceback too.
    trace = traceback.format_exc()
    print(f"이벤트 처리 중 오류 ({event}):\n{trace}")
    last_line = trace.strip().splitlines()[-1] if trace.strip() else "알 수 없는 오류"
    notify_admin("이벤트 처리 중 오류", last_line, f"이벤트: {event}", trace)


@bot.listen("on_command")
async def count_commands(ctx):
    if ctx.guild:
        record_traffic("commands", ctx.guild.id)


# ---------------------------------------------------------------- 관리자 명령어 (£)

OWNER_COMMANDS = [
    ("£도움말", "이 목록을 봅니다."),
    ("£servers", "참여 중인 서버와 최근 24시간 메시지·명령어 수를 봅니다."),
    ("£DB", "로컬 DB와 파일 저장소 상태(응답 시간, 용량, 파일 수 등)를 봅니다."),
    ("£DB 파일 [서버ID]", "올라간 파일을 번호와 함께 봅니다. 서버ID를 붙이면 그 서버만 봅니다."),
    ("£DB 삭제 <번호...>", "번호로 파일을 지웁니다. 여러 개는 띄어서 적습니다. (예: `£DB 삭제 12 15`)"),
    ("£DB 만료 <번호> <기간>", "파일 하나의 만료일을 지금부터 그 기간 뒤로 바꿉니다. (예: `£DB 만료 12 3일`)"),
    ("£DB 서버삭제 <서버ID>", "그 서버에 올라간 파일을 전부 지웁니다. 확인 버튼을 한 번 더 누릅니다."),
    ("£DB 기간 [기간]", "자동 삭제 기간을 보거나 바꿉니다. 새로 올리는 파일부터 적용됩니다. (예: `£DB 기간 3일`, `£DB 기간 12시간`)"),
    ("£DB 정리", "기간이 지난 파일을 바로 지우고, 목록에 없이 저장소에만 남은 파일도 정리합니다."),
    ("£DB 테스트", "저장소에 실제로 시험 업로드를 해서 봇 업로드·서명 링크·웹 업로드(CORS)가 되는지 하나씩 확인합니다."),
    ("£DB 관리자 [추가|삭제] [사용자]", "파일 관리자 목록을 보거나 바꿉니다. 파일 관리자는 모든 사람의 파일을 보고 지울 수 있습니다. "
                                    "봇 관리자는 항상 포함됩니다. (예: `£DB 관리자 추가 @닉네임`)"),
    ("£DB 한도 [서버ID|여기]", "서버별 파일 한도(서버 전체 용량, 파일당 최대 크기)와 사용량을 봅니다."),
    ("£DB 한도 <서버ID|여기> [용량 <크기>] [파일 <크기>]",
     "그 서버의 한도를 바꿉니다. 크기는 마음대로 정할 수 있고, `없음`은 한도 없음, `기본`은 기본값, `초기화`는 둘 다 되돌리기입니다. "
     "(예: `£DB 한도 여기 용량 2GB 파일 500MB`, `£DB 한도 여기 용량 없음`, `£DB 한도 123456789 파일 기본`)"),
    ("£DB 한도 기본 [용량 <크기>] [파일 <크기>]", "따로 정하지 않은 서버들에 쓰는 기본 한도를 바꿉니다. (예: `£DB 한도 기본 파일 1GB`)"),
]


@bot.listen("on_message")
async def owner_commands(message):
    # Developer-only "£..." commands. Anyone else gets no reaction at all.
    content = message.content.strip()
    if message.author.bot or not content.startswith("£"):
        return
    words = content[1:].split()
    if not words:
        return
    command, args = words[0].lower(), words[1:]
    handlers = {"도움말": owner_help, "help": owner_help, "servers": owner_servers, "db": owner_db}
    if command not in handlers or not await bot.is_owner(message.author):
        return
    await handlers[command](message, args)


async def owner_help(message, args):
    embed = discord.Embed(title="관리자 명령어", color=discord.Color.blurple(),
                          description="봇 관리자만 쓸 수 있고, 다른 사람이 치면 아무 반응이 없습니다.")
    for name, text in OWNER_COMMANDS:
        embed.add_field(name=name, value=text, inline=False)
    await message.reply(embed=embed, mention_author=False)


async def owner_db(message, args):
    sub = args[0] if args else ""
    rest = args[1:]
    if not sub:
        async with message.channel.typing():
            embed = await db_status_embed()
        await message.reply(embed=embed, mention_author=False)
        return
    handlers = {"파일": owner_db_files, "삭제": owner_db_delete, "서버삭제": owner_db_delete_guild,
                "기간": owner_db_retention, "정리": owner_db_cleanup, "관리자": owner_db_admins,
                "테스트": owner_db_test, "한도": owner_db_limits, "만료": owner_db_expiry}
    handler = handlers.get(sub)
    if handler is None:
        await message.reply("알 수 없는 명령어입니다. `£도움말`을 확인해 주세요.", mention_author=False)
        return
    if sub not in ("기간", "관리자", "한도") and not storage.configured():
        await message.reply(FILE_NOT_CONFIGURED, mention_author=False)
        return
    await handler(message, rest)


def guild_label(guild_id):
    guild = bot.get_guild(guild_id) if guild_id else None
    return discord.utils.escape_markdown(guild.name) if guild else f"서버 {guild_id}"


async def owner_db_files(message, args):
    with connect_db() as conn:
        if args and args[0].isdigit():
            rows = conn.execute("SELECT * FROM files WHERE guild_id = ? ORDER BY uploaded_at DESC",
                                (int(args[0]),)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM files ORDER BY uploaded_at DESC").fetchall()
    if not rows:
        await message.reply("올라간 파일이 없습니다.", mention_author=False)
        return
    lines = []
    for row in rows:
        state = ("업로드 중" if row["status"] == "pending" else "삭제 안 함" if kept_for_good(row["expires_at"])
                 else f"<t:{int(row['expires_at'])}:R> 삭제")
        lines.append(f"`#{row['id']}` **{discord.utils.escape_markdown(row['name'] or '(이름 없음)')}** · "
                     f"{guild_label(row['guild_id'])} · {human_size(row['size'])} · {state}")
    text = ""
    for i, line in enumerate(lines):
        if len(text) + len(line) > 3900:
            text += f"… 외 {len(lines) - i}개"
            break
        text += line + "\n"
    embed = discord.Embed(title=f"파일 {len(rows)}개", description=text, color=discord.Color.blurple())
    embed.set_footer(text="£DB 삭제 <번호>로 지우기")
    await message.reply(embed=embed, mention_author=False)


async def owner_db_expiry(message, args):
    seconds = parse_duration(" ".join(args[1:])) if len(args) > 1 else None
    if not args or not args[0].lstrip("#").isdigit() or seconds is None:
        await message.reply("사용법: `£DB 만료 <번호> <지금부터 기간>` (예: `£DB 만료 12 3일`, `£DB 만료 12 6시간`)",
                            mention_author=False)
        return
    _, text = await set_file_expiry(int(args[0].lstrip("#")), time.time() + seconds)
    await message.reply(text, mention_author=False)


async def owner_db_delete(message, args):
    ids = [int(a.lstrip("#")) for a in args if a.lstrip("#").isdigit()]
    if not ids:
        await message.reply("사용법: `£DB 삭제 <번호...>` (번호는 `£DB 파일`에서 확인)", mention_author=False)
        return
    with connect_db() as conn:
        rows = [dict(r) for r in conn.execute(
            f"SELECT * FROM files WHERE id IN ({','.join('?' * len(ids))})", ids)]
    found = {r["id"] for r in rows}
    deleted, failed = [], []
    for row in rows:
        (deleted if await remove_file(row) else failed).append(f"`#{row['id']}` {row['name'] or '(이름 없음)'}")
    lines = []
    if deleted:
        lines.append("삭제했습니다: " + ", ".join(deleted))
    if failed:
        lines.append("지우지 못했습니다 (저장소 오류): " + ", ".join(failed))
    missing = [f"`#{i}`" for i in ids if i not in found]
    if missing:
        lines.append("없는 번호: " + ", ".join(missing))
    await message.reply("\n".join(lines)[:2000], mention_author=False)


class ConfirmView(discord.ui.View):
    """[삭제] / [취소] that only the person who asked can press."""

    def __init__(self, user_id, on_confirm):
        super().__init__(timeout=60)
        self.user_id = user_id
        self.on_confirm = on_confirm
        self.message = None

    async def interaction_check(self, interaction):
        return interaction.user.id == self.user_id

    @discord.ui.button(label="삭제", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        self.stop()
        await interaction.response.edit_message(content="지우는 중...", view=None)
        await interaction.edit_original_response(content=await self.on_confirm())

    @discord.ui.button(label="취소", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        self.stop()
        await interaction.response.edit_message(content="취소했습니다.", view=None)

    async def on_timeout(self):
        if self.message:
            try:
                await self.message.edit(content="시간이 지나 취소되었습니다.", view=None)
            except discord.HTTPException:
                pass


async def owner_db_delete_guild(message, args):
    if not args or not args[0].isdigit():
        await message.reply("사용법: `£DB 서버삭제 <서버ID>`", mention_author=False)
        return
    guild_id = int(args[0])
    with connect_db() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM files WHERE guild_id = ?", (guild_id,))]
    if not rows:
        await message.reply(f"{guild_label(guild_id)}에 올라간 파일이 없습니다.", mention_author=False)
        return

    async def delete_all():
        return (await delete_guild_files(guild_id))[1]

    view = ConfirmView(message.author.id, delete_all)
    total = sum(r["size"] for r in rows)
    view.message = await message.reply(
        f"**{guild_label(guild_id)}**의 파일 **{len(rows)}개** ({human_size(total)})를 전부 지울까요? 되돌릴 수 없습니다.",
        view=view, mention_author=False,
    )


async def delete_guild_files(guild_id):
    """(ok, message) after deleting every file of a server (£DB 서버삭제 and the dashboard)."""
    with connect_db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, key, url, player_key, bar_channel_id, bar_message_id FROM files WHERE guild_id = ?", (guild_id,))]
    if not rows:
        return True, f"{guild_label(guild_id)}에 올라간 파일이 없습니다."
    if not await storage.delete([k for r in rows for k in row_keys(r)]):
        return False, "지우지 못했습니다. 저장소 연결을 확인해 주세요."
    with connect_db() as conn:
        conn.executemany("DELETE FROM files WHERE id = ?", [(r["id"],) for r in rows])
    for row in rows:
        await shortlinks.delete(row.get("url"))
        await delete_bar_message(row)
    return True, f"{guild_label(guild_id)}의 파일 {len(rows)}개를 삭제했습니다."


def parse_duration(text):
    """'3일', '12시간', '1일 6시간', '90분', '2d', '36h' -> seconds, or None."""
    units = {"일": 86400, "d": 86400, "day": 86400, "days": 86400, "시간": 3600, "h": 3600, "hour": 3600,
             "hours": 3600, "분": 60, "m": 60, "min": 60}
    matches = re.findall(r"(\d+)\s*(일|days?|d|시간|hours?|h|분|min|m)", text.lower())
    if not matches or re.sub(r"(\d+)\s*(일|days?|d|시간|hours?|h|분|min|m)|\s", "", text.lower()):
        return None
    return sum(int(n) * units[u] for n, u in matches)


async def owner_db_retention(message, args):
    current = retention_text()
    limit = "삭제 안 함" if is_forever(file_retention_max()) else human_duration(file_retention_max())
    if not args:
        await message.reply(f"자동 삭제 기간: **{current}** (설정할 수 있는 범위: 1시간 ~ {limit})\n"
                            "바꾸려면 `£DB 기간 3일`처럼 입력해 주세요.", mention_author=False)
        return
    await message.reply(apply_retention(" ".join(args))[1], mention_author=False)


def apply_retention(text):
    """(ok, message): set the auto-delete period from text like '3일' (£DB 기간 and the dashboard)."""
    current = retention_text()
    keep = text.strip().replace(" ", "") in ("삭제안함", "무제한", "영구", "없음", "forever")
    seconds = FILE_RETENTION_FOREVER if keep else parse_duration(text)
    if seconds is None:
        return False, "기간을 알아보지 못했습니다. 예: `3일`, `12시간`, `1일 6시간`"
    if not FILE_RETENTION_MIN <= seconds <= file_retention_max():
        return False, ("1시간 ~ 7일 사이로 정해 주세요. 비공개 저장소는 단축 링크(NKOTE)가 설정돼 있어야 "
                       "7일보다 길게 보관할 수 있습니다.")
    set_setting("file_retention_seconds", seconds)
    return True, (f"자동 삭제 기간을 **{current} → {retention_text()}**(으)로 바꿨습니다. "
                  "새로 올리는 파일부터 적용되며, 이미 올라간 파일은 원래 기간대로 지워집니다.")


async def owner_db_test(message, args):
    async with message.channel.typing():
        results = await storage.self_test() + await shortlinks.self_test()
    lines = [f"{'🟢' if ok else '🔴'} **{label}** · {detail[:300]}" for label, ok, detail in results]
    embed = discord.Embed(title="저장소 시험", description="\n".join(lines)[:4000],
                          color=discord.Color.green() if all(ok for _, ok, _ in results) else discord.Color.red())
    embed.set_footer(text="시험용 파일은 바로 지웁니다")
    await message.reply(embed=embed, mention_author=False)


async def owner_db_admins(message, args):
    admins = file_admin_ids()
    action = args[0] if args else ""
    ids = {int(i) for i in re.findall(r"\d{15,20}", " ".join(args[1:]))}
    if action in ("추가", "삭제"):
        if not ids:
            await message.reply(f"사용법: `£DB 관리자 {action} <@사용자 또는 사용자ID>`", mention_author=False)
            return
        admins = admins | ids if action == "추가" else admins - ids
        set_setting("file_admins", ",".join(str(i) for i in sorted(admins)))
        done = "추가했습니다" if action == "추가" else "뺐습니다"
        await message.reply(f"파일 관리자에서 {' '.join(f'<@{i}>' for i in sorted(ids))}님을 {done}.", mention_author=False)
        return
    try:
        owners = await admin_user_ids()
    except discord.HTTPException:
        owners = set()
    lines = [f"• <@{i}> (봇 관리자)" for i in sorted(owners)] + [f"• <@{i}>" for i in sorted(admins - owners)]
    await message.reply(
        "**파일 관리자** (모든 사람의 파일을 보고 지울 수 있음)\n" + "\n".join(lines)
        + "\n바꾸려면 `£DB 관리자 추가 @사용자` / `£DB 관리자 삭제 @사용자`", mention_author=False,
    )


UNLIMITED_WORDS = {"없음", "한도없음", "무제한", "제한없음", "unlimited", "none", "∞"}


def parse_size(text):
    """'500MB', '2GB', '1.5g', '300' (MB) -> bytes; '없음' / '무제한' / '한도 없음' -> UNLIMITED; else None."""
    text = text.strip().lower()
    if text.replace(" ", "") in UNLIMITED_WORDS:
        return UNLIMITED
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(b|kb|mb|gb|tb|k|m|g|t)?", text)
    if not match:
        return None
    unit = {"b": 0, "k": 1, "m": 2, "g": 3, "t": 4}[(match[2] or "m")[0]]
    size = int(float(match[1]) * 1024 ** unit)
    return size if size > 0 else None


LIMIT_WORDS = {"용량": "quota", "전체": "quota", "파일": "max", "최대": "max"}


def limits_line(guild_id):
    quota, used = guild_quota(guild_id), file_usage(guild_id)
    marks = {kind: "" if _limit_setting(kind, guild_id) else " (기본)" for kind in LIMIT_NAMES}
    return (f"용량 **{human_size(used)} / {limit_size(quota)}**{marks['quota']} · "
            f"파일당 최대 **{limit_size(guild_limit(guild_id, 'max'))}**{marks['max']}")


async def owner_db_limits(message, args):
    if len(args) <= 1 and (not args or args[0] in ("여기", "기본") or args[0].isdigit()):
        await message.reply(embed=limits_embed(message, args[0] if args else None), mention_author=False)
        return
    target = args[0]
    if target == "여기":
        if message.guild is None:
            await message.reply("`여기`는 서버 채널에서만 쓸 수 있습니다. 서버ID를 적어 주세요.", mention_author=False)
            return
        scope = message.guild.id
    elif target == "기본":
        scope = "default"
    elif target.isdigit():
        scope = int(target)
    else:
        await message.reply("사용법: `£DB 한도 <서버ID|여기|기본> [용량 <크기>] [파일 <크기>]` (`£도움말` 참고)",
                            mention_author=False)
        return
    rest = args[1:]
    if rest == ["초기화"]:
        changes = {"quota": None, "max": None}
    else:
        changes = {}
        if len(rest) % 2 or not rest:
            rest = []
        for word, value in zip(rest[::2], rest[1::2]):
            kind = LIMIT_WORDS.get(word)
            size = None if value == "기본" else parse_size(value)
            if kind is None or (size is None and value != "기본"):
                changes = {}
                break
            changes[kind] = size
        if not changes:
            await message.reply("알아보지 못했습니다. 예: `£DB 한도 여기 용량 2GB 파일 500MB`, `£DB 한도 기본 파일 1GB`, "
                                "`£DB 한도 여기 용량 없음`, `£DB 한도 123456789 초기화`", mention_author=False)
            return
    await message.reply(apply_limits(scope, changes)[1], mention_author=False)


def limit_changes(quota=None, max_size=None):
    """{kind: bytes or None (= back to default)} from dashboard text ('' or missing = unchanged), or a
    message saying what couldn't be read."""
    changes = {}
    for kind, value in (("quota", quota), ("max", max_size)):
        value = str(value or "").strip()
        if not value:
            continue
        size = None if value == "기본" else parse_size(value)
        if size is None and value != "기본":
            return f"{LIMIT_NAMES[kind]} `{value}`를 알아보지 못했습니다. 예: 500MB, 2GB, 한도 없음"
        changes[kind] = size
    return changes or "바꿀 값이 없습니다."


def apply_limits(scope, changes):
    """(ok, message): save a server's limits (scope = guild id) or the defaults (scope = "default")."""
    # Any size (or 한도 없음). Only what storage itself can't do is noted: one upload holds at most 5GB, and
    # everything together stays within the bot-wide FILE_TOTAL_QUOTA.
    for kind, size in changes.items():
        key = f"file_limit:{kind}:{scope}"
        if size is None:
            with connect_db() as conn:
                conn.execute("DELETE FROM settings WHERE key = ?", (key,))
        else:
            set_setting(key, "unlimited" if size == UNLIMITED else int(size))
    if scope == "default":
        where = "기본 한도 (따로 정하지 않은 서버)"
        result = (f"서버 전체 용량 **{limit_size(default_limit('quota'))}** · "
                  f"파일당 최대 **{limit_size(default_limit('max'))}**")
    else:
        where = f"**{guild_label(scope)}**"
        result = limits_line(scope)
    notes = []
    if scope != "default" and file_usage(scope) > guild_quota(scope):
        notes.append("지금 쓰는 용량이 한도보다 많습니다. 이미 올라간 파일은 그대로 두고, 공간이 생길 때까지 새로 올리기만 막습니다.")
    if changes.get("quota") and changes["quota"] > FILE_TOTAL_QUOTA:
        notes.append(f"봇 전체 한도({human_size(FILE_TOTAL_QUOTA)}, `FILE_TOTAL_QUOTA_MB`) 안에서만 올라갑니다.")
    if changes.get("max") and changes["max"] > FILE_MAX_HARD:
        notes.append(f"저장소는 한 번에 {human_size(FILE_MAX_HARD)}까지만 받아서, 그보다 큰 파일은 올라가지 않습니다.")
    return True, (f"{where} 한도를 바꿨습니다.\n{result}" + "".join(f"\n-# {n}" for n in notes)
                  + "\n-# 이미 열어 둔 업로드 링크도 올린 뒤에 새 한도로 다시 확인합니다.")


def limits_embed(message, target):
    embed = discord.Embed(title="파일 한도", color=discord.Color.blurple())
    embed.add_field(name="기본 (따로 정하지 않은 서버)", inline=False, value=(
        f"서버 전체 용량 **{limit_size(default_limit('quota'))}** · 파일당 최대 **{limit_size(default_limit('max'))}**\n"
        f"-# 봇 전체: {human_size(file_usage())} / {human_size(FILE_TOTAL_QUOTA)} · 한 파일은 5GB를 넘을 수 없습니다"
    ))
    if target == "여기" and message.guild:
        target = str(message.guild.id)
    if target and target.isdigit():
        embed.add_field(name=guild_label(int(target)), value=limits_line(int(target)), inline=False)
    with connect_db() as conn:
        keys = [r["key"] for r in conn.execute("SELECT key FROM settings WHERE key LIKE 'file_limit:%'")]
    custom = sorted({int(k.rsplit(":", 1)[1]) for k in keys if k.rsplit(":", 1)[1].isdigit()})
    if custom:
        lines = [f"• {guild_label(g)} (`{g}`) · {limits_line(g)}" for g in custom]
        text = ""
        for i, line in enumerate(lines):
            if len(text) + len(line) > 1000:
                text += f"… 외 {len(lines) - i}개"
                break
            text += line + "\n"
        embed.add_field(name="따로 정한 서버", value=text, inline=False)
    embed.set_footer(text="바꾸기: £DB 한도 <서버ID|여기|기본> 용량 2GB 파일 500MB (없음 = 한도 없음)")
    return embed


async def owner_db_cleanup(message, args):
    async with message.channel.typing():
        _, text = await run_cleanup()
    await message.reply(text, mention_author=False)


async def run_cleanup():
    """(ok, message): delete expired files now and files left in storage without a list entry."""
    with connect_db() as conn:
        expired = conn.execute("SELECT COUNT(*) FROM files WHERE expires_at < ?", (time.time(),)).fetchone()[0]
    await clean_files.coro()
    with connect_db() as conn:
        known = stored_keys(conn)
    orphans = await storage.orphan_keys(known)
    if orphans is None:
        return False, f"기간이 지난 파일 {expired}개를 정리했습니다. 저장소 목록은 확인하지 못했습니다."
    removed = len(orphans) if orphans and await storage.delete(orphans) else 0
    return True, (f"기간이 지난 파일 **{expired}개**를 정리했습니다.\n"
                  f"목록에 없이 저장소에만 남아 있던 파일 **{removed}개**를 지웠습니다."
                  + ("\n⚠️ 일부를 지우지 못했습니다." if orphans and not removed else ""))


def container_memory():
    """(container total, file cache) in bytes from the cgroup, or None where it can't be read.
    Hosting panels usually show the container total, which includes the file cache: files the system read
    or wrote recently (the bot's DB, logs ...) kept in spare RAM, freed as soon as a program needs it."""
    for usage_path, stat_path, cache_key in (
        ("/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory.stat", "file"),  # cgroup v2
        ("/sys/fs/cgroup/memory/memory.usage_in_bytes", "/sys/fs/cgroup/memory/memory.stat", "total_cache"),  # v1
    ):
        try:
            with open(usage_path) as f:
                total = int(f.read().strip())
            stats = {}
            with open(stat_path) as f:
                for line in f:
                    key, _, value = line.partition(" ")
                    stats[key] = int(value)
            cache = stats.get(cache_key, stats.get("cache"))
            if cache is not None:
                return total, cache
        except (OSError, ValueError):
            continue
    return None


def memory_usage():
    """(current or None, peak or None) memory of this process in bytes. Some hosts' /proc lacks the
    peak line (or /proc entirely), so each value falls back on its own."""
    current = peak = None
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    current = int(line.split()[1]) * 1024
                elif line.startswith("VmHWM:"):
                    peak = int(line.split()[1]) * 1024
    except (OSError, ValueError):
        pass
    if current is None:
        try:
            with open("/proc/self/statm") as f:
                current = int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
        except (OSError, ValueError, IndexError, AttributeError):
            pass
    if peak is None:
        try:
            import resource
            peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024  # KB on Linux
        except (ImportError, OSError):
            pass
    return current, peak


async def db_status_embed():
    embed = discord.Embed(title="DB 상태", color=discord.Color.blurple(), timestamp=datetime.now(timezone.utc))
    current, peak = memory_usage()
    limit = env_int("MEMORY_LIMIT_MB", 256, 1) * 1024 * 1024
    parts = []
    if current is not None:
        parts.append(f"지금 **{human_size(current)}** / {human_size(limit)} ({current / limit * 100:.0f}%)")
    if peak is not None:
        parts.append(f"켜진 뒤 최대 {human_size(peak)}")
    text = "봇 프로세스 " + " · ".join(parts) if parts else "이 호스팅에서는 확인할 수 없습니다"
    container = container_memory()
    if container:
        total, cache = container
        text += (f"\n컨테이너 전체 {human_size(total)} (그중 파일 캐시 {human_size(cache)})\n"
                 "-# 호스팅 패널은 보통 컨테이너 전체를 보여 줍니다. 파일 캐시는 최근 읽고 쓴 파일(DB, 로그 등)을 남는 램에 "
                 "둔 것으로, 다른 프로그램이 램을 필요로 하면 바로 비워집니다.")
    embed.add_field(name="🧠 메모리", inline=False, value=text)
    if current is not None and current > limit * 0.9:
        embed.color = discord.Color.orange()

    # Local sqlite (servers, uptime samples, file list)
    try:
        start = time.perf_counter()
        with connect_db() as conn:
            conn.execute("SELECT 1").fetchone()
            ping_ms = (time.perf_counter() - start) * 1000
            servers = conn.execute("SELECT COUNT(*) FROM servers").fetchone()[0]
            samples = conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
            ready, pending = (conn.execute(
                "SELECT COALESCE(SUM(status = 'ready'), 0), COALESCE(SUM(status = 'pending'), 0) FROM files"
            ).fetchone())
            next_expiry = conn.execute("SELECT MIN(expires_at) FROM files WHERE status = 'ready'").fetchone()[0]
            top = conn.execute(
                "SELECT guild_id, COUNT(*), SUM(size) FROM files WHERE status = 'ready' "
                "GROUP BY guild_id ORDER BY SUM(size) DESC LIMIT 5"
            ).fetchall()
        size = os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0
        embed.add_field(name="🗄️ 로컬 DB (sqlite)", inline=False, value=(
            f"🟢 연결됨 · 응답 {ping_ms:.1f} ms · 파일 크기 {human_size(size)}\n"
            f"등록 서버 {servers:,}개 · 업타임 기록 {samples:,}개 ({history_days()}일 보관)"
        ))
    except sqlite3.Error as exc:
        embed.add_field(name="🗄️ 로컬 DB (sqlite)", value=f"🔴 오류: {type(exc).__name__}: {exc}", inline=False)
        embed.color = discord.Color.red()
        return embed

    # File storage (Backblaze B2 / R2 ...)
    if not storage.configured():
        embed.add_field(name="📦 파일 저장소", value="⚪ 설정되지 않음 (`STORAGE_*` 값 없음)", inline=False)
        return embed
    host, bucket, mode = storage.describe()
    ping_ms, error = await storage.ping()
    if error:
        embed.color = discord.Color.red()
        connection = f"🔴 연결 실패 · {error}"
    else:
        connection = f"🟢 연결됨 · 응답 {ping_ms:.0f} ms"
    used, total = file_usage(), FILE_TOTAL_QUOTA
    embed.add_field(name="📦 파일 저장소", inline=False, value=(
        f"{connection}\n`{host}` · 버킷 `{bucket}` · {mode}\n"
        f"단축 링크 {f'`{shortlinks.host()}`' if shortlinks.configured() else '설정 안 됨 (긴 링크 사용)'}\n"
        f"자동 삭제 {'🟢 동작 중' if clean_files.is_running() else '🔴 멈춤'} · "
        f"영상 변환(ffmpeg) {'있음' if storage.can_convert() else '없음'} · "
        f"노래 재생 {'🟢 가능' if storage.ffmpeg_path() else '🔴 ffmpeg 없음 (imageio-ffmpeg 설치 필요)'}"
    ))
    percent = used / total * 100 if total else 0
    embed.add_field(name="💾 용량", inline=False, value=(
        f"사용 {human_size(used)} / {human_size(total)} ({percent:.1f}%)\n"
        f"남은 용량 **{human_size(max(0, total - used))}** · 기본 한도: 서버당 {limit_size(default_limit('quota'))}, "
        f"파일당 {limit_size(default_limit('max'))} (`£DB 한도`)"
    ))
    files_text = f"올라간 파일 **{ready:,}개** · 업로드 진행 중 {pending:,}개 · 자동 삭제 기간 {retention_text()}"
    if next_expiry:
        files_text += f"\n다음 자동 삭제 <t:{int(next_expiry)}:R>"
    if not error:
        actual = await storage.bucket_usage()
        if actual:
            count, actual_size, more = actual
            files_text += (f"\n저장소 실제: {count:,}{'+' if more else ''}개 · {human_size(actual_size)}"
                           f"{'+' if more else ''}")
    embed.add_field(name="📁 파일", value=files_text, inline=False)
    if top:
        lines = []
        for guild_id, count, total_size in top:
            guild = bot.get_guild(guild_id) if guild_id else None
            name = discord.utils.escape_markdown(guild.name) if guild else f"서버 {guild_id}"
            lines.append(f"{name} · {count}개 · {human_size(total_size)}")
        embed.add_field(name="많이 쓰는 서버", value="\n".join(lines), inline=False)
    embed.set_footer(text="£도움말로 관리자 명령어 보기 · 저장소 실제 값은 목록 요청으로 확인 (B2 무료 하루 2,500회에 포함)")
    return embed


async def owner_servers(message, args):
    # £servers: lists the servers the bot is in.
    guilds = sorted(bot.guilds, key=lambda g: g.member_count or 0, reverse=True)
    day, week = traffic_totals(24), traffic_totals(TRAFFIC_KEEP_HOURS)
    lines = [
        f"**{discord.utils.escape_markdown(g.name)}** · 멤버 "
        f"{f'{g.member_count:,}' if g.member_count is not None else '?'}명 · "
        f"메시지 {day.get(('messages', g.id), 0):,} (7일 {week.get(('messages', g.id), 0):,}) · "
        f"명령어 {day.get(('commands', g.id), 0):,} (7일 {week.get(('commands', g.id), 0):,})"
        for g in guilds
    ]
    description = ""
    for i, line in enumerate(lines):
        if len(description) + len(line) + 30 > 4000:  # embed description limit is 4096
            description += f"… 외 {len(lines) - i}개"
            break
        description += line + "\n"
    embed = discord.Embed(
        title=f"참여 중인 서버 {len(guilds)}개",
        description=description or "참여 중인 서버가 없습니다.",
        color=discord.Color.blurple(),
    )
    embed.set_footer(text="메시지·명령어 수: 최근 24시간 (괄호는 7일) · 1주일이 지난 기록은 자동 삭제")
    await message.reply(embed=embed, mention_author=False)


@bot.command(name="도움말", aliases=["help"])
async def help_command(ctx):
    embed = command_list_embed(ctx.guild.id if ctx.guild else None)
    if not missing_permissions(ctx.channel, ("embed_links",)):
        await ctx.reply(embed=embed, mention_author=False)
        return
    # Without 링크 첨부 Discord drops embeds, so the list goes out as plain text (2,000 characters a message).
    lines = [f"**{embed.title}** 명령어 목록"]
    lines += [f"**{f.name}**\n{f.value}" for f in embed.fields]
    lines.append(permission_problem_text(ctx.channel, missing_permissions(ctx.channel)).replace(
        "명령어를 실행할 수 없습니다", "일부 명령어(카드·그림·게임)를 실행할 수 없습니다"))
    chunk = ""
    for line in lines:
        if chunk and len(chunk) + len(line) + 2 > 2000:
            await ctx.send(chunk)
            chunk = ""
        chunk = f"{chunk}\n\n{line}" if chunk else line
    await ctx.send(chunk)


def command_list_embed(guild_id=None):
    """Every ! command; used by !도움말 and the /도움말 명령어 보기 button. Switched-off games are left out."""
    embed = discord.Embed(title="Maco", color=discord.Color.green())
    if uptime_enabled(guild_id):
        embed.add_field(
            name="!주소추가 <주소> [이름]",
            value="서버를 추가합니다.\n(예: `!주소추가 hypixel.net 하이픽셀`)",
            inline=False,
        )
        embed.add_field(name="!업타임", value="등록된 모든 서버의 상태를 봅니다.", inline=False)
        embed.add_field(name="!업타임 <이름>", value="서버 하나의 자세한 업타임을 봅니다.", inline=False)
        embed.add_field(name="!주소", value="등록된 서버 목록을 봅니다.", inline=False)
        embed.add_field(name="!주소삭제 <이름>", value="서버를 삭제합니다.", inline=False)
    embed.add_field(
        name="!H전적 <닉네임> [bw|sw|duels]",
        value="하이픽셀 전적 카드를 봅니다. 게임을 붙이면 게임 별 전적을 봅니다.",
        inline=False,
    )
    embed.add_field(
        name="!V전적 <이름#태그> [지역]",
        value="발로란트 전적 카드를 봅니다. 지역은 자동으로 찾습니다.",
        inline=False,
    )
    embed.add_field(name="!올리기 [이름]", value="파일을 첨부하면 올리고 미리보기로 보여 줍니다(영상은 바로 재생, 노래는 바 형태·영상 형태 중 고름). 첨부 없이 쓰면 큰 파일용 링크를 DM으로 받습니다. "
                    f"{retention_phrase()}.", inline=False)
    embed.add_field(name="!파일 [이름]", value="내가 올린 파일 목록을 봅니다. 이름을 붙이면 그 파일을 미리보기로 보냅니다.", inline=False)
    embed.add_field(name="!파일삭제 <이름>", value="내가 올린 파일을 지웁니다.", inline=False)
    embed.add_field(name="!운세", value="오늘의 운세를 봅니다. 칠 때마다 무작위로 나옵니다.", inline=False)
    embed.add_field(name="!일침", value="정신이 번쩍 드는 한마디를 듣습니다. 칠 때마다 무작위로 나옵니다.", inline=False)
    embed.add_field(name="!dday [이름]", value="이 서버의 D-Day 목록을 보거나 그 날까지 남은 시간을 봅니다. "
                                              "만들기·편집·삭제는 `/도움말` → 기타 → D-Day에서 합니다.", inline=False)
    embed.add_field(name="!나라시작 [쉬움|보통|어려움] · !추측 <나라> · !포기 · !나라순위 · !나라출제",
                    value="나라 맞히기: 채널의 모두가 함께 비밀 나라를 맞힙니다. 추측할 때마다 대륙·인구·면적·거리·방향 힌트가 표로 나옵니다. "
                          "쉬움은 잘 알려진 나라, 보통은 모든 나라, 어려움은 잘 알려지지 않은 나라를 10번 안에 맞힙니다.",
                    inline=False)
    embed.add_field(name="!국기시작 [쉬움|보통|어려움] [문제 수] · !국기 <나라> · !힌트 · !국기순위",
                    value="국기 맞히기: 국기를 보고 가장 먼저 나라를 맞힌 사람의 개수가 기록됩니다. `!힌트`는 초성, "
                          "`!국기스킵`은 넘기기, `!국기중지`는 그만하기입니다.", inline=False)
    embed.add_field(name="!수도시작 [쉬움|보통|어려움] [문제 수] · !수도 <수도> · !힌트 · !수도순위",
                    value="수도 맞히기: 나라가 나오면 가장 먼저 수도를 맞힌 사람의 개수가 기록됩니다. "
                          "`!수도스킵`은 넘기기, `!수도중지`는 그만하기입니다.", inline=False)
    embed.add_field(name="!지도시작 [쉬움|보통|어려움] [문제 수] · !지도 <나라> · !힌트 · !지도순위",
                    value="지도 맞히기: 지도(어려움은 나라 모양)를 보고 가장 먼저 나라를 맞힌 사람의 개수가 기록됩니다.",
                    inline=False)
    if game_enabled("anime", guild_id):
        embed.add_field(name="!캐릭시작 [쉬움|보통] [문제 수] · !캐릭 <이름> · !힌트 · !캐릭순위",
                        value="애니캐 맞히기: 애니 캐릭터 그림을 보고 가장 먼저 이름을 맞힌 사람의 개수가 기록됩니다. "
                              "`!애니 리스트`로 작품 목록, `!애니 추천 <작품>`으로 작품 추천.",
                        inline=False)
    if game_enabled("song", guild_id):
        embed.add_field(name="!노래시작 [쉬움|보통|어려움] [장르] [문제 수] · !노래 <제목> · !노래추가 <곡>",
                        value="노래 맞히기: 노래 일부분(공식 미리듣기)을 듣고 가장 먼저 제목을 맞힌 사람의 개수가 기록됩니다. "
                              "`!노래추가`로 이 서버에 곡을 넣을 수 있습니다.", inline=False)
    embed.add_field(name="!xp [이름] · !순위" + (" · !순위 전체" if rank_setting("global", guild_id) else ""),
                    value="`!xp`는 내 XP와 게임별 기록(`!xp @사람`은 그 사람), `!순위`는 모든 게임을 합친 XP 순서. "
                          "지리(나라 한 판·수도·지도) 1 XP, 국기·애니캐·노래 0.5 XP.", inline=False)
    if rank_setting("bet", guild_id):
        embed.add_field(name="!○○시작 ... 배팅 · !배팅 <XP>",
                        value="게임 시작 명령어 끝에 `배팅`을 붙이면 배팅 판(예: `!국기시작 보통 배팅`, `!나라시작 배팅`). "
                              f"처음 {BET_OPEN_SECONDS}초 동안 `!배팅 10`처럼 {BET_MIN}~{BET_MAX} XP를 걸고, "
                              "배팅한 사람 중 가장 많이 맞힌 사람이 판돈을 모두 가져갑니다.", inline=False)
    embed.add_field(name="!정보 <나라>", value="나라의 소개(위키백과)와 수도·인구·면적·언어·통화·이웃 나라를 봅니다. `!나라정보`도 됩니다.",
                    inline=False)
    embed.add_field(name="/글자콘 글자:<글자> [메시지:<메시지>]",
                    value=f"글자를 디시 글자콘처럼 그림으로 만들어 올립니다 (최대 {textcon.MAX_CHARS}자). 메시지를 넣으면 같이 보냅니다.", inline=False)
    # Divider between the feature commands and contacting the developer (embeds have no real separator).
    embed.add_field(name="​", value="─" * 24, inline=False)
    embed.add_field(
        name="!개발자소통",
        value="개발자에게 메시지를 보냅니다.\n(`/도움말`의 ✉️ DM to Dev. 버튼으로도 보낼 수 있습니다)\n"
              f"사용법 질문·오류 신고·건의는 [마코 서포트 센터]({SUPPORT_SERVER_URL})에서도 받습니다.",
        inline=False,
    )
    return embed


BOT_DIR = os.path.dirname(os.path.abspath(__file__))
# key -> (sentence file, line used when the file is missing or empty)
RANDOM_LINES = {
    "fortune": ("fortunes.txt", "오늘은 운세가 쉬어 가는 날입니다."),
    "ilchim": ("ilchim.txt", "오늘은 일침이 쉬어 가는 날입니다."),
}


def random_line(key):
    """A random line of the key's text file, read on every call so edits apply without a restart."""
    filename, fallback = RANDOM_LINES[key]
    try:
        with open(os.path.join(BOT_DIR, filename), encoding="utf-8-sig") as f:
            lines = [line.strip() for line in f if line.strip() and not line.lstrip().startswith("#")]
    except OSError as exc:
        print(f"{filename} 읽기 실패: {type(exc).__name__}: {exc}")
        lines = []
    return random.choice(lines) if lines else fallback


@bot.command(name="운세")
async def fortune(ctx):
    await ctx.reply(random_line("fortune"), mention_author=False)


@bot.command(name="일침")
async def ilchim(ctx):
    await ctx.reply(random_line("ilchim"), mention_author=False)


@bot.command(name="주소추가")
@commands.guild_only()
async def add_server(ctx, address: str = "", *, name: str = ""):
    if not uptime_enabled(ctx.guild.id):
        await ctx.reply(UPTIME_OFF_MESSAGE, mention_author=False)
        return
    if not address:
        await ctx.reply(
            "사용법: `!주소추가 <주소> [이름]` (예: `!주소추가 hypixel.net 하이픽셀`)",
            mention_author=False,
        )
        return

    try:
        address, name = check_new_server(ctx.guild.id, address, name)
    except ValueError as exc:
        await ctx.reply(str(exc), mention_author=False)
        return
    await register_server(ctx, address, name)


def check_new_server(guild_id, address, name):
    """Validated (address, name) for a new server, or ValueError with the message to show."""
    try:
        address = validate_address(address)
    except ValueError as exc:
        raise ValueError(f"주소 형식 오류: {exc}") from None

    name = name.strip() or address
    if len(name) > MAX_NAME_LENGTH:
        raise ValueError(f"이름은 {MAX_NAME_LENGTH}자 이하로 입력해 주세요.")

    servers = guild_servers(guild_id)
    if len(servers) >= MAX_SERVERS_PER_GUILD:
        raise ValueError(f"서버는 최대 {MAX_SERVERS_PER_GUILD}개까지 등록할 수 있습니다.")
    if any(
        s["name"].lower() == name.lower() or s["address"].lower() == address.lower()
        for s in servers
    ):
        raise ValueError("이미 등록된 이름 또는 주소입니다.")
    return address, name


async def register_server(ctx, address, name):
    # Register even if temporarily offline, but tell the user the initial probe result.
    if await points_inside(address_host(address)):
        await ctx.reply(INSIDE_ADDRESS_MESSAGE, mention_author=False)
        return
    try:
        result, notice = await run_with_notice(ctx, query_server(address, strict=True))
    except InsideAddress:
        await send_result(ctx, getattr(ctx, "notice", None), content=INSIDE_ADDRESS_MESSAGE)
        return
    online, latency, players_online, players_max, external = result
    with connect_db() as conn:
        conn.execute(
            "INSERT INTO servers(guild_id, name, address, registered_by, registered_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (ctx.guild.id, name, address, ctx.author.id, time.time()),
        )
        conn.commit()

    msg = f"추가했습니다: **{name}** (`{address}`)\n"
    if online:
        msg += "🟢 온라인 (외부 조회)" if external else f"🟢 온라인 ({latency:.0f} ms)"
        if players_online is not None and players_max is not None:
            msg += f" · 접속 {players_online}/{players_max}"
    else:
        msg += "🔴 응답이 없습니다. 주소와 포트를 확인해 주세요."
    await send_result(ctx, notice, content=msg)


@bot.command(name="주소")
@commands.guild_only()
async def show_address(ctx):
    if not uptime_enabled(ctx.guild.id):
        await ctx.reply(UPTIME_OFF_MESSAGE, mention_author=False)
        return
    servers = guild_servers(ctx.guild.id)
    if not servers:
        await ctx.reply(NO_SERVER_MESSAGE, mention_author=False)
        return
    lines = [f"• **{s['name']}** — `{s['address']}`" for s in servers]
    # An embed allows 4096 characters; ten long addresses can pass a plain message's 2000.
    embed = discord.Embed(title="등록된 서버", description="\n".join(lines), color=discord.Color.green())
    await ctx.reply(embed=embed, mention_author=False)


@bot.command(name="주소삭제")
@commands.guild_only()
@commands.has_guild_permissions(manage_guild=True)
async def delete_address(ctx, *, name: str = ""):
    if not uptime_enabled(ctx.guild.id):
        await ctx.reply(UPTIME_OFF_MESSAGE, mention_author=False)
        return
    if not name:
        await ctx.reply("사용법: `!주소삭제 <이름>`", mention_author=False)
        return
    server = find_server(ctx.guild.id, name)
    if not server:
        await ctx.reply(f"`{name}` 서버가 없습니다. `!주소`로 목록을 확인해 주세요.", mention_author=False)
        return
    remove_server(server["id"])
    await ctx.reply(f"**{server['name']}** 서버와 업타임 기록을 삭제했습니다.", mention_author=False)


def remove_server(server_id):
    with connect_db() as conn:
        conn.execute("DELETE FROM servers WHERE id = ?", (server_id,))
        conn.execute("DELETE FROM samples WHERE server_id = ?", (server_id,))
        conn.commit()


@delete_address.error
async def delete_address_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.reply("서버 관리 권한이 필요합니다.", mention_author=False)
    elif isinstance(error, commands.NoPrivateMessage):
        await ctx.reply(SERVER_ONLY_MESSAGE, mention_author=False)
    else:
        await report_error(ctx, error)


def permission_problem_text(channel, missing):
    names = ", ".join(f"**{PERMISSION_NAMES.get(n, n)}**" for n in missing) or "필요한"
    where = f" {channel.mention}" if hasattr(channel, "mention") else ""
    return (f"⚠️ 봇에게{where} 채널의 {names} 권한이 없어 명령어를 실행할 수 없습니다.\n"
            "서버 관리자에게 봇 역할이나 채널 설정에서 이 권한을 켜 달라고 요청해 주세요.")


async def tell_missing_permissions(ctx, missing=None):
    """Say which permission is missing: in the channel if the bot can still post there, otherwise
    privately (an interaction's own reply, or a DM)."""
    channel = ctx.channel
    missing = missing or missing_permissions(
        channel, ("send_messages", "embed_links", "attach_files", "add_reactions")) or []
    text = permission_problem_text(channel, missing)
    interaction = getattr(ctx, "interaction", None)
    if interaction is not None:
        try:
            send = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
            await send(text, ephemeral=True)
            return
        except discord.HTTPException:
            pass
    if "send_messages" not in missing:
        try:
            await channel.send(text)
            return
        except discord.HTTPException:
            pass
    guild = getattr(ctx, "guild", None)
    try:
        await ctx.author.send(f"**{guild.name if guild else '서버'}**에서 보낸 명령어에 답하지 못했습니다.\n{text}")
    except discord.HTTPException:
        pass  # DMs closed too: nothing more the bot can do


async def report_error(ctx, error):
    """Log an unexpected command error and tell the user, replacing the waiting notice if one is up."""
    original = getattr(error, "original", error)
    if isinstance(original, TooFast):
        await tell_too_fast(ctx, original)
        return
    if isinstance(original, BotMissingPermissions):
        await tell_missing_permissions(ctx, original.missing)
        return
    if isinstance(original, discord.Forbidden) and getattr(ctx, "guild", None):
        await tell_missing_permissions(ctx)  # a permission taken away mid-command, or a rarer one
        return
    traceback.print_exception(type(original), original, original.__traceback__)
    notify_admin(
        "명령어 처리 중 오류", f"{type(original).__name__}: {original}", describe_ctx(ctx),
        "".join(traceback.format_exception(type(original), original, original.__traceback__)),
    )
    notice = getattr(ctx, "notice", None)
    if notice:
        try:
            await notice.edit(content="오류가 발생했습니다.", embed=None, attachments=[], view=None)
            return
        except discord.HTTPException:
            pass
    await ctx.reply("오류가 발생했습니다.", mention_author=False)


@bot.event
async def on_command_error(ctx, error):
    if ctx.command and ctx.command.has_error_handler():
        return  # the command's own handler deals with it
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.NoPrivateMessage):
        await ctx.reply(SERVER_ONLY_MESSAGE, mention_author=False)
        return
    await report_error(ctx, error)


class Progress:
    """Waiting message that says, in one sentence, which step a slow lookup is on."""

    def __init__(self, steps):
        self.steps = dict(steps)  # step key -> sentence such as "계정 찾는 중", in order
        self.current = next(iter(self.steps))
        self.edit = None  # async callable(text) once the message exists
        self.last_edit = 0.0

    def render(self):
        return f"{self.steps[self.current]}..."

    def attach(self, edit):
        self.edit = edit
        self.last_edit = time.monotonic()

    async def step(self, key):
        if key == self.current:
            return  # same sentence; editing would only spend rate limit
        self.current = key
        await self.refresh()

    async def switch(self, after, key, sentence):
        """Drop the steps after `after` and continue with a new step instead."""
        keys = list(self.steps)
        self.steps = {k: self.steps[k] for k in keys[:keys.index(after) + 1]}
        self.steps[key] = sentence
        await self.step(key)

    async def refresh(self):
        # Skipped edits are fine: the next step or the final result overwrites the message anyway.
        if not self.edit or time.monotonic() - self.last_edit < PROGRESS_EDIT_INTERVAL:
            return
        self.last_edit = time.monotonic()
        try:
            await self.edit(self.render())
        except discord.HTTPException:
            pass


async def run_with_notice(ctx, awaitable, progress=None):
    """Await a slow lookup, posting a waiting notice (or progress checklist) only if it takes a while."""
    task = asyncio.ensure_future(awaitable)
    done, _ = await asyncio.wait({task}, timeout=SLOW_NOTICE_SECONDS)
    notice = None
    if not done:
        try:
            notice = await ctx.reply(progress.render() if progress else "잠시만 기다려 주세요...", mention_author=False)
        except discord.HTTPException as exc:
            # The notice is only a courtesy; keep waiting for the lookup and send the result normally.
            print(f"대기 문구 전송 실패: {type(exc).__name__}: {exc}")
    if notice:
        ctx.notice = notice  # lets report_error replace it if the command fails later
        if progress:
            progress.attach(lambda text: notice.edit(content=text))
    try:
        return await task, notice
    except Exception:
        if notice:
            await notice.edit(content="오류가 발생했습니다.")
        raise


async def send_result(ctx, notice, content=None, embed=None, file=None, view=None):
    """Post the result (editing the waiting notice if there is one) and return the message."""
    if notice:
        return await notice.edit(content=content, embed=embed, attachments=[file] if file else [], view=view)
    extra = {"file": file} if file else {}
    if view:
        extra["view"] = view
    return await ctx.reply(content=content, embed=embed, mention_author=False, **extra)


def build_list_embed(servers, results, now, stats=None):
    lines_by_server = []
    online_count = 0
    for server, (online, latency, players_online, players_max, external) in zip(servers, results):
        online_count += int(online)
        st = (stats or uptime_stats_many([server["id"]], now))[server["id"]]
        day_percent, week_percent = st["day"][0], st["week"][0]
        if online:
            status = f"{latency:.0f} ms" if latency is not None else "외부 조회" if external else "-"
            if players_online is not None and players_max is not None:
                status = f"접속 {players_online}/{players_max} · {status}"
        else:
            status = "응답 없음"
        value = (f"{status}\n24시간 {percent_text(day_percent)} · 7일 {percent_text(week_percent)}"
                 if uptime_background_on() else status)
        lines_by_server.append((f"{'🟢' if online else '🔴'} {server['name']}", value))

    if online_count == len(servers):
        color = discord.Color.green()
    elif online_count == 0:
        color = discord.Color.red()
    else:
        color = discord.Color.orange()
    embed = discord.Embed(
        title="서버 업타임",
        description=f"{online_count}/{len(servers)} 온라인",
        color=color,
        timestamp=datetime.now(timezone.utc),
    )
    for name, value in lines_by_server:
        embed.add_field(name=name, value=value, inline=False)
    embed.set_footer(text=f"{uptime_footer()} · 자세히: !업타임 <이름>")
    return embed


def build_detail_embed(server, result, now, stats=None):
    online, latency, players_online, players_max, external = result
    st = (stats or uptime_stats_many([server["id"]], now))[server["id"]]
    day_percent, day_samples, avg_latency = st["day"]
    week_percent, week_samples, _ = st["week"]

    embed = discord.Embed(
        title=f"{server['name']} 업타임",
        description=f"`{server['address']}`",
        color=discord.Color.green() if online else discord.Color.red(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="상태", value="🟢 온라인" if online else "🔴 오프라인", inline=True)
    embed.add_field(
        name="응답",
        value=f"{latency:.0f} ms" if latency is not None else "외부 조회" if online and external else "-",
        inline=True,
    )

    if players_online is not None and players_max is not None:
        embed.add_field(name="접속", value=f"{players_online}/{players_max}", inline=True)

    if not uptime_background_on():
        embed.set_footer(text=uptime_footer())
        return embed
    embed.add_field(
        name="24시간 업타임",
        value=f"{day_percent:.2f}% ({day_samples}회)" if day_percent is not None else "수집 중",
        inline=True,
    )
    embed.add_field(
        name="7일 업타임",
        value=f"{week_percent:.2f}% ({week_samples}회)" if week_percent is not None else "수집 중",
        inline=True,
    )
    if avg_latency is not None:
        embed.add_field(name="평균 응답", value=f"{avg_latency:.0f} ms", inline=True)
    embed.set_footer(text=uptime_footer())
    return embed


def uptime_footer():
    if not uptime_background_on():
        return "지금 상태만 확인 (자동 확인이 꺼져 있어 업타임 %는 없습니다)"
    seconds = uptime_interval()
    return f"{human_duration(seconds) if seconds >= 60 else f'{seconds}초'}마다 확인"


@bot.command(name="업타임")
@commands.guild_only()
async def uptime(ctx, *, name: str = ""):
    if not uptime_enabled(ctx.guild.id):
        await ctx.reply(UPTIME_OFF_MESSAGE, mention_author=False)
        return
    servers = guild_servers(ctx.guild.id)
    if not servers:
        await ctx.reply(NO_SERVER_MESSAGE, mention_author=False)
        return

    if name:
        server = find_server(ctx.guild.id, name)
        if not server:
            await ctx.reply(f"`{name}` 서버가 없습니다. `!주소`로 목록을 확인해 주세요.", mention_author=False)
            return
        servers = [server]

    results, notice = await run_with_notice(
        ctx, asyncio.gather(*(query_server(s["address"]) for s in servers))
    )
    if uptime_background_on():  # with automatic checks off, a few on-demand samples would only skew the %
        save_samples([(server["id"], ctx.guild.id, online, latency)
                      for server, (online, latency, _, _, _) in zip(servers, results)])

    now = time.time()
    stats = await asyncio.to_thread(uptime_stats_many, [s["id"] for s in servers], now)
    if name:
        embed = build_detail_embed(servers[0], results[0], now, stats)
    else:
        embed = build_list_embed(servers, results, now, stats)
    await send_result(ctx, notice, embed=embed)


async def lookup(ctx, awaitable):
    """Turn a StatsError into a (None, message) pair so it can be shown to the user; API problems also go to the admin."""
    try:
        result = await awaitable, None
        mark_health("hypixel", True)
        return result
    except stats.StatsError as exc:
        # a missing player is the API working
        mark_health("hypixel", not isinstance(exc, stats.ServiceError), getattr(exc, "detail", exc))
        notify_service_error(exc, describe_ctx(ctx))
        return None, str(exc)


@bot.command(name="H전적")
@commands.cooldown(1, STATS_COOLDOWN_SECONDS, commands.BucketType.user)
async def hypixel_stats(ctx, name: str = "", game: str = ""):
    if not name:
        ctx.command.reset_cooldown(ctx)
        await ctx.reply("사용법: `!H전적 <닉네임> [bw|sw|duels]` (예: `!H전적 hypixel bw`)", mention_author=False)
        return
    game_key = HYPIXEL_GAMES.get(game.lower()) if game else None
    if game and not game_key:
        ctx.command.reset_cooldown(ctx)
        await ctx.reply("게임은 `bw`(베드워즈), `sw`(스카이워즈), `duels`(듀얼) 중에서 골라 주세요.", mention_author=False)
        return
    await run_hypixel(RequestContext(ctx, "Hypixel 전적"), name, game_key)


async def run_hypixel(ctx, name, game_key):
    progress = Progress(HYPIXEL_STEPS)
    (profile, error), notice = await run_with_notice(
        ctx, lookup(ctx, stats.hypixel_profile(name, progress.step)), progress
    )
    if error:
        await send_result(ctx, notice, content=error)
        return
    await progress.step("card")
    if game_key:
        image = await asyncio.to_thread(cards.hypixel_game_card, profile, game_key)
    else:
        image = await asyncio.to_thread(cards.hypixel_card, profile)
    await send_result(ctx, notice, file=discord.File(image, filename=f"hypixel_{profile['name']}.png"))


MATCH_FAILURE_WINDOW = 600
MATCH_FAILURE_ALERT = 3  # tell the admin once recent-match loading has failed this often within the window
_match_failures = []


def note_match_failure(error, where):
    """One slow stored-matches call is HenrikDev having a moment; several in a row are worth a DM."""
    now = time.time()
    _match_failures[:] = [t for t in _match_failures if now - t < MATCH_FAILURE_WINDOW] + [now]
    if len(_match_failures) >= MATCH_FAILURE_ALERT:
        # Fixed text, so notify_admin's 10-minute duplicate filter applies.
        notify_admin("외부 API 문제", f"최근 경기 불러오기가 10분 안에 {MATCH_FAILURE_ALERT}번 이상 실패 · {error.detail}",
                     where)


async def valorant_lookup(name, tag, region, suggest=True, progress=None, where=""):
    """Returns (profile, error, suggestions); suggestions are similar existing accounts when name#tag is unknown.
    `where` describes the request for admin error DMs."""
    try:
        profile = await stats.valorant_profile(name, tag, region, progress.step if progress else None)
        mark_health("valorant", not profile.get("matches_error"),
                    getattr(profile.get("matches_error"), "detail", "최근 경기 불러오기 실패"))
        if profile.get("matches_error"):
            note_match_failure(profile["matches_error"], where)
        return profile, None, []
    except stats.PlayerNotFound:
        mark_health("valorant", True)
        error = f"`{name}#{tag}` 계정을 찾을 수 없습니다."
        suggestions, failure = [], None
        if suggest:
            if progress:
                await progress.switch("account", "similar", "비슷한 계정 찾는 중")
            suggestions, failure = await stats.similar_accounts(name, tag)
        if failure:
            notify_service_error(failure, where)
            if not suggestions:
                error += f" 비슷한 계정은 확인하지 못했습니다: {failure}"
        elif not suggestions:
            error += " 이름과 태그를 다시 확인해 주세요. (대문자 I와 소문자 l, 숫자 0과 영문 O를 헷갈리기 쉽습니다)"
        return None, error, suggestions
    except stats.StatsError as exc:
        mark_health("valorant", not isinstance(exc, stats.ServiceError), getattr(exc, "detail", exc))
        notify_service_error(exc, where)
        return None, str(exc), []


class RiotIdChoice(discord.ui.View):
    """Buttons asking whether the user meant one of the similar Riot IDs."""

    def __init__(self, author_id, accounts, region, header=None):
        super().__init__(timeout=60)
        self.author_id = author_id
        self.region = region
        self.header = header  # "누구의 요청" line when started from /도움말, kept on every edit
        self.message = None
        for account in accounts:
            button = discord.ui.Button(label=f"{account['name']}#{account['tag']}", style=discord.ButtonStyle.primary)
            button.callback = self.choose(account)
            self.add_item(button)
        decline = discord.ui.Button(label="아니요", style=discord.ButtonStyle.secondary)
        decline.callback = self.decline
        self.add_item(decline)

    async def interaction_check(self, interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("명령어를 입력한 사람만 고를 수 있습니다.", ephemeral=True)
            return False
        return True

    def text(self, content):
        if not self.header:
            return content
        return f"{self.header}\n{content}" if content else self.header

    def choose(self, account):
        async def callback(interaction):
            self.stop()
            progress = Progress(VALORANT_STEPS)
            await interaction.response.edit_message(content=self.text(progress.render()), view=None)
            progress.attach(lambda text: interaction.edit_original_response(content=self.text(text)))
            where = describe_interaction(interaction, f"버튼: Riot ID 선택 ({account['query_name']}#{account['query_tag']})")
            try:
                profile, error, _ = await valorant_lookup(
                    account["query_name"], account["query_tag"], self.region,
                    suggest=False, progress=progress, where=where,
                )
                if error:
                    await interaction.edit_original_response(content=self.text(error))
                    return
                await progress.step("card")
                image = await asyncio.to_thread(cards.valorant_card, profile)
                await interaction.edit_original_response(
                    content=self.text(None), attachments=[discord.File(image, filename="valorant.png")]
                )
            except Exception as exc:
                # Without this the message would stay on "불러오는 중입니다..." forever.
                print(f"전적 버튼 오류: {type(exc).__name__}: {exc}")
                notify_admin("전적 버튼 처리 중 오류", f"{type(exc).__name__}: {exc}", where, traceback.format_exc())
                await interaction.edit_original_response(content=self.text("오류가 발생했습니다."))
        return callback

    async def decline(self, interaction):
        self.stop()
        await interaction.response.edit_message(content=self.text("알겠습니다. Riot ID를 다시 확인해 주세요."), view=None)

    async def on_timeout(self):
        if self.message:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass


@bot.command(name="V전적")
@commands.cooldown(1, STATS_COOLDOWN_SECONDS, commands.BucketType.user)
async def valorant_stats(ctx, *, riot_id: str = ""):
    if not riot_id:
        ctx.command.reset_cooldown(ctx)
        await ctx.reply("사용법: `!V전적 <이름#태그> [지역]` (예: `!V전적 Hide on bush#KR1`)", mention_author=False)
        return
    try:
        name, tag, region = stats.parse_riot_id(riot_id)
    except stats.StatsError as exc:
        ctx.command.reset_cooldown(ctx)
        await ctx.reply(str(exc), mention_author=False)
        return
    await run_valorant(RequestContext(ctx, "Valorant 전적"), name, tag, region)


async def run_valorant(ctx, name, tag, region):
    progress = Progress(VALORANT_STEPS)
    (profile, error, suggestions), notice = await run_with_notice(
        ctx, valorant_lookup(name, tag, region, progress=progress, where=describe_ctx(ctx)), progress
    )
    if suggestions:
        view = RiotIdChoice(ctx.author.id, suggestions, region, getattr(ctx, "header", None))
        view.message = await send_result(ctx, notice, content=f"{error}\n혹시 아래 계정인가요?", view=view)
        return
    if error:
        await send_result(ctx, notice, content=error)
        return
    await progress.step("card")
    image = await asyncio.to_thread(cards.valorant_card, profile)
    await send_result(ctx, notice, file=discord.File(image, filename="valorant.png"))


@hypixel_stats.error
@valorant_stats.error
async def stats_command_error(ctx, error):
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.reply(f"{math.ceil(error.retry_after)}초 후에 다시 시도해 주세요.", mention_author=False)
    else:
        await report_error(ctx, error)


# ---------------------------------------------------------------- 개발자 소통

FEEDBACK_KINDS = ["버그 신고", "건의", "질문", "기타"]
FEEDBACK_COOLDOWN_SECONDS = 300
FEEDBACK_MAX_LENGTH = 1500
FEEDBACK_MAX_PHOTOS = 3
FEEDBACK_MAX_PHOTO_BYTES = 8 * 1024 * 1024
FEEDBACK_WAIT_SECONDS = 300
_feedback_last_sent = {}  # user id -> when their last message reached the developer
_feedback_sessions = {}  # user id -> token of their current !개발자소통 DM prompt


def feedback_wait_left(user_id):
    """Seconds until this user may send another message (0 if they can send now)."""
    return max(0, math.ceil(FEEDBACK_COOLDOWN_SECONDS - (time.time() - _feedback_last_sent.get(user_id, 0))))


def describe_place(guild, channel):
    return f"{guild.name} ({guild.id}) · #{channel}" if guild else "DM"


async def collect_photos(attachments):
    """(filename, bytes) for up to FEEDBACK_MAX_PHOTOS images. Discord's attachment links expire,
    so the files are re-uploaded rather than linked."""
    photos = []
    for attachment in attachments:
        if len(photos) >= FEEDBACK_MAX_PHOTOS:
            break
        if not (attachment.content_type or "").startswith("image/") or attachment.size > FEEDBACK_MAX_PHOTO_BYTES:
            continue
        try:
            data = await attachment.read()
        except discord.HTTPException as exc:
            print(f"사진 받기 실패: {type(exc).__name__}: {exc}")
            continue
        extension = os.path.splitext(attachment.filename)[1].lower() or ".png"
        photos.append((f"photo{len(photos) + 1}{extension}", data))
    return photos


async def deliver_to_admins(sender, title, body, place, photos=()):
    """DM a user's message to every admin, with a 답장 button. Returns how many admins received it."""
    embed = discord.Embed(
        title=f"📨 {title}"[:256], description=body[:4000] or "(내용 없음)",
        color=discord.Color.blurple(), timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="보낸 사람", value=f"{sender} ({sender.id})", inline=False)
    embed.add_field(name="보낸 곳", value=place[:1024], inline=False)
    if photos:
        embed.set_image(url=f"attachment://{photos[0][0]}")
    embed.set_footer(text="답장 버튼을 누르면 이 사용자에게 DM으로 전달됩니다")
    view = discord.ui.View(timeout=None)
    view.add_item(AdminReplyButton(sender.id))
    try:
        admin_ids = await admin_user_ids()
    except discord.HTTPException as exc:
        print(f"관리자 확인 실패: {type(exc).__name__}: {exc}")
        return 0
    delivered = 0
    for admin_id in admin_ids:
        try:
            admin = bot.get_user(admin_id) or await bot.fetch_user(admin_id)
            # discord.File objects can only be sent once, so build them per admin.
            files = [discord.File(io.BytesIO(data), filename=name) for name, data in photos]
            await admin.send(embed=embed, files=files, view=view)
            delivered += 1
        except discord.HTTPException as exc:
            print(f"개발자 소통 전달 실패 ({admin_id}): {type(exc).__name__}: {exc}")
    return delivered


async def finish_feedback(sender, title, body, place, photos):
    """Deliver and start the sender's cooldown. Returns the message to show the sender."""
    if await deliver_to_admins(sender, title, body, place, photos):
        _feedback_last_sent[sender.id] = time.time()
        return "개발자에게 전달했습니다. 답장이 오면 DM으로 알려 드릴게요."
    return "전달하지 못했습니다. 잠시 후 다시 시도해 주세요."


class SafeModal(discord.ui.Modal):
    """Modal that tells the user and the admin when submitting fails, instead of failing silently."""

    async def on_error(self, interaction, error):
        trace = "".join(traceback.format_exception(type(error), error, error.__traceback__))
        print(f"입력 창 처리 중 오류:\n{trace}")
        notify_admin("입력 창 처리 중 오류", f"{type(error).__name__}: {error}",
                     describe_interaction(interaction, f"입력 창: {self.title}"), trace)
        send = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
        await send("오류가 발생했습니다.", ephemeral=True)


class FeedbackModal(SafeModal, title="개발자에게 보내기"):
    def __init__(self):
        super().__init__()
        self.kind = discord.ui.Select(options=[discord.SelectOption(label=k, value=k) for k in FEEDBACK_KINDS])
        self.subject = discord.ui.TextInput(max_length=100, placeholder="한 줄로 요약해 주세요")
        self.body = discord.ui.TextInput(style=discord.TextStyle.paragraph, max_length=FEEDBACK_MAX_LENGTH)
        self.photos = discord.ui.FileUpload(required=False, max_values=FEEDBACK_MAX_PHOTOS)
        self.add_item(discord.ui.Label(text="종류", component=self.kind))
        self.add_item(discord.ui.Label(text="제목", component=self.subject))
        self.add_item(discord.ui.Label(text="내용", component=self.body))
        self.add_item(discord.ui.Label(text="사진 (선택, 최대 3장)", component=self.photos))

    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        kind = self.kind.values[0] if self.kind.values else "기타"
        photos = await collect_photos(self.photos.values)
        message = await finish_feedback(
            interaction.user, f"[{kind}] {self.subject.value}", self.body.value,
            describe_place(interaction.guild, interaction.channel), photos,
        )
        await interaction.followup.send(message, ephemeral=True)


class ReplyModal(SafeModal):
    """recipient_id set: an admin answering that user. None: a user answering the developer."""

    def __init__(self, recipient_id=None):
        super().__init__(title="사용자에게 답장" if recipient_id else "개발자에게 답장")
        self.recipient_id = recipient_id
        self.body = discord.ui.TextInput(style=discord.TextStyle.paragraph, max_length=FEEDBACK_MAX_LENGTH)
        self.add_item(discord.ui.Label(text="내용", component=self.body))

    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        if self.recipient_id is None:
            message = await finish_feedback(interaction.user, "[답장]", self.body.value, "DM 답장", ())
        else:
            embed = discord.Embed(title="💬 개발자 답장", description=self.body.value, color=discord.Color.green())
            embed.set_footer(text="아래 버튼으로 다시 답장할 수 있습니다")
            view = discord.ui.View(timeout=None)
            view.add_item(UserAnswerButton())
            try:
                user = bot.get_user(self.recipient_id) or await bot.fetch_user(self.recipient_id)
                await user.send(embed=embed, view=view)
                message = f"{user}님에게 전달했습니다."
            except discord.HTTPException:
                message = "전달하지 못했습니다. 사용자가 DM을 막아 두었거나 봇과 같은 서버에 없을 수 있습니다."
        await interaction.followup.send(message, ephemeral=True)


# Buttons keep working after a restart: discord.py rebuilds them from their custom_id.
class AdminReplyButton(discord.ui.DynamicItem[discord.ui.Button], template=r"devmsg:reply:(?P<user_id>\d+)"):
    def __init__(self, user_id):
        super().__init__(discord.ui.Button(
            label="답장", style=discord.ButtonStyle.primary, custom_id=f"devmsg:reply:{user_id}",
        ))
        self.user_id = user_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["user_id"]))

    async def interaction_check(self, interaction):
        return await bot.is_owner(interaction.user)

    async def callback(self, interaction):
        await interaction.response.send_modal(ReplyModal(self.user_id))


class UserAnswerButton(discord.ui.DynamicItem[discord.ui.Button], template=r"devmsg:answer"):
    def __init__(self):
        super().__init__(discord.ui.Button(
            label="답장하기", style=discord.ButtonStyle.secondary, custom_id="devmsg:answer",
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls()

    async def callback(self, interaction):
        left = feedback_wait_left(interaction.user.id)
        if left:
            await interaction.response.send_message(f"{left}초 후에 다시 보낼 수 있습니다.", ephemeral=True)
            return
        await interaction.response.send_modal(ReplyModal())


@bot.tree.error
async def on_app_command_error(interaction, error):
    if isinstance(error, app_commands.CommandOnCooldown):
        await interaction.response.send_message(f"{math.ceil(error.retry_after)}초 후에 다시 사용할 수 있습니다.", ephemeral=True)
        return
    original = getattr(error, "original", error)
    trace = "".join(traceback.format_exception(type(original), original, original.__traceback__))
    print(f"슬래시 명령어 오류:\n{trace}")
    notify_admin("슬래시 명령어 오류", f"{type(original).__name__}: {original}",
                 describe_interaction(interaction, f"명령어: /{interaction.command.name if interaction.command else '?'}"), trace)
    send = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
    await send("오류가 발생했습니다.", ephemeral=True)


@bot.command(name="개발자소통")
async def feedback_prefix(ctx):
    # The original DM-based flow, kept for servers where slash commands aren't available.
    left = feedback_wait_left(ctx.author.id)
    if left:
        await ctx.reply(f"{left}초 후에 다시 보낼 수 있습니다.", mention_author=False)
        return
    try:
        prompt = await ctx.author.send(
            "개발자에게 보낼 내용을 입력해 주세요. 사진은 3장까지 함께 보낼 수 있습니다.\n"
            "5분 안에 입력해 주세요. 취소하려면 `취소`라고 입력하세요."
        )
    except discord.HTTPException:
        await ctx.reply(
            "DM을 보낼 수 없습니다. 디스코드 설정 → 개인정보 보호에서 서버 멤버의 DM을 허용해 주세요.",
            mention_author=False,
        )
        return
    if ctx.guild:
        await ctx.reply("DM을 확인해 주세요. (`/도움말`의 ✉️ DM to Dev.를 쓰면 나만 보이는 입력 창으로 보낼 수 있습니다)",
                        mention_author=False)

    token = object()  # a newer !개발자소통 replaces this prompt
    _feedback_sessions[ctx.author.id] = token

    def is_answer(message):
        return (message.author.id == ctx.author.id and isinstance(message.channel, discord.DMChannel)
                and _feedback_sessions.get(ctx.author.id) is token)

    try:
        answer = await bot.wait_for("message", check=is_answer, timeout=FEEDBACK_WAIT_SECONDS)
    except asyncio.TimeoutError:
        if _feedback_sessions.get(ctx.author.id) is token:
            del _feedback_sessions[ctx.author.id]
            await prompt.channel.send("시간이 지나 취소되었습니다. 다시 보내려면 `!개발자소통`을 입력해 주세요.")
        return
    del _feedback_sessions[ctx.author.id]

    content = answer.content.strip()
    if content == "취소":
        await prompt.channel.send("취소했습니다.")
        return
    if not content and not answer.attachments:
        await prompt.channel.send("내용이 없어서 취소했습니다.")
        return
    if len(content) > FEEDBACK_MAX_LENGTH:
        await prompt.channel.send(f"{FEEDBACK_MAX_LENGTH}자 이하로 다시 보내 주세요. `!개발자소통`으로 다시 시작할 수 있습니다.")
        return
    photos = await collect_photos(answer.attachments)
    message = await finish_feedback(
        ctx.author, "[일반] !개발자소통", content or "(사진만 보냄)", describe_place(ctx.guild, ctx.channel), photos,
    )
    await prompt.channel.send(message)


# ---------------------------------------------------------------- 파일 (S3 호환 저장소: Backblaze B2, Cloudflare R2 등)

FILE_RETENTION_DEFAULT = 7 * 24 * 60 * 60
FILE_RETENTION_MIN = 60 * 60
FILE_PENDING_SECONDS = 60 * 60  # an unfinished browser upload is cleaned up after this
FILE_MAX_HARD = 5 * 1024 ** 3  # the most one S3 / B2 upload can take
FILE_TOTAL_QUOTA = env_int("FILE_TOTAL_QUOTA_MB", 10240, 1) * 1024 * 1024  # B2 / R2 free tiers are 10GB
# Starting defaults only: bot admins change them (for all servers or one) with £DB 한도, kept in the DB.
FILE_GUILD_QUOTA = env_int("FILE_GUILD_QUOTA_MB", 10240, 1) * 1024 * 1024
FILE_MAX_BYTES = min(env_int("FILE_MAX_MB", 5120, 1) * 1024 * 1024, FILE_MAX_HARD)
FILE_MAX_PER_MESSAGE = 5
FILE_COOLDOWN_SECONDS = 10


# "삭제 안 함": kept for 100 years, i.e. until someone deletes it.
FILE_RETENTION_FOREVER = 100 * 365 * 24 * 60 * 60
# A short link is made to expire at most this far ahead (NKOTE allows up to 365 days) and is pushed
# further out again as it is refreshed.
SHORT_LINK_MAX_SECONDS = 300 * 24 * 60 * 60
# On a private bucket the storage links behind the short links are presigned for at most 7 days, so short
# links of files kept longer are pointed at a fresh one this often (public buckets: just to extend them).
SHORT_LINK_REFRESH_SECONDS = 5 * 24 * 60 * 60


def file_retention_max():
    """Any period, "삭제 안 함" included, when links can outlive 7 days: a public bucket's plain links, or
    short links the bot keeps refreshing. Only private storage without NKOTE stays at 7 days."""
    if storage.PUBLIC_URL or shortlinks.configured():
        return FILE_RETENTION_FOREVER
    return storage.MAX_LINK_SECONDS


def is_forever(seconds):
    return seconds >= FILE_RETENTION_FOREVER - 86400


def kept_for_good(expires_at):
    """A file set to 삭제 안 함 (its expiry is decades away)."""
    return expires_at - time.time() > 50 * 365 * 86400


def file_retention():
    """How long new uploads are kept (£DB 기간 changes it)."""
    try:
        seconds = int(get_setting("file_retention_seconds", FILE_RETENTION_DEFAULT))
    except (ValueError, sqlite3.Error):
        seconds = FILE_RETENTION_DEFAULT
    return max(FILE_RETENTION_MIN, min(seconds, file_retention_max()))


def human_duration(seconds):
    days, rest = divmod(int(seconds), 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    parts = [f"{days}일" if days else "", f"{hours}시간" if hours else "", f"{minutes}분" if minutes else ""]
    return " ".join(p for p in parts if p) or "0분"


def retention_text():
    return "삭제 안 함" if is_forever(file_retention()) else human_duration(file_retention())


def retention_phrase():
    """'7일 후 자동 삭제' or, when files are kept, '자동으로 삭제되지 않음'."""
    return "자동으로 삭제되지 않음" if is_forever(file_retention()) else f"{retention_text()} 후 자동 삭제"


def file_notice():
    kept = ("**자동으로 삭제되지 않습니다** (직접 지울 때까지 보관)" if is_forever(file_retention())
            else f"**{retention_text()} 후 자동으로 삭제**됩니다")
    return f"📦 올린 파일은 봇의 파일 저장소에 올라가며 {kept}. 링크가 있으면 누구나 볼 수 있습니다."
FILE_NOT_CONFIGURED = "파일 저장소가 설정되지 않았습니다. 봇 관리자에게 문의해 주세요."


def human_size(size):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024


def file_usage(guild_id=None):
    with connect_db() as conn:
        if guild_id is None:
            row = conn.execute("SELECT COALESCE(SUM(size), 0) FROM files WHERE status = 'ready'").fetchone()
        else:
            row = conn.execute("SELECT COALESCE(SUM(size), 0) FROM files WHERE status = 'ready' AND guild_id = ?",
                               (guild_id,)).fetchone()
    return row[0]


# ---- per-server limits (£DB 한도): "quota" = the server's total space, "max" = the largest single file.
# Settings keys: file_limit:<quota|max>:default and file_limit:<quota|max>:<guild id>.
# A limit of UNLIMITED ("한도 없음", stored as "unlimited") never blocks by itself.
LIMIT_NAMES = {"quota": "서버 전체 용량", "max": "파일당 최대 크기"}
UNLIMITED = math.inf


def _limit_setting(kind, scope):
    try:
        value = get_setting(f"file_limit:{kind}:{scope}")
        if value == "unlimited":
            return UNLIMITED
        return int(value) if value else None
    except (ValueError, sqlite3.Error):
        return None


def default_limit(kind):
    value = _limit_setting(kind, "default")
    return value if value else FILE_GUILD_QUOTA if kind == "quota" else FILE_MAX_BYTES


def guild_limit(guild_id, kind):
    """The limit as set: the server's own if a bot admin set one, else the default (may be UNLIMITED)."""
    value = _limit_setting(kind, guild_id) if guild_id else None
    return value or default_limit(kind)


def limit_size(value):
    return "한도 없음" if value == UNLIMITED else human_size(value)


def guild_quota(guild_id):
    return guild_limit(guild_id, "quota")


def guild_max_file(guild_id):
    """Largest file the server allows: its setting, but never more than one upload can hold (5GB)."""
    return min(guild_limit(guild_id, "max"), FILE_MAX_HARD)


def max_file_text(guild_id):
    value = guild_limit(guild_id, "max")
    return f"한도 없음 (한 번에 {human_size(FILE_MAX_HARD)}까지)" if value >= FILE_MAX_HARD else human_size(value)


def guild_upload_max(guild_id):
    """Largest file this server can take right now: its per-file limit, or its free space if that is less."""
    space = max(0, guild_quota(guild_id) - file_usage(guild_id))
    return min(guild_max_file(guild_id), space, max(0, FILE_TOTAL_QUOTA - file_usage()))


def too_big_message(guild_id):
    if guild_limit(guild_id, "max") >= FILE_MAX_HARD:
        return f"파일이 너무 큽니다. (저장소는 한 번에 {human_size(FILE_MAX_HARD)}까지만 받습니다)"
    return f"파일이 너무 큽니다. (이 서버는 파일당 최대 {human_size(guild_max_file(guild_id))})"


def check_file_size(guild_id, size):
    if size > guild_max_file(guild_id):
        raise storage.StorageError(too_big_message(guild_id))


def check_file_quota(guild_id, size):
    quota = guild_quota(guild_id)
    used = file_usage(guild_id)
    if used + size > quota:
        raise storage.StorageError(
            f"이 서버의 저장 공간이 부족합니다. (남은 용량 {human_size(max(0, quota - used))} / {limit_size(quota)}) "
            "`!파일삭제`로 정리하거나 자동 삭제를 기다려 주세요."
        )
    if file_usage() + size > FILE_TOTAL_QUOTA:
        notify_admin("파일 저장 공간 가득 참", f"전체 한도 {human_size(FILE_TOTAL_QUOTA)}에 도달했습니다.")
        raise storage.StorageError("봇 전체 저장 공간이 가득 찼습니다. 오래된 파일이 자동 삭제된 뒤 다시 시도해 주세요.")


def file_admin_ids():
    """Extra file admins set with £DB 관리자 (bot owners always count)."""
    return {int(i) for i in re.findall(r"\d+", get_setting("file_admins", "") or "")}


async def is_file_admin(user):
    """Bot admins can see and delete every file; everyone else only their own."""
    return user.id in file_admin_ids() or await bot.is_owner(user)


def guild_files(guild_id, user_id=None):
    """Ready files of a server, newest first; only one person's when user_id is given."""
    with connect_db() as conn:
        if user_id is None:
            rows = conn.execute("SELECT * FROM files WHERE guild_id = ? AND status = 'ready' ORDER BY uploaded_at DESC",
                                (guild_id,))
        else:
            rows = conn.execute("SELECT * FROM files WHERE guild_id = ? AND uploaded_by = ? AND status = 'ready' "
                                "ORDER BY uploaded_at DESC", (guild_id, user_id))
        return [dict(r) for r in rows]


async def visible_files(guild_id, user):
    return guild_files(guild_id) if await is_file_admin(user) else guild_files(guild_id, user.id)


async def find_file(guild_id, name, user):
    """A file this person may see, by name (their own first, so an admin's own file wins a name clash)."""
    key = name.strip().lower()
    own = next((f for f in guild_files(guild_id, user.id) if f["name"].lower() == key), None)
    if own or not await is_file_admin(user):
        return own
    return next((f for f in guild_files(guild_id) if f["name"].lower() == key), None)


def unique_file_name(guild_id, user_id, name):
    """Names only need to be unique per person now that everyone sees just their own files."""
    taken = {f["name"].lower() for f in guild_files(guild_id, user_id)}
    candidate, number = name, 2
    while candidate.lower() in taken:
        candidate = f"{name} ({number})"
        number += 1
    return candidate


def file_display_name(name, filename):
    return (name.strip() or os.path.splitext(filename)[0] or "파일")[:MAX_NAME_LENGTH]


def save_file_row(guild_id, channel_id, user_id, name, key, content_type, size, status="ready"):
    now = time.time()
    expires = now + (file_retention() if status == "ready" else FILE_PENDING_SECONDS)
    with connect_db() as conn:
        cursor = conn.execute(
            "INSERT INTO files(guild_id, channel_id, name, key, url, content_type, size, uploaded_by, "
            "uploaded_at, expires_at, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (guild_id, channel_id, name, key, "", content_type, size, user_id, now, expires, status),
        )
        conn.commit()
        return dict(conn.execute("SELECT * FROM files WHERE id = ?", (cursor.lastrowid,)).fetchone())


async def store_attachment(guild_id, channel_id, user_id, attachment, name=""):
    """Stream one Discord attachment into storage and record it. The file never sits in memory as a whole
    (the bot host has 128MB). Videos are converted only when FILE_CONVERT_VIDEO is on."""
    filename, size = attachment.filename, attachment.size
    check_upload_allowed(guild_id, user_id)
    check_file_size(guild_id, size)
    hasher = hashlib.sha256()  # worked out while the file streams through, for the virus check
    converted = await storage.convert_url(attachment.url, filename) if storage.should_convert(filename, size) else None
    if converted:
        path, filename, size, cleanup = converted
        try:
            check_file_size(guild_id, size)
            check_file_quota(guild_id, size)
            key = storage.new_key(guild_id, filename)
            content_type = await storage.put_path(key, path, filename, hasher=hasher)
        finally:
            cleanup()
    else:
        check_file_quota(guild_id, size)
        key = storage.new_key(guild_id, filename)
        content_type = await storage.put_from_url(key, attachment.url, size, filename, hasher=hasher)
    name = unique_file_name(guild_id, user_id, file_display_name(name, filename))
    row = save_file_row(guild_id, channel_id, user_id, name, key, content_type, size)
    with connect_db() as conn:
        conn.execute("UPDATE files SET sha256 = ? WHERE id = ?", (hasher.hexdigest(), row["id"]))
        conn.commit()
    row["sha256"] = hasher.hexdigest()
    queue_virus_scan(row["id"])
    return row


async def store_file(guild_id, channel_id, user_id, filename, data, name=""):
    """Upload small in-memory data and record it (used by tests and tools; users go through attachments)."""
    check_file_size(guild_id, len(data))
    check_file_quota(guild_id, len(data))
    key = storage.new_key(guild_id, filename)
    content_type = await storage.put_bytes(key, data, filename)
    name = unique_file_name(guild_id, user_id, file_display_name(name, filename))
    return save_file_row(guild_id, channel_id, user_id, name, key, content_type, len(data))


def row_keys(row):
    """Everything stored for a file: the file itself and, for a song, its MP4 player."""
    return [row["key"]] + ([row["player_key"]] if row.get("player_key") else [])


def stored_keys(conn):
    keys = set()
    for row in conn.execute("SELECT key, player_key FROM files"):
        keys.add(row["key"])
        if row["player_key"]:
            keys.add(row["player_key"])
    return keys


def is_song(row):
    return storage.is_audio(row.get("content_type"), row.get("name"))


async def add_song_player(row):
    """For a song: make its MP4 player (album art or Maco + title, with the song's sound) so it plays in the
    chat at any size. Updates row; on failure the card simply has no player (the 원본 열기 button still plays
    it in the browser). Only the song counts towards the storage limits."""
    if not is_song(row) or row.get("player_key") or not storage.configured():
        return row
    player_key = f"{row['key']}.player.mp4"
    try:
        made = await storage.make_song_player(row["key"], row["name"], player_key, audio_cover.render)
    except Exception as exc:
        print(f"노래 플레이어 만들기 오류: {type(exc).__name__}: {exc}")
        notify_admin("노래 플레이어 만들기 오류", f"{type(exc).__name__}: {exc}", f"파일 #{row['id']}",
                     traceback.format_exc())
        made = False
    if not made:
        if not storage.ffmpeg_path():
            notify_admin("노래 플레이어를 만들 수 없음", "ffmpeg가 없습니다. requirements.txt의 imageio-ffmpeg를 설치해 주세요.")
        return row
    with connect_db() as conn:
        conn.execute("UPDATE files SET player_key = ? WHERE id = ?", (player_key, row["id"]))
    row["player_key"] = player_key
    return row


def later_add_players(message, rows, errors=(), header=None):
    """After the card is up, make the players of its songs in the background and put them into the same
    card. (Converting a song takes a few CPU-seconds, which on a small host can be tens of seconds; the
    card shouldn't wait for that.)"""
    songs = [row for row in rows if row.get("player_pending")]
    if not songs or message is None:
        return

    async def work():
        for row in songs:
            await add_song_player(row)
            row["player_pending"] = False
        try:
            await message.edit(view=file_layout(rows, errors, header))
        except (discord.HTTPException, AttributeError) as exc:
            print(f"노래 플레이어를 카드에 넣지 못했습니다: {type(exc).__name__}: {exc}")

    task = asyncio.get_running_loop().create_task(work())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def file_link(row):
    """Made fresh each time: on a private bucket it is a presigned link that lasts until the file expires."""
    return storage.link(row["key"], row["expires_at"] - time.time())


def open_link(row):
    """Link for buttons and text: the file's short link if it has one, else the long one."""
    return row.get("url") or file_link(row)


async def add_short_links(rows):
    """Give each file a nnaco.kro.kr short link (made once, kept in the url column, expiring with the file)."""
    for row in rows:
        if row.get("url") or not shortlinks.configured():
            continue
        short = await shortlinks.shorten(file_link(row), short_link_expiry(row["expires_at"]))
        if short:
            row["url"] = short
            with connect_db() as conn:
                conn.execute("UPDATE files SET url = ?, link_at = ? WHERE id = ?", (short, time.time(), row["id"]))


def short_link_expiry(expires_at):
    return min(expires_at, time.time() + SHORT_LINK_MAX_SECONDS)


async def refresh_short_links():
    """Keep the short links of long-kept files working: point them at a fresh storage link (private
    buckets' links last 7 days) and push their expiry out again. Runs with the 10-minute cleanup."""
    if not shortlinks.configured():
        return
    now = time.time()
    with connect_db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM files WHERE status = 'ready' AND url != '' AND expires_at > ? "
            "AND COALESCE(link_at, uploaded_at) < ? ORDER BY COALESCE(link_at, uploaded_at) LIMIT 50",
            (now + 3600, now - SHORT_LINK_REFRESH_SECONDS))]
    for row in rows:
        if await shortlinks.update(row["url"], file_link(row), short_link_expiry(row["expires_at"])):
            with connect_db() as conn:
                conn.execute("UPDATE files SET link_at = ? WHERE id = ?", (time.time(), row["id"]))
                conn.commit()


def file_title(row):
    return f"**{discord.utils.escape_markdown(row['name'])}** · {human_size(row['size'])}"


def file_layout(rows, errors=(), header=None):
    """The posted result: a card per file with the picture or a playable video right in the chat and the
    long link hidden behind a button (Discord's components v2 layout)."""
    view = discord.ui.LayoutView(timeout=None)
    if header:
        view.add_item(discord.ui.TextDisplay(header))
    for row in rows:
        url = open_link(row)
        media = (row["content_type"] or "").startswith(("image/", "video/"))
        player = row.get("player_key")
        card = discord.ui.Container(accent_colour=discord.Colour.green())
        card.add_item(discord.ui.TextDisplay(f"{'🎵' if is_song(row) else '📁'} {file_title(row)}"))
        if player:  # a song: its MP4 player, which Discord plays in the chat
            card.add_item(discord.ui.MediaGallery(discord.MediaGalleryItem(
                storage.link(player, row["expires_at"] - time.time()))))
        elif row.get("player_pending"):
            card.add_item(discord.ui.TextDisplay("-# 🎵 재생 준비 중… 지금은 [원본 열기]로 들을 수 있어요"))
        elif is_song(row) and storage.configured():
            limit = attach_limit(row)
            card.add_item(discord.ui.TextDisplay(
                "-# 🎵 어떻게 보여 줄까요? (올린 사람이 고를 수 있어요)\n"
                f"-# **바 형태**: 디스코드 음악 재생 바, 바로 올라가요 ({human_size(limit)} 이하) · "
                "**영상 형태**: 크기 제한 없이 재생, 큰 파일은 만드는 데 시간이 걸려요"))
            choice = discord.ui.ActionRow()
            choice.add_item(SongDisplayButton("bar", row["id"], disabled=row["size"] > limit))
            choice.add_item(SongDisplayButton("video", row["id"]))
            card.add_item(choice)
        elif media:  # the storage link itself: Discord may not preview through a redirect
            card.add_item(discord.ui.MediaGallery(discord.MediaGalleryItem(file_link(row))))
        buttons = discord.ui.ActionRow()
        if len(url) <= 512:  # Discord's limit for a link button
            buttons.add_item(discord.ui.Button(
                label="원본 열기" if storage.is_inline(row["content_type"]) else "다운로드", emoji="⬇️", url=url,
            ))
        else:
            card.add_item(discord.ui.TextDisplay(f"[⬇️ 다운로드]({url})"))
        buttons.add_item(CopyLinkButton(row["id"]))
        card.add_item(buttons)
        view.add_item(card)
    for error in errors:
        view.add_item(discord.ui.TextDisplay(f"⚠️ {error}"[:1900]))
    return view


def attach_limit(row):
    """The largest file the bot can attach in this server (Discord: 10MB, more with boosts)."""
    guild = bot.get_guild(row["guild_id"]) if row.get("guild_id") else None
    return guild.filesize_limit if guild else discord.utils.DEFAULT_FILE_SIZE_LIMIT_BYTES


async def delete_bar_message(row):
    """Remove a song's bar message (its Discord copy), best effort."""
    if row.get("bar_message_id"):
        try:
            await bot.http.delete_message(row["bar_channel_id"], row["bar_message_id"])
        except discord.HTTPException:
            pass


def card_rows_and_header(message):
    """(files on this card message, its top line) to redraw it: rows from the buttons' ids, the header from the
    first text if it is a small "-#" line."""
    ids, header = [], None
    for index, component in enumerate(message.components):
        if index == 0 and isinstance(component, discord.components.TextDisplay) and component.content.startswith("-# "):
            header = component.content
    for match in re.finditer(r"maco:copy:(\d+)", str([c.to_dict() for c in message.components])):
        if int(match[1]) not in ids:
            ids.append(int(match[1]))
    with connect_db() as conn:
        found = {r["id"]: dict(r) for r in conn.execute(
            f"SELECT * FROM files WHERE id IN ({','.join('?' * len(ids))})", ids)} if ids else {}
    return [found[i] for i in ids if i in found], header


class SongDisplayButton(discord.ui.DynamicItem[discord.ui.Button], template=r"maco:song:(?P<how>bar|video):(?P<row_id>\d+)"):
    """바 형태 / 영상 형태 on a song's card. Only the uploader (or a file admin) chooses; works after a restart."""

    def __init__(self, how, row_id, disabled=False):
        label = "바 형태 (빠르게)" if how == "bar" else "영상 형태"
        super().__init__(discord.ui.Button(
            label=label, emoji="🎵" if how == "bar" else "🎬", disabled=disabled, custom_id=f"maco:song:{how}:{row_id}",
            style=discord.ButtonStyle.primary if how == "bar" else discord.ButtonStyle.secondary,
        ))
        self.how, self.row_id = how, row_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["how"], int(match["row_id"]))

    async def callback(self, interaction):
        with connect_db() as conn:
            row = conn.execute("SELECT * FROM files WHERE id = ? AND status = 'ready'", (self.row_id,)).fetchone()
        if not row:
            await interaction.response.send_message("삭제됐거나 기간이 지난 파일입니다.", ephemeral=True)
            return
        row = dict(row)
        if not await can_manage_file(interaction.user, row):
            await interaction.response.send_message("올린 사람만 고를 수 있습니다.", ephemeral=True)
            return
        rows, header = card_rows_and_header(interaction.message)
        rows = rows or [row]
        if self.how == "video":
            for r in rows:
                if r["id"] == row["id"]:
                    r["player_pending"] = True
            await interaction.response.edit_message(view=file_layout(rows, header=header))
            later_add_players(interaction.message, rows, (), header)
            return
        if row["size"] > attach_limit(row):
            await interaction.response.send_message(
                f"이 서버에서는 {human_size(attach_limit(row))}보다 큰 파일을 첨부할 수 없어서 바 형태로 올릴 수 없습니다. "
                "영상 형태를 골라 주세요.", ephemeral=True)
            return
        await interaction.response.defer()
        posted = await post_song_bar(interaction.message.channel, row, header)
        if not posted:
            await interaction.followup.send("바 형태로 올리지 못했습니다. 잠시 후 다시 시도하거나 영상 형태를 골라 주세요.",
                                            ephemeral=True)
            return
        rest = [r for r in rows if r["id"] != row["id"]]
        try:
            if rest:
                await interaction.message.edit(view=file_layout(rest, header=header))
            else:
                await interaction.message.delete()
        except discord.HTTPException:
            pass


async def post_song_bar(channel, row, header=None):
    """Post the song itself as an attachment, so Discord shows its audio bar, with the usual buttons.
    Goes through a temporary file, never memory. Returns the message, or None on failure."""
    folder = tempfile.mkdtemp(prefix="maco-bar-")
    try:
        filename = storage.clean_filename(os.path.basename(row["key"])) or "song.mp3"
        path = os.path.join(folder, filename)
        await storage.download_to(row["key"], path)
        await add_short_links([row])
        view = discord.ui.View(timeout=None)
        url = open_link(row)
        if len(url) <= 512:
            view.add_item(discord.ui.Button(label="원본 열기", emoji="⬇️", url=url))
        view.add_item(CopyLinkButton(row["id"]))
        text = f"🎵 {file_title(row)}"
        message = await channel.send(f"{header}\n{text}" if header else text, file=discord.File(path, filename=filename),
                                     view=view, allowed_mentions=discord.AllowedMentions.none())
    except Exception as exc:
        print(f"노래 바 형태 올리기 실패: {type(exc).__name__}: {exc}")
        return None
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    await delete_bar_message(row)  # posted again: keep a single copy
    with connect_db() as conn:
        conn.execute("UPDATE files SET bar_channel_id = ?, bar_message_id = ? WHERE id = ?",
                     (message.channel.id, message.id, row["id"]))
    return message


class CopyLinkButton(discord.ui.DynamicItem[discord.ui.Button], template=r"maco:copy:(?P<row_id>\d+)"):
    """[📋 링크 복사]: Discord buttons can't write to the clipboard, so this shows the link privately in a code
    box (long-press or select to copy). Works after a restart too."""

    def __init__(self, row_id):
        super().__init__(discord.ui.Button(
            label="링크 복사", emoji="📋", style=discord.ButtonStyle.secondary, custom_id=f"maco:copy:{row_id}",
        ))
        self.row_id = row_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["row_id"]))

    async def callback(self, interaction):
        with connect_db() as conn:
            row = conn.execute("SELECT * FROM files WHERE id = ? AND status = 'ready'", (self.row_id,)).fetchone()
        if not row:
            await interaction.response.send_message("삭제됐거나 기간이 지난 파일입니다.", ephemeral=True)
            return
        row = dict(row)
        if row.get("url") or not shortlinks.configured():
            await interaction.response.send_message(f"```\n{open_link(row)}\n```", ephemeral=True)
            return
        # Making the short link first can take longer than the 3 seconds Discord waits for an answer.
        await interaction.response.defer(ephemeral=True, thinking=True)
        await add_short_links([row])
        await interaction.followup.send(f"```\n{open_link(row)}\n```", ephemeral=True)


def file_line(row):
    return f"{file_title(row)}\n{open_link(row)}"


def file_messages(rows, errors=()):
    """Plain-text fallback (if Discord refuses the layout), split under 2000 characters without cutting a link."""
    parts = [file_line(r) for r in rows] + [f"⚠️ {e}"[:1900] for e in errors]
    messages = [""]
    for part in parts:
        if messages[-1] and len(messages[-1]) + 2 + len(part) > 2000:
            messages.append("")
        messages[-1] = f"{messages[-1]}\n\n{part}" if messages[-1] else part
    return messages


async def send_files(send, rows, errors=(), header=None):
    """Post the files with send(**kwargs) (a reply, a channel.send ...): the preview layout, or plain links
    if Discord refuses it. Returns the first message."""
    if not rows:
        text = "\n".join(f"⚠️ {e}" for e in errors)[:2000] or "올린 파일이 없습니다."
        return await send(content=f"{header}\n{text}" if header else text)
    await add_short_links(rows)
    try:
        return await send(view=file_layout(rows, errors, header))
    except discord.HTTPException as exc:
        print(f"파일 미리보기 전송 실패, 링크로 보냅니다: {type(exc).__name__}: {exc}")
        first = None
        for i, message in enumerate(file_messages(rows, errors)):
            sent = await send(content=f"{header}\n{message}" if header and i == 0 else message)
            first = first or sent
        return first


def ctx_sender(ctx):
    """send(**kwargs) that answers a ! command with a reply, or a /도움말 button with a channel message."""
    if isinstance(ctx, HelpContext):
        async def send(**kwargs):
            try:
                return await ctx.channel.send(**kwargs)
            except (discord.Forbidden, AttributeError):
                return await ctx.interaction.followup.send(wait=True, **kwargs)
        return send

    async def reply(**kwargs):
        return await ctx.reply(mention_author=False, **kwargs)
    return reply


async def upload_attachments(ctx, attachments, name=""):
    """Store the attachments and post them (the waiting notice, if one appeared, is removed afterwards)."""
    async def work():
        rows, errors = [], []
        for attachment in attachments[:FILE_MAX_PER_MESSAGE]:
            if attachment.size > guild_max_file(ctx.guild.id):
                errors.append(f"`{attachment.filename}`: {too_big_message(ctx.guild.id)}")
                continue
            try:
                rows.append(await store_attachment(ctx.guild.id, ctx.channel.id, ctx.author.id, attachment,
                                                   name if len(attachments) == 1 else ""))
                mark_health("files", True)
            except storage.StorageError as exc:
                if exc.__cause__:  # the storage itself failed (not a size limit or a block)
                    mark_health("files", False, f"파일 저장소 오류 · {type(exc.__cause__).__name__}")
                    notify_admin("파일 저장소 오류", f"{type(exc.__cause__).__name__}: {exc.__cause__}", describe_ctx(ctx))
                errors.append(f"`{attachment.filename}`: {exc}")
            except discord.HTTPException:
                errors.append(f"`{attachment.filename}`: 디스코드에서 파일을 받지 못했습니다.")
        return rows, errors

    progress = Progress([("upload", "파일 올리는 중")])
    (rows, errors), notice = await run_with_notice(ctx, work(), progress)
    # The preview is a different kind of message (components v2), so it is posted fresh instead of
    # editing the waiting notice into it.
    header = getattr(ctx, "header", None)
    await send_files(ctx_sender(ctx), rows, errors, header)  # a song's card asks how to show it
    if notice:
        try:
            await notice.delete()
        except discord.HTTPException:
            pass


# ---- uploads through the web page (big files)

UPLOAD_WATCH_DELAYS = [5, 5, 10, 10, 15, 15, 20, 20, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30]  # ~10 min
_upload_locks = {}


async def start_browser_upload(guild_id, channel_id, user_id, name=""):
    """A pending upload and the page link where the user picks the file (up to the server's limits).
    Raises StorageError when the server has no space left or uploads are blocked."""
    check_upload_allowed(guild_id, user_id)
    max_bytes = guild_upload_max(guild_id)
    if max_bytes <= 0:
        check_file_quota(guild_id, 1)  # raises with the server's numbers
        raise storage.StorageError("봇 전체 저장 공간이 가득 찼습니다. 오래된 파일이 자동 삭제된 뒤 다시 시도해 주세요.")
    key = storage.new_key(guild_id, "upload")
    row = save_file_row(guild_id, channel_id, user_id, name.strip()[:MAX_NAME_LENGTH], key, "", 0, status="pending")
    link = await storage.upload_page_link(key, retention_text(), shortlinks.signal_url(key), max_bytes)  # page shows "삭제 안 함" itself
    # nnaco.kro.kr/xxxx: fits a link button, and has no "#|" for Discord to rewrite. Lives as long as the row.
    return row, await shortlinks.shorten(link, row["expires_at"]) or link


def browser_upload_text(guild_id, link=None):
    """link=None when the link is on a button instead (Discord link buttons hold up to 512 characters)."""
    return (
        (f"**[🌐 파일 올리는 페이지 열기]({link})**\n" if link else "")
        + f"페이지에서 파일을 올리면 **자동으로 채널에 올라갑니다.** (최대 {max_file_text(guild_id)}, "
          "링크는 1시간 동안 사용)"
    )


LINK_BUTTON_MAX = 512


def browser_upload_view(row_id, link=None):
    view = discord.ui.View(timeout=None)
    if link and len(link) <= LINK_BUTTON_MAX:
        view.add_item(discord.ui.Button(label="업로드 페이지 열기", emoji="🌐", url=link))
    view.add_item(UploadDoneButton(row_id))
    return view


def drop_file_row(row_id):
    with connect_db() as conn:
        conn.execute("DELETE FROM files WHERE id = ?", (row_id,))
        conn.commit()


async def finish_upload(row_id):
    """Check a browser upload and, once the file is there, finish it and post it to its channel.
    Returns (state, message): state is "waiting", "done", "failed" or "gone". Safe to call from the
    [업로드 완료] button and the background watcher at the same time."""
    lock = _upload_locks.setdefault(row_id, asyncio.Lock())
    async with lock:
        with connect_db() as conn:
            row = conn.execute("SELECT * FROM files WHERE id = ?", (row_id,)).fetchone()
        row = dict(row) if row else None
        if not row or row["status"] != "pending":
            return "gone", "이미 처리된 업로드입니다."
        info = await storage.head(row["key"])
        if info is None:
            if row["expires_at"] < time.time():
                return "gone", "업로드 시간이 지났습니다. 다시 `!올리기`를 해 주세요."
            return "waiting", "아직 올라간 파일이 없습니다. 페이지에서 파일을 다 올린 뒤 눌러 주세요."
        size, content_type, filename = info
        await upload_placeholder(row, "⏳ 파일 불러오는 중...")
        try:
            check_file_size(row["guild_id"], size)
            check_file_quota(row["guild_id"], size)
        except storage.StorageError as exc:
            await storage.delete([row["key"]])
            drop_file_row(row_id)
            await upload_placeholder(row, f"⚠️ {exc} 올린 파일은 지웠습니다.", final=True)
            return "failed", f"{exc} 올린 파일은 지웠습니다."
        filename = filename or storage.name_for_type(content_type)
        key, stored_type, size, filename = await storage.finish_browser_upload(row["key"], filename, content_type, size)
        name = unique_file_name(row["guild_id"], row["uploaded_by"], file_display_name(row["name"], filename))
        now = time.time()
        with connect_db() as conn:
            conn.execute(
                "UPDATE files SET name = ?, key = ?, url = ?, content_type = ?, size = ?, uploaded_at = ?, "
                "expires_at = ?, status = 'ready' WHERE id = ?",
                (name, key, "", stored_type, size, now, now + file_retention(), row_id),
            )
            conn.commit()
            done = dict(conn.execute("SELECT * FROM files WHERE id = ?", (row_id,)).fetchone())
    _upload_locks.pop(row_id, None)
    await post_upload(done)
    queue_virus_scan(row_id)  # read back from storage to hash it (files up to VIRUS_SCAN_MAX_BYTES)
    return "done", f"✅ **{discord.utils.escape_markdown(name)}** 올리기를 마쳤습니다. 채널에 올렸습니다."


_placeholders = {}  # row id -> the "⏳ ..." channel message that becomes the file card


async def upload_channel(row):
    channel = bot.get_channel(row["channel_id"])
    if channel is None:
        try:
            channel = await bot.fetch_channel(row["channel_id"])
        except discord.HTTPException:
            return None
    return channel


def upload_header(row):
    return f"-# <@{row['uploaded_by']}>님이 올린 파일"


async def upload_placeholder(row, text, final=False):
    """Show (or update) a channel message for a browser upload in progress: "⏳ 올리는 중..." when the page
    starts, "⏳ 불러오는 중..." once the file is in storage. It turns into the file card at the end."""
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.TextDisplay(f"{upload_header(row)}\n{text}"))
    message = _placeholders.pop(row["id"], None) if final else _placeholders.get(row["id"])
    try:
        if message:
            await message.edit(view=view)
        elif not final:
            channel = await upload_channel(row)
            if channel:
                _placeholders[row["id"]] = await channel.send(view=view)
    except discord.HTTPException as exc:
        print(f"업로드 진행 표시 실패: {type(exc).__name__}: {exc}")


async def post_upload(row):
    """Post a finished browser upload in the channel where it was started (turning the "⏳" message into it)."""
    placeholder = _placeholders.pop(row["id"], None)
    if placeholder:
        await add_short_links([row])
        try:
            await placeholder.edit(view=file_layout([row], header=upload_header(row)))
            return placeholder
        except discord.HTTPException as exc:
            print(f"업로드 결과로 바꾸기 실패, 새로 보냅니다: {type(exc).__name__}: {exc}")
            try:
                await placeholder.delete()
            except discord.HTTPException:
                pass
    channel = await upload_channel(row)
    if channel is None:
        return
    try:
        return await send_files(channel.send, [row], header=upload_header(row))
    except discord.HTTPException as exc:
        print(f"업로드 결과 전송 실패: {type(exc).__name__}: {exc}")


SIGNAL_WATCH_SECONDS = 60 * 60  # as long as the link and the pending upload last
STORAGE_CHECK_SECONDS = 60  # with signals, still look in the storage now and then (signal lost, NKOTE down)


def signal_poll_delay(elapsed):
    """Seconds between signal checks: quick while an upload is likely, slower later (NKOTE requests)."""
    return 3 if elapsed < 180 else 6 if elapsed < 600 else 20


def watch_upload(row_id, on_finish):
    """Post the file as soon as it arrives, so nobody has to press [업로드 완료]. With NKOTE the upload page
    signals "started"/"done" and the bot checks that every few seconds; without it the bot looks in the
    storage with growing gaps (about 10 minutes). on_finish(message) updates the private link message."""
    async def finish(log_label):
        try:
            return await finish_upload(row_id)
        except Exception as exc:
            print(f"업로드 확인 오류 ({log_label}): {type(exc).__name__}: {exc}")
            return "waiting", ""

    async def report(state, message):
        if state != "gone" or "시간이 지났" in message:
            try:
                await on_finish(message)
            except discord.HTTPException:
                pass

    async def watch_storage():
        for delay in UPLOAD_WATCH_DELAYS:
            await asyncio.sleep(delay)
            state, message = await finish("저장소")
            if state != "waiting":
                return await report(state, message)

    async def watch_signal(row):
        started = time.time()
        last_storage_check = started
        announced = False
        failures = 0
        while time.time() - started < SIGNAL_WATCH_SECONDS:
            await asyncio.sleep(signal_poll_delay(time.time() - started))
            try:
                signal = await shortlinks.get_signal(row["key"])
                failures = 0
            except Exception as exc:
                failures += 1
                signal = None
                if failures == 5:
                    print(f"NKOTE 신호 확인 실패가 계속됩니다: {type(exc).__name__}: {exc}")
            if signal == "started" and not announced:
                announced = True
                await upload_placeholder(row, "⏳ 파일 올리는 중...")
            if signal == "done" or time.time() - last_storage_check > STORAGE_CHECK_SECONDS * (1 + failures // 5):
                last_storage_check = time.time()
                state, message = await finish("신호" if signal == "done" else "저장소 확인")
                if state != "waiting":
                    return await report(state, message)

    async def watch():
        with connect_db() as conn:
            row = conn.execute("SELECT * FROM files WHERE id = ?", (row_id,)).fetchone()
        if not row:
            return
        if shortlinks.configured():
            await watch_signal(dict(row))
        else:
            await watch_storage()
        await upload_placeholder(dict(row), "⚠️ 업로드가 끝나지 않았습니다.", final=True)  # only if one was shown

    task = asyncio.get_running_loop().create_task(watch())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


class UploadDoneButton(discord.ui.DynamicItem[discord.ui.Button], template=r"maco:upload:(?P<row_id>\d+)"):
    """[업로드 완료] under a browser-upload link (a backup for the automatic check); works after a restart too."""

    def __init__(self, row_id):
        super().__init__(discord.ui.Button(
            label="업로드 완료", emoji="✅", style=discord.ButtonStyle.success, custom_id=f"maco:upload:{row_id}",
        ))
        self.row_id = row_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["row_id"]))

    async def callback(self, interaction):
        with connect_db() as conn:
            owner = conn.execute("SELECT uploaded_by FROM files WHERE id = ?", (self.row_id,)).fetchone()
        if owner and owner["uploaded_by"] != interaction.user.id:
            await interaction.response.send_message("업로드를 시작한 사람만 누를 수 있습니다.", ephemeral=True)
            return
        await interaction.response.defer()
        try:
            state, message = await finish_upload(self.row_id)
        except storage.StorageError as exc:
            if exc.__cause__:
                notify_admin("파일 저장소 오류", f"{type(exc.__cause__).__name__}: {exc.__cause__}",
                             describe_interaction(interaction, "버튼: 업로드 완료"))
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        except Exception as exc:
            trace = traceback.format_exc()
            print(f"업로드 완료 처리 오류:\n{trace}")
            notify_admin("업로드 완료 처리 오류", f"{type(exc).__name__}: {exc}",
                         describe_interaction(interaction, "버튼: 업로드 완료"), trace)
            await interaction.followup.send("오류가 발생했습니다.", ephemeral=True)
            return
        if state == "waiting":
            await interaction.followup.send(message, ephemeral=True)
            return
        await finish_link_message(interaction, message)


async def finish_link_message(interaction, message):
    """Replace the private message that held the upload link with the outcome."""
    if interaction.guild is None:  # the DM from !올리기
        await interaction.edit_original_response(content=message, view=None)
    else:  # the /도움말 message
        await interaction.edit_original_response(**help_message(page="files", selected="upload", notice=message,
                                                                 guild_id=interaction.guild.id))


async def can_manage_file(user, row):
    return row["uploaded_by"] == user.id or await is_file_admin(user)


FILE_EXPIRY_MIN_SECONDS = 5 * 60


async def set_file_expiry(row_id, expires_at):
    """(ok, message): change when one file is deleted (£DB 만료 and the dashboard). Its short link keeps
    its address and gets a fresh storage link that lasts until the new time."""
    with connect_db() as conn:
        row = conn.execute("SELECT * FROM files WHERE id = ? AND status = 'ready'", (row_id,)).fetchone()
    if not row:
        return False, f"#{row_id} 파일이 없습니다."
    row = dict(row)
    now = time.time()
    latest = now + file_retention_max()
    if expires_at < now + FILE_EXPIRY_MIN_SECONDS:
        return False, "만료일은 지금부터 5분 뒤 이후로 정해 주세요."
    if expires_at > latest + 60:
        return False, f"비공개 저장소의 링크는 최대 7일까지만 쓸 수 있습니다. 지금부터 7일 안으로 정해 주세요. (최대 <t:{int(latest)}:f>)"
    expires_at = min(expires_at, latest)
    row["expires_at"] = expires_at
    note = ""
    if row.get("url") and not await shortlinks.update(row["url"], file_link(row), short_link_expiry(expires_at)):
        note = "\n-# 단축 링크는 바꾸지 못해서, 이미 올라간 링크는 원래 만료일까지만 열립니다."
    with connect_db() as conn:
        conn.execute("UPDATE files SET expires_at = ?, link_at = ? WHERE id = ?", (expires_at, now, row_id))
    if not storage.PUBLIC_URL and not note:
        note = "\n-# 채팅에 이미 올라간 미리보기(사진·영상)는 올린 지 7일이 지나면 안 보일 수 있습니다. [원본 열기]·[링크 복사]는 새 만료일까지 열립니다."
    when = "**삭제 안 함**" if kept_for_good(expires_at) else f"<t:{int(expires_at)}:f>"
    return True, (f"**{discord.utils.escape_markdown(row['name'])}** 만료일을 {when}(으)로 바꿨습니다." + note)


# ---------------------------------------------------------------- 업로드 차단 · 바이러스 검사

def upload_ban(user_id):
    """The person's upload ban row as a dict, or None (expired bans are removed here)."""
    with connect_db() as conn:
        row = conn.execute("SELECT * FROM upload_bans WHERE user_id = ?", (user_id,)).fetchone()
        if row and row["until"] is not None and row["until"] <= time.time():
            conn.execute("DELETE FROM upload_bans WHERE user_id = ?", (user_id,))
            conn.commit()
            row = None
    return dict(row) if row else None


def set_upload_ban(user_id, seconds, reason=""):
    """seconds None: for good. A shorter ban never replaces a longer one that is already there."""
    current = upload_ban(user_id)
    until = None if seconds is None else time.time() + seconds
    if current and (current["until"] is None or (until is not None and current["until"] >= until)):
        return current
    with connect_db() as conn:
        conn.execute("INSERT INTO upload_bans(user_id, until, reason, created_at) VALUES (?, ?, ?, ?) "
                     "ON CONFLICT(user_id) DO UPDATE SET until = excluded.until, reason = excluded.reason, "
                     "created_at = excluded.created_at", (user_id, until, reason[:200], time.time()))
        conn.commit()
    return upload_ban(user_id)


def remove_upload_ban(user_id):
    with connect_db() as conn:
        conn.execute("DELETE FROM upload_bans WHERE user_id = ?", (user_id,))
        conn.commit()


def all_upload_bans():
    now = time.time()
    with connect_db() as conn:
        conn.execute("DELETE FROM upload_bans WHERE until IS NOT NULL AND until <= ?", (now,))
        conn.commit()
        return [dict(r) for r in conn.execute("SELECT * FROM upload_bans ORDER BY created_at DESC")]


def upload_server_setting(guild_id):
    """This server's own 파일 업로드 setting: True (켜짐), False (꺼짐) or None (follows the default)."""
    keys = switch_keys()
    if guild_id and f"upload_block:{guild_id}" in keys:
        return False
    if guild_id and f"upload_allow:{guild_id}" in keys:
        return True
    return None


def upload_default_on():
    return "upload_block:all" not in switch_keys()


def upload_enabled(guild_id):
    own = upload_server_setting(guild_id)
    return upload_default_on() if own is None else own


def upload_blocked_reason(guild_id, user_id):
    """Why this person can't upload here right now (admin panel: the default, this server or this person)."""
    if not upload_enabled(guild_id):
        if upload_server_setting(guild_id) is False:
            return "🚫 이 서버에서는 파일 올리기가 막혀 있습니다. (봇 관리자 설정)"
        return "🚫 지금은 파일 올리기가 잠시 막혀 있습니다. (봇 관리자 설정)"
    ban = upload_ban(user_id) if user_id else None
    if ban:
        when = "영구 차단" if ban["until"] is None else f"{human_duration(ban['until'] - time.time())} 남음"
        reason = f" · 사유: {ban['reason']}" if ban["reason"] else ""
        return f"🚫 파일 올리기가 차단된 계정입니다. ({when}{reason})"
    return None


def check_upload_allowed(guild_id, user_id):
    reason = upload_blocked_reason(guild_id, user_id)
    if reason:
        raise storage.StorageError(reason)


VIRUSTOTAL_KEY = os.getenv("VIRUSTOTAL_API_KEY", "").strip()
VIRUSTOTAL_URL = "https://www.virustotal.com/api/v3/files/"
VIRUSTOTAL_GAP_SECONDS = 16  # free key: 4 lookups a minute
VIRUSTOTAL_DAILY = 480  # free key: 500 a day (a little kept back)
VIRUSTOTAL_MIN_ENGINES = 3  # flagged by this many engines -> blocked; fewer -> the admin is only told
VIRUS_SCAN_MAX_BYTES = 50 * 1024 * 1024  # page uploads bigger than this aren't read back just to hash them
VIRUS_FREEZE_SECONDS = 24 * 3600  # uploader's ban after a flagged file
BLOCKED_KEEP_SECONDS = 90 * 24 * 3600  # a flagged file is kept this long as evidence (or until deleted)
_scan_queue = None  # asyncio.Queue of file row ids, made when the first scan is queued
_scan_day = {"day": "", "count": 0}


def queue_virus_scan(row_id):
    """Look the file up on VirusTotal in the background (only its SHA-256 is sent, never the file)."""
    global _scan_queue
    if not VIRUSTOTAL_KEY:
        return
    if _scan_queue is None:
        _scan_queue = asyncio.Queue(maxsize=1000)
        task = asyncio.get_running_loop().create_task(virus_scan_worker())
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
    try:
        _scan_queue.put_nowait(row_id)
    except asyncio.QueueFull:
        print(f"바이러스 검사 대기열이 가득 차서 건너뜀: 파일 {row_id}")


async def virustotal_lookup(sha256):
    """(malicious, suspicious, engines) for a known file, None if VirusTotal has never seen it.
    Raises RuntimeError("rate") when the key's limit is reached."""
    async with net.session().get(VIRUSTOTAL_URL + sha256, headers={"x-apikey": VIRUSTOTAL_KEY},
                                 timeout=aiohttp.ClientTimeout(total=20)) as response:
        if response.status == 404:
            return None
        if response.status == 429:
            raise RuntimeError("rate")
        if response.status != 200:
            raise RuntimeError(f"VirusTotal HTTP {response.status}")
        body = await response.json(content_type=None)
    stats = (body.get("data") or {}).get("attributes", {}).get("last_analysis_stats") or {}
    engines = sum(v for v in stats.values() if isinstance(v, int))
    return int(stats.get("malicious") or 0), int(stats.get("suspicious") or 0), engines


async def virus_scan_worker():
    while True:
        row_id = await _scan_queue.get()
        try:
            await virus_scan(row_id)
        except Exception as exc:
            print(f"바이러스 검사 오류 (파일 {row_id}): {type(exc).__name__}: {exc}")
        await asyncio.sleep(VIRUSTOTAL_GAP_SECONDS)


async def virus_scan(row_id):
    with connect_db() as conn:
        row = conn.execute("SELECT * FROM files WHERE id = ?", (row_id,)).fetchone()
    row = dict(row) if row else None
    if not row or row["status"] != "ready":
        return
    if not row["sha256"]:
        if row["size"] > VIRUS_SCAN_MAX_BYTES:
            return
        sha = await storage.sha256_of(row["key"])
        with connect_db() as conn:
            conn.execute("UPDATE files SET sha256 = ? WHERE id = ?", (sha, row_id))
            conn.commit()
        row["sha256"] = sha
    today = time.strftime("%Y-%m-%d", time.gmtime())
    if _scan_day["day"] != today:
        _scan_day.update(day=today, count=0)
    if _scan_day["count"] >= VIRUSTOTAL_DAILY:
        return  # today's free lookups are used up
    _scan_day["count"] += 1
    for attempt in range(3):
        try:
            result = await virustotal_lookup(row["sha256"])
            break
        except RuntimeError as exc:
            if str(exc) != "rate" or attempt == 2:
                raise
            await asyncio.sleep(60)
    if result is None:
        return
    malicious, suspicious, engines = result
    if malicious >= VIRUSTOTAL_MIN_ENGINES:
        await block_flagged_file(row, malicious, engines)
    elif malicious or suspicious:
        notify_admin("의심 파일 (차단 기준 미만)", f"악성 {malicious} · 의심 {suspicious} / 엔진 {engines}개",
                     flagged_file_text(row))


def flagged_file_text(row):
    guild = bot.get_guild(row["guild_id"]) if row["guild_id"] else None
    return "\n".join([
        f"올린 사람: <@{row['uploaded_by']}> · 디스코드 ID `{row['uploaded_by']}` · {user_name(row['uploaded_by']) or '(이름 모름)'}",
        f"서버: {guild.name if guild else '(알 수 없음)'} (`{row['guild_id']}`) · 채널 `{row['channel_id']}`",
        f"파일: {row['name']} · {human_size(row['size'])} · 올린 시각 <t:{int(row['uploaded_at'])}:F>",
        f"SHA-256: `{row['sha256']}`",
        f"VirusTotal: https://www.virustotal.com/gui/file/{row['sha256']}",
    ])


async def block_flagged_file(row, malicious, engines):
    """Take a flagged file out of reach but keep it as evidence: it moves to a new random name (old links
    stop working), its short link and song bar go, and only the admin panel can open or delete it.
    The uploader can't upload for a day; the admin is told who it was."""
    new_key = storage.new_key("quarantine", row["key"].rsplit("/", 1)[-1])
    moved = True
    try:
        await storage.copy(row["key"], new_key, row["key"].rsplit("/", 1)[-1], row["content_type"])
        await storage.delete(row_keys(row))
    except Exception as exc:
        moved = False
        print(f"악성 파일 옮기기 실패: {type(exc).__name__}: {exc}")
    await shortlinks.delete(row.get("url"))
    await delete_bar_message(row)
    scan = f"악성 {malicious} / 엔진 {engines}개"
    with connect_db() as conn:
        conn.execute("UPDATE files SET status = 'blocked', key = ?, url = '', player_key = NULL, scan = ?, "
                     "expires_at = ? WHERE id = ?",
                     (new_key if moved else row["key"], scan, time.time() + BLOCKED_KEEP_SECONDS, row["id"]))
        conn.commit()
    set_upload_ban(row["uploaded_by"], VIRUS_FREEZE_SECONDS, "악성 파일 업로드 (자동 1일 차단)")
    notify_admin("🦠 악성 파일을 차단했습니다", f"{scan} · 파일을 비활성화하고 올린 사람의 업로드를 1일 막았습니다."
                 + ("" if moved else " (저장소에서 옮기지 못해 원래 자리에 있습니다)"),
                 flagged_file_text(row) + "\n증거 파일은 NKOTE 관리자 패널의 '차단된 파일'에서 받을 수 있습니다 (90일 보관).")


async def remove_file(row):
    """Delete from storage and the list; False if storage refused (the row is kept so cleanup retries)."""
    if not await storage.delete(row_keys(row)):
        return False
    await shortlinks.delete(row.get("url"))
    await delete_bar_message(row)
    with connect_db() as conn:
        conn.execute("DELETE FROM files WHERE id = ?", (row["id"],))
        conn.commit()
    return True


def file_list_text(files, everyone):
    """List text; everyone=True (an admin) also shows who uploaded each file."""
    if not files:
        return None
    lines = [
        f"• **{discord.utils.escape_markdown(f['name'])}** · {human_size(f['size'])}"
        + (f" · <@{f['uploaded_by']}>" if everyone else "")
        + (" · 삭제 안 함" if kept_for_good(f["expires_at"]) else f" · 만료 <t:{int(f['expires_at'])}:f> (<t:{int(f['expires_at'])}:R>)")
        for f in files
    ]
    text = ""
    for i, line in enumerate(lines):
        if len(text) + len(line) > 3500:
            text += f"… 외 {len(lines) - i}개"
            break
        text += line + "\n"
    title = "이 서버에 올라간 파일 (관리자 보기)" if everyone else "내가 올린 파일"
    return f"**{title} {len(files)}개** · {human_size(sum(f['size'] for f in files))}\n{text}"


def files_disabled_reason(guild):
    if not storage.configured():
        return FILE_NOT_CONFIGURED
    if guild is None:
        return SERVER_ONLY_MESSAGE
    return None


@bot.command(name="올리기")
@commands.cooldown(1, FILE_COOLDOWN_SECONDS, commands.BucketType.user)
async def upload_command(ctx, *, name: str = ""):
    reason = files_disabled_reason(ctx.guild) or upload_blocked_reason(ctx.guild.id, ctx.author.id)
    if reason:
        ctx.command.reset_cooldown(ctx)
        await ctx.reply(reason, mention_author=False)
        return
    if ctx.message.attachments:
        await upload_attachments(ctx, ctx.message.attachments, name)
        return
    # No attachment: big files go through the web page, sent by DM so nobody else can use the link.
    try:
        row, link = await start_browser_upload(ctx.guild.id, ctx.channel.id, ctx.author.id, name)
    except storage.StorageError as exc:
        await ctx.reply(str(exc), mention_author=False)
        return
    try:
        on_button = len(link) <= LINK_BUTTON_MAX
        dm = await ctx.author.send(browser_upload_text(ctx.guild.id, None if on_button else link),
                                   view=browser_upload_view(row["id"], link))
    except discord.HTTPException:
        drop_file_row(row["id"])
        await ctx.reply("DM을 보낼 수 없습니다. 파일을 메시지에 첨부해서 `!올리기`를 하거나, "
                        "디스코드 설정 → 개인정보 보호에서 서버 멤버의 DM을 허용해 주세요.", mention_author=False)
        return
    watch_upload(row["id"], lambda message: dm.edit(content=message, view=None))
    await ctx.reply("DM으로 파일 올리는 링크를 보냈습니다. (작은 파일은 메시지에 첨부해서 `!올리기`)",
                    mention_author=False)


@bot.command(name="파일")
async def files_command(ctx, *, name: str = ""):
    reason = files_disabled_reason(ctx.guild)
    if reason:
        await ctx.reply(reason, mention_author=False)
        return
    if name:
        row = await find_file(ctx.guild.id, name, ctx.author)
        if not row:
            await ctx.reply(f"`{name}` 파일이 없습니다. `!파일`로 내 파일 목록을 확인해 주세요.", mention_author=False)
            return
        await send_files(ctx_sender(ctx), [row])
        return
    everyone = await is_file_admin(ctx.author)
    text = file_list_text(guild_files(ctx.guild.id) if everyone else guild_files(ctx.guild.id, ctx.author.id), everyone)
    if not text:
        await ctx.reply("올린 파일이 없습니다. 파일을 첨부해서 `!올리기`를 해 보세요.", mention_author=False)
        return
    embed = discord.Embed(description=text, color=discord.Color.green())
    embed.set_footer(text=f"!파일 <이름>으로 미리보기 · {retention_phrase()}")
    await ctx.reply(embed=embed, mention_author=False)


@bot.command(name="파일삭제")
async def delete_file_command(ctx, *, name: str = ""):
    reason = files_disabled_reason(ctx.guild)
    if reason:
        await ctx.reply(reason, mention_author=False)
        return
    if not name:
        await ctx.reply("사용법: `!파일삭제 <이름>`", mention_author=False)
        return
    row = await find_file(ctx.guild.id, name, ctx.author)
    if not row:
        await ctx.reply(f"`{name}` 파일이 없습니다. `!파일`로 내 파일 목록을 확인해 주세요.", mention_author=False)
        return
    if not await can_manage_file(ctx.author, row):
        await ctx.reply("자기가 올린 파일만 지울 수 있습니다.", mention_author=False)
        return
    if not await remove_file(row):
        await ctx.reply("지우지 못했습니다. 잠시 후 다시 시도해 주세요.", mention_author=False)
        return
    await ctx.reply(f"**{row['name']}** 파일을 삭제했습니다.", mention_author=False)


@upload_command.error
async def upload_command_error(ctx, error):
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.reply(f"{math.ceil(error.retry_after)}초 후에 다시 시도해 주세요.", mention_author=False)
    else:
        await report_error(ctx, error)


ORPHAN_SWEEP_SECONDS = 6 * 60 * 60
_last_orphan_sweep = [0.0]


async def sweep_orphans():
    """Remove objects under files/ that the bot's list doesn't know (older than 2 hours). B2's browser
    upload token is good for 24 hours and any file name, so this stops it being used to park other files."""
    _last_orphan_sweep[0] = time.time()
    with connect_db() as conn:
        known = stored_keys(conn)
    orphans = await storage.orphan_keys(known, min_age_seconds=2 * 60 * 60)
    if orphans and await storage.delete(orphans):
        print(f"목록에 없는 저장소 파일 {len(orphans)}개를 지웠습니다.")


@tasks.loop(minutes=10)
async def clean_files():
    """Delete files older than 7 days (and unfinished browser uploads) from storage and the list."""
    try:
        await refresh_short_links()
    except Exception as exc:
        print(f"단축 링크 새로 고침 오류: {type(exc).__name__}: {exc}")
    if time.time() - _last_orphan_sweep[0] > ORPHAN_SWEEP_SECONDS:
        try:
            await sweep_orphans()
        except Exception as exc:
            print(f"저장소 정리 오류: {type(exc).__name__}: {exc}")
    try:
        with connect_db() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT id, key, player_key, bar_channel_id, bar_message_id FROM files WHERE expires_at < ?",
                (time.time(),))]
        if not rows:
            return
        if await storage.delete([k for r in rows for k in row_keys(r)]):
            for row in rows:
                await delete_bar_message(row)
            with connect_db() as conn:
                conn.executemany("DELETE FROM files WHERE id = ?", [(r["id"],) for r in rows])
                conn.commit()
            print(f"만료된 파일 {len(rows)}개를 삭제했습니다.")
    except Exception as exc:
        print(f"파일 자동 삭제 오류: {type(exc).__name__}: {exc}")
        notify_admin("파일 자동 삭제 오류", f"{type(exc).__name__}: {exc}", "", traceback.format_exc())


@clean_files.before_loop
async def before_clean_files():
    await bot.wait_until_ready()


# ---------------------------------------------------------------- /도움말 (buttons)

HELP_CATEGORIES = {
    "uptime": ("🖥️", "서버 업타임", ["add", "uptime", "list", "delete"]),
    "stats": ("📊", "전적", ["hypixel", "valorant"]),
    "files": ("📁", "파일", ["upload", "files", "filedelete"]),
    "etc": ("🎲", "기타", ["fortune", "ilchim", "dday"]),
    "game": ("🎮", "게임", ["nara", "flag", "capital", "map", "anime", "song", "ranking"]),
}
# key -> (button label, command name, description shown on the help page and at the top of its input form)
HELP_COMMANDS = {
    "add": ("주소추가", "!주소추가", "`!주소추가 <주소> [이름]`\n서버를 추가합니다.\n예: `!주소추가 hypixel.net 하이픽셀`"),
    "uptime": ("업타임", "!업타임", "`!업타임 [이름]`\n등록된 모든 서버의 상태를 봅니다. 이름을 붙이면 서버 하나의 자세한 업타임을 봅니다.\n"
               "예: `!업타임 하이픽셀`"),
    "list": ("주소", "!주소", "`!주소`\n등록된 서버 목록을 봅니다."),
    "delete": ("주소삭제", "!주소삭제", "`!주소삭제 <이름>`\n서버와 업타임 기록을 삭제합니다. 서버 관리 권한이 필요합니다.\n"
               "예: `!주소삭제 하이픽셀`"),
    "hypixel": ("Hypixel 전적", "!H전적", "`!H전적 <닉네임> [bw|sw|duels]`\n하이픽셀 전적 카드를 봅니다. 게임을 붙이면 게임 별 전적을 봅니다.\n"
                "예: `!H전적 hypixel bw`"),
    "upload": ("올리기", "!올리기", "`!올리기 [이름]` + 파일 첨부 · 첨부 없이 쓰면 웹 업로드 링크\n"
               "**🌐 올리기**를 누르면 업로드 페이지가 바로 열리고, 올리면 **자동으로 채널에 올라갑니다**.\n"
               "사진은 바로 보이고 영상은 채팅창에서 바로 재생되며, 노래는 **바 형태**(빠르게)·**영상 형태** 중 고를 수 있습니다. 작은 파일은 **📎 첨부**로도 올릴 수 있습니다.\n"
               "올린 파일은 봇의 파일 저장소에 올라가며 {보관}."),
    "files": ("목록", "!파일", "`!파일 [이름]`\n내가 올린 파일 목록을 봅니다. 이름을 붙이면 그 파일을 미리보기로 보냅니다."),
    "filedelete": ("삭제", "!파일삭제", "`!파일삭제 <이름>`\n내가 올린 파일을 지웁니다."),
    "fortune": ("운세", "!운세", "`!운세`\n오늘의 운세를 봅니다. 칠 때마다 무작위로 나옵니다."),
    "ilchim": ("일침", "!일침", "`!일침`\n정신이 번쩍 드는 한마디를 듣습니다. 칠 때마다 무작위로 나옵니다."),
    "dday": ("D-Day", "!dday", "`!dday [이름]`\n이 서버의 D-Day 목록을 보거나, 이름을 붙이면 그 날까지 남은 시간을 봅니다.\n"
             "**➕ 만들기**로 날짜·문구·자동 알림을 정하고, 만든 뒤 **🎉 축하 문구**(당일 문구)도 정할 수 있습니다. "
             "내가 만든 D-Day는 **✏️ 편집**·**🗑️ 삭제**할 수 있습니다.\n"
             "문구 칸: `{이름}` `{D}` `{년}` `{월}` `{일}` `{시}` `{분}` `{초}` (예: `{이름}까지 {일} 남았습니다`)"),
    "ranking": ("순위", "!순위",
                "`!xp [이름 또는 @멘션]` 내(또는 그 사람) XP · `!순위` · `!순위 전체`(켜진 서버만)\n"
                "모든 게임을 합친 **XP 순서**예요. 🌍 나라(한 판)·🏛️ 수도·🗺️ 지도는 1 XP, "
                "🚩 국기·🎌 애니캐·🎵 노래는 0.5 XP. 아래 **🏆 전체**는 서버 순서, **👤 개인**은 고른 사람의 게임별 기록 "
                "(📢 채팅방에 보내기로 공유).\n"
                "🎲 각 게임의 **배팅 판** 버튼(또는 `!국기시작 보통 배팅`)으로 XP를 걸고 겨룰 수 있어요."),
    "nara": ("나라 맞히기", "!나라시작",
             "봇이 몰래 고른 나라를 채널의 모두가 함께 맞힙니다.\n\n"
             "**명령어**\n"
             "`!나라시작 [쉬움|보통|어려움]` 새 판 (진행 중이면 이어서)\n"
             "`!추측 <나라>` 추측 · `!포기` 정답 공개\n"
             "`!나라출제` 내가 고른 나라를 다른 사람들이 맞히게 하기 (순위에는 안 들어감)\n"
             "`!나라순위` 순위 · `!정보 <나라>` 나라 정보\n\n"
             "**난이도**\n"
             "🟢 쉬움 — 잘 알려진 나라 · 횟수 무제한\n"
             "🌍 보통 — 모든 나라 · 횟수 무제한\n"
             "🔥 어려움 — 덜 알려진 나라 · **10번 안에**\n\n"
             "**표 보는 법**\n"
             "🟩 같음 · 🟥 다름 · 🟨 비슷함\n"
             "▲ 정답이 더 큼 · ▼ 정답이 더 작음\n"
             "거리·방향은 추측한 나라 수도에서 정답 나라 수도까지\n"
             "어려움은 이웃·평균 기온 칸이 나오고 거리·방향은 없습니다."),
    "flag": ("국기 맞히기", "!국기시작",
             "국기를 보고 나라를 맞힙니다. **가장 먼저 맞힌 사람**의 개수가 기록됩니다.\n\n"
             "**명령어**\n"
             "`!국기시작 [쉬움|보통|어려움] [문제 수]` 시작 (기본 10문제, 최대 50)\n"
             "`!국기 <나라>` 답하기 · `!힌트` 초성\n"
             "`!국기스킵` 넘기기 · `!국기중지` 그만 · `!국기순위` 순위\n\n"
             "**난이도**\n"
             "🟢 쉬움 — 잘 알려진 나라\n"
             "🌍 보통 — 모든 나라\n"
             "🔥 어려움 — 덜 알려진 나라 · 비슷한 국기가 자주 나옴\n\n"
             "**규칙**\n"
             "시간·횟수 제한 없음 · 틀리면 ❌ · 한발 늦으면 ⏱️"),
    "capital": ("수도 맞히기", "!수도시작",
                "나라를 보고 수도를 맞힙니다. **가장 먼저 맞힌 사람**의 개수가 기록됩니다.\n\n"
                "**명령어**\n"
                "`!수도시작 [쉬움|보통|어려움] [문제 수]` 시작 (기본 10문제, 최대 50)\n"
                "`!수도 <수도>` 답하기 · `!힌트` 초성\n"
                "`!수도스킵` 넘기기 · `!수도중지` 그만 · `!수도순위` 순위\n\n"
                "**난이도**\n"
                "🟢 쉬움 — 잘 알려진 나라\n"
                "🌍 보통 — 모든 나라\n"
                "🔥 어려움 — 덜 알려진 나라\n\n"
                "**규칙**\n"
                "수도 이름은 정확히 (흔한 다른 표기·영어도 정답)\n"
                "수도가 여럿인 나라는 그중 하나면 정답\n"
                "시간·횟수 제한 없음 · 틀리면 ❌ · 한발 늦으면 ⏱️"),
    "map": ("지도 맞히기", "!지도시작",
            "지도를 보고 나라를 맞힙니다. **가장 먼저 맞힌 사람**의 개수가 기록됩니다.\n\n"
            "**명령어**\n"
            "`!지도시작 [쉬움|보통|어려움] [문제 수]` 시작 (기본 10문제, 최대 50)\n"
            "`!지도 <나라>` 답하기 · `!힌트` 초성\n"
            "`!지도스킵` 넘기기 · `!지도중지` 그만 · `!지도순위` 순위\n\n"
            "**난이도**\n"
            "🟢 쉬움 — 잘 알려진 나라 · 주변이 보이는 지도에 빨간색\n"
            "🌍 보통 — 모든 나라 · 주변이 보이는 지도에 빨간색\n"
            "🔥 어려움 — 덜 알려진 나라 · **나라 모양(실루엣)만**\n\n"
            "**규칙**\n"
            "아주 작은 나라는 빨간 동그라미로 표시 · 시간·횟수 제한 없음 · 틀리면 ❌ · 한발 늦으면 ⏱️"),
    "anime": ("애니캐 맞히기", "!캐릭시작",
              "애니 캐릭터 그림을 보고 이름을 맞힙니다. **가장 먼저 맞힌 사람**의 개수가 기록됩니다.\n\n"
              "**명령어**\n"
              "`!캐릭시작 [쉬움|보통] [문제 수]` 시작 (기본 10문제, 최대 50)\n"
              "`!캐릭 <이름>` 답하기 · `!힌트` 작품 → 한 번 더 치면 초성\n"
              "`!캐릭스킵` 넘기기 · `!캐릭중지` 그만 · `!캐릭순위` 순위\n"
              "`!캐릭설정` 이 서버에서 나올 작품 고르기 (DM으로 10분짜리 링크)\n"
              "`!애니 리스트` 작품 목록 · `!애니 추천 <작품>` 넣고 싶은 작품 추천\n\n"
              "**난이도**\n"
              "🟢 쉬움 — 누구나 아는 대표 캐릭터\n"
              "🌍 보통 — 66개 작품의 캐릭터 약 750명\n\n"
              "**규칙**\n"
              "이름만 써도 정답 (예: `탄지로`) · 영어·일본어 이름도 정답\n"
              "시간·횟수 제한 없음 · 틀리면 ❌ · 한발 늦으면 ⏱️\n"
              "-# 그림·캐릭터 정보: AniList"),
    "song": ("노래 맞히기", "!노래시작",
             "노래 일부분을 듣고 제목을 맞힙니다. **가장 먼저 맞힌 사람**의 개수가 기록됩니다.\n\n"
             "**명령어**\n"
             "`!노래시작 [쉬움|보통|어려움] [장르] [문제 수]` 시작 (기본 10문제, 최대 50)\n"
             "`!노래 <제목>` 답하기 · `!힌트` 가수 → 한 번 더 치면 초성\n"
             "`!노래스킵` 넘기기 · `!노래중지` 그만 · `!노래순위` 순위\n"
             "`!노래추가 <가수와 제목> [| 다른 이름]` 이 서버에 곡 추가 · `!노래목록` · `!노래삭제 <번호>`\n\n"
             "**난이도** (들려주는 길이)\n"
             "🟢 쉬움 — 10초 · 🌍 보통 — 5초 · 🔥 어려움 — 2초\n\n"
             "**장르** (안 쓰면 전부)\n"
             "`케이팝` · `제이팝`(애니 포함) · `팝송` · `추가곡`(이 서버에서 추가한 곡만)\n\n"
             "**규칙**\n"
             "제목만 맞히면 정답 (한글·영어·흔한 다른 표기) · 틀리면 ❌ · 한발 늦으면 ⏱️\n"
             "-# 노래는 Deezer·Apple Music의 공식 30초 미리듣기 중 일부입니다. 정답이 나오면 30초를 재생할 수 있습니다"),
    "valorant": ("Valorant 전적", "!V전적", "`!V전적 <이름#태그> [지역]`\n발로란트 전적 카드를 봅니다. 지역은 자동으로 찾습니다.\n"
                 "`!V전적 <이름#태그> 지역`으로도 검색할 수 있습니다."),
}
# Linked rather than uploaded: uploading the picture with every response could take longer than the 3 seconds
# Discord allows, giving "응답하지 않았어요". The file lives in this repo (assets/images/help_banner.jpg).
HELP_BANNER_URL = os.getenv(
    "HELP_BANNER_URL", "https://raw.githubusercontent.com/nkxwert/discord-bot/main/assets/images/help_banner.jpg"
).strip()
# Icons left of the stats buttons: help command key -> image, uploaded once as the app's own emojis
# (usable in every server without being added there). Filled by load_help_emojis() at startup.
HELP_EMOJI_FILES = {"hypixel": "hypixel.png", "valorant": "valorant.png"}
HELP_EMOJI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "emojis")
help_emojis = {}
HELP_BUTTON_EMOJIS = {"ranking": "🏆", "fortune": "🔮", "ilchim": "💢", "dday": "📅", "nara": "🌍", "flag": "🚩", "capital": "🏛️", "map": "🗺️", "anime": "🎌", "song": "🎵", "upload": "📤", "files": "📄",
                      "filedelete": "🗑️"}  # plain emoji for command buttons without an uploaded icon


async def load_help_emojis():
    """Find (or upload, the first time) the button icons. Without them the buttons just have no icon."""
    try:
        existing = {e.name: e for e in await bot.fetch_application_emojis()}
        for key, filename in HELP_EMOJI_FILES.items():
            name = f"maco_{key}"
            if name not in existing:
                with open(os.path.join(HELP_EMOJI_DIR, filename), "rb") as f:
                    existing[name] = await bot.create_application_emoji(name=name, image=f.read())
            help_emojis[key] = existing[name]
    except (discord.HTTPException, discord.errors.MissingApplicationID, OSError) as exc:
        print(f"버튼 아이콘 준비 실패: {type(exc).__name__}: {exc}")


class HelpContext:
    """Just enough of commands.Context for the ! command code to run from a /도움말 button.
    Replies are plain public messages in the channel, starting with a small line naming who asked.
    (A follow-up to the private help message would show "원본 메시지가 삭제되었어요" once that is closed.)"""

    def __init__(self, interaction, what):
        self.interaction = interaction
        self.guild = interaction.guild
        self.author = interaction.user
        self.channel = interaction.channel
        self.message = type("HelpMessage", (), {"content": f"/도움말 → {what}"})()  # for describe_ctx
        self.notice = None
        self.header = f"-# {interaction.user.mention}님의 요청 · {what}"  # mentions never ping (allowed_mentions)

    def with_header(self, content):
        return f"{self.header}\n{content}" if content else self.header

    async def reply(self, content=None, *, embed=None, file=None, view=None, mention_author=None):
        extra = {k: v for k, v in {"embed": embed, "file": file, "view": view}.items() if v is not None}
        content = self.with_header(content)
        try:
            if missing_permissions(self.channel):
                raise PermissionError  # Discord would drop the embed or refuse the file: answer privately
            message = await self.channel.send(content, **extra)
        except (discord.HTTPException, AttributeError, PermissionError):
            # No permission to post in this channel (or no channel object): answer through the interaction.
            if file is not None:
                file.reset()
            message = await self.interaction.followup.send(content, wait=True, **extra)
        return HeaderMessage(message, self)


class RequestContext:
    """A ! command whose results are plain channel messages under "-# @누구님의 요청 · 무엇" instead of
    replies: a reply shows "원본 메시지가 삭제되었어요" once the command message is deleted (stats take a
    few seconds, and some servers clean up command messages)."""

    def __init__(self, ctx, what):
        self._ctx = ctx
        self.guild, self.author, self.channel, self.message = ctx.guild, ctx.author, ctx.channel, ctx.message
        self.header = f"-# {ctx.author.mention}님의 요청 · {what}"  # mentions never ping (allowed_mentions)

    # The waiting notice is kept on the real context too, so the command's error handler can replace it.
    @property
    def notice(self):
        return getattr(self._ctx, "notice", None)

    @notice.setter
    def notice(self, value):
        self._ctx.notice = value

    def with_header(self, content):
        return f"{self.header}\n{content}" if content else self.header

    async def reply(self, content=None, *, embed=None, file=None, view=None, mention_author=None):
        extra = {k: v for k, v in {"embed": embed, "file": file, "view": view}.items() if v is not None}
        return HeaderMessage(await self.channel.send(self.with_header(content), **extra), self)


class HeaderMessage:
    """A sent message whose later edits (progress -> result) keep the "누구의 요청" line on top."""

    def __init__(self, message, ctx):
        self._message = message
        self._ctx = ctx

    def __getattr__(self, name):
        return getattr(self._message, name)

    async def edit(self, **changes):
        if "content" in changes:
            changes["content"] = self._ctx.with_header(changes["content"])
        await self._message.edit(**changes)
        return self


async def run_from_help(ctx, awaitable, channel_only=False):
    """Run a ! command's code for a /도움말 button. channel_only: it posts straight into the channel (games),
    so it can't fall back to a private answer and needs the bot's channel permissions first."""
    missing = missing_permissions(ctx.channel) if channel_only else []
    if missing:
        awaitable.close()
        await tell_missing_permissions(ctx, missing)
        return
    try:
        await awaitable
    except Exception as exc:
        await report_error(ctx, exc)


def stats_wait_left(command, ctx):
    """Spend the ! command's cooldown (shared with the button) and return the seconds left if it was in use."""
    retry_after = command._buckets.update_rate_limit(ctx)
    return math.ceil(retry_after) if retry_after else 0


def help_message(page="home", selected=None, notice=None, details=None, extra_items=(), upload_url=None,
                 upload_pending=False, guild_id=None):
    """Embed and buttons for one page of the private /도움말 message.
    Everything private (lists, notices) is shown by editing this one message; only results meant
    for the channel (uptime, stats cards, ...) become separate public messages."""
    color = discord.Color.green()
    view = discord.ui.View(timeout=None)

    def button(action, label, emoji=None, style=discord.ButtonStyle.primary, row=0):
        view.add_item(HelpButton(action, label, emoji, style, row))

    if page == "commands":
        embed = command_list_embed(guild_id)
        button("home", "◀ 처음으로", style=discord.ButtonStyle.secondary)
        return {"embed": embed, "view": view}
    if page == "home":
        embed = discord.Embed(description=notice, color=color)
        if HELP_BANNER_URL:
            embed.set_image(url=HELP_BANNER_URL)
        else:
            embed.title = "Maco"
        for key, (emoji, title, _) in HELP_CATEGORIES.items():
            if key == "uptime" and not uptime_enabled(guild_id):
                continue  # switched off for this server in the admin panel
            button(f"page_{key}", title, emoji)
        # A row holds 5 buttons: categories on the first, the rest on the second.
        button("dm", "DM to Dev.", "✉️", row=1)
        button("commands", "명령어 보기", "📋", row=1)
        view.add_item(discord.ui.Button(label="서비스 상태", emoji="🟢", url=STATUS_PAGE_URL, row=1))
        view.add_item(discord.ui.Button(label="서포트 센터", emoji="💬", url=SUPPORT_SERVER_URL, row=1))
        return {"embed": embed, "view": view}

    emoji, title, keys = HELP_CATEGORIES[page]
    keys = [k for k in keys if game_enabled(k, guild_id)]  # switched-off games have no button
    if selected:
        _, command, _ = HELP_COMMANDS[selected]
        text = help_text(selected)
        title = f"{emoji} {title} · {command}"
    else:
        title, text = f"{emoji} {title}", "버튼을 누르면 바로 사용할 수 있습니다."
    for extra in (details, notice):
        if extra:
            text += f"\n\n{extra}"
    for index, key in enumerate(keys):
        style = discord.ButtonStyle.success if key == selected else discord.ButtonStyle.secondary
        if key == "upload" and upload_url:
            # A link button: one press opens the upload page; the file is posted automatically afterwards.
            view.add_item(discord.ui.Button(label="올리기", emoji="🌐", url=upload_url, row=0))
        elif key == "upload" and upload_pending:
            # Shown at once while the link is made (B2 + NKOTE can take a few seconds), so the press shows.
            view.add_item(discord.ui.Button(label="준비 중...", emoji="⏳", disabled=True, row=0,
                                            custom_id="maco:help:wait"))
        else:
            # A row holds 5 buttons: a 6th command (the 게임 tab) goes on the next row, beside ◀ 처음으로.
            button(f"run_{key}", HELP_COMMANDS[key][0], help_emojis.get(key) or HELP_BUTTON_EMOJIS.get(key), style,
                   row=index // 5)
        if key == "upload":
            button("attach", "첨부", "📎", discord.ButtonStyle.secondary)
    button("home", "◀ 처음으로", style=discord.ButtonStyle.secondary, row=1)
    for item in extra_items:
        view.add_item(item)
    embed = discord.Embed(title=title, description=text[:4096], color=color)
    if page == "files" and guild_id and storage.configured():
        quota, used = guild_quota(guild_id), file_usage(guild_id)
        space = "용량 한도 없음" if quota == UNLIMITED else f"남은 용량 {human_size(max(0, quota - used))} / {human_size(quota)}"
        embed.set_footer(text=f"이 서버 · {space} · 파일당 최대 {max_file_text(guild_id)}")
    return {"embed": embed, "view": view}


def help_text(key):
    kept = ("**자동으로 삭제되지 않습니다**" if is_forever(file_retention())
            else f"**{retention_text()} 후 자동으로 삭제**됩니다")
    return HELP_COMMANDS[key][2].replace("{보관}", kept)


def help_page_of(key):
    return next(page for page, (_, _, keys) in HELP_CATEGORIES.items() if key in keys)


async def show_help(interaction, **page):
    """Redraw the /도움말 message the interaction came from (a button, or a form opened from one).
    The 파일 page gets a ready upload link on its [올리기] button. Making a new link can take a few
    seconds, so the page is shown at once with [⏳ 준비 중...] and the link is filled in afterwards
    (waiting first made the 파일 button look like it did nothing)."""
    async def draw(**changes):
        if interaction.response.is_done():
            await interaction.edit_original_response(**changes)
        else:
            await interaction.response.edit_message(**changes)

    if interaction.guild:
        page.setdefault("guild_id", interaction.guild.id)
    wants_link = (page.get("page") == "files" and "upload_url" not in page and interaction.guild
                  and storage.configured())
    if wants_link:
        cached = cached_upload_link(interaction)
        if cached:
            page["upload_url"] = cached["url"] if len(cached["url"]) <= LINK_BUTTON_MAX else None
        else:
            await draw(**help_message(**page, upload_pending=True))
            try:
                _, url = await upload_link_for(interaction)
                if len(url) <= LINK_BUTTON_MAX:
                    page["upload_url"] = url
            except storage.StorageError as exc:  # e.g. the server's space is full
                page["notice"] = "\n".join(filter(None, [page.get("notice"), f"⚠️ {exc}"]))
            except Exception as exc:
                print(f"업로드 링크 만들기 실패: {type(exc).__name__}: {exc}")
    await draw(**help_message(**page))


# A DynamicItem keeps working after a restart (and on any copy of the bot): the custom_id says what the
# button does, so nothing has to be remembered in memory. That was the cause of "응답하지 않았어요".
class HelpButton(discord.ui.DynamicItem[discord.ui.Button], template=r"maco:help:(?P<action>\w+)"):
    def __init__(self, action, label="?", emoji=None, style=discord.ButtonStyle.secondary, row=0):
        super().__init__(discord.ui.Button(
            label=label, emoji=emoji, style=style, row=row, custom_id=f"maco:help:{action}",
        ))
        self.action = action

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["action"])

    async def callback(self, interaction):
        handler = HELP_ACTIONS.get(self.action)
        if handler is None:
            return
        try:
            await handler(interaction)
        except Exception as exc:
            trace = traceback.format_exc()
            print(f"도움말 버튼 오류 ({self.action}):\n{trace}")
            notify_admin("도움말 버튼 오류", f"{type(exc).__name__}: {exc}",
                         describe_interaction(interaction, f"버튼: {self.action}"), trace)
            send = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
            try:
                await send("오류가 발생했습니다.", ephemeral=True)
            except discord.HTTPException:
                pass


async def help_open_page(interaction, page):
    if page == "uptime" and interaction.guild is None:
        await show_help(interaction, notice=SERVER_ONLY_MESSAGE)
        return
    if page == "uptime" and not uptime_enabled(interaction.guild.id):
        await show_help(interaction, notice=UPTIME_OFF_MESSAGE)
        return
    if page == "files" and files_disabled_reason(interaction.guild):
        await show_help(interaction, notice=files_disabled_reason(interaction.guild))
        return
    await show_help(interaction, page=page)


async def help_dm_to_dev(interaction):
    left = feedback_wait_left(interaction.user.id)
    if left:
        await show_help(interaction, notice=f"{left}초 후에 다시 보낼 수 있습니다.")
        return
    await interaction.response.send_modal(HelpFeedbackModal())


async def help_run_uptime(interaction):
    if not guild_servers(interaction.guild.id):
        await show_help(interaction, page="uptime", selected="uptime", notice=NO_SERVER_MESSAGE)
        return
    await show_help(interaction, page="uptime", selected="uptime")
    ctx = HelpContext(interaction, "업타임")
    await run_from_help(ctx, uptime.callback(ctx))


async def help_run_list(interaction):
    servers = guild_servers(interaction.guild.id)
    if not servers:
        await show_help(interaction, page="uptime", selected="list", notice=NO_SERVER_MESSAGE)
        return
    lines = "\n".join(f"• **{s['name']}** — `{s['address']}`" for s in servers)
    await show_help(interaction, page="uptime", selected="list", details=f"**등록된 서버**\n{lines}")


async def help_run_delete(interaction):
    if not interaction.user.guild_permissions.manage_guild:
        await show_help(interaction, page="uptime", selected="delete", notice="서버 관리 권한이 필요합니다.")
        return
    servers = guild_servers(interaction.guild.id)
    if not servers:
        await show_help(interaction, page="uptime", selected="delete", notice=NO_SERVER_MESSAGE)
        return
    await interaction.response.send_modal(DeleteServerModal(servers))


# A link works for an hour (the pending upload, its short link and the watcher all last that long), so it is
# reused for 45 minutes: fewer new links (each one asks B2 and NKOTE) and fewer watchers running at once.
UPLOAD_LINK_REUSE_SECONDS = 45 * 60
_upload_links = {}  # (guild id, user id) -> {"row": row id, "url": link, "at": time}
_link_interactions = {}  # row id -> the latest /도움말 interaction showing that link (to update it when done)


def cached_upload_link(interaction):
    """The person's recent unused upload link, so moving around the menu doesn't start a new upload
    (and a new background check) every time."""
    entry = _upload_links.get((interaction.guild.id, interaction.user.id))
    if not entry or time.time() - entry["at"] > UPLOAD_LINK_REUSE_SECONDS:
        return None
    with connect_db() as conn:
        row = conn.execute("SELECT status FROM files WHERE id = ?", (entry["row"],)).fetchone()
    return entry if row and row["status"] == "pending" else None


async def upload_link_for(interaction):
    """(row id, link) of an upload link for this person, new or reused; a new one is watched in the
    background so the file is posted as soon as it arrives."""
    entry = cached_upload_link(interaction)
    if entry is None:
        row, url = await start_browser_upload(interaction.guild.id, interaction.channel.id, interaction.user.id)
        entry = {"row": row["id"], "url": url, "at": time.time()}
        _upload_links[(interaction.guild.id, interaction.user.id)] = entry
        watch_upload(row["id"], lambda message, row_id=row["id"]: link_upload_finished(row_id, message))
    _link_interactions[entry["row"]] = interaction
    return entry["row"], entry["url"]


async def link_upload_finished(row_id, message):
    """Show the outcome on the /도움말 message that last showed this link (its [올리기] gets a fresh link
    the next time the 파일 page is opened)."""
    for key in [k for k, v in _upload_links.items() if v["row"] == row_id]:
        del _upload_links[key]
    interaction = _link_interactions.pop(row_id, None)
    if interaction:
        await interaction.edit_original_response(**help_message(page="files", selected="upload", notice=message,
                                                                 guild_id=interaction.guild.id))


async def help_run_upload(interaction):
    """[올리기] as a plain button: shown when the link didn't fit a link button, or after an upload finished."""
    await interaction.response.edit_message(**help_message(page="files", selected="upload", upload_pending=True,
                                                                guild_id=interaction.guild.id))
    try:
        row_id, url = await upload_link_for(interaction)
    except storage.StorageError as exc:
        await show_help(interaction, page="files", selected="upload", upload_url=None, notice=f"⚠️ {exc}")
        return
    if len(url) <= LINK_BUTTON_MAX:
        await show_help(interaction, page="files", selected="upload", upload_url=url)
    else:
        await show_help(interaction, page="files", selected="upload", details=browser_upload_text(interaction.guild.id, url),
                        upload_url=None, extra_items=[UploadDoneButton(row_id)])


async def help_run_files(interaction):
    everyone = await is_file_admin(interaction.user)
    files = guild_files(interaction.guild.id) if everyone else guild_files(interaction.guild.id, interaction.user.id)
    text = file_list_text(files, everyone)
    await show_help(interaction, page="files", selected="files",
                    details=text, notice=None if text else "올린 파일이 없습니다.")


async def help_run_filedelete(interaction):
    files = await visible_files(interaction.guild.id, interaction.user)
    if not files:
        await show_help(interaction, page="files", selected="filedelete", notice="올린 파일이 없습니다.")
        return
    await interaction.response.send_modal(FileDeleteModal(files))


def help_run_random_line(key):
    async def handler(interaction):
        await show_help(interaction, page="etc", selected=key)
        await HelpContext(interaction, HELP_COMMANDS[key][0]).reply(random_line(key))
    return handler


def help_open_form(modal_class):
    async def handler(interaction):
        await interaction.response.send_modal(modal_class())
    return handler


class HelpFeedbackModal(FeedbackModal):
    """The developer form opened from /도움말; the outcome replaces the help message instead of a new one."""

    async def on_submit(self, interaction):
        await interaction.response.defer()  # photos can take a while; keeps the help message as it is
        kind = self.kind.values[0] if self.kind.values else "기타"
        photos = await collect_photos(self.photos.values)
        message = await finish_feedback(
            interaction.user, f"[{kind}] {self.subject.value}", self.body.value,
            describe_place(interaction.guild, interaction.channel), photos,
        )
        await show_help(interaction, notice=message)


class HelpModal(SafeModal):
    """Input form opened from a /도움말 button, with the command's description at the top."""

    key = ""

    def __init__(self):
        label = HELP_COMMANDS[self.key][0]
        super().__init__(title=label)
        self.add_item(discord.ui.TextDisplay(help_text(self.key)))

    async def reject(self, interaction, notice):
        """Show why the input can't be used on the help message, without posting anything."""
        await show_help(interaction, page=help_page_of(self.key), selected=self.key, notice=notice)

    async def finish(self, interaction):
        """Show this command's description on the help message, then the result follows publicly."""
        await show_help(interaction, page=help_page_of(self.key), selected=self.key)
        return HelpContext(interaction, HELP_COMMANDS[self.key][0])


class AddServerModal(HelpModal):
    key = "add"

    def __init__(self):
        super().__init__()
        self.address = discord.ui.TextInput(max_length=253, placeholder="hypixel.net")
        self.server_name = discord.ui.TextInput(required=False, max_length=MAX_NAME_LENGTH, placeholder="하이픽셀 (비우면 주소)")
        self.add_item(discord.ui.Label(text="주소", component=self.address))
        self.add_item(discord.ui.Label(text="이름 (선택)", component=self.server_name))

    async def on_submit(self, interaction):
        try:
            address, name = check_new_server(interaction.guild.id, self.address.value, self.server_name.value)
        except ValueError as exc:
            await self.reject(interaction, str(exc))
            return
        ctx = await self.finish(interaction)
        await run_from_help(ctx, register_server(ctx, address, name))


class DeleteServerModal(HelpModal):
    key = "delete"

    def __init__(self, servers):
        super().__init__()
        self.server = discord.ui.Select(options=[
            discord.SelectOption(label=s["name"][:100], description=s["address"][:100], value=str(s["id"]))
            for s in servers
        ])
        self.add_item(discord.ui.Label(text="삭제할 서버", component=self.server))

    async def on_submit(self, interaction):
        if not interaction.user.guild_permissions.manage_guild:
            await self.reject(interaction, "서버 관리 권한이 필요합니다.")
            return
        chosen = self.server.values[0] if self.server.values else None
        server = next((s for s in guild_servers(interaction.guild.id) if str(s["id"]) == chosen), None)
        if not server:
            await self.reject(interaction, "이미 삭제된 서버입니다.")
            return
        remove_server(server["id"])
        ctx = await self.finish(interaction)
        await ctx.reply(f"**{server['name']}** 서버와 업타임 기록을 삭제했습니다.")


class HypixelModal(HelpModal):
    key = "hypixel"
    GAMES = [("전체", "all"), ("베드워즈 (bw)", "bedwars"), ("스카이워즈 (sw)", "skywars"), ("듀얼 (duels)", "duels")]

    def __init__(self):
        super().__init__()
        self.player = discord.ui.TextInput(max_length=16, placeholder="hypixel")
        self.game = discord.ui.Select(options=[
            discord.SelectOption(label=label, value=value, default=value == "all") for label, value in self.GAMES
        ])
        self.add_item(discord.ui.Label(text="닉네임", component=self.player))
        self.add_item(discord.ui.Label(text="유형", component=self.game))

    async def on_submit(self, interaction):
        ctx = HelpContext(interaction, "Hypixel 전적")
        left = stats_wait_left(hypixel_stats, ctx)
        if left:
            await self.reject(interaction, f"{left}초 후에 다시 시도해 주세요.")
            return
        game = self.game.values[0] if self.game.values else "all"
        ctx = await self.finish(interaction)
        await run_from_help(ctx, run_hypixel(ctx, self.player.value.strip(), None if game == "all" else game))


class ValorantModal(HelpModal):
    key = "valorant"

    def __init__(self):
        super().__init__()
        self.riot_id = discord.ui.TextInput(max_length=40, placeholder="snowe #UIpop")
        self.add_item(discord.ui.Label(text="이름#태그 (지역은 선택)", component=self.riot_id))

    async def on_submit(self, interaction):
        try:
            name, tag, region = stats.parse_riot_id(self.riot_id.value)
        except stats.StatsError as exc:
            await self.reject(interaction, str(exc))
            return
        ctx = HelpContext(interaction, "Valorant 전적")
        left = stats_wait_left(valorant_stats, ctx)
        if left:
            await self.reject(interaction, f"{left}초 후에 다시 시도해 주세요.")
            return
        ctx = await self.finish(interaction)
        await run_from_help(ctx, run_valorant(ctx, name, tag, region))


class UploadModal(HelpModal):
    key = "upload"

    def __init__(self):
        super().__init__()
        self.files = discord.ui.FileUpload(max_values=FILE_MAX_PER_MESSAGE)
        self.file_name = discord.ui.TextInput(required=False, max_length=MAX_NAME_LENGTH, placeholder="비우면 파일 이름")
        self.add_item(discord.ui.Label(text=f"파일 (최대 {FILE_MAX_PER_MESSAGE}개)", component=self.files))
        self.add_item(discord.ui.Label(text="이름 (선택, 파일 1개일 때)", component=self.file_name))

    async def on_submit(self, interaction):
        if not self.files.values:
            await self.reject(interaction, "파일을 골라 주세요.")
            return
        ctx = HelpContext(interaction, "올리기")
        left = stats_wait_left(upload_command, ctx)
        if left:
            await self.reject(interaction, f"{left}초 후에 다시 시도해 주세요.")
            return
        ctx = await self.finish(interaction)
        await run_from_help(ctx, upload_attachments(ctx, self.files.values, self.file_name.value))


class FileDeleteModal(HelpModal):
    key = "filedelete"

    def __init__(self, files):
        super().__init__()
        self.file = discord.ui.Select(options=[
            discord.SelectOption(label=f["name"][:100], value=str(f["id"]),
                                 description=f"{human_size(f['size'])} · {datetime.fromtimestamp(f['uploaded_at']):%m/%d %H:%M} 올림")
            for f in files[:25]
        ])
        self.add_item(discord.ui.Label(text="삭제할 파일", component=self.file))

    async def on_submit(self, interaction):
        chosen = self.file.values[0] if self.file.values else None
        row = next((f for f in guild_files(interaction.guild.id) if str(f["id"]) == chosen), None)
        if not row:
            await self.reject(interaction, "이미 삭제된 파일입니다.")
            return
        if not await can_manage_file(interaction.user, row):
            await self.reject(interaction, "자기가 올린 파일만 지울 수 있습니다.")
            return
        if not await remove_file(row):
            await self.reject(interaction, "지우지 못했습니다. 잠시 후 다시 시도해 주세요.")
            return
        ctx = await self.finish(interaction)
        await ctx.reply(f"**{row['name']}** 파일을 삭제했습니다.")


# ---------------------------------------------------------------- D-Day

DDAY_MAX_PER_GUILD = 25  # also what one select menu can list
DDAY_MAX_PER_USER = 10
DDAY_NAME_LENGTH = 30


def guild_ddays(guild_id, user_id=None):
    with connect_db() as conn:
        if user_id is None:
            rows = conn.execute("SELECT * FROM ddays WHERE guild_id = ? ORDER BY target", (guild_id,))
        else:
            rows = conn.execute("SELECT * FROM ddays WHERE guild_id = ? AND created_by = ? ORDER BY target",
                                (guild_id, user_id))
        return [dict(r) for r in rows]


def find_dday(guild_id, name):
    key = name.strip().lower()
    return next((d for d in guild_ddays(guild_id) if d["name"].lower() == key), None)


def dday_target(row):
    return datetime.fromtimestamp(row["target"], dday.KST)


def dday_message(row):
    count_up = bool(row["count_up"])  # the date was already past when it was saved: counts the days since
    default = dday.COUNT_UP_TEMPLATE if count_up else dday.DEFAULT_TEMPLATE
    return dday.render(row["template"] or default, row["name"], dday_target(row), count_up=count_up,
                       celebrate=row.get("celebrate"))


def dday_line(row, mine=False):
    left = dday.days_left(dday_target(row))
    line = (f"• **{discord.utils.escape_markdown(row['name'])}** · `{dday.d_label(left)}` · "
            f"{dday.target_text(dday_target(row), row['has_time'])}")
    if row["alert_time"] and row["next_alert"]:
        line += f" · 🔔 {row['alert_time']} <#{row['channel_id']}>"
    return line + (" · ✏️ 내 D-Day" if mine else f" · <@{row['created_by']}>")


def dday_list_text(guild_id, user_id=None, limit=3500):
    rows = guild_ddays(guild_id)
    if not rows:
        return None
    text = ""
    for i, row in enumerate(rows):
        line = dday_line(row, mine=row["created_by"] == user_id)
        if len(text) + len(line) > limit:
            text += f"… 외 {len(rows) - i}개"
            break
        text += line + "\n"
    return text


@bot.command(name="dday", aliases=["디데이", "d-day"])
@commands.guild_only()
async def dday_command(ctx, *, name: str = ""):
    if not name:
        text = dday_list_text(ctx.guild.id)
        if not text:
            await ctx.reply("이 서버에는 D-Day가 없습니다. `/도움말` → 기타 → D-Day에서 만들 수 있습니다.", mention_author=False)
            return
        embed = discord.Embed(title="📅 D-Day", description=text, color=discord.Color.green())
        embed.set_footer(text="!dday <이름>으로 남은 시간 보기 · 만들기·편집·삭제는 /도움말 → 기타 → D-Day")
        await ctx.reply(embed=embed, mention_author=False)
        return
    row = find_dday(ctx.guild.id, name)
    if not row:
        await ctx.reply(f"`{name}` D-Day가 없습니다. `!dday`로 목록을 확인해 주세요.", mention_author=False)
        return
    await ctx.reply(dday_message(row), mention_author=False)


def dday_buttons():
    return [HelpButton("dday_new", "만들기", "➕", discord.ButtonStyle.success, row=2),
            HelpButton("dday_edit", "편집", "✏️", discord.ButtonStyle.secondary, row=2),
            HelpButton("dday_del", "삭제", "🗑️", discord.ButtonStyle.secondary, row=2)]


async def show_dday_page(interaction, notice=None, extra_items=()):
    if interaction.guild is None:
        await show_help(interaction, notice=SERVER_ONLY_MESSAGE)
        return
    text = dday_list_text(interaction.guild.id, interaction.user.id, limit=2500)
    await show_help(interaction, page="etc", selected="dday",
                    details=f"**이 서버의 D-Day**\n{text}" if text else "이 서버에는 아직 D-Day가 없습니다.",
                    notice=notice, extra_items=[*dday_buttons(), *extra_items])


async def dday_open_form(interaction, row=None):
    if interaction.guild is None:
        await show_help(interaction, notice=SERVER_ONLY_MESSAGE)
        return
    if row is None:
        if len(guild_ddays(interaction.guild.id)) >= DDAY_MAX_PER_GUILD:
            await show_dday_page(interaction, f"⚠️ 한 서버에 D-Day는 {DDAY_MAX_PER_GUILD}개까지 만들 수 있습니다.")
            return
        if len(guild_ddays(interaction.guild.id, interaction.user.id)) >= DDAY_MAX_PER_USER:
            await show_dday_page(interaction, f"⚠️ 한 사람이 D-Day를 {DDAY_MAX_PER_USER}개까지 만들 수 있습니다.")
            return
    await interaction.response.send_modal(DdayModal(interaction.channel, row))


async def dday_choose(interaction, what):
    """Edit/delete: only one's own D-Days. One of them goes straight on; several get a menu."""
    if interaction.guild is None:
        await show_help(interaction, notice=SERVER_ONLY_MESSAGE)
        return
    mine = guild_ddays(interaction.guild.id, interaction.user.id)
    if not mine:
        await show_dday_page(interaction, "⚠️ 내가 만든 D-Day가 없습니다. (편집·삭제는 만든 사람만 할 수 있습니다)")
        return
    if what == "edit" and len(mine) == 1:
        await dday_open_form(interaction, mine[0])
        return
    await show_dday_page(interaction, "편집할 D-Day를 골라 주세요." if what == "edit" else "삭제할 D-Day를 골라 주세요.",
                         extra_items=[DdayPick(what, mine)])


class DdayPick(discord.ui.DynamicItem[discord.ui.Select], template=r"maco:ddaypick:(?P<what>edit|del)"):
    """Menu of one's own D-Days on the help message, for 편집 or 삭제."""

    def __init__(self, what, rows=()):
        options = [discord.SelectOption(label=r["name"][:100], value=str(r["id"]),
                                        description=dday.target_text(dday_target(r), r["has_time"]))
                   for r in rows][:25] or [discord.SelectOption(label="-", value="0")]
        super().__init__(discord.ui.Select(
            custom_id=f"maco:ddaypick:{what}", options=options, row=3,
            placeholder="편집할 D-Day" if what == "edit" else "삭제할 D-Day (바로 지워집니다)",
        ))
        self.what = what

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["what"])

    async def callback(self, interaction):
        chosen = (interaction.data or {}).get("values") or ["0"]
        with connect_db() as conn:
            row = conn.execute("SELECT * FROM ddays WHERE id = ?", (int(chosen[0]),)).fetchone()
        row = dict(row) if row else None
        if not row or row["guild_id"] != getattr(interaction.guild, "id", None):
            await show_dday_page(interaction, "⚠️ 이미 지워진 D-Day입니다.")
            return
        if row["created_by"] != interaction.user.id:
            await show_dday_page(interaction, "⚠️ 만든 사람만 편집하거나 지울 수 있습니다.")
            return
        if self.what == "edit":
            await dday_open_form(interaction, row)
            return
        with connect_db() as conn:
            conn.execute("DELETE FROM ddays WHERE id = ?", (row["id"],))
        await show_dday_page(interaction, f"🗑️ **{discord.utils.escape_markdown(row['name'])}** D-Day를 지웠습니다.")


class DdayAfter(discord.ui.DynamicItem[discord.ui.Select], template=r"maco:ddayafter:(?P<id>\d+)"):
    """What happens once the day comes: shown right after making or editing (a form holds only 5 fields)."""

    def __init__(self, dday_id, current="stop"):
        super().__init__(discord.ui.Select(
            custom_id=f"maco:ddayafter:{dday_id}", row=3, placeholder="날짜가 지나면",
            options=[discord.SelectOption(label=f"날짜가 지나면: {label}", value=key, default=key == current)
                     for key, label in dday.AFTER_MODES.items()],
        ))
        self.dday_id = dday_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["id"]))

    async def callback(self, interaction):
        mode = ((interaction.data or {}).get("values") or ["stop"])[0]
        with connect_db() as conn:
            row = conn.execute("SELECT * FROM ddays WHERE id = ?", (self.dday_id,)).fetchone()
            if row and row["created_by"] == interaction.user.id and mode in dday.AFTER_MODES:
                conn.execute("UPDATE ddays SET after = ? WHERE id = ?", (mode, self.dday_id))
        if not row:
            await show_dday_page(interaction, "⚠️ 이미 지워진 D-Day입니다.")
        elif row["created_by"] != interaction.user.id:
            await show_dday_page(interaction, "⚠️ 만든 사람만 바꿀 수 있습니다.")
        else:
            await show_dday_page(interaction, f"✅ **{discord.utils.escape_markdown(row['name'])}** · 날짜가 지나면: "
                                              f"{dday.AFTER_MODES[mode]}", extra_items=dday_extras(self.dday_id, mode))


def dday_extras(dday_id, after):
    """Shown under a D-Day just made or edited: what happens after the day, and its celebration message
    (neither fits the form, which holds 5 fields)."""
    return [DdayCelebrateButton(dday_id), DdayAfter(dday_id, after)]


class DdayCelebrateButton(discord.ui.DynamicItem[discord.ui.Button], template=r"maco:ddaycel:(?P<id>\d+)"):
    def __init__(self, dday_id):
        super().__init__(discord.ui.Button(label="축하 문구", emoji="🎉", style=discord.ButtonStyle.secondary,
                                           custom_id=f"maco:ddaycel:{dday_id}", row=2))
        self.dday_id = dday_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["id"]))

    async def callback(self, interaction):
        with connect_db() as conn:
            row = conn.execute("SELECT * FROM ddays WHERE id = ?", (self.dday_id,)).fetchone()
        if not row:
            await show_dday_page(interaction, "⚠️ 이미 지워진 D-Day입니다.")
        elif row["created_by"] != interaction.user.id:
            await show_dday_page(interaction, "⚠️ 만든 사람만 바꿀 수 있습니다.")
        else:
            await interaction.response.send_modal(DdayCelebrateModal(dict(row)))


class DdayCelebrateModal(SafeModal):
    """The message on the day itself (also the alert sent that day)."""

    def __init__(self, row):
        super().__init__(title="D-Day 축하 문구")
        self.row = row
        self.text = discord.ui.TextInput(required=False, max_length=200, placeholder=dday.DEFAULT_CELEBRATE,
                                         default=row.get("celebrate"))
        self.add_item(discord.ui.Label(text="당일에 보여 줄 문구 (비우면 기본 문구)", component=self.text,
                                       description="칸: {이름} {D}"))

    async def on_submit(self, interaction):
        try:
            text = dday.check_template(self.text.value.strip(), dday.CELEBRATE_FIELDS) or None
        except ValueError as exc:
            await show_dday_page(interaction, f"⚠️ {exc}", extra_items=dday_extras(self.row["id"], self.row["after"]))
            return
        with connect_db() as conn:
            conn.execute("UPDATE ddays SET celebrate = ? WHERE id = ? AND created_by = ?",
                         (text, self.row["id"], interaction.user.id))
        preview = (text or dday.DEFAULT_CELEBRATE).replace("{이름}", self.row["name"]).replace("{D}", "D-DAY")
        await show_dday_page(interaction, f"✅ **{discord.utils.escape_markdown(self.row['name'])}** 당일 문구를 "
                                          f"정했습니다.\n> {preview}", extra_items=dday_extras(self.row["id"], self.row["after"]))


class DdayModal(SafeModal):
    """만들기 / 편집. Five fields (a form's limit): name, date, message, alert time, alert channel."""

    def __init__(self, channel, row=None):
        super().__init__(title="D-Day 편집" if row else "D-Day 만들기")
        self.row = row
        target = dday_target(row) if row else None
        self.name = discord.ui.TextInput(max_length=DDAY_NAME_LENGTH, placeholder="수능", default=row["name"] if row else None)
        self.date = discord.ui.TextInput(
            max_length=30, placeholder="2027-11-18  (시각도: 2027-11-18 08:40)",
            default=(target.strftime("%Y-%m-%d %H:%M") if row["has_time"] else target.strftime("%Y-%m-%d")) if row else None)
        self.template = discord.ui.TextInput(
            required=False, max_length=200, placeholder=dday.DEFAULT_TEMPLATE, default=row["template"] if row else None)
        self.alert = discord.ui.TextInput(
            required=False, max_length=10, placeholder="00:00  (비우면 알림 없음)", default=row["alert_time"] if row else None)
        channel_id = (row["channel_id"] if row and row["channel_id"] else getattr(channel, "id", None))
        self.channel = discord.ui.ChannelSelect(
            required=False, placeholder="알림 보낼 채널 (비우면 이 채널)",
            channel_types=[discord.ChannelType.text, discord.ChannelType.news],
            default_values=[discord.Object(id=channel_id)] if channel_id else [],
        )
        self.add_item(discord.ui.Label(text="이름", component=self.name))
        self.add_item(discord.ui.Label(text="날짜 (한국 시간)", component=self.date))
        self.add_item(discord.ui.Label(text="표시 문구 (선택)", component=self.template,
                                       description="칸: {이름} {D} {년} {월} {일} {시} {분} {초}"))
        self.add_item(discord.ui.Label(text="자동 알림 시각 (선택)", component=self.alert,
                                       description="매일 이 시각에 남은 날을 보냅니다"))
        self.add_item(discord.ui.Label(text="알림 채널", component=self.channel))

    async def on_submit(self, interaction):
        guild = interaction.guild
        name = " ".join(self.name.value.split())
        try:
            if not name:
                raise ValueError("이름을 적어 주세요.")
            target, has_time = dday.parse_target(self.date.value)
            template = dday.check_template(self.template.value.strip()) or None
            alert = dday.parse_alert(self.alert.value)
        except ValueError as exc:
            await show_dday_page(interaction, f"⚠️ {exc}")
            return
        same = find_dday(guild.id, name)
        if same and (not self.row or same["id"] != self.row["id"]):
            await show_dday_page(interaction, f"⚠️ 이 서버에 `{name}` D-Day가 이미 있습니다. 다른 이름을 써 주세요.")
            return
        picked = self.channel.values[0].id if self.channel.values else getattr(interaction.channel, "id", None)
        channel = guild.get_channel(picked) if picked else None
        if alert:
            perms = channel.permissions_for(guild.me) if channel else None
            if not perms or not (perms.view_channel and perms.send_messages):
                await show_dday_page(interaction, "⚠️ 마코가 그 채널에 글을 쓸 수 없습니다. 다른 채널을 고르거나 권한을 켜 주세요.")
                return
        next_at = dday.next_alert(alert) if alert else None
        count_up = int(dday.days_left(target) < 0)
        values = (name, target.timestamp(), int(has_time), template, alert, picked if alert else None, next_at, count_up)
        with connect_db() as conn:
            if self.row:
                conn.execute("UPDATE ddays SET name = ?, target = ?, has_time = ?, template = ?, alert_time = ?, "
                             "channel_id = ?, next_alert = ?, count_up = ? WHERE id = ? AND created_by = ?",
                             (*values, self.row["id"], interaction.user.id))
                dday_id, after = self.row["id"], self.row["after"]
            else:
                after = "count" if count_up else "stop"  # a past date: counting D+ is the point
                dday_id = conn.execute(
                    "INSERT INTO ddays(name, target, has_time, template, alert_time, channel_id, next_alert, count_up, "
                    "guild_id, after, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (*values, guild.id, after, interaction.user.id, time.time())).lastrowid
        with connect_db() as conn:
            row = dict(conn.execute("SELECT * FROM ddays WHERE id = ?", (dday_id,)).fetchone())
        alert_text = f" · 매일 {alert}에 <#{picked}>로 알림" if alert else ""
        await show_dday_page(
            interaction, f"✅ **{discord.utils.escape_markdown(name)}** D-Day를 {'고쳤' if self.row else '만들었'}습니다"
                         f"{alert_text}.\n> {dday_message(row)}\n아래에서 **🎉 축하 문구**(당일 문구)와 날짜가 지난 뒤를 정할 수 있습니다.",
            extra_items=dday_extras(dday_id, after))


async def send_dday_alert(row):
    """Post one D-Day alert and plan the next (or end/delete it once the day has come, as its owner chose)."""
    target = dday_target(row)
    left = dday.days_left(target)
    channel = bot.get_channel(row["channel_id"]) if row["channel_id"] else None
    if channel is None and row["channel_id"]:
        try:
            channel = await bot.fetch_channel(row["channel_id"])
        except discord.HTTPException:
            channel = None
    sent = False
    if channel is not None:
        try:
            await channel.send(f"-# ⏰ D-Day 알림 · <@{row['created_by']}>님이 만든 D-Day\n{dday_message(row)}")
            sent = True
        except discord.HTTPException as exc:
            print(f"D-Day 알림 실패 ({row['name']}): {type(exc).__name__}: {exc}")
    finished = left <= 0 and row["after"] in ("stop", "delete")
    with connect_db() as conn:
        if channel is None:  # the channel is gone: stop instead of retrying every minute
            conn.execute("UPDATE ddays SET next_alert = NULL WHERE id = ?", (row["id"],))
        elif finished and row["after"] == "delete" and sent:
            conn.execute("DELETE FROM ddays WHERE id = ?", (row["id"],))
        elif finished:
            conn.execute("UPDATE ddays SET next_alert = NULL WHERE id = ?", (row["id"],))
        else:
            # The next one after now: an alert missed while the bot was off is sent once, not once per day.
            conn.execute("UPDATE ddays SET next_alert = ? WHERE id = ?", (dday.next_alert(row["alert_time"]), row["id"]))


@tasks.loop(seconds=60)
async def dday_alerts():
    try:
        with connect_db() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM ddays WHERE next_alert IS NOT NULL AND next_alert <= ?", (time.time(),))]
        for row in rows:
            await send_dday_alert(row)
    except Exception as exc:
        print(f"D-Day 알림 오류: {type(exc).__name__}: {exc}")
        notify_admin("D-Day 알림 오류", f"{type(exc).__name__}: {exc}", "", traceback.format_exc())


@tasks.loop(minutes=1)
async def save_traffic():
    try:
        flush_traffic()
    except sqlite3.Error as exc:
        print(f"활동 집계 저장 오류: {type(exc).__name__}: {exc}")


@tasks.loop(minutes=10)
async def tidy_memory():
    """Drop in-memory entries nothing will use again. Each is small, but these only ever grew: an unused
    upload link, a finished upload's lock or "⏳" message, an old feedback cooldown."""
    try:
        now = time.time()
        with connect_db() as conn:
            pending = {r["id"] for r in conn.execute("SELECT id FROM files WHERE status = 'pending'")}
        for key in [k for k, v in _upload_links.items() if now - v["at"] > UPLOAD_LINK_REUSE_SECONDS]:
            del _upload_links[key]
        for table in (_link_interactions, _placeholders):
            for row_id in [i for i in table if i not in pending]:
                del table[row_id]
        for row_id in [i for i, lock in _upload_locks.items() if i not in pending and not lock.locked()]:
            del _upload_locks[row_id]
        for user_id in [u for u, at in _feedback_last_sent.items() if now - at > FEEDBACK_COOLDOWN_SECONDS]:
            del _feedback_last_sent[user_id]
    except Exception as exc:
        print(f"메모리 정리 오류: {type(exc).__name__}: {exc}")


@tidy_memory.before_loop
async def before_tidy_memory():
    await bot.wait_until_ready()


@dday_alerts.before_loop
async def before_dday_alerts():
    await bot.wait_until_ready()


# ---------------------------------------------------------------- 나라 맞히기

NARA_FILE = "나라맞히기.png"
_nara_locks = {}  # channel id -> lock, so two guesses at once don't both post a board
_nara_just_won = {}  # channel id -> (country, winner, when): tells a right guess a moment too late who was first


def nara_lock(channel_id):
    lock = _nara_locks.get(channel_id)
    if lock is None:
        if len(_nara_locks) >= 300:
            for key in [k for k, v in _nara_locks.items() if not v.locked()]:
                del _nara_locks[key]
        lock = _nara_locks[channel_id] = asyncio.Lock()
    return lock


def nara_game(channel_id):
    with connect_db() as conn:
        return conn.execute("SELECT * FROM nara_games WHERE channel_id = ?", (channel_id,)).fetchone()


def nara_end_game(channel_id):
    with connect_db() as conn:
        conn.execute("DELETE FROM nara_games WHERE channel_id = ?", (channel_id,))


async def nara_post_board(channel, game, guesses, content, finished=False):
    """Post the board (the last BOARD_ROWS guesses) under content. Earlier boards stay in the chat."""
    answer, level = game["answer"], game["level"]
    rows = [nara.compare(code, answer) for code, _ in guesses[-nara.BOARD_ROWS:]]
    png = await asyncio.to_thread(nara.render_board, rows, answer if finished else None, len(guesses), level)
    return await channel.send(content, file=discord.File(io.BytesIO(png), NARA_FILE))


NARA_LEVEL_EMOJI = {nara.EASY: "🟢", nara.NORMAL: "🌍", nara.HARD: "🔥"}


def nara_rules(level):
    if level == nara.HARD:
        return f"**{nara.HARD_MAX_GUESSES}번** 안에 맞혀야 합니다."
    return "**횟수 제한 없이** 추측할 수 있습니다."


def nara_title(level, setter=None):
    if setter:
        return f"✏️ **나라 맞히기 · 출제** ({nara.LEVEL_NAMES[level]} 규칙)"
    return f"{NARA_LEVEL_EMOJI[level]} **나라 맞히기 · {nara.LEVEL_NAMES[level]}**"


async def nara_start(channel, guild, user, level=nara.NORMAL, answer=None, bet=False):
    """Start a game in this channel, or show the one already going on there. With answer, user chose it
    (!나라출제): they can't guess, and winning it doesn't count in the ranking."""
    if bet and not rank_setting("bet", guild.id if guild else None):
        await channel.send(f"-# {user.mention}님의 요청 · 나라 맞히기\n{BET_OFF}")
        return
    async with nara_lock(channel.id):
        game = nara_game(channel.id)
        header = f"-# {user.mention}님의 요청 · 나라 맞히기" + (" · 🎲 배팅" if bet else "")
        if game:
            if bet and not bet_round("nara", channel.id):
                header += "\n-# 진행 중인 판이 있어서 배팅 판으로 시작하지 못했습니다."
            guesses = json.loads(game["guesses"])
            other = ""
            if game["level"] != level and not game["setter"]:
                other = f"\n-# {nara.LEVEL_NAMES[level]}으로 하려면 `!포기` 후 다시 시작하세요."
            content = (f"{header}\n{nara_title(game['level'], game['setter'])} · 이어서\n"
                       f"지금까지 추측 {len(guesses)}번 · {nara_rules(game['level'])}{other}")
            await nara_post_board(channel, game, guesses, content)
            return
        setter = user.id if answer else None
        game = {"answer": answer or nara.pick(level), "level": level}
        if level == nara.HARD:
            rule = f"누구나 `!추측 <나라>`로 추측할 수 있습니다. {nara_rules(level)}"
        else:
            rule = "누구나 `!추측 <나라>`로 **횟수 제한 없이** 추측할 수 있습니다."
        if setter:
            header = f"-# {user.mention}님의 출제 · 나라 맞히기"
            rule = f"{user.mention}님이 낸 나라를 맞혀 보세요! {rule}"
        content = f"{header}\n{nara_title(level, setter)}\n{rule}"
        if bet and guild and not setter:  # (출제 판은 배팅 없음: 출제자가 답을 압니다)
            await open_bets("nara", "나라 맞히기", channel, guild)
        message = await nara_post_board(channel, game, [], content)
        with connect_db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO nara_games(channel_id, guild_id, answer, guesses, started_by, started_at, "
                "board_message_id, level, setter) VALUES (?, ?, ?, '[]', ?, ?, ?, ?, ?)",
                (channel.id, guild.id if guild else None, game["answer"], user.id, time.time(), message.id, level, setter),
            )


@bot.command(name="나라시작", aliases=["나라맞히기", "나라"])
@commands.guild_only()
async def nara_start_command(ctx, *, level: str = ""):
    if level.strip() == "출제":
        await nara_set_command(ctx)
        return
    bet, level = split_bet_word(level)
    if bet and not rank_setting("bet", ctx.guild.id):
        await ctx.reply(BET_OFF, mention_author=False)
        return
    chosen = nara.level_of(level)
    if chosen is None:
        await ctx.reply("난이도는 `쉬움`, `보통`, `어려움`입니다. 예: `!나라시작 쉬움` · 배팅 판: `!나라시작 보통 배팅`",
                        mention_author=False)
        return
    await nara_start(ctx.channel, ctx.guild, ctx.author, chosen, bet=bet)


@bot.command(name="guess", aliases=["추측"])
@commands.guild_only()
async def nara_guess_command(ctx, *, name: str = ""):
    name = name.strip()
    if not name:
        await ctx.reply("`!추측 <나라 이름>`처럼 써 주세요. 예: `!추측 프랑스`", mention_author=False)
        return
    country = nara.find(name)
    if country is None:
        await ctx.reply(f"{nara_not_found(name)}\n-# 이 추측은 횟수에 들어가지 않습니다.", mention_author=False)
        return
    async with nara_lock(ctx.channel.id):
        game = nara_game(ctx.channel.id)
        if game is None:
            last = _nara_just_won.get(ctx.channel.id)
            if last and last[0] == country["code"] and time.monotonic() - last[2] < QUIZ_LATE_SECONDS:
                await ctx.reply(f"⏱️ 아깝습니다! 방금 <@{last[1]}>님이 **{country['ko']}**{nara.josa(country['ko'], '을', '를')} "
                                "먼저 맞혔습니다. 새 판: `!나라시작`", mention_author=False)
                return
            await ctx.reply("이 채널에는 진행 중인 판이 없습니다. `!나라시작`으로 시작해 주세요.", mention_author=False)
            return
        if game["setter"] == ctx.author.id:
            await ctx.reply("출제자는 추측할 수 없습니다. 다른 사람들이 맞힐 때까지 기다려 주세요!", mention_author=False)
            return
        guesses = json.loads(game["guesses"])
        code, answer, level = country["code"], game["answer"], game["level"]
        if any(done == code for done, _ in guesses):
            await ctx.reply(f"**{country['ko']}**{nara.josa(country['ko'], '은', '는')} 이미 추측한 나라입니다.\n"
                            "-# 이 추측은 횟수에 들어가지 않습니다.", mention_author=False)
            return
        guesses.append([code, ctx.author.id])
        await close_bets("nara", ctx.channel)  # the first guess ends the betting (the board starts giving hints)
        header = f"-# {ctx.author.mention}님의 추측 · {country['ko']}"
        answer_name = nara.countries()[answer]["ko"]
        limit = nara.max_guesses(level)
        if code == answer:
            setter = f"\n-# 출제: <@{game['setter']}>" if game["setter"] else ""
            content = (f"{header}\n🎉 {ctx.author.mention}님이 정답을 맞혔습니다! 정답은 **{answer_name}**입니다.\n"
                       f"-# 추측 시도 {len(guesses)}번{setter}\n-# {nara.summary(answer)}")
            if len(_nara_just_won) >= 300:
                _nara_just_won.clear()
            _nara_just_won[ctx.channel.id] = (code, ctx.author.id, time.monotonic())
            settled = settle_bets("nara", ctx.channel.id, {str(ctx.author.id): 1})
            await nara_post_board(ctx.channel, game, guesses, content + (f"\n{settled}" if settled else ""), finished=True)
            column = {nara.EASY: "easy_wins", nara.NORMAL: "wins", nara.HARD: "hard_wins"}[level]
            with connect_db() as conn:
                conn.execute("DELETE FROM nara_games WHERE channel_id = ?", (ctx.channel.id,))
                if game["setter"]:  # a chosen answer isn't a fair game for the ranking
                    return
                conn.execute(
                    f"INSERT INTO nara_scores(guild_id, user_id, {column}) VALUES (?, ?, 1) "
                    f"ON CONFLICT(guild_id, user_id) DO UPDATE SET {column} = {column} + 1",
                    (ctx.guild.id, ctx.author.id),
                )
            return
        if limit and len(guesses) >= limit:
            content = (f"{header}\n💀 {limit}번 안에 맞히지 못했습니다. 정답은 **{answer_name}**입니다.\n"
                       f"-# {nara.summary(answer)}")
            settled = settle_bets("nara", ctx.channel.id, {})
            await nara_post_board(ctx.channel, game, guesses, content + (f"\n{settled}" if settled else ""), finished=True)
            nara_end_game(ctx.channel.id)
            return
        content = header + (f"\n-# 남은 기회 {limit - len(guesses)}번" if limit else "")
        await nara_post_board(ctx.channel, game, guesses, content)
        with connect_db() as conn:
            conn.execute("UPDATE nara_games SET guesses = ? WHERE channel_id = ?", (json.dumps(guesses), ctx.channel.id))


@bot.command(name="포기", aliases=["나라포기"])
@commands.guild_only()
async def nara_giveup_command(ctx):
    async with nara_lock(ctx.channel.id):
        game = nara_game(ctx.channel.id)
        if game is None:
            await ctx.reply("이 채널에는 진행 중인 나라 맞히기 판이 없습니다.", mention_author=False)
            return
        guesses, answer = json.loads(game["guesses"]), game["answer"]
        refunded = refund_bets("nara", ctx.channel.id)
        content = (f"-# {ctx.author.mention}님이 포기했습니다 · 나라 맞히기\n"
                   f"🏳️ 정답은 **{nara.countries()[answer]['ko']}**입니다.\n"
                   f"-# 추측 시도 {len(guesses)}번\n-# {nara.summary(answer)}" + (f"\n{refunded}" if refunded else ""))
        await nara_post_board(ctx.channel, game, guesses, content, finished=True)
        nara_end_game(ctx.channel.id)


@bot.command(name="나라출제")
@commands.guild_only()
async def nara_set_command(ctx):
    """Choose the answer yourself: a button (only for you) opens a private form, so nobody sees the country."""
    if nara_game(ctx.channel.id):
        await ctx.reply("이 채널에는 진행 중인 판이 있습니다. 끝난 뒤(또는 `!포기` 후) 출제해 주세요.", mention_author=False)
        return
    await ctx.channel.send(f"-# {ctx.author.mention}님의 요청 · 나라 맞히기 출제\n"
                           "✏️ 아래 버튼을 눌러 낼 나라를 정해 주세요. 입력 창은 출제자에게만 보입니다.",
                           view=discord.ui.View(timeout=None).add_item(NaraSetButton(ctx.author.id)))


class NaraSetButton(discord.ui.DynamicItem[discord.ui.Button], template=r"maco:naraset:(?P<user_id>\d+)"):
    """[나라 정하기] under !나라출제; only the person who asked can use it (works after a restart)."""

    def __init__(self, user_id):
        super().__init__(discord.ui.Button(label="나라 정하기", emoji="✏️", style=discord.ButtonStyle.primary,
                                           custom_id=f"maco:naraset:{user_id}"))
        self.user_id = user_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["user_id"]))

    async def callback(self, interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("출제한 사람만 누를 수 있습니다. `!나라출제`로 직접 출제해 보세요!",
                                                    ephemeral=True)
            return
        await interaction.response.send_modal(NaraSetModal())


class NaraSetModal(SafeModal, title="나라 출제"):
    def __init__(self):
        super().__init__()
        self.country = discord.ui.TextInput(max_length=60, placeholder="예: 이집트")
        self.rule = discord.ui.Select(options=[
            discord.SelectOption(label="보통 규칙", value=nara.NORMAL, default=True,
                                 description="모든 힌트 · 횟수 제한 없음"),
            discord.SelectOption(label="어려움 규칙", value=nara.HARD,
                                 description="이웃·평균 기온 칸 · 10번 안에"),
        ])
        self.add_item(discord.ui.Label(text="낼 나라", component=self.country))
        self.add_item(discord.ui.Label(text="규칙", component=self.rule))

    async def on_submit(self, interaction):
        name = self.country.value.strip()
        country = nara.find(name)
        if country is None:
            await interaction.response.send_message(f"{nara_not_found(name)}\n-# 버튼을 다시 눌러 주세요.", ephemeral=True)
            return
        if nara_game(interaction.channel.id):
            await interaction.response.send_message("그새 이 채널에 다른 판이 시작됐습니다. 끝난 뒤 다시 출제해 주세요.",
                                                    ephemeral=True)
            return
        await interaction.response.send_message(f"**{country['ko']}**{nara.josa(country['ko'], '을', '를')} 냈습니다! "
                                                "다른 사람들이 맞힐 때까지 기다려 주세요.", ephemeral=True)
        level = self.rule.values[0] if self.rule.values else nara.NORMAL
        try:
            await interaction.message.delete()  # the button message, done with
        except (discord.HTTPException, AttributeError):
            pass
        await nara_start(interaction.channel, interaction.guild, interaction.user, level, country["code"])


def nara_ranking_text(guild_id, limit=10):
    with connect_db() as conn:
        rows = conn.execute(
            "SELECT user_id, easy_wins, wins, hard_wins FROM nara_scores WHERE guild_id = ? "
            "AND easy_wins + wins + hard_wins > 0 ORDER BY hard_wins DESC, wins DESC, easy_wins DESC LIMIT ?",
            (guild_id, limit),
        ).fetchall()
    place = rank_prefix(guild_id)
    return "\n".join(f"{place(i)}<@{r['user_id']}> · 쉬움 {r['easy_wins']}승 · "
                     f"보통 {r['wins']}승 · 어려움 {r['hard_wins']}승"
                     for i, r in enumerate(rows, 1))


async def nara_ranking_send(target):
    text = nara_ranking_text(target.guild.id)
    if not text:
        await target.reply("아직 이 서버에서 나라를 맞힌 사람이 없습니다. `!나라시작`으로 시작해 보세요!", mention_author=False)
        return
    embed = discord.Embed(title="🏆 나라 맞히기 순위", description=text, color=discord.Color.green())
    embed.set_footer(text="어려움 승리가 많은 순, 같으면 보통·쉬움 승리 순 · 모든 게임을 합친 순위: !순위")
    await target.reply(embed=embed, mention_author=False)


@bot.command(name="나라순위", aliases=["나라랭킹"])
@commands.guild_only()
async def nara_ranking_command(ctx):
    await nara_ranking_send(ctx)


def help_run_nara(level, bet=False):
    async def handler(interaction):
        if interaction.guild is None or interaction.channel is None:
            await show_help(interaction, notice=SERVER_ONLY_MESSAGE)
            return
        await show_nara_page(interaction, bet=bet)
        ctx = HelpContext(interaction, f"나라 맞히기 · {nara.LEVEL_NAMES[level]}" + (" · 배팅" if bet else ""))
        await run_from_help(ctx, nara_start(interaction.channel, interaction.guild, interaction.user, level, bet=bet),
                            channel_only=True)
    return handler


async def help_run_nara_rank(interaction):
    if interaction.guild is None:
        await show_help(interaction, notice=SERVER_ONLY_MESSAGE)
        return
    ranking = nara_ranking_text(interaction.guild.id)
    await show_nara_page(interaction,
                         details=f"**이 서버 순위**\n{ranking}" if ranking else "아직 이 서버에서 나라를 맞힌 사람이 없습니다.")


def nara_buttons(bet=False, guild_id=None):
    start, word = ("nara_bet_", "배팅") if bet else ("nara_", "시작")
    return [HelpButton(f"{start}easy", f"쉬움 {word}", "🟢", discord.ButtonStyle.success, row=2),
            HelpButton(f"{start}normal", f"보통 {word}", "🌍", discord.ButtonStyle.primary, row=2),
            HelpButton(f"{start}hard", f"어려움 {word}", "🔥", discord.ButtonStyle.danger, row=2),
            HelpButton("nara_info", "나라 정보", "📖", discord.ButtonStyle.secondary, row=2)] + bet_toggle("nara", bet, guild_id)


async def show_nara_page(interaction, details=None, notice=None, bet=False):
    """/도움말 → 게임 → 나라 맞히기: the game's own buttons (difficulty, ranking, country info); bet: 배팅 starts."""
    if bet:
        details = BET_MODE_TEXT + (f"\n{details}" if details else "")
    await show_help(interaction, page="game", selected="nara", details=details, notice=notice,
                    extra_items=nara_buttons(bet, interaction.guild.id if interaction.guild else None))


# !정보: the introduction from Korean Wikipedia and the numbers, all bundled in assets/countries.json. (Asking
# Wikipedia when the command was used often failed: it turns away busy shared server addresses with 429.)
WIKI_PAGE_URL = "https://ko.wikipedia.org/wiki/{}"


def nara_not_found(name):
    hints = nara.suggest(name)
    hint = f" 혹시 {', '.join(f'**{h}**' for h in hints)}인가요?" if hints else ""
    return f"`{name[:50]}`라는 나라를 찾지 못했습니다.{hint}"


def nara_info_message(country):
    """{'embed', 'file'} for !정보: the bundled introduction, the numbers, and the flag from assets/flags."""
    text = nara.intro(country.get("intro", "")) or nara.basic_intro(country)
    embed = discord.Embed(title=f"{country.get('flag', '')} {country['ko']} ({country['en']})",
                          description=text,
                          url=WIKI_PAGE_URL.format(quote(country["wiki"].replace(" ", "_"))) if country.get("wiki") else None,
                          color=discord.Color.green())
    for name, value in nara.info_fields(country):
        embed.add_field(name=name, value=value, inline=True)
    embed.set_footer(text="출처: 위키백과 (소개, CC BY-SA), World Bank (인구), world-countries · Wikidata")
    message = {"embed": embed}
    flag = quiz.flag_path(country["code"])
    if flag.exists():
        message["file"] = discord.File(flag, "flag.png")
        embed.set_thumbnail(url="attachment://flag.png")
    return message


@bot.command(name="정보", aliases=["나라정보"])
async def nara_info_command(ctx, *, name: str = ""):
    name = name.strip()
    if not name:
        await ctx.reply("`!정보 <나라 이름>`처럼 써 주세요. 예: `!정보 이집트`", mention_author=False)
        return
    country = nara.find(name)
    if country is None:
        await ctx.reply(nara_not_found(name), mention_author=False)
        return
    if not nara.has_info():
        await ctx.reply("봇의 나라 데이터 파일(assets/countries.json)이 예전 버전이라 정보를 보여 줄 수 없습니다.", mention_author=False)
        return
    await ctx.reply(mention_author=False, **nara_info_message(country))


class NaraInfoModal(SafeModal, title="나라 정보"):
    def __init__(self):
        super().__init__()
        self.country = discord.ui.TextInput(max_length=60, placeholder="이집트")
        self.add_item(discord.ui.Label(text="나라 이름", component=self.country))

    async def on_submit(self, interaction):
        name = self.country.value.strip()
        country = nara.find(name)
        if country is None:
            await show_nara_page(interaction, notice=nara_not_found(name))
            return
        await show_nara_page(interaction)
        ctx = HelpContext(interaction, f"나라 정보 · {country['ko']}")

        async def post():
            await ctx.reply(**nara_info_message(country))
        await run_from_help(ctx, post())


# ---------------------------------------------------------------- 국기 맞히기 · 수도 맞히기 (quizzes)

class QuizKind:
    """What differs between the quizzes; everything else (questions in a row, the first right answer scores,
    ⏱️ for a moment too late, hints, skip/stop, counts per difficulty) is shared below."""

    def __init__(self, key, title, emoji, table, commands, noun):
        self.key, self.title, self.emoji, self.table, self.noun = key, title, emoji, table, noun
        self.start, self.answer, self.skip, self.stop, self.rank = commands


FLAG_QUIZ = QuizKind(quiz.FLAG, "국기 맞히기", "🚩", "flag", ("국기시작", "국기", "국기스킵", "국기중지", "국기순위"), "나라")
CAPITAL_QUIZ = QuizKind(quiz.CAPITAL, "수도 맞히기", "🏛️", "capital", ("수도시작", "수도", "수도스킵", "수도중지", "수도순위"), "수도")
MAP_QUIZ = QuizKind(quiz.MAP, "지도 맞히기", "🗺️", "map", ("지도시작", "지도", "지도스킵", "지도중지", "지도순위"), "나라")
ANIME_QUIZ = QuizKind(quiz.ANIME, "애니캐 맞히기", "🎌", "anime", ("캐릭시작", "캐릭", "캐릭스킵", "캐릭중지", "캐릭순위"), "이름")
SONG_QUIZ = QuizKind(quiz.SONG, "노래 맞히기", "🎵", "song", ("노래시작", "노래", "노래스킵", "노래중지", "노래순위"), "제목")
QUIZZES = (FLAG_QUIZ, CAPITAL_QUIZ, MAP_QUIZ, ANIME_QUIZ, SONG_QUIZ)
ANILIST_URL = "https://graphql.anilist.co"
ANIME_FILE_MAX = 8 * 1024 * 1024
QUIZ_FLAG_FILE = "국기.png"  # always these names, so the file name never gives the answer away
QUIZ_MAP_FILE = "지도.png"
QUIZ_RECENT = 30  # countries asked lately in a channel's quiz, not asked again soon
QUIZ_LATE_SECONDS = 15
_quiz_just_answered = {}  # (kind, channel id) -> (country, when): a right answer a moment too late gets ⏱️, not ❌


def quiz_game(q, channel_id):
    with connect_db() as conn:
        return conn.execute(f"SELECT * FROM {q.table}_games WHERE channel_id = ?", (channel_id,)).fetchone()


def quiz_lock(q, channel_id):
    return nara_lock(f"{q.key}:{channel_id}")


def quiz_reveal(q, code, guild_id=None):
    """The answer of a question, for messages: '🇫🇷 **프랑스**' / '🇲🇳 몽골의 수도 **울란바토르**'"""
    if q is SONG_QUIZ:
        info = song_info(guild_id, code)
        return f"**{info['title']}** - {info['artist']}" if info else "(목록에서 지워진 곡)"
    if q is ANIME_QUIZ:
        c = quiz.anime()[code]
        return f"**{c['ko']}** · 『{c['work']}』"
    c = nara.countries()[code]
    if q is CAPITAL_QUIZ:
        return f"{c.get('flag', '')} {c['ko']}의 수도 **{quiz.capital_text(code)}**"
    return f"{c.get('flag', '')} **{c['ko']}**"


def quiz_hint_word(q, code, guild_id=None):
    if q is SONG_QUIZ:
        return song_hint_word(song_info(guild_id, code) or {"title": "?", "names": []})
    if q is ANIME_QUIZ:
        return quiz.anime()[code]["ko"]
    c = nara.countries()[code]
    return c.get("capitals", [c["capital"]])[0] if q is CAPITAL_QUIZ else c["ko"]


async def quiz_post_question(q, channel, game, header=None):
    head = f"{q.emoji} **{q.title} · {quiz.LEVEL_NAMES[game['level']]}** · {game['number']}/{game['total']}"
    ro = "으로" if q.noun in ("이름", "제목") else "로"
    hint = {ANIME_QUIZ: "`!힌트` 작품·초성", SONG_QUIZ: "`!힌트` 가수·초성"}.get(q, "`!힌트` 초성")
    help_line = f"`!{q.answer} <{q.noun}>`{ro} 답하기 · {hint} · `!{q.skip}` 넘기기 · `!{q.stop}` 그만"
    file = None
    if q is CAPITAL_QUIZ:
        c = nara.countries()[game["answer"]]
        text = f"{head}\n## {c.get('flag', '')} {c['ko']}의 수도는?\n-# {help_line}"
    elif q is SONG_QUIZ:
        seconds = quiz.SONG_SECONDS[game["level"]]
        file = await song_clip(game["guild_id"], game["answer"], seconds)
        text = f"{head} · {seconds}초\n-# {help_line}"
        if file is None:
            text += f"\n-# (노래를 불러오지 못했습니다. `!{q.skip}`으로 넘겨 주세요)"
    elif q is ANIME_QUIZ:
        file = await anime_picture(game["answer"])
        text = f"{head}\n-# {help_line}"
        if file is None:
            text += f"\n-# (그림을 불러오지 못했습니다. `!{q.skip}`으로 넘겨 주세요)"
    elif q is MAP_QUIZ:
        text = f"{head}\n-# {help_line}"
        file = discord.File(quiz.map_path(game["answer"], game["level"]), QUIZ_MAP_FILE)
    else:
        text = f"{head}\n-# {help_line}"
        file = discord.File(quiz.flag_path(game["answer"]), QUIZ_FLAG_FILE)
    await channel.send(f"{header}\n{text}" if header else text, **({"file": file} if file else {}))
    if q is SONG_QUIZ:
        song_prefetch(game)


async def anime_picture(cid):
    """The character's picture from AniList as a file named 캐릭터.<ext> (the address would give the answer
    away), or None. If the saved address stopped working, the current one is asked for once."""
    async def download(url):
        try:
            async with net.session().get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200 or (resp.content_length or 0) > ANIME_FILE_MAX:
                    return None
                return await resp.read()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return None

    url = quiz.anime()[cid]["image"]
    data = await download(url)
    if data is None:
        try:
            query = "query($id:Int){ Character(id:$id){ image{ large } } }"
            async with net.session().post(ANILIST_URL, json={"query": query, "variables": {"id": int(cid)}},
                                          timeout=aiohttp.ClientTimeout(total=10)) as resp:
                url = (await resp.json())["data"]["Character"]["image"]["large"]
            data = await download(url)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError, TypeError):
            data = None
    mark_health("anime", bool(data), "AniList 캐릭터 그림을 받지 못함")
    if not data or len(data) > ANIME_FILE_MAX:
        return None
    ext = url.rsplit(".", 1)[-1].lower() if "." in url.rsplit("/", 1)[-1] else "png"
    return discord.File(io.BytesIO(data), f"캐릭터.{ext if ext in ('png', 'jpg', 'jpeg', 'gif', 'webp') else 'png'}")


def quiz_session_text(scores, guild_id=None):
    """This quiz's scorers, best first: '🥇 @a 3개 · 🥈 @b 1개' (no medals while 순위 보이기 is off)."""
    place = rank_prefix(guild_id, plain=True)
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    return " · ".join(f"{place(i)}<@{uid}> {n}개" for i, (uid, n) in enumerate(ranked, 1))


def quiz_result_line(game):
    result = quiz_session_text(json.loads(game["scores"]), game["guild_id"])
    return f"이번에 맞힌 개수: {result}" if result else "이번에는 맞힌 사람이 없습니다."


async def quiz_next(q, channel, game, header, answer_file=None):
    """Move on after a question is answered or skipped: the next question, or the end of the quiz.
    answer_file (노래 맞히기: the whole preview) goes with the answer in a message of its own."""
    if game["number"] >= game["total"]:
        with connect_db() as conn:
            conn.execute(f"DELETE FROM {q.table}_games WHERE channel_id = ?", (channel.id,))
        settled = settle_bets(q.key, channel.id, json.loads(game["scores"]))
        await channel.send(f"{header}\n🏁 **{q.title} · {game['total']}문제 끝!**\n-# {quiz_result_line(game)}"
                           + (f"\n{settled}" if settled else ""), **({"file": answer_file} if answer_file else {}))
        return
    if answer_file:
        await channel.send(header.rstrip(), file=answer_file)
        header = None
    recent = json.loads(game["recent"])
    if q is SONG_QUIZ:
        upcoming = game["upcoming"]
        answer = (upcoming if upcoming and upcoming not in recent else None) or \
            song_pick(game["guild_id"], game["genre"], recent) or game["answer"]
    else:
        answer = quiz.pick(game["level"], recent, q.key, quiz_works(q, game["guild_id"]))
    with connect_db() as conn:
        conn.execute(f"UPDATE {q.table}_games SET answer = ?, number = number + 1, recent = ?, hinted = 0 "
                     "WHERE channel_id = ?",
                     (answer, json.dumps((recent + [answer])[-QUIZ_RECENT:]), channel.id))
    await quiz_post_question(q, channel, quiz_game(q, channel.id), header)


def anime_works_of(guild_id):
    """The works this server chose for 애니캐 맞히기 (a set), or None for all."""
    if not guild_id:
        return None
    with connect_db() as conn:
        row = conn.execute("SELECT works FROM anime_works WHERE guild_id = ?", (guild_id,)).fetchone()
    if not row:
        return None
    known = {c["work"] for c in quiz.anime().values()}
    works = set(json.loads(row["works"])) & known  # a work dropped from the list later is ignored
    return works or None


def quiz_works(q, guild_id):
    return anime_works_of(guild_id) if q is ANIME_QUIZ else None


async def quiz_start(q, channel, guild, user, level=quiz.NORMAL, count=quiz.DEFAULT_COUNT, genre=None, bet=False):
    off = game_off_message(q.key, guild) or (BET_OFF if bet and not rank_setting("bet", guild.id if guild else None) else None)
    if off:  # a /도움말 message opened before the game (or 배팅) was switched off
        await channel.send(f"-# {user.mention}님의 요청 · {q.title}\n{off}")
        return
    async with quiz_lock(q, channel.id):
        game = quiz_game(q, channel.id)
        header = f"-# {user.mention}님의 요청 · {q.title}" + (" · 🎲 배팅" if bet else "")
        if game:
            busy = "\n-# 진행 중인 판이 있어서 배팅 판으로 시작하지 못했습니다." if bet and not bet_round(q.key, channel.id) else ""
            await quiz_post_question(q, channel, game, f"{header}\n진행 중인 퀴즈를 이어서 합니다.{busy}")
            return
        if q is SONG_QUIZ:
            answer = song_pick(guild.id if guild else None, genre)
            if answer is None:
                await channel.send(f"{header}\n이 서버에 추가한 곡이 없습니다. `!노래추가 <가수와 제목>`으로 먼저 추가해 주세요.")
                return
        else:
            answer = quiz.pick(level, kind=q.key, works=quiz_works(q, guild.id if guild else None))
        with connect_db() as conn:
            conn.execute(
                f"INSERT OR REPLACE INTO {q.table}_games(channel_id, guild_id, level, answer, number, total, recent, "
                "scores, hinted, started_by, started_at) VALUES (?, ?, ?, ?, 1, ?, ?, '{}', 0, ?, ?)",
                (channel.id, guild.id if guild else None, level, answer, count, json.dumps([answer]), user.id, time.time()),
            )
            if q is SONG_QUIZ:
                conn.execute("UPDATE song_games SET genre = ? WHERE channel_id = ?", (genre, channel.id))
        if q is SONG_QUIZ and genre:
            header += f"\n-# 장르: {quiz.SONG_GENRES[genre]}"
        works = quiz_works(q, guild.id if guild else None)
        if works:
            header += f"\n-# 이 서버에서 고른 작품 {len(works)}개에서 나옵니다 · 바꾸기: `!캐릭설정`"
        if bet and guild:
            await open_bets(q.key, q.title, channel, guild)
        await quiz_post_question(q, channel, quiz_game(q, channel.id), header)


async def quiz_start_command(q, ctx, options):
    off = game_off_message(q.key, ctx.guild)
    if off:
        await ctx.reply(off, mention_author=False)
        return
    genre = None
    bet, options = split_bet_word(options)
    if bet and not rank_setting("bet", ctx.guild.id if ctx.guild else None):
        await ctx.reply(BET_OFF, mention_author=False)
        return
    if q is SONG_QUIZ:
        genre, options = quiz.split_song_options(options)
    parsed = quiz.parse_start(options)
    if q is CAPITAL_QUIZ and not nara.has_info():
        await ctx.reply("봇의 나라 데이터 파일(assets/countries.json)이 예전 버전이라 수도 맞히기를 할 수 없습니다.",
                        mention_author=False)
        return
    if parsed and q is ANIME_QUIZ and parsed[0] == quiz.HARD:
        await ctx.reply("애니캐 맞히기는 `쉬움`과 `보통`만 있습니다. 예: `!캐릭시작 쉬움 20`", mention_author=False)
        return
    if parsed is None:
        await ctx.reply(f"`!{q.start} [쉬움|보통|어려움] [문제 수 1~{quiz.MAX_COUNT}]`처럼 써 주세요. "
                        f"예: `!{q.start} 어려움 20` · 배팅 판: `!{q.start} 보통 배팅`", mention_author=False)
        return
    await quiz_start(q, ctx.channel, ctx.guild, ctx.author, *parsed, genre=genre, bet=bet)


async def quiz_react(message, emoji):
    try:
        await message.add_reaction(emoji)
    except discord.HTTPException:
        pass


async def quiz_answer_command(q, ctx, text):
    text = text.strip()
    game = quiz_game(q, ctx.channel.id)
    if game is None:
        await ctx.reply(f"이 채널에는 진행 중인 {q.title} 퀴즈가 없습니다. `!{q.start}`으로 시작해 주세요.",
                        mention_author=False)
        return
    if not text:
        await quiz_post_question(q, ctx.channel, game)
        return
    if q is CAPITAL_QUIZ:
        named = quiz.capital_codes(text)  # an unknown name is simply wrong: suggesting one would give it away
    elif q is ANIME_QUIZ:
        named = quiz.anime_codes(text)
    elif q is SONG_QUIZ:
        named = quiz.song_codes(text, song_custom(game["guild_id"]))
    else:
        country = nara.find(text)
        if country is None:
            await ctx.reply(nara_not_found(text), mention_author=False)
            return
        named = {country["code"]}
    async with quiz_lock(q, ctx.channel.id):
        game = quiz_game(q, ctx.channel.id)
        if game is None or game["answer"] not in named:
            last = _quiz_just_answered.get((q.key, ctx.channel.id))
            late = last and last[0] in named and time.monotonic() - last[1] < QUIZ_LATE_SECONDS
            await quiz_react(ctx.message, "⏱️" if late else "❌")
            return
        level, answer = game["level"], game["answer"]
        if len(_quiz_just_answered) >= 300:
            _quiz_just_answered.clear()
        _quiz_just_answered[(q.key, ctx.channel.id)] = (answer, time.monotonic())
        scores = json.loads(game["scores"])
        uid = str(ctx.author.id)
        scores[uid] = scores.get(uid, 0) + 1
        with connect_db() as conn:
            conn.execute(f"UPDATE {q.table}_games SET scores = ? WHERE channel_id = ?", (json.dumps(scores), ctx.channel.id))
            conn.execute(
                f"INSERT INTO {q.table}_scores(guild_id, user_id, {level}) VALUES (?, ?, 1) "
                f"ON CONFLICT(guild_id, user_id) DO UPDATE SET {level} = {level} + 1",
                (ctx.guild.id, ctx.author.id),
            )
            total = conn.execute(f"SELECT {level} FROM {q.table}_scores WHERE guild_id = ? AND user_id = ?",
                                 (ctx.guild.id, ctx.author.id)).fetchone()[0]
        header = (f"-# {ctx.author.mention}님의 정답 · {q.title}\n🎉 {quiz_reveal(q, answer, game['guild_id'])}\n"
                  f"-# {quiz.LEVEL_NAMES[level]} 누적 {total}개\n")
        answer_file = await song_answer_file(game["guild_id"], answer) if q is SONG_QUIZ else None
        await close_bets(q.key, ctx.channel)  # the first answer ends the betting
        await quiz_next(q, ctx.channel, quiz_game(q, ctx.channel.id), header, answer_file)


async def quiz_skip_command(q, ctx):
    async with quiz_lock(q, ctx.channel.id):
        game = quiz_game(q, ctx.channel.id)
        if game is None:
            await ctx.reply(f"이 채널에는 진행 중인 {q.title} 퀴즈가 없습니다.", mention_author=False)
            return
        answer_file = await song_answer_file(game["guild_id"], game["answer"]) if q is SONG_QUIZ else None
        await quiz_next(q, ctx.channel, game,
                        f"-# {ctx.author.mention}님이 넘겼습니다 · {q.title}\n⏭️ 정답: {quiz_reveal(q, game['answer'], game['guild_id'])}\n",
                        answer_file)


async def quiz_stop_command(q, ctx):
    async with quiz_lock(q, ctx.channel.id):
        game = quiz_game(q, ctx.channel.id)
        if game is None:
            await ctx.reply(f"이 채널에는 진행 중인 {q.title} 퀴즈가 없습니다.", mention_author=False)
            return
        with connect_db() as conn:
            conn.execute(f"DELETE FROM {q.table}_games WHERE channel_id = ?", (ctx.channel.id,))
        refunded = refund_bets(q.key, ctx.channel.id)
        answer_file = await song_answer_file(game["guild_id"], game["answer"]) if q is SONG_QUIZ else None
        await ctx.channel.send(f"-# {ctx.author.mention}님이 멈췄습니다 · {q.title}\n"
                               f"🏁 마지막 문제 정답: {quiz_reveal(q, game['answer'], game['guild_id'])}\n-# {quiz_result_line(game)}"
                               + (f"\n{refunded}" if refunded else ""), **({"file": answer_file} if answer_file else {}))


def quiz_ranking_text(q, guild_id, limit=10):
    with connect_db() as conn:
        rows = conn.execute(
            f"SELECT user_id, easy, normal, hard FROM {q.table}_scores WHERE guild_id = ? AND easy + normal + hard > 0 "
            "ORDER BY hard DESC, normal DESC, easy DESC LIMIT ?",
            (guild_id, limit),
        ).fetchall()
    place = rank_prefix(guild_id)
    hard = (lambda r: "") if q is ANIME_QUIZ else (lambda r: f" · 어려움 {r['hard']}개")  # 애니캐 has no 어려움
    return "\n".join(f"{place(i)}<@{r['user_id']}> · 쉬움 {r['easy']}개 · 보통 {r['normal']}개{hard(r)}"
                      for i, r in enumerate(rows, 1))


async def quiz_ranking_command(q, ctx):
    text = quiz_ranking_text(q, ctx.guild.id)
    if not text:
        await ctx.reply(f"아직 이 서버에서 {q.title} 퀴즈를 맞힌 사람이 없습니다. `!{q.start}`으로 시작해 보세요!",
                        mention_author=False)
        return
    embed = discord.Embed(title=f"🏆 {q.title} 순위", description=text, color=discord.Color.green())
    embed.set_footer(text="맞힌 개수 · 어려움이 많은 순, 같으면 보통·쉬움 순 · 모든 게임을 합친 순위: !순위")
    await ctx.reply(embed=embed, mention_author=False)


# ---------------------------------------------------------------- XP 배팅
# A game started with "배팅" (`!국기시작 보통 배팅`, `!나라시작 배팅`) takes bets for BET_OPEN_SECONDS or until the
# first answer. Only people who bet are affected: at the end the bettor(s) with the most correct answers take
# the whole pot (a tie splits it); if no bettor scored, or fewer than two people bet, everyone gets their XP back.
# XP only moves between bettors (nothing is created), and one person can win at most BET_DAILY_WIN XP a day.
BET_OPEN_SECONDS = 30
BET_MIN, BET_MAX = 1, 50
BET_DAILY_WIN = 100
BET_WORDS = {"배팅", "베팅", "bet", "내기"}
BET_OFF = "이 서버에서는 XP 배팅을 쓸 수 없습니다. (관리자 설정)"
_bet_tasks = set()


def split_bet_word(text):
    """(betting?, the rest) from what follows a start command: '보통 배팅 20' -> (True, '보통 20')."""
    words = text.split()
    rest = [w for w in words if w.lower() not in BET_WORDS]
    return len(rest) != len(words), " ".join(rest)


_bet_live = None  # {(game, channel id)} with a bet round: lets normal games skip the DB look-up on every answer


def bet_live():
    global _bet_live
    if _bet_live is None:
        with connect_db() as conn:
            _bet_live = {(r["game"], r["channel_id"]) for r in conn.execute("SELECT game, channel_id FROM bet_rounds")}
    return _bet_live


def bet_round(game, channel_id):
    if (game, channel_id) not in bet_live():
        return None
    with connect_db() as conn:
        row = conn.execute("SELECT * FROM bet_rounds WHERE game = ? AND channel_id = ?", (game, channel_id)).fetchone()
    return dict(row) if row else None


def bet_round_in(channel_id):
    if not any(ch == channel_id for _, ch in bet_live()):
        return None
    with connect_db() as conn:
        row = conn.execute("SELECT * FROM bet_rounds WHERE channel_id = ? ORDER BY open_until DESC LIMIT 1",
                           (channel_id,)).fetchone()
    return dict(row) if row else None


def xp_of(guild_id, user_id):
    return sum(r[5] for r in xp_breakdown(guild_id, user_id))


def staked_elsewhere(guild_id, user_id, game, channel_id):
    """XP this person has riding on other bet games in this server (it can't be bet twice)."""
    with connect_db() as conn:
        rows = conn.execute("SELECT game, channel_id, bets FROM bet_rounds WHERE guild_id = ?", (guild_id,)).fetchall()
    return sum(json.loads(r["bets"]).get(str(user_id), 0) for r in rows if (r["game"], r["channel_id"]) != (game, channel_id))


def won_today(guild_id, user_id):
    start = time.time() - (time.time() + 9 * 3600) % 86400  # midnight, Korean time
    with connect_db() as conn:
        return conn.execute("SELECT COALESCE(SUM(delta), 0) FROM xp_bets WHERE guild_id = ? AND user_id = ? "
                            "AND delta > 0 AND at >= ?", (guild_id, user_id, start)).fetchone()[0]


async def open_bets(game, title, channel, guild):
    """Make this game a bet game and tell the channel how to join; the window closes on its own."""
    until = time.time() + BET_OPEN_SECONDS
    with connect_db() as conn:
        conn.execute("INSERT OR REPLACE INTO bet_rounds(game, channel_id, guild_id, open_until, bets) VALUES (?, ?, ?, ?, '{}')",
                     (game, channel.id, guild.id, until))
    bet_live().add((game, channel.id))
    await channel.send(f"🎲 **배팅 판**입니다! {BET_OPEN_SECONDS}초 안에(첫 정답 전까지) `!배팅 <XP>`로 참여하세요. "
                       f"({BET_MIN}~{BET_MAX} XP)\n-# 배팅한 사람 중 가장 많이 맞힌 사람이 판돈을 모두 가져갑니다 · "
                       "배팅하지 않으면 XP는 그대로예요")

    async def close_later():
        await asyncio.sleep(BET_OPEN_SECONDS + 1)
        await close_bets(game, channel)
    task = asyncio.get_running_loop().create_task(close_later())
    _bet_tasks.add(task)
    task.add_done_callback(_bet_tasks.discard)


def bet_list_text(bets):
    pot = sum(bets.values())
    people = " · ".join(f"<@{uid}> {xp_text(n)}" for uid, n in bets.items())
    return f"🎲 배팅 마감 · 판돈 **{xp_text(pot)}** ({len(bets)}명)\n-# {people}" if bets else "🎲 배팅 마감 · 아무도 배팅하지 않았습니다."


async def close_bets(game, channel):
    """Stop taking bets (first answer, or the time is up) and show who is in."""
    row = bet_round(game, channel.id)
    if not row or row["open_until"] == 0:
        return
    with connect_db() as conn:
        conn.execute("UPDATE bet_rounds SET open_until = 0 WHERE game = ? AND channel_id = ?", (game, channel.id))
    bets = json.loads(row["bets"])
    note = "" if len(bets) != 1 else "\n-# 배팅한 사람이 한 명뿐이라 판이 끝나면 돌려받습니다."
    try:
        await channel.send(bet_list_text(bets) + note)
    except discord.HTTPException:
        pass


def refund_bets(game, channel_id):
    """The game was stopped or given up: nothing is settled, everyone keeps their XP."""
    row = bet_round(game, channel_id)
    if not row:
        return None
    with connect_db() as conn:
        conn.execute("DELETE FROM bet_rounds WHERE game = ? AND channel_id = ?", (game, channel_id))
    bet_live().discard((game, channel_id))
    return "-# 🎲 배팅은 모두 돌려받았습니다." if json.loads(row["bets"]) else None


def settle_bets(game, channel_id, scores):
    """At the end of a bet game: scores {user id: correct answers}. Returns the text to post, or None."""
    row = bet_round(game, channel_id)
    if not row:
        return None
    with connect_db() as conn:
        conn.execute("DELETE FROM bet_rounds WHERE game = ? AND channel_id = ?", (game, channel_id))
    bet_live().discard((game, channel_id))
    bets = {str(k): v for k, v in json.loads(row["bets"]).items()}
    if not bets:
        return None
    pot = sum(bets.values())
    best = max((scores.get(uid, 0) for uid in bets), default=0)
    if len(bets) < 2 or best == 0:
        why = "배팅한 사람이 한 명뿐이라" if len(bets) < 2 else "배팅한 사람 중 맞힌 사람이 없어"
        return f"🎲 {why} 배팅한 XP를 모두 돌려받았습니다. (판돈 {xp_text(pot)})"
    winners = [uid for uid in bets if scores.get(uid, 0) == best]
    losers = [uid for uid in bets if uid not in winners]
    if not losers:
        return f"🎲 배팅한 사람 모두 {best}개로 같아서 판돈을 돌려받았습니다."
    prize = sum(bets[uid] for uid in losers)
    share = prize / len(winners)
    now, guild_id = time.time(), row["guild_id"]
    lines, rows = [f"🎲 **배팅 결과** · 판돈 {xp_text(pot)}"], []
    for uid in winners:
        room = max(0.0, BET_DAILY_WIN - won_today(guild_id, int(uid)))
        gain = min(share, room)
        rows.append((guild_id, int(uid), gain, game, now))
        capped = " (오늘 배팅으로 얻을 수 있는 XP 한도에 걸림)" if gain < share else ""
        lines.append(f"🏆 <@{uid}> **+{xp_text(gain)}** (맞힌 개수 {scores.get(uid, 0)}){capped}")
    for uid in losers:
        rows.append((guild_id, int(uid), -float(bets[uid]), game, now))
        lines.append(f"-# <@{uid}> −{xp_text(bets[uid])} (맞힌 개수 {scores.get(uid, 0)})")
    with connect_db() as conn:
        conn.executemany("INSERT INTO xp_bets(guild_id, user_id, delta, game, at) VALUES (?, ?, ?, ?, ?)",
                         [r for r in rows if r[2]])
    _global_xp["at"] = 0.0
    return "\n".join(lines)


@bot.command(name="배팅", aliases=["베팅", "bet"])
@commands.guild_only()
async def bet_command(ctx, amount: str = ""):
    row = bet_round_in(ctx.channel.id)
    if row is None:
        await ctx.reply("이 채널에는 배팅 판이 없습니다. `!국기시작 보통 배팅`처럼 `배팅`을 붙여 시작해 주세요.",
                        mention_author=False)
        return
    if row["open_until"] < time.time():
        await ctx.reply("배팅이 이미 마감됐습니다. 다음 배팅 판을 기다려 주세요!", mention_author=False)
        return
    try:
        value = float(amount)
    except ValueError:
        value = 0
    if value != int(value) or not BET_MIN <= value <= BET_MAX:
        await ctx.reply(f"`!배팅 <XP>`처럼 {BET_MIN}~{BET_MAX} 사이 정수로 써 주세요. 예: `!배팅 10`", mention_author=False)
        return
    value = int(value)
    have = xp_of(ctx.guild.id, ctx.author.id) - staked_elsewhere(ctx.guild.id, ctx.author.id, row["game"], ctx.channel.id)
    if value > have:
        await ctx.reply(f"XP가 부족합니다. 걸 수 있는 XP: **{xp_text(max(have, 0))}**", mention_author=False)
        return
    with connect_db() as conn:
        current = conn.execute("SELECT bets, open_until FROM bet_rounds WHERE game = ? AND channel_id = ?",
                               (row["game"], ctx.channel.id)).fetchone()
        if current is None or current["open_until"] < time.time():
            await ctx.reply("배팅이 이미 마감됐습니다.", mention_author=False)
            return
        bets = json.loads(current["bets"])
        changed = str(ctx.author.id) in bets
        bets[str(ctx.author.id)] = value
        conn.execute("UPDATE bet_rounds SET bets = ? WHERE game = ? AND channel_id = ?",
                     (json.dumps(bets), row["game"], ctx.channel.id))
    left = max(1, int(current["open_until"] - time.time()))
    await ctx.reply(f"🎲 {'배팅을 바꿨습니다' if changed else '배팅했습니다'}: **{value} XP** · 판돈 {xp_text(sum(bets.values()))} "
                    f"({len(bets)}명) · {left}초 뒤 마감", mention_author=False)


# ---------------------------------------------------------------- 랭킹 (XP)
# One ranking over all games, worked out from the per-game records that are already kept (so past
# answers count and nothing new is stored). 지리 (나라 한 판, 수도·지도 한 문제) 1 XP; 국기·애니캐·노래 0.5 XP.
XP_SOURCES = [  # (key, title, emoji, table, count SQL, XP each, unit)
    ("nara", "나라 맞히기", "🌍", "nara_scores", "easy_wins + wins + hard_wins", 1.0, "승"),
    ("capital", "수도 맞히기", "🏛️", "capital_scores", "easy + normal + hard", 1.0, "개"),
    ("map", "지도 맞히기", "🗺️", "map_scores", "easy + normal + hard", 1.0, "개"),
    ("flag", "국기 맞히기", "🚩", "flag_scores", "easy + normal + hard", 0.5, "개"),
    ("anime", "애니캐 맞히기", "🎌", "anime_scores", "easy + normal + hard", 0.5, "개"),
    ("song", "노래 맞히기", "🎵", "song_scores", "easy + normal + hard", 0.5, "개"),
]
RANK_PAGE_SIZE = 15
# Admin panel switches (settings rows "rank:show:default", "rank:show:<server id>", "rank:global:..."):
# show  = 순위 보이기 (medals, 1. 2. 3., "n위"); global = 전체 랭킹 (!순위 전체, and the server's records count in it).
# A server without its own value follows the default; the defaults start off.
RANK_SETTINGS = {"show": "순위 보이기", "global": "전체 랭킹", "bet": "XP 배팅"}
RANK_DEFAULTS = {"show": False, "global": False, "bet": True}  # until the admin panel sets a default


def rank_values():
    if "rank" not in _switch_cache:
        with connect_db() as conn:
            _switch_cache["rank"] = {r["key"]: r["value"] == "1" for r in conn.execute(
                "SELECT key, value FROM settings WHERE key LIKE 'rank:%'")}
    return _switch_cache["rank"]


def rank_setting(name, guild_id=None):
    values = rank_values()
    if guild_id and f"rank:{name}:{guild_id}" in values:
        return values[f"rank:{name}:{guild_id}"]
    return values.get(f"rank:{name}:default", RANK_DEFAULTS.get(name, False))


def global_rank_guilds():
    """Servers whose records make up the 전체 랭킹: those where it is switched on."""
    return [g.id for g in bot.guilds if rank_setting("global", g.id)]


def xp_text(xp):
    return f"{xp:g} XP"


def xp_totals(guild_id=None, guild_ids=None):
    """[(user id, xp)] best first, for one server, or those servers (guild_ids) added up."""
    if guild_id:
        where, ids = "WHERE guild_id = ?", [guild_id]
    else:
        ids = list(guild_ids or [])
        if not ids:
            return []
        where = f"WHERE guild_id IN ({','.join('?' * len(ids))})"
    parts = [f"SELECT user_id, ({count}) * {weight} AS xp FROM {table} {where}"
             for _, _, _, table, count, weight, _ in XP_SOURCES]
    parts.append(f"SELECT user_id, delta AS xp FROM xp_bets {where}")  # XP won or lost in 배팅
    params = ids * len(parts)
    with connect_db() as conn:
        rows = conn.execute(f"SELECT user_id, SUM(xp) AS xp FROM ({' UNION ALL '.join(parts)}) "
                            "GROUP BY user_id HAVING SUM(xp) > 0 ORDER BY SUM(xp) DESC, user_id", params).fetchall()
    return [(r["user_id"], r["xp"]) for r in rows]


def xp_breakdown(guild_id, user_id):
    """[(key, title, emoji, count, levels text, xp, unit)] of one person's records in one server."""
    out = []
    with connect_db() as conn:
        for key, title, emoji, table, count, weight, unit in XP_SOURCES:
            if key == "nara":
                row = conn.execute("SELECT easy_wins, wins, hard_wins FROM nara_scores WHERE guild_id = ? AND user_id = ?",
                                   (guild_id, user_id)).fetchone()
                levels = (row["easy_wins"], row["wins"], row["hard_wins"]) if row else (0, 0, 0)
            else:
                row = conn.execute(f"SELECT easy, normal, hard FROM {table} WHERE guild_id = ? AND user_id = ?",
                                   (guild_id, user_id)).fetchone()
                levels = (row["easy"], row["normal"], row["hard"]) if row else (0, 0, 0)
            total = sum(levels)
            names = ("쉬움", "보통") if key == "anime" else ("쉬움", "보통", "어려움")  # 애니캐 has no 어려움
            detail = " · ".join(f"{n} {v}" for n, v in zip(names, levels) if v)
            out.append((key, title, emoji, total, detail, total * weight, unit))
        row = conn.execute("SELECT COALESCE(SUM(delta), 0), COALESCE(SUM(delta > 0), 0), COALESCE(SUM(delta < 0), 0) "
                           "FROM xp_bets WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)).fetchone()
        if row[1] or row[2]:
            out.append(("bet", "XP 배팅", "🎲", row[1], f"{row[1]}승 {row[2]}패", row[0], "승"))
    return out


_rank_names = {}  # user id -> (name, when looked up), for the all-servers ranking


async def rank_name(user_id):
    """A display name for someone who may not be in this server (mentions of strangers show as @알 수 없음)."""
    cached = _rank_names.get(user_id)
    if cached and time.time() - cached[1] < 3600:
        return cached[0]
    user = bot.get_user(user_id)
    if user is None:
        try:
            user = await bot.fetch_user(user_id)
        except discord.HTTPException:
            user = None
    name = discord.utils.escape_markdown(user.display_name if user else "알 수 없는 사용자")
    if len(_rank_names) >= 500:
        _rank_names.clear()
    _rank_names[user_id] = (name, time.time())
    return name


MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


def rank_prefix(guild_id, plain=False):
    """place -> '🥇 ' / '`4.` ' (or '4. ' when plain), or '' for every place while 순위 보이기 is off here.
    Used by !순위 and by each game's own ranking, so the admin switch hides places everywhere."""
    if not rank_setting("show", guild_id):
        return lambda place: ""
    return lambda place: MEDALS.get(place, f"{place}." if plain else f"`{place}.`") + " "


_global_xp = {"at": 0.0, "rows": [], "guilds": None}  # the all-servers ranking, reused for a minute


async def global_xp_totals():
    """The servers that take part, added up: grows with every player everywhere, so it is counted in a
    worker thread (the bot keeps answering meanwhile) and reused for a minute."""
    guilds = global_rank_guilds()
    if time.time() - _global_xp["at"] > 60 or _global_xp["guilds"] != guilds:
        _global_xp["rows"] = await asyncio.to_thread(xp_totals, None, guilds)
        _global_xp.update(at=time.time(), guilds=guilds)
    return _global_xp["rows"]


def rank_line(place, who, xp, show):
    prefix = f"{MEDALS.get(place, f'`{place}.`')} " if show else ""
    return f"{prefix}{who} · **{xp_text(xp)}**"


async def ranking_embed(guild, viewer_id, everywhere=False, page=0):
    """One page (RANK_PAGE_SIZE people) of the XP list, best first. Returns (embed, page, pages)."""
    try:
        totals = await global_xp_totals() if everywhere else xp_totals(guild.id)
    except sqlite3.Error as exc:
        mark_health("ranking", False, f"순위 계산 오류 · {type(exc).__name__}")
        raise
    mark_health("ranking", True)
    show = rank_setting("show", guild.id)
    title = "🏆 전체 서버 순위" if everywhere else "🏆 서버 순위"
    if not totals:
        return discord.Embed(title=title, description="아직 게임 기록이 없습니다. `/도움말` → 게임에서 시작해 보세요!",
                             color=discord.Color.gold()), 0, 1
    pages = (len(totals) + RANK_PAGE_SIZE - 1) // RANK_PAGE_SIZE
    page = max(0, min(page, pages - 1))
    chunk = totals[page * RANK_PAGE_SIZE:(page + 1) * RANK_PAGE_SIZE]
    # Names of people from other servers are looked up all at once, not one after another.
    names = await asyncio.gather(*(rank_name(uid) for uid, _ in chunk)) if everywhere else [f"<@{uid}>" for uid, _ in chunk]
    first = page * RANK_PAGE_SIZE + 1
    lines = [rank_line(first + i, who, xp, show) for i, ((uid, xp), who) in enumerate(zip(chunk, names))]
    embed = discord.Embed(title=title, description="\n".join(lines), color=discord.Color.gold())
    mine = next(((i, xp) for i, (uid, xp) in enumerate(totals, 1) if uid == viewer_id), None)
    me = ("아직 기록 없음" if not mine else f"{mine[0]}위 · {xp_text(mine[1])}" if show else xp_text(mine[1]))
    extra = " · 모든 서버: !순위 전체" if rank_setting("global", guild.id) and not everywhere else ""
    embed.set_footer(text=f"{'내 순위' if show else '내 XP'}: {me} · {page + 1}/{pages}쪽 · "
                          f"지리 1 XP · 국기·애니캐·노래 0.5 XP{extra}")
    return embed, page, pages


class RankPageButton(discord.ui.DynamicItem[discord.ui.Button],
                     template=r"maco:rank:(?P<scope>[sg]):(?P<page>\d+):(?P<way>[pn])"):
    """◀ / ▶ under a ranking (the ! command's message or the /도움말 page). Anyone may turn the pages."""

    def __init__(self, scope, page, way, disabled=False):
        super().__init__(discord.ui.Button(
            emoji="◀" if way == "p" else "▶", label="이전" if way == "p" else "다음", disabled=disabled, row=3,
            style=discord.ButtonStyle.secondary, custom_id=f"maco:rank:{scope}:{page}:{way}"))
        self.scope, self.page = scope, page

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["scope"], int(match["page"]), match["way"])

    async def callback(self, interaction):
        if interaction.guild is None:
            return
        if interaction.message is not None and interaction.message.flags.ephemeral:  # the /도움말 page
            await help_run_ranking(interaction, everywhere=self.scope == "g", page=self.page, shown=True)
            return
        embed, page, pages = await ranking_embed(interaction.guild, interaction.user.id, self.scope == "g", self.page)
        await interaction.response.edit_message(embed=embed, view=rank_page_view(self.scope, page, pages))


def rank_page_items(scope, page, pages):
    if pages <= 1:
        return []
    return [RankPageButton(scope, max(page - 1, 0), "p", disabled=page == 0),
            RankPageButton(scope, min(page + 1, pages - 1), "n", disabled=page >= pages - 1)]


def rank_page_view(scope, page, pages):
    view = discord.ui.View(timeout=None)
    for item in rank_page_items(scope, page, pages):
        view.add_item(item)
    return view


def ranking_detail_embed(guild, member):
    rows = xp_breakdown(guild.id, member.id)
    total = sum(r[5] for r in rows)
    place = None
    if rank_setting("show", guild.id):
        totals = xp_totals(guild.id)
        place = next((i for i, (uid, _) in enumerate(totals, 1) if uid == member.id), None)
    name = discord.utils.escape_markdown(member.display_name)
    embed = discord.Embed(title=f"📊 {name}님의 기록",
                          description=f"**{xp_text(total)}**" + (f" · 서버 {place}위" if place else ""),
                          color=discord.Color.gold())
    for key, title, emoji, count, detail, xp, unit in rows:
        if key == "bet":
            embed.add_field(name=f"{emoji} {title}", value=f"**{detail}**\n→ {'+' if xp >= 0 else ''}{xp_text(xp)}", inline=True)
            continue
        embed.add_field(name=f"{emoji} {title}",
                        value=f"**{count}{unit}**" + (f" ({detail})" if detail else "") + f"\n→ {xp_text(xp)}", inline=True)
    embed.set_footer(text="지리(나라 한 판·수도·지도) 1 XP · 국기·애니캐·노래 0.5 XP · 출제 판은 제외")
    return embed


async def find_member(ctx, text):
    """@멘션, ID or (display/user) name in this server, or None."""
    text = text.strip()
    if not text:
        return ctx.author
    try:
        return await commands.MemberConverter().convert(ctx, text)
    except commands.BadArgument:
        pass
    try:
        found = await ctx.guild.query_members(query=text, limit=1)
    except (discord.HTTPException, discord.ClientException, asyncio.TimeoutError):
        found = []
    return found[0] if found else None


GLOBAL_RANK_OFF = "이 서버에서는 전체 랭킹을 쓸 수 없습니다. (관리자 설정)"


@bot.command(name="xp", aliases=["내xp", "XP확인"])
@commands.guild_only()
async def xp_command(ctx, *, who: str = ""):
    """!xp: my XP · !xp @사람 / 이름: someone else's."""
    member = await find_member(ctx, who)
    if member is None:
        await ctx.reply(f"이 서버에서 `{discord.utils.escape_markdown(who[:40])}`님을 찾지 못했습니다. "
                        "`!xp @멘션`처럼 써 주세요.", mention_author=False)
        return
    await ctx.reply(embed=ranking_detail_embed(ctx.guild, member), mention_author=False)


@bot.command(name="순위", aliases=["랭킹", "경험치"])
@commands.guild_only()
async def ranking_command(ctx, action: str = "", *, rest: str = ""):
    """!순위 · !순위 전체 · !순위 자세히 [이름|@멘션]"""
    if action in ("전체", "모든서버", "all"):
        if not rank_setting("global", ctx.guild.id):
            await ctx.reply(GLOBAL_RANK_OFF, mention_author=False)
            return
        embed, page, pages = await ranking_embed(ctx.guild, ctx.author.id, everywhere=True)
        await ctx.reply(embed=embed, view=rank_page_view("g", page, pages), mention_author=False)
        return
    if action in ("자세히", "상세", "기록", "detail"):
        member = await find_member(ctx, rest)
        if member is None:
            await ctx.reply(f"이 서버에서 `{discord.utils.escape_markdown(rest[:40])}`님을 찾지 못했습니다. "
                            "`!순위 자세히 @멘션`처럼 써 주세요.", mention_author=False)
            return
        await ctx.reply(embed=ranking_detail_embed(ctx.guild, member), mention_author=False)
        return
    if action:
        await ctx.reply("`!순위`, `!순위 전체`, `!순위 자세히 [이름]`처럼 써 주세요.", mention_author=False)
        return
    embed, page, pages = await ranking_embed(ctx.guild, ctx.author.id)
    await ctx.reply(embed=embed, view=rank_page_view("s", page, pages), mention_author=False)


def embed_as_text(embed):
    """An embed's content as the /도움말 page text (title, lines, fields, footer)."""
    lines = [f"**{embed.title}**"]
    if embed.description:
        lines.append(embed.description)
    lines += [f"{f.name} · {f.value.replace(chr(10), ' ')}" for f in embed.fields]
    if embed.footer and embed.footer.text:
        lines.append(f"-# {embed.footer.text}")
    return "\n".join(lines)


async def help_run_ranking(interaction, everywhere=False, page=0, shown=False):
    """/도움말 → 게임 → 🏆 순위. shown=True: the ranking itself; else just the [전체] [개인] choice."""
    if interaction.guild is None:
        await show_help(interaction, notice=SERVER_ONLY_MESSAGE)
        return
    if everywhere and not rank_setting("global", interaction.guild.id):
        await show_help(interaction, page="game", selected="ranking", extra_items=ranking_buttons(interaction.guild.id),
                        notice=GLOBAL_RANK_OFF)
        return
    if not shown and not everywhere:
        await show_help(interaction, page="game", selected="ranking", extra_items=ranking_buttons(interaction.guild.id),
                        details="**🏆 전체**는 서버 XP 순서, **👤 개인**은 원하는 사람의 게임별 기록이에요.")
        return
    scope = "g" if everywhere else "s"
    embed, page, pages = await ranking_embed(interaction.guild, interaction.user.id, everywhere, page)
    await show_help(interaction, page="game", selected="ranking",
                    extra_items=ranking_buttons(interaction.guild.id) + [RankShareButton(scope, 0, page)]
                    + rank_page_items(scope, page, pages),
                    details=embed_as_text(embed))


class PersonPickModal(discord.ui.Modal, title="개인 XP 보기"):
    """[👤 개인]: a member picker (Discord's own search), starting on the person who opened it."""

    def __init__(self, user_id):
        super().__init__()
        self.who = discord.ui.UserSelect(placeholder="이름으로 검색해서 고르세요", min_values=1, max_values=1,
                                         default_values=[discord.Object(id=user_id)])
        self.add_item(discord.ui.Label(text="누구의 XP를 볼까요?", description="처음에는 '나'가 골라져 있어요",
                                       component=self.who))

    async def on_submit(self, interaction):
        picked = self.who.values[0] if self.who.values else interaction.user
        member = interaction.guild.get_member(picked.id) or picked
        await help_show_person(interaction, member)


async def help_show_person(interaction, member):
    embed = ranking_detail_embed(interaction.guild, member)
    await show_help(interaction, page="game", selected="ranking",
                    extra_items=ranking_buttons(interaction.guild.id) + [RankShareButton("u", member.id, 0)],
                    details=embed_as_text(embed))


async def help_run_ranking_me(interaction):
    if interaction.guild is None:
        await show_help(interaction, notice=SERVER_ONLY_MESSAGE)
        return
    await interaction.response.send_modal(PersonPickModal(interaction.user.id))


_share_last = {}  # user id -> when they last posted a ranking to the channel (a small spam guard)


class RankShareButton(discord.ui.DynamicItem[discord.ui.Button],
                      template=r"maco:rankshare:(?P<kind>[sgu]):(?P<target>\d+):(?P<page>\d+)"):
    """📢 채팅방에 보내기 under a ranking or a person's XP on the private /도움말 page: posts that view to the
    channel for everyone. Only the person whose /도움말 it is can see (and so press) it."""

    def __init__(self, kind, target, page):
        super().__init__(discord.ui.Button(label="채팅방에 보내기", emoji="📢", style=discord.ButtonStyle.primary, row=3,
                                           custom_id=f"maco:rankshare:{kind}:{target}:{page}"))
        self.kind, self.target, self.page = kind, target, page

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["kind"], int(match["target"]), int(match["page"]))

    async def callback(self, interaction):
        if interaction.guild is None:
            return
        now = time.time()
        if now - _share_last.get(interaction.user.id, 0) < 10:
            await interaction.response.send_message("⏳ 잠시 뒤에 다시 보내 주세요.", ephemeral=True)
            return
        _share_last[interaction.user.id] = now
        if len(_share_last) > 500:
            _share_last.clear()
        if self.kind == "u":
            member = interaction.guild.get_member(self.target)
            if member is None:
                try:
                    member = await interaction.guild.fetch_member(self.target)
                except discord.HTTPException:  # left the server: their records are still there
                    member = type("Gone", (), {"id": self.target, "display_name": "알 수 없는 사용자"})()
            embed, what = ranking_detail_embed(interaction.guild, member), "XP"
        else:
            if self.kind == "g" and not rank_setting("global", interaction.guild.id):
                await interaction.response.send_message(GLOBAL_RANK_OFF, ephemeral=True)
                return
            embed, _, _ = await ranking_embed(interaction.guild, interaction.user.id, self.kind == "g", self.page)
            what = "전체 서버 순위" if self.kind == "g" else "서버 순위"
        await interaction.response.defer()
        ctx = HelpContext(interaction, what)
        await run_from_help(ctx, ctx.reply(embed=embed))


def ranking_buttons(guild_id=None):
    # (not "run_ranking": that is the 순위 tab button on the same page, and Discord refuses repeated ids)
    items = [HelpButton("ranking_server", "전체", "🏆", discord.ButtonStyle.primary, row=2),
             HelpButton("ranking_me", "개인", "👤", discord.ButtonStyle.primary, row=2)]
    if rank_setting("global", guild_id):
        items.append(HelpButton("ranking_all", "모든 서버", "🌐", discord.ButtonStyle.secondary, row=2))
    return items


def quiz_register(q, start_aliases, answer_aliases, skip_aliases, stop_aliases, rank_aliases):
    """The quiz's ! commands; each only passes its kind to the shared code above."""
    guild = commands.guild_only()

    async def start(ctx, *, options: str = ""):
        await quiz_start_command(q, ctx, options)

    async def answer(ctx, *, text: str = ""):
        await quiz_answer_command(q, ctx, text)

    async def skip(ctx):
        await quiz_skip_command(q, ctx)

    async def stop(ctx):
        await quiz_stop_command(q, ctx)

    async def rank(ctx):
        await quiz_ranking_command(q, ctx)

    for func, name, aliases in ((start, q.start, start_aliases), (answer, q.answer, answer_aliases),
                                (skip, q.skip, skip_aliases), (stop, q.stop, stop_aliases), (rank, q.rank, rank_aliases)):
        bot.command(name=name, aliases=aliases)(guild(func))


quiz_register(FLAG_QUIZ, ["국기맞히기"], [], ["국기패스", "국기넘기기"], ["국기그만"], ["국기랭킹"])
quiz_register(CAPITAL_QUIZ, ["수도맞히기"], [], ["수도패스", "수도넘기기"], ["수도그만"], ["수도랭킹"])
quiz_register(MAP_QUIZ, ["지도맞히기"], [], ["지도패스", "지도넘기기"], ["지도그만"], ["지도랭킹"])
quiz_register(SONG_QUIZ, ["노래맞히기"], ["곡"], ["노래패스", "노래넘기기"], ["노래그만"], ["노래랭킹"])
quiz_register(ANIME_QUIZ, ["캐릭맞히기", "애니캐"], ["캐릭터"], ["캐릭패스", "캐릭넘기기"], ["캐릭그만"], ["캐릭랭킹"])


@bot.command(name="힌트")
@commands.guild_only()
async def quiz_hint_command(ctx):
    """The initial consonants of the answer of every quiz going on in this channel."""
    lines = []
    for q in QUIZZES:
        game = quiz_game(q, ctx.channel.id)
        if game:
            word = quiz_hint_word(q, game["answer"], game["guild_id"])
            if q is SONG_QUIZ and not game["hinted"]:  # first the singer, then the initials
                with connect_db() as conn:
                    conn.execute("UPDATE song_games SET hinted = 1 WHERE channel_id = ?", (ctx.channel.id,))
                info = song_info(game["guild_id"], game["answer"]) or {"artist": "?"}
                lines.append(f"💡 {q.title} 힌트: **{info['artist']}**의 노래입니다. (한 번 더 `!힌트`면 초성)")
                continue
            if q is ANIME_QUIZ and not game["hinted"]:  # first the work, then the initials
                with connect_db() as conn:
                    conn.execute(f"UPDATE {q.table}_games SET hinted = 1 WHERE channel_id = ?", (ctx.channel.id,))
                lines.append(f"💡 {q.title} 힌트: 『{quiz.anime()[game['answer']]['work']}』의 캐릭터입니다. "
                             "(한 번 더 `!힌트`면 초성)")
                continue
            lines.append(f"💡 {q.title} 초성 힌트: **{quiz.chosung(word)}** ({len(word.replace(' ', ''))}글자)")
    await ctx.reply("\n".join(lines) or "이 채널에는 진행 중인 퀴즈가 없습니다.", mention_author=False)


async def anime_settings_link(guild):
    """(url, None) for the server's 작품 고르기 page, or (None, why not)."""
    if not dashboard.configured():
        return None, "봇에 사이트(NKOTE) 연결이 설정되지 않아 작품 고르기를 쓸 수 없습니다."
    works = anime_works_of(guild.id)
    try:
        url = await dashboard.anime_link(guild.id, guild.name, quiz.anime_works(), sorted(works) if works else None)
        return url, None
    except dashboard.NotBound:
        return None, "사이트에 연결된 봇 키가 아니라 링크를 만들 수 없습니다. 대시보드 연결을 확인해 주세요."
    except Exception as exc:
        print(f"작품 고르기 링크 실패: {type(exc).__name__}: {exc}")
        return None, "링크를 만들지 못했습니다. 잠시 후 다시 시도해 주세요."


@bot.command(name="캐릭설정", aliases=["애니캐설정", "캐릭작품"])
@commands.guild_only()
async def anime_settings_command(ctx):
    """The link goes by DM: anyone with it can change the server's works for 10 minutes."""
    off = game_off_message("anime", ctx.guild)
    if off:
        await ctx.reply(off, mention_author=False)
        return
    url, problem = await anime_settings_link(ctx.guild)
    if problem:
        await ctx.reply(problem, mention_author=False)
        return
    view = discord.ui.View().add_item(discord.ui.Button(label="작품 고르기", emoji="🎌", url=url))
    try:
        await ctx.author.send(f"🎌 **{ctx.guild.name}** 애니캐 맞히기에서 나올 작품을 고르는 링크입니다. "
                              "**10분** 동안 쓸 수 있습니다.", view=view)
    except discord.HTTPException:
        await ctx.reply("DM을 보낼 수 없습니다. DM을 열거나 `/도움말` → 게임 → 애니캐 맞히기 → ⚙️ 작품 고르기를 써 주세요.",
                        mention_author=False)
        return
    await ctx.reply("📬 DM으로 작품 고르기 링크를 보냈습니다. (10분 동안 유효)", mention_author=False)


async def help_anime_settings(interaction):
    if interaction.guild is None:
        await show_help(interaction, notice=SERVER_ONLY_MESSAGE)
        return
    off = game_off_message("anime", interaction.guild)
    if off:
        await show_help(interaction, page="game", notice=off)
        return
    await interaction.response.defer()
    url, problem = await anime_settings_link(interaction.guild)
    if problem:
        await show_quiz_page(ANIME_QUIZ, interaction, details=problem)
        return
    link = discord.ui.Button(label="작품 고르기 열기", emoji="🌐", url=url, row=3)
    await show_quiz_page(ANIME_QUIZ, interaction, details="🎌 이 서버에서 나올 작품을 고르는 링크입니다. **10분** 동안 쓸 수 있습니다.",
                         extra=[link])


ANIME_SUGGEST_COOLDOWN = 60
_anime_suggest_last = {}  # user id -> when they last suggested a work


def anime_list_embed(guild_id, work=None):
    """!애니 리스트: every work in 애니캐 맞히기 (✅ = used in this server), or one work's characters."""
    works = quiz.anime_works()
    chosen = anime_works_of(guild_id)
    if work:
        wanted = nara.normalize(work)
        found = next((w for w in works if nara.normalize(w["title"]) == wanted), None) or \
            next((w for w in works if wanted in nara.normalize(w["title"])), None)
        if not found:
            return None
        stars = {c["ko"] for c in quiz.anime().values() if c["work"] == found["title"] and c["star"]}
        names = " · ".join(f"★{n}" if n in stars else n for n in found["chars"])
        embed = discord.Embed(title=f"🎌 『{found['title']}』 캐릭터 {len(found['chars'])}명", description=names,
                              color=discord.Color.green())
        embed.set_footer(text="★ 쉬움에도 나오는 캐릭터")
        return embed
    lines = [f"{'✅' if chosen is None or w['title'] in chosen else '▫️'} {w['title']} · {len(w['chars'])}명" for w in works]
    scope = "모든 작품" if chosen is None else f"고른 작품 {len(chosen)}개 (✅)"
    embed = discord.Embed(title=f"🎌 애니캐 맞히기 작품 {len(works)}개", description="\n".join(lines)[:4096],
                          color=discord.Color.green())
    embed.set_footer(text=f"이 서버: {scope} · 캐릭터 보기: !애니 리스트 <작품> · 고르기: !캐릭설정 · 추천: !애니 추천 <작품>")
    return embed


async def anilist_find(title):
    """(display title, url) of the best AniList match for a work's name, or None."""
    query = "query($s:String){ Media(search:$s, type:ANIME, sort:SEARCH_MATCH){ title{ romaji native } siteUrl } }"
    try:
        async with net.session().post(ANILIST_URL, json={"query": query, "variables": {"s": title}},
                                      timeout=aiohttp.ClientTimeout(total=8)) as resp:
            media = ((await resp.json()).get("data") or {}).get("Media")
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return None
    if not media:
        return None
    t = media["title"]
    return " / ".join(x for x in (t.get("native"), t.get("romaji")) if x), media.get("siteUrl")


@bot.command(name="애니", aliases=["애니메이션"])
async def anime_command(ctx, action: str = "", *, rest: str = ""):
    """!애니 리스트 [작품] · !애니 추천 <작품>"""
    off = game_off_message("anime", ctx.guild)
    if off:
        await ctx.reply(off, mention_author=False)
        return
    rest = rest.strip()
    if action in ("리스트", "목록", "list"):
        embed = anime_list_embed(ctx.guild.id if ctx.guild else None, rest or None)
        if embed is None:
            await ctx.reply(f"`{rest[:50]}` 작품이 목록에 없습니다. `!애니 리스트`로 전체를 보거나 `!애니 추천 {rest[:50]}`으로 추천해 주세요.",
                            mention_author=False)
            return
        await ctx.reply(embed=embed, mention_author=False)
        return
    if action in ("추천", "recommend"):
        if not rest:
            await ctx.reply("`!애니 추천 <작품 이름>`처럼 써 주세요. 예: `!애니 추천 귀멸의 칼날`", mention_author=False)
            return
        if len(rest) > 100:
            await ctx.reply("작품 이름은 100자까지 쓸 수 있습니다.", mention_author=False)
            return
        if any(nara.normalize(w["title"]) == nara.normalize(rest) for w in quiz.anime_works()):
            await ctx.reply(f"**{rest}**{nara.josa(rest, '은', '는')} 이미 애니캐 맞히기에 있습니다. `!애니 리스트`로 확인해 보세요.", mention_author=False)
            return
        left = math.ceil(ANIME_SUGGEST_COOLDOWN - (time.time() - _anime_suggest_last.get(ctx.author.id, 0)))
        if left > 0:
            await ctx.reply(f"{left}초 후에 다시 추천해 주세요.", mention_author=False)
            return
        found = await anilist_find(rest)
        detail = f"AniList: {found[0]}\n{found[1]}" if found else "AniList에서 찾지 못했습니다 (한글 제목은 대개 검색되지 않습니다)."
        delivered = await deliver_to_admins(ctx.author, f"[애니 추천] {rest}", f"애니캐 맞히기에 넣어 주세요: {rest}\n{detail}",
                                            describe_place(ctx.guild, ctx.channel), [])
        if not delivered:
            await ctx.reply("전달하지 못했습니다. 잠시 후 다시 시도해 주세요.", mention_author=False)
            return
        if len(_anime_suggest_last) >= 500:
            _anime_suggest_last.clear()
        _anime_suggest_last[ctx.author.id] = time.time()
        found_text = f" (AniList: {found[0]})" if found else ""
        await ctx.reply(f"📮 **{rest}**{found_text} 추천을 개발자에게 전달했습니다. 고마워요!", mention_author=False)
        return
    await ctx.reply("`!애니 리스트` 작품 목록 · `!애니 리스트 <작품>` 캐릭터 목록 · `!애니 추천 <작품>` 작품 추천",
                    mention_author=False)


# ---------------------------------------------------------------- 노래 맞히기 (the parts only songs need)

DEEZER_API = "https://api.deezer.com"
SONG_FILE = "노래.mp3"
SONG_PREVIEW_MAX = 4 * 1024 * 1024
SONG_CUSTOM_MAX = 300  # songs one server can add
SONG_SEARCH_JUNK = re.compile(r"karaoke|instrumental|inst\.|cover|piano|8-bit|lullaby|music box|orgel|tribute|"
                              r"originally performed|가라오케|노래방|カラオケ", re.I)


def song_custom(guild_id):
    """[(id, names)] of the songs this server added, for matching answers."""
    if not guild_id:
        return []
    with connect_db() as conn:
        return [(r["id"], json.loads(r["names"])) for r in
                conn.execute("SELECT id, names FROM song_custom WHERE guild_id = ?", (guild_id,))]


def song_info(guild_id, sid):
    """{title, artist, names, cover} of a built-in or server-added song, or None."""
    if sid in quiz.songs():
        return quiz.songs()[sid]
    with connect_db() as conn:
        row = conn.execute("SELECT * FROM song_custom WHERE guild_id = ? AND id = ?", (guild_id, sid)).fetchone()
    return {"title": row["title"], "artist": row["artist"], "names": json.loads(row["names"]), "cover": row["cover"]} if row else None


def song_pool(guild_id, genre):
    """Song ids for a quiz: one genre, the server's added songs ("custom"), or (None) everything."""
    custom = [sid for sid, _ in song_custom(guild_id)]
    if genre == "custom":
        return custom
    built = [sid for sid, s in quiz.songs().items() if genre is None or s["genre"] == genre]
    return built + (custom if genre is None else [])


def song_pick(guild_id, genre, recent=()):
    pool = song_pool(guild_id, genre)
    if not pool:
        return None
    return random.choice([s for s in pool if s not in recent] or pool)


def song_hint_word(info):
    """The title to give the initials of: a Korean name when there is one."""
    korean = [n for n in info["names"] if re.search(r"[가-힣]", n)]
    return korean[0] if korean else info["title"]


ITUNES_API = "https://itunes.apple.com"
DEEZER_RETRY_SECONDS = 3600  # after Deezer fails (e.g. a server in Korea, where it is not available), Apple only for a while
SONG_READY_MAX = 8
_deezer_down_until = [0.0]
_song_ready = {}  # song id -> task giving {"clip": {seconds: mp3}, "answer": mp3 or None}: questions prepared ahead


async def _json_get(url, params=None, timeout=6):
    try:
        async with net.session().get(url, params=params, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            if resp.status != 200:
                return None
            return await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return None


async def itunes_search(term, limit=10):
    """Apple's search (no key needed); the Korean store first, which knows Korean titles, then the US one."""
    for country in ("KR", "US"):
        data = await _json_get(f"{ITUNES_API}/search", {"term": term, "entity": "song", "limit": limit, "country": country})
        if data and data.get("results"):
            return data["results"]
    return []


def song_apple_id(sid):
    """Apple's id for a song found earlier by name, so the search isn't repeated."""
    with connect_db() as conn:
        row = conn.execute("SELECT apple FROM song_apple WHERE id = ?", (sid,)).fetchone()
    return row["apple"] if row else None


async def song_preview_url(guild_id, sid):
    """The current 30-second official preview address: Deezer's (signed, so asked for each time) or, where Deezer
    can't be reached (it is not available in Korea) or has none, Apple's (iTunes)."""
    info = song_info(guild_id, sid) or {}
    if sid.startswith("dz") and time.time() >= _deezer_down_until[0]:
        data = await _json_get(f"{DEEZER_API}/track/{sid[2:]}", timeout=4)
        if data and data.get("preview"):
            return data["preview"]
        if data is None or (data.get("error") or {}).get("code") not in (None, 800):  # 800: no such track
            _deezer_down_until[0] = time.time() + DEEZER_RETRY_SECONDS
    apple = sid[2:] if sid.startswith("it") else info.get("itunes") or song_apple_id(sid)
    if apple:
        data = await _json_get(f"{ITUNES_API}/lookup", {"id": apple, "country": "KR"}) or {}
        for r in data.get("results") or []:
            if r.get("previewUrl"):
                return r["previewUrl"]
    if info:  # no Apple id yet: look the song up by name once, and remember what was found
        for r in await itunes_search(f"{info['artist']} {quiz.song_names(info['title'])[0]}", 5):
            if r.get("previewUrl") and nara.normalize(info["artist"])[:4] in nara.normalize(r.get("artistName", "")):
                with connect_db() as conn:
                    conn.execute("INSERT OR REPLACE INTO song_apple(id, apple) VALUES (?, ?)", (sid, str(r["trackId"])))
                return r["previewUrl"]
    return None


async def song_preview(guild_id, sid):
    """(bytes, extension) of the song's preview, or None."""
    result = await _song_preview(guild_id, sid)
    mark_health("song", result is not None,
                "Deezer·Apple 미리듣기를 받지 못함" + (" (Deezer 1시간 건너뜀 중)" if time.time() < _deezer_down_until[0] else ""))
    return result


async def _song_preview(guild_id, sid):
    url = await song_preview_url(guild_id, sid)
    if not url:
        return None
    try:
        async with net.session().get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200 or (resp.content_length or 0) > SONG_PREVIEW_MAX:
                return None
            data = await resp.read()
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return None
    if not data or len(data) > SONG_PREVIEW_MAX:
        return None
    return data, ("m4a" if (".m4a" in url or ".aac" in url) else "mp3")


async def song_encode(data, ext, clips=(), whole=False):
    """One ffmpeg run: {seconds: mp3 of a random part that long, faded in and out} for each length in clips, and
    under "answer" the whole preview as mp3 when whole. Missing ones are left out."""
    ffmpeg = storage.ffmpeg_path()
    if not ffmpeg:
        return {"answer": data} if (whole and ext == "mp3") else {}
    folder = tempfile.mkdtemp(prefix="maco-song-")
    try:
        source = os.path.join(folder, f"in.{ext}")
        with open(source, "wb") as f:
            f.write(data)
        outputs, args = {}, []
        for seconds in clips:
            fade = min(0.4, seconds / 5)
            start = random.uniform(1.5, max(1.5, 28 - seconds))
            out = os.path.join(folder, f"clip{seconds}.mp3")
            outputs[seconds] = out
            args += ["-ss", f"{start:.2f}", "-t", str(seconds),
                     "-af", f"afade=t=in:d={fade / 2:.2f},afade=t=out:st={seconds - fade:.2f}:d={fade:.2f}",
                     "-vn", "-c:a", "libmp3lame", "-b:a", "128k", out]
        if whole and ext != "mp3":
            outputs["answer"] = os.path.join(folder, "answer.mp3")
            args += ["-vn", "-c:a", "libmp3lame", "-b:a", "128k", outputs["answer"]]
        result = {"answer": data} if (whole and ext == "mp3") else {}
        if args:
            code, _ = await storage._run_capture(ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", source,
                                                 *args, timeout=30)
            for key, path in outputs.items():
                if code == 0 and os.path.exists(path):
                    with open(path, "rb") as f:
                        result[key] = f.read()
        return result
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def song_ready(guild_id, sid, seconds):
    """The task preparing a question's clip and its answer (started at once, shared by both). The next question's
    is started while the current one is being played, so it is usually ready when needed."""
    task = _song_ready.get(sid)
    if task is not None and not (task.done() and (task.cancelled() or task.exception() or not task.result())):
        return task

    async def prepare():
        preview = await song_preview(guild_id, sid)
        return await song_encode(*preview, clips=(seconds,), whole=True) if preview else {}

    for old in [k for k, t in _song_ready.items() if t.done()][:max(0, len(_song_ready) - SONG_READY_MAX + 1)]:
        del _song_ready[old]
    task = _song_ready[sid] = asyncio.get_running_loop().create_task(prepare())
    return task


async def song_clip(guild_id, sid, seconds):
    """A few seconds of the song's official preview as 노래.mp3, or None. Nothing is kept on disk."""
    ready = await song_ready(guild_id, sid, seconds)
    clip = ready.get(seconds)
    if clip is None and ready:  # prepared for another length (the difficulty changed): cut again
        preview = await song_preview(guild_id, sid)
        clip = (await song_encode(*preview, clips=(seconds,))).get(seconds) if preview else None
    return discord.File(io.BytesIO(clip), SONG_FILE) if clip else None


async def song_answer_file(guild_id, sid):
    """The whole 30-second preview, named after the song, to play from the answer message (rather than a link:
    the services' pages don't open everywhere, e.g. Deezer in Korea)."""
    info = song_info(guild_id, sid)
    if info is None:  # a server's song deleted meanwhile
        _song_ready.pop(sid, None)
        return None
    task = _song_ready.get(sid)
    data = (await task).get("answer") if task else None
    if not data:
        preview = await song_preview(guild_id, sid)
        data = (await song_encode(*preview, whole=True)).get("answer") if preview else None
    if not data:
        return None
    _song_ready.pop(sid, None)  # done with this song
    name = re.sub(r'[\\/:*?"<>|]+', " ", f"{info['title']} - {info['artist']}").strip()[:80] or "노래"
    return discord.File(io.BytesIO(data), f"{name}.mp3")


def song_prefetch(game):
    """Choose the next question's song now and start preparing it (saved as the game's upcoming song)."""
    if game["number"] >= game["total"]:
        return
    recent = json.loads(game["recent"])
    upcoming = song_pick(game["guild_id"], game["genre"], recent)
    if not upcoming or upcoming == game["answer"]:
        return
    with connect_db() as conn:
        conn.execute("UPDATE song_games SET upcoming = ? WHERE channel_id = ?", (upcoming, game["channel_id"]))
    song_ready(game["guild_id"], upcoming, quiz.SONG_SECONDS[game["level"]])


async def song_search(text, limit=5):
    """[{id, title, artist, cover, source}] for !노래추가 from Apple (iTunes, which also knows Korean titles) and
    Deezer, without karaoke, covers and the like and only songs with a preview; None if both searches failed."""
    found, seen, answered = [], set(), False

    def keep(sid, title, artist, cover, source):
        key = (nara.normalize(title), nara.normalize(artist))
        if SONG_SEARCH_JUNK.search(f"{title} {artist}") or key in seen:
            return
        seen.add(key)
        found.append({"id": sid, "title": title, "artist": artist, "cover": cover, "source": source})

    rows = await itunes_search(text, 25)
    answered = answered or rows is not None
    for r in rows:
        if r.get("previewUrl"):
            keep(f"it{r['trackId']}", r.get("trackName") or "", r.get("artistName") or "", r.get("artworkUrl100") or "", "Apple")
    data = await _json_get(f"{DEEZER_API}/search", {"q": text, "limit": 25})
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        answered = True
        for r in data["data"]:
            if r.get("preview"):
                keep(f"dz{r['id']}", r.get("title") or "", (r.get("artist") or {}).get("name") or "",
                     (r.get("album") or {}).get("cover_medium") or "", "Deezer")
    return found[:limit] if (found or answered) else None


class SongAddSelect(discord.ui.Select):
    """The search results of !노래추가; only the person who searched can pick."""

    def __init__(self, user_id, results, aliases):
        options = [discord.SelectOption(label=r["title"][:100], description=f"{r['artist']} · {r['source']}"[:100],
                                        value=str(i)) for i, r in enumerate(results)]
        super().__init__(placeholder="추가할 곡을 고르세요", options=options)
        self.user_id, self.results, self.aliases = user_id, results, aliases

    async def callback(self, interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("검색한 사람만 고를 수 있습니다.", ephemeral=True)
            return
        song = self.results[int(self.values[0])]
        message = song_add(interaction.guild.id, song, self.aliases, interaction.user.id)
        await interaction.response.edit_message(content=message, view=None)


def song_add(guild_id, song, aliases, user_id):
    """Add one search result to the server's songs; the message to show."""
    same = lambda s: (nara.normalize(quiz.song_names(s["title"])[0]), nara.normalize(s["artist"]))
    if song["id"] in quiz.songs() or any(same(s) == same(song) for s in quiz.songs().values()):
        return f"🎵 이미 기본 목록에 있는 곡입니다: **{song['title']}** - {song['artist']}"
    with connect_db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM song_custom WHERE guild_id = ?", (guild_id,)).fetchone()[0]
        if count >= SONG_CUSTOM_MAX:
            return f"이 서버는 곡을 {SONG_CUSTOM_MAX}개까지 추가할 수 있습니다. `!노래삭제`로 정리해 주세요."
        conn.execute(
            "INSERT OR REPLACE INTO song_custom(guild_id, id, title, artist, names, cover, added_by, added_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (guild_id, song["id"], song["title"], song["artist"],
             json.dumps(quiz.song_names(song["title"], *aliases), ensure_ascii=False), song["cover"], user_id, time.time()),
        )
    names = quiz.song_names(song["title"], *aliases)
    return (f"✅ 이 서버의 노래 맞히기에 추가했습니다: **{song['title']}** - {song['artist']}\n"
            f"-# 정답으로 받는 이름: {', '.join(names)}")


@bot.command(name="노래추가")
@commands.guild_only()
async def song_add_command(ctx, *, text: str = ""):
    """!노래추가 <검색어> [| 다른 이름, 다른 이름]"""
    off = game_off_message("song", ctx.guild)
    if off:
        await ctx.reply(off, mention_author=False)
        return
    query, _, extra = text.partition("|")
    query = query.strip()
    aliases = [a.strip() for a in extra.split(",") if a.strip()][:5]
    if not query:
        await ctx.reply("`!노래추가 <가수와 제목>`처럼 써 주세요. 예: `!노래추가 아이유 밤편지`\n"
                        "-# 다른 이름도 정답으로 받으려면 `!노래추가 아이유 밤편지 | Through the Night`처럼 `|` 뒤에 적어 주세요.",
                        mention_author=False)
        return
    results = await song_search(query[:100])
    if results is None:
        await ctx.reply("곡을 검색하지 못했습니다. 잠시 후 다시 시도해 주세요.", mention_author=False)
        return
    if not results:
        await ctx.reply(f"`{query[:50]}`로 찾은 곡이 없습니다. 가수 이름과 제목을 같이 써 보세요.", mention_author=False)
        return
    view = discord.ui.View(timeout=180).add_item(SongAddSelect(ctx.author.id, results, aliases))
    await ctx.reply("🔎 추가할 곡을 골라 주세요 (공식 30초 미리듣기가 있는 곡만, Apple Music·Deezer에서 찾았습니다)",
                    view=view, mention_author=False)


@bot.command(name="노래목록", aliases=["노래리스트"])
@commands.guild_only()
async def song_list_command(ctx):
    with connect_db() as conn:
        rows = conn.execute("SELECT title, artist FROM song_custom WHERE guild_id = ? ORDER BY added_at",
                            (ctx.guild.id,)).fetchall()
    counts = {g: sum(1 for s in quiz.songs().values() if s["genre"] == g) for g in ("kpop", "jpop", "pop")}
    base = " · ".join(f"{quiz.SONG_GENRES[g]} {n}곡" for g, n in counts.items())
    lines = [f"`{i}.` {r['title']} - {r['artist']}" for i, r in enumerate(rows, 1)]
    embed = discord.Embed(title=f"🎵 노래 맞히기 · 이 서버 추가곡 {len(rows)}개",
                          description="\n".join(lines)[:4000] or "아직 추가한 곡이 없습니다. `!노래추가 <가수와 제목>`으로 추가해 보세요.",
                          color=discord.Color.green())
    embed.set_footer(text=f"기본 목록: {base} · 지우기: !노래삭제 <번호>")
    await ctx.reply(embed=embed, mention_author=False)


@bot.command(name="노래삭제")
@commands.guild_only()
async def song_delete_command(ctx, number: str = ""):
    with connect_db() as conn:
        rows = conn.execute("SELECT id, title, artist, added_by FROM song_custom WHERE guild_id = ? ORDER BY added_at",
                            (ctx.guild.id,)).fetchall()
    if not number.isdigit() or not 1 <= int(number) <= len(rows):
        await ctx.reply("`!노래삭제 <번호>`처럼 써 주세요. 번호는 `!노래목록`에서 볼 수 있습니다.", mention_author=False)
        return
    row = rows[int(number) - 1]
    if row["added_by"] != ctx.author.id and not ctx.author.guild_permissions.manage_messages:
        await ctx.reply("추가한 사람이나 메시지 관리 권한이 있는 사람만 지울 수 있습니다.", mention_author=False)
        return
    with connect_db() as conn:
        conn.execute("DELETE FROM song_custom WHERE guild_id = ? AND id = ?", (ctx.guild.id, row["id"]))
    await ctx.reply(f"🗑️ 지웠습니다: **{row['title']}** - {row['artist']}", mention_author=False)


BET_MODE_TEXT = ("🎲 **배팅 판** · 아래 버튼으로 시작하면 처음 30초 동안(첫 정답 전까지) `!배팅 <XP>`로 1~50 XP를 걸 수 있어요. "
                 "배팅한 사람 중 가장 많이 맞힌 사람이 판돈을 모두 가져가고, 배팅하지 않은 사람의 XP는 그대로예요.")


def bet_toggle(key, bet, guild_id):
    """Row-3 button that flips a game page between normal and 배팅 starts (so the start buttons don't double)."""
    if bet:
        return [HelpButton(f"{key}_plain", "일반 판으로", "◀", discord.ButtonStyle.secondary, row=3)]
    if rank_setting("bet", guild_id):
        return [HelpButton(f"{key}_betmode", "배팅 판", "🎲", discord.ButtonStyle.secondary, row=3)]
    return []


def quiz_buttons(q, bet=False, guild_id=None):
    start = f"{q.key}_bet_" if bet else f"{q.key}_"
    word = "배팅" if bet else "시작"
    buttons = [HelpButton(f"{start}easy", f"쉬움 {word}", "🟢", discord.ButtonStyle.success, row=2),
               HelpButton(f"{start}normal", f"보통 {word}", "🌍", discord.ButtonStyle.primary, row=2),
               HelpButton(f"{start}hard", f"어려움 {word}", "🔥", discord.ButtonStyle.danger, row=2)]
    # (each game's own 순위 button went: rankings now live under 🏆 순위 on the 게임 tab)
    if q is ANIME_QUIZ:
        buttons = [b for b in buttons if not b.action.endswith("_hard")]
        buttons.append(HelpButton("anime_settings", "작품 고르기", "⚙️", discord.ButtonStyle.secondary, row=2))
    return buttons + bet_toggle(q.key, bet, guild_id)


async def show_quiz_page(q, interaction, details=None, extra=(), bet=False):
    """/도움말 → 게임 → one quiz's page (bet: its 배팅 version)."""
    off = game_off_message(q.key, interaction.guild)
    if off:  # a button from before the game was switched off
        await show_help(interaction, page="game", notice=off)
        return
    if q is ANIME_QUIZ and interaction.guild and details is None:
        works = anime_works_of(interaction.guild.id)
        details = f"-# 이 서버: 고른 작품 {len(works)}개" if works else "-# 이 서버: 모든 작품"
    if bet:
        details = BET_MODE_TEXT + (f"\n{details}" if details else "")
    await show_help(interaction, page="game", selected=q.key, details=details,
                    extra_items=[*quiz_buttons(q, bet, interaction.guild.id if interaction.guild else None), *extra])


def help_quiz_actions(q):
    """HELP_ACTIONS entries for one quiz's page and buttons."""
    def start(level, bet=False):
        async def handler(interaction):
            if interaction.guild is None or interaction.channel is None:
                await show_help(interaction, notice=SERVER_ONLY_MESSAGE)
                return
            await show_quiz_page(q, interaction, bet=bet)
            ctx = HelpContext(interaction, f"{q.title} · {quiz.LEVEL_NAMES[level]}" + (" · 배팅" if bet else ""))
            await run_from_help(ctx, quiz_start(q, interaction.channel, interaction.guild, interaction.user, level, bet=bet),
                                channel_only=True)
        return handler

    async def rank(interaction):
        if interaction.guild is None:
            await show_help(interaction, notice=SERVER_ONLY_MESSAGE)
            return
        ranking = quiz_ranking_text(q, interaction.guild.id)
        await show_quiz_page(q, interaction, details=f"**이 서버 순위**\n{ranking}" if ranking
                             else f"아직 이 서버에서 {q.title} 퀴즈를 맞힌 사람이 없습니다.")

    return {
        f"run_{q.key}": lambda interaction: show_quiz_page(q, interaction),
        **{f"{q.key}_{level}": start(level) for level in (quiz.EASY, quiz.NORMAL, quiz.HARD)},
        **{f"{q.key}_bet_{level}": start(level, bet=True) for level in (quiz.EASY, quiz.NORMAL, quiz.HARD)},
        f"{q.key}_betmode": lambda interaction: show_quiz_page(q, interaction, bet=True),
        f"{q.key}_plain": lambda interaction: show_quiz_page(q, interaction),
        f"{q.key}_rank": rank,
    }


HELP_ACTIONS = {
    "home": lambda interaction: show_help(interaction),
    "commands": lambda interaction: show_help(interaction, page="commands"),
    "page_uptime": lambda interaction: help_open_page(interaction, "uptime"),
    "page_stats": lambda interaction: help_open_page(interaction, "stats"),
    "page_etc": lambda interaction: help_open_page(interaction, "etc"),
    "page_files": lambda interaction: help_open_page(interaction, "files"),
    "run_upload": help_run_upload,
    "attach": help_open_form(UploadModal),
    "web": help_run_upload,
    "run_files": help_run_files,
    "run_filedelete": help_run_filedelete,
    "run_fortune": help_run_random_line("fortune"),
    "run_ilchim": help_run_random_line("ilchim"),
    "run_dday": lambda interaction: show_dday_page(interaction),
    "dday_new": lambda interaction: dday_open_form(interaction),
    "dday_edit": lambda interaction: dday_choose(interaction, "edit"),
    "dday_del": lambda interaction: dday_choose(interaction, "del"),
    "page_game": lambda interaction: help_open_page(interaction, "game"),
    "run_nara": lambda interaction: show_nara_page(interaction),
    "nara_easy": help_run_nara(nara.EASY),
    "nara_normal": help_run_nara(nara.NORMAL),
    "nara_hard": help_run_nara(nara.HARD),
    "nara_rank": help_run_nara_rank,
    "nara_bet_easy": help_run_nara(nara.EASY, bet=True),
    "nara_bet_normal": help_run_nara(nara.NORMAL, bet=True),
    "nara_bet_hard": help_run_nara(nara.HARD, bet=True),
    "nara_betmode": lambda interaction: show_nara_page(interaction, bet=True),
    "nara_plain": lambda interaction: show_nara_page(interaction),
    "run_ranking": help_run_ranking,
    "ranking_server": lambda interaction: help_run_ranking(interaction, shown=True),
    "ranking_all": lambda interaction: help_run_ranking(interaction, everywhere=True),
    "ranking_me": help_run_ranking_me,
    "nara_info": lambda interaction: interaction.response.send_modal(NaraInfoModal()),
    **help_quiz_actions(FLAG_QUIZ),
    **help_quiz_actions(CAPITAL_QUIZ),
    **help_quiz_actions(MAP_QUIZ),
    **help_quiz_actions(ANIME_QUIZ),
    **help_quiz_actions(SONG_QUIZ),
    "anime_settings": help_anime_settings,
    "dm": help_dm_to_dev,
    "run_add": help_open_form(AddServerModal),
    "run_uptime": help_run_uptime,
    "run_list": help_run_list,
    "run_delete": help_run_delete,
    "run_hypixel": help_open_form(HypixelModal),
    "run_valorant": help_open_form(ValorantModal),
}


TEXTCON_WEBHOOK_NAME = "마코 글자콘"
_textcon_webhooks = {}  # channel id -> the bot's webhook there (a few dozen at most; looked up once)


async def textcon_webhook(channel):
    """The bot's webhook in this channel (made the first time), or None if it can't have one
    (DM, no 웹훅 관리 permission, ...). Threads use their parent channel's webhook."""
    if not can_post_as_user(channel):
        return None
    parent = channel.parent if isinstance(channel, discord.Thread) else channel
    hook = _textcon_webhooks.get(parent.id)
    if hook is None:
        hooks = await parent.webhooks()
        hook = next((h for h in hooks if h.user and h.user.id == bot.user.id and h.name == TEXTCON_WEBHOOK_NAME), None)
        hook = hook or await parent.create_webhook(name=TEXTCON_WEBHOOK_NAME, reason="/글자콘")
        if len(_textcon_webhooks) >= 200:
            _textcon_webhooks.pop(next(iter(_textcon_webhooks)))
        _textcon_webhooks[parent.id] = hook
    return hook


def can_post_as_user(channel):
    parent = channel.parent if isinstance(channel, discord.Thread) else channel
    return (isinstance(parent, (discord.TextChannel, discord.VoiceChannel, discord.StageChannel, discord.ForumChannel))
            and parent.permissions_for(parent.guild.me).manage_webhooks)


async def send_as_user(interaction, content, png):
    """Post as the person (their server nickname and picture), like /unflip. Bots can't speak from someone
    else's account, so this goes through a webhook: Discord marks it "앱", and it can't be edited by them.
    Returns False when that isn't possible here, so the caller posts as the bot instead."""
    channel = interaction.channel
    if interaction.guild is None or channel is None:
        return False
    try:
        hook = await textcon_webhook(channel)
        if hook is None:
            return False
        user = interaction.user
        extra = {"thread": channel} if isinstance(channel, discord.Thread) else {}
        await hook.send(content=content, file=discord.File(io.BytesIO(png), "글자콘.png"),
                        username=user.display_name[:80], avatar_url=user.display_avatar.url,
                        allowed_mentions=discord.AllowedMentions.none(), **extra)
        return True
    except discord.NotFound:  # someone deleted the webhook: forget it and post as the bot this time
        _textcon_webhooks.pop(getattr(channel, "parent_id", None) or channel.id, None)
    except discord.HTTPException as exc:  # e.g. a nickname Discord refuses for webhooks
        print(f"글자콘 웹훅 전송 실패: {type(exc).__name__}: {exc}")
    return False


@bot.tree.command(name="글자콘", description="글자를 디시 글자콘처럼 그림으로 만들어 내 이름으로 올립니다")
@app_commands.rename(text="글자", message="메시지")
@app_commands.describe(text=f"그림으로 만들 글자 (최대 {textcon.MAX_CHARS}자)", message="글자콘과 같이 보낼 메시지 (선택)")
@app_commands.checks.cooldown(1, 3.0)
async def textcon_slash(interaction: discord.Interaction, text: app_commands.Range[str, 1, textcon.MAX_CHARS],
                        message: app_commands.Range[str, 1, 1800] | None = None):
    """Like /unflip: posted under the person's name with the optional message, in one message."""
    try:
        text = textcon.clean(text)
    except ValueError as exc:
        await interaction.response.send_message(str(exc), ephemeral=True)
        return
    file = lambda: discord.File(io.BytesIO(png), "글자콘.png")
    if not can_post_as_user(interaction.channel):
        # DM, or no 웹훅 관리 permission: post as the bot (Discord shows who used the command above it).
        await interaction.response.defer(thinking=True)
        png = await asyncio.to_thread(textcon.render, text)
        await interaction.followup.send(content=message, file=file(), allowed_mentions=discord.AllowedMentions.none())
        return
    # A private "thinking" answer, removed once the picture is posted under the person's name.
    await interaction.response.defer(ephemeral=True, thinking=True)
    png = await asyncio.to_thread(textcon.render, text)
    if not await send_as_user(interaction, message, png):
        # The webhook failed after all (deleted meanwhile, a nickname Discord refuses ...): post as the bot.
        header = f"-# {interaction.user.mention}님의 글자콘"
        try:
            await interaction.channel.send(content=f"{header}\n{message}" if message else header, file=file(),
                                           allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            await interaction.followup.send("글자콘을 올리지 못했습니다.", ephemeral=True)
            return
    try:
        await interaction.delete_original_response()
    except discord.HTTPException:
        pass


@bot.tree.command(name="도움말", description="버튼으로 명령어를 보고 바로 사용합니다")
async def help_slash(interaction: discord.Interaction):
    await interaction.response.send_message(ephemeral=True, **help_message(guild_id=interaction.guild_id))


# ---------------------------------------------------------------- 대시보드 (nnaco.kro.kr/admin, on NKOTE)
# The dashboard does what the £ commands do. The bot only polls NKOTE (see dashboard.py): no web server,
# no extra library, nothing kept between polls, so it costs next to no memory.

STARTED_AT = time.time()
DASHBOARD_IDLE_SECONDS = 20  # nobody has the dashboard open (the most the page waits for the bot to wake)
DASHBOARD_AWAKE_SECONDS = 3  # someone does
DASHBOARD_IDLE_SNAPSHOT_SECONDS = 30 * 60  # so the page opens with fairly recent numbers
DASHBOARD_MAX_FILES = 300


KST = timezone(timedelta(hours=9))


def plain_text(text):
    """Discord markdown → plain text for the web page (<t:...> times in Korean time)."""
    text = re.sub(r"^-# ", "", text, flags=re.M)
    text = re.sub(r"<t:(\d+)(?::\w)?>", lambda m: datetime.fromtimestamp(int(m[1]), KST).strftime("%Y-%m-%d %H:%M"), text)
    text = text.replace("**", "").replace("`", "")
    return re.sub(r"\\([*_`~|>\\])", r"\1", text)


def json_limit(value):
    """-1 for 한도 없음 (JSON has no infinity)."""
    return -1 if value == UNLIMITED else int(value)


def user_name(user_id):
    user = bot.get_user(user_id)
    return user.display_name if user else ""


async def dashboard_snapshot():
    """What the dashboard shows. Built on demand and dropped right after sending (a few KB to ~60KB)."""
    current, peak = memory_usage()
    retention_max = file_retention_max()
    snapshot = {
        "bot": {
            "name": str(bot.user) if bot.user else "마코", "startedAt": int(STARTED_AT), "guilds": len(bot.guilds),
            "latencyMs": round(bot.latency * 1000) if bot.latency == bot.latency else None,  # NaN before connecting
            "memory": {"current": current, "peak": peak, "limit": env_int("MEMORY_LIMIT_MB", 256, 1) * 1024 * 1024},
            "connection": connection_health(),
        },
        "retention": {"seconds": file_retention(), "text": retention_text(), "forever": is_forever(file_retention()),
                      "maxText": "삭제 안 함" if is_forever(retention_max) else human_duration(retention_max),
                      "maxSeconds": retention_max},
        "limits": {"quota": json_limit(default_limit("quota")), "max": json_limit(default_limit("max")),
                   "hardMax": FILE_MAX_HARD, "totalQuota": FILE_TOTAL_QUOTA},
        "storage": {"configured": storage.configured()},
        "guilds": [], "admins": [], "files": [], "filesTotal": 0,
        # 관리자 패널의 켜기/끄기와 차단
        "games": {game: {"title": title, "on": game_enabled(game)} for game, title in GAME_SWITCHES.items()},
        "uploadBlocked": "upload_block:all" in switch_keys(),  # i.e. the default is 꺼짐
        "bans": [{"id": str(b["user_id"]), "name": user_name(b["user_id"]), "until": int(b["until"]) if b["until"] else None,
                  "reason": b["reason"], "at": int(b["created_at"])} for b in all_upload_bans()],
        "virusCheck": bool(VIRUSTOTAL_KEY), "blockedFiles": [],
        "ranking": {name: rank_setting(name) for name in RANK_SETTINGS},
        "uptime": {"interval": uptime_interval(), "days": history_days(), "background": uptime_background_on(),
                   "running": monitor_servers.is_running(), "servers": uptime_server_list()},
    }
    with connect_db() as conn:
        usage = {r[0]: (r[1], r[2]) for r in conn.execute(
            "SELECT guild_id, COUNT(*), COALESCE(SUM(size), 0) FROM files WHERE status = 'ready' GROUP BY guild_id")}
        custom = {r["key"] for r in conn.execute("SELECT key FROM settings WHERE key LIKE 'file_limit:%'")}
        ready, pending = conn.execute(
            "SELECT COALESCE(SUM(status = 'ready'), 0), COALESCE(SUM(status = 'pending'), 0) FROM files").fetchone()
        next_expiry = conn.execute("SELECT MIN(expires_at) FROM files WHERE status = 'ready'").fetchone()[0]
        files = [dict(r) for r in conn.execute(
            "SELECT id, guild_id, name, key, url, size, uploaded_by, expires_at, status FROM files "
            "ORDER BY uploaded_at DESC LIMIT ?", (DASHBOARD_MAX_FILES,))]
    snapshot["filesTotal"] = ready + pending
    guild_ids = [g.id for g in sorted(bot.guilds, key=lambda g: g.member_count or 0, reverse=True)]
    guild_ids += [g for g in usage if g and g not in guild_ids]  # servers the bot left that still have files
    for guild_id in guild_ids:
        guild = bot.get_guild(guild_id)
        count, used = usage.get(guild_id, (0, 0))
        snapshot["guilds"].append({
            "id": str(guild_id), "name": guild.name if guild else f"(나간 서버) {guild_id}",
            "members": guild.member_count if guild else None, "used": used, "files": count,
            "quota": json_limit(guild_quota(guild_id)), "max": json_limit(guild_limit(guild_id, "max")),
            "customQuota": f"file_limit:quota:{guild_id}" in custom, "customMax": f"file_limit:max:{guild_id}" in custom,
            "gamesOff": [game for game in GAME_SWITCHES if f"game_off:{game}:{guild_id}" in switch_keys()],
            "uploadBlocked": not upload_enabled(guild_id),
            "upload": upload_server_setting(guild_id),  # True / False / None (follows the default)
            "uptimeOff": not uptime_enabled(guild_id),
            # 순위 보이기 / 전체 랭킹 for this server: True, False, or None (follows the default)
            "ranking": {name: rank_values().get(f"rank:{name}:{guild_id}") for name in RANK_SETTINGS},
        })
    with connect_db() as conn:
        blocked = [dict(r) for r in conn.execute("SELECT * FROM files WHERE status = 'blocked' ORDER BY uploaded_at DESC")]
    for row in blocked:
        guild = bot.get_guild(row["guild_id"]) if row["guild_id"] else None
        snapshot["blockedFiles"].append({
            "id": row["id"], "guild": guild.name if guild else str(row["guild_id"]), "guildId": str(row["guild_id"]),
            "name": row["name"], "size": row["size"], "uploader": str(row["uploaded_by"]),
            "uploaderName": user_name(row["uploaded_by"]), "uploadedAt": int(row["uploaded_at"]),
            "expiresAt": int(row["expires_at"]), "sha256": row["sha256"] or "", "scan": row["scan"] or "",
            # evidence download: a storage link made now (on a private bucket it lasts at most 7 days)
            "url": file_link(row) if storage.configured() else None,
        })
    for row in files:
        guild = bot.get_guild(row["guild_id"]) if row["guild_id"] else None
        snapshot["files"].append({
            "id": row["id"], "guildId": str(row["guild_id"]), "guild": guild.name if guild else str(row["guild_id"]),
            "name": row["name"] or "(이름 없음)", "size": row["size"], "uploader": str(row["uploaded_by"]),
            "uploaderName": user_name(row["uploaded_by"]), "expiresAt": int(row["expires_at"]), "status": row["status"],
            # where the file is stored: its short link, else a storage link made now (opens until it expires)
            "url": open_link(row) if row["status"] == "ready" and storage.configured() else None,
        })
    try:
        owners = await admin_user_ids()
    except discord.HTTPException:
        owners = set()
    owners.discard(None)
    snapshot["admins"] = [{"id": str(i), "name": user_name(i) or str(i), "owner": i in owners}
                          for i in sorted(owners) + sorted(file_admin_ids() - owners)]
    if storage.configured():
        host, bucket, mode = storage.describe()
        ping_ms, error = await storage_ping_cached()
        snapshot["storage"].update({
            "host": host, "bucket": bucket, "mode": mode, "ok": not error, "error": error,
            "pingMs": round(ping_ms) if ping_ms is not None else None, "used": file_usage(), "total": FILE_TOTAL_QUOTA,
            "ready": ready, "pending": pending, "nextExpiry": int(next_expiry) if next_expiry else None,
            "cleaner": clean_files.is_running(), "shortlinks": shortlinks.host() if shortlinks.configured() else None,
        })
    return snapshot


_ping_cache = {"at": None, "value": (None, None)}  # None: never checked (time.monotonic() can be under 60)


async def storage_ping_cached():
    """The storage check takes 0.3-1 s; a snapshot after each dashboard button reuses it for a minute."""
    if _ping_cache["at"] is None or time.monotonic() - _ping_cache["at"] > 60 or _ping_cache["value"][1]:
        _ping_cache["value"] = await storage.ping()
        _ping_cache["at"] = time.monotonic()
    return _ping_cache["value"]


_period_uptime = {"at": 0.0, "values": {}, "task": None}  # server id -> (samples, online) over the kept period


def count_period_uptime():
    """Whole-period samples and online count per server. Up to ~90,000 rows a server, which can take
    seconds on a slow disk, so it runs in a worker thread (its own SQLite connection) every 10 minutes."""
    with connect_db() as conn:
        return {r[0]: (r[1], r[2]) for r in conn.execute(
            "SELECT server_id, COUNT(*), COALESCE(SUM(online), 0) FROM samples GROUP BY server_id")}


def refresh_period_uptime():
    task = _period_uptime["task"]
    if time.time() - _period_uptime["at"] < 600 or (task and not task.done()):
        return

    async def run():
        try:
            _period_uptime["values"] = await asyncio.to_thread(count_period_uptime)
            _period_uptime["at"] = time.time()
        except sqlite3.Error as exc:
            print(f"업타임 기간 계산 오류: {type(exc).__name__}: {exc}")

    _period_uptime["task"] = asyncio.get_running_loop().create_task(run())


def uptime_server_list():
    """Every registered Minecraft server with its last result and uptime, for the admin panel. Each server is
    read through its (server_id, checked_at) index; the whole-period numbers come from the background count
    (shown as soon as it is done), so a snapshot never waits on the big table."""
    now = time.time()
    try:
        refresh_period_uptime()
    except RuntimeError:  # no running loop (tests)
        pass
    out = []
    with connect_db() as conn:
        servers = [dict(r) for r in conn.execute("SELECT id, guild_id, name, address FROM servers ORDER BY guild_id, id")]
        for row in servers:
            sid = row["id"]
            last = conn.execute("SELECT online, latency_ms, checked_at FROM samples WHERE server_id = ? "
                                "ORDER BY checked_at DESC LIMIT 1", (sid,)).fetchone()
            count, up = conn.execute("SELECT COUNT(*), COALESCE(SUM(online), 0) FROM samples "
                                     "WHERE server_id = ? AND checked_at >= ?", (sid, now - 86400)).fetchone()
            all_count, all_up = _period_uptime["values"].get(sid, (0, 0))
            guild = bot.get_guild(row["guild_id"])
            out.append({
                "id": sid, "guildId": str(row["guild_id"]),
                "guild": guild.name if guild else f"(나간 서버) {row['guild_id']}",
                "name": row["name"], "address": row["address"],
                "online": None if last is None else bool(last["online"]),
                "latencyMs": round(last["latency_ms"]) if last and last["latency_ms"] else None,
                "checkedAt": int(last["checked_at"]) if last else None,
                "day": round(up / count * 100, 2) if count else None,
                "all": round(all_up / all_count * 100, 2) if all_count else None, "samples": all_count,
            })
    return out


def dashboard_guild_id(value):
    text = str(value or "")
    return int(text) if text.isdigit() else None


async def dashboard_job(job):
    """Do one dashboard job; {"id", "ok", "message", "data"?} for NKOTE. The bot checks every value itself."""
    kind, args = job.get("type"), job.get("args") or {}
    ok, message, data = True, "", None
    needs_storage = kind in ("delete_files", "delete_guild_files", "cleanup", "test", "set_expiry")
    try:
        if needs_storage and not storage.configured():
            ok, message = False, FILE_NOT_CONFIGURED
        elif kind == "snapshot":
            pass
        elif kind == "set_anime_works":
            guild_id = dashboard_guild_id(args.get("guild_id"))
            known = {c["work"] for c in quiz.anime().values()}
            works = args.get("works")
            if not guild_id:
                ok, message = False, "서버를 알 수 없습니다."
            elif works is None:
                with connect_db() as conn:
                    conn.execute("DELETE FROM anime_works WHERE guild_id = ?", (guild_id,))
                message = "애니캐 맞히기: 모든 작품"
            else:
                chosen = sorted({w for w in works if isinstance(w, str)} & known)
                if not chosen:
                    ok, message = False, "알 수 있는 작품이 없습니다."
                else:
                    with connect_db() as conn:
                        conn.execute("INSERT OR REPLACE INTO anime_works(guild_id, works) VALUES (?, ?)",
                                     (guild_id, json.dumps(chosen, ensure_ascii=False)))
                    message = f"애니캐 맞히기: 작품 {len(chosen)}개"
        elif kind == "set_game":
            game, scope = args.get("game"), args.get("scope")
            guild_id = "all" if scope == "all" else dashboard_guild_id(scope)
            if game not in GAME_SWITCHES or not guild_id:
                ok, message = False, "게임이나 서버를 알 수 없습니다."
            else:
                on = bool(args.get("on"))
                set_switch(f"game_off:{game}:{guild_id}", not on)
                where = "전체" if guild_id == "all" else guild_label(guild_id)
                message = f"{GAME_SWITCHES[game]} · {where}: {'켜짐' if on else '꺼짐'}"
        elif kind == "set_uptime":
            changes = []
            if "interval" in args:
                seconds = args.get("interval")
                if not isinstance(seconds, int) or not UPTIME_INTERVAL_MIN <= seconds <= UPTIME_INTERVAL_MAX:
                    raise ValueError(f"확인 간격은 {UPTIME_INTERVAL_MIN}초 ~ 1시간 사이로 정해 주세요.")
                set_setting("uptime_interval", seconds)
                monitor_servers.change_interval(seconds=seconds)
                changes.append(f"확인 간격 {human_duration(seconds) if seconds >= 60 else f'{seconds}초'}")
            if "days" in args:
                days = args.get("days")
                if days not in UPTIME_DAYS_CHOICES:
                    raise ValueError("보관 기간은 7일, 14일, 30일 중에서 골라 주세요.")
                set_setting("uptime_days", days)
                with connect_db() as conn:  # a shorter period frees the space right away
                    conn.execute("DELETE FROM samples WHERE checked_at < ?", (time.time() - days * 86400,))
                    conn.commit()
                changes.append(f"기록 보관 {days}일")
            if "background" in args:
                set_switch("uptime_off:all", not args.get("background"))
                changes.append(f"자동 확인 {'켜짐' if args.get('background') else '꺼짐'}")
            if not changes:
                ok, message = False, "바꿀 것이 없습니다."
            else:
                message = "업타임: " + " · ".join(changes)
        elif kind == "set_ranking":
            name, scope, value = args.get("name"), args.get("scope"), args.get("value")
            guild_id = "default" if scope == "default" else dashboard_guild_id(scope)
            if name not in RANK_SETTINGS or not guild_id or (value is None and guild_id == "default") \
                    or value not in (True, False, None):
                ok, message = False, "설정을 알 수 없습니다."
            else:
                key = f"rank:{name}:{guild_id}"
                if value is None:
                    delete_setting(key)
                else:
                    set_setting(key, "1" if value else "0")
                where = "기본값" if guild_id == "default" else guild_label(guild_id)
                message = f"{RANK_SETTINGS[name]} · {where}: " + ("기본값 따름" if value is None else "켜짐" if value else "꺼짐")
        elif kind == "set_uptime_guild":
            guild_id = dashboard_guild_id(args.get("scope"))
            if not guild_id:
                ok, message = False, "서버를 알 수 없습니다."
            else:
                set_switch(f"uptime_off:{guild_id}", not args.get("on"))
                message = f"서버 업타임 · {guild_label(guild_id)}: {'켜짐' if args.get('on') else '꺼짐'}"
        elif kind == "delete_uptime_server":
            server_id = args.get("id")
            with connect_db() as conn:
                row = conn.execute("SELECT name, address FROM servers WHERE id = ?", (server_id,)).fetchone() \
                    if isinstance(server_id, int) else None
            if not row:
                ok, message = False, "등록된 마크 서버를 찾지 못했습니다."
            else:
                remove_server(server_id)
                message = f"{row['name']} ({row['address']})와 업타임 기록을 삭제했습니다."
        elif kind == "set_upload":
            scope, value = args.get("scope"), args.get("value")
            guild_id = "all" if scope == "all" else dashboard_guild_id(scope)
            if not guild_id or value not in (True, False, None) or (guild_id == "all" and value is None):
                ok, message = False, "설정을 알 수 없습니다."
            elif guild_id == "all":
                set_switch("upload_block:all", not value)
                message = f"파일 업로드 · 기본값: {'켜짐' if value else '꺼짐'}"
            else:
                set_switch(f"upload_block:{guild_id}", value is False)
                set_switch(f"upload_allow:{guild_id}", value is True)
                message = (f"파일 업로드 · {guild_label(guild_id)}: "
                           + ("기본값 따름" if value is None else "켜짐" if value else "꺼짐"))
        elif kind == "set_upload_block":
            scope = args.get("scope")
            guild_id = "all" if scope == "all" else dashboard_guild_id(scope)
            if not guild_id:
                ok, message = False, "서버를 알 수 없습니다."
            else:
                blocked_now = bool(args.get("blocked"))
                set_switch(f"upload_block:{guild_id}", blocked_now)
                where = "전체" if guild_id == "all" else guild_label(guild_id)
                message = f"파일 업로드 · {where}: {'차단' if blocked_now else '허용'}"
        elif kind == "ban_user":
            user_id = dashboard_guild_id(args.get("userId"))  # same rule: digits only
            hours = args.get("hours")
            if not user_id or len(str(user_id)) < 15:
                ok, message = False, "디스코드 ID(숫자 17~20자리)를 확인해 주세요."
            elif hours is not None and (not isinstance(hours, (int, float)) or not 0 < hours <= 24 * 365):
                ok, message = False, "차단 시간은 1시간 ~ 1년 사이로 정해 주세요."
            else:
                remove_upload_ban(user_id)  # the admin's choice replaces the current ban, shorter or not
                set_upload_ban(user_id, None if hours is None else hours * 3600, str(args.get("reason") or "")[:200])
                who = user_name(user_id) or user_id
                message = f"{who}: 업로드 {'영구 차단' if hours is None else human_duration(hours * 3600) + ' 차단'}"
        elif kind == "unban_user":
            user_id = dashboard_guild_id(args.get("userId"))
            if not user_id:
                ok, message = False, "디스코드 ID를 확인해 주세요."
            else:
                remove_upload_ban(user_id)
                message = f"{user_name(user_id) or user_id}: 업로드 차단을 풀었습니다."
        elif kind == "set_limits":
            scope = "default" if args.get("scope") == "default" else dashboard_guild_id(args.get("scope"))
            changes = limit_changes(args.get("quota"), args.get("max")) if scope else "서버를 알 수 없습니다."
            ok, message = (False, changes) if isinstance(changes, str) else apply_limits(scope, changes)
        elif kind == "set_retention":
            ok, message = apply_retention(str(args.get("value", "")))
        elif kind == "set_admins":
            ids = lambda key: {int(i) for i in args.get(key) or [] if re.fullmatch(r"\d{15,20}", str(i))}
            add, remove = ids("add"), ids("remove")
            set_setting("file_admins", ",".join(str(i) for i in sorted((file_admin_ids() | add) - remove)))
            message = "파일 관리자를 바꿨습니다."
        elif kind == "delete_files":
            wanted = [int(i) for i in args.get("ids") or [] if str(i).isdigit()][:100]
            with connect_db() as conn:
                rows = [dict(r) for r in conn.execute(
                    f"SELECT * FROM files WHERE id IN ({','.join('?' * len(wanted))})", wanted)] if wanted else []
            removed = [row for row in rows if await remove_file(row)]
            ok = len(removed) == len(rows)
            message = f"파일 {len(removed)}개를 삭제했습니다." + ("" if ok else f" {len(rows) - len(removed)}개는 저장소 오류로 지우지 못했습니다.")
        elif kind == "set_expiry":
            row_id, when = args.get("id"), args.get("expiresAt")
            if not str(row_id).isdigit() or not isinstance(when, (int, float)):
                ok, message = False, "파일이나 날짜를 알 수 없습니다."
            else:
                ok, message = await set_file_expiry(int(row_id), float(when))
        elif kind == "delete_guild_files":
            guild_id = dashboard_guild_id(args.get("guildId"))
            ok, message = await delete_guild_files(guild_id) if guild_id else (False, "서버를 알 수 없습니다.")
        elif kind == "cleanup":
            ok, message = await run_cleanup()
        elif kind == "test":
            results = await storage.self_test() + await shortlinks.self_test()
            data = [[label, bool(good), str(detail)[:300]] for label, good, detail in results]
            ok = all(good for _, good, _ in results)
            message = "저장소 테스트를 마쳤습니다." + ("" if ok else " 🔴 항목을 확인해 주세요.")
        else:
            ok, message = False, "알 수 없는 작업입니다."
    except ValueError as exc:  # a value the admin typed that the bot refuses, with the reason
        ok, message = False, str(exc)
    except Exception as exc:
        trace = traceback.format_exc()
        print(f"대시보드 작업 오류 ({kind}):\n{trace}")
        notify_admin("대시보드 작업 오류", f"{type(exc).__name__}: {exc}", f"작업: {kind}", trace)
        ok, message = False, f"오류가 발생했습니다: {type(exc).__name__}"
    return {"id": job.get("id"), "ok": ok, "message": plain_text(message), "data": data}


async def dashboard_loop():
    await bot.wait_until_ready()
    last_snapshot = 0
    last_problem = None
    while not bot.is_closed():
        delay = DASHBOARD_IDLE_SECONDS
        problem = None
        try:
            data = await dashboard.sync()
            if data.get("missing"):
                delay = 600
            elif data.get("wake"):
                delay = DASHBOARD_AWAKE_SECONDS
                results = [await dashboard_job(job) for job in data.get("jobs") or []]
                if results or time.time() - last_snapshot > 60:
                    snapshot = await dashboard_snapshot()
                    last_snapshot = time.time()
                    await dashboard.report(snapshot, results)
            elif time.time() - last_snapshot > DASHBOARD_IDLE_SNAPSHOT_SECONDS:
                snapshot = await dashboard_snapshot()
                last_snapshot = time.time()
                await dashboard.report(snapshot)
        except dashboard.NotBound:
            problem, delay = "다른 키가 대시보드에 연결돼 있습니다. 대시보드 → 설정 → 봇 연결에서 해제해 주세요.", 300
        except (asyncio.TimeoutError, aiohttp.ClientConnectionError) as exc:
            # TimeoutError carries no message of its own; say where and what to check.
            reason = "10초 안에 응답하지 않았습니다" if isinstance(exc, asyncio.TimeoutError) else f"연결 실패 ({exc})"
            problem, delay = (f"NKOTE({shortlinks.BASE_URL})가 {reason}. 브라우저에서 이 주소가 열리는지, "
                              "`£DB 테스트`의 단축 링크 항목이 되는지 확인해 주세요. 1분 뒤 다시 시도합니다."), 60
        except Exception as exc:
            problem, delay = f"{type(exc).__name__}: {exc}", 60
        if problem and problem != last_problem:
            print(f"대시보드 연결 문제: {problem}")
        last_problem = problem
        await asyncio.sleep(delay)


async def setup_hook():
    if dashboard.configured():
        heartbeat = asyncio.get_running_loop().create_task(status_heartbeat())
        _background_tasks.add(heartbeat)
        heartbeat.add_done_callback(_background_tasks.discard)
    task = asyncio.get_running_loop().create_task(watch_loop_lag())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    # Card drawing runs in threads; two are enough and keep memory down (the default can start a dozen).
    asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=2, thread_name_prefix="maco"))
    bot.add_dynamic_items(AdminReplyButton, UserAnswerButton, HelpButton, UploadDoneButton, CopyLinkButton,
                          DdayPick, DdayAfter, DdayCelebrateButton, SongDisplayButton, NaraSetButton, RankPageButton,
                          RankShareButton)
    await load_help_emojis()
    if dashboard.configured():
        task = asyncio.get_running_loop().create_task(dashboard_loop())
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
    if storage.configured():
        problems = await storage.setup_bucket()
        if problems:
            notify_admin("파일 저장소 설정 문제", "\n".join(problems), "봇 시작")
    try:
        synced = await bot.tree.sync()
        print(f"슬래시 명령어 {len(synced)}개를 등록했습니다.")
    except discord.HTTPException as exc:
        print(f"슬래시 명령어 등록 실패: {type(exc).__name__}: {exc}")

bot.setup_hook = setup_hook


MONITOR_CONCURRENCY = 5  # servers checked at once: one slow server no longer delays all the others
_monitor_state = {}  # server id -> online at the last background check (to log only changes)


@tasks.loop(seconds=UPTIME_INTERVAL_DEFAULT)
async def monitor_servers():
    # An exception escaping this loop stops background checks for good, so contain every error.
    if not uptime_background_on():
        return
    try:
        rows = [r for r in all_servers() if uptime_enabled(r["guild_id"])]
    except sqlite3.Error as exc:
        print(f"백그라운드 확인 오류: {type(exc).__name__}: {exc}")
        notify_admin("백그라운드 서버 확인 오류", f"{type(exc).__name__}: {exc}", "서버 목록 읽기", traceback.format_exc())
        return
    limit = asyncio.Semaphore(MONITOR_CONCURRENCY)

    async def check(row):
        async with limit:
            try:
                online, latency, _, _, _ = await query_server(row["address"], quiet=True)
            except Exception as exc:
                print(f"백그라운드 확인 오류 ({row['address']}): {type(exc).__name__}: {exc}")
                notify_admin("백그라운드 서버 확인 오류", f"{type(exc).__name__}: {exc}", f"주소: {row['address']}",
                             traceback.format_exc())
                return None
            if _monitor_state.get(row["id"], True) != online:
                print(f"서버 {'다시 켜짐' if online else '응답 없음'}: {row['address']}")
            _monitor_state[row["id"]] = online
            return row["id"], row["guild_id"], online, latency

    results = [r for r in await asyncio.gather(*(check(row) for row in rows)) if r]
    known = {row["id"] for row in rows}
    for server_id in [i for i in _monitor_state if i not in known]:  # deleted servers
        del _monitor_state[server_id]
    try:
        if results:
            save_samples(results)
    except sqlite3.Error as exc:
        print(f"업타임 기록 저장 오류: {type(exc).__name__}: {exc}")
        notify_admin("업타임 기록 저장 오류", f"{type(exc).__name__}: {exc}", "백그라운드 확인", traceback.format_exc())


@monitor_servers.before_loop
async def before_monitor():
    await bot.wait_until_ready()
    monitor_servers.change_interval(seconds=uptime_interval())


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN이 .env 파일에 설정되어 있지 않습니다.")
    init_db()
    bot.run(TOKEN)
    flush_traffic()  # the last minute's counts, when the bot is stopped normally
