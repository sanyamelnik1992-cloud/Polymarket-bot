# Инструменты отчёта 77

Всё — публичные данные биржи (`data-api.polymarket.com/v2`, Gamma, CLOB). Адресов кошельков в репозитории нет:
наш кошелёк передаётся через `PMBOT_WALLET`, чужие печатаются только префиксом на экран.

| шаг | что | файлы |
|---|---|---|
| 1 | Наши реальные деньги по записи биржи | `PMBOT_WALLET=... python own_wallet.py OUT` |
| 2 | Лидерборды 10 категорий, дневной PnL кошельков | `lb.py` → `upnl.py crypto/month,...` → `prof.py`, `prof_all.py` |
| 3 | Сделки кошельков и их стиль | `trades_of.py LIST.json DAYS OUT.json` → `style.py`, `cat_style.py` (+ `catlib.py`) |
| 4 | 5/15-минутки: где у лучших преимущество | `outcomes.py` → `edge.py`, `updown_profile.py` |
| 5 | 5/15-минутки: все покупатели рынка, не только лучшие | `mkt_map.py` → `mkt_map_an.py`, `late_favourites.py` |
| 6 | Погода: специалисты и их преимущество | `trades_of.py weather_lb.json 3 trades_weather.json` → `wx_outcomes.py` → `wx_edge.py` |

Скрипты пишут и читают файлы в текущей папке; запускать из отдельной рабочей папки.
