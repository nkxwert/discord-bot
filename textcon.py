"""글자콘: text drawn as a sticker-like picture (thick white letters, black outline, transparent background),
like the text emoticons on DC Inside. Used by /글자콘.

Memory: one font size is ever loaded (letters are drawn at SIZE and the picture is scaled down to fit), and
each picture is a few tens of KB that is dropped right after sending."""
import io
import math
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

import cards

FONT_PATH = Path(__file__).parent / "assets" / "fonts" / "BlackHanSans-Regular.ttf"
SIZE = 120  # drawing size; the picture is scaled to fit MAX_WIDTH x MAX_HEIGHT afterwards
STROKE = 10
MAX_WIDTH, MAX_HEIGHT = 480, 320  # about what Discord shows; bigger shrinks the outline less
MAX_CHARS = 30
LINE_WIDTH = SIZE * 7  # about 7 Korean letters; wider text is wrapped onto several balanced lines
FILL, OUTLINE = (255, 255, 255, 255), (0, 0, 0, 255)

_main = None


def main_font():
    global _main
    if _main is None:
        _main = ImageFont.truetype(str(FONT_PATH), SIZE)
    return _main


def fallback_font():
    """Galmuri (already used by the stat cards) for symbols Black Han Sans lacks, such as ♥."""
    return cards.font(SIZE, "kr")


@lru_cache(maxsize=4)
def _missing_mask(index):
    """What the font draws for a character it doesn't have (its empty box, or nothing)."""
    face = (main_font(), fallback_font())[index]
    mask = face.getmask("\U0010FFFD")
    return mask.size, bytes(mask)


@lru_cache(maxsize=1024)
def _glyph_font(ch):
    """0 = main font, 1 = fallback, None = neither has it (emoji etc.; the character is left out)."""
    if ch.isspace():
        return 0
    for index, face in enumerate((main_font(), fallback_font())):
        mask = face.getmask(ch)
        if mask.getbbox() and (mask.size, bytes(mask)) != _missing_mask(index):
            return index
    return None


def clean(text):
    """The text that can be drawn (spaces collapsed, unknown characters such as emoji dropped).
    Raises ValueError with a message for the user when nothing is left or it is too long."""
    text = " ".join(text.split())
    if len(text) > MAX_CHARS:
        raise ValueError(f"글자는 {MAX_CHARS}자까지 쓸 수 있습니다.")
    text = "".join(ch for ch in text if _glyph_font(ch) is not None).strip()
    if not text:
        raise ValueError("글자콘으로 만들 수 있는 글자가 없습니다. (이모지는 그릴 수 없습니다)")
    return text


def _width(text):
    return sum(face.getlength(run) for run, face in _runs(text))


def wrap(text):
    """Balanced lines no wider than about LINE_WIDTH, breaking at spaces when there are any."""
    total = _width(text)
    if total <= LINE_WIDTH:
        return [text]
    target = total / math.ceil(total / LINE_WIDTH)
    pieces = text.split(" ") if " " in text else list(text)
    joiner = " " if " " in text else ""
    lines, current = [], ""
    for piece in pieces:
        candidate = f"{current}{joiner}{piece}" if current else piece
        if current and _width(candidate) > target * (1.15 if joiner else 1.02):
            lines.append(current)
            current = piece
        else:
            current = candidate
    lines.append(current)
    if joiner and any(_width(line) > LINE_WIDTH * 1.3 for line in lines):  # one very long word
        return wrap(text.replace(" ", ""))
    return lines


def _runs(line):
    """[(text, font)] pieces of a line, split where the font changes."""
    faces = (main_font(), fallback_font())
    runs = []
    for ch in line:
        face = faces[_glyph_font(ch)]
        if runs and runs[-1][1] is face:
            runs[-1][0] += ch
        else:
            runs.append([ch, face])
    return runs


def render(text):
    """PNG bytes of the 글자콘 for already-cleaned text."""
    lines = wrap(text)
    ascent, descent = main_font().getmetrics()
    line_height = ascent + descent
    widths = [_width(line) for line in lines]
    pad = STROKE * 2
    width = int(max(widths)) + pad * 2
    height = line_height * len(lines) + pad * 2
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    for i, line in enumerate(lines):
        x = pad + (width - pad * 2 - widths[i]) / 2
        baseline = pad + i * line_height + ascent
        for run, face in _runs(line):
            draw.text((x, baseline), run, font=face, fill=FILL, stroke_width=STROKE, stroke_fill=OUTLINE, anchor="ls")
            x += face.getlength(run)
    box = image.getbbox()
    if box:
        image = image.crop((max(0, box[0] - 4), max(0, box[1] - 4), min(width, box[2] + 4), min(height, box[3] + 4)))
    scale = min(MAX_WIDTH / image.width, MAX_HEIGHT / image.height)
    if scale < 1:
        image = image.resize((max(1, round(image.width * scale)), max(1, round(image.height * scale))), Image.LANCZOS)
    out = io.BytesIO()
    image.save(out, "PNG", optimize=True)
    image.close()
    return out.getvalue()
