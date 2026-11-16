"""Resolution rules of the "Highest temperature in CITY on DATE" markets, as their descriptions state (reports/78):

* the qualifying value is the highest temperature of the ROUTINE METAR observations of one airport station whose time
  stamp, converted to the station's local time, falls on the date; a report starting with SPECI does not count;
* the temperature of an observation is the remark group T[01]ddd[01]ddd in tenths of a degree C if present, else the
  body group (18/12, M05/M10) in whole degrees;
* it is rounded half up to a whole degree C, or converted to F (C * 9 / 5 + 32) and rounded half up;
* each bucket is a whole degree ("16°C"), a range ("48-49°F") or an open end ("9°C or below", "19°C or higher").
Checked on 640 resolved events of 3-16 Nov 2026: 639 winners rebuilt from the station's observations.
"""
import datetime
import math
import re
from zoneinfo import ZoneInfo

TGRP = re.compile(r"\bT([01])(\d{3})([01])(\d{3})\b")
BODY = re.compile(r"\s(M?\d{2})/(M?\d{2}|//)?(?=\s)")
OBS_TIME = re.compile(r"\b(\d{2})(\d{2})(\d{2})Z\b")
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november",
          "december"]
DATE_SLUG = re.compile(r"(" + "|".join(MONTHS) + r")-(\d{1,2})-(\d{4})$")


def metar_temp(metar):
    """Temperature (C) of one observation, or None: the T group of the remarks, else the body group."""
    if not metar:
        return None
    body, _, rmk = metar.partition(" RMK ")
    m = TGRP.search(rmk)
    if m:
        v = int(m.group(2)) / 10.0
        return -v if m.group(1) == "1" else v
    m = BODY.search(" " + body + " ")
    if m:
        t = m.group(1)
        return -int(t[1:]) if t.startswith("M") else int(t)
    return None


def is_special(line):
    """A report whose line begins with SPECI (aviationweather.gov raw format) does not count."""
    return line.lstrip().startswith("SPECI")


def round_half_up(x):
    return math.floor(x + 0.5)


def qualifying(temp_c, unit):
    """The whole degree in the market's unit ("C" or "F") that decides the bucket."""
    if unit == "F":
        return round_half_up(temp_c * 9 / 5 + 32 + 1e-9)
    return round_half_up(temp_c + 1e-9)


def parse_bucket(label):
    """'16°C' -> (16, 16); '9°C or below' -> (-inf, 9); '19°C or higher' -> (19, inf); '48-49°F' -> (48, 49)."""
    s = label.replace("°C", "").replace("°F", "").strip()
    if s.endswith("or below") or s.endswith("or lower"):
        return (-math.inf, int(s.split()[0]))
    if s.endswith("or higher") or s.endswith("or above"):
        return (int(s.split()[0]), math.inf)
    m = re.match(r"^(-?\d+)\s*-\s*(-?\d+)$", s)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    return (int(s), int(s))


def bucket_of(value, buckets):
    """Index of the bucket (lo, hi) containing a qualifying value, or None."""
    for i, (lo, hi) in enumerate(buckets):
        if lo <= value <= hi:
            return i
    return None


def event_date(slug):
    """The observation date of an event slug '...-on-november-17-2026'."""
    m = DATE_SLUG.search(slug)
    if not m:
        return None
    return datetime.date(int(m.group(3)), MONTHS.index(m.group(1)) + 1, int(m.group(2)))


def date_slug(d):
    return f"{MONTHS[d.month - 1]}-{d.day}-{d.year}"


def local_dt(ts, tz):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).astimezone(ZoneInfo(tz))


def obs_time(line, now):
    """UTC timestamp of a raw METAR line from its DDHHMMZ group, the latest such time not after now + 1 h."""
    m = OBS_TIME.search(line)
    if not m:
        return None
    day, hh, mm = int(m.group(1)), int(m.group(2)), int(m.group(3))
    base = datetime.datetime.fromtimestamp(now, datetime.timezone.utc)
    for back in range(0, 3):
        y, mo = base.year, base.month - back
        while mo < 1:
            mo += 12
            y -= 1
        try:
            t = datetime.datetime(y, mo, day, hh, mm, tzinfo=datetime.timezone.utc).timestamp()
        except ValueError:
            continue
        if t <= now + 3600:
            return t
    return None


def day_max(lines, tz, date, now=None):
    """(qualifying max in C, number of routine observations) of the local date from raw METAR lines."""
    now = now if now is not None else datetime.datetime.now(datetime.timezone.utc).timestamp()
    temps = []
    for line in lines:
        if is_special(line):
            continue
        t = obs_time(line, now)
        if t is None or local_dt(t, tz).date() != date:
            continue
        v = metar_temp(line)
        if v is not None:
            temps.append(v)
    return (max(temps) if temps else None), len(temps)
