import os
import random
import re
import math
import time
import sqlite3
import asyncio
import io
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

import cards
import dashboard
import net
import easter_eggs
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
CHECK_INTERVAL_SECONDS = env_int("CHECK_INTERVAL_SECONDS", 10, 1)
HISTORY_DAYS = 30
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
class MacoContext(commands.Context):
    async def reply(self, content=None, **kwargs):
        """Reply to the command message; if it was deleted meanwhile (stats take seconds), the answer still
        arrives as a plain message instead of failing with an error."""
        kwargs.setdefault("reference", self.message.to_reference(fail_if_not_exists=False))
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
)

# Per-server activity for £servers: kind -> guild id -> {hour number: count}. Memory only, last 24 hours.
traffic = {"messages": {}, "commands": {}}


def record_traffic(kind, guild_id):
    hour = int(time.time() // 3600)
    buckets = traffic[kind].setdefault(guild_id, {})
    buckets[hour] = buckets.get(hour, 0) + 1
    for old in [h for h in buckets if h <= hour - 24]:
        del buckets[old]


def traffic_24h(kind, guild_id):
    hour = int(time.time() // 3600)
    return sum(count for h, count in traffic[kind].get(guild_id, {}).items() if h > hour - 24)


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
        # Small key/value settings changed with owner commands (e.g. £DB 기간).
        conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
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


async def query_server(address: str, quiet=False):
    """Returns (online, latency_ms, players_online, players_max, via_external). quiet: no log line on failure
    (the background check logs only when a server goes down)."""
    try:
        # async_lookup: the plain lookup() resolves SRV records synchronously and freezes the whole bot.
        server = await asyncio.wait_for(JavaServer.async_lookup(address), timeout=5)
        status = await asyncio.wait_for(server.async_status(), timeout=5)
        latency = float(status.latency) if status.latency is not None else None
        return True, latency, status.players.online, status.players.max, False
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
                (now - HISTORY_DAYS * 24 * 60 * 60,),
            )
            last_prune = now
        conn.commit()


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


@bot.event
async def on_ready():
    init_db()
    if storage.configured() and not clean_files.is_running():
        clean_files.start()
    if not monitor_servers.is_running():
        monitor_servers.start()
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
            await message.reply("이스터에그는 디스코드 서버에서만 등록할 수 있습니다.", mention_author=False)
            return
        try:
            key = easter_eggs.register(message.guild.id, message.guild.name, *registration)
        except ValueError as exc:
            await message.reply(str(exc), mention_author=False)
            return
        except OSError as exc:
            print(f"이스터에그 저장 실패: {type(exc).__name__}: {exc}")
            await message.reply("저장 실패", mention_author=False)
            return
        await message.reply(f"굿: `€{key}`", mention_author=False)
        return

    if content.startswith("€") and content[1:].strip():
        text = None
        if message.guild:
            try:
                text = easter_eggs.find(message.guild.id, content[1:].strip())
            except OSError as exc:
                print(f"이스터에그 파일 읽기 실패: {type(exc).__name__}: {exc}")
        await message.reply(text or "바보", mention_author=False)


@bot.listen("on_message")
async def count_messages(message):
    if message.guild and message.author != bot.user:
        record_traffic("messages", message.guild.id)


@bot.listen("on_guild_remove")
async def forget_guild_traffic(guild):
    for counts in traffic.values():
        counts.pop(guild.id, None)


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
        state = "업로드 중" if row["status"] == "pending" else f"<t:{int(row['expires_at'])}:R> 삭제"
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
        rows = [dict(r) for r in conn.execute("SELECT id, key, url FROM files WHERE guild_id = ?", (guild_id,))]
    if not rows:
        return True, f"{guild_label(guild_id)}에 올라간 파일이 없습니다."
    if not await storage.delete([r["key"] for r in rows]):
        return False, "지우지 못했습니다. 저장소 연결을 확인해 주세요."
    with connect_db() as conn:
        conn.executemany("DELETE FROM files WHERE id = ?", [(r["id"],) for r in rows])
    for row in rows:
        await shortlinks.delete(row.get("url"))
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
    limit = human_duration(file_retention_max())
    if not args:
        await message.reply(f"자동 삭제 기간: **{current}** (설정할 수 있는 범위: 1시간 ~ {limit})\n"
                            "바꾸려면 `£DB 기간 3일`처럼 입력해 주세요.", mention_author=False)
        return
    await message.reply(apply_retention(" ".join(args))[1], mention_author=False)


def apply_retention(text):
    """(ok, message): set the auto-delete period from text like '3일' (£DB 기간 and the dashboard)."""
    current = retention_text()
    seconds = parse_duration(text)
    if seconds is None:
        return False, "기간을 알아보지 못했습니다. 예: `3일`, `12시간`, `1일 6시간`"
    if not FILE_RETENTION_MIN <= seconds <= file_retention_max():
        reason = " 비공개 저장소의 링크는 최대 7일까지만 쓸 수 있습니다." if not storage.PUBLIC_URL else ""
        return False, f"1시간 ~ {human_duration(file_retention_max())} 사이로 정해 주세요.{reason}"
    set_setting("file_retention_seconds", seconds)
    return True, (f"자동 삭제 기간을 **{current} → {human_duration(seconds)}**(으)로 바꿨습니다. "
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
        known = {r["key"] for r in conn.execute("SELECT key FROM files")}
    orphans = await storage.orphan_keys(known)
    if orphans is None:
        return False, f"기간이 지난 파일 {expired}개를 정리했습니다. 저장소 목록은 확인하지 못했습니다."
    removed = len(orphans) if orphans and await storage.delete(orphans) else 0
    return True, (f"기간이 지난 파일 **{expired}개**를 정리했습니다.\n"
                  f"목록에 없이 저장소에만 남아 있던 파일 **{removed}개**를 지웠습니다."
                  + ("\n⚠️ 일부를 지우지 못했습니다." if orphans and not removed else ""))


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
    limit = env_int("MEMORY_LIMIT_MB", 128, 1) * 1024 * 1024
    parts = []
    if current is not None:
        parts.append(f"지금 **{human_size(current)}** / {human_size(limit)} ({current / limit * 100:.0f}%)")
    if peak is not None:
        parts.append(f"켜진 뒤 최대 {human_size(peak)}")
    embed.add_field(name="🧠 메모리", inline=False, value=" · ".join(parts) or "이 호스팅에서는 확인할 수 없습니다")
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
            f"등록 서버 {servers:,}개 · 업타임 기록 {samples:,}개 (30일 보관)"
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
        f"영상 변환(ffmpeg) {'있음' if storage.can_convert() else '없음'}"
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
    lines = [
        f"**{discord.utils.escape_markdown(g.name)}** · 멤버 "
        f"{f'{g.member_count:,}' if g.member_count is not None else '?'}명 · "
        f"메시지 {traffic_24h('messages', g.id):,} · 명령어 {traffic_24h('commands', g.id):,}"
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
    embed.set_footer(text="메시지·명령어 수: 최근 24시간, 봇이 켜진 뒤부터 집계")
    await message.reply(embed=embed, mention_author=False)


@bot.command(name="도움말", aliases=["help"])
async def help_command(ctx):
    await ctx.reply(embed=command_list_embed(), mention_author=False)


def command_list_embed():
    """Every ! command; used by !도움말 and the /도움말 명령어 보기 button."""
    embed = discord.Embed(title="Maco", color=discord.Color.green())
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
    embed.add_field(name="!올리기 [이름]", value="파일을 첨부하면 올리고 미리보기로 보여 줍니다(영상은 바로 재생). 첨부 없이 쓰면 큰 파일용 링크를 DM으로 받습니다. "
                    f"{retention_text()} 후 자동 삭제됩니다.", inline=False)
    embed.add_field(name="!파일 [이름]", value="내가 올린 파일 목록을 봅니다. 이름을 붙이면 그 파일을 미리보기로 보냅니다.", inline=False)
    embed.add_field(name="!파일삭제 <이름>", value="내가 올린 파일을 지웁니다.", inline=False)
    embed.add_field(name="!운세", value="오늘의 운세를 봅니다. 칠 때마다 무작위로 나옵니다.", inline=False)
    embed.add_field(name="!일침", value="정신이 번쩍 드는 한마디를 듣습니다. 칠 때마다 무작위로 나옵니다.", inline=False)
    embed.add_field(name="/글자콘 글자:<글자> [메시지:<메시지>]",
                    value=f"글자를 디시 글자콘처럼 그림으로 만들어 올립니다 (최대 {textcon.MAX_CHARS}자). 메시지를 넣으면 같이 보냅니다.", inline=False)
    # Divider between the feature commands and contacting the developer (embeds have no real separator).
    embed.add_field(name="​", value="─" * 24, inline=False)
    embed.add_field(
        name="!개발자소통",
        value="개발자에게 메시지를 보냅니다.\n(`/도움말`의 ✉️ DM to Dev. 버튼으로도 보낼 수 있습니다)",
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
    result, notice = await run_with_notice(ctx, query_server(address))
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


async def report_error(ctx, error):
    """Log an unexpected command error and tell the user, replacing the waiting notice if one is up."""
    original = getattr(error, "original", error)
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


def build_list_embed(servers, results, now):
    lines_by_server = []
    online_count = 0
    for server, (online, latency, players_online, players_max, external) in zip(servers, results):
        online_count += int(online)
        day_percent, _, _ = uptime_stats(server["id"], now - 24 * 60 * 60)
        week_percent, _, _ = uptime_stats(server["id"], now - 7 * 24 * 60 * 60)
        if online:
            status = f"{latency:.0f} ms" if latency is not None else "외부 조회" if external else "-"
            if players_online is not None and players_max is not None:
                status = f"접속 {players_online}/{players_max} · {status}"
        else:
            status = "응답 없음"
        value = f"{status}\n24시간 {percent_text(day_percent)} · 7일 {percent_text(week_percent)}"
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
    embed.set_footer(text=f"{CHECK_INTERVAL_SECONDS}초마다 확인 · 자세히: !업타임 <이름>")
    return embed


def build_detail_embed(server, result, now):
    online, latency, players_online, players_max, external = result
    day_percent, day_samples, avg_latency = uptime_stats(server["id"], now - 24 * 60 * 60)
    week_percent, week_samples, _ = uptime_stats(server["id"], now - 7 * 24 * 60 * 60)

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
    embed.set_footer(text=f"{CHECK_INTERVAL_SECONDS}초마다 확인")
    return embed


@bot.command(name="업타임")
@commands.guild_only()
async def uptime(ctx, *, name: str = ""):
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
    save_samples([(server["id"], ctx.guild.id, online, latency)
                  for server, (online, latency, _, _, _) in zip(servers, results)])

    now = time.time()
    if name:
        embed = build_detail_embed(servers[0], results[0], now)
    else:
        embed = build_list_embed(servers, results, now)
    await send_result(ctx, notice, embed=embed)


async def lookup(ctx, awaitable):
    """Turn a StatsError into a (None, message) pair so it can be shown to the user; API problems also go to the admin."""
    try:
        return await awaitable, None
    except stats.StatsError as exc:
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
        if profile.get("matches_error"):
            note_match_failure(profile["matches_error"], where)
        return profile, None, []
    except stats.PlayerNotFound:
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


def file_retention_max():
    # On a private bucket links are presigned, and those last at most 7 days.
    return 30 * 24 * 60 * 60 if storage.PUBLIC_URL else storage.MAX_LINK_SECONDS


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
    return human_duration(file_retention())


def file_notice():
    return (f"📦 올린 파일은 봇의 파일 저장소에 올라가며 **{retention_text()} 후 자동으로 삭제**됩니다. "
            "링크가 있으면 누구나 볼 수 있습니다.")
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
    check_file_size(guild_id, size)
    converted = await storage.convert_url(attachment.url, filename) if storage.should_convert(filename, size) else None
    if converted:
        path, filename, size, cleanup = converted
        try:
            check_file_size(guild_id, size)
            check_file_quota(guild_id, size)
            key = storage.new_key(guild_id, filename)
            content_type = await storage.put_path(key, path, filename)
        finally:
            cleanup()
    else:
        check_file_quota(guild_id, size)
        key = storage.new_key(guild_id, filename)
        content_type = await storage.put_from_url(key, attachment.url, size, filename)
    name = unique_file_name(guild_id, user_id, file_display_name(name, filename))
    return save_file_row(guild_id, channel_id, user_id, name, key, content_type, size)


async def store_file(guild_id, channel_id, user_id, filename, data, name=""):
    """Upload small in-memory data and record it (used by tests and tools; users go through attachments)."""
    check_file_size(guild_id, len(data))
    check_file_quota(guild_id, len(data))
    key = storage.new_key(guild_id, filename)
    content_type = await storage.put_bytes(key, data, filename)
    name = unique_file_name(guild_id, user_id, file_display_name(name, filename))
    return save_file_row(guild_id, channel_id, user_id, name, key, content_type, len(data))


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
        short = await shortlinks.shorten(file_link(row), row["expires_at"])
        if short:
            row["url"] = short
            with connect_db() as conn:
                conn.execute("UPDATE files SET url = ? WHERE id = ?", (short, row["id"]))


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
        card = discord.ui.Container(accent_colour=discord.Colour.green())
        card.add_item(discord.ui.TextDisplay(f"📁 {file_title(row)}"))
        if media:  # the storage link itself: Discord may not preview through a redirect
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
            except storage.StorageError as exc:
                if exc.__cause__:
                    notify_admin("파일 저장소 오류", f"{type(exc.__cause__).__name__}: {exc.__cause__}", describe_ctx(ctx))
                errors.append(f"`{attachment.filename}`: {exc}")
            except discord.HTTPException:
                errors.append(f"`{attachment.filename}`: 디스코드에서 파일을 받지 못했습니다.")
        return rows, errors

    progress = Progress([("upload", "파일 올리는 중")])
    (rows, errors), notice = await run_with_notice(ctx, work(), progress)
    # The preview is a different kind of message (components v2), so it is posted fresh instead of
    # editing the waiting notice into it.
    await send_files(ctx_sender(ctx), rows, errors, getattr(ctx, "header", None))
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
    Raises StorageError when the server has no space left."""
    max_bytes = guild_upload_max(guild_id)
    if max_bytes <= 0:
        check_file_quota(guild_id, 1)  # raises with the server's numbers
        raise storage.StorageError("봇 전체 저장 공간이 가득 찼습니다. 오래된 파일이 자동 삭제된 뒤 다시 시도해 주세요.")
    key = storage.new_key(guild_id, "upload")
    row = save_file_row(guild_id, channel_id, user_id, name.strip()[:MAX_NAME_LENGTH], key, "", 0, status="pending")
    link = await storage.upload_page_link(key, retention_text(), shortlinks.signal_url(key), max_bytes)
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
            return
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
        await send_files(channel.send, [row], header=upload_header(row))
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
        reason = "비공개 저장소의 링크는 최대 7일까지만 쓸 수 있습니다. " if not storage.PUBLIC_URL else ""
        return False, f"{reason}지금부터 {human_duration(file_retention_max())} 안으로 정해 주세요. (최대 <t:{int(latest)}:f>)"
    expires_at = min(expires_at, latest)
    row["expires_at"] = expires_at
    note = ""
    if row.get("url") and not await shortlinks.update(row["url"], file_link(row), expires_at):
        note = "\n-# 단축 링크는 바꾸지 못해서, 이미 올라간 링크는 원래 만료일까지만 열립니다."
    with connect_db() as conn:
        conn.execute("UPDATE files SET expires_at = ? WHERE id = ?", (expires_at, row_id))
    if not storage.PUBLIC_URL and not note:
        note = "\n-# 채팅에 이미 올라간 미리보기는 원래 만료일 뒤에 안 보일 수 있습니다. [원본 열기]·[링크 복사]는 새 만료일까지 열립니다."
    return True, (f"**{discord.utils.escape_markdown(row['name'])}** 만료일을 <t:{int(expires_at)}:f>(으)로 바꿨습니다." + note)


async def remove_file(row):
    """Delete from storage and the list; False if storage refused (the row is kept so cleanup retries)."""
    if not await storage.delete([row["key"]]):
        return False
    await shortlinks.delete(row.get("url"))
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
        + f" · 만료 <t:{int(f['expires_at'])}:f> (<t:{int(f['expires_at'])}:R>)"
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
    reason = files_disabled_reason(ctx.guild)
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
    embed.set_footer(text=f"!파일 <이름>으로 미리보기 · {retention_text()} 후 자동 삭제")
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
        known = {r["key"] for r in conn.execute("SELECT key FROM files")}
    orphans = await storage.orphan_keys(known, min_age_seconds=2 * 60 * 60)
    if orphans and await storage.delete(orphans):
        print(f"목록에 없는 저장소 파일 {len(orphans)}개를 지웠습니다.")


@tasks.loop(minutes=10)
async def clean_files():
    """Delete files older than 7 days (and unfinished browser uploads) from storage and the list."""
    if time.time() - _last_orphan_sweep[0] > ORPHAN_SWEEP_SECONDS:
        try:
            await sweep_orphans()
        except Exception as exc:
            print(f"저장소 정리 오류: {type(exc).__name__}: {exc}")
    try:
        with connect_db() as conn:
            rows = [dict(r) for r in conn.execute("SELECT id, key FROM files WHERE expires_at < ?", (time.time(),))]
        if not rows:
            return
        if await storage.delete([r["key"] for r in rows]):
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
    "etc": ("🎲", "기타", ["fortune", "ilchim"]),
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
               "사진은 바로 보이고 영상은 채팅창에서 바로 재생됩니다. 작은 파일은 **📎 첨부**로도 올릴 수 있습니다.\n"
               "올린 파일은 봇의 파일 저장소에 올라가며 **{보관} 후 자동으로 삭제**됩니다."),
    "files": ("목록", "!파일", "`!파일 [이름]`\n내가 올린 파일 목록을 봅니다. 이름을 붙이면 그 파일을 미리보기로 보냅니다."),
    "filedelete": ("삭제", "!파일삭제", "`!파일삭제 <이름>`\n내가 올린 파일을 지웁니다."),
    "fortune": ("운세", "!운세", "`!운세`\n오늘의 운세를 봅니다. 칠 때마다 무작위로 나옵니다."),
    "ilchim": ("일침", "!일침", "`!일침`\n정신이 번쩍 드는 한마디를 듣습니다. 칠 때마다 무작위로 나옵니다."),
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
HELP_BUTTON_EMOJIS = {"fortune": "🔮", "ilchim": "💢", "upload": "📤", "files": "📄",
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
            message = await self.channel.send(content, **extra)
        except (discord.HTTPException, AttributeError):
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


async def run_from_help(ctx, awaitable):
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
        embed = command_list_embed()
        button("home", "◀ 처음으로", style=discord.ButtonStyle.secondary)
        return {"embed": embed, "view": view}
    if page == "home":
        embed = discord.Embed(description=notice, color=color)
        if HELP_BANNER_URL:
            embed.set_image(url=HELP_BANNER_URL)
        else:
            embed.title = "Maco"
        for key, (emoji, title, _) in HELP_CATEGORIES.items():
            button(f"page_{key}", title, emoji)
        # A row holds 5 buttons: categories on the first, the rest on the second.
        button("dm", "DM to Dev.", "✉️", row=1)
        button("commands", "명령어 보기", "📋", row=1)
        return {"embed": embed, "view": view}

    emoji, title, keys = HELP_CATEGORIES[page]
    if selected:
        _, command, _ = HELP_COMMANDS[selected]
        text = help_text(selected)
        title = f"{emoji} {title} · {command}"
    else:
        title, text = f"{emoji} {title}", "버튼을 누르면 바로 사용할 수 있습니다."
    for extra in (details, notice):
        if extra:
            text += f"\n\n{extra}"
    for key in keys:
        style = discord.ButtonStyle.success if key == selected else discord.ButtonStyle.secondary
        if key == "upload" and upload_url:
            # A link button: one press opens the upload page; the file is posted automatically afterwards.
            view.add_item(discord.ui.Button(label="올리기", emoji="🌐", url=upload_url, row=0))
        elif key == "upload" and upload_pending:
            # Shown at once while the link is made (B2 + NKOTE can take a few seconds), so the press shows.
            view.add_item(discord.ui.Button(label="준비 중...", emoji="⏳", disabled=True, row=0,
                                            custom_id="maco:help:wait"))
        else:
            button(f"run_{key}", HELP_COMMANDS[key][0], help_emojis.get(key) or HELP_BUTTON_EMOJIS.get(key), style)
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
    return HELP_COMMANDS[key][2].replace("{보관}", retention_text())


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

    if page.get("page") == "files" and interaction.guild:
        page["guild_id"] = interaction.guild.id
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
    await interaction.response.send_message(ephemeral=True, **help_message())


# ---------------------------------------------------------------- 대시보드 (nnaco.kro.kr/admin, on NKOTE)
# The dashboard does what the £ commands do. The bot only polls NKOTE (see dashboard.py): no web server,
# no extra library, nothing kept between polls, so it costs next to no memory.

STARTED_AT = time.time()
DASHBOARD_IDLE_SECONDS = 30  # nobody has the dashboard open
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
            "memory": {"current": current, "peak": peak, "limit": env_int("MEMORY_LIMIT_MB", 128, 1) * 1024 * 1024},
        },
        "retention": {"seconds": file_retention(), "text": retention_text(), "maxText": human_duration(retention_max),
                      "maxSeconds": retention_max},
        "limits": {"quota": json_limit(default_limit("quota")), "max": json_limit(default_limit("max")),
                   "hardMax": FILE_MAX_HARD, "totalQuota": FILE_TOTAL_QUOTA},
        "storage": {"configured": storage.configured()},
        "guilds": [], "admins": [], "files": [], "filesTotal": 0,
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
        ping_ms, error = await storage.ping()
        snapshot["storage"].update({
            "host": host, "bucket": bucket, "mode": mode, "ok": not error, "error": error,
            "pingMs": round(ping_ms) if ping_ms is not None else None, "used": file_usage(), "total": FILE_TOTAL_QUOTA,
            "ready": ready, "pending": pending, "nextExpiry": int(next_expiry) if next_expiry else None,
            "cleaner": clean_files.is_running(), "shortlinks": shortlinks.host() if shortlinks.configured() else None,
        })
    return snapshot


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
    # Card drawing runs in threads; two are enough and keep memory down (the default can start a dozen).
    asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=2, thread_name_prefix="maco"))
    bot.add_dynamic_items(AdminReplyButton, UserAnswerButton, HelpButton, UploadDoneButton, CopyLinkButton)
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


@tasks.loop(seconds=CHECK_INTERVAL_SECONDS)
async def monitor_servers():
    # An exception escaping this loop stops background checks for good, so contain every error.
    try:
        rows = all_servers()
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


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN이 .env 파일에 설정되어 있지 않습니다.")
    init_db()
    bot.run(TOKEN)
