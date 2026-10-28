import os
import re
import time
import sqlite3
import asyncio
import ipaddress
from datetime import datetime, timezone
from urllib.parse import quote

import aiohttp
import discord
from discord.ext import commands, tasks
from mcstatus import JavaServer
from dotenv import load_dotenv

import cards
import stats

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
DB_PATH = os.getenv("DATABASE_PATH", "uptime.db")
CHECK_INTERVAL_SECONDS = max(1, int(os.getenv("CHECK_INTERVAL_SECONDS", "10")))
HISTORY_DAYS = 30
PRUNE_INTERVAL_SECONDS = 60
MAX_SERVERS_PER_GUILD = 10
MAX_NAME_LENGTH = 30
NO_SERVER_MESSAGE = "등록된 서버가 없습니다. `!주소추가 <주소> [이름]`으로 추가해 주세요."
# Last resort when the direct ping fails; only domain names are ever sent out, never IPs.
EXTERNAL_FALLBACK = os.getenv("EXTERNAL_FALLBACK", "true").strip().lower() not in ("0", "false", "no", "off")
EXTERNAL_API_URL = "https://api.mcsrvstat.us/3/"
EXTERNAL_TIMEOUT_SECONDS = 8
EXTERNAL_COOLDOWN_SECONDS = 60
SLOW_NOTICE_SECONDS = 2
STATS_COOLDOWN_SECONDS = 5
HYPIXEL_GAMES = {
    "bw": "bedwars", "bedwars": "bedwars", "베드워즈": "bedwars", "베워": "bedwars",
    "sw": "skywars", "skywars": "skywars", "스카이워즈": "skywars", "스카": "skywars",
    "duels": "duels", "duel": "duels", "듀얼": "duels",
}
external_cache = {}
last_prune = 0.0

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None, case_insensitive=True)


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
        host = address

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
    external_cache[address.lower()] = (now, result)
    return result


async def query_server(address: str):
    """Returns (online, latency_ms, players_online, players_max, via_external)."""
    try:
        server = JavaServer.lookup(address)
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


@bot.command(name="도움말", aliases=["help"])
async def help_command(ctx):
    embed = discord.Embed(
        title="마인크래프트 업타임 봇",
        color=discord.Color.green(),
    )
    embed.add_field(
        name="!주소추가 <주소> [이름]",
        value="서버를 추가합니다. (예: `!주소추가 hypixel.net 하이픽셀`)",
        inline=False,
    )
    embed.add_field(name="!업타임", value="등록된 모든 서버의 상태를 봅니다.", inline=False)
    embed.add_field(name="!업타임 <이름>", value="서버 하나의 자세한 업타임을 봅니다.", inline=False)
    embed.add_field(name="!주소", value="등록된 서버 목록을 봅니다.", inline=False)
    embed.add_field(name="!주소삭제 <이름>", value="서버를 삭제합니다. (서버 관리 권한 필요)", inline=False)
    embed.add_field(
        name="!H전적 <닉네임> [bw|sw|duels]",
        value="하이픽셀 전적 카드를 봅니다. 게임을 붙이면 게임별 전적을 봅니다.",
        inline=False,
    )
    embed.add_field(
        name="!V전적 <이름#태그> [지역]",
        value="발로란트 랭크와 최근 경기를 봅니다. 지역 기본값은 kr입니다.",
        inline=False,
    )
    embed.set_footer(text=f"확인 주기: {CHECK_INTERVAL_SECONDS}초")
    await ctx.reply(embed=embed, mention_author=False)


@bot.command(name="주소추가")
@commands.guild_only()
async def add_server(ctx, address: str = "", name: str = ""):
    if not address:
        await ctx.reply(
            "사용법: `!주소추가 <주소> [이름]` (예: `!주소추가 hypixel.net 하이픽셀`)",
            mention_author=False,
        )
        return

    try:
        address = validate_address(address)
    except ValueError as exc:
        await ctx.reply(f"주소 형식 오류: {exc}", mention_author=False)
        return

    name = name or address
    if len(name) > MAX_NAME_LENGTH:
        await ctx.reply(f"이름은 {MAX_NAME_LENGTH}자 이하로 입력해 주세요.", mention_author=False)
        return

    servers = guild_servers(ctx.guild.id)
    if len(servers) >= MAX_SERVERS_PER_GUILD:
        await ctx.reply(f"서버는 최대 {MAX_SERVERS_PER_GUILD}개까지 등록할 수 있습니다.", mention_author=False)
        return
    if any(
        s["name"].lower() == name.lower() or s["address"].lower() == address.lower()
        for s in servers
    ):
        await ctx.reply("이미 등록된 이름 또는 주소입니다.", mention_author=False)
        return

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
    await ctx.reply("\n".join(lines), mention_author=False)


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
    with connect_db() as conn:
        conn.execute("DELETE FROM servers WHERE id = ?", (server["id"],))
        conn.execute("DELETE FROM samples WHERE server_id = ?", (server["id"],))
        conn.commit()
    await ctx.reply(f"**{server['name']}** 서버와 업타임 기록을 삭제했습니다.", mention_author=False)


@delete_address.error
async def delete_address_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.reply("서버 관리 권한이 필요합니다.", mention_author=False)
    else:
        await ctx.reply("오류가 발생했습니다.", mention_author=False)


async def run_with_notice(ctx, awaitable):
    """Await a slow lookup, posting a waiting notice only if it takes a while."""
    task = asyncio.ensure_future(awaitable)
    done, _ = await asyncio.wait({task}, timeout=SLOW_NOTICE_SECONDS)
    notice = None if done else await ctx.reply("잠시만 기다려 주세요...", mention_author=False)
    try:
        return await task, notice
    except Exception:
        if notice:
            await notice.edit(content="오류가 발생했습니다.")
        raise


async def send_result(ctx, notice, content=None, embed=None, file=None):
    if notice:
        await notice.edit(content=content, embed=embed, attachments=[file] if file else [])
    elif file:
        await ctx.reply(content=content, embed=embed, file=file, mention_author=False)
    else:
        await ctx.reply(content=content, embed=embed, mention_author=False)


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


async def lookup(awaitable):
    """Turn a StatsError into a (None, message) pair so it can be shown to the user."""
    try:
        return await awaitable, None
    except stats.StatsError as exc:
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

    (profile, error), notice = await run_with_notice(ctx, lookup(stats.hypixel_profile(name)))
    if error:
        await send_result(ctx, notice, content=error)
        return
    if game_key:
        image = await asyncio.to_thread(cards.hypixel_game_card, profile, game_key)
    else:
        image = await asyncio.to_thread(cards.hypixel_card, profile)
    await send_result(ctx, notice, file=discord.File(image, filename=f"hypixel_{profile['name']}.png"))


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

    (profile, error), notice = await run_with_notice(ctx, lookup(stats.valorant_profile(name, tag, region)))
    if error:
        await send_result(ctx, notice, content=error)
        return
    image = await asyncio.to_thread(cards.valorant_card, profile)
    await send_result(ctx, notice, file=discord.File(image, filename="valorant.png"))


@hypixel_stats.error
@valorant_stats.error
async def stats_command_error(ctx, error):
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.reply(f"{error.retry_after:.0f}초 후에 다시 시도해 주세요.", mention_author=False)
    else:
        print(f"전적 명령어 오류: {type(error).__name__}: {error}")
        await ctx.reply("오류가 발생했습니다.", mention_author=False)


@tasks.loop(seconds=CHECK_INTERVAL_SECONDS)
async def monitor_servers():
    for row in all_servers():
        online, latency, _, _, _ = await query_server(row["address"])
        save_sample(row["id"], row["guild_id"], online, latency)
        await asyncio.sleep(0.2)


@monitor_servers.before_loop
async def before_monitor():
    await bot.wait_until_ready()


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN이 .env 파일에 설정되어 있지 않습니다.")
    init_db()
    bot.run(TOKEN)
