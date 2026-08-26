#!/usr/bin/env python3
"""Run multiple travel_monitor.py configs in parallel (separate data_dir per filter)."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from filter_trip import load_config_json, should_skip_monitor_config, skip_monitor_reason

DEFAULT_CONFIGS = [
    "config_ci_filter_7_10.json",
    "config_ci_filter_13_16.json",
    "config_ci_filter_egypt_autumn_7_10.json",
    "config_ci_filter_egypt_autumn_7_10_any_airports.json",
    "config_ci_filter_egypt_autumn_13_16.json",
    "config_ci_filter_egypt_autumn_13_16_any_airports.json",
    "config_ci_filter_egypt_ny_dec24_7_10.json",
    "config_ci_filter_egypt_ny_dec28_7_10.json",
    "config_ci_filter_turkey_7_10.json",
    "config_ci_filter_turkey_9_11.json",
    "config_ci_filter_turkey_vacation_jul18.json",
    "config_ci_filter_turkey_13_16.json",
    "config_ci_filter_greece_7_10.json",
    "config_ci_filter_greece_13_16.json",
]


def _run_one(config_name: str, log_dir: Path) -> tuple[str, int, float]:
    config_path = ROOT / config_name
    if not config_path.is_file():
        return config_name, 127, 0.0
    if should_skip_monitor_config(str(config_path)):
        try:
            reason = skip_monitor_reason(load_config_json(str(config_path))) or "пропуск"
        except Exception:
            reason = "пропуск"
        print(f"  ⏭ {config_name} ({reason})")
        return config_name, 0, 0.0

    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{config_path.stem}.log"
    t0 = time.monotonic()
    with log_path.open("w", encoding="utf-8") as log_f:
        log_f.write(
            f"# {config_name}\n"
            f"# started {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"# pid={os.getpid()} jobs_env={os.environ.get('MONITOR_PARALLEL_JOBS', '')}\n"
            f"# HTTP_MAX_RETRIES={os.environ.get('HTTP_MAX_RETRIES', '')}\n\n"
        )
        log_f.flush()
        proc = subprocess.run(
            [sys.executable, str(ROOT / "travel_monitor.py"), "--config", str(config_path)],
            cwd=str(ROOT),
            stdout=log_f,
            stderr=subprocess.STDOUT,
            env=os.environ.copy(),
        )
    elapsed = time.monotonic() - t0
    return config_name, proc.returncode, elapsed


def main() -> int:
    parser = argparse.ArgumentParser(description="Parallel travel_monitor runs")
    parser.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=int(os.environ.get("MONITOR_PARALLEL_JOBS", "3")),
        help="Max concurrent monitors (default: 3, env MONITOR_PARALLEL_JOBS)",
    )
    parser.add_argument(
        "--log-dir",
        default=str(ROOT / "logs" / "parallel"),
        help="Directory for per-config log files",
    )
    parser.add_argument(
        "configs",
        nargs="*",
        default=DEFAULT_CONFIGS,
        help="Config JSON paths (default: all 8 CI filters)",
    )
    args = parser.parse_args()
    jobs = max(1, args.jobs)
    log_dir = Path(args.log_dir)

    print(f"Parallel monitoring: {len(args.configs)} configs, jobs={jobs}")
    print(f"Logs: {log_dir}")
    t0 = time.monotonic()
    failed: list[str] = []

    with ProcessPoolExecutor(max_workers=jobs) as pool:
        futures = {pool.submit(_run_one, cfg, log_dir): cfg for cfg in args.configs}
        for fut in as_completed(futures):
            cfg, code, elapsed = fut.result()
            if code == 0:
                print(f"  ✓ {cfg} ({elapsed:.0f}s)")
            else:
                print(f"  ✗ {cfg} exit={code} ({elapsed:.0f}s) → {log_dir / Path(cfg).stem}.log")
                failed.append(cfg)

    total = time.monotonic() - t0
    print(f"Done in {total:.0f}s — ok {len(args.configs) - len(failed)}/{len(args.configs)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
