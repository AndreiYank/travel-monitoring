#!/usr/bin/env bash
# Собирает site/ для GitHub Pages: HTML + hotel_series + hotel_images (без тяжёлых CSV).
# Retired / expired trip filters are omitted from the published site.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"

# shellcheck disable=SC2016
ACTIVE_JSON="$(python3 - <<'PY'
import json
from filter_registry import active_filter_groups, FILTER_GROUPS
from filter_params import DATA_DIR_CONFIG_FILES

active_hrefs = {flt["href"] for _, flt in ((g, f) for g in active_filter_groups() for f in g["filters"])}
active_data_dirs = []
for group in active_filter_groups():
    for flt in group["filters"]:
        charts = flt.get("charts_subdir") or ""
        # hotel-charts/filter_X -> filter_X
        data_id = charts.rsplit("/", 1)[-1] if charts else ""
        if data_id:
            active_data_dirs.append(data_id)

print(json.dumps({"hrefs": sorted(active_hrefs), "data_dirs": sorted(set(active_data_dirs))}))
PY
)"

mapfile -t ACTIVE_HREFS < <(python3 -c 'import json,sys; print("\n".join(json.loads(sys.argv[1])["hrefs"]))' "$ACTIVE_JSON")
mapfile -t FILTER_IDS < <(python3 -c 'import json,sys; print("\n".join(json.loads(sys.argv[1])["data_dirs"]))' "$ACTIVE_JSON")

rm -rf site
mkdir -p site/data/filters

cp -f favicon.svg site/favicon.svg
cp -f hotel-chart.html site/hotel-chart.html
cp -f index.html site/index.html

for href in "${ACTIVE_HREFS[@]}"; do
  if [ -f "${ROOT}/${href}" ]; then
    cp -f "${ROOT}/${href}" site/
  else
    echo "⚠️ skip missing ${href}"
  fi
done

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

echo "site/ готов ($(du -sh site | awk '{print $1}')) — фильтров: ${#FILTER_IDS[@]}"
echo "HTML: ${ACTIVE_HREFS[*]}"
