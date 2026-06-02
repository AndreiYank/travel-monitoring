#!/usr/bin/env bash
# Полная локальная сборка как в CI (дашборды + landing + site/)
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"

echo "═══════════════════════════════════════════════════════════"
echo "  Travel Price Monitor — полная локальная сборка"
echo "  $(date)"
echo "═══════════════════════════════════════════════════════════"

gen_dashboard() {
  local data="$1" output="$2" title="$3" charts="$4" alerts="$5"
  echo ""
  echo "▶ $output"
  local t0=$SECONDS
  python3 generate_inline_charts_dashboard.py \
    --data-file "$data" \
    --output "$output" \
    --title "$title" \
    --charts-dir "$charts" \
    --alerts-file "$alerts" \
    --display-price-ceiling 10000 \
    --history-price-ceiling 20000
  echo "   ✓ за $((SECONDS - t0)) с"
}

TOTAL_START=$SECONDS

gen_dashboard \
  data/filters/filter_7_10_days/travel_prices.csv \
  index_filter_7_10_days.html \
  "Мониторинг цен • Египет • 7–10 дней" \
  hotel-charts/filter_7_10_days \
  data/filters/filter_7_10_days/travel_prices_alerts.json

gen_dashboard \
  data/filters/filter_13_16_days/travel_prices.csv \
  index_filter_13_16_days.html \
  "Мониторинг цен • Египет • 13–16 дней" \
  hotel-charts/filter_13_16_days \
  data/filters/filter_13_16_days/travel_prices_alerts.json

gen_dashboard \
  data/filters/filter_turkey_7_10_days/travel_prices.csv \
  index_filter_turkey_7_10_days.html \
  "Мониторинг цен • Турция • 7–10 дней" \
  hotel-charts/filter_turkey_7_10_days \
  data/filters/filter_turkey_7_10_days/travel_prices_alerts.json

gen_dashboard \
  data/filters/filter_turkey_13_16_days/travel_prices.csv \
  index_filter_turkey_13_16_days.html \
  "Мониторинг цен • Турция • 13–16 дней" \
  hotel-charts/filter_turkey_13_16_days \
  data/filters/filter_turkey_13_16_days/travel_prices_alerts.json

gen_dashboard \
  data/filters/filter_greece_7_10_days/travel_prices.csv \
  index_filter_greece_7_10_days.html \
  "Мониторинг цен • Греция • 7–10 дней" \
  hotel-charts/filter_greece_7_10_days \
  data/filters/filter_greece_7_10_days/travel_prices_alerts.json

gen_dashboard \
  data/filters/filter_greece_13_16_days/travel_prices.csv \
  index_filter_greece_13_16_days.html \
  "Мониторинг цен • Греция • 13–16 дней" \
  hotel-charts/filter_greece_13_16_days \
  data/filters/filter_greece_13_16_days/travel_prices_alerts.json

echo ""
echo "▶ generate_landing.py"
t0=$SECONDS
python3 generate_landing.py
echo "   ✓ за $((SECONDS - t0)) с"

echo ""
echo "▶ Сборка site/ (как GitHub Pages)"
rm -rf site
mkdir -p site/data
cp -f favicon.svg site/favicon.svg
cp -f hotel-chart.html site/hotel-chart.html
cp -f index.html site/index.html
cp -f index_filter_7_10_days.html site/
cp -f index_filter_13_16_days.html site/
cp -f index_filter_turkey_7_10_days.html site/
cp -f index_filter_turkey_13_16_days.html site/
cp -f index_filter_greece_7_10_days.html site/
cp -f index_filter_greece_13_16_days.html site/
cp -R data/filters site/data/

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
