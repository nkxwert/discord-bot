"""Renders Hypixel stat cards as PNG images with Pillow."""
import io
from datetime import timedelta, timezone
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONT_PATH = Path(__file__).parent / "assets" / "fonts" / "Monocraft.ttf"
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


def font(size):
    if size not in _fonts:
        _fonts[size] = ImageFont.truetype(str(FONT_PATH), size)
    return _fonts[size]


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


def fit(text, size, max_width):
    """Shrink the font (down to 14px) and then truncate so plain text fits max_width."""
    while size > 14 and font(size).getlength(plain(text)) > max_width:
        size -= 2
    if font(size).getlength(plain(text)) > max_width:
        while text and font(size).getlength(plain(text) + "...") > max_width:
            text = text[:-1]
        text += "..."
    return text, size


def draw_text(draw, x, y, text, size, default=WHITE, align="center"):
    """Draw §-coloured text with a Minecraft-style drop shadow. y is the top edge."""
    f = font(size)
    runs = color_segments(text, default)
    width = sum(f.getlength(run) for run, _ in runs)
    if align == "center":
        x -= width / 2
    shadow = max(2, size // 12)
    for run, rgb in runs:
        draw.text((x + shadow, y + shadow), run, font=f, fill=tuple(c // 4 for c in rgb))
        draw.text((x, y), run, font=f, fill=rgb)
        x += f.getlength(run)


class Card:
    def __init__(self, height):
        self.image = Image.new("RGBA", (WIDTH, height))
        top, bottom = (18, 28, 46), (14, 58, 70)
        pixels = ImageDraw.Draw(self.image)
        for y in range(height):
            t = y / max(1, height - 1)
            pixels.line([(0, y), (WIDTH, y)], fill=tuple(round(a + (b - a) * t) for a, b in zip(top, bottom)))
        self.overlay = Image.new("RGBA", self.image.size, (0, 0, 0, 0))
        self.panels = ImageDraw.Draw(self.overlay)
        self.texts = []

    def panel(self, box):
        self.panels.rounded_rectangle(box, radius=10, fill=(0, 0, 0, 120), outline=(255, 255, 255, 45), width=2)

    def text(self, *args, **kwargs):
        self.texts.append((args, kwargs))

    def stat(self, box, label, value, rgb):
        self.panel(box)
        x0, y0, x1, y1 = box
        cx = (x0 + x1) / 2
        label, label_size = fit(label, 20, x1 - x0 - 16)
        value, value_size = fit(value if isinstance(value, str) else f"{value:,}", 32, x1 - x0 - 16)
        self.text(cx, y0 + 14, label, label_size, rgb)
        self.text(cx, y1 - value_size - 16, value, value_size, rgb)

    def stat_row(self, y, height, cells):
        cell_width = (WIDTH - 2 * MARGIN - GAP * (len(cells) - 1)) / len(cells)
        for i, (label, value, rgb) in enumerate(cells):
            x0 = MARGIN + i * (cell_width + GAP)
            self.stat((round(x0), y, round(x0 + cell_width), y + height), label, value, rgb)

    def skin(self, box, data):
        self.panel(box)
        if not data:
            x0, y0, x1, y1 = box
            self.text((x0 + x1) / 2, (y0 + y1) / 2 - 20, "?", 40, GRAY)
            return
        try:
            skin = Image.open(io.BytesIO(data)).convert("RGBA")
        except Exception:
            return
        x0, y0, x1, y1 = box
        scale = min((x1 - x0 - 16) / skin.width, (y1 - y0 - 16) / skin.height)
        size = (max(1, round(skin.width * scale)), max(1, round(skin.height * scale)))
        skin = skin.resize(size, Image.NEAREST if scale >= 1 else Image.LANCZOS)
        self.overlay.alpha_composite(skin, (round((x0 + x1 - size[0]) / 2), round((y0 + y1 - size[1]) / 2)))

    def footer(self, y):
        self.text(WIDTH / 2, y, "§7Minecraft Uptime Bot", 16)

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
    name, size = fit(profile["display_name"], 34, WIDTH - MARGIN - right - 24)
    card.panel((right, y, WIDTH - MARGIN, y + 64))
    card.text((right + WIDTH - MARGIN) / 2, y + 32 - size / 2 - 4, name, size)
    return right


def hypixel_card(profile):
    card = Card(800)
    right = header(card, profile)
    mid = right + (WIDTH - MARGIN - right - GAP) // 2

    guild = profile.get("guild")
    guild_name, guild_size = fit(f"§7Guild: §a{guild['name'] if guild else 'None'}", 20, mid - right - 24)
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
