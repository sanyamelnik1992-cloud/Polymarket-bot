"""Weather markets ("Highest temperature in <city> on <date>"): METAR parsing exactly as in the market rules.

Rules (from the market description):
  * qualifying value = max over ROUTINE METAR observations (SPECI excluded) whose local time falls on the date
  * value = hourly remark T-group in tenths of °C (T01390133 -> 13.9); if absent, body temp in whole °C (18/12, M05/M07)
  * °C markets: round half up to a whole degree; °F markets: convert C*9/5+32 first, then round half up
"""
import math
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

STATION_TZ = {
    "RJTT": "Asia/Tokyo", "RKSI": "Asia/Seoul", "RKPK": "Asia/Seoul", "ZSPD": "Asia/Shanghai",
    "ZBAA": "Asia/Shanghai", "ZUUU": "Asia/Shanghai", "ZUCK": "Asia/Shanghai", "ZHHH": "Asia/Shanghai",
    "ZGSZ": "Asia/Shanghai", "ZGGG": "Asia/Shanghai", "ZSQD": "Asia/Shanghai", "RCSS": "Asia/Taipei",
    "WSSS": "Asia/Singapore", "WMKK": "Asia/Kuala_Lumpur", "RPLL": "Asia/Manila", "VILK": "Asia/Kolkata",
    "OPKC": "Asia/Karachi", "OEJN": "Asia/Riyadh", "LLBG": "Asia/Jerusalem", "LTAC": "Europe/Istanbul",
    "LTFM": "Europe/Istanbul", "UUWW": "Europe/Moscow", "EFHK": "Europe/Helsinki", "EPWA": "Europe/Warsaw",
    "EDDM": "Europe/Berlin", "EHAM": "Europe/Amsterdam", "LFPB": "Europe/Paris", "EGLC": "Europe/London",
    "LEMD": "Europe/Madrid", "LIMC": "Europe/Rome", "FACT": "Africa/Johannesburg", "SAEZ": "America/Argentina/Buenos_Aires",
    "SBGR": "America/Sao_Paulo", "MMMX": "America/Mexico_City", "MPMG": "America/Panama", "CYYZ": "America/Toronto",
    "KLGA": "America/New_York", "KMIA": "America/New_York", "KATL": "America/New_York", "KORD": "America/Chicago",
    "KDAL": "America/Chicago", "KHOU": "America/Chicago", "KAUS": "America/Chicago", "KDEN": "America/Denver",
    "KLAX": "America/Los_Angeles", "KSFO": "America/Los_Angeles", "KSEA": "America/Los_Angeles",
    "NZWN": "Pacific/Auckland",
}

T_GROUP = re.compile(r"\sT([01])(\d{3})[01]\d{3}\b")
BODY = re.compile(r"\s(M?\d{1,2})/(M?\d{1,2})?(?=\s)")


def parse_temp_c(metar: str):
    """Temperature in °C (tenths when the T-group exists, else whole degrees) or None."""
    if not metar:
        return None
    if metar.lstrip().startswith("SPECI"):
        return None
    m = T_GROUP.search(metar)
    if m:
        v = int(m.group(2)) / 10.0
        return -v if m.group(1) == "1" else v
    body = metar.split(" RMK", 1)[0] + " "
    m = BODY.search(body)
    if m:
        t = m.group(1)
        return -int(t[1:]) if t.startswith("M") else int(t)
    return None


def round_half_up(x: float) -> int:
    return int(math.floor(x + 0.5 + 1e-9))


def qualifying(max_c: float, unit: str) -> int:
    return round_half_up(max_c * 9 / 5 + 32) if unit == "F" else round_half_up(max_c)


def local_date(ts_utc: datetime, station: str) -> str:
    return ts_utc.astimezone(ZoneInfo(STATION_TZ[station])).strftime("%Y-%m-%d")


def local_hour(ts_utc: datetime, station: str) -> float:
    lt = ts_utc.astimezone(ZoneInfo(STATION_TZ[station]))
    return lt.hour + lt.minute / 60


def utc(ts: str) -> datetime:
    return datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)


def bucket_contains(lo, hi, v) -> bool:
    return lo <= v <= hi
