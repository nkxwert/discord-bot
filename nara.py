"""나라 맞히기: guess a secret country; every guess shows how it compares with the answer.

Pure game logic and the board picture; bot.py keeps the games (one per channel) and the scores.
Country data: assets/countries.json (197 countries; see assets/countries-SOURCES.md), about 50KB in memory."""
import io
import json
import math
import random
import re
import unicodedata
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw

import cards

DATA_PATH = Path(__file__).parent / "assets" / "countries.json"
CLOSE_RATIO = 0.25  # population/area within ±25% counts as close (yellow)
NEAR_KM = 1500  # capitals this close count as near (yellow distance)
BOARD_ROWS = 15  # guesses drawn on the board; older ones are summarised in one line

# Difficulty. 쉬움: the answer is a well-known country (FAMOUS); 보통: any country. Both show every hint exactly.
# 어려움: the answer is one of the other, lesser-known countries; population and area say only
# bigger/smaller, distance only near/far (HARD_NEAR_KM), and there are HARD_MAX_GUESSES guesses.
# and the direction is hidden. Continent, hemisphere and landlocked stay.
EASY, NORMAL, HARD = "easy", "normal", "hard"
LEVEL_NAMES = {EASY: "쉬움", NORMAL: "보통", HARD: "어려움"}
LEVEL_WORDS = {"": NORMAL, "보통": NORMAL, "normal": NORMAL, "쉬움": EASY, "쉽게": EASY, "easy": EASY,
               "어려움": HARD, "hard": HARD, "어렵게": HARD, "하드": HARD}
HARD_MAX_GUESSES = 10
HARD_NEAR_KM = 2000
FAMOUS = {
    # 아시아
    "KOR", "JPN", "CHN", "TWN", "PRK", "MNG", "VNM", "THA", "PHL", "IDN", "MYS", "SGP", "IND", "PAK", "BGD",
    "NPL", "KHM", "LKA", "KAZ", "UZB", "AFG",
    # 아시아 (서아시아)
    "TUR", "IRN", "IRQ", "SAU", "ARE", "ISR", "QAT", "SYR",
    # 유럽
    "GBR", "FRA", "DEU", "ITA", "ESP", "PRT", "NLD", "BEL", "CHE", "AUT", "SWE", "NOR", "DNK", "FIN", "ISL",
    "IRL", "POL", "CZE", "HUN", "GRC", "RUS", "UKR", "ROU", "HRV",
    # 아프리카
    "EGY", "ZAF", "NGA", "KEN", "ETH", "MAR", "GHA", "DZA", "TZA", "MDG",
    # 아메리카
    "USA", "CAN", "MEX", "CUB", "JAM", "BRA", "ARG", "CHL", "COL", "PER", "VEN", "URY", "BOL", "ECU",
    # 오세아니아
    "AUS", "NZL", "FJI",
}


@lru_cache(maxsize=1)
def countries():
    with open(DATA_PATH, encoding="utf-8") as f:
        data = {c["code"]: c for c in json.load(f)}
    if not has_info(data):
        print("assets/countries.json이 예전 버전입니다: !정보와 국기 이모지가 빠집니다. 새 파일로 바꿔 주세요.")
    return data


def has_info(data=None):
    """False for the countries.json from before !정보 (no capital, languages, flag, ...)."""
    return all("capital" in c for c in (data or countries()).values())


def normalize(text):
    """Lower case, accents and spaces/punctuation removed: 'Côte d’Ivoire' -> 'cotedivoire'."""
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return unicodedata.normalize("NFC", re.sub(r"[\s.\-'’‘`,()·]", "", text))


@lru_cache(maxsize=1)
def _index():
    index = {}
    for code, c in countries().items():
        for name in (c["ko"], *c["names"]):
            index.setdefault(normalize(name), code)
    return index


def find(text):
    """The country with this name (Korean, English, common aliases or code), or None."""
    return countries().get(_index().get(normalize(text)))


def _jamo(text):
    """Hangul split into letters, so '프랑스' and '프랑서' are judged nearly the same."""
    out = []
    for ch in text:
        code = ord(ch) - 0xAC00
        if 0 <= code < 11172:
            out += [chr(0x1100 + code // 588), chr(0x1161 + code % 588 // 28)]
            if code % 28:
                out.append(chr(0x11A7 + code % 28))
        else:
            out.append(ch)
    return "".join(out)


def suggest(text, limit=3):
    """Korean names of the countries whose names look most like the text (for typos)."""
    target = _jamo(normalize(text))
    if not target:
        return []
    best = {}
    for key, code in _index().items():
        score = SequenceMatcher(None, target, _jamo(key)).ratio()
        if key.startswith(normalize(text)) or normalize(text) in key:
            score = max(score, 0.75)
        if score > best.get(code, 0):
            best[code] = score
    ranked = sorted((s, code) for code, s in best.items() if s >= 0.6)[::-1]
    # Only names nearly as close as the best one: '프량스' -> 프랑스, not 키프로스 as well.
    return [countries()[code]["ko"] for s, code in ranked[:limit] if s >= ranked[0][0] - 0.08]


def josa(word, with_final, without_final):
    """The particle that fits the word: josa('프랑스', '은', '는') -> '는'"""
    code = ord(word[-1]) - 0xAC00 if word else -1
    return with_final if 0 <= code < 11172 and code % 28 else without_final


def level_of(text):
    """EASY/NORMAL/HARD for what was typed after !나라시작, or None if it isn't a difficulty."""
    return LEVEL_WORDS.get(text.strip().lower())


def max_guesses(level):
    return HARD_MAX_GUESSES if level == HARD else None


def pick(level=NORMAL, rng=random):
    if level == NORMAL:
        pool = list(countries())
    else:
        pool = [code for code in countries() if (code in FAMOUS) == (level == EASY)]
    return rng.choice(sorted(pool))


# ------------------------------------------------------------------ comparing a guess with the answer

def distance_km(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, (a["lat"], a["lon"], b["lat"], b["lon"]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return round(2 * 6371 * math.asin(min(1, math.sqrt(h))))


def direction(a, b):
    """Map direction from a's capital to b's, in degrees (0 = east, 90 = north), going the shorter way
    round the date line; None for the same place."""
    dlat = b["lat"] - a["lat"]
    dlon = (b["lon"] - a["lon"] + 540) % 360 - 180
    if abs(dlat) < 0.01 and abs(dlon) < 0.01:
        return None
    return round(math.degrees(math.atan2(dlat, dlon * math.cos(math.radians((a["lat"] + b["lat"]) / 2)))) / 45) * 45 % 360


def _amount(guess, answer):
    """'same', 'up' (the answer is bigger), 'down', with close=True within ±CLOSE_RATIO."""
    if guess == answer:
        return {"result": "same", "close": True}
    return {"result": "up" if answer > guess else "down", "close": abs(answer - guess) <= CLOSE_RATIO * guess}


def hemisphere(c):
    return "북반구" if c["lat"] >= 0 else "남반구"


def compare(guess_code, answer_code):
    g, a = countries()[guess_code], countries()[answer_code]
    return {
        "code": guess_code,
        "correct": guess_code == answer_code,
        "continent": g["continent"] == a["continent"],
        "population": _amount(g["population"], a["population"]),
        "area": _amount(g["area"], a["area"]),
        "distance": distance_km(g, a),
        "direction": direction(g, a),
        "hemisphere": hemisphere(g) == hemisphere(a),
        "landlocked": g["landlocked"] == a["landlocked"],
    }


# ------------------------------------------------------------------ text

def korean_number(n):
    """5168만, 1억 4351만, 800"""
    n = round(n)
    if n >= 100_000_000:
        eok, man = divmod(n, 100_000_000)
        return f"{eok}억 {man // 10_000}만" if man >= 10_000 else f"{eok}억"
    if n >= 10_000:
        return f"{n // 10_000:,}만"
    return f"{n:,}"


def area_text(km2):
    if km2 >= 10_000:
        return f"{korean_number(km2)}㎢"
    return f"{km2:,.0f}㎢" if km2 >= 10 else f"{km2:g}㎢"


def summary(code):
    """One line about the answer, for when it is revealed."""
    c = countries()[code]
    return (f"{c['continent']} · 인구 {korean_number(c['population'])}명 · 면적 {area_text(c['area'])} · "
            f"{'내륙국' if c['landlocked'] else '바다에 닿은 나라'}")


# ------------------------------------------------------------------ board picture

BG = (43, 45, 49)
PANEL = (30, 31, 34)
GREEN_T = (59, 165, 93)
YELLOW_T = (201, 160, 40)
RED_T = (165, 60, 60)
GRAY_T = (78, 80, 88)
WHITE = (242, 243, 245)
MUTED = (148, 155, 164)
COLUMNS = [("나라", 190), ("대륙", 112), ("인구", 130), ("면적", 130), ("거리", 120), ("방향", 76), ("반구", 92),
           ("내륙국", 92)]
CELL_H, GAP, PAD = 56, 6, 20


def _tile(draw, x, y, w, color, text=None, size=20, sub=None):
    draw.rounded_rectangle((x, y, x + w, y + CELL_H), radius=8, fill=color)
    if text is not None:
        face = cards.font(size, "kr")
        cy = y + CELL_H / 2 - (8 if sub else 0)
        draw.text((x + w / 2, cy), text, font=face, fill=WHITE, anchor="mm")
        if sub:
            draw.text((x + w / 2, y + CELL_H / 2 + 13), sub, font=cards.font(11, "kr"), fill=WHITE, anchor="mm")


def _fit(text, size, width):
    while size > 11 and cards.font(size, "kr").getlength(text) > width:
        size -= 1
    return size


def _arrow(draw, cx, cy, angle, length=34, color=WHITE):
    """An arrow pointing at angle degrees (0 = east, 90 = north)."""
    rad = math.radians(angle)
    dx, dy = math.cos(rad), -math.sin(rad)
    tail = (cx - dx * length / 2, cy - dy * length / 2)
    tip = (cx + dx * length / 2, cy + dy * length / 2)
    base = (tip[0] - dx * 13, tip[1] - dy * 13)
    draw.line((tail, base), fill=color, width=5)
    draw.polygon([tip, (base[0] - dy * 9, base[1] + dx * 9), (base[0] + dy * 9, base[1] - dx * 9)], fill=color)


def _amount_tile(draw, x, y, w, info, value_text, level):
    if info["result"] == "same":
        _tile(draw, x, y, w, GREEN_T, value_text, _fit(value_text, 18, w - 12))
        return
    color = YELLOW_T if info["close"] and level != HARD else GRAY_T
    mark = "▲" if info["result"] == "up" else "▼"
    _tile(draw, x, y, w, color, f"{value_text} {mark}", _fit(f"{value_text} {mark}", 18, w - 12))


def render_board(rows, answer=None, total=None, level=NORMAL):
    """PNG of the guesses so far (each from compare()), newest last. answer: code to show at the bottom when
    the game has ended. total: how many guesses there were (more than the rows drawn when long).
    level: HARD shows fewer details (see the top of this file)."""
    total = total or len(rows)
    hidden = total - len(rows)
    width = PAD * 2 + sum(w for _, w in COLUMNS) + GAP * (len(COLUMNS) - 1)
    head_h = 70
    extra = 34 if hidden else 0
    foot = 64 if answer else 0
    legend_h = 30
    height = PAD + head_h + 30 + extra + max(1, len(rows)) * (CELL_H + GAP) + foot + legend_h + PAD
    image = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(image)
    draw.text((PAD, PAD + 4), "나라 맞히기", font=cards.font(33, "kr"), fill=WHITE)
    title_w = cards.font(33, "kr").getlength("나라 맞히기")
    badge = LEVEL_NAMES[level]
    badge_w = cards.font(18, "kr").getlength(badge) + 24
    bx = PAD + title_w + 14
    draw.rounded_rectangle((bx, PAD + 8, bx + badge_w, PAD + 40), radius=8, fill={EASY: GREEN_T, NORMAL: (88, 101, 242), HARD: RED_T}[level])
    draw.text((bx + badge_w / 2, PAD + 24), badge, font=cards.font(18, "kr"), fill=WHITE, anchor="mm")
    count = f"추측 {total}/{HARD_MAX_GUESSES}" if level == HARD else f"추측 {total}번"
    draw.text((width - PAD, PAD + 14), count, font=cards.font(22, "kr"), fill=MUTED, anchor="ra")

    y = PAD + head_h
    x = PAD
    for title, w in COLUMNS:
        draw.text((x + w / 2, y + 10), title, font=cards.font(16, "kr"), fill=MUTED, anchor="mm")
        x += w + GAP
    y += 30
    if hidden:
        draw.text((width / 2, y + 12), f"··· 앞의 추측 {hidden}개는 생략 ···", font=cards.font(14, "kr"), fill=MUTED, anchor="mm")
        y += extra
    if not rows:
        draw.rounded_rectangle((PAD, y, width - PAD, y + CELL_H), radius=8, fill=PANEL)
        draw.text((width / 2, y + CELL_H / 2), "!추측 <나라 이름> 으로 첫 추측을 해 보세요", font=cards.font(18, "kr"),
                  fill=MUTED, anchor="mm")
        y += CELL_H + GAP

    data = countries()
    for row in rows:
        c = data[row["code"]]
        x = PAD
        cells = iter(w for _, w in COLUMNS)
        w = next(cells)
        _tile(draw, x, y, w, GREEN_T if row["correct"] else PANEL, c["ko"], _fit(c["ko"], 20, w - 14))
        x += w + GAP
        w = next(cells)
        _tile(draw, x, y, w, GREEN_T if row["continent"] else RED_T, c["continent"], _fit(c["continent"], 18, w - 10))
        x += w + GAP
        w = next(cells)
        _amount_tile(draw, x, y, w, row["population"], korean_number(c["population"]), level)
        x += w + GAP
        w = next(cells)
        _amount_tile(draw, x, y, w, row["area"], area_text(c["area"]), level)
        x += w + GAP
        w = next(cells)
        km = row["distance"]
        near = km <= (HARD_NEAR_KM if level == HARD else NEAR_KM)
        text = "정답" if row["correct"] else ("가까움" if near else "멀음") if level == HARD else f"{km:,}km"
        _tile(draw, x, y, w, GREEN_T if row["correct"] else YELLOW_T if near else GRAY_T, text, _fit(text, 18, w - 10))
        x += w + GAP
        w = next(cells)
        if row["correct"]:
            _tile(draw, x, y, w, GREEN_T)
            cx, cy = x + w / 2, y + CELL_H / 2
            draw.line(((cx - 12, cy), (cx - 3, cy + 9), (cx + 13, cy - 9)), fill=WHITE, width=5, joint="curve")
        elif level == HARD:
            _tile(draw, x, y, w, PANEL, "?", 22)
        elif row["direction"] is None:
            _tile(draw, x, y, w, GRAY_T, "·", 22)
        else:
            _tile(draw, x, y, w, GRAY_T)
            _arrow(draw, x + w / 2, y + CELL_H / 2, row["direction"])
        x += w + GAP
        w = next(cells)
        hemi = hemisphere(c)
        _tile(draw, x, y, w, GREEN_T if row["hemisphere"] else RED_T, hemi, 18)
        x += w + GAP
        w = next(cells)
        land = "내륙국" if c["landlocked"] else "아님"
        _tile(draw, x, y, w, GREEN_T if row["landlocked"] else RED_T, land, 18)
        y += CELL_H + GAP

    if answer:
        a = data[answer]
        y += 8
        draw.rounded_rectangle((PAD, y, width - PAD, y + 50), radius=8, fill=PANEL)
        text = f"정답: {a['ko']} ({a['en']})"
        draw.text((width / 2, y + 25), text, font=cards.font(_fit(text, 24, width - PAD * 4), "kr"), fill=WHITE, anchor="mm")
        y += 50 + GAP
    if level == HARD:
        legend = (f"▲ 정답이 더 많음/넓음  ▼ 더 적음/좁음  ·  가까움: 수도끼리 {HARD_NEAR_KM:,}km 이내  ·  "
                  f"방향은 숨김  ·  {HARD_MAX_GUESSES}번 안에 맞히기")
    else:
        legend = (f"▲ 정답이 더 많음/넓음  ▼ 더 적음/좁음  ·  노란색: 인구·면적 ±{round(CLOSE_RATIO * 100)}% 이내, "
                  f"거리 {NEAR_KM:,}km 이내  ·  거리·방향은 수도 기준")
    draw.text((width / 2, y + 16), legend, font=cards.font(_fit(legend, 14, width - PAD * 2), "kr"), fill=MUTED, anchor="mm")

    out = io.BytesIO()
    image.save(out, "PNG", optimize=True)
    image.close()
    return out.getvalue()


# ------------------------------------------------------------------ !정보

KOREA_AREA = 100_210
OUTSIDE_NAMES = {"HKG": "홍콩", "MAC": "마카오", "ESH": "서사하라", "GUF": "프랑스령 기아나", "GIB": "지브롤터"}


def area_compare(km2):
    """'한국의 약 10배' / '한국의 약 1/5' / '한국과 비슷'"""
    ratio = km2 / KOREA_AREA
    if 0.8 <= ratio <= 1.25:
        return "한국과 비슷"
    if ratio > 1:
        return f"한국의 약 {ratio:,.0f}배" if ratio >= 10 else f"한국의 약 {ratio:.1f}배"
    return f"한국의 약 1/{1 / ratio:,.0f}"


def border_names(c):
    data = countries()
    return [data[b]["ko"] if b in data else OUTSIDE_NAMES.get(b, b) for b in c["borders"]]


def short_list(items, limit=4):
    if not items:
        return "정보 없음"
    shown = ", ".join(items[:limit])
    return f"{shown} 외 {len(items) - limit}개" if len(items) > limit else shown


def intro(extract, limit=420):
    """The first few sentences of a Wikipedia summary, cut at a sentence end within limit characters."""
    text = " ".join((extract or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("다. "))
    return cut[:end + 1] if end > limit // 3 else cut.rstrip() + "…"


def info_fields(c):
    """[(name, value)] for the !정보 embed."""
    density = c["population"] / c["area"] if c["area"] else 0
    return [
        ("대륙", c["continent"]),
        ("수도", c["capital"]),
        ("인구", f"약 {korean_number(c['population'])}명"),
        ("면적", f"{c['area']:,.0f}㎢" if c["code"] == "KOR" else
                 f"{c['area']:,.0f}㎢\n({area_compare(c['area'])})" if c["area"] >= 10 else
                 f"{c['area']:g}㎢\n({area_compare(c['area'])})"),
        ("인구 밀도", f"{density:,.0f}명/㎢"),
        ("바다", "없음 (내륙국)" if c["landlocked"] else "접함"),
        ("언어", short_list(c["languages"])),
        ("통화", short_list(c["currencies"], 3)),
        ("이웃 나라", short_list(border_names(c), 8) if c["borders"] else "없음 (육지 국경 없음)"),
    ]
