"""Quizzes where the first right answer scores a point: 국기 맞히기 (a flag -> its country),
수도 맞히기 (a country -> its capital) and 지도 맞히기 (a map or silhouette -> its country).

Pure logic; bot.py keeps the quizzes (one of each kind per channel) and the counts. Country names, matching
and the well-known list come from nara.py; the flag pictures are assets/flags/<code>.png (flagcdn.com, public
domain); capitals and their accepted spellings are in assets/countries.json ('capitals', 'capital_names')."""
import json
import random
from functools import lru_cache
from pathlib import Path

import nara

FLAG_DIR = Path(__file__).parent / "assets" / "flags"
EASY, NORMAL, HARD = "easy", "normal", "hard"
LEVEL_NAMES = {EASY: "쉬움", NORMAL: "보통", HARD: "어려움"}
LEVEL_WORDS = {"쉬움": EASY, "쉽게": EASY, "easy": EASY, "보통": NORMAL, "normal": NORMAL,
               "어려움": HARD, "어렵게": HARD, "hard": HARD, "하드": HARD}
DEFAULT_COUNT = 10
MAX_COUNT = 50
LOOKALIKE_WEIGHT = 3  # in 어려움, flags easily mixed up with another come up this many times as often

# Flags that look alike; 어려움 asks for these more often.
LOOKALIKES = [
    {"TCD", "ROU", "AND", "MDA"}, {"IDN", "MCO", "POL", "SGP"}, {"IRL", "CIV", "ITA"},
    {"NLD", "LUX", "PRY", "HRV"}, {"AUS", "NZL"}, {"SEN", "MLI", "GIN", "CMR"}, {"SVN", "SVK", "RUS", "SRB"},
    {"NOR", "ISL", "DNK", "FIN", "SWE"}, {"HTI", "LIE"}, {"ECU", "COL", "VEN"},
    {"JOR", "PSE", "SDN", "ARE", "KWT"}, {"EGY", "SYR", "IRQ", "YEM"}, {"HND", "NIC", "SLV", "GTM"},
    {"GHA", "BOL", "LTU", "ETH"}, {"BEL", "DEU"}, {"QAT", "BHR"}, {"NER", "IND"}, {"AUT", "LVA", "LBN"},
    {"GNB", "BFA", "GHA"}, {"KIR", "TUV", "FJI"}, {"BEN", "TGO"}, {"LBR", "USA", "MYS"},
]
_LOOKALIKE = set().union(*LOOKALIKES)


def level_of(word):
    return LEVEL_WORDS.get(word.strip().lower())


def parse_start(text):
    """(level, count) from what follows !국기시작, in any order: '어려움 20', '5', ''. None if not understood."""
    level, count = NORMAL, DEFAULT_COUNT
    for word in text.split():
        if word.isdigit():
            count = int(word)
            if not 1 <= count <= MAX_COUNT:
                return None
        elif level_of(word):
            level = level_of(word)
        else:
            return None
    return level, count


FLAG, CAPITAL, MAP, ANIME, SONG = "flag", "capital", "map", "anime", "song"
SONG_PATH = Path(__file__).parent / "assets" / "songs.json"
SONG_SECONDS = {EASY: 10, NORMAL: 5, HARD: 2}  # how long the clip of 노래 맞히기 is, by difficulty
SONG_GENRES = {"kpop": "K-POP", "jpop": "J-POP·애니", "pop": "팝송", "custom": "이 서버 추가곡"}
SONG_GENRE_WORDS = {"케이팝": "kpop", "kpop": "kpop", "k-pop": "kpop", "제이팝": "jpop", "jpop": "jpop", "j-pop": "jpop",
                    "애니": "jpop", "팝": "pop", "팝송": "pop", "pop": "pop", "추가곡": "custom", "서버": "custom"}
ANIME_PATH = Path(__file__).parent / "assets" / "anime.json"
MAP_DIR = Path(__file__).parent / "assets" / "maps"
MAP_MIN_HARD_AREA = 2500  # km²; smaller countries are dots or specks as a silhouette, so 어려움 skips them


ANIME_MIN_EASY = 5  # with fewer ★ characters in a server's chosen works, 쉬움 uses all of their characters


def pool(level, kind=FLAG, works=None):
    if kind == ANIME:  # 쉬움: the best-known characters (★); 보통: all of them. works: the server's choice (None: all)
        chosen = [cid for cid, c in anime().items() if works is None or c["work"] in works]
        if level == EASY:
            stars = [cid for cid in chosen if anime()[cid]["star"]]
            chosen = stars if len(stars) >= ANIME_MIN_EASY else chosen
        return sorted(chosen) or sorted(anime())
    codes = sorted(nara.countries())
    if level == EASY:
        return [c for c in codes if c in nara.FAMOUS]
    if level == HARD:
        hard = [c for c in codes if c not in nara.FAMOUS]
        if kind == MAP:
            return [c for c in hard if nara.countries()[c]["area"] >= MAP_MIN_HARD_AREA]
        if kind == FLAG:
            return hard + [c for c in hard if c in _LOOKALIKE] * (LOOKALIKE_WEIGHT - 1)
        return hard
    return codes


def pick(level, recent=(), kind=FLAG, works=None, rng=random):
    """A country (or character) for the next question, avoiding the ones asked recently in this quiz."""
    choices = [c for c in pool(level, kind, works) if c not in recent] or pool(level, kind, works)
    return rng.choice(choices)


@lru_cache(maxsize=1)
def _capital_index():
    """normalized capital name -> codes of the countries it is a capital of"""
    index = {}
    for code, c in nara.countries().items():
        for name in (*c.get("capitals", [c["capital"]]), *c.get("capital_names", [])):
            index.setdefault(nara.normalize(name), set()).add(code)
    return index


def capital_codes(text):
    """The countries whose capital the text names exactly (spaces and punctuation aside)."""
    return _capital_index().get(nara.normalize(text), set())


def capital_text(code):
    """'프리토리아 (케이프타운·블룸폰테인도 정답)' for a country with several capitals, else just the capital."""
    c = nara.countries()[code]
    capitals = c.get("capitals", [c["capital"]])
    return capitals[0] + (f" ({'·'.join(capitals[1:])}도 정답)" if len(capitals) > 1 else "")


def flag_path(code):
    return FLAG_DIR / f"{code}.png"


def map_path(code, level):
    """The location map (the country in red among its neighbours), or for 어려움 its silhouette alone.
    Drawn beforehand from Natural Earth (public domain); see assets/maps/SOURCES.md."""
    return MAP_DIR / (f"{code}_s.png" if level == HARD else f"{code}.png")


CHOSUNG = "ㄱㄲㄴㄷㄸㄹㅁㅂㅃㅅㅆㅇㅈㅉㅊㅋㅌㅍㅎ"


def chosung(text):
    """'프랑스' -> 'ㅍㄹㅅ'; spaces and other letters are kept."""
    out = []
    for ch in text:
        code = ord(ch) - 0xAC00
        out.append(CHOSUNG[code // 588] if 0 <= code < 11172 else ch)
    return "".join(out)


# ------------------------------------------------------------------ 애니캐 맞히기

@lru_cache(maxsize=1)
def anime():
    """AniList character id -> {ko, work, star, names, image}. The pictures are not kept here: the bot
    downloads one from AniList for each question (see assets/anime-SOURCES.md)."""
    with open(ANIME_PATH, encoding="utf-8") as f:
        return {c["id"]: c for c in json.load(f)}


@lru_cache(maxsize=1)
def _anime_index():
    index = {}
    for cid, c in anime().items():
        for name in c["names"]:
            index.setdefault(nara.normalize(name), set()).add(cid)
    return index


def anime_codes(text):
    """The characters this name belongs to: the Korean name, the given name alone, or the English/Japanese name."""
    return _anime_index().get(nara.normalize(text), set())


def anime_works():
    """[{title, stars, chars}] in list order, for the 작품 고르기 page."""
    works = {}
    for c in anime().values():
        w = works.setdefault(c["work"], {"title": c["work"], "stars": 0, "chars": []})
        w["chars"].append(c["ko"])
        w["stars"] += c["star"]
    return list(works.values())


# ------------------------------------------------------------------ 노래 맞히기

@lru_cache(maxsize=1)
def songs():
    """Deezer track id ("dz…") -> {genre, title, artist, names, cover}: the built-in list. Only names are kept;
    the 30-second official preview is fetched from Deezer for each question (see assets/songs-SOURCES.md)."""
    with open(SONG_PATH, encoding="utf-8") as f:
        return {s["id"]: s for s in json.load(f)}


@lru_cache(maxsize=1)
def _song_index():
    index = {}
    for sid, s in songs().items():
        for name in s["names"]:
            index.setdefault(nara.normalize(name), set()).add(sid)
    return index


def song_codes(text, extra=()):
    """Songs whose title (or another accepted name) this is; extra: (id, names) of a server's added songs."""
    key = nara.normalize(text)
    found = set(_song_index().get(key, set()))
    found |= {sid for sid, names in extra if key in {nara.normalize(n) for n in names}}
    return found


def split_song_options(text):
    """(genre or None, the rest) from what follows !노래시작: '케이팝 어려움 20' -> ('kpop', '어려움 20')."""
    genre, rest = None, []
    for word in text.split():
        if word.lower() in SONG_GENRE_WORDS:
            genre = SONG_GENRE_WORDS[word.lower()]
        else:
            rest.append(word)
    return genre, " ".join(rest)


def song_names(title, *extra):
    """Accepted names for a song: its title, the title without (feat. …)/[…] parts, and any extras."""
    import re
    clean = re.sub(r"\s*[\(\[].*?[\)\]]", "", title).strip()
    return sorted({n.strip() for n in (title, clean, *extra) if n and n.strip()})
