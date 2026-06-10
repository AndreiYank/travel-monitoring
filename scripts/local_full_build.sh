#!/usr/bin/env bash
# Полная локальная сборка как в CI (дашборды + landing + site/)
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"

echo "═══════════════════════════════════════════════════════════"
echo "  Travel Price Monitor — полная локальная сборка"
echo "  $(date)"
echo "═══════════════════════════════════════════════════════════"

TOTAL_START=$SECONDS

echo ""
echo "▶ Параллельная генерация дашбордов"
t0=$SECONDS
DASHBOARD_PARALLEL_JOBS="${DASHBOARD_PARALLEL_JOBS:-3}" \
  python3 scripts/run_dashboards_parallel.py -j "${DASHBOARD_PARALLEL_JOBS}"
echo "   ✓ за $((SECONDS - t0)) с"

echo ""
echo "▶ Сборка site/ (как GitHub Pages)"
bash scripts/prepare_pages_site.sh

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  Готово за $((SECONDS - TOTAL_START)) с"
echo "═══════════════════════════════════════════════════════════"
echo ""
echo "Размер site/:"
du -sh site site/data/filters/*/hotel_series 2>/dev/null | head -20
echo ""
echo "Проверка JSON серий:"
for d in site/data/filters/*/hotel_series; do
  [ -d "$d" ] || continue
  n=$(find "$d" -name '*.json' ! -name manifest.json | wc -l | tr -d ' ')
  echo "  $d: $n файлов"
done
echo ""
echo "Локальный просмотр:"
echo "  cd $ROOT/site && python3 -m http.server 8765"
echo "  → http://127.0.0.1:8765/"
echo "  → http://127.0.0.1:8765/index_filter_7_10_days.html"
echo "  → график: hotel-chart.html?filter=filter_7_10_days&hotel=<slug>"
