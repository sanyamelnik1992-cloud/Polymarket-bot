"""Weather markets (reports/78): resolution rules and the recorder (no orders)."""
import datetime
import gzip
import json
import math

import pytest

from pmbot.weather import rules
from pmbot.weather import sources as src


def test_metar_temperature_takes_the_remark_tenths_else_the_body():
    assert rules.metar_temp("METAR KLGA 171551Z 27012KT 10SM FEW250 18/03 A3005 RMK AO2 SLP176 T01830033") == 18.3
    assert rules.metar_temp("METAR KORD 171551Z 27012KT 10SM M02/M08 A3005 RMK AO2 T10171083") == -1.7
    assert rules.metar_temp("METAR RJTT 161530Z 02008KT CAVOK 15/08 Q1022 NOSIG") == 15
    assert rules.metar_temp("METAR UUWW 161530Z 02008KT CAVOK M11/M17 Q1022 NOSIG") == -11
    assert rules.metar_temp("METAR XXXX 161530Z 02008KT CAVOK Q1022") is None
    assert rules.is_special("SPECI KLGA 171612Z 27012KT 10SM 19/03 A3005") and not rules.is_special("METAR KLGA 171551Z")


def test_rounding_and_buckets_follow_the_market_text():
    # examples from the rules: 13.9 -> 14, 17.4 -> 17, 16.5 -> 17, -1.7 -> -2, -8.5 -> -8; 18.3 C = 64.94 F -> 65 ...
    assert [rules.qualifying(x, "C") for x in (13.9, 17.4, 16.5, -1.7, -8.5)] == [14, 17, 17, -2, -8]
    assert [rules.qualifying(x, "F") for x in (18.3, 17.8, 19, -22.5)] == [65, 64, 66, -8]
    assert rules.parse_bucket("16°C") == (16, 16) and rules.parse_bucket("48-49°F") == (48, 49)
    assert rules.parse_bucket("9°C or below") == (-math.inf, 9) and rules.parse_bucket("19°C or higher") == (19, math.inf)
    b = [rules.parse_bucket(x) for x in ("47°F or below", "48-49°F", "50-51°F", "52°F or higher")]
    assert rules.bucket_of(49, b) == 1 and rules.bucket_of(40, b) == 0 and rules.bucket_of(60, b) == 3


def test_day_max_counts_routine_reports_of_the_local_date_only():
    now = datetime.datetime(2026, 11, 17, 18, 0, tzinfo=datetime.timezone.utc).timestamp()
    lines = ["METAR RJTT 161430Z 07006KT CAVOK 17/09 Q1021",          # 23:30 JST on the 16th: another date
             "METAR RJTT 161500Z 03004KT CAVOK 16/07 Q1022",          # 00:00 JST on the 17th
             "SPECI RJTT 170512Z 03004KT CAVOK 21/07 Q1022",          # special: does not count
             "METAR RJTT 170500Z 03004KT CAVOK 19/07 Q1022"]
    mx, n = rules.day_max(lines, "Asia/Tokyo", datetime.date(2026, 11, 17), now=now)
    assert mx == 19 and n == 2
    assert rules.event_date("highest-temperature-in-tokyo-on-november-17-2026") == datetime.date(2026, 11, 17)
    assert rules.date_slug(datetime.date(2026, 11, 7)) == "november-7-2026"


EVENT = {"slug": "highest-temperature-in-nyc-on-november-17-2026", "endDate": "2026-11-17T12:00:00Z", "closed": False,
         "description": "... routine (METAR) observation ... rounded to the nearest whole degree ...",
         "markets": [{"groupItemTitle": "48-49°F", "conditionId": "c2", "clobTokenIds": '["y2", "n2"]', "outcomePrices": '["0.3", "0.7"]',
                      "resolutionSource": "https://aviationweather.gov/api/data/metar?ids=KLGA&format=raw"},
                     {"groupItemTitle": "47°F or below", "conditionId": "c1", "clobTokenIds": '["y1", "n1"]', "outcomePrices": '["0.1", "0.9"]'},
                     {"groupItemTitle": "50°F or higher", "conditionId": "c3", "clobTokenIds": '["y3", "n3"]', "outcomePrices": '["0.6", "0.4"]'}]}


def test_event_parsing():
    e = src.parse_event(EVENT)
    assert e["city"] == "nyc" and e["date"] == "2026-11-17" and e["station"] == "KLGA" and e["unit"] == "F" and e["rules_ok"]
    assert [m["label"] for m in e["markets"]] == ["47°F or below", "48-49°F", "50°F or higher"]
    assert e["markets"][0]["lo"] is None and e["markets"][2]["hi"] is None and e["markets"][1]["yes"] == "y2"
    assert json.loads(json.dumps(e)) == e                              # plain JSON: no Infinity in the files
    assert src.parse_event({"slug": "will-it-rain", "markets": []}) is None


class FakeSources:
    utc_dates_around = staticmethod(src.utc_dates_around)

    def __init__(self):
        self.calls = []

    def discover_cities(self):
        return ["nyc"]

    def events_for(self, cities, dates):
        self.calls.append(("events", tuple(cities)))
        e = src.parse_event(EVENT)
        e["date"] = str(datetime.datetime.now(datetime.timezone.utc).date())
        return [e]

    def station_info(self, stations):
        return {}

    def metar(self, stations, hours=3):
        self.calls.append(("metar", tuple(stations)))
        return {"KLGA": ["METAR KLGA 171551Z 27012KT 10SM 18/03 A3005 RMK T01830033"]}

    def books(self, tokens):
        return {t: dict(bid=0.2, bid_sz=10, ask=0.25, ask_sz=5, bid_depth=10, ask_depth=5) for t in tokens}

    def forecast(self, lat, lon):
        return {"timezone": "America/New_York", "hourly": {"time": ["2026-11-17T00:00"], "temperature_2m_ecmwf_ifs025": [8.1]}}


def test_recorder_writes_each_source_and_does_not_repeat_itself(tmp_path, monkeypatch):
    import importlib.util
    import os
    spec = importlib.util.spec_from_file_location("record_weather", os.path.join(os.path.dirname(__file__), "..", "scripts", "record_weather.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    fs = FakeSources()
    rec = mod.WeatherRecorder(str(tmp_path), fetch=fs)
    rec.poll()
    rec.poll()                                                         # nothing is due again: no second writes
    files = {p.name: [json.loads(x) for x in gzip.open(p, "rt")] for p in tmp_path.glob("*/*.jsonl.gz")}
    assert set(files) == {"events.jsonl.gz", "metar.jsonl.gz", "books.jsonl.gz", "forecasts.jsonl.gz"}
    assert len(files["events.jsonl.gz"]) == 1 and len(files["metar.jsonl.gz"]) == 1 and len(files["books.jsonl.gz"]) == 3
    assert files["books.jsonl.gz"][0]["slug"] == EVENT["slug"] and files["forecasts.jsonl.gz"][0]["temps"] == {"ecmwf_ifs025": [8.1]}
    assert sum(1 for c in fs.calls if c[0] == "metar") == 1
    rec.last = {k: -1e18 for k in rec.last}                            # everything due again: an unchanged event is not rewritten
    rec.poll()
    files = {p.name: [json.loads(x) for x in gzip.open(p, "rt")] for p in tmp_path.glob("*/*.jsonl.gz")}
    assert len(files["events.jsonl.gz"]) == 1 and len(files["metar.jsonl.gz"]) == 1 and len(files["books.jsonl.gz"]) == 6


def test_weather_unit_is_data_only_and_wired():
    import os
    from pmbot import ops
    root = os.path.join(os.path.dirname(__file__), "..")
    text = open(os.path.join(root, "deploy", "pmbot-weather-rec.service")).read()
    assert "record_weather.py" in text and "--live" not in text and "Nice=10" in text
    assert "pmbot-weather-rec" in ops.ALL_UNITS and "pmbot-weather-rec" not in ops.BOT_UNITS
    for f in ("install_units.sh", "autoupdate.sh"):
        assert " pmbot-weather-rec " in open(os.path.join(root, "deploy", f)).read(), f
    st = json.load(open(os.path.join(root, "config", "weather_stations.json")))
    assert st["KLGA"]["tz"] == "America/New_York" and st["RJTT"]["tz"] == "Asia/Tokyo" and len(st) >= 40
