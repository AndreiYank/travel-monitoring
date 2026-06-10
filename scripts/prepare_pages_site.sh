#!/usr/bin/env bash
# Собирает site/ для GitHub Pages: HTML + hotel_series + hotel_images (без тяжёлых CSV).
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"

FILTER_IDS=(
  filter_7_10_days
  filter_13_16_days
  filter_turkey_7_10_days
  filter_turkey_13_16_days
  filter_greece_7_10_days
  filter_greece_13_16_days
)

rm -rf site
mkdir -p site/data/filters

cp -f favicon.svg site/favicon.svg
cp -f hotel-chart.html site/hotel-chart.html
cp -f index.html site/index.html
cp -f index_filter_7_10_days.html site/
cp -f index_filter_13_16_days.html site/
cp -f index_filter_turkey_7_10_days.html site/
cp -f index_filter_turkey_13_16_days.html site/
cp -f index_filter_greece_7_10_days.html site/
cp -f index_filter_greece_13_16_days.html site/

for filter_id in "${FILTER_IDS[@]}"; do
  src="${ROOT}/data/filters/${filter_id}"
  dst="${ROOT}/site/data/filters/${filter_id}"
  mkdir -p "${dst}/hotel_series"
  if [ -d "${src}/hotel_series" ]; then
    cp -R "${src}/hotel_series/." "${dst}/hotel_series/"
  fi
  if [ -f "${src}/hotel_images.json" ]; then
    cp "${src}/hotel_images.json" "${dst}/"
  fi
done

if [ -f "${ROOT}/data/hotel_images.json" ]; then
  cp "${ROOT}/data/hotel_images.json" site/data/
fi

echo "site/ готов ($(du -sh site | awk '{print $1}'))"
