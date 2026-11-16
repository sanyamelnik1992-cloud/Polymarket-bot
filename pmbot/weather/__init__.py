"""Weather (temperature) markets of Polymarket.

`legacy` (was pmbot/weather.py, reports/19 and 35): METAR parsing, station time zones, used by scripts/collect_weather.py,
analyze_weather.py, weather_forecast_snapshot.py / _eval.py. `rules` and `sources` (reports/78): the resolution rules
with the routine / SPECI split and public data sources for scripts/record_weather.py.
"""
from .legacy import STATION_TZ, local_date, parse_temp_c, qualifying, round_half_up  # noqa: F401
