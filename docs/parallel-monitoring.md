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

## На что смотреть в Actions

- Время шага **Run monitoring (filters)** — должно упасть примерно в 2 раза при `jobs=3`
- Время шага **Generate dashboards** — ~3× быстрее при `DASHBOARD_PARALLEL_JOBS=3`
- **Prepare site** — только `hotel_series` + `hotel_images` (не полный `data/filters`)
- Ошибки / пустые CSV — смотреть `logs/parallel/*.log` в артефактах (если добавите upload) или повторить локально
- Частый fallback на Playwright — уменьшить `MONITOR_PARALLEL_JOBS` до `2`
