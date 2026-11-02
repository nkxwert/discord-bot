"""Per-server easter eggs ("€김치" -> "맛있다"), kept in plain text files that can be edited in Notepad.

Each Discord server gets easter_eggs/<server id>.txt with one "명령어 | 할 말" per line.
Lines starting with # are comments. Deleting a line in Notepad deletes that easter egg.
"""
import os
import re
from pathlib import Path

EGG_DIR = Path(os.getenv("EASTER_EGG_DIR", "easter_eggs"))
MAX_EGGS_PER_GUILD = 33
MAX_KEY_LENGTH = 30
MAX_TEXT_LENGTH = 200
SEPARATOR = "|"
REGISTER_PATTERN = re.compile(r"if\s+(.+?)\s+then\s+say\s+(.+)", re.IGNORECASE | re.DOTALL)

_cache = {}  # guild id -> (file mtime, {key: text}); re-read whenever the file changes


def _path(guild_id):
    return EGG_DIR / f"{guild_id}.txt"


def load(guild_id):
    path = _path(guild_id)
    try:
        mtime = path.stat().st_mtime_ns
    except FileNotFoundError:
        return {}
    hit = _cache.get(guild_id)
    if hit and hit[0] == mtime:
        return hit[1]
    eggs = {}
    # utf-8-sig: Notepad may save UTF-8 with a BOM.
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or SEPARATOR not in line:
            continue
        key, text = (part.strip() for part in line.split(SEPARATOR, 1))
        if key and text:
            eggs[key] = text
    _cache[guild_id] = (mtime, eggs)
    return eggs


def find(guild_id, key):
    key = key.casefold()
    for name, text in load(guild_id).items():
        if name.casefold() == key:
            return text
    return None


def _save(guild_id, guild_name, eggs):
    path = _path(guild_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# 이스터에그 - 서버: {guild_name} ({guild_id})",
        "# 형식: 명령어 | 할 말   (줄을 지우면 그 이스터에그가 삭제됩니다)",
        *(f"{key} {SEPARATOR} {text}" for key, text in eggs.items()),
    ]
    temp = path.with_suffix(".tmp")
    temp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temp.replace(path)


def _strip_parens(text):
    text = text.strip()
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1].strip()
    return text


def parse_registration(content):
    """'if 김치 then say 맛있다' (parentheses optional) -> ('김치', '맛있다'), or None if it isn't one."""
    match = REGISTER_PATTERN.fullmatch(content.strip())
    if not match:
        return None
    return _strip_parens(match.group(1)), _strip_parens(match.group(2))


def register(guild_id, guild_name, key, text):
    """Save or replace an easter egg. Raises ValueError with a user-facing message when it is invalid."""
    if not key or not text:
        raise ValueError("명령어와 할 말을 모두 입력해 주세요. 예: `if 김치 then say 맛있다`")
    if "\n" in key or "\n" in text:
        raise ValueError("명령어와 할 말은 한 줄로 입력해 주세요.")
    if SEPARATOR in key or key.startswith("#"):
        raise ValueError(f"명령어에는 `{SEPARATOR}`를 쓸 수 없고 `#`으로 시작할 수 없습니다.")
    if len(key) > MAX_KEY_LENGTH or len(text) > MAX_TEXT_LENGTH:
        raise ValueError(f"명령어는 {MAX_KEY_LENGTH}자, 할 말은 {MAX_TEXT_LENGTH}자까지 입력할 수 있습니다.")

    eggs = dict(load(guild_id))
    existing = next((name for name in eggs if name.casefold() == key.casefold()), None)
    if existing is None and len(eggs) >= MAX_EGGS_PER_GUILD:
        raise ValueError(
            f"이스터에그는 서버마다 {MAX_EGGS_PER_GUILD}개까지 저장할 수 있습니다. 메모장에서 지운 뒤 다시 등록해 주세요."
        )
    if existing is not None:
        eggs[existing] = text  # replace, keeping its place in the file
    else:
        eggs[key] = text
    _save(guild_id, guild_name, eggs)
    return existing or key
