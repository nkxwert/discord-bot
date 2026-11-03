import os
import re
import math
import time
import sqlite3
import asyncio
import io
import ipaddress
import traceback
from datetime import datetime, timezone
from urllib.parse import quote

import aiohttp
import discord
from discord.ext import commands, tasks
from mcstatus import JavaServer
from dotenv import load_dotenv

import cards
import easter_eggs
import stats

load_dotenv()


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
PRUNE_INTERVAL_SECONDS = 60
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
# Replies echo user input (server names, Riot IDs); never let that ping @everyone, roles or users.
bot = commands.Bot(
    command_prefix="!",
    intents=intents,
    help_command=None,
    case_insensitive=True,
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


def connect_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


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
        headers = {"User-Agent": "discord-minecraft-uptime-bot"}
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(EXTERNAL_API_URL + quote(address, safe=":")) as response:
                response.raise_for_status()
                data = await response.json()
        players = data.get("players") or {}
        result = (bool(data.get("online")), None, players.get("online"), players.get("max"))
    except Exception as exc:
        print(f"외부 조회 실패 ({address}): {type(exc).__name__}: {exc}")
        # Keyed by error kind, not address, so an mcsrvstat outage is one DM rather than one per server.
        kind = f"HTTP {exc.status}" if isinstance(exc, aiohttp.ClientResponseError) else type(exc).__name__
        notify_admin("외부 상태 조회(mcsrvstat) 실패", kind, f"주소: {address}\n{type(exc).__name__}: {exc}")
    external_cache[address.lower()] = (now, result)
    return result


async def query_server(address: str):
    """Returns (online, latency_ms, players_online, players_max, via_external)."""
    try:
        # async_lookup: the plain lookup() resolves SRV records synchronously and freezes the whole bot.
        server = await JavaServer.async_lookup(address)
        status = await asyncio.wait_for(server.async_status(), timeout=5)
        latency = float(status.latency) if status.latency is not None else None
        return True, latency, status.players.online, status.players.max, False
    except Exception as exc:
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


def save_sample(server_id: int, guild_id: int, online: bool, latency_ms):
    global last_prune
    now = time.time()
    with connect_db() as conn:
        conn.execute(
            "INSERT INTO samples(server_id, guild_id, checked_at, online, latency_ms) "
            "VALUES (?, ?, ?, ?, ?)",
            (server_id, guild_id, now, int(online), latency_ms),
        )
        if now - last_prune >= PRUNE_INTERVAL_SECONDS:
            conn.execute(
                "DELETE FROM samples WHERE checked_at < ?",
                (now - HISTORY_DAYS * 24 * 60 * 60,),
            )
            last_prune = now
        conn.commit()


def uptime_stats(server_id: int, since: float):
    with connect_db() as conn:
        rows = conn.execute(
            "SELECT online, latency_ms FROM samples "
            "WHERE server_id = ? AND checked_at >= ? ORDER BY checked_at",
            (server_id, since),
        ).fetchall()
    if not rows:
        return None, 0, None
    online_count = sum(row["online"] for row in rows)
    percentage = online_count / len(rows) * 100
    latencies = [
        row["latency_ms"] for row in rows
        if row["online"] and row["latency_ms"] is not None
    ]
    average_latency = sum(latencies) / len(latencies) if latencies else None
    return percentage, len(rows), average_latency


def percent_text(percentage):
    return f"{percentage:.2f}%" if percentage is not None else "수집 중"


@bot.event
async def on_ready():
    init_db()
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


@bot.listen("on_message")
async def owner_servers(message):
    # Developer-only "£servers": lists the servers the bot is in. Anyone else gets no reaction at all.
    if message.author.bot or message.content.strip().lower() != "£servers":
        return
    if not await bot.is_owner(message.author):
        return
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
    # Divider between the feature commands and contacting the developer (embeds have no real separator).
    embed.add_field(name="​", value="─" * 24, inline=False)
    embed.add_field(
        name="!개발자소통",
        value="개발자에게 메시지를 보냅니다.\n(`/도움말`의 ✉️ DM to Dev. 버튼으로도 보낼 수 있습니다)",
        inline=False,
    )
    return embed


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
    for server, (online, latency, _, _, _) in zip(servers, results):
        save_sample(server["id"], ctx.guild.id, online, latency)

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
    await run_hypixel(ctx, name, game_key)


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


async def valorant_lookup(name, tag, region, suggest=True, progress=None, where=""):
    """Returns (profile, error, suggestions); suggestions are similar existing accounts when name#tag is unknown.
    `where` describes the request for admin error DMs."""
    try:
        return await stats.valorant_profile(name, tag, region, progress.step if progress else None), None, []
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

    def __init__(self, author_id, accounts, region):
        super().__init__(timeout=60)
        self.author_id = author_id
        self.region = region
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

    def choose(self, account):
        async def callback(interaction):
            self.stop()
            progress = Progress(VALORANT_STEPS)
            await interaction.response.edit_message(content=progress.render(), view=None)
            progress.attach(lambda text: interaction.edit_original_response(content=text))
            where = describe_interaction(interaction, f"버튼: Riot ID 선택 ({account['query_name']}#{account['query_tag']})")
            try:
                profile, error, _ = await valorant_lookup(
                    account["query_name"], account["query_tag"], self.region,
                    suggest=False, progress=progress, where=where,
                )
                if error:
                    await interaction.edit_original_response(content=error)
                    return
                await progress.step("card")
                image = await asyncio.to_thread(cards.valorant_card, profile)
                await interaction.edit_original_response(
                    content=None, attachments=[discord.File(image, filename="valorant.png")]
                )
            except Exception as exc:
                # Without this the message would stay on "불러오는 중입니다..." forever.
                print(f"전적 버튼 오류: {type(exc).__name__}: {exc}")
                notify_admin("전적 버튼 처리 중 오류", f"{type(exc).__name__}: {exc}", where, traceback.format_exc())
                await interaction.edit_original_response(content="오류가 발생했습니다.")
        return callback

    async def decline(self, interaction):
        self.stop()
        await interaction.response.edit_message(content="알겠습니다. Riot ID를 다시 확인해 주세요.", view=None)

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
    await run_valorant(ctx, name, tag, region)


async def run_valorant(ctx, name, tag, region):
    progress = Progress(VALORANT_STEPS)
    (profile, error, suggestions), notice = await run_with_notice(
        ctx, valorant_lookup(name, tag, region, progress=progress, where=describe_ctx(ctx)), progress
    )
    if suggestions:
        view = RiotIdChoice(ctx.author.id, suggestions, region)
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


# ---------------------------------------------------------------- /도움말 (buttons)

HELP_CATEGORIES = {
    "uptime": ("🖥️", "서버 업타임", ["add", "uptime", "list", "delete"]),
    "stats": ("📊", "전적", ["hypixel", "valorant"]),
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
    "valorant": ("Valorant 전적", "!V전적", "`!V전적 <이름#태그> [지역]`\n발로란트 전적 카드를 봅니다. 지역은 자동으로 찾습니다.\n"
                 "`!V전적 <이름#태그> 지역`으로도 검색할 수 있습니다."),
}
HELP_TIMEOUT_SECONDS = 300
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
    Replies are public follow-up messages, so the result is visible in the channel like a ! command."""

    def __init__(self, interaction, what):
        self.interaction = interaction
        self.guild = interaction.guild
        self.author = interaction.user
        self.channel = interaction.channel
        self.message = type("HelpMessage", (), {"content": f"/도움말 → {what}"})()  # for describe_ctx
        self.notice = None

    async def reply(self, content=None, *, embed=None, file=None, view=None, mention_author=None):
        extra = {k: v for k, v in {"content": content, "embed": embed, "file": file, "view": view}.items() if v is not None}
        return await self.interaction.followup.send(wait=True, **extra)


async def run_from_help(ctx, awaitable):
    try:
        await awaitable
    except Exception as exc:
        await report_error(ctx, exc)


def stats_wait_left(command, ctx):
    """Spend the ! command's cooldown (shared with the button) and return the seconds left if it was in use."""
    retry_after = command._buckets.update_rate_limit(ctx)
    return math.ceil(retry_after) if retry_after else 0


class HelpView(discord.ui.View):
    """The private /도움말 message: categories -> command buttons that run the command.
    Everything private (lists, notices) is shown by editing this one message; only results meant
    for the channel (uptime, stats cards, ...) become separate public messages."""

    def __init__(self, interaction):
        super().__init__(timeout=HELP_TIMEOUT_SECONDS)
        self.last_interaction = interaction  # edits the message when the buttons time out
        self.page = "home"
        self.selected = None
        self.notice = None  # one-off line such as "서버 관리 권한이 필요합니다."
        self.details = None  # extra private content, such as the 주소 list
        self.build()

    def embed(self):
        color = discord.Color.green()
        if self.page == "commands":
            return command_list_embed()
        if self.page == "home":
            embed = discord.Embed(description=self.notice, color=color)
            if HELP_BANNER_URL:
                embed.set_image(url=HELP_BANNER_URL)
            else:
                embed.title = "Maco"
            return embed
        emoji, title, _ = HELP_CATEGORIES[self.page]
        if self.selected:
            _, command, text = HELP_COMMANDS[self.selected]
            title = f"{emoji} {title} · {command}"
        else:
            title, text = f"{emoji} {title}", "버튼을 누르면 바로 사용할 수 있습니다."
        for extra in (self.details, self.notice):
            if extra:
                text += f"\n\n{extra}"
        return discord.Embed(title=title, description=text[:4096], color=color)

    def build(self):
        self.clear_items()
        if self.page == "home":
            for key, (emoji, title, _) in HELP_CATEGORIES.items():
                self.add_button(title, emoji, discord.ButtonStyle.primary, self.open_category(key))
            self.add_button("DM to Dev.", "✉️", discord.ButtonStyle.primary, self.dm_to_dev)
            self.add_button("명령어 보기", "📋", discord.ButtonStyle.primary, self.show_commands)
            return
        if self.page == "commands":
            self.add_button("◀ 처음으로", None, discord.ButtonStyle.secondary, self.go_home)
            return
        for key in HELP_CATEGORIES[self.page][2]:
            style = discord.ButtonStyle.success if key == self.selected else discord.ButtonStyle.secondary
            self.add_button(HELP_COMMANDS[key][0], help_emojis.get(key), style, getattr(self, f"run_{key}"))
        self.add_button("◀ 처음으로", None, discord.ButtonStyle.secondary, self.go_home, row=1)

    def add_button(self, label, emoji, style, callback, row=0):
        button = discord.ui.Button(label=label, emoji=emoji, style=style, row=row)
        button.callback = callback
        self.add_item(button)

    async def send(self, interaction):
        await interaction.response.send_message(embed=self.embed(), view=self, ephemeral=True)

    async def interaction_check(self, interaction):
        self.last_interaction = interaction
        return True

    async def show(self, interaction, notice=None, details=None, **state):
        """Redraw this message in place. `page` opens a category, `selected` marks a command."""
        if "page" in state:
            self.page, self.selected = state["page"], None
        if "selected" in state:
            self.selected = state["selected"]
        self.notice, self.details = notice, details
        self.build()
        changes = {"embed": self.embed(), "view": self}
        if interaction.response.is_done():
            await interaction.edit_original_response(**changes)
        else:
            await interaction.response.edit_message(**changes)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        try:
            await self.last_interaction.edit_original_response(view=self)
        except discord.HTTPException:
            pass  # the message was dismissed, or the interaction token expired

    async def on_error(self, interaction, error, item):
        trace = "".join(traceback.format_exception(type(error), error, error.__traceback__))
        print(f"도움말 버튼 오류:\n{trace}")
        notify_admin("도움말 버튼 오류", f"{type(error).__name__}: {error}",
                     describe_interaction(interaction, f"버튼: {getattr(item, 'label', '?')}"), trace)
        send = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
        await send("오류가 발생했습니다.", ephemeral=True)

    # ---- home page

    def open_category(self, key):
        async def callback(interaction):
            if key == "uptime" and interaction.guild is None:
                await self.show(interaction, notice=SERVER_ONLY_MESSAGE)
                return
            await self.show(interaction, page=key)
        return callback

    async def dm_to_dev(self, interaction):
        left = feedback_wait_left(interaction.user.id)
        if left:
            await self.show(interaction, notice=f"{left}초 후에 다시 보낼 수 있습니다.")
            return
        await interaction.response.send_modal(HelpFeedbackModal(self))

    async def show_commands(self, interaction):
        await self.show(interaction, page="commands")

    async def go_home(self, interaction):
        await self.show(interaction, page="home")

    # ---- command buttons

    async def run_add(self, interaction):
        await interaction.response.send_modal(AddServerModal(self))

    async def run_uptime(self, interaction):
        if not guild_servers(interaction.guild.id):
            await self.show(interaction, selected="uptime", notice=NO_SERVER_MESSAGE)
            return
        await self.show(interaction, selected="uptime")
        ctx = HelpContext(interaction, "업타임")
        await run_from_help(ctx, uptime.callback(ctx))

    async def run_list(self, interaction):
        servers = guild_servers(interaction.guild.id)
        if not servers:
            await self.show(interaction, selected="list", notice=NO_SERVER_MESSAGE)
            return
        lines = "\n".join(f"• **{s['name']}** — `{s['address']}`" for s in servers)
        await self.show(interaction, selected="list", details=f"**등록된 서버**\n{lines}")

    async def run_delete(self, interaction):
        if not interaction.user.guild_permissions.manage_guild:
            await self.show(interaction, selected="delete", notice="서버 관리 권한이 필요합니다.")
            return
        servers = guild_servers(interaction.guild.id)
        if not servers:
            await self.show(interaction, selected="delete", notice=NO_SERVER_MESSAGE)
            return
        await interaction.response.send_modal(DeleteServerModal(self, servers))

    async def run_hypixel(self, interaction):
        await interaction.response.send_modal(HypixelModal(self))

    async def run_valorant(self, interaction):
        await interaction.response.send_modal(ValorantModal(self))


class HelpFeedbackModal(FeedbackModal):
    """The developer form opened from /도움말; the outcome replaces the help message instead of a new one."""

    def __init__(self, help_view):
        super().__init__()
        self.help_view = help_view

    async def on_submit(self, interaction):
        self.help_view.last_interaction = interaction
        await interaction.response.defer()  # photos can take a while; keeps the help message as it is
        kind = self.kind.values[0] if self.kind.values else "기타"
        photos = await collect_photos(self.photos.values)
        message = await finish_feedback(
            interaction.user, f"[{kind}] {self.subject.value}", self.body.value,
            describe_place(interaction.guild, interaction.channel), photos,
        )
        await self.help_view.show(interaction, notice=message)


class HelpModal(SafeModal):
    """Input form opened from a /도움말 button, with the command's description at the top."""

    def __init__(self, help_view, key):
        label, _, text = HELP_COMMANDS[key]
        super().__init__(title=label)
        self.help_view = help_view
        self.key = key
        self.add_item(discord.ui.TextDisplay(text))

    async def reject(self, interaction, notice):
        """Show why the input can't be used on the help message, without posting anything."""
        self.help_view.last_interaction = interaction
        await self.help_view.show(interaction, selected=self.key, notice=notice)

    async def finish(self, interaction):
        """Show this command's description on the help message, then the result follows publicly."""
        self.help_view.last_interaction = interaction
        await self.help_view.show(interaction, selected=self.key)
        return HelpContext(interaction, HELP_COMMANDS[self.key][0])


class AddServerModal(HelpModal):
    def __init__(self, help_view):
        super().__init__(help_view, "add")
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
    def __init__(self, help_view, servers):
        super().__init__(help_view, "delete")
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
    GAMES = [("전체", "all"), ("베드워즈 (bw)", "bedwars"), ("스카이워즈 (sw)", "skywars"), ("듀얼 (duels)", "duels")]

    def __init__(self, help_view):
        super().__init__(help_view, "hypixel")
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
    def __init__(self, help_view):
        super().__init__(help_view, "valorant")
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


@bot.tree.command(name="도움말", description="버튼으로 명령어를 보고 바로 사용합니다")
async def help_slash(interaction: discord.Interaction):
    await HelpView(interaction).send(interaction)


async def setup_hook():
    bot.add_dynamic_items(AdminReplyButton, UserAnswerButton)
    await load_help_emojis()
    try:
        synced = await bot.tree.sync()
        print(f"슬래시 명령어 {len(synced)}개를 등록했습니다.")
    except discord.HTTPException as exc:
        print(f"슬래시 명령어 등록 실패: {type(exc).__name__}: {exc}")

bot.setup_hook = setup_hook


@tasks.loop(seconds=CHECK_INTERVAL_SECONDS)
async def monitor_servers():
    # An exception escaping this loop stops background checks for good, so contain every error.
    try:
        rows = all_servers()
    except sqlite3.Error as exc:
        print(f"백그라운드 확인 오류: {type(exc).__name__}: {exc}")
        notify_admin("백그라운드 서버 확인 오류", f"{type(exc).__name__}: {exc}", "서버 목록 읽기", traceback.format_exc())
        return
    for row in rows:
        try:
            online, latency, _, _, _ = await query_server(row["address"])
            save_sample(row["id"], row["guild_id"], online, latency)
        except Exception as exc:
            print(f"백그라운드 확인 오류 ({row['address']}): {type(exc).__name__}: {exc}")
            notify_admin(
                "백그라운드 서버 확인 오류", f"{type(exc).__name__}: {exc}", f"주소: {row['address']}",
                traceback.format_exc(),
            )
        await asyncio.sleep(0.2)


@monitor_servers.before_loop
async def before_monitor():
    await bot.wait_until_ready()


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN이 .env 파일에 설정되어 있지 않습니다.")
    init_db()
    bot.run(TOKEN)
