#!/usr/bin/env python3
"""Generate filter dashboards in parallel, then landing page."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

DEFAULT_DASHBOARDS = [
    {
        "data": "data/filters/filter_7_10_days/travel_prices.csv",
        "output": "index_filter_7_10_days.html",
        "title": "Мониторинг цен • Египет • 7–10 дней",
        "charts": "hotel-charts/filter_7_10_days",
        "alerts": "data/filters/filter_7_10_days/travel_prices_alerts.json",
    },
    {
        "data": "data/filters/filter_13_16_days/travel_prices.csv",
        "output": "index_filter_13_16_days.html",
        "title": "Мониторинг цен • Египет • 13–16 дней",
        "charts": "hotel-charts/filter_13_16_days",
        "alerts": "data/filters/filter_13_16_days/travel_prices_alerts.json",
    },
    {
        "data": "data/filters/filter_turkey_7_10_days/travel_prices.csv",
        "output": "index_filter_turkey_7_10_days.html",
        "title": "Мониторинг цен • Турция • 7–10 дней",
        "charts": "hotel-charts/filter_turkey_7_10_days",
        "alerts": "data/filters/filter_turkey_7_10_days/travel_prices_alerts.json",
    },
    {
        "data": "data/filters/filter_turkey_9_11_days/travel_prices.csv",
        "output": "index_filter_turkey_9_11_days.html",
        "title": "Мониторинг цен • Турция • 9–11 дней",
        "charts": "hotel-charts/filter_turkey_9_11_days",
        "alerts": "data/filters/filter_turkey_9_11_days/travel_prices_alerts.json",
    },
    {
        "data": "data/filters/filter_turkey_vacation_jul18_2026/travel_prices.csv",
        "output": "index_filter_turkey_vacation_jul18.html",
        "title": "Мониторинг цен • Турция • отпуск 18 июля 2026",
        "charts": "hotel-charts/filter_turkey_vacation_jul18_2026",
        "alerts": "data/filters/filter_turkey_vacation_jul18_2026/travel_prices_alerts.json",
        "config": "config_ci_filter_turkey_vacation_jul18.json",
    },
    {
        "data": "data/filters/filter_turkey_13_16_days/travel_prices.csv",
        "output": "index_filter_turkey_13_16_days.html",
        "title": "Мониторинг цен • Турция • 13–16 дней",
        "charts": "hotel-charts/filter_turkey_13_16_days",
        "alerts": "data/filters/filter_turkey_13_16_days/travel_prices_alerts.json",
    },
    {
        "data": "data/filters/filter_greece_7_10_days/travel_prices.csv",
        "output": "index_filter_greece_7_10_days.html",
        "title": "Мониторинг цен • Греция • 7–10 дней",
        "charts": "hotel-charts/filter_greece_7_10_days",
        "alerts": "data/filters/filter_greece_7_10_days/travel_prices_alerts.json",
    },
    {
        "data": "data/filters/filter_greece_13_16_days/travel_prices.csv",
        "output": "index_filter_greece_13_16_days.html",
        "title": "Мониторинг цен • Греция • 13–16 дней",
        "charts": "hotel-charts/filter_greece_13_16_days",
        "alerts": "data/filters/filter_greece_13_16_days/travel_prices_alerts.json",
    },
]


def _run_one(spec: dict, log_dir: Path) -> tuple[str, int, float]:
    output = spec["output"]
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{Path(output).stem}.log"
    cmd = [
        sys.executable,
        str(ROOT / "generate_inline_charts_dashboard.py"),
        "--data-file",
        spec["data"],
        "--output",
        output,
        "--title",
        spec["title"],
        "--charts-dir",
        spec["charts"],
        "--alerts-file",
        spec["alerts"],
        "--display-price-ceiling",
        "10000",
        "--history-price-ceiling",
        "20000",
    ]
    if spec.get("config"):
        cmd.extend(["--config-file", spec["config"]])
    t0 = time.monotonic()
    with log_path.open("w", encoding="utf-8") as log_f:
        log_f.write(f"# {output}\n# started {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        log_f.flush()
        proc = subprocess.run(
            cmd,
            cwd=str(ROOT),
            stdout=log_f,
            stderr=subprocess.STDOUT,
            env=os.environ.copy(),
        )
    return output, proc.returncode, time.monotonic() - t0


def main() -> int:
    parser = argparse.ArgumentParser(description="Parallel dashboard generation")
    parser.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=int(os.environ.get("DASHBOARD_PARALLEL_JOBS", "3")),
        help="Max concurrent generators (default: 3, env DASHBOARD_PARALLEL_JOBS)",
    )
    parser.add_argument(
        "--log-dir",
        default=str(ROOT / "logs" / "parallel" / "dashboards"),
        help="Directory for per-dashboard log files",
    )
    parser.add_argument(
        "--skip-landing",
        action="store_true",
        help="Only generate filter dashboards, not index.html landing",
    )
    args = parser.parse_args()
    jobs = max(1, args.jobs)
    log_dir = Path(args.log_dir)

    print(f"Parallel dashboards: {len(DEFAULT_DASHBOARDS)} filters, jobs={jobs}")
    print(f"Logs: {log_dir}")
    t0 = time.monotonic()
    failed: list[str] = []

    with ProcessPoolExecutor(max_workers=jobs) as pool:
        futures = {
            pool.submit(_run_one, spec, log_dir): spec["output"]
            for spec in DEFAULT_DASHBOARDS
        }
        for fut in as_completed(futures):
            output, code, elapsed = fut.result()
            if code == 0:
                print(f"  ✓ {output} ({elapsed:.0f}s)")
            else:
                print(f"  ✗ {output} exit={code} ({elapsed:.0f}s)")
                failed.append(output)

    if failed:
        print(f"Dashboard generation failed: {', '.join(failed)}")
        return 1

    if not args.skip_landing:
        print("▶ generate_landing.py")
        landing_t0 = time.monotonic()
        proc = subprocess.run(
            [sys.executable, str(ROOT / "generate_landing.py")],
            cwd=str(ROOT),
        )
        if proc.returncode != 0:
            print("✗ generate_landing.py failed")
            return proc.returncode
        print(f"  ✓ landing ({time.monotonic() - landing_t0:.0f}s)")

    print(f"Done in {time.monotonic() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
