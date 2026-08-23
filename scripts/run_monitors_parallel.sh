#!/usr/bin/env bash
# Параллельный скрапинг всех CI-фильтров (см. run_monitors_parallel.py).
# Пример: MONITOR_PARALLEL_JOBS=6 ./scripts/run_monitors_parallel.sh
set -euo pipefail
cd "$(dirname "$0")/.."
exec python3 scripts/run_monitors_parallel.py "$@"
