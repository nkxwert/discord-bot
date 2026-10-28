"""Renders Hypixel and Valorant stat cards as PNG images with Pillow."""
import io
from datetime import timedelta, timezone
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONT_DIR = Path(__file__).parent / "assets" / "fonts"
# "mc": Minecraft-style Latin font. "kr": pixel font with Hangul, for Riot IDs and Korean labels.
FONT_PATHS = {"mc": FONT_DIR / "Monocraft.ttf", "kr": FONT_DIR / "Galmuri11-Bold.ttf"}
WIDTH = 800
MARGIN = 16
GAP = 12
KST = timezone(timedelta(hours=9))

# Minecraft chat colours (§0-§f).
MC_RGB = {
    "0": (0, 0, 0), "1": (0, 0, 170), "2": (0, 170, 0), "3": (0, 170, 170),
    "4": (170, 0, 0), "5": (170, 0, 170), "6": (255, 170, 0), "7": (170, 170, 170),
    "8": (85, 85, 85), "9": (85, 85, 255), "a": (85, 255, 85), "b": (85, 255, 255),
    "c": (255, 85, 85), "d": (255, 85, 255), "e": (255, 255, 85), "f": (255, 255, 255),
}
WHITE = MC_RGB["f"]
GRAY = MC_RGB["7"]
GOLD = MC_RGB["6"]
GREEN = MC_RGB["a"]
PINK = MC_RGB["d"]
DARK_GREEN = MC_RGB["2"]
AQUA = MC_RGB["b"]
RED = MC_RGB["c"]

_fonts = {}


def font(size, family="mc"):
    if (family, size) not in _fonts:
        _fonts[family, size] = ImageFont.truetype(str(FONT_PATHS[family]), size)
    return _fonts[family, size]


def color_segments(text, default):
    """Split a §-coded string into (text, rgb) runs. Formatting codes (§l, §o...) are ignored."""
    runs, buf, color, i = [], "", default, 0
    while i < len(text):
        if text[i] == "§" and i + 1 < len(text):
            if buf:
                runs.append((buf, color))
                buf = ""
            code = text[i + 1].lower()
            if code in MC_RGB:
                color = MC_RGB[code]
            elif code == "r":
                color = default
            i += 2
            continue
        buf += text[i]
        i += 1
    if buf:
        runs.append((buf, color))
    return runs


def plain(text):
    return "".join(run for run, _ in color_segments(text, WHITE))


def fit(text, size, max_width, family="mc"):
    """Shrink the font (down to 14px) and then truncate so plain text fits max_width."""
    while size > 14 and font(size, family).getlength(plain(text)) > max_width:
        size -= 2
    if font(size, family).getlength(plain(text)) > max_width:
        while text and font(size, family).getlength(plain(text) + "...") > max_width:
            text = text[:-1]
        text += "..."
    return text, size


def draw_text(draw, x, y, text, size, default=WHITE, align="center", family="mc"):
    """Draw §-coloured text with a Minecraft-style drop shadow. y is the top edge."""
    f = font(size, family)
    runs = color_segments(text, default)
    width = sum(f.getlength(run) for run, _ in runs)
    if align == "center":
        x -= width / 2
    elif align == "right":
        x -= width
    shadow = max(2, size // 12)
    for run, rgb in runs:
        draw.text((x + shadow, y + shadow), run, font=f, fill=tuple(c // 4 for c in rgb))
        draw.text((x, y), run, font=f, fill=rgb)
        x += f.getlength(run)


class Card:
    def __init__(self, height, top=(18, 28, 46), bottom=(14, 58, 70), family="mc"):
        self.family = family
        self.image = Image.new("RGBA", (WIDTH, height))
        pixels = ImageDraw.Draw(self.image)
        for y in range(height):
            t = y / max(1, height - 1)
            pixels.line([(0, y), (WIDTH, y)], fill=tuple(round(a + (b - a) * t) for a, b in zip(top, bottom)))
        self.overlay = Image.new("RGBA", self.image.size, (0, 0, 0, 0))
        self.panels = ImageDraw.Draw(self.overlay)
        self.texts = []

    def panel(self, box):
        self.panels.rounded_rectangle(box, radius=10, fill=(0, 0, 0, 120), outline=(255, 255, 255, 45), width=2)

    def rect(self, box, rgba, radius=4):
        self.panels.rounded_rectangle(box, radius=radius, fill=rgba)

    def text(self, *args, **kwargs):
        kwargs.setdefault("family", self.family)
        self.texts.append((args, kwargs))

    def fit(self, text, size, max_width):
        return fit(text, size, max_width, self.family)

    def stat(self, box, label, value, rgb):
        self.panel(box)
        x0, y0, x1, y1 = box
        cx = (x0 + x1) / 2
        label, label_size = self.fit(label, 20, x1 - x0 - 16)
        value, value_size = self.fit(value if isinstance(value, str) else f"{value:,}", 32, x1 - x0 - 16)
        self.text(cx, y0 + 14, label, label_size, rgb)
        self.text(cx, y1 - value_size - 16, value, value_size, rgb)

    def stat_row(self, y, height, cells):
        cell_width = (WIDTH - 2 * MARGIN - GAP * (len(cells) - 1)) / len(cells)
        for i, (label, value, rgb) in enumerate(cells):
            x0 = MARGIN + i * (cell_width + GAP)
            self.stat((round(x0), y, round(x0 + cell_width), y + height), label, value, rgb)

    def picture(self, box, data, pixel_art=False, padding=8):
        """Fit image bytes inside box, centred. Returns False if there is nothing to draw."""
        if not data:
            return False
        try:
            picture = Image.open(io.BytesIO(data)).convert("RGBA")
        except Exception:
            return False
        x0, y0, x1, y1 = box
        scale = min((x1 - x0 - 2 * padding) / picture.width, (y1 - y0 - 2 * padding) / picture.height)
        size = (max(1, round(picture.width * scale)), max(1, round(picture.height * scale)))
        picture = picture.resize(size, Image.NEAREST if pixel_art and scale >= 1 else Image.LANCZOS)
        self.overlay.alpha_composite(picture, (round((x0 + x1 - size[0]) / 2), round((y0 + y1 - size[1]) / 2)))
        return True

    def skin(self, box, data):
        self.panel(box)
        if not self.picture(box, data, pixel_art=True):
            x0, y0, x1, y1 = box
            self.text((x0 + x1) / 2, (y0 + y1) / 2 - 20, "?", 40, GRAY)

    def footer(self, y, text="§7Minecraft Uptime Bot"):
        self.text(WIDTH / 2, y, text, 16)

    def render(self):
        self.image.alpha_composite(self.overlay)
        draw = ImageDraw.Draw(self.image)
        for args, kwargs in self.texts:
            draw_text(draw, *args, **kwargs)
        buffer = io.BytesIO()
        self.image.convert("RGB").save(buffer, format="PNG", optimize=True)
        buffer.seek(0)
        return buffer


def date_text(value):
    return value.astimezone(KST).strftime("%Y-%m-%d %H:%M") if value else "N/A"


def header(card, profile, y=MARGIN, skin_height=200):
    """Skin on the left, coloured name on the right. Returns the x where the right column starts."""
    card.skin((MARGIN, y, MARGIN + 140, y + skin_height), profile.get("skin"))
    right = MARGIN + 140 + GAP
    name, size = card.fit(profile["display_name"], 34, WIDTH - MARGIN - right - 24)
    card.panel((right, y, WIDTH - MARGIN, y + 64))
    card.text((right + WIDTH - MARGIN) / 2, y + 32 - size / 2 - 4, name, size)
    return right


def hypixel_card(profile):
    card = Card(800)
    right = header(card, profile)
    mid = right + (WIDTH - MARGIN - right - GAP) // 2

    guild = profile.get("guild")
    guild_name, guild_size = card.fit(f"§7Guild: §a{guild['name'] if guild else 'None'}", 20, mid - right - 24)
    card.panel((right, 92, mid, 146))
    card.text((right + mid) / 2, 108, guild_name, guild_size)

    online = profile.get("online")
    status = "§aOnline" if online else "§cOffline" if online is False else "§7N/A"
    card.panel((right, 162, mid, 216))
    card.text((right + mid) / 2, 178, f"§7Status: {status}", 20)

    card.panel((mid + GAP, 92, WIDTH - MARGIN, 216))
    for y, label, key in ((102, "First Login", "first_login"), (160, "Last Login", "last_login")):
        card.text(mid + GAP + 16, y, f"§7{label}", 18, align="left")
        card.text(mid + GAP + 16, y + 24, f"§b{date_text(profile.get(key))}", 18, align="left")

    card.stat_row(232, 90, [
        ("Network Level", profile["level"], GOLD),
        ("Achievement Points", profile["achievement_points"], GOLD),
    ])
    card.stat_row(334, 90, [
        ("Quests", profile["quests"], GREEN),
        ("Challenges", profile["challenges"], GREEN),
    ])
    card.stat_row(436, 90, [
        ("Karma", profile["karma"], PINK),
        ("Reward Streak", profile["reward_streak"], PINK),
        ("Gifts Sent", profile["gifts_sent"], PINK),
        ("Ranks Gifted", profile["ranks_gifted"], PINK),
    ])
    card.stat_row(538, 90, [
        ("Guild Quests", guild["quests"] if guild else 0, DARK_GREEN),
        ("Daily GEXP", guild["daily_gexp"] if guild else 0, DARK_GREEN),
        ("Weekly GEXP", guild["weekly_gexp"] if guild else 0, DARK_GREEN),
    ])
    games = profile["games"]
    card.stat_row(640, 90, [
        (f"{games[key]['title']} Wins", games[key]["wins"], AQUA)
        for key in ("bedwars", "skywars", "duels")
    ])
    card.footer(762)
    return card.render()


def hypixel_game_card(profile, game_key):
    game = profile["games"][game_key]
    rows = [game["rows"][i:i + 3] for i in range(0, len(game["rows"]), 3)]
    card = Card(MARGIN + 140 + GAP + len(rows) * 102 + 44)
    right = header(card, profile, skin_height=140)
    card.panel((right, 76, WIDTH - MARGIN, 140))
    card.text((right + WIDTH - MARGIN) / 2, 94, game["title"], 28, GOLD)

    y = MARGIN + 140 + GAP
    palette = (AQUA, GREEN, PINK, GOLD)
    for i, row in enumerate(rows):
        card.stat_row(y, 90, [(label, value, palette[i % len(palette)]) for label, value in row])
        y += 102
    card.footer(y + 8)
    return card.render()


# ---------------------------------------------------------------- Valorant

VALORANT_RED = (255, 70, 85)
WIN_GREEN = (80, 220, 140)
TIER_RGB = {
    "iron": (150, 150, 150), "bronze": (190, 140, 90), "silver": (210, 215, 220),
    "gold": (235, 195, 80), "platinum": (70, 190, 200), "diamond": (190, 130, 240),
    "ascendant": (70, 200, 120), "immortal": (230, 70, 100), "radiant": (255, 235, 160),
}


def tier_rgb(rank):
    return TIER_RGB.get((rank or "").split(" ")[0].lower(), GRAY)


def valorant_card(profile):
    matches = profile["matches"]
    rows_top = 444
    row_height = 64
    card = Card(rows_top + max(1, len(matches)) * (row_height + 8) + 40,
                top=(16, 20, 30), bottom=(48, 20, 32), family="kr")
    rank_rgb = tier_rgb(profile["rank"])

    # Header: big rank icon, Riot ID, rank and RR bar.
    icon_box = (MARGIN, MARGIN, MARGIN + 180, MARGIN + 180)
    card.panel(icon_box)
    if not card.picture(icon_box, profile.get("rank_icon"), padding=14):
        card.text(MARGIN + 90, MARGIN + 74, "?", 40, GRAY)
    left = MARGIN + 180 + GAP
    card.panel((left, MARGIN, WIDTH - MARGIN, MARGIN + 180))
    inner = WIDTH - MARGIN - left - 40
    riot_id, size = card.fit(f"{profile['name']}§7#{profile['tag']}", 32, inner - 70)
    card.text(left + 20, 34, riot_id, size, align="left")
    card.text(WIDTH - MARGIN - 20, 40, f"§7{profile['region'].upper()}", 20, align="right")
    card.text(left + 20, 86, profile["rank"], 30, rank_rgb, align="left")

    bar = (left + 20, 136, WIDTH - MARGIN - 20, 152)
    card.rect(bar, (255, 255, 255, 40))
    rr = profile.get("rr")
    if rr is not None:
        filled = bar[0] + (bar[2] - bar[0]) * max(0, min(rr, 100)) / 100
        if filled > bar[0]:
            card.rect((bar[0], bar[1], round(filled), bar[3]), rank_rgb + (255,))
        rr_text = f"{rr} RR"
        change = profile.get("rr_change")
        if change is not None:
            rr_text += f"  §{'a' if change >= 0 else 'c'}{int(change):+d}"
        card.text(left + 20, 160, rr_text, 18, align="left")

    # Peak rank.
    peak_box = (MARGIN, 208, WIDTH - MARGIN, 268)
    card.panel(peak_box)
    peak = profile.get("peak") or "-"
    has_icon = card.picture((MARGIN + 8, 212, MARGIN + 60, 264), profile.get("peak_icon"), padding=2)
    text_x = MARGIN + (72 if has_icon else 20)
    card.text(text_x, 226, "§7최고 랭크", 20, align="left")
    season = f" §7({profile['peak_season']})" if profile.get("peak") and profile.get("peak_season") else ""
    card.text(text_x + 130, 226, peak + season, 22, tier_rgb(peak), align="left")

    # Averages over recent matches.
    totals = profile.get("totals")
    if totals:
        card.text(MARGIN + 4, 282, f"§7최근 {totals['games']}경기 · 데스매치 제외", 18, align="left")
        card.stat_row(308, 90, [
            (f"승률 {totals['record']}".strip(), totals["win_rate"], WIN_GREEN),
            ("K/D", totals["kd"], VALORANT_RED),
            ("평균 K/D/A", totals["kda"], WHITE),
            ("헤드샷", totals["headshot"], GOLD),
            ("ADR", totals["adr"], AQUA),
        ])
    else:
        card.panel((MARGIN, 308, WIDTH - MARGIN, 398))
        card.text(WIDTH / 2, 340, "§7집계할 경기 기록이 없습니다", 22)

    # Recent matches.
    card.text(MARGIN + 4, 414, "§7최근 경기", 18, align="left")
    if not matches:
        card.panel((MARGIN, rows_top, WIDTH - MARGIN, rows_top + row_height))
        card.text(WIDTH / 2, rows_top + 20, "§7기록이 없습니다", 22)
    for i, match in enumerate(matches):
        y = rows_top + i * (row_height + 8)
        card.panel((MARGIN, y, WIDTH - MARGIN, y + row_height))
        won = match.get("won")
        accent = WIN_GREEN if won else VALORANT_RED if won is False else GRAY
        card.rect((MARGIN + 4, y + 8, MARGIN + 10, y + row_height - 8), accent + (255,), radius=3)
        icon = (MARGIN + 18, y + 6, MARGIN + 18 + 52, y + row_height - 6)
        has_agent = card.picture(icon, profile.get("agent_icons", {}).get(match.get("agent_id")), padding=0)
        x = icon[2] + 12 if has_agent else MARGIN + 24
        agent, agent_size = card.fit(match["agent"], 22, 330 - x)
        card.text(x, y + 10, agent, agent_size, align="left")
        where, where_size = card.fit(f"§7{match['map']} · {match['mode']}", 16, 330 - x)
        card.text(x, y + 38, where, where_size, align="left")
        card.text(420, y + 20, match["result"], 22, accent)
        card.text(590, y + 20, match["kda"], 22)
        card.text(WIDTH - MARGIN - 20, y + 10, "§7K/D", 14, align="right")
        card.text(WIDTH - MARGIN - 20, y + 30, match["kd"], 22,
                  VALORANT_RED if float(match["kd"]) < 1 else WIN_GREEN, align="right")

    card.footer(card.image.height - 30, "§7HenrikDev API · Minecraft Uptime Bot")
    return card.render()
