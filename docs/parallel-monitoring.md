# Параллельный скрапинг фильтров

## Включено

- `scripts/run_monitors_parallel.py` — до N фильтров одновременно (`-j` / `MONITOR_PARALLEL_JOBS`)
- CI: `.github/workflows/travel_monitor_sequential.yml` с `MONITOR_PARALLEL_ENABLED: "true"` и `MONITOR_PARALLEL_JOBS: "3"`

Логи: `logs/parallel/config_ci_filter_*.log`

## Быстрый откат (без revert)

В workflow поменять одну строку и запушить:

```yaml
MONITOR_PARALLEL_ENABLED: "false"
```

Следующий hourly run снова пойдёт **последовательно**, как раньше.

## Откат через git

```bash
git revert <commit-sha>   # коммит с parallel monitoring
git push origin main
```

## Локально

```bash
# параллельно (как CI)
MONITOR_PARALLEL_JOBS=3 ./scripts/run_monitors_parallel.sh

# последовательно (старый способ)
python travel_monitor.py --config config_ci_filter_7_10.json
# … остальные 5 конфигов
```

## Параллельные дашборды

- `scripts/run_dashboards_parallel.py` — до N фильтров одновременно (`-j` / `DASHBOARD_PARALLEL_JOBS`)
- CI: шаг **Generate dashboards** с `DASHBOARD_PARALLEL_JOBS: "3"`

```bash
DASHBOARD_PARALLEL_JOBS=3 python scripts/run_dashboards_parallel.py
```

## HTTP vs Playwright

Скрапер сначала делает быстрый HTTP-парсинг HTML. В Playwright падает, если после
`HTTP_MAX_RETRIES` (CI: **6**) попыток офферов нет.

Типичные причины пустого HTTP (смотри `logs/parallel/*.log`):

- `HTTP 429/503` или `сеть: timeout` — fly.pl тормозит при `MONITOR_PARALLEL_JOBS=3`
- `короткий HTML` / нет `card-offer-search` — анти-бот или пустая страница
- `после фильтра 0 офферов` — карточки есть, но не проходят scope (`/wycieczka/turcja`, `transport=accommodation_flight`)

Настройки (config или env):

- `HTTP_MAX_RETRIES` — полные повторы HTTP-скана (default **6**)
- `http_page_retries` — ретраи одной страницы внутри попытки (default **3**)
- `http_retry_delay` / экспоненциальный backoff до `http_retry_delay_max` (45 с)

## На что смотреть в Actions

- Время шага **Run monitoring (filters)** — должно упасть примерно в 2 раза при `jobs=3`
- Время шага **Generate dashboards** — ~3× быстрее при `DASHBOARD_PARALLEL_JOBS=3`
- **Prepare site** — только `hotel_series` + `hotel_images` (не полный `data/filters`)
- Ошибки / пустые CSV — смотреть `logs/parallel/*.log` в артефактах (если добавите upload) или повторить локально
- Частый fallback на Playwright — уменьшить `MONITOR_PARALLEL_JOBS` до `2` или поднять `HTTP_MAX_RETRIES`
