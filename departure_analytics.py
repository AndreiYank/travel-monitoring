#!/usr/bin/env python3
"""Regional departure cohort analytics."""

from __future__ import annotations

import argparse
import csv
import json
import os
from typing import Any, Dict, List

import pandas as pd

from departure_identity import DEPARTURE_FIELDS, build_departure_identity


BASE_OFFER_FIELDS = [
    "hotel_name",
    "price",
    "dates",
    "duration",
    "rating",
    "departure_airport",
    "scraped_at",
    "url",
    "image_url",
    "offer_url",
]


COHORT_FIELDS = [
    "run_started_at",
    "departure_key",
    "filter_id",
    "country",
    "region",
    "departure_date",
    "return_date",
    "nights",
    "origin_scope",
    "pax_profile",
    "days_to_departure",
    "offer_count",
    "hotel_count",
    "min_price",
    "p10_price",
    "p25_price",
    "median_price",
    "max_price",
    "below_10000_count",
    "below_8000_count",
    "prev_min_price",
    "prev_p10_price",
    "prev_median_price",
    "min_change_pct",
    "p10_change_pct",
    "median_change_pct",
    "hotel_count_delta",
    "hot_score",
]


def load_departure_offers(path: str) -> pd.DataFrame:
    if not os.path.exists(path):
        return pd.DataFrame(columns=BASE_OFFER_FIELDS + DEPARTURE_FIELDS)
    df = pd.read_csv(path, quoting=csv.QUOTE_ALL, on_bad_lines="skip")
    for col in BASE_OFFER_FIELDS + DEPARTURE_FIELDS:
        if col not in df.columns:
            df[col] = ""
    missing_identity = (
        df["departure_key"].fillna("").astype(str).eq("")
        | df["region"].fillna("").astype(str).eq("")
        | df["departure_date"].fillna("").astype(str).eq("")
    )
    if missing_identity.any():
        for idx, row in df[missing_identity].iterrows():
            ident = build_departure_identity(row.to_dict())
            for key, value in ident.items():
                df.at[idx, key] = value
    return df


def assign_scrape_runs(df: pd.DataFrame, gap_minutes: int = 5) -> pd.DataFrame:
    work = df.copy()
    work["_ts"] = pd.to_datetime(work["scraped_at"], errors="coerce", utc=True)
    work = work.dropna(subset=["_ts"]).sort_values("_ts").reset_index(drop=True)
    if work.empty:
        return work.assign(run_started_at=pd.Series(dtype="object"))
    gap = work["_ts"].diff().dt.total_seconds().fillna(0) > (gap_minutes * 60)
    work["_run_id"] = gap.cumsum()
    run_starts = work.groupby("_run_id")["_ts"].transform("min")
    work["run_started_at"] = run_starts.dt.strftime("%Y-%m-%dT%H:%M:%S%z")
    return work


def _hot_score(row: Dict[str, Any]) -> int:
    """Score only the late price-drop signal, not absolute cheapness or breadth."""
    days = row.get("days_to_departure")
    try:
        days_f = float(days)
    except (TypeError, ValueError):
        days_f = None

    if days_f is None or days_f < 0 or days_f > 8:
        return 0

    try:
        p10_change = float(row.get("p10_change_pct") or 0)
    except (TypeError, ValueError):
        p10_change = 0
    try:
        median_change = float(row.get("median_change_pct") or 0)
    except (TypeError, ValueError):
        median_change = 0
    try:
        min_change = float(row.get("min_change_pct") or 0)
    except (TypeError, ValueError):
        min_change = 0

    best_drop = max(
        abs(median_change) * 6.0 if median_change <= -3.0 else 0,
        abs(p10_change) * 4.5 if p10_change <= -5.0 else 0,
        abs(min_change) * 2.0 if min_change <= -8.0 else 0,
    )
    if best_drop <= 0:
        return 0

    proximity_bonus = 0
    if days_f <= 2:
        proximity_bonus = 18
    elif days_f <= 5:
        proximity_bonus = 12
    elif days_f <= 8:
        proximity_bonus = 8

    score = int(round(best_drop + proximity_bonus))

    # If the departure is getting more expensive right now, it may still be
    # close/cheap, but it should not look like a strong "burning" signal.
    if p10_change > 1.0 or median_change > 1.0:
        score = min(score, 60)
    if p10_change > 5.0 or median_change > 5.0:
        score = min(score, 45)
    return min(100, score)


def _add_change_columns(cohorts: pd.DataFrame) -> pd.DataFrame:
    if cohorts.empty:
        return cohorts
    work = cohorts.copy()
    work["_run_ts"] = pd.to_datetime(work["run_started_at"], errors="coerce", utc=True)
    work = work.sort_values(["departure_key", "_run_ts"]).reset_index(drop=True)
    grp = work.groupby("departure_key", sort=False)

    def pct_change(col: str) -> pd.Series:
        prev = grp[col].shift(1)
        return ((work[col] - prev) / prev * 100.0).where(prev > 0).fillna(0.0)

    work["min_change_pct"] = pct_change("min_price").round(2)
    work["p10_change_pct"] = pct_change("p10_price").round(2)
    work["median_change_pct"] = pct_change("median_price").round(2)
    work["prev_min_price"] = grp["min_price"].shift(1).fillna(0).round(2)
    work["prev_p10_price"] = grp["p10_price"].shift(1).fillna(0).round(2)
    work["prev_median_price"] = grp["median_price"].shift(1).fillna(0).round(2)
    work["hotel_count_delta"] = (work["hotel_count"] - grp["hotel_count"].shift(1)).fillna(0).astype(int)
    work["hot_score"] = work.apply(lambda row: _hot_score(row.to_dict()), axis=1)
    return work.drop(columns=["_run_ts"])


def build_cohort_snapshots(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=COHORT_FIELDS)
    work = assign_scrape_runs(df)
    work["price"] = pd.to_numeric(work["price"], errors="coerce")
    work["nights"] = pd.to_numeric(work["nights"], errors="coerce")
    work = work.dropna(subset=["price"])
    work = work[
        work["departure_key"].fillna("").astype(str).ne("")
        & work["region"].fillna("").astype(str).ne("")
        & work["departure_date"].fillna("").astype(str).ne("")
    ].copy()
    if work.empty:
        return pd.DataFrame(columns=COHORT_FIELDS)

    rows: List[Dict[str, Any]] = []
    group_cols = ["run_started_at", "departure_key"]
    for (run_started_at, departure_key), grp in work.groupby(group_cols, sort=True):
        prices = grp["price"].astype(float)
        first = grp.iloc[0]
        try:
            dep = pd.to_datetime(first["departure_date"], errors="coerce")
            run_ts = pd.to_datetime(run_started_at, errors="coerce", utc=True)
            days_to_departure = int((dep.date() - run_ts.date()).days) if pd.notna(dep) else ""
        except Exception:
            days_to_departure = ""
        row = {
            "run_started_at": run_started_at,
            "departure_key": departure_key,
            "filter_id": first.get("filter_id", ""),
            "country": first.get("country", ""),
            "region": first.get("region", ""),
            "departure_date": first.get("departure_date", ""),
            "return_date": first.get("return_date", ""),
            "nights": int(first["nights"]) if pd.notna(first.get("nights")) else "",
            "origin_scope": first.get("origin_scope", ""),
            "pax_profile": first.get("pax_profile", ""),
            "days_to_departure": days_to_departure,
            "offer_count": int(len(grp)),
            "hotel_count": int(grp["hotel_name"].fillna("").astype(str).nunique()),
            "min_price": round(float(prices.min()), 2),
            "p10_price": round(float(prices.quantile(0.10)), 2),
            "p25_price": round(float(prices.quantile(0.25)), 2),
            "median_price": round(float(prices.median()), 2),
            "max_price": round(float(prices.max()), 2),
            "below_10000_count": int((prices <= 10000).sum()),
            "below_8000_count": int((prices <= 8000).sum()),
        }
        row["min_change_pct"] = 0.0
        row["p10_change_pct"] = 0.0
        row["median_change_pct"] = 0.0
        row["prev_min_price"] = 0.0
        row["prev_p10_price"] = 0.0
        row["prev_median_price"] = 0.0
        row["hotel_count_delta"] = 0
        row["hot_score"] = 0
        rows.append(row)
    return _add_change_columns(pd.DataFrame(rows, columns=COHORT_FIELDS))


def _offer_payload(row: pd.Series) -> Dict[str, Any]:
    return {
        "hotel_name": str(row.get("hotel_name") or ""),
        "price": round(float(row.get("price") or 0), 2),
        "dates": str(row.get("dates") or ""),
        "duration": str(row.get("duration") or ""),
        "offer_url": str(row.get("offer_url") or ""),
        "image_url": str(row.get("image_url") or ""),
    }


def build_departure_offers_index(
    df: pd.DataFrame,
    departure_keys: List[str],
    preferred_runs: Dict[str, str] | None = None,
) -> Dict[str, Dict[str, Any]]:
    """Map departure_key -> hotel offers for a chosen scrape run."""
    preferred_runs = preferred_runs or {}
    keys = [str(key) for key in departure_keys if str(key)]
    if not keys or df.empty:
        return {}

    work = assign_scrape_runs(df)
    work["price"] = pd.to_numeric(work["price"], errors="coerce")
    work = work.dropna(subset=["price"])
    work = work[work["departure_key"].fillna("").astype(str).isin(keys)].copy()
    if work.empty:
        return {}

    work["_run_ts"] = pd.to_datetime(work["run_started_at"], errors="coerce", utc=True)
    index: Dict[str, Dict[str, Any]] = {}

    for departure_key, grp in work.groupby("departure_key", sort=False):
        preferred = preferred_runs.get(departure_key)
        if preferred:
            run_grp = grp[grp["run_started_at"].astype(str) == str(preferred)]
            if run_grp.empty:
                pref_ts = pd.to_datetime(preferred, errors="coerce", utc=True)
                if pd.notna(pref_ts):
                    delta = (grp["_run_ts"] - pref_ts).abs()
                    closest_run = grp.loc[delta.idxmin(), "run_started_at"]
                    run_grp = grp[grp["run_started_at"] == closest_run]
                else:
                    run_grp = grp[grp["_run_ts"] == grp["_run_ts"].max()]
        else:
            latest_ts = grp["_run_ts"].max()
            run_grp = grp[grp["_run_ts"] == latest_ts]

        if run_grp.empty:
            continue

        deduped = (
            run_grp.sort_values("price")
            .drop_duplicates(subset=["hotel_name"], keep="first")
            .sort_values("price")
        )
        first = run_grp.iloc[0]
        index[departure_key] = {
            "departure_key": departure_key,
            "region": str(first.get("region") or ""),
            "departure_date": str(first.get("departure_date") or ""),
            "return_date": str(first.get("return_date") or ""),
            "nights": int(first["nights"]) if pd.notna(first.get("nights")) else "",
            "run_started_at": str(first.get("run_started_at") or ""),
            "offers": [_offer_payload(row) for _, row in deduped.iterrows()],
        }
    return index


def build_departure_events(cohorts: pd.DataFrame) -> List[Dict[str, Any]]:
    if cohorts.empty:
        return []
    work = cohorts.copy()
    work["_run_ts"] = pd.to_datetime(work["run_started_at"], errors="coerce", utc=True)
    work = work.sort_values(["departure_key", "_run_ts"])
    events: List[Dict[str, Any]] = []
    for departure_key, grp in work.groupby("departure_key", sort=False):
        prev = None
        for _, row in grp.iterrows():
            current = row.to_dict()
            if prev is None:
                prev = current
                continue
            event = _event_from_pair(departure_key, prev, current)
            if event:
                events.append(event)
            prev = current
    return events


def build_hot_departure_history(cohorts: pd.DataFrame) -> List[Dict[str, Any]]:
    """Season archive of completed departures with a real late-buy hot signal."""
    if cohorts.empty:
        return []
    work = cohorts.copy()
    work["_run_ts"] = pd.to_datetime(work["run_started_at"], errors="coerce", utc=True)
    work = work.dropna(subset=["_run_ts"])
    if work.empty:
        return []
    for col in [
        "hot_score", "days_to_departure", "min_price", "p10_price", "median_price",
        "hotel_count", "below_10000_count", "p10_change_pct", "median_change_pct",
    ]:
        if col in work.columns:
            work[col] = pd.to_numeric(work[col], errors="coerce")
    as_of_date = work["_run_ts"].max().date()
    work["_departure_dt"] = pd.to_datetime(work["departure_date"], errors="coerce")

    history: List[Dict[str, Any]] = []
    for departure_key, grp in work.sort_values("_run_ts").groupby("departure_key", sort=False):
        if grp.empty:
            continue
        departure_dt = grp["_departure_dt"].dropna()
        if departure_dt.empty or departure_dt.iloc[0].date() > as_of_date:
            # История — только уже наступившие вылеты; будущие остаются в текущем блоке.
            continue

        late_window = grp["days_to_departure"].fillna(9999) <= 7
        cheap_enough = grp["p10_price"].fillna(float("inf")) <= 10000
        moderate_drop = (
            (grp["hotel_count"].fillna(0) >= 3)
            & (
                (grp["p10_change_pct"].fillna(0) <= -10)
                | (grp["median_change_pct"].fillna(0) <= -8)
            )
        )
        strong_drop = (
            (grp["hotel_count"].fillna(0) >= 2)
            & (
                (grp["p10_change_pct"].fillna(0) <= -15)
                | (grp["median_change_pct"].fillna(0) <= -12)
            )
        )
        hot_mask = (
            late_window
            & cheap_enough
            & (moderate_drop | strong_drop)
        )
        if not hot_mask.any():
            continue

        hot_grp = grp[hot_mask].copy()
        # Best moment among real late-buy rows. Lower p10 breaks ties.
        ranked = grp.assign(
            _rank_score=(
                grp["hot_score"].fillna(0) * 1000
                - grp["p10_price"].fillna(999999) / 10
                - grp["days_to_departure"].fillna(9999)
            )
        )
        ranked = ranked.loc[hot_grp.index]
        best = ranked.sort_values(["_rank_score", "p10_price"], ascending=[False, True]).iloc[0]
        first = grp.iloc[0]
        last = grp.iloc[-1]
        max_p10_drop = float(grp["p10_change_pct"].min()) if "p10_change_pct" in grp else 0.0
        max_median_drop = float(grp["median_change_pct"].min()) if "median_change_pct" in grp else 0.0

        history.append({
            "departure_key": departure_key,
            "country": best.get("country", ""),
            "region": best.get("region", ""),
            "departure_date": best.get("departure_date", ""),
            "return_date": best.get("return_date", ""),
            "nights": int(best["nights"]) if pd.notna(best.get("nights")) else "",
            "first_seen_at": first.get("run_started_at", ""),
            "last_seen_at": last.get("run_started_at", ""),
            "best_seen_at": best.get("run_started_at", ""),
            "days_to_departure_at_best": int(best["days_to_departure"]) if pd.notna(best.get("days_to_departure")) else "",
            "best_min_price": round(float(best.get("min_price") or 0), 2),
            "best_p10_price": round(float(best.get("p10_price") or 0), 2),
            "best_median_price": round(float(best.get("median_price") or 0), 2),
            "best_prev_min_price": round(float(best.get("prev_min_price") or 0), 2),
            "best_prev_p10_price": round(float(best.get("prev_p10_price") or 0), 2),
            "best_prev_median_price": round(float(best.get("prev_median_price") or 0), 2),
            "max_hot_score": int(grp["hot_score"].max()) if pd.notna(grp["hot_score"].max()) else 0,
            "best_p10_change_pct": round(float(best.get("p10_change_pct") or 0), 2),
            "best_median_change_pct": round(float(best.get("median_change_pct") or 0), 2),
            "max_p10_drop_pct": round(max_p10_drop, 2),
            "max_median_drop_pct": round(max_median_drop, 2),
            "max_hotel_count": int(grp["hotel_count"].max()) if pd.notna(grp["hotel_count"].max()) else 0,
            "max_below_10000_count": int(grp["below_10000_count"].max()) if pd.notna(grp["below_10000_count"].max()) else 0,
            "hot_runs_count": int(hot_mask.sum()),
            "observations": int(len(grp)),
        })

    history.sort(
        key=lambda x: (
            x["max_hot_score"],
            -float(x["days_to_departure_at_best"] or 999),
            -float(x["best_p10_price"] or 999999),
        ),
        reverse=True,
    )
    return history


def _pct(old: Any, new: Any) -> float:
    try:
        old_f = float(old)
        new_f = float(new)
        if old_f <= 0:
            return 0.0
        return (new_f - old_f) / old_f * 100.0
    except (TypeError, ValueError):
        return 0.0


def _event_from_pair(departure_key: str, prev: Dict[str, Any], curr: Dict[str, Any]):
    min_pct = _pct(prev.get("min_price"), curr.get("min_price"))
    p10_pct = _pct(prev.get("p10_price"), curr.get("p10_price"))
    median_pct = _pct(prev.get("median_price"), curr.get("median_price"))
    hotel_delta = int(curr.get("hotel_count") or 0) - int(prev.get("hotel_count") or 0)

    is_drop = min_pct <= -8.0 or p10_pct <= -8.0 or median_pct <= -5.0
    is_broad = int(curr.get("hotel_count") or 0) >= 3
    if not (is_drop and is_broad):
        return None

    days = curr.get("days_to_departure")
    try:
        days_f = float(days)
    except (TypeError, ValueError):
        days_f = None

    event_type = "mass_price_drop"
    severity = "medium"
    if days_f is not None and days_f <= 7:
        event_type = "late_price_dump"
        severity = "high"
    if int(curr.get("hot_score") or 0) >= 70:
        severity = "high"

    return {
        "event_time": curr.get("run_started_at", ""),
        "departure_key": departure_key,
        "event_type": event_type,
        "severity": severity,
        "country": curr.get("country", ""),
        "region": curr.get("region", ""),
        "departure_date": curr.get("departure_date", ""),
        "return_date": curr.get("return_date", ""),
        "nights": curr.get("nights", ""),
        "days_to_departure": curr.get("days_to_departure", ""),
        "old_min_price": prev.get("min_price", ""),
        "new_min_price": curr.get("min_price", ""),
        "min_drop_pct": round(min_pct, 2),
        "p10_drop_pct": round(p10_pct, 2),
        "median_drop_pct": round(median_pct, 2),
        "hotel_count": curr.get("hotel_count", ""),
        "hotel_delta": hotel_delta,
        "hot_score": curr.get("hot_score", ""),
    }


def write_departure_analytics(input_csv: str, output_dir: str) -> dict:
    df = load_departure_offers(input_csv)
    cohorts = build_cohort_snapshots(df)
    events = build_departure_events(cohorts)
    hot_history = build_hot_departure_history(cohorts)

    os.makedirs(output_dir, exist_ok=True)
    cohorts_path = os.path.join(output_dir, "departure_cohorts.csv")
    events_path = os.path.join(output_dir, "departure_events.json")
    history_path = os.path.join(output_dir, "departure_hot_history.json")
    cohorts.to_csv(cohorts_path, index=False, quoting=csv.QUOTE_ALL)
    with open(events_path, "w", encoding="utf-8") as f:
        json.dump({"events": events}, f, ensure_ascii=False, indent=2)
    with open(history_path, "w", encoding="utf-8") as f:
        json.dump({"departures": hot_history}, f, ensure_ascii=False, indent=2)
    return {
        "offers": int(len(df)),
        "cohorts": int(len(cohorts)),
        "events": int(len(events)),
        "hot_history": int(len(hot_history)),
        "cohorts_path": cohorts_path,
        "events_path": events_path,
        "history_path": history_path,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build regional departure cohort analytics")
    parser.add_argument("input_csv", help="departure_offers.csv or legacy travel_prices.csv")
    parser.add_argument("--output-dir", default=None, help="Directory for departure_cohorts/events")
    args = parser.parse_args()
    output_dir = args.output_dir or os.path.dirname(args.input_csv) or "."
    result = write_departure_analytics(args.input_csv, output_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
