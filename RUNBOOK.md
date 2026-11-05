# RUNBOOK: запуск ботов на сервере

Два бота (подробности и доказательства — в `STRATEGIES.md` и `README.md`):

| Бот | Что делает | Конфиг paper | Шаблон live |
|---|---|---|---|
| **hourly** (`scripts/run_hourly.py`) | Часовые BTC/ETH Up/Down: в последние 30 мин покупает сторону, где модель на Binance даёт edge ≥ 8¢ после комиссии (FAK с лимитом) | `config/hourly.toml` | `config/hourly_live.example.toml` |
| **5m** (`scripts/run_bot.py`) | 5-минутные BTC/ETH/SOL: два post-only бида Up/Down на 3¢ ниже середины рынка; модель на Chainlink работает как фильтр | `config/paper_v3.toml` (прежний: `paper_v2.toml`) | `config/live.example.toml` (только после доказанного edge) |
| recorder (`scripts/record_live.py`) | пишет стаканы + Chainlink + Binance для replay-бэктестов | — | — |

## 0. Установка на сервер (Ubuntu/Debian)

```bash
git clone https://github.com/sanyamelnik1992-cloud/Polymarket-bot && cd Polymarket-bot
git checkout claude/polymarket-5m-research
sudo bash deploy/setup.sh                    # venv, зависимости, chrony, systemd-юниты, /etc/pmbot/pmbot.env
.venv/bin/python scripts/preflight.py        # ДОЛЖНО быть "0 failed"; geoblock: blocked=False
sudo systemctl start pmbot-hourly pmbot-5m pmbot-recorder   # всё в PAPER-режиме
journalctl -u pmbot-hourly -f                # живой лог
```

### Сервер рядом с Polymarket (для 5m)

Матчинг — AWS eu-west-2 (Лондон). Берём **eu-west-1 (Дублин)**: Ирландия ограничена только на сайте, API открыт.
Обычный VPS в Великобритании — close-only. Сервер не меняет требования к юрисдикции владельца аккаунта.
```bash
.venv/bin/python scripts/latency_probe.py 120   # цель: CLOB REST RTT < 15 мс, CLOB ws < 10 мс, Binance ~110–120 мс
# 5m на быстром сервере: в /etc/systemd/system/pmbot-5m.service заменить config/paper_v3.toml на config/colo.toml
sudo systemctl daemon-reload && sudo systemctl restart pmbot-5m
```
Почему именно так и чего ждать — `reports/29_colocation_5m.md`. Коротко: в replay быстрый мейкер с котировкой в 1¢
от середины в плюсе на всех трёх записях, медленный — около нуля. Тейкер на 5m убыточен из любой точки.
`config/colo.toml` котирует на 1¢ от середины — **на медленном сервере его не запускать**.

### Диск

Рекордер пишет ~13 ГБ в сутки (в основном `clob.jsonl`), каждые 24 часа — новый каталог `data/rec/<дата>`.
Нужен диск от 200 ГБ, а старые записи надо регулярно удалять или переносить. Автоочистку я в юнит не добавлял:
что и когда удалять, решаете вы. Проверка места: `du -sh data/rec/* | sort -h`.

Отчёты:
```bash
.venv/bin/python scripts/report_journal.py data/journal/<run>/          # 5m
grep -h '"settle"' data/journal-hourly/*/events.jsonl | tail             # hourly
.venv/bin/python scripts/replay_bot.py data/rec/<rec> out/ config/paper_v3.toml --grid2   # replay вариантов
```
Остановить всё немедленно: `touch STOP` в каталоге репозитория (или `systemctl stop pmbot-*`).
Telegram-оповещения: заполнить `PMBOT_TG_TOKEN` и `PMBOT_TG_CHAT` в `/etc/pmbot/pmbot.env`.

## 1. Слои защиты

**Фильтры 5m** (`pmbot/bot/filters.py`; если не прошёл хотя бы один фильтр, котировки снимаются):
- свежесть данных: Chainlink ≤ 3.5 с, Binance ≤ 5 с, WebSocket стакана жив;
- страйк известен: опубликован Polymarket или посчитан минимум по 50 из 60 секунд Chainlink;
- спред ≤ 3¢; |fair − середина| ≤ 10¢; P(Up) в 5–95%; время в окне 5–240 с;
- скачок Binance больше 6σ за 1 с → пауза 3 с; короткая волатильность больше 2× длинной → не котировать;
- fair сдвинулся больше чем на 10¢ за 5 с → не котировать; ≥2 исполнения или ≥10 акций на стороне за 5 с → сторона блокируется на 30 с;
- XRP исключён.

**Фильтры hourly** (`pmbot/bot/hourly.py`): последние 30 мин и не позже чем за 10 с до конца; Binance ≤ 3 с;
стакан жив; цена 3–97¢; пауза 20 с между входами; только одна сторона за час.

**Риск (оба бота):** лимит на рынок или окно, общий лимит открытого риска (активы коррелированы),
дневной стоп по реализованному убытку → kill switch. **Дневной PnL и kill switch сохраняются на диск**
(`risk.state_file` / `hourly.state_file`): после рестарта бот не начнёт торговать до следующих суток UTC.
Kill-файл `STOP`. systemd: `Restart=on-failure`, но не больше 5 рестартов за 10 мин; kill switch завершает
процесс с кодом 0, поэтому systemd его не перезапускает.

**Исполнение live** (`pmbot/bot/live.py`):
- 5m: post-only + GTD + heartbeat (без сигнала биржа отменяет всё через ~10 с), `cancel_all` при старте и остановке,
  подгрузка уже открытых позиций кошелька;
- hourly: FAK с `max_price` и `max_spend` — никогда не дороже лимита и не больше $ на вход;
- старт live запрещён без: флага `--live` **и** `mode = "live"` в конфиге; `PMBOT_LIVE_CONFIRM=I_UNDERSTAND_REAL_MONEY_RISK`;
  ключа в окружении; `geoblock: blocked=false`; баланса pUSD ≥ лимита риска; `state_file`.
  Ключ никогда не пишется в журнал (покрыто тестом).

## 2. Чек-лист перед реальными деньгами

1. **Юрисдикция.** Аккаунт и сервер — в стране, где Polymarket разрешает торговлю. Россия, США, Великобритания,
   Германия, Франция, Польша и др. — close-only. Обход через VPN нарушает правила и грозит заморозкой средств.
2. **Paper минимум 3 дня** на этом же сервере. Критерии перехода:
   - hourly: ≥ 30 закрытых часов с позицией, суммарный PnL > 0, t > 2, нет дня с убытком больше дневного стопа;
   - 5m: ≥ 150 окон, t > 2 в разные дни (пока не выполнено — 5m остаётся в paper).
3. **Отдельный кошелёк** только для бота, на нём ровно бюджет теста ($50–200). Лучше **Session Key** (beta):
   им можно торговать, но нельзя вывести средства. Основной ключ на сервер не кладём.
4. `cp config/hourly_live.example.toml config/hourly_live.toml`, проверить каждое число
   ($5 на вход, $10 на час, $20 общий риск, стоп $15 в день).
5. В `/etc/pmbot/pmbot.env`: `PMBOT_PRIVATE_KEY`, `PMBOT_WALLET` (опц.), `PMBOT_LIVE_CONFIRM=I_UNDERSTAND_REAL_MONEY_RISK`.
   Затем `.venv/bin/python scripts/preflight.py --live` — 0 FAIL.
6. В `deploy/pmbot-hourly.service` заменить `--config config/hourly.toml` на `--config config/hourly_live.toml --live`,
   выполнить `sudo bash deploy/setup.sh && sudo systemctl restart pmbot-hourly`.
7. Первые часы — под присмотром: сверять `order_filled` (live) с сигналами и ценами paper. Если исполнение заметно
   хуже — `touch STOP`.
8. Лимиты увеличивать не чаще раза в неделю и только при выполнении критериев п.2 на живых деньгах.

## 3. Что может пойти не так

| Риск | Защита |
|---|---|
| Модель ошибается (базис, неверный страйк, переуверенность) | фильтр расхождения с рынком, смесь с ценой рынка (v3), страйк из точного источника, лимиты |
| Резкое движение, устаревшие заявки | jump/vol/instability-фильтры, моментальное понижение бидов, taker delay 150 мс |
| Бот или сервер упал с открытыми ордерами | heartbeat → автоотмена через ~10 с; GTD; у hourly висящих заявок нет вообще (FAK) |
| Рестарт после убытков | дневной PnL и kill switch на диске; лимит рестартов systemd |
| Серия неудач | дневной стоп, пауза после серии убытков, kill-файл |
| Обрыв данных | фильтры свежести → снятие котировок или пропуск сигналов |
| Высокая задержка сервера | сервер в Дублине (eu-west-1), `latency_probe.py`; replay: при 300 мс вместо 150 мс 5m-стратегия уходит в −40% |
| Конкуренция за те же сделки (в paper её нет) | маленький старт, сверка live-исполнения с paper |
| Ошибка в коде | 42 юнит-теста, paper по умолчанию, лимиты в $ на всё |

## 4. Что ещё не проверено

- реальный ордерный цикл ни разу не запускался на бирже (из этой среды торговля заблокирована геоблоком);
  первый live — минимальные лимиты;
- самый быстрый источник Chainlink для 5m — PolyBolt (`price.crypto`, нужны API-ключи); сейчас RTDS (+~1.4 с);
- арбитраж 5m↔15m и сканер лестниц — пока отдельные paper-инструменты.
