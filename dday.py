"""D-Day: date parsing, the message template and alert times (Korean time). No Discord or DB code here.

Template fields: {이름} {D} {년} {월} {일} {시} {분} {초}. The time left is split into the fields the template
uses, so "{일} 남았습니다" gives "400일" while "{년} {월} {일}" gives "1년 1개월 5일". With {시}/{분}/{초} the count
runs to the exact moment; without them it counts calendar days (like D-1, D-DAY)."""
import calendar
import re
from datetime import date, datetime, time, timedelta, timezone

KST = timezone(timedelta(hours=9))
DEFAULT_TEMPLATE = "{이름}까지 {일} 남았습니다. ({D})"
COUNT_UP_TEMPLATE = "{이름} {D} ({일}째)"
DEFAULT_CELEBRATE = "🎉 오늘이 **{이름}**입니다! ({D})"
CELEBRATE_FIELDS = ("이름", "D")  # on the day there is no time left to show
FIELDS = ("이름", "D", "년", "월", "일", "시", "분", "초")
SUFFIX = {"년": "년", "월": "개월", "일": "일", "시": "시간", "분": "분", "초": "초"}
SECONDS = {"일": 86400, "시": 3600, "분": 60, "초": 1}
AFTER_MODES = {
    "count": "D+로 계속 세기 (알림도 계속)",
    "stop": "당일에 축하하고 알림 끝내기",
    "delete": "당일에 축하하고 지우기",
}


def now_kst():
    return datetime.now(KST)


def parse_target(text, now=None):
    """'2027-11-18', '2027.11.18', '2027/11/18', '20271118', '11-18' (the next one), each optionally with a time
    ('08:40', '8시', '8시 40분'). Returns (aware datetime in KST, has_time) or raises ValueError."""
    now = now or now_kst()
    text = " ".join(text.strip().split())
    clock = None
    m = re.search(r"\s+(\d{1,2})(?::(\d{2})|시(?:\s*(\d{1,2})분)?)$", text)
    if m:
        hour, minute = int(m[1]), int(m[2] or m[3] or 0)
        if hour > 23 or minute > 59:
            raise ValueError("시각은 00:00 ~ 23:59 사이로 적어 주세요.")
        clock = time(hour, minute)
        text = text[:m.start()]
    m = re.fullmatch(r"(\d{4})[-./ ](\d{1,2})[-./ ](\d{1,2})\.?|(\d{4})(\d{2})(\d{2})|(\d{1,2})[-./](\d{1,2})\.?", text)
    if not m:
        raise ValueError("날짜를 알아보지 못했습니다. 예: 2027-11-18, 2027-11-18 08:40")
    try:
        if m[1]:
            day = date(int(m[1]), int(m[2]), int(m[3]))
        elif m[4]:
            day = date(int(m[4]), int(m[5]), int(m[6]))
        else:
            day = date(now.year, int(m[7]), int(m[8]))
            if day < now.date():
                day = date(now.year + 1, day.month, day.day)
    except ValueError:
        raise ValueError("없는 날짜입니다. 날짜를 다시 확인해 주세요.") from None
    if not 2000 <= day.year <= 2200:
        raise ValueError("2000년 ~ 2200년 사이의 날짜로 정해 주세요.")
    return datetime.combine(day, clock or time(0, 0), KST), clock is not None


def parse_alert(text):
    """'00:00', '7:30', '7', '7시', '7시 30분', '자정' -> 'HH:MM'; '' -> None; else ValueError."""
    text = text.strip().replace(" ", "")
    if not text:
        return None
    if text in ("자정", "0시"):
        return "00:00"
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2})|시(?:(\d{1,2})분)?)?", text)
    if not m or int(m[1]) > 23 or int(m[2] or m[3] or 0) > 59:
        raise ValueError("알림 시각은 00:00 ~ 23:59로 적어 주세요. (예: 00:00, 7:30, 9시)")
    return f"{int(m[1]):02d}:{int(m[2] or m[3] or 0):02d}"


def check_template(template, fields=FIELDS):
    """The template, or ValueError naming a field that doesn't exist."""
    for field in re.findall(r"\{([^{}]*)\}", template):
        if field not in fields:
            raise ValueError(f"문구에 없는 칸 {{{field}}}이(가) 있습니다. 쓸 수 있는 칸: "
                             + " ".join("{" + f + "}" for f in fields))
    return template


def days_left(target, now=None):
    """Calendar days from today to the target date (0 on the day, negative after)."""
    now = (now or now_kst()).astimezone(KST)
    return (target.astimezone(KST).date() - now.date()).days


def d_label(days):
    return "D-DAY" if days == 0 else f"D-{days}" if days > 0 else f"D+{-days}"


def _add_months(moment, months):
    total = moment.month - 1 + months
    year, month = moment.year + total // 12, total % 12 + 1
    return moment.replace(year=year, month=month, day=min(moment.day, calendar.monthrange(year, month)[1]))


def _months_between(start, end):
    months = (end.year - start.year) * 12 + end.month - start.month
    while months > 0 and _add_months(start, months) > end:
        months -= 1
    return months


def split(start, end, used):
    """{field: number} for the fields in used, splitting the time from start to end (end >= start)."""
    values = {}
    if "년" in used or "월" in used:
        months = _months_between(start, end)
        if "년" in used:
            values["년"] = months // 12
            months = months % 12 if "월" in used else 0
            start = _add_months(start, values["년"] * 12)
        if "월" in used:
            values["월"] = months
            start = _add_months(start, months)
    rest = (end - start).total_seconds() if isinstance(end, datetime) else (end - start).days * 86400
    for field in ("일", "시", "분", "초"):
        if field in used:
            values[field], rest = divmod(int(rest), SECONDS[field])
    return values


def render(template, name, target, now=None, count_up=False, celebrate=None):
    """The message for this moment: the template before the day, a celebration on the day, D+ after.
    count_up: a D-Day made for a day already past (사귄 날 ...): the template is filled with the time since."""
    now = (now or now_kst()).astimezone(KST)
    target = target.astimezone(KST)
    left = days_left(target, now)
    used = {f for f in re.findall(r"\{([^{}]*)\}", template) if f in SUFFIX}
    exact = bool(used & {"시", "분", "초"})
    ahead = (exact and now < target) or left > 0
    if ahead or count_up:
        if ahead:
            start, end = (now.replace(microsecond=0), target) if exact else (now.date(), target.date())
        else:
            start, end = (target, now.replace(microsecond=0)) if exact else (target.date(), now.date())
        values = split(start, end, used)
        # Leading units that are 0 are left out ("10개월 16일", not "0년 10개월 16일"); the smallest one stays.
        order = [f for f in ("년", "월", "일", "시", "분", "초") if f in values]
        for field in order[:-1]:
            if values[field]:
                break
            values[field] = None
        text = template.replace("{이름}", name).replace("{D}", d_label(left))
        for field, value in values.items():
            text = text.replace("{" + field + "}", "" if value is None else f"{value}{SUFFIX[field]}")
        return re.sub(r"[ \t]{2,}", " ", text).strip()
    if left == 0:
        return (celebrate or DEFAULT_CELEBRATE).replace("{이름}", name).replace("{D}", d_label(0))
    return f"**{name}** · {d_label(left)} ({-left}일 지났습니다)"


def next_alert(alert_time, now=None):
    """Epoch seconds of the next 'HH:MM' (Korean time) after now."""
    now = (now or now_kst()).astimezone(KST)
    hour, minute = map(int, alert_time.split(":"))
    moment = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if moment <= now:
        moment += timedelta(days=1)
    return moment.timestamp()


def target_text(target, has_time):
    target = target.astimezone(KST)
    weekday = "월화수목금토일"[target.weekday()]
    return target.strftime("%Y-%m-%d") + f" ({weekday})" + (target.strftime(" %H:%M") if has_time else "")
