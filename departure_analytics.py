#!/usr/bin/env python3
"""Regional departure cohort analytics."""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import time
from datetime import timedelta
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

# Minimum shared hotels between consecutive runs to trust a move signal.
MIN_COMMON_HOTELS = 8
# Minimum hotels in a departure to score deal-based heat.
MIN_DEAL_HOTELS = 3
# Cohort price move: compare to scrape run closest to this many hours ago.
COHORT_LOOKBACK_TARGET_HOURS = 24
COHORT_LOOKBACK_MIN_HOURS = 12
COHORT_LOOKBACK_MAX_HOURS = 36
# Price timeline on hot-departure modal: D-N … D-0 (day of departure).
HOT_DEPARTURE_CHART_DAYS_MAX = 14
# Cheap tier = mean of the bottom bucket (min 3 hotels, ~25% of the common set).
CHEAP_TIER_MIN_HOTELS = 3
CHEAP_TIER_SHARE = 0.25

from departure_identity import DEPARTURE_FIELDS, build_departure_identity
from departure_airports import hub_regions_subtitle, parse_hub_departure_key, turkey_hub_label, turkey_hub_regions
from hotel_deal_score import compute_hotel_deal_metrics

logger = logging.getLogger(__name__)


def _log_timing(label: str, started: float, extra: str = "") -> float:
    elapsed = time.monotonic() - started
    suffix = f" | {extra}" if extra else ""
    logger.info(f"⏱ departure_analytics {label}: {elapsed:.2f}s{suffix}")
    return elapsed


BASE_OFFER_FIELDS = [
    "hotel_name",
    "price",
    "dates",
    "duration",
    "rating",
    "ta_rating",
    "ta_review_count",
    "ta_source",
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
    "common_hotel_count",
    "avg_deal_score",
    "mean_avg_delta_pct",
    "hot_deal_count",
    "good_deal_count",
    "median_ta_rating",
    "ta_rated_hotel_count",
    "hot_score",
]


def cheap_tier_bucket_size(hotel_count: Any) -> int:
    try:
        n = int(hotel_count or 0)
    except (TypeError, ValueError):
        return 0
    if n < MIN_COMMON_HOTELS:
        return 0
    return max(CHEAP_TIER_MIN_HOTELS, min(n, int(round(n * CHEAP_TIER_SHARE))))


def cheap_tier_label(common_hotel_count: Any) -> str:
    """UI label for the low-price tier used in run-to-run change."""
    bucket = cheap_tier_bucket_size(common_hotel_count)
    if bucket <= 0:
        return "нижний сегмент"
    return f"нижние {bucket}"


def _nights_series_eq(series: pd.Series, expected: Any) -> pd.Series:
    """Match nights across int/float/str (7, 7.0, \"7\")."""
    vals = pd.to_numeric(series, errors="coerce")
    exp = pd.to_numeric(expected, errors="coerce")
    if pd.notna(exp):
        return vals.eq(exp)
    return series.fillna("").astype(str).str.strip().eq(str(expected or "").strip())


def load_combined_departure_offers(
    data_dir: str,
    travel_prices_file: str | None = None,
) -> pd.DataFrame:
    """Raw offers for modals: prefer departure_offers.csv + archive/, backfill from travel_prices."""
    frames: List[pd.DataFrame] = []
    offers_path = os.path.join(data_dir, "departure_offers.csv")

    # Сначала загружаем архивные файлы прошлых месяцев (отсортировано по имени = по хронологии)
    archive_dir = os.path.join(data_dir, "archive")
    if os.path.isdir(archive_dir):
        archive_files = sorted(
            f for f in os.listdir(archive_dir)
            if f.startswith("departure_offers_") and f.endswith(".csv")
        )
        for af in archive_files:
            frames.append(load_departure_offers(os.path.join(archive_dir, af)))

    # Затем текущий месяц
    if os.path.exists(offers_path):
        frames.append(load_departure_offers(offers_path))

    if travel_prices_file and os.path.exists(travel_prices_file):
        frames.append(load_departure_offers(travel_prices_file))
    if not frames:
        return pd.DataFrame()
    combined = pd.concat(frames, ignore_index=True, sort=False)
    dedupe_cols = [c for c in ["hotel_name", "scraped_at", "departure_key", "price"] if c in combined.columns]
    if dedupe_cols:
        combined = combined.drop_duplicates(subset=dedupe_cols, keep="first")
    return combined



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
        sub_records = df[missing_identity].to_dict("records")
        ident_records = [build_departure_identity(r) for r in sub_records]
        if ident_records:
            ident_df = pd.DataFrame(ident_records, index=df[missing_identity].index)
            for key in ident_df.columns:
                df.loc[missing_identity, key] = ident_df[key]
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


def _proximity_bonus(days_f: float | None) -> int:
    if days_f is None:
        return 0
    if days_f <= 2:
        return 18
    if days_f <= 5:
        return 12
    if days_f <= 8:
        return 8
    return 0


def _days_to_departure_float(row: Dict[str, Any]) -> float | None:
    days = row.get("days_to_departure")
    try:
        return float(days)
    except (TypeError, ValueError):
        return None


def _hot_score_run_delta(row: Dict[str, Any]) -> int:
    """Heat from cohort move vs scrape run ~24h ago (stable intersection)."""
    days_f = _days_to_departure_float(row)
    if days_f is None or days_f < 0 or days_f > 8:
        return 0

    try:
        common_n = int(row.get("common_hotel_count") or 0)
    except (TypeError, ValueError):
        common_n = 0
    if common_n < MIN_COMMON_HOTELS:
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

    cheap_tier_drop = abs(p10_change) * 4.5 if p10_change <= -5.0 else 0
    best_drop = max(
        abs(median_change) * 6.0 if median_change <= -3.0 else 0,
        cheap_tier_drop,
        abs(min_change) * 2.0 if min_change <= -8.0 else 0,
    )
    if best_drop <= 0:
        return 0

    score = int(round(best_drop + _proximity_bonus(days_f)))
    if p10_change > 1.0 or median_change > 1.0:
        score = min(score, 60)
    if p10_change > 5.0 or median_change > 5.0:
        score = min(score, 45)
    return min(100, score)


def _hot_score_deal_aggregate(row: Dict[str, Any]) -> int:
    """Heat when many hotels on this departure are below their typical price (Deal / Δ ср.)."""
    days_f = _days_to_departure_float(row)
    if days_f is None or days_f < 0 or days_f > 8:
        return 0

    try:
        hotel_count = int(row.get("hotel_count") or 0)
    except (TypeError, ValueError):
        hotel_count = 0
    if hotel_count < MIN_DEAL_HOTELS:
        return 0

    avg_deal = float(row.get("avg_deal_score") or 0)
    mean_delta = float(row.get("mean_avg_delta_pct") or 0)
    hot_deals = int(row.get("hot_deal_count") or 0)
    good_deals = int(row.get("good_deal_count") or 0)

    if mean_delta > 3.0 and avg_deal < 55:
        return 0

    delta_part = 0.0
    if mean_delta <= -10:
        delta_part = min(42, abs(mean_delta) * 3.2)
    elif mean_delta <= -6:
        delta_part = min(28, abs(mean_delta) * 2.8)
    elif mean_delta <= -3:
        delta_part = min(12, abs(mean_delta) * 2.0)

    deal_part = 0.0
    if avg_deal >= 80:
        deal_part = min(32, (avg_deal - 55) * 0.9)
    elif avg_deal >= 70:
        deal_part = min(22, (avg_deal - 55) * 0.7)
    elif avg_deal >= 65:
        deal_part = min(14, (avg_deal - 55) * 0.5)

    share_hot = hot_deals / hotel_count if hotel_count else 0.0
    share_good = (hot_deals + good_deals) / hotel_count if hotel_count else 0.0

    try:
        median_ta = float(row.get("median_ta_rating") or 0)
        ta_rated = int(row.get("ta_rated_hotel_count") or 0)
    except (TypeError, ValueError):
        median_ta = 0.0
        ta_rated = 0
    ta_part = 0.0
    if ta_rated >= MIN_DEAL_HOTELS and median_ta >= 4.0:
        ta_part = min(12, (median_ta - 3.8) * 18)
    elif ta_rated >= MIN_DEAL_HOTELS and median_ta > 0 and median_ta < 3.5:
        ta_part = -min(8, (3.5 - median_ta) * 12)
    breadth_part = min(22, share_hot * 55 + share_good * 12)

    score = int(round(delta_part + deal_part + breadth_part + ta_part + _proximity_bonus(days_f)))
    return min(100, score)


def _hot_score(row: Dict[str, Any]) -> int:
    """Run-to-run cohort drop and/or aggregate deal quality on this departure."""
    return min(100, max(_hot_score_run_delta(row), _hot_score_deal_aggregate(row)))


def departure_status_label(row: Dict[str, Any] | pd.Series) -> str:
    """Card badge: cohort drop vs below-typical deal heat."""
    if isinstance(row, pd.Series):
        row = row.to_dict()
    run_s = _hot_score_run_delta(row)
    deal_s = _hot_score_deal_aggregate(row)
    total = min(100, max(run_s, deal_s))
    if total >= 70:
        return f"🔥 Горит · {total}"
    if total >= 45:
        if run_s >= deal_s and run_s >= 45:
            return f"Снижается · {total}"
        return f"Выгодно · {total}"
    return "Не горит"


def build_departure_hotel_histories(offers: pd.DataFrame) -> Dict[str, pd.DataFrame]:
    """Per-hotel offer history for Deal / Δ типичной (same source as cohort deal metrics)."""
    if offers is None or offers.empty:
        return {}
    work = assign_scrape_runs(offers)
    work["price"] = pd.to_numeric(work["price"], errors="coerce")
    work = work.dropna(subset=["price"])
    if work.empty:
        return {}
    return {str(name): grp for name, grp in work.groupby("hotel_name", sort=False)}


def _cheap_tier_price(prices: pd.Series) -> float:
    bucket = cheap_tier_bucket_size(len(prices))
    if bucket <= 0:
        return float(prices.median())
    return float(prices.nsmallest(bucket).mean())


def _price_change_pct(prev: float, curr: float) -> float:
    if prev <= 0:
        return 0.0
    return round((curr - prev) / prev * 100.0, 2)


def _pick_lookback_run(
    runs: List[Tuple[pd.Timestamp, str]],
    curr_ts: pd.Timestamp,
) -> Optional[str]:
    """Baseline run closest to ~24h before curr_ts (window 12–36h)."""
    if not runs or pd.isna(curr_ts):
        return None

    target = curr_ts - timedelta(hours=COHORT_LOOKBACK_TARGET_HOURS)
    min_age = timedelta(hours=COHORT_LOOKBACK_MIN_HOURS)
    max_age = timedelta(hours=COHORT_LOOKBACK_MAX_HOURS)

    candidates: List[Tuple[float, str]] = []
    for ts, run_id in runs:
        if pd.isna(ts) or ts >= curr_ts:
            continue
        age = curr_ts - ts
        if age < min_age or age > max_age:
            continue
        candidates.append((abs((ts - target).total_seconds()), run_id))

    if candidates:
        return min(candidates, key=lambda x: x[0])[1]

    older = [
        (curr_ts - ts, run_id)
        for ts, run_id in runs
        if not pd.isna(ts) and ts < curr_ts and curr_ts - ts >= min_age
    ]
    if older:
        return min(older, key=lambda x: x[0])[1]
    return None


def _offers_through_run(offer_work: pd.DataFrame, run_started_at: str) -> pd.DataFrame:
    """Офферы, известные на момент scrape-run (без «будущей» истории)."""
    cutoff = pd.to_datetime(run_started_at, errors="coerce", utc=True)
    if pd.isna(cutoff) or offer_work.empty:
        return offer_work.iloc[0:0].copy()
    run_ts = pd.to_datetime(offer_work["run_started_at"], errors="coerce", utc=True)
    return offer_work.loc[run_ts <= cutoff].copy()


def _hotel_histories_for_run(
    offer_work: pd.DataFrame,
    run_started_at: str,
) -> Dict[str, pd.DataFrame]:
    pit = _offers_through_run(offer_work, run_started_at)
    if pit.empty:
        return {}
    return {str(name): grp for name, grp in pit.groupby("hotel_name", sort=False)}


def _ta_stats_for_offer_group(grp: pd.DataFrame) -> Dict[str, Any]:
    if grp.empty or "ta_rating" not in grp.columns:
        return {"median_ta_rating": 0.0, "ta_rated_hotel_count": 0}
    ta = pd.to_numeric(grp["ta_rating"], errors="coerce")
    rated_mask = ta > 0
    rated = ta[rated_mask]
    per_hotel = (
        grp.assign(_ta=ta)
        .groupby("hotel_name", sort=False)["_ta"]
        .max()
    )
    per_hotel = per_hotel[per_hotel > 0]
    return {
        "median_ta_rating": round(float(per_hotel.median()), 2) if not per_hotel.empty else 0.0,
        "ta_rated_hotel_count": int(len(per_hotel)),
    }


def _build_ta_lookup_by_run_hotel(offer_work: pd.DataFrame) -> Dict[Tuple[str, str, str], Tuple[Any, Any]]:
    lookup: Dict[Tuple[str, str, str], Tuple[Any, Any]] = {}
    if offer_work.empty or "ta_rating" not in offer_work.columns:
        return lookup
    work = offer_work.copy()
    work["_ta_rating"] = pd.to_numeric(work["ta_rating"], errors="coerce")
    work["_ta_reviews"] = pd.to_numeric(work.get("ta_review_count"), errors="coerce")
    group_cols = ["departure_key", "run_started_at", "hotel_name"]
    for (departure_key, run_started_at, hotel_name), grp in work.groupby(group_cols, sort=False):
        pick = grp.sort_values(["price", "_ta_reviews"], ascending=[True, False]).iloc[0]
        lookup[(str(departure_key), str(run_started_at), str(hotel_name))] = (
            pick["_ta_rating"] if pd.notna(pick["_ta_rating"]) else None,
            int(pick["_ta_reviews"]) if pd.notna(pick["_ta_reviews"]) else None,
        )
    return lookup


def _cohort_rows_to_update(
    work: pd.DataFrame,
    runs_to_update: Optional[set],
) -> pd.Series:
    if runs_to_update is None:
        return pd.Series(True, index=work.index)
    return work["run_started_at"].astype(str).isin(runs_to_update)


def _build_run_hotel_prices(work: pd.DataFrame) -> Dict[Tuple[str, str], pd.Series]:
    prices_by_run: Dict[Tuple[str, str], pd.Series] = {}
    if work.empty:
        return prices_by_run
    grouped = work.groupby(["departure_key", "run_started_at"], sort=False)
    for (departure_key, run_started_at), grp in grouped:
        hotel_prices = (
            grp.groupby("hotel_name")["price"]
            .min()
            .astype(float)
            .sort_index()
        )
        prices_by_run[(str(departure_key), str(run_started_at))] = hotel_prices
    return prices_by_run


def _intersection_changes(prev_prices: pd.Series, curr_prices: pd.Series) -> Dict[str, Any]:
    common = prev_prices.index.intersection(curr_prices.index)
    n = len(common)
    if n < MIN_COMMON_HOTELS:
        return {
            "common_hotel_count": n,
            "prev_min_price": 0.0,
            "prev_p10_price": 0.0,
            "prev_median_price": 0.0,
            "min_change_pct": 0.0,
            "p10_change_pct": 0.0,
            "median_change_pct": 0.0,
        }

    prev = prev_prices.loc[common]
    curr = curr_prices.loc[common]
    prev_min = float(prev.min())
    curr_min = float(curr.min())
    prev_median = float(prev.median())
    curr_median = float(curr.median())
    prev_cheap = _cheap_tier_price(prev)
    curr_cheap = _cheap_tier_price(curr)

    return {
        "common_hotel_count": n,
        "prev_min_price": round(prev_min, 2),
        "prev_p10_price": round(prev_cheap, 2),
        "prev_median_price": round(prev_median, 2),
        "min_change_pct": _price_change_pct(prev_min, curr_min),
        "p10_change_pct": _price_change_pct(prev_cheap, curr_cheap),
        "median_change_pct": _price_change_pct(prev_median, curr_median),
    }


def _enrich_deal_metrics(
    cohorts: pd.DataFrame,
    offers: pd.DataFrame,
    runs_to_update: Optional[set] = None,
) -> pd.DataFrame:
    """Per-run deal aggregates (Deal Score) с point-in-time историей по run."""
    enrich_t0 = time.monotonic()
    work = cohorts.copy()
    update_mask = _cohort_rows_to_update(work, runs_to_update)
    deal_defaults = (
        ("avg_deal_score", 0),
        ("mean_avg_delta_pct", 0.0),
        ("hot_deal_count", 0),
        ("good_deal_count", 0),
    )
    if runs_to_update is None:
        for col, default in deal_defaults:
            work[col] = default
    else:
        for col, default in deal_defaults:
            work.loc[update_mask, col] = default

    if work.empty or offers.empty or not update_mask.any():
        return work

    offer_work = assign_scrape_runs(offers)
    offer_work["price"] = pd.to_numeric(offer_work["price"], errors="coerce")
    offer_work = offer_work.dropna(subset=["price"])
    if offer_work.empty:
        return work

    prices_by_run = _build_run_hotel_prices(offer_work)
    ta_lookup = _build_ta_lookup_by_run_hotel(offer_work)
    runs_needed = set(work.loc[update_mask, "run_started_at"].astype(str).unique())
    hist_by_run: Dict[str, Dict[str, pd.DataFrame]] = {
        run_key: _hotel_histories_for_run(offer_work, run_key) for run_key in runs_needed
    }

    updated_n = 0
    for idx, row in work.loc[update_mask].iterrows():
        departure_key = str(row["departure_key"])
        run_started_at = str(row["run_started_at"])
        curr_prices = prices_by_run.get((departure_key, run_started_at), {})
        if len(curr_prices) < MIN_DEAL_HOTELS:
            continue

        hotel_histories = hist_by_run.get(run_started_at, {})
        deal_scores: List[int] = []
        avg_deltas: List[float] = []
        hot_n = 0
        good_n = 0
        for hotel_name, price in curr_prices.items():
            hist = hotel_histories.get(hotel_name)
            if hist is None or len(hist) < 2:
                continue
            ta_rating, ta_reviews = ta_lookup.get(
                (departure_key, run_started_at, str(hotel_name)),
                (None, None),
            )
            metrics = compute_hotel_deal_metrics(
                hist,
                price,
                ta_rating=ta_rating,
                ta_review_count=ta_reviews,
            )
            deal_scores.append(int(metrics["deal_score"]))
            avg_deltas.append(float(metrics["avg_delta_pct"]))
            if metrics["deal_score"] >= 80:
                hot_n += 1
            elif metrics["deal_score"] >= 65:
                good_n += 1

        if not deal_scores:
            continue

        work.at[idx, "avg_deal_score"] = int(round(sum(deal_scores) / len(deal_scores)))
        work.at[idx, "mean_avg_delta_pct"] = round(sum(avg_deltas) / len(avg_deltas), 2)
        work.at[idx, "hot_deal_count"] = hot_n
        work.at[idx, "good_deal_count"] = good_n
        updated_n += 1

    scope = f"all {len(work)}" if runs_to_update is None else f"{updated_n}/{int(update_mask.sum())} updated"
    _log_timing(
        "_enrich_deal_metrics",
        enrich_t0,
        f"cohorts={len(cohorts)}, offers={len(offers)}, scope={scope}, point_in_time=True",
    )
    return work


def _add_change_columns(
    cohorts: pd.DataFrame,
    offers: pd.DataFrame | None = None,
    runs_to_update: Optional[set] = None,
) -> pd.DataFrame:
    add_t0 = time.monotonic()
    if cohorts.empty:
        return cohorts
    work = cohorts.copy()
    work["_run_ts"] = pd.to_datetime(work["run_started_at"], errors="coerce", utc=True)
    work = work.sort_values(["departure_key", "_run_ts"]).reset_index(drop=True)
    update_mask = _cohort_rows_to_update(work, runs_to_update)
    if runs_to_update is None:
        work["hotel_count_delta"] = 0
        work["common_hotel_count"] = 0
    else:
        work.loc[update_mask, "hotel_count_delta"] = 0
        work.loc[update_mask, "common_hotel_count"] = 0

    prices_t0 = time.monotonic()
    prices_by_run = _build_run_hotel_prices(offers) if offers is not None and not offers.empty else {}
    _log_timing("_build_run_hotel_prices", prices_t0, f"runs={len(prices_by_run)}")

    runs_t0 = time.monotonic()
    runs_by_key: Dict[str, List[Tuple[pd.Timestamp, str]]] = {}
    hotel_count_by_run: Dict[Tuple[str, str], int] = {}
    for _, row in work.iterrows():
        departure_key = str(row["departure_key"])
        run_started_at = str(row["run_started_at"])
        runs_by_key.setdefault(departure_key, []).append((row["_run_ts"], run_started_at))
        try:
            hotel_count_by_run[(departure_key, run_started_at)] = int(row["hotel_count"])
        except (TypeError, ValueError):
            hotel_count_by_run[(departure_key, run_started_at)] = 0
    _log_timing("_add_change_columns index runs", runs_t0, f"cohorts={len(work)}")

    changes_t0 = time.monotonic()
    lookback_updated = 0
    for idx, row in work.iterrows():
        run_started_at = str(row["run_started_at"])
        if runs_to_update is not None and run_started_at not in runs_to_update:
            continue
        departure_key = str(row["departure_key"])
        prev_run = _pick_lookback_run(runs_by_key.get(departure_key, []), row["_run_ts"])
        if prev_run:
            prev_prices = prices_by_run.get((departure_key, prev_run))
            curr_prices = prices_by_run.get((departure_key, run_started_at))
            if prev_prices is not None and curr_prices is not None:
                changes = _intersection_changes(prev_prices, curr_prices)
                for key, value in changes.items():
                    work.at[idx, key] = value
            else:
                work.at[idx, "common_hotel_count"] = 0
                for col in [
                    "prev_min_price", "prev_p10_price", "prev_median_price",
                    "min_change_pct", "p10_change_pct", "median_change_pct",
                ]:
                    work.at[idx, col] = 0.0
            prev_hc = hotel_count_by_run.get((departure_key, prev_run))
            if prev_hc is not None:
                try:
                    curr_hc = int(row["hotel_count"])
                except (TypeError, ValueError):
                    curr_hc = 0
                work.at[idx, "hotel_count_delta"] = curr_hc - prev_hc
        else:
            for col in [
                "prev_min_price", "prev_p10_price", "prev_median_price",
                "min_change_pct", "p10_change_pct", "median_change_pct",
            ]:
                work.at[idx, col] = 0.0
        lookback_updated += 1
    lookback_scope = (
        f"cohorts={len(work)}"
        if runs_to_update is None
        else f"updated={lookback_updated}/{int(update_mask.sum())}"
    )
    _log_timing("_add_change_columns lookback loop", changes_t0, lookback_scope)

    if offers is not None and not offers.empty:
        work = _enrich_deal_metrics(work, offers, runs_to_update=runs_to_update)
    hot_t0 = time.monotonic()
    if runs_to_update is None:
        work["hot_score"] = work.apply(lambda row: _hot_score(row.to_dict()), axis=1)
    else:
        work.loc[update_mask, "hot_score"] = work.loc[update_mask].apply(
            lambda row: _hot_score(row.to_dict()),
            axis=1,
        )
    _log_timing("_add_change_columns hot_score", hot_t0)
    _log_timing(
        "_add_change_columns total",
        add_t0,
        f"cohorts={len(cohorts)}, incremental={runs_to_update is not None}",
    )
    return work.drop(columns=["_run_ts"])


def _prepare_cohort_offer_work(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df.copy()
    work = assign_scrape_runs(df)
    work["price"] = pd.to_numeric(work["price"], errors="coerce")
    work["nights"] = pd.to_numeric(work["nights"], errors="coerce")
    work = work.dropna(subset=["price"])
    work = work[
        work["departure_key"].fillna("").astype(str).ne("")
        & work["region"].fillna("").astype(str).ne("")
        & work["departure_date"].fillna("").astype(str).ne("")
    ].copy()
    return work


def _cohort_snapshot_rows(
    work: pd.DataFrame,
    only_run_started_at: Optional[set] = None,
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    group_cols = ["run_started_at", "departure_key"]
    for (run_started_at, departure_key), grp in work.groupby(group_cols, sort=True):
        run_key = str(run_started_at)
        if only_run_started_at is not None and run_key not in only_run_started_at:
            continue
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
        row.update(_ta_stats_for_offer_group(grp))
        row["min_change_pct"] = 0.0
        row["p10_change_pct"] = 0.0
        row["median_change_pct"] = 0.0
        row["prev_min_price"] = 0.0
        row["prev_p10_price"] = 0.0
        row["prev_median_price"] = 0.0
        row["hotel_count_delta"] = 0
        row["common_hotel_count"] = 0
        row["avg_deal_score"] = 0
        row["mean_avg_delta_pct"] = 0.0
        row["hot_deal_count"] = 0
        row["good_deal_count"] = 0
        if "median_ta_rating" not in row:
            row["median_ta_rating"] = 0.0
        if "ta_rated_hotel_count" not in row:
            row["ta_rated_hotel_count"] = 0
        row["hot_score"] = 0
        rows.append(row)
    return rows


def build_cohort_snapshot_frame(
    df: pd.DataFrame,
    only_run_started_at: Optional[set] = None,
    *,
    work: Optional[pd.DataFrame] = None,
) -> pd.DataFrame:
    offer_work = work if work is not None else _prepare_cohort_offer_work(df)
    if offer_work.empty:
        return pd.DataFrame(columns=COHORT_FIELDS)
    rows = _cohort_snapshot_rows(offer_work, only_run_started_at)
    if not rows:
        return pd.DataFrame(columns=COHORT_FIELDS)
    return pd.DataFrame(rows, columns=COHORT_FIELDS)


def build_cohort_snapshots(df: pd.DataFrame) -> pd.DataFrame:
    work = _prepare_cohort_offer_work(df)
    if work.empty:
        return pd.DataFrame(columns=COHORT_FIELDS)
    frame = build_cohort_snapshot_frame(df, work=work)
    return _add_change_columns(frame, work)


def _offers_input_fingerprint(path: str) -> str:
    if not os.path.exists(path):
        return ""
    stat = os.stat(path)
    return f"{stat.st_size}:{int(stat.st_mtime)}"


TRAVEL_COHORTS_CACHE_FILE = "travel_prices_cohorts.csv"
TRAVEL_COHORTS_FP_FILE = ".travel_prices_cohorts_fingerprint"


def _load_travel_prices_cohort_snapshots(
    travel_prices_file: str,
    data_dir: str,
) -> pd.DataFrame:
    """Incremental cache for travel_prices cohort layer (D-14…D-0 curves)."""
    cache_path = os.path.join(data_dir, TRAVEL_COHORTS_CACHE_FILE)
    fp_path = os.path.join(data_dir, TRAVEL_COHORTS_FP_FILE)
    current_fp = _offers_input_fingerprint(travel_prices_file)

    if not current_fp:
        return pd.DataFrame(columns=COHORT_FIELDS)

    if os.path.exists(cache_path) and os.path.exists(fp_path):
        try:
            if open(fp_path, encoding="utf-8").read().strip() == current_fp:
                cached = pd.read_csv(cache_path, quoting=csv.QUOTE_ALL, on_bad_lines="skip")
                print(
                    f"📂 Cohorts history: {len(cached)} снимков из кэша {TRAVEL_COHORTS_CACHE_FILE}"
                )
                return cached
        except Exception:
            pass

    tp = load_departure_offers(travel_prices_file)
    if tp.empty:
        return pd.DataFrame(columns=COHORT_FIELDS)

    work = _prepare_cohort_offer_work(tp)
    if work.empty:
        return pd.DataFrame(columns=COHORT_FIELDS)

    existing = pd.DataFrame(columns=COHORT_FIELDS)
    if os.path.exists(cache_path):
        try:
            existing = pd.read_csv(cache_path, quoting=csv.QUOTE_ALL, on_bad_lines="skip")
        except Exception:
            existing = pd.DataFrame(columns=COHORT_FIELDS)

    all_runs = set(work["run_started_at"].astype(str).unique())
    if existing.empty:
        travel_snapshots = build_cohort_snapshot_frame(tp, work=work)
        rebuilt = len(all_runs)
    else:
        known_runs = set(existing["run_started_at"].astype(str).unique())
        new_runs = all_runs - known_runs
        latest_run = max(all_runs) if all_runs else None
        runs_to_rebuild = set(new_runs)
        if latest_run:
            runs_to_rebuild.add(latest_run)
        keep = existing[~existing["run_started_at"].astype(str).isin(runs_to_rebuild)]
        new_frame = build_cohort_snapshot_frame(tp, runs_to_rebuild, work=work)
        travel_snapshots = pd.concat([keep, new_frame], ignore_index=True, sort=False)
        rebuilt = len(runs_to_rebuild)

    travel_snapshots.to_csv(cache_path, index=False, quoting=csv.QUOTE_ALL)
    with open(fp_path, "w", encoding="utf-8") as f:
        f.write(current_fp)

    if rebuilt < len(all_runs):
        print(
            f"📈 Cohorts history: обновлено {rebuilt} ранов → {len(travel_snapshots)} снимков "
            f"(кэш {TRAVEL_COHORTS_CACHE_FILE}, кривые D-{HOT_DEPARTURE_CHART_DAYS_MAX}…D-0)"
        )
    else:
        print(
            f"📈 Cohorts history: +{len(travel_snapshots)} снимков из travel_prices "
            f"(для кривых D-{HOT_DEPARTURE_CHART_DAYS_MAX}…D-0)"
        )
    return travel_snapshots


def _merge_cohort_history_frames(*frames: pd.DataFrame) -> pd.DataFrame:
    """Union cohort snapshots; enriched rows from monitor override travel backfill."""
    parts = [frame for frame in frames if frame is not None and not frame.empty]
    if not parts:
        return pd.DataFrame(columns=COHORT_FIELDS)
    merged = pd.concat(parts, ignore_index=True, sort=False)
    key_cols = ["run_started_at", "departure_key"]
    if all(col in merged.columns for col in key_cols):
        merged = merged.drop_duplicates(subset=key_cols, keep="last")
    return merged


def load_stored_departure_cohorts(
    data_dir: str,
    travel_prices_file: str | None = None,
) -> Tuple[pd.DataFrame, pd.DataFrame, Optional[List[Dict[str, Any]]]]:
    """Load monitor cohort cache + travel_prices backfill for D-14…D-0 curves.

    ``current`` — enriched cohorts from departure_offers (алерты, hot_score).
    ``history`` — travel snapshot layer merged in (графики вылета в модалке).
    """
    cohorts_path = os.path.join(data_dir, "departure_cohorts.csv")
    hot_history_path = os.path.join(data_dir, "departure_hot_history.json")

    current = pd.DataFrame(columns=COHORT_FIELDS)
    if os.path.exists(cohorts_path):
        current = pd.read_csv(cohorts_path, quoting=csv.QUOTE_ALL, on_bad_lines="skip")
        print(f"📂 Cohorts: загружено {len(current)} снимков из {cohorts_path}")

    if current.empty:
        # При отсутствии кэша — полный пересчёт из всех архивных файлов + текущего месяца
        offers_combined = load_combined_departure_offers(data_dir, travel_prices_file)
        if not offers_combined.empty:
            print(f"⚠️ Cohorts cache отсутствует — полный пересчёт из всех архивных departure_offers")
            current = build_cohort_snapshots(offers_combined)


    travel_snapshots = pd.DataFrame(columns=COHORT_FIELDS)
    if travel_prices_file and os.path.exists(travel_prices_file):
        travel_snapshots = _load_travel_prices_cohort_snapshots(travel_prices_file, data_dir)

    history = _merge_cohort_history_frames(travel_snapshots, current)
    if history.empty:
        history = current

    hot_history: Optional[List[Dict[str, Any]]] = None
    if os.path.exists(hot_history_path):
        try:
            with open(hot_history_path, encoding="utf-8") as f:
                payload = json.load(f)
            hot_history = payload.get("departures") or []
            print(f"📂 Hot departure history: {len(hot_history)} записей из кэша")
        except Exception as e:
            print(f"⚠️ Не удалось прочитать {hot_history_path}: {e}")

    return current, history, hot_history


def _offer_payload(row: pd.Series) -> Dict[str, Any]:
    return {
        "hotel_name": str(row.get("hotel_name") or ""),
        "price": round(float(row.get("price") or 0), 2),
        "dates": str(row.get("dates") or ""),
        "duration": str(row.get("duration") or ""),
        "offer_url": str(row.get("offer_url") or ""),
        "image_url": str(row.get("image_url") or ""),
    }


def _offers_for_departure_key(work: pd.DataFrame, departure_key: str) -> pd.DataFrame:
    hub = parse_hub_departure_key(departure_key)
    if hub:
        regions = turkey_hub_regions(hub["hub_id"])
        mask = (
            work["country"].fillna("").astype(str).str.lower().eq(hub["country"])
            & work["region"].fillna("").astype(str).str.lower().isin(regions)
            & work["departure_date"].fillna("").astype(str).eq(hub["departure_date"])
            & work["return_date"].fillna("").astype(str).eq(hub["return_date"])
            & _nights_series_eq(work["nights"], hub["nights"])
            & work["origin_scope"].fillna("").astype(str).eq(hub["origin_scope"])
            & work["pax_profile"].fillna("").astype(str).eq(hub["pax_profile"])
        )
        return work[mask].copy()
    return work[work["departure_key"].fillna("").astype(str).eq(departure_key)].copy()


def _departure_day_run_started_at(grp: pd.DataFrame) -> str:
    """Last scrape on departure day (D-0), else closest pre-departure snapshot."""
    if grp.empty:
        return ""
    work = grp.copy()
    work["_run_ts"] = pd.to_datetime(work["run_started_at"], errors="coerce", utc=True)
    work["days_to_departure"] = pd.to_numeric(work["days_to_departure"], errors="coerce")
    d0 = work[work["days_to_departure"].fillna(-1) == 0]
    if not d0.empty:
        return str(d0.sort_values("_run_ts").iloc[-1]["run_started_at"])
    before = work[work["days_to_departure"].fillna(9999) >= 0]
    if not before.empty:
        min_days = before["days_to_departure"].min()
        subset = before[before["days_to_departure"] == min_days]
        return str(subset.sort_values("_run_ts").iloc[-1]["run_started_at"])
    return str(work.sort_values("_run_ts").iloc[-1]["run_started_at"])


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
    if work.empty:
        return {}

    work["_run_ts"] = pd.to_datetime(work["run_started_at"], errors="coerce", utc=True)
    index: Dict[str, Dict[str, Any]] = {}

    for departure_key in keys:
        grp = _offers_for_departure_key(work, departure_key)
        if grp.empty:
            continue

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

        # One row per hotel: latest observation in this scrape (not historical min).
        sort_cols = ["hotel_name"]
        dedupe_work = run_grp.copy()
        if "scraped_at" in dedupe_work.columns:
            dedupe_work["_obs_ts"] = pd.to_datetime(
                dedupe_work["scraped_at"], errors="coerce", utc=True
            )
            sort_cols.append("_obs_ts")
        elif "_run_ts" in dedupe_work.columns:
            sort_cols.append("_run_ts")
        else:
            sort_cols.append("price")
        deduped = (
            dedupe_work.sort_values(sort_cols)
            .drop_duplicates(subset=["hotel_name"], keep="last")
            .sort_values("price")
        )
        first = run_grp.iloc[0]
        hub = parse_hub_departure_key(departure_key)
        region_label = str(first.get("region") or "")
        if hub:
            regions = sorted(run_grp["region"].fillna("").astype(str).unique())
            region_label = turkey_hub_label(hub["hub_id"])
            subtitle = hub_regions_subtitle(regions)
        else:
            subtitle = ""
        index[departure_key] = {
            "departure_key": departure_key,
            "region": region_label,
            "hub_subtitle": subtitle,
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
        for _, row in grp.iterrows():
            event = _event_from_pair(departure_key, {}, row.to_dict())
            if event:
                events.append(event)
    return events


def _cohorts_for_departure_key(cohorts: pd.DataFrame, departure_key: str) -> pd.DataFrame:
    hub = parse_hub_departure_key(departure_key)
    if hub:
        regions = turkey_hub_regions(hub["hub_id"])
        mask = (
            cohorts["country"].fillna("").astype(str).str.lower().eq(hub["country"])
            & cohorts["region"].fillna("").astype(str).str.lower().isin(regions)
            & cohorts["departure_date"].fillna("").astype(str).eq(hub["departure_date"])
            & cohorts["return_date"].fillna("").astype(str).eq(hub["return_date"])
            & _nights_series_eq(cohorts["nights"], hub["nights"])
            & cohorts["origin_scope"].fillna("").astype(str).eq(hub["origin_scope"])
            & cohorts["pax_profile"].fillna("").astype(str).eq(hub["pax_profile"])
        )
        return cohorts[mask].copy()
    return cohorts[cohorts["departure_key"].fillna("").astype(str).eq(str(departure_key))].copy()


def build_departure_price_curve(
    cohorts: pd.DataFrame,
    departure_key: str,
    days_min: int = 0,
    days_max: int = HOT_DEPARTURE_CHART_DAYS_MAX,
) -> Dict[str, Any]:
    """Median / cheap-tier price curve for a departure key over time (preserves all scrape runs)."""
    empty: Dict[str, Any] = {
        "days": [],
        "labels": [],
        "timestamps": [],
        "median_price": [],
        "p10_price": [],
        "min_price": [],
        "hotel_count": [],
    }
    work = _cohorts_for_departure_key(cohorts, departure_key)
    if work.empty:
        return empty

    work = work.copy()
    work["days_to_departure"] = pd.to_numeric(work["days_to_departure"], errors="coerce")
    work = work[work["days_to_departure"].between(days_min, days_max, inclusive="both")]
    if work.empty:
        return empty

    work["_run_ts"] = pd.to_datetime(work["run_started_at"], errors="coerce", utc=True)
    work = work.dropna(subset=["_run_ts"]).sort_values("_run_ts")

    points: List[Dict[str, Any]] = []
    for _ts, rgrp in work.groupby("_run_ts", sort=True):
        latest = rgrp.iloc[-1]
        days_val = int(latest.get("days_to_departure") or 0)
        ts_str = _ts.strftime("%d.%m %H:%M")
        points.append({
            "days": days_val,
            "label": f"D-{days_val} ({ts_str})",
            "timestamp": _ts.isoformat(),
            "median_price": round(float(latest.get("median_price") or 0), 2),
            "p10_price": round(float(latest.get("p10_price") or 0), 2),
            "min_price": round(float(latest.get("min_price") or 0), 2),
            "hotel_count": int(latest.get("hotel_count") or 0),
        })

    return {
        "days": [row["days"] for row in points],
        "labels": [row["label"] for row in points],
        "timestamps": [row["timestamp"] for row in points],
        "median_price": [row["median_price"] for row in points],
        "p10_price": [row["p10_price"] for row in points],
        "min_price": [row["min_price"] for row in points],
        "hotel_count": [row["hotel_count"] for row in points],
    }


def build_departure_price_curves(
    cohorts: pd.DataFrame,
    departure_keys: List[str],
    days_max: int = HOT_DEPARTURE_CHART_DAYS_MAX,
) -> Dict[str, Dict[str, Any]]:
    curves: Dict[str, Dict[str, Any]] = {}
    for departure_key in departure_keys:
        key = str(departure_key or "")
        if not key:
            continue
        curve = build_departure_price_curve(cohorts, key, days_max=days_max)
        if curve.get("days"):
            curves[key] = curve
    return curves


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
        "avg_deal_score", "mean_avg_delta_pct", "hot_deal_count", "good_deal_count",
        "median_ta_rating", "ta_rated_hotel_count",
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
        stable_common = grp["common_hotel_count"].fillna(0) >= MIN_COMMON_HOTELS
        moderate_drop = (
            stable_common
            & (grp["hotel_count"].fillna(0) >= 3)
            & (
                (grp["p10_change_pct"].fillna(0) <= -10)
                | (grp["median_change_pct"].fillna(0) <= -8)
            )
        )
        strong_drop = (
            stable_common
            & (grp["hotel_count"].fillna(0) >= 2)
            & (
                (grp["p10_change_pct"].fillna(0) <= -15)
                | (grp["median_change_pct"].fillna(0) <= -12)
            )
        )
        ta_known = grp["median_ta_rating"].fillna(0) > 0
        ta_ok = (~ta_known) | (grp["median_ta_rating"].fillna(0) >= 3.8)
        deal_signal = (
            (grp["hotel_count"].fillna(0) >= MIN_DEAL_HOTELS)
            & (grp["avg_deal_score"].fillna(0) >= 68)
            & (grp["mean_avg_delta_pct"].fillna(0) <= -5)
            & ta_ok
        )
        hot_mask = (
            late_window
            & cheap_enough
            & (moderate_drop | strong_drop | deal_signal)
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
        departure_day_at = _departure_day_run_started_at(grp)
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
            "departure_day_at": departure_day_at,
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
    try:
        min_pct = float(curr.get("min_change_pct") or 0)
        p10_pct = float(curr.get("p10_change_pct") or 0)
        median_pct = float(curr.get("median_change_pct") or 0)
    except (TypeError, ValueError):
        min_pct = p10_pct = median_pct = 0.0
    if min_pct == 0 and p10_pct == 0 and median_pct == 0:
        min_pct = _pct(prev.get("min_price"), curr.get("min_price"))
        p10_pct = _pct(prev.get("p10_price"), curr.get("p10_price"))
        median_pct = _pct(prev.get("median_price"), curr.get("median_price"))
    hotel_delta = int(curr.get("hotel_count_delta") or 0)

    is_drop = min_pct <= -8.0 or p10_pct <= -8.0 or median_pct <= -5.0
    is_broad = int(curr.get("hotel_count") or 0) >= 3
    is_stable = int(curr.get("common_hotel_count") or 0) >= MIN_COMMON_HOTELS
    cohort_drop = is_drop and is_broad and is_stable
    deal_drop = (
        is_broad
        and float(curr.get("avg_deal_score") or 0) >= 70
        and float(curr.get("mean_avg_delta_pct") or 0) <= -6
    )
    if not (cohort_drop or deal_drop):
        return None

    days = curr.get("days_to_departure")
    try:
        days_f = float(days)
    except (TypeError, ValueError):
        days_f = None

    event_type = "deal_quality_surge" if deal_drop and not cohort_drop else "mass_price_drop"
    severity = "medium"
    if days_f is not None and days_f <= 7:
        event_type = "late_price_dump" if cohort_drop else "late_deal_window"
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


def write_departure_analytics(
    input_csv: str,
    output_dir: str,
    force_full: bool = False,
) -> dict:
    write_t0 = time.monotonic()
    load_t0 = time.monotonic()
    df = load_departure_offers(input_csv)
    _log_timing("load_departure_offers", load_t0, f"rows={len(df)}")
    os.makedirs(output_dir, exist_ok=True)
    cohorts_path = os.path.join(output_dir, "departure_cohorts.csv")
    events_path = os.path.join(output_dir, "departure_events.json")
    history_path = os.path.join(output_dir, "departure_hot_history.json")
    fingerprint_path = os.path.join(output_dir, ".departure_analytics_fingerprint")

    current_fp = _offers_input_fingerprint(input_csv)
    if (
        not force_full
        and current_fp
        and os.path.exists(cohorts_path)
        and os.path.exists(fingerprint_path)
    ):
        try:
            if open(fingerprint_path, encoding="utf-8").read().strip() == current_fp:
                cohorts_cached = pd.read_csv(
                    cohorts_path, quoting=csv.QUOTE_ALL, on_bad_lines="skip"
                )
                events_n = 0
                hot_n = 0
                if os.path.exists(events_path):
                    with open(events_path, encoding="utf-8") as f:
                        events_n = len(json.load(f).get("events") or [])
                if os.path.exists(history_path):
                    with open(history_path, encoding="utf-8") as f:
                        hot_n = len(json.load(f).get("departures") or [])
                return {
                    "skipped": True,
                    "offers": int(len(df)),
                    "cohorts": int(len(cohorts_cached)),
                    "events": events_n,
                    "hot_history": hot_n,
                    "cohorts_path": cohorts_path,
                    "events_path": events_path,
                    "history_path": history_path,
                }
        except Exception:
            pass

    prep_t0 = time.monotonic()
    work = _prepare_cohort_offer_work(df)
    _log_timing("_prepare_cohort_offer_work", prep_t0, f"offers={len(work)}")
    runs_to_rebuild: Optional[set] = None
    existing = pd.DataFrame(columns=COHORT_FIELDS)
    if not force_full and os.path.exists(cohorts_path):
        try:
            existing = pd.read_csv(cohorts_path, quoting=csv.QUOTE_ALL, on_bad_lines="skip")
        except Exception:
            existing = pd.DataFrame(columns=COHORT_FIELDS)

    cohort_t0 = time.monotonic()
    if force_full or existing.empty or work.empty:
        cohorts = build_cohort_snapshots(df) if not work.empty else pd.DataFrame(columns=COHORT_FIELDS)
        logger.info("⏱ departure_analytics: full cohort rebuild")
    else:
        all_runs = set(work["run_started_at"].astype(str).unique())
        known_runs = set(existing["run_started_at"].astype(str).unique())
        new_runs = all_runs - known_runs
        latest_run = max(all_runs) if all_runs else None
        runs_to_rebuild = set(new_runs)
        if latest_run:
            runs_to_rebuild.add(latest_run)
        keep = existing[~existing["run_started_at"].astype(str).isin(runs_to_rebuild)]
        new_frame = build_cohort_snapshot_frame(df, runs_to_rebuild, work=work)
        combined = pd.concat([keep, new_frame], ignore_index=True, sort=False)
        logger.info(
            f"⏱ departure_analytics incremental: cohorts={len(combined)} "
            f"rebuild_runs={len(runs_to_rebuild)} new_rows={len(new_frame)}"
        )
        cohorts = _add_change_columns(combined, work, runs_to_update=runs_to_rebuild)
    _log_timing("build cohorts", cohort_t0, f"rows={len(cohorts)}")

    ev_t0 = time.monotonic()
    events = build_departure_events(cohorts)
    _log_timing("build_departure_events", ev_t0, f"n={len(events)}")
    hot_t0 = time.monotonic()
    hot_history = build_hot_departure_history(cohorts)
    _log_timing("build_hot_departure_history", hot_t0, f"n={len(hot_history)}")

    save_t0 = time.monotonic()
    cohorts.to_csv(cohorts_path, index=False, quoting=csv.QUOTE_ALL)
    with open(events_path, "w", encoding="utf-8") as f:
        json.dump({"events": events}, f, ensure_ascii=False, indent=2)
    with open(history_path, "w", encoding="utf-8") as f:
        json.dump({"departures": hot_history}, f, ensure_ascii=False, indent=2)
    if current_fp:
        with open(fingerprint_path, "w", encoding="utf-8") as f:
            f.write(current_fp)
    _log_timing("save outputs", save_t0)
    _log_timing("write_departure_analytics total", write_t0, f"cohorts={len(cohorts)}")

    return {
        "skipped": False,
        "offers": int(len(df)),
        "cohorts": int(len(cohorts)),
        "events": int(len(events)),
        "hot_history": int(len(hot_history)),
        "cohorts_path": cohorts_path,
        "events_path": events_path,
        "history_path": history_path,
        "rebuilt_runs": int(len(runs_to_rebuild)) if runs_to_rebuild is not None else None,
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
