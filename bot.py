import os
import re
import time
import sqlite3
import asyncio
import ipaddress
from datetime import datetime, timezone

import discord
from discord.ext import commands, tasks
from mcstatus import JavaServer
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
DB_PATH = os.getenv("DATABASE_PATH", "uptime.db")
CHECK_INTERVAL_SECONDS = max(30, int(os.getenv("CHECK_INTERVAL_SECONDS", "60")))
HISTORY_DAYS = 30

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)


def connect_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with connect_db() as conn:
        # One shared address per Discord server (guild), visible to all members.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS shared_servers (
                guild_id INTEGER PRIMARY KEY,
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
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_samples_guild_time ON samples(guild_id, checked_at)"
        )
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


async def query_server(address: str):
    try:
        server = JavaServer.lookup(address)
        status = await asyncio.wait_for(server.async_status(), timeout=5)
        return True, float(status.latency) if status.latency is not None else None, status.players.online, status.players.max
    except Exception:
        return False, None, None, None


def shared_server(guild_id: int):
    with connect_db() as conn:
        return conn.execute(
            "SELECT guild_id, address, registered_by, registered_at "
            "FROM shared_servers WHERE guild_id = ?",
            (guild_id,),
        ).fetchone()


def all_shared_servers():
    with connect_db() as conn:
        return conn.execute("SELECT guild_id, address FROM shared_servers").fetchall()


def save_sample(guild_id: int, online: bool, latency_ms):
    now = time.time()
    with connect_db() as conn:
        conn.execute(
            "INSERT INTO samples(guild_id, checked_at, online, latency_ms) VALUES (?, ?, ?, ?)",
            (guild_id, now, int(online), latency_ms),
        )
        conn.execute(
            "DELETE FROM samples WHERE checked_at < ?",
            (now - HISTORY_DAYS * 24 * 60 * 60,),
        )
        conn.commit()


def uptime_stats(guild_id: int, since: float):
    with connect_db() as conn:
        rows = conn.execute(
            "SELECT online, latency_ms FROM samples "
            "WHERE guild_id = ? AND checked_at >= ? ORDER BY checked_at",
            (guild_id, since),
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


@bot.event
async def on_ready():
    init_db()
    if not monitor_servers.is_running():
        monitor_servers.start()
    print(f"로그인 완료: {bot.user} (ID: {bot.user.id})")


@bot.command(name="도움말", aliases=["help"])
async def help_command(ctx):
    embed = discord.Embed(
        title="공용 마인크래프트 업타임 봇",
        description="주소는 디스코드 서버 전체에서 공유됩니다.",
        color=discord.Color.green(),
    )
    embed.add_field(
        name="!주소설정 <주소 또는 IP>",
        value="이 디스코드 서버의 공용 서버 주소를 등록하거나 변경합니다.\n"
              "예: `!주소설정 hypixel.net`",
        inline=False,
    )
    embed.add_field(
        name="!업타임",
        value="누구나 공용 서버의 현재 상태와 관측 업타임을 확인합니다.",
        inline=False,
    )
    embed.add_field(name="!주소", value="현재 공용 서버 주소를 확인합니다.", inline=False)
    embed.add_field(name="!주소삭제", value="공용 서버 주소와 업타임 기록을 삭제합니다.", inline=False)
    embed.set_footer(text=f"상태 확인 주기: {CHECK_INTERVAL_SECONDS}초")
    await ctx.reply(embed=embed, mention_author=False)


@bot.command(name="주소설정")
@commands.guild_only()
async def set_address(ctx, *, address: str = ""):
    if not address:
        await ctx.reply(
            "사용법: `!주소설정 <주소 또는 IP>`\n예: `!주소설정 hypixel.net`",
            mention_author=False,
        )
        return

    try:
        address = validate_address(address)
    except ValueError as exc:
        await ctx.reply(f"주소 형식 오류: {exc}", mention_author=False)
        return

    # Register even if temporarily offline, but tell the user the initial probe result.
    online, latency, players_online, players_max = await query_server(address)
    now = time.time()
    with connect_db() as conn:
        conn.execute(
            "INSERT INTO shared_servers(guild_id, address, registered_by, registered_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(guild_id) DO UPDATE SET "
            "address = excluded.address, registered_by = excluded.registered_by, "
            "registered_at = excluded.registered_at",
            (ctx.guild.id, address, ctx.author.id, now),
        )
        # Reset prior samples because they belong to the old shared address.
        conn.execute("DELETE FROM samples WHERE guild_id = ?", (ctx.guild.id,))
        conn.commit()

    if online:
        msg = f"공용 서버 주소를 등록했어: `{address}`\n현재 상태: 🟢 온라인 ({latency:.0f} ms)"
        if players_online is not None and players_max is not None:
            msg += f"\n접속 인원: {players_online}/{players_max}"
    else:
        msg = (
            f"공용 서버 주소를 등록했어: `{address}`\n"
            "현재 응답이 없어. 주소와 포트를 확인해 줘. 등록은 완료됐고 이후 상태를 계속 확인할게."
        )
    await ctx.reply(msg, mention_author=False)


@bot.command(name="주소")
@commands.guild_only()
async def show_address(ctx):
    server = shared_server(ctx.guild.id)
    if not server:
        await ctx.reply("아직 공용 서버 주소가 등록되지 않았어. `!주소설정 <주소 또는 IP>`를 사용해 줘.", mention_author=False)
        return
    await ctx.reply(f"현재 공용 서버 주소: `{server['address']}`", mention_author=False)


@bot.command(name="주소삭제")
@commands.guild_only()
@commands.has_guild_permissions(manage_guild=True)
async def delete_address(ctx):
    with connect_db() as conn:
        conn.execute("DELETE FROM shared_servers WHERE guild_id = ?", (ctx.guild.id,))
        conn.execute("DELETE FROM samples WHERE guild_id = ?", (ctx.guild.id,))
        conn.commit()
    await ctx.reply("이 디스코드 서버의 공용 주소와 업타임 기록을 삭제했어.", mention_author=False)


@delete_address.error
async def delete_address_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.reply("`!주소삭제`는 서버 관리 권한이 있는 사람만 사용할 수 있어.", mention_author=False)
    else:
        await ctx.reply("명령어 처리 중 오류가 발생했어.", mention_author=False)


@bot.command(name="업타임")
@commands.guild_only()
async def uptime(ctx):
    server = shared_server(ctx.guild.id)
    if not server:
        await ctx.reply("아직 공용 서버 주소가 등록되지 않았어. `!주소설정 <주소 또는 IP>`를 사용해 줘.", mention_author=False)
        return

    address = server["address"]
    online, latency, players_online, players_max = await query_server(address)
    save_sample(ctx.guild.id, online, latency)

    now = time.time()
    day_percent, day_samples, avg_latency = uptime_stats(ctx.guild.id, now - 24 * 60 * 60)
    week_percent, week_samples, _ = uptime_stats(ctx.guild.id, now - 7 * 24 * 60 * 60)

    embed = discord.Embed(
        title="마인크래프트 서버 업타임",
        description=f"공용 서버 주소: `{address}`",
        color=discord.Color.green() if online else discord.Color.red(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="현재 상태", value="🟢 온라인" if online else "🔴 오프라인 / 응답 없음", inline=True)
    embed.add_field(name="응답 시간", value=f"{latency:.0f} ms" if latency is not None else "측정 불가", inline=True)

    if players_online is not None and players_max is not None:
        embed.add_field(name="접속 인원", value=f"{players_online}/{players_max}", inline=True)

    embed.add_field(
        name="최근 24시간 관측 업타임",
        value=f"{day_percent:.2f}% ({day_samples}회 확인)" if day_percent is not None else "기록 수집 중",
        inline=False,
    )
    embed.add_field(
        name="최근 7일 관측 업타임",
        value=f"{week_percent:.2f}% ({week_samples}회 확인)" if week_percent is not None else "기록 수집 중",
        inline=False,
    )
    if avg_latency is not None:
        embed.add_field(name="평균 응답 시간 (24시간)", value=f"{avg_latency:.0f} ms", inline=True)
    embed.set_footer(
        text=f"이 디스코드 서버의 공용 주소 · 확인 주기 {CHECK_INTERVAL_SECONDS}초 · 기록은 봇 관측 표본 기준"
    )
    await ctx.reply(embed=embed, mention_author=False)


@tasks.loop(seconds=CHECK_INTERVAL_SECONDS)
async def monitor_servers():
    for row in all_shared_servers():
        online, latency, _, _ = await query_server(row["address"])
        save_sample(row["guild_id"], online, latency)
        await asyncio.sleep(0.2)


@monitor_servers.before_loop
async def before_monitor():
    await bot.wait_until_ready()


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN이 .env 파일에 설정되어 있지 않습니다.")
    init_db()
    bot.run(TOKEN)
