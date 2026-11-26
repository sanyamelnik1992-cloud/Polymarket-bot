# Инструменты отчёта 96

| скрипт | что делает |
|---|---|
| `window_sim.py TABLE.npz [...]` | мейкер на ленте ПО ОКНАМ: заявки по правилу, одна заявка на 5 акций на сторону, лимиты живого теста, сдвиг цены от позиции; два вида исполнения («по цене» / «насквозь»); итог на акцию, $ на окно, доля проигранных окон, 5 % худших окон, шанс стопа −$5 за 50 окон, по дням |
| `live_fills.py SETTLES.csv UNIT DATE BN1S.npz` | реальные исполнения живого юнита по публичным сделкам: наш кошелёк находится по совпадению акций Up / Down во всех окнах дня (адрес нигде не пишется); для каждого исполнения — модельная цена, запас, движение Binance, ход цены за 1 с, исход, кто был на другой стороне |

Таблицы ленты строит `reports/95_tools/tape_table.py`, секундные цены Binance — `reports/95_tools/bn_fetch.py`.

Порядок (26 ноября): `bn_fetch.py 2026-11-24 2026-11-27 bn1s.npz BTCUSDT`, `fetch_tape.py 2026-11-26 2026-11-27 tape btc 5m`,
`tape_table.py tape bn1s.npz table26.npz`, `window_sim.py table26.npz`;
`git show origin/server-reports:settles.csv > settles.csv`, `live_fills.py settles.csv pmbot-maker-live 2026-11-26 bn1s.npz`.

Правила в `window_sim.py` задаются словарём, неизвестный ключ — ошибка. Поля: `hf` / `hd` (запас фаворита / аутсайдера),
`rel_lo` / `rel_hi` (секунды окна), `pmin` / `pmax`, `gate` / `gate_col` (фильтр Binance), `max_side`, `max_unp`, `skew`, `shares`,
`repl_s`, `k_time` (заявка не ближе k × хода цены за 1 с), `vol_ratio` (× σ 10 мин / σ часа), `calm` (пауза при беспокойном рынке),
`cool_s` (сторона молчит после исполнения), `pair_stop`, `late_rel`.
