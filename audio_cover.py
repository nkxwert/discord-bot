"""The still picture of a song's player: the song's own album art when it has one, else Maco and the title.
Drawn once per upload (768x432, a few tens of KB) and dropped right after."""
import os
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

import cards
import textcon

WIDTH, HEIGHT = 768, 432
AVATAR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "images", "avatar.png")
SAGE_TOP, SAGE_BOTTOM = (223, 233, 223), (143, 172, 148)
INK = (43, 53, 48)


@lru_cache(maxsize=4)
def _font(size, family):
    if family == "main":
        return ImageFont.truetype(str(textcon.FONT_PATH), size)
    return cards.font(size, "kr")  # Galmuri: kana, symbols and the like that Black Han Sans lacks


@lru_cache(maxsize=8)
def _blank(size, family):
    mask = _font(size, family).getmask("\U0010FFFD")
    return mask.size, bytes(mask)


@lru_cache(maxsize=2048)
def _has(size, family, ch):
    """Whether the font draws this letter (checked once per letter: drawing a cover used to redo it
    hundreds of times while wrapping the title, which took seconds)."""
    if ch.isspace():
        return True
    mask = _font(size, family).getmask(ch)
    return bool(mask.getbbox()) and (mask.size, bytes(mask)) != _blank(size, family)


def _runs(text, size):
    """[(piece, font)]: Black Han Sans where it has the letter, Galmuri otherwise; letters neither has are left out."""
    main, other = _font(size, "main"), _font(size, "other")
    runs = []
    for ch in text:
        face = main if _has(size, "main", ch) else other if _has(size, "other", ch) else None
        if face is None:
            continue
        if runs and runs[-1][1] is face:
            runs[-1][0] += ch
        else:
            runs.append([ch, face])
    return runs


def _width(text, size):
    return sum(face.getlength(piece) for piece, face in _runs(text, size))


def _wrap(text, size, max_width, max_lines):
    """Lines no wider than max_width (breaking between words, or anywhere in a long word); '…' if cut."""
    lines, line = [], ""
    for ch in text:
        if _width(line + ch, size) <= max_width:
            line += ch
            continue
        cut = line.rfind(" ")
        if cut > 0 and ch != " ":
            lines.append(line[:cut])
            line = line[cut + 1:] + ch
        else:
            lines.append(line)
            line = "" if ch == " " else ch
    if line:
        lines.append(line)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        while lines[-1] and _width(lines[-1] + "…", size) > max_width:
            lines[-1] = lines[-1][:-1]
        lines[-1] += "…"
    return [l.strip() for l in lines]


def _draw_text(draw, x, y, text, size, fill):
    for piece, face in _runs(text, size):
        draw.text((x, y), piece, font=face, fill=fill)
        x += face.getlength(piece)


def _rounded(image, radius):
    mask = Image.new("L", image.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, image.width - 1, image.height - 1), radius, fill=255)
    out = image.convert("RGBA")
    out.putalpha(mask)
    return out


def render(title, art_path, out_path):
    """Save the cover to out_path. art_path: the song's album art (any image file) or None."""
    art = None
    if art_path:
        try:
            art = Image.open(art_path).convert("RGB")
        except Exception:
            art = None
    if art:
        # The art itself on the left, a blurred and darkened copy of it filling the background.
        image = ImageOps.fit(art, (WIDTH, HEIGHT)).filter(ImageFilter.GaussianBlur(28))
        image = Image.blend(image, Image.new("RGB", image.size, (0, 0, 0)), 0.45)
        side = 320
        picture = _rounded(ImageOps.fit(art, (side, side)), 18)
        shadow = Image.new("RGBA", (side + 40, side + 40), (0, 0, 0, 0))
        ImageDraw.Draw(shadow).rounded_rectangle((20, 24, side + 20, side + 24), 18, fill=(0, 0, 0, 120))
        image = image.convert("RGBA")
        image.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(10)), (36, (HEIGHT - side) // 2 - 20))
        image.alpha_composite(picture, (56, (HEIGHT - side) // 2))
        text_x, colour, faint = 56 + side + 36, (255, 255, 255), (220, 220, 220)
    else:
        # Sage background like the site, Maco on the left.
        image = Image.new("RGB", (WIDTH, HEIGHT))
        draw = ImageDraw.Draw(image)
        for y in range(HEIGHT):
            t = y / (HEIGHT - 1)
            draw.line([(0, y), (WIDTH, y)], fill=tuple(round(a + (b - a) * t) for a, b in zip(SAGE_TOP, SAGE_BOTTOM)))
        image = image.convert("RGBA")
        try:
            avatar = Image.open(AVATAR).convert("RGBA").resize((300, 300), Image.LANCZOS)
            circle = Image.new("RGBA", (316, 316), (255, 255, 255, 0))
            ImageDraw.Draw(circle).ellipse((0, 0, 315, 315), fill=(255, 255, 255, 170))
            image.alpha_composite(circle, (40, (HEIGHT - 316) // 2))
            image.alpha_composite(avatar, (48, (HEIGHT - 300) // 2))
        except OSError:
            pass
        text_x, colour, faint = 48 + 300 + 40, INK, (79, 107, 86)
    draw = ImageDraw.Draw(image)
    max_width = WIDTH - text_x - 36
    # Letters neither font has (kanji, emoji ...) are left out; tidy what that leaves at the ends.
    title = "".join(ch for ch in " ".join(title.split()) if _has(40, "main", ch) or _has(40, "other", ch))
    title = " ".join(title.split()).strip(" -_·|~.,()[]") or "노래"
    lines = _wrap(title, 40, max_width, 3)
    block = 34 + 14 + len(lines) * 52
    y = (HEIGHT - block) // 2
    _draw_text(draw, text_x, y, "♪ 노래", 26, faint)
    y += 34 + 14
    for line in lines:
        _draw_text(draw, text_x, y, line, 40, colour)
        y += 52
    image.convert("RGB").save(out_path, "PNG", compress_level=6)
