"""Rules of the temperature markets: METAR temperature (T-group tenths, else body whole degrees), local date,
rounding half up, buckets in degrees C or F."""
import re, math, datetime
from zoneinfo import ZoneInfo
TGRP = re.compile(r"\bT([01])(\d{3})([01])(\d{3})\b")
BODY = re.compile(r"\s(M?\d{2})/(M?\d{2}|//)?\s")
def metar_temp(metar):
    """Temperature in C of one observation: the T group of the remarks (tenths) if any, else the body (whole)."""
    if not metar: return None
    rmk = metar.split(" RMK ", 1)[1] if " RMK " in metar else ""
    m = TGRP.search(rmk)
    if m:
        v = int(m.group(2)) / 10.0
        return -v if m.group(1) == "1" else v
    m = BODY.search(" " + metar.split(" RMK ", 1)[0] + " ")
    if m:
        t = m.group(1)
        return -int(t[1:]) if t.startswith("M") else int(t)
    return None
def round_half_up(x):
    return math.floor(x + 0.5)
def qualifying(temp_c, unit):
    """Whole degree in the market's unit that decides the bucket."""
    if unit == "F":
        return round_half_up(temp_c * 9 / 5 + 32 + 1e-9)
    return round_half_up(temp_c + 1e-9)
def parse_bucket(label):
    """'16°C' -> (16, 16); '9°C or below' -> (-inf, 9); '19°C or higher' -> (19, inf); '48-49°F' -> (48, 49)."""
    s = label.replace("°C", "").replace("°F", "").strip()
    if s.endswith("or below"): return (-math.inf, int(s.split()[0]))
    if s.endswith("or higher") or s.endswith("or above"): return (int(s.split()[0]), math.inf)
    m = re.match(r"^(-?\d+)\s*-\s*(-?\d+)$", s)
    if m: return (int(m.group(1)), int(m.group(2)))
    return (int(s), int(s))
def local_date(utc_str, tz):
    t = datetime.datetime.fromisoformat(utc_str).replace(tzinfo=datetime.timezone.utc)
    return t.astimezone(ZoneInfo(tz))
DATE_RE = re.compile(r"(january|february|march|april|may|june|july|august|september|october|november|december)-(\d+)-(\d{4})")
def event_date(date_slug):
    m = DATE_RE.match(date_slug)
    months = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"]
    return datetime.date(int(m.group(3)), months.index(m.group(1)) + 1, int(m.group(2)))
