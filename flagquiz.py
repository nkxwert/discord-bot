"""국기 맞히기: a flag is shown, and the first to type its country (!국기 <나라>) gets a point.

Pure logic; bot.py keeps the quiz (one per channel) and the counts. Country names, matching and the
well-known list come from nara.py; the flag pictures are assets/flags/<code>.png (flagcdn.com, public domain)."""
import random
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


def pool(level):
    codes = sorted(nara.countries())
    if level == EASY:
        return [c for c in codes if c in nara.FAMOUS]
    if level == HARD:
        hard = [c for c in codes if c not in nara.FAMOUS]
        return hard + [c for c in hard if c in _LOOKALIKE] * (LOOKALIKE_WEIGHT - 1)
    return codes


def pick(level, recent=(), rng=random):
    """A country for the next question, avoiding the ones asked recently in this quiz."""
    choices = [c for c in pool(level) if c not in recent] or pool(level)
    return rng.choice(choices)


def flag_path(code):
    return FLAG_DIR / f"{code}.png"


CHOSUNG = "ㄱㄲㄴㄷㄸㄹㅁㅂㅃㅅㅆㅇㅈㅉㅊㅋㅌㅍㅎ"


def chosung(text):
    """'프랑스' -> 'ㅍㄹㅅ'; spaces and other letters are kept."""
    out = []
    for ch in text:
        code = ord(ch) - 0xAC00
        out.append(CHOSUNG[code // 588] if 0 <= code < 11172 else ch)
    return "".join(out)
