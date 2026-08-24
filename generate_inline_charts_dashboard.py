#!/usr/bin/env python3
"""
Дашборд с встроенными графиками вместо модальных окон
"""

import pandas as pd
import json
import csv
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional
import os
import re
import html as html_lib
from urllib.parse import urlparse, parse_qs, quote
from purchase_timing_analysis import analyze_purchase_timing
from departure_analytics import (
    build_departure_offers_index,
    build_departure_hotel_histories,
    build_hot_departure_history,
    build_departure_price_curves,
    cheap_tier_label,
    departure_status_label,
    _hot_score,
    HOT_DEPARTURE_CHART_DAYS_MAX,
    load_combined_departure_offers,
    load_stored_departure_cohorts,
    MIN_COMMON_HOTELS,
    MIN_DEAL_HOTELS,
    COHORT_LOOKBACK_TARGET_HOURS,
)
from hotel_deal_score import (
    blend_tripadvisor_into_deal_score,
    build_premium_history_by_hotel,
    comeback_from_premium,
    compute_hotel_deal_metrics,
    fetch_tripadvisor_from_listing_url,
    robust_premium_peak,
)
from departure_identity import parse_offer_path
from departure_airports import (
    aggregate_cohorts_by_arrival_airport,
    arrival_hub_label,
    hub_regions_subtitle,
    should_group_by_arrival_airport,
)
from filter_registry import active_filter_groups, active_filter_id, filter_href_by_charts_subdir
from filter_params import (
    DATA_DIR_CONFIG_FILES,
    load_filter_config,
    resolve_config_url,
    render_filter_params_html,
    render_global_duration_switch_html,
    resolve_config_path,
)
from filter_trip import (
    _duration_bucket_is_missing,
    infer_offer_duration_bucket,
    is_fixed_trip_config,
    offer_duration_days,
    parse_trip_duration_buckets,
    trip_duration_bucket_id,
    trip_row_key,
)

# Официальный logomark TripAdvisor (owl), cropped from TA_logo_primary.svg (tacdn.com).
TRIPADVISOR_HEADER_ICON_HTML = (
    '<span class="th-ta-icon" aria-hidden="true">'
    '<svg class="th-ta-svg" viewBox="0 0 50 30" xmlns="http://www.w3.org/2000/svg">'
    '<path fill="#FAC415" d="M8.69853,6.34985C13.42485,5.74015,26.834,5.32015,23.16029,24.69632l4.16692-.339C25.50191,12.41381,29.31986,6.324,41.25838,5.74015,21.45779-5.22456,10.18426,6.15809,8.69853,6.34985Z"/>'
    '<path fill="#fff" d="M27.13191,21.62794A11.34312,11.34312,0,1,0,33.84147,7.055,11.347,11.347,0,0,0,27.13191,21.62794Z"/>'
    '<circle fill="#fff" cx="12.62599" cy="17.65102" r="11.34537"/>'
    '<circle fill="#EE6946" cx="12.4632" cy="17.53993" r="2.0971"/>'
    '<circle fill="#00AF87" cx="37.74486" cy="17.53993" r="2.09618"/>'
    '<path fill="#000" d="M47.89824,10.18a16.25082,16.25082,0,0,1,2.48088-5.0444l-8.41926-.00647A30.65918,30.65918,0,0,0,25.14368.461,31.36745,31.36745,0,0,0,7.90015,5.18868L0,5.19309a16.3389,16.3389,0,0,1,2.46941,5.00044A12.60265,12.60265,0,0,0,22.46235,25.53426L25.14721,29.554l2.71074-4.05353A12.61369,12.61369,0,0,0,47.89824,10.18ZM37.37,5.094A12.57259,12.57259,0,0,0,25.19676,16.73368,12.6202,12.6202,0,0,0,12.87912,5.04515,31.17654,31.17654,0,0,1,25.14368,2.66044,29.67419,29.67419,0,0,1,37.37,5.094ZM12.62632,27.71926A10.06971,10.06971,0,1,1,22.695,17.652,10.08062,10.08062,0,0,1,12.62632,27.71926Zm28.63412-.57147a10.07577,10.07577,0,0,1-12.93515-5.96V21.185A10.07008,10.07008,0,1,1,41.26044,27.14779Z"/>'
    '<path fill="#000" d="M12.47059,11.3094a6.231,6.231,0,1,0,6.22191,6.23A6.24029,6.24029,0,0,0,12.47059,11.3094Zm0,10.31575a4.08509,4.08509,0,1,1,4.07706-4.08574A4.09513,4.09513,0,0,1,12.47059,21.62515Z"/>'
    '<path fill="#000" d="M37.74486,11.3094a6.231,6.231,0,1,0,6.22779,6.23A6.23822,6.23822,0,0,0,37.74486,11.3094Zm0,10.31575a4.08508,4.08508,0,1,1,4.08367-4.08574A4.091,4.091,0,0,1,37.74486,21.62515Z"/>'
    '</svg></span>'
)


def _parse_ta_rating_value(value: Any) -> Optional[float]:
    try:
        rating = float(value)
    except (TypeError, ValueError):
        return None
    return rating if rating > 0 else None


def _parse_ta_review_count(value: Any) -> int:
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return 0


def _resolve_listing_url_for_backfill(
    df: pd.DataFrame,
    config_file: Optional[str],
    data_file: str,
) -> str:
    if config_file and os.path.isfile(config_file):
        try:
            with open(config_file, encoding="utf-8") as f:
                cfg = json.load(f)
            url = resolve_config_url(cfg)
            if url:
                return url
        except Exception:
            pass
    if not df.empty and "url" in df.columns:
        for val in reversed(df["url"].dropna().astype(str).tolist()):
            if "fly.pl/oferta/" in val:
                return val
    data_dir = os.path.dirname(data_file) or "."
    folder = os.path.basename(os.path.normpath(data_dir))
    cfg_name = DATA_DIR_CONFIG_FILES.get(folder)
    if cfg_name and os.path.isfile(cfg_name):
        try:
            with open(cfg_name, encoding="utf-8") as f:
                return resolve_config_url(json.load(f))
        except Exception:
            pass
    return ""


def _backfill_ta_for_latest_rows(
    latest_rows: List[dict],
    df: pd.DataFrame,
    config_file: Optional[str],
    data_file: str,
) -> int:
    if not latest_rows:
        return 0
    missing = [
        row for row in latest_rows
        if _parse_ta_rating_value(row.get("ta_rating")) is None
    ]
    if not missing:
        return 0
    search_url = _resolve_listing_url_for_backfill(df, config_file, data_file)
    if not search_url:
        print("⚠️ TA backfill: не найден URL выдачи fly.pl")
        return 0
    print(f"⭐ TA backfill: live-выдача fly.pl для {len(missing)} отелей без оценки...")
    lookup = fetch_tripadvisor_from_listing_url(search_url, max_pages=8)
    by_name = lookup.get("by_name") or {}
    by_offer = lookup.get("by_offer") or {}
    filled = 0
    for row in latest_rows:
        if _parse_ta_rating_value(row.get("ta_rating")) is not None:
            continue
        offer_url = str(row.get("offer_url") or "").strip()
        hotel_name = str(row.get("hotel_name") or "").strip()
        ta = by_offer.get(offer_url) or by_name.get(hotel_name)
        if not ta:
            continue
        row.update(ta)
        filled += 1
    print(f"⭐ TA backfill: заполнено {filled}/{len(missing)}")
    return filled


def _render_ta_rating_html(ta_rating: Any, ta_review_count: Any) -> str:
    rating = _parse_ta_rating_value(ta_rating)
    reviews = _parse_ta_review_count(ta_review_count)
    if rating is None:
        return (
            '<span class="ta-rating ta-rating--empty" data-sort-value="-1" '
            'title="Новый отель или нет отзывов TripAdvisor">'
            '<span class="ta-stars ta-stars--empty">—</span>'
            '<span class="ta-meta"><span class="ta-score">—</span></span>'
            '</span>'
        )
    full = int(rating)
    frac = rating - full
    stars = []
    for idx in range(1, 6):
        if rating >= idx:
            stars.append('<span class="ta-star ta-star--full">★</span>')
        elif idx - 1 < rating < idx and frac >= 0.25:
            stars.append('<span class="ta-star ta-star--half">★</span>')
        else:
            stars.append('<span class="ta-star ta-star--empty">☆</span>')
    stars_html = "".join(stars)
    reviews_html = (
        f'<span class="ta-reviews">{reviews} opinii</span>'
        if reviews > 0 else '<span class="ta-reviews ta-reviews--new">новый</span>'
    )
    return (
        f'<span class="ta-rating" data-sort-value="{rating:.2f}" '
        f'title="TripAdvisor {rating:.1f}/5 · {reviews} opinii">'
        f'<span class="ta-stars" style="--ta-fill:{min(100, rating / 5 * 100):.0f}%">{stars_html}</span>'
        f'<span class="ta-meta"><span class="ta-score">{rating:.1f}</span>{reviews_html}</span>'
        f'</span>'
    )


def _merge_departure_cohorts(*frames: pd.DataFrame) -> pd.DataFrame:
    parts = [frame for frame in frames if frame is not None and not frame.empty]
    if not parts:
        return pd.DataFrame()
    merged = pd.concat(parts, ignore_index=True, sort=False)
    key_cols = ["run_started_at", "departure_key"]
    if all(col in merged.columns for col in key_cols):
        merged = merged.drop_duplicates(subset=key_cols, keep="last")
    return merged


def _prepare_departure_cohorts(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    work = df.copy()
    work["_run_ts"] = pd.to_datetime(work["run_started_at"], errors="coerce", utc=True)
    work = work.dropna(subset=["_run_ts"])
    for col in [
        "hot_score", "hotel_count", "below_10000_count", "days_to_departure",
        "min_price", "p10_price", "median_price", "p10_change_pct",
        "median_change_pct", "min_change_pct", "hotel_count_delta", "common_hotel_count",
        "avg_deal_score", "mean_avg_delta_pct", "hot_deal_count", "good_deal_count",
        "median_ta_rating", "ta_rated_hotel_count",
    ]:
        if col in work.columns:
            work[col] = pd.to_numeric(work[col], errors="coerce")
    work = work[work["days_to_departure"].fillna(9999) >= 0]
    return work


def _load_departure_cohort_frames(data_dir: str, data_file: str):
    """Load cohorts + hot history from monitor cache (no full rebuild in CI)."""
    return load_stored_departure_cohorts(data_dir, travel_prices_file=data_file)


def _hotel_chart_viewer_href(filter_id: str, hotel_slug: str) -> str:
    return f"hotel-chart.html?filter={quote(str(filter_id))}&hotel={quote(str(hotel_slug))}"


def slugify(text: str) -> str:
    text = str(text).lower().strip()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = re.sub(r"-+", "-", text).strip('-')
    return text or "hotel"


def _hotel_series_payload_hash(payload: dict) -> str:
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _load_hotel_series_manifest(series_dir: str) -> dict:
    path = os.path.join(series_dir, "manifest.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_hotel_series_manifest(series_dir: str, manifest: dict) -> None:
    os.makedirs(series_dir, exist_ok=True)
    with open(os.path.join(series_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2, sort_keys=True)


def _parse_price_ceiling(display_price_ceiling):
    if display_price_ceiling is None:
        return None
    try:
        return float(display_price_ceiling)
    except (TypeError, ValueError):
        return None


def _resolve_history_ceiling(display_ceiling, history_ceiling):
    """History band for charts/vanished: defaults to 20k when display ceiling is set."""
    parsed = _parse_price_ceiling(history_ceiling)
    if parsed is not None:
        return parsed
    if display_ceiling is not None:
        return 20000.0
    return None


def _lowest_price_row(grp):
    """Строка с минимальной ценой среди офферов одного отеля."""
    prices = grp['price'].astype(float)
    return grp.loc[prices.idxmin()]


def _typical_observation_gap_hours(times, default=1.0):
    """Median interval between consecutive observations (hours), ignoring long data gaps."""
    gaps = []
    for i in range(1, len(times)):
        gaps.append((times[i] - times[i - 1]).total_seconds() / 3600.0)
    if not gaps:
        return max(default, 0.5)
    sorted_gaps = sorted(gaps)
    if len(sorted_gaps) >= 3:
        core = sorted_gaps[: max(1, (len(sorted_gaps) * 2) // 3)]
        typical = core[len(core) // 2]
    elif len(sorted_gaps) == 2:
        typical = sorted_gaps[0]
    else:
        typical = sorted_gaps[0]
    return max(typical, 0.5)


def _data_gap_threshold_hours(typical_gap):
    """Gap longer than this is treated as missing data, not price continuity."""
    return max(6.0, float(typical_gap) * 3.0)


def _iter_price_plateaus(times, prices):
    """Yield (price, duration_hours) for each constant-price plateau."""
    if not times or not prices:
        return
    if len(times) != len(prices):
        return

    typical_gap = _typical_observation_gap_hours(times)
    gap_threshold = _data_gap_threshold_hours(typical_gap)

    idx = 0
    while idx < len(prices):
        price = float(prices[idx])
        end = idx
        while end + 1 < len(prices) and float(prices[end + 1]) == price:
            end += 1
        if end + 1 < len(times):
            gap_after = (times[end + 1] - times[end]).total_seconds() / 3600.0
            if gap_after > gap_threshold:
                # No data after this plateau — do not count silence as this price.
                if end > idx:
                    hours = (times[end] - times[idx]).total_seconds() / 3600.0 + typical_gap
                else:
                    hours = typical_gap
            else:
                hours = (times[end + 1] - times[idx]).total_seconds() / 3600.0
        elif end > idx:
            hours = (times[end] - times[idx]).total_seconds() / 3600.0 + typical_gap
        elif idx > 0:
            hours = (times[idx] - times[idx - 1]).total_seconds() / 3600.0
        else:
            hours = typical_gap
        yield price, max(hours, 0.5)
        idx = end + 1


def _break_series_at_data_gaps(x_values, y_values, extra_series=None):
    """Insert None between points when the interval implies missing observations."""
    if not x_values or not y_values:
        return x_values, y_values, extra_series or []
    times = pd.to_datetime(pd.Series(x_values), utc=True).tolist()
    typical_gap = _typical_observation_gap_hours(times)
    gap_threshold = _data_gap_threshold_hours(typical_gap)
    extras = extra_series if extra_series is not None else []
    out_x, out_y = [], []
    out_extras = [[] for _ in extras] if extras else None
    for i in range(len(y_values)):
        if i > 0:
            gap_h = (times[i] - times[i - 1]).total_seconds() / 3600.0
            if gap_h > gap_threshold:
                out_x.append(None)
                out_y.append(None)
                if out_extras is not None:
                    for bucket in out_extras:
                        bucket.append(None)
        out_x.append(x_values[i])
        out_y.append(y_values[i])
        if out_extras is not None:
            for j, bucket in enumerate(out_extras):
                bucket.append(extras[j][i])
    if out_extras is None:
        return out_x, out_y, None
    return out_x, out_y, out_extras


def _time_weighted_price_baseline(grp, time_col='scraped_at_display', price_col='price'):
    """Typical price: weighted mean by how long each plateau was held (hours/days)."""
    segments = _price_plateau_segments(grp, time_col=time_col, price_col=price_col)
    if not segments:
        return None
    total_hours = sum(h for _, h in segments)
    if total_hours <= 0:
        return float(segments[-1][0])
    return sum(p * h for p, h in segments) / total_hours


def _price_plateau_segments(grp, time_col='scraped_at_display', price_col='price'):
    if grp is None or grp.empty:
        return []
    work = grp.sort_values(time_col).dropna(subset=[time_col, price_col])
    if work.empty:
        return []
    times = pd.to_datetime(work[time_col], utc=True).tolist()
    prices = work[price_col].astype(float).tolist()
    return list(_iter_price_plateaus(times, prices))


def _time_weighted_price_quantile(grp, quantile, time_col='scraped_at_display', price_col='price'):
    """Quantile weighted by plateau duration (not by scrape count)."""
    segments = _price_plateau_segments(grp, time_col=time_col, price_col=price_col)
    if not segments:
        return None
    q = max(0.0, min(1.0, float(quantile)))
    ordered = sorted(segments, key=lambda x: x[0])
    total_hours = sum(h for _, h in ordered)
    if total_hours <= 0:
        return float(ordered[-1][0])
    target = total_hours * q
    seen = 0.0
    for price, hours in ordered:
        seen += hours
        if seen >= target:
            return float(price)
    return float(ordered[-1][0])


def _time_weighted_price_volatility(grp, time_col='scraped_at_display', price_col='price'):
    """Coefficient of variation using duration-weighted mean/std."""
    segments = _price_plateau_segments(grp, time_col=time_col, price_col=price_col)
    if len(segments) < 2:
        return None
    total_hours = sum(h for _, h in segments)
    if total_hours <= 0:
        return None
    mean = sum(p * h for p, h in segments) / total_hours
    if mean <= 0:
        return None
    var = sum(h * (p - mean) ** 2 for p, h in segments) / total_hours
    return float(var ** 0.5 / mean)


def _last_run_slice(df, time_col='scraped_at_display', gap_minutes=5):
    """Последний ран скрейпа как DataFrame."""
    runs = list(iter_scrape_runs(df, time_col=time_col, gap_minutes=gap_minutes))
    if not runs:
        return df.iloc[0:0].copy()
    return runs[-1][2].copy()


def iter_scrape_runs(df, time_col='scraped_at_display', gap_minutes=5):
    """Итератор ранов скрейпа: (start_pos, end_pos, slice), позиции для iloc."""
    if df.empty:
        return
    df_time = df.dropna(subset=[time_col]).sort_values(time_col).reset_index(drop=True)
    tdiff = df_time[time_col].diff()
    boundaries = df_time.index[tdiff > pd.Timedelta(minutes=gap_minutes)].tolist()
    starts = [0] + boundaries
    ends = boundaries + [len(df_time)]
    for start_idx, end_idx in zip(starts, ends):
        yield start_idx, end_idx, df_time.iloc[start_idx:end_idx]


def collapse_canonical_per_run(df, ceiling_val=None, run_gap_minutes=5, group_cols=None):
    """One canonical row per hotel (and optional duration bucket) per scrape run.

    With a display ceiling, consider only offers at or below it, then pick
    the cheapest. Hotels only seen above the ceiling in a run are omitted
    from that band (they remain in the wider history band up to history ceiling).
    """
    group_cols = group_cols or ['hotel_name']
    if df.empty:
        return df.copy()

    rows = []
    for _, _, run_slice in iter_scrape_runs(df, gap_minutes=run_gap_minutes):
        for _, grp in run_slice.groupby(group_cols, sort=False):
            pick = grp.sort_values('scraped_at_display')
            if ceiling_val is not None:
                in_band = pick[pick['price'].astype(float) <= ceiling_val]
                if in_band.empty:
                    continue
                pick = in_band
            rows.append(_lowest_price_row(pick))

    if not rows:
        return pd.DataFrame(columns=df.columns)
    sort_cols = list(group_cols) + ['scraped_at_display']
    return pd.DataFrame(rows).sort_values(sort_cols).reset_index(drop=True)


def build_daily_offers_count_timeline(
    df,
    ceiling_val=None,
    group_cols=None,
    tz='Europe/Warsaw',
    pick='last',
    run_gap_minutes=5,
):
    """One point per calendar day: canonical offer count from first/last scrape run that day."""
    empty = {'dates': [], 'counts': [], 'meta': []}
    if df is None or df.empty:
        return empty

    group_cols = group_cols or ['hotel_name']
    canon = collapse_canonical_per_run(
        df,
        ceiling_val,
        group_cols=group_cols,
        run_gap_minutes=run_gap_minutes,
    )
    if canon.empty:
        return empty

    runs = []
    for _, _, run_slice in iter_scrape_runs(canon, gap_minutes=run_gap_minutes):
        if run_slice.empty:
            continue
        run_time = run_slice['scraped_at_display'].iloc[0]
        try:
            day = pd.Timestamp(run_time).tz_convert(tz).date()
        except Exception:
            day = pd.Timestamp(run_time).date()
        runs.append({
            'day': day,
            'time': run_time,
            'count': len(run_slice),
        })

    if not runs:
        return empty

    run_df = pd.DataFrame(runs).sort_values('time')
    if pick == 'first':
        daily = run_df.groupby('day', as_index=False).first()
    else:
        daily = run_df.groupby('day', as_index=False).last()

    meta = []
    dates = []
    counts = []
    for _, row in daily.iterrows():
        day_ts = pd.Timestamp(row['day'])
        run_ts = pd.Timestamp(row['time'])
        try:
            run_label = run_ts.tz_convert(tz).strftime('%d.%m.%Y %H:%M')
        except Exception:
            run_label = run_ts.strftime('%d.%m.%Y %H:%M')
        dates.append(day_ts.strftime('%Y-%m-%d'))
        counts.append(int(row['count']))
        meta.append({
            'day': day_ts.strftime('%d.%m.%Y'),
            'count': int(row['count']),
            'run_time': run_label,
            'pick': pick,
        })

    return {'dates': dates, 'counts': counts, 'meta': meta}


def _prepare_trip_duration_columns(df: pd.DataFrame, filter_config: dict) -> tuple[list[str], list[dict], bool]:
    buckets = (
        parse_trip_duration_buckets(filter_config)
        if is_fixed_trip_config(filter_config)
        else []
    )
    if not buckets:
        return ['hotel_name'], buckets, False
    if 'duration_bucket' not in df.columns:
        df['duration_bucket'] = ''
    mask = df['duration_bucket'].apply(
        lambda value: _duration_bucket_is_missing(value)
    )
    if mask.any():
        df.loc[mask, 'duration_bucket'] = df.loc[mask].apply(
            lambda row: infer_offer_duration_bucket(row.to_dict(), filter_config),
            axis=1,
        )
    known_ids = {str(b['id']) for b in buckets}
    stale = ~df['duration_bucket'].astype(str).isin(known_ids)
    if stale.any():
        df.loc[stale, 'duration_bucket'] = df.loc[stale].apply(
            lambda row: trip_duration_bucket_id(
                offer_duration_days(row.to_dict()), buckets
            ),
            axis=1,
        )
    df.drop(
        df[~df['duration_bucket'].astype(str).isin(known_ids)].index,
        inplace=True,
    )
    return ['hotel_name', 'duration_bucket'], buckets, True


def _unpack_table_group_key(group_key, use_trip_buckets: bool) -> tuple[str, str, str]:
    if use_trip_buckets:
        if isinstance(group_key, tuple):
            hotel_name = str(group_key[0])
            bucket = str(group_key[1] if len(group_key) > 1 else '')
        else:
            hotel_name = str(group_key)
            bucket = ''
        return hotel_name, bucket, trip_row_key(hotel_name, bucket)
    hotel_name = str(group_key[0] if isinstance(group_key, tuple) else group_key)
    return hotel_name, '', hotel_name


def _build_premium_history_index(df_history, display_ceiling, group_cols):
    out = {}
    if df_history is None or df_history.empty:
        return out
    use_buckets = 'duration_bucket' in group_cols
    for group_key, grp in df_history.groupby(group_cols, sort=False):
        _, _, row_id = _unpack_table_group_key(group_key, use_buckets)
        prices = grp['price'].astype(float)
        if prices.empty:
            continue
        premium_peak = robust_premium_peak(grp, display_ceiling, 'scraped_at_display', 'price')
        out[row_id] = {
            'history_max': float(prices.max()),
            'premium_peak': premium_peak,
        }
    return out


def classify_deal_badge(deal_score, confidence, delta48_pct=None, avg_pct=None, comeback_drop_pct=None):
    """Returns (label, css_class, display_badge)."""
    is_bad = (
        delta48_pct is not None and delta48_pct > 0
        and avg_pct is not None and avg_pct > 0
    )
    strong_comeback = comeback_drop_pct is not None and comeback_drop_pct >= 8.0
    if confidence == "Low" and not strong_comeback:
        return "Warm-up", "warm", "⏳ Warm-up"
    if is_bad and not strong_comeback:
        return "Bad", "bad", "📈 Bad"
    if deal_score >= 80:
        return "Hot", "hot", "🔥 Hot"
    if deal_score >= 65:
        return "Good", "good", "✅ Good"
    return "Normal", "normal", "↔️ Normal"


def determine_price_forecast(
    deal_score, confidence, avg_pct, delta48_pct, comeback_drop_pct=None,
):
    """Returns price forecast classification: {text, class, icon}."""
    strong_comeback = comeback_drop_pct is not None and comeback_drop_pct >= 8.0
    if confidence == "Low":
        if strong_comeback:
            return {'text': 'Наблюдать', 'class': 'forecast-wait', 'icon': '🟡'}
        return {'text': 'Мало данных', 'class': 'forecast-nodata', 'icon': '⏳'}

    avg_pct_val = float(avg_pct) if avg_pct is not None else 0.0
    delta48_pct_val = float(delta48_pct) if delta48_pct is not None else 0.0
    is_bad = (
        delta48_pct is not None and delta48_pct_val > 0
        and avg_pct is not None and avg_pct_val > 0
    )

    if strong_comeback:
        if avg_pct_val < -5.0 and delta48_pct_val <= 0.0:
            return {'text': 'Покупать', 'class': 'forecast-buy', 'icon': '🟢'}
        return {'text': 'Наблюдать', 'class': 'forecast-wait', 'icon': '🟡'}

    if avg_pct_val < -5.0:
        if delta48_pct_val <= 0.0:
            return {'text': 'Покупать', 'class': 'forecast-buy', 'icon': '🟢'}
        return {'text': 'Наблюдать', 'class': 'forecast-wait', 'icon': '🟡'}
    if -5.0 <= avg_pct_val <= 5.0:
        return {'text': 'Наблюдать', 'class': 'forecast-wait', 'icon': '🟡'}
    if is_bad or deal_score < 80:
        return {'text': 'Дорого', 'class': 'forecast-expensive', 'icon': '🔴'}
    return {'text': 'Наблюдать', 'class': 'forecast-wait', 'icon': '🟡'}


def _table_deal_badge_compact(display_badge: str) -> str:
    text = str(display_badge or "").strip()
    return text.split()[0] if text else ""


def _table_duration_compact(duration: str) -> str:
    match = re.search(r"(\d+)\s*dni/(\d+)\s*noc", str(duration), re.I)
    if match:
        return f"{match.group(1)}d/{match.group(2)}n"
    return str(duration)


def _metric_card(value_html, label, tip=""):
    tip_attr = f' title="{html_lib.escape(tip)}"' if tip else ""
    tip_cls = " metric-tip" if tip else ""
    return (
        f'<div class="metric{tip_cls}"{tip_attr}>'
        f'<div class="metric-value">{value_html}</div>'
        f'<div class="metric-label">{html_lib.escape(label)}</div>'
        f'</div>'
    )


def _should_show_alert(alert) -> bool:
    alert_type = alert.get('alert_type') or alert.get('type') or ''
    return alert_type != 'zone_entry'


def _hotel_base_name(name: str) -> str:
    text = str(name or "").strip()
    for marker in (" Grecja,", " Turcja,", " Grecja ", " Turcja "):
        if marker in text:
            return text.split(marker, 1)[0].strip()
    return text


def _register_chart_href(lookup: dict, hotel_name: str, href: str) -> None:
    name = str(hotel_name or "").strip()
    if not name or not href:
        return
    lookup[name] = href
    lookup[_hotel_base_name(name).lower()] = href
    slug = href.rsplit("/", 1)[-1]
    if slug.endswith(".html"):
        lookup[f"__slug__:{slug[:-5]}"] = href
    if "hotel=" in href:
        try:
            qs = parse_qs(urlparse(href).query)
            hotel_slug = (qs.get("hotel") or [""])[0]
            if hotel_slug:
                lookup[f"__slug__:{hotel_slug}"] = href
        except Exception:
            pass


def _resolve_chart_href(hotel_name: str, lookup: dict, charts_subdir: str, slugify_fn) -> str:
    name = str(hotel_name or "").strip()
    if not name:
        return ""
    if name in lookup:
        return lookup[name]
    base = _hotel_base_name(name).lower()
    if base in lookup:
        return lookup[base]
    slug = slugify_fn(name)
    slug_key = f"__slug__:{slug}"
    if slug_key in lookup:
        return lookup[slug_key]
    sub = (charts_subdir or "").rstrip("/")
    data_id = sub.split("/")[-1] if sub else ""
    if data_id.startswith("filter_"):
        return _hotel_chart_viewer_href(data_id, slug)
    return _hotel_chart_viewer_href(active_filter_id(charts_subdir), slug)


def _render_top_movers_html(decreases_48h, increases_48h, slugify_fn, filter_data_id):
    """Генерирует виджет Top Movers (топ снижений и роста цен за 48 часов)."""
    if not decreases_48h and not increases_48h:
        return ""

    def _render_list(items, is_drop=True):
        if not items:
            return '<div class="top-mover-empty">За последние 48ч изменения цен не зафиксированы</div>'
        rows = []
        for item in items[:3]:
            hotel_name = html_lib.escape(str(item['hotel_name']))
            slug = slugify_fn(item['hotel_name'])
            href = _hotel_chart_viewer_href(filter_data_id, slug)
            old_p = float(item['old_price'])
            new_p = float(item['new_price'])
            pct = float(item['change_percent'])
            cls = 'drop' if is_drop else 'up'
            sign = '' if pct < 0 else '+'
            arrow = '↓' if is_drop else '↑'
            rows.append(
                f'<a href="{href}" target="_blank" class="top-mover-item">'
                f'<span class="top-mover-name" title="{hotel_name}">{hotel_name}</span>'
                f'<span class="top-mover-prices">{old_p:.0f} → <strong>{new_p:.0f} PLN</strong></span>'
                f'<span class="top-mover-badge {cls}">{arrow} {sign}{pct:.1f}%</span>'
                f'</a>'
            )
        return "".join(rows)

    drops_html = _render_list(decreases_48h, is_drop=True)
    rises_html = _render_list(increases_48h, is_drop=False)

    return f"""
        <div class="top-movers-section">
            <div class="top-movers-head">
                <h3>📊 Динамика цен за 48 часов (Top Movers)</h3>
                <span class="top-movers-sub">Отели с наибольшим изменением стоимости</span>
            </div>
            <div class="top-movers-grid">
                <div class="top-movers-card top-movers-card--drops">
                    <div class="top-movers-card-title">📉 Лидеры снижения цены</div>
                    <div class="top-movers-list">{drops_html}</div>
                </div>
                <div class="top-movers-card top-movers-card--rises">
                    <div class="top-movers-card-title">📈 Лидеры роста цены</div>
                    <div class="top-movers-list">{rises_html}</div>
                </div>
            </div>
        </div>
    """



def _alert_is_current(alert, table_prices, tolerance=2.0, scope_duration_bucket=None):
    """Alert is current if the hotel is in the last run at the alert's new price."""
    hotel_name = str(alert.get('hotel_name') or alert.get('hotel') or '')
    alert_type = alert.get('alert_type') or alert.get('type') or ''
    new_price = alert.get('new_price') if 'new_price' in alert else (alert.get('to') or alert.get('current_price'))
    if not hotel_name or alert_type == 'missing' or new_price in (None, '', 'null'):
        return False
    alert_bucket = str(alert.get('duration_bucket') or '').strip()
    if _duration_bucket_is_missing(alert_bucket):
        alert_bucket = ''
    scope_bucket = str(scope_duration_bucket or '').strip()
    if scope_bucket:
        if alert_bucket and alert_bucket != scope_bucket:
            return False
        if not alert_bucket:
            return False
    row_id = trip_row_key(hotel_name, alert_bucket or scope_bucket) if (alert_bucket or scope_bucket) else hotel_name
    candidates = []
    if row_id in table_prices:
        candidates.append(float(table_prices[row_id]))
    if not scope_bucket:
        if hotel_name in table_prices:
            candidates.append(float(table_prices[hotel_name]))
        prefix = f"{hotel_name}|"
        for key, value in table_prices.items():
            if str(key).startswith(prefix):
                candidates.append(float(value))
    if not candidates:
        return False
    try:
        target = float(new_price)
    except (TypeError, ValueError):
        return False
    return any(abs(current - target) <= tolerance for current in candidates)


def _alert_display_fields(alert, meta, slugify_fn, parse_iso_fn):
    hotel_name = str(alert.get('hotel_name') or alert.get('hotel') or 'Unknown')
    hotel_name_html = meta.get('hotel_name_html') or html_lib.escape(hotel_name)
    default_href = _hotel_chart_viewer_href(
        meta.get("filter_id") or "filter",
        slugify_fn(hotel_name),
    )
    chart_href = html_lib.escape(meta.get('chart_href') or default_href, quote=True)
    offer_url = meta.get('offer_url') or ''
    dates = meta.get('dates') or '—'
    duration = meta.get('duration') or '—'

    alert_type = alert.get('alert_type') or alert.get('type') or ''
    old_price = alert.get('old_price') or alert.get('from') or alert.get('previous_price')
    new_price = alert.get('new_price') if 'new_price' in alert else (alert.get('to') or alert.get('current_price'))
    ts_raw = alert.get('created_at') or alert.get('timestamp') or alert.get('time') or ''
    try:
        ts_fmt = parse_iso_fn(ts_raw).astimezone().strftime('%d.%m.%Y %H:%M')
    except Exception:
        ts_fmt = str(ts_raw)

    if alert_type == 'zone_entry':
        kind = 'drop'
        badge = '+'
        new_cls = 'drop'
        change_pct = float(alert.get('price_change_pct') or 0.0)
        try:
            new_fmt = f"{float(new_price):.0f}"
        except (TypeError, ValueError):
            new_fmt = str(new_price)
        try:
            old_f = float(old_price) if old_price is not None else None
            new_f = float(new_price)
            if old_f is not None and abs(old_f - new_f) > 1:
                old_fmt = f"{old_f:.0f}"
                pct_text = f'{change_pct:+.1f}%'
            else:
                old_fmt = '—'
                pct_text = ''
        except (TypeError, ValueError):
            old_fmt = '—'
            pct_text = ''
        note = 'Вошёл в зону отслеживания'
    elif alert_type == 'zone_exit':
        kind = 'up'
        badge = '↑'
        new_cls = 'up'
        change_pct = float(alert.get('price_change_pct') or 0.0)
        pct_text = f'{change_pct:+.1f}%' if change_pct else ''
        try:
            old_fmt = f"{float(old_price):.0f}"
            new_fmt = f"{float(new_price):.0f}"
        except (TypeError, ValueError):
            old_fmt = str(old_price)
            new_fmt = str(new_price)
        note = 'Вышел из зоны отслеживания'
    elif alert_type == 'missing' or new_price in (None, '', 'null'):
        kind = 'missing'
        badge = '—'
        pct_text = ''
        try:
            old_fmt = f"{float(old_price):.0f}" if old_price is not None else '—'
        except (TypeError, ValueError):
            old_fmt = str(old_price or '—')
        new_fmt = '—'
        new_cls = ''
        note = html_lib.escape(alert.get('message') or alert.get('note') or 'Исчез из выдачи')
    else:
        change_pct = float(alert.get('price_change_pct') or 0.0)
        price_change = float(alert.get('price_change') or 0.0)
        pct_text = f'{change_pct:+.1f}%'
        if alert_type == 'premium_comeback':
            pct_text = f'↩ {pct_text}'
        if price_change > 0:
            kind = 'up'
            badge = '↑'
            new_cls = 'up'
        elif price_change < 0:
            kind = 'drop'
            badge = '↓'
            new_cls = 'drop'
        else:
            kind = 'missing'
            badge = '→'
            new_cls = ''
        try:
            old_fmt = f"{float(old_price):.0f}"
            new_fmt = f"{float(new_price):.0f}"
        except (TypeError, ValueError):
            old_fmt = str(old_price)
            new_fmt = str(new_price)
        note = ''

    meta_line = f'{html_lib.escape(str(dates))} · {html_lib.escape(str(duration))}'
    return {
        'alert_type': alert_type,
        'kind': kind,
        'badge': badge,
        'hotel_name_html': hotel_name_html,
        'chart_href': chart_href,
        'offer_url': offer_url,
        'old_fmt': old_fmt,
        'new_fmt': new_fmt,
        'new_cls': new_cls,
        'pct_text': pct_text,
        'ts_fmt': ts_fmt,
        'meta_line': meta_line,
        'note': note,
    }


def _render_note_price_block(d, *, as_history=False):
    """Price row for zone_entry / zone_exit alerts (with note, not plain price change)."""
    if d['alert_type'] == 'zone_entry':
        if d['old_fmt'] != '—' and d['pct_text']:
            if as_history:
                return (
                    f'<span class="alert-history-old">{d["old_fmt"]}</span>'
                    f'<span class="alert-history-arrow">→</span>'
                    f'<span class="alert-history-new {d["new_cls"]}">{d["new_fmt"]}</span>'
                ), f'<span class="alert-history-pct {d["new_cls"]}">{d["pct_text"]}</span>'
            pct_block = f'<span class="alert-change-pct {d["new_cls"]}">{d["pct_text"]}</span>'
            return (
                f'<div class="alert-price-row">'
                f'<span class="alert-price-old">{d["old_fmt"]}</span>'
                f'<span aria-hidden="true">→</span>'
                f'<span class="alert-price-new {d["new_cls"]}">{d["new_fmt"]}</span>'
                f'{pct_block}'
                f'</div>'
            ), None
        if as_history:
            return f'<span class="alert-history-new {d["new_cls"]}">{d["new_fmt"]} PLN</span>', None
        return f'<div class="alert-price-row"><span class="alert-price-new {d["new_cls"]}">{d["new_fmt"]} PLN</span></div>', None

    if as_history:
        return f'<span class="alert-history-new">{d["old_fmt"]} PLN</span>', None
    return f'<div class="alert-price-row"><span class="alert-price-new">{d["old_fmt"]} PLN</span></div>', None


def _render_alert_card(alert, meta, slugify_fn, parse_iso_fn):
    d = _alert_display_fields(alert, meta, slugify_fn, parse_iso_fn)
    image_url = meta.get('image_url') or ''
    if image_url:
        img_block = (
            f'<img src="{html_lib.escape(image_url, quote=True)}" alt="" loading="lazy" '
            f'onerror="this.onerror=null;this.parentElement.innerHTML=\'<div>🏨</div>\';" />'
        )
    else:
        img_block = '<div>🏨</div>'

    offer_btn = (
        f'<a class="card-btn" href="{html_lib.escape(d["offer_url"], quote=True)}" target="_blank" rel="noopener">Оффер</a>'
        if d['offer_url'] else
        '<span class="card-btn" style="opacity:.55;">—</span>'
    )

    if d['note']:
        price_block, _ = _render_note_price_block(d)
        sub_line = f'{d["note"]} · {d["meta_line"]}'
    else:
        pct_block = f'<span class="alert-change-pct {d["new_cls"]}">{d["pct_text"]}</span>' if d['pct_text'] else ''
        price_block = (
            f'<div class="alert-price-row">'
            f'<span class="alert-price-old">{d["old_fmt"]}</span>'
            f'<span aria-hidden="true">→</span>'
            f'<span class="alert-price-new {d["new_cls"]}">{d["new_fmt"]}</span>'
            f'{pct_block}'
            f'</div>'
        )
        sub_line = f'{d["ts_fmt"]} · {d["meta_line"]}'

    return f"""
                    <article class="alert-card {d["kind"]}">
                        <div class="alert-card-img">{img_block}</div>
                        <div class="alert-card-body">
                            <div class="alert-card-top">
                                <h4 class="alert-card-title"><a href="{d["chart_href"]}" target="_blank" rel="noopener">{d["hotel_name_html"]}</a></h4>
                                <span class="alert-badge {d["kind"]}">{d["badge"]}</span>
                            </div>
                            {price_block}
                            <div class="alert-card-meta">{sub_line}</div>
                            <div class="alert-card-actions">
                                <a class="card-btn primary" href="{d["chart_href"]}" target="_blank" rel="noopener">График</a>
                                {offer_btn}
                            </div>
                        </div>
                    </article>
"""


def _render_alert_history_row(alert, meta, slugify_fn, parse_iso_fn):
    d = _alert_display_fields(alert, meta, slugify_fn, parse_iso_fn)
    if d['note']:
        price_html, pct_html = _render_note_price_block(d, as_history=True)
        note_html = f'<span class="alert-history-pct">{html_lib.escape(d["note"])}</span>'
        pct_html = pct_html or note_html
    else:
        price_html = (
            f'<span class="alert-history-old">{d["old_fmt"]}</span>'
            f'<span class="alert-history-arrow">→</span>'
            f'<span class="alert-history-new {d["new_cls"]}">{d["new_fmt"]}</span>'
        )
        pct_html = f'<span class="alert-history-pct {d["new_cls"]}">{d["pct_text"]}</span>' if d['pct_text'] else ''

    offer_link = (
        f'<a class="alert-history-offer" href="{html_lib.escape(d["offer_url"], quote=True)}" target="_blank" rel="noopener" title="Оффер">🔗</a>'
        if d['offer_url'] else ''
    )
    return f"""
                        <div class="alert-history-row {d["kind"]}">
                            <span class="alert-history-badge {d["kind"]}" aria-hidden="true">{d["badge"]}</span>
                            <div class="alert-history-info">
                                <a class="alert-history-name" href="{d["chart_href"]}" target="_blank" rel="noopener">{d["hotel_name_html"]}</a>
                                <span class="alert-history-sub">{d["ts_fmt"]} · {d["meta_line"]}</span>
                            </div>
                            <div class="alert-history-price">{price_html}{pct_html}</div>
                            <div class="alert-history-links">
                                <a class="alert-history-chart" href="{d["chart_href"]}" target="_blank" rel="noopener">График</a>
                                {offer_link}
                            </div>
                        </div>
"""


def _alert_matches_scope(alert, scope_hotel_names: set, scope_duration_bucket: str = '') -> bool:
    hotel_name = str(alert.get('hotel_name') or alert.get('hotel') or '')
    if hotel_name not in scope_hotel_names:
        return False
    scope_bucket = str(scope_duration_bucket or '').strip()
    if not scope_bucket:
        return True
    alert_bucket = str(alert.get('duration_bucket') or '').strip()
    if _duration_bucket_is_missing(alert_bucket):
        return False
    return alert_bucket == scope_bucket


def _build_alerts_panel_html(
    *,
    alerts: list,
    table_prices: dict,
    premium_history_by_hotel: dict,
    scope_hotel_names: set,
    hotel_meta_by_name: dict,
    latest_run_ts,
    ceiling_val,
    alert_threshold_percent: float,
    parse_iso_fn,
    scope_duration_bucket: str = '',
) -> tuple[str, str]:
    """Build alert summary chips and inner content for one duration scope."""
    from price_alerts_v2 import ALERT_THRESHOLD_PERCENT

    threshold = float(alert_threshold_percent or ALERT_THRESHOLD_PERCENT)
    scoped_alerts = [
        a for a in alerts
        if _alert_matches_scope(a, scope_hotel_names, scope_duration_bucket)
    ]

    current_alerts = []
    current_keys = set()
    for alert in scoped_alerts:
        if not _alert_is_current(alert, table_prices, scope_duration_bucket=scope_duration_bucket):
            continue
        hotel_name = str(alert.get('hotel_name') or alert.get('hotel') or '')
        unique_key = alert.get('unique_key') or f"{hotel_name}:{alert.get('new_price')}"
        if unique_key in current_keys:
            continue
        current_keys.add(unique_key)
        current_alerts.append(alert)

    for row_id, price in table_prices.items():
        hotel_name = str(row_id).split('|', 1)[0] if '|' in str(row_id) else str(row_id)
        row_bucket = str(row_id).split('|', 1)[1] if '|' in str(row_id) else ''
        if hotel_name not in scope_hotel_names:
            continue
        if scope_duration_bucket and row_bucket != scope_duration_bucket:
            continue
        comeback_key = f"{row_id}_comeback"
        if comeback_key in current_keys:
            continue
        comeback = comeback_from_premium(
            price, premium_history_by_hotel.get(row_id), ceiling_val
        )
        if not comeback or comeback['drop_from_peak_pct'] < threshold:
            continue
        peak = float(comeback['peak_price'])
        curr = float(price)
        current_alerts.append({
            'hotel_name': hotel_name,
            'duration_bucket': row_bucket,
            'old_price': peak,
            'new_price': curr,
            'price_change': curr - peak,
            'price_change_pct': (curr - peak) / peak * 100.0 if peak else 0.0,
            'timestamp': latest_run_ts,
            'alert_type': 'premium_comeback',
            'created_at': latest_run_ts.isoformat() if latest_run_ts is not None else '',
            'threshold_percent': threshold,
            'unique_key': f"{row_id}_comeback_{peak:.0f}_to_{curr:.0f}",
        })
        current_keys.add(comeback_key)

    current_alert_keys = {a.get('unique_key') for a in current_alerts if a.get('unique_key')}
    history_alerts = [
        a for a in scoped_alerts
        if not a.get('unique_key') or a.get('unique_key') not in current_alert_keys
    ]

    def _count_alert_kinds(items):
        drops = sum(1 for a in items if float(a.get('price_change') or 0) < 0)
        ups = sum(1 for a in items if float(a.get('price_change') or 0) > 0)
        return drops, ups

    cur_drops, cur_ups = _count_alert_kinds(current_alerts)
    chips_html = ""
    if current_alerts:
        if cur_drops:
            chips_html += f'<span class="alert-chip drop">↓ {cur_drops} подешевело сейчас</span>'
        if cur_ups:
            chips_html += f'<span class="alert-chip up">↑ {cur_ups} подорожало сейчас</span>'
    if history_alerts:
        chips_html += f'<span class="alert-chip missing">🕘 {len(history_alerts)} в истории</span>'

    content_parts = []
    if scoped_alerts or current_alerts:
        if current_alerts:
            content_parts.append(
                f'<p class="alerts-section-label">Действует сейчас · {len(current_alerts)}</p>'
                '<div class="alerts-grid">'
            )
            for alert in current_alerts:
                hotel_name = str(alert.get('hotel_name') or alert.get('hotel') or 'Unknown')
                meta = hotel_meta_by_name.get(hotel_name, {})
                content_parts.append(_render_alert_card(alert, meta, slugify, parse_iso_fn))
            content_parts.append('</div>')
        else:
            content_parts.append(
                '<div class="alerts-empty">Сейчас нет отелей с актуальным изменением цены — '
                'зафиксированные сдвиги уже устарели. Смотрите «Историю» ниже.</div>'
            )
        if history_alerts:
            content_parts.append(
                f'<details class="alerts-history-fold" onclick="event.stopPropagation()">'
                f'<summary>🕘 Показать историю ({len(history_alerts)} прошлых изменений)</summary>'
                '<div class="alert-history-list">'
            )
            for alert in history_alerts:
                hotel_name = str(alert.get('hotel_name') or alert.get('hotel') or 'Unknown')
                meta = hotel_meta_by_name.get(hotel_name, {})
                content_parts.append(_render_alert_history_row(alert, meta, slugify, parse_iso_fn))
            content_parts.append('</div></details>')
    else:
        content_parts.append(
            f'<div class="alerts-empty">Пока нет изменений от {threshold:.0f}% — '
            'они появятся здесь после следующих проверок.</div>'
        )

    return chips_html, ''.join(content_parts)


def _compute_chart_point_meta(y_values, alert_threshold, ceiling_val=None):
    """Marker sizes/colors and step % for each price point."""
    sizes = []
    colors = []
    step_pcts = []
    prev_price = None
    for i, price in enumerate(y_values):
        if price is None:
            sizes.append(0)
            colors.append('rgba(0,0,0,0)')
            step_pcts.append(0.0)
            continue
        price = float(price)
        # После разрыва (None) сравниваем с последней реальной ценой до паузы.
        if prev_price is None and i > 0:
            for j in range(i - 1, -1, -1):
                if y_values[j] is not None:
                    prev_price = float(y_values[j])
                    break
        if prev_price is None:
            pct = 0.0
        else:
            pct = ((price - prev_price) / prev_price * 100.0) if prev_price else 0.0
        step_pcts.append(round(pct, 1))
        above_ceiling = ceiling_val is not None and price > float(ceiling_val)
        crossed_gap = i > 0 and y_values[i - 1] is None
        if prev_price is not None and abs(pct) >= alert_threshold:
            sizes.append(15 if crossed_gap else 14)
            colors.append('#ef4444' if pct > 0 else '#10b981')
        elif crossed_gap:
            sizes.append(12)
            colors.append('#6366f1')
        elif above_ceiling:
            sizes.append(9 if i == len(y_values) - 1 else 8)
            colors.append('#f59e0b')
        else:
            sizes.append(8 if i == len(y_values) - 1 else 7)
            colors.append('#6366f1' if i == len(y_values) - 1 else '#4f46e5')
        # Подсветить точку перед паузой, если после паузы цена заметно изменилась.
        if i < len(y_values) - 1:
            for j in range(i + 1, len(y_values)):
                if y_values[j] is None:
                    continue
                jump_pct = ((float(y_values[j]) - price) / price * 100.0) if price else 0.0
                if abs(jump_pct) >= alert_threshold:
                    sizes[-1] = max(sizes[-1], 13)
                    colors[-1] = '#ef4444' if jump_pct > 0 else '#10b981'
                break
        prev_price = price
    if sizes:
        for j in range(len(sizes) - 1, -1, -1):
            if y_values[j] is not None:
                sizes[j] = max(sizes[j], 10)
                break
    return sizes, colors, step_pcts


def _build_gap_jump_series(chart_x, chart_y, alert_threshold):
    """Пунктир через паузу в данных, если цена заметно изменилась."""
    jump_x, jump_y = [], []
    last_idx = None
    for i, y in enumerate(chart_y):
        if y is None:
            continue
        if last_idx is not None:
            gap_between = any(v is None for v in chart_y[last_idx + 1:i])
            if gap_between and last_idx + 1 < i:
                prev_y = float(chart_y[last_idx])
                pct = ((float(y) - prev_y) / prev_y * 100.0) if prev_y else 0.0
                if abs(pct) >= max(3.0, float(alert_threshold) * 0.5):
                    jump_x.extend([chart_x[last_idx], chart_x[i], None])
                    jump_y.extend([prev_y, float(y), None])
        last_idx = i
    return jump_x, jump_y


def _render_hotel_chart_page(
    hotel_name,
    hotel_name_html,
    x_values,
    y_values,
    hover_lines,
    meta,
    back_href,
    deal_score,
    deal_label,
    deal_class,
    delta48_str,
    delta_avg_str,
    confidence,
    median_p,
    min_p,
    max_p,
    samples,
    alert_threshold,
    trip_dates_label,
    display_price_ceiling=None,
    history_price_ceiling=None,
    favicon_href="favicon.svg",
):
    current_p = float(y_values[-1]) if y_values else 0.0
    is_at_min = bool(y_values) and current_p <= min_p + 2.0
    above_ceiling_now = (
        display_price_ceiling is not None
        and y_values
        and current_p > float(display_price_ceiling)
    )
    _, _, step_pcts_raw = _compute_chart_point_meta(
        y_values, alert_threshold, display_price_ceiling
    )
    chart_x = x_values
    chart_y = y_values
    chart_hover = hover_lines

    enriched_hover = []
    prev_price = None
    for i, base in enumerate(chart_hover):
        price = float(chart_y[i]) if i < len(chart_y) else 0.0
        if prev_price is not None and prev_price > 0:
            pct = (price - prev_price) / prev_price * 100.0
        else:
            pct = 0.0
        extra = f'<br>Δ к прошлому замеру: {pct:+.1f}%' if i > 0 else ''
        if i > 0 and abs(pct) >= alert_threshold:
            extra += '<br><b>Заметное изменение</b>'
        enriched_hover.append((base or '') + extra)
        prev_price = price

    image_url = meta.get('image_url') or ''
    offer_url = meta.get('offer_url') or ''
    dates = html_lib.escape(str(meta.get('dates') or '—'))
    duration = html_lib.escape(str(meta.get('duration') or '—'))

    if image_url:
        img_html = (
            f'<img src="{html_lib.escape(image_url, quote=True)}" alt="" loading="lazy" '
            f'onerror="this.onerror=null;this.parentElement.innerHTML=\'<div class=\\\'chart-hero-placeholder\\\'>🏨</div>\';" />'
        )
    else:
        img_html = '<div class="chart-hero-placeholder">🏨</div>'

    offer_btn = (
        f'<a class="chart-btn primary" href="{html_lib.escape(offer_url, quote=True)}" target="_blank" rel="noopener">Открыть оффер</a>'
        if offer_url else
        '<span class="chart-btn disabled">Оффер недоступен</span>'
    )
    min_badge = '<span class="chart-min-badge">🔥 Исторический минимум</span>' if is_at_min else ''
    ceiling_badge = (
        f'<span class="chart-ceiling-badge">Выше потолка показа (&gt;{display_price_ceiling:.0f})</span>'
        if above_ceiling_now else ''
    )

    recent_rows = ''
    for i in range(max(0, len(x_values) - 5), len(x_values)):
        step = step_pcts_raw[i] if i < len(step_pcts_raw) else 0.0
        step_cls = 'up' if step > 0 else ('drop' if step < 0 else 'flat')
        step_txt = f'{step:+.1f}%' if i > 0 else '—'
        try:
            x_label = pd.to_datetime(x_values[i]).strftime('%d.%m.%Y %H:%M')
        except Exception:
            x_label = str(x_values[i])
        recent_rows += (
            f'<tr><td>{html_lib.escape(x_label)}</td>'
            f'<td><strong>{float(y_values[i]):.0f}</strong> PLN</td>'
            f'<td class="{step_cls}">{step_txt}</td></tr>'
        )

    title_esc = html_lib.escape(str(hotel_name))
    back_href_esc = html_lib.escape(back_href, quote=True)
    meta_desc = html_lib.escape(
        f'{hotel_name}: {current_p:.0f} PLN. Deal Score {deal_score} {deal_label}.'
    )

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta name="description" content="{meta_desc}">
    <meta property="og:title" content="{title_esc} — {current_p:.0f} PLN">
    <meta property="og:description" content="{meta_desc}">
    <meta property="og:image" content="{html_lib.escape(image_url or '')}">
    <meta property="og:type" content="website">
    <title>{title_esc} — {current_p:.0f} PLN</title>
    <link rel="icon" href="{html_lib.escape(favicon_href)}" type="image/svg+xml">
    <link rel="apple-touch-icon" href="{html_lib.escape(favicon_href)}">
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
    <script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
    <style>
        :root {{
            --gradient-primary: linear-gradient(135deg, #4f46e5 0%, #0ea5e9 100%);
            --border-soft: rgba(148, 163, 184, 0.28);
            --text-muted: #64748b;
            --radius-lg: 14px;
            --shadow-md: 0 8px 24px rgba(15, 23, 42, 0.08);
        }}
        * {{ box-sizing: border-box; }}
        body {{
            margin: 0;
            font-family: 'Inter', sans-serif;
            background: #f3f6ff;
            color: #0f172a;
            line-height: 1.45;
        }}
        .page {{
            max-width: 980px;
            margin: 0 auto;
            padding: 1rem 1rem 2rem;
        }}
        .chart-topbar {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: .75rem;
            margin-bottom: .85rem;
        }}
        .chart-back {{
            color: #4f46e5;
            text-decoration: none;
            font-weight: 600;
            font-size: .88rem;
        }}
        .chart-back:hover {{ text-decoration: underline; }}
        .chart-topbar-actions {{
            display: flex;
            gap: .5rem;
            align-items: center;
        }}
        .chart-btn {{
            display: inline-flex;
            align-items: center;
            justify-content: center;
            padding: .45rem .85rem;
            border-radius: 10px;
            font-size: .82rem;
            font-weight: 700;
            text-decoration: none;
            border: 1px solid var(--border-soft);
            background: #fff;
            color: #1e293b;
            cursor: pointer;
            transition: all .15s ease;
        }}
        .chart-btn:hover {{ background: #f8fafc; border-color: #cbd5e1; }}
        .chart-btn.primary {{
            background: var(--gradient-primary);
            color: #fff;
            border-color: transparent;
        }}
        .chart-btn.disabled {{
            opacity: .55;
            cursor: default;
        }}
        .chart-hero {{
            display: grid;
            grid-template-columns: 140px minmax(0, 1fr);
            gap: 1rem;
            background: #fff;
            border: 1px solid var(--border-soft);
            border-radius: var(--radius-lg);
            box-shadow: var(--shadow-md);
            overflow: hidden;
            margin-bottom: .85rem;
        }}
        .chart-hero-img {{
            min-height: 120px;
            background: linear-gradient(135deg, rgba(79,70,229,.12), rgba(14,165,233,.12));
        }}
        .chart-hero-img img {{
            width: 100%;
            height: 100%;
            object-fit: cover;
            display: block;
            min-height: 120px;
        }}
        .chart-hero-placeholder {{
            display: flex;
            align-items: center;
            justify-content: center;
            min-height: 120px;
            font-size: 2rem;
            opacity: .55;
        }}
        .chart-hero-body {{
            padding: .85rem .95rem .9rem 0;
        }}
        .chart-hero-body h1 {{
            margin: 0 0 .35rem;
            font-size: 1.15rem;
            line-height: 1.25;
        }}
        .chart-hero-meta {{
            margin: 0 0 .55rem;
            font-size: .82rem;
            color: var(--text-muted);
        }}
        .chart-hero-price-row {{
            display: flex;
            flex-wrap: wrap;
            align-items: center;
            gap: .45rem;
        }}
        .chart-current-price {{
            font-size: 1.65rem;
            font-weight: 800;
            background: var(--gradient-primary);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
        }}
        .deal-pill {{
            border-radius: 999px;
            padding: .18rem .55rem;
            font-size: .72rem;
            font-weight: 700;
            border: 1px solid transparent;
        }}
        .deal-pill.hot {{ background: rgba(245,158,11,.18); color: #92400e; border-color: rgba(245,158,11,.32); }}
        .deal-pill.good {{ background: rgba(16,185,129,.17); color: #065f46; border-color: rgba(16,185,129,.32); }}
        .deal-pill.normal {{ background: rgba(148,163,184,.18); color: #334155; border-color: rgba(148,163,184,.35); }}
        .deal-pill.bad {{ background: rgba(239,68,68,.15); color: #991b1b; border-color: rgba(239,68,68,.32); }}
        .deal-pill.warm {{ background: rgba(14,165,233,.16); color: #0c4a6e; border-color: rgba(14,165,233,.35); }}
        .chart-min-badge {{
            font-size: .72rem;
            font-weight: 700;
            color: #b45309;
            background: rgba(245,158,11,.14);
            border: 1px solid rgba(245,158,11,.28);
            border-radius: 999px;
            padding: .15rem .5rem;
        }}
        .chart-ceiling-badge {{
            font-size: .72rem;
            font-weight: 700;
            color: #b45309;
            background: rgba(245,158,11,.14);
            border: 1px solid rgba(245,158,11,.28);
            border-radius: 999px;
            padding: .15rem .5rem;
        }}
        .chart-kpis {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
            gap: .55rem;
            margin-bottom: .85rem;
        }}
        .chart-kpi {{
            background: #fff;
            border: 1px solid var(--border-soft);
            border-radius: 12px;
            padding: .55rem .65rem;
            box-shadow: var(--shadow-md);
        }}
        .chart-kpi .v {{
            font-size: 1.02rem;
            font-weight: 800;
            color: #111827;
        }}
        .chart-kpi .l {{
            font-size: .72rem;
            color: var(--text-muted);
            font-weight: 600;
            margin-top: .12rem;
        }}
        .chart-kpi .v.drop {{ color: #047857; }}
        .chart-kpi .v.up {{ color: #b91c1c; }}
        .chart-panel {{
            background: #fff;
            border: 1px solid var(--border-soft);
            border-radius: var(--radius-lg);
            box-shadow: var(--shadow-md);
            padding: .65rem .45rem .25rem;
            margin-bottom: .85rem;
        }}
        .chart-panel h2 {{
            margin: 0 0 .35rem .55rem;
            font-size: .95rem;
            color: #334155;
        }}
        #chart {{ height: 420px; }}
        .chart-recent {{
            background: #fff;
            border: 1px solid var(--border-soft);
            border-radius: var(--radius-lg);
            box-shadow: var(--shadow-md);
            padding: .65rem .75rem .75rem;
        }}
        .chart-recent h3 {{
            margin: 0 0 .45rem;
            font-size: .88rem;
            color: #334155;
        }}
        .chart-recent table {{
            width: 100%;
            border-collapse: collapse;
            font-size: .78rem;
        }}
        .chart-recent th, .chart-recent td {{
            padding: .35rem .25rem;
            border-bottom: 1px solid rgba(148,163,184,.18);
            text-align: left;
        }}
        .chart-recent td.drop {{ color: #047857; font-weight: 700; }}
        .chart-recent td.up {{ color: #b91c1c; font-weight: 700; }}
        .chart-recent td.flat {{ color: #64748b; }}
        .chart-legend-note {{
            margin: .35rem .55rem .5rem;
            font-size: .72rem;
            color: var(--text-muted);
        }}
        @media (max-width: 640px) {{
            .chart-hero {{ grid-template-columns: 1fr; }}
            .chart-hero-body {{ padding: .75rem; }}
            .chart-hero-img {{ max-height: 140px; }}
            .chart-topbar {{
                flex-wrap: wrap;
                align-items: flex-start;
            }}
            .chart-btn {{
                width: 100%;
            }}
            #chart {{ height: 320px; }}
        }}
    </style>
    <!-- Cloudflare Web Analytics --><script defer src='https://static.cloudflareinsights.com/beacon.min.js' data-cf-beacon='{{"token": "1b9c3c0ee6164106a1cb5eda9e61a045"}}'></script><!-- End Cloudflare Web Analytics -->
</head>
<body>
    <div class="page">
        <div class="chart-topbar">
            <a class="chart-back" href="{back_href_esc}">← Назад к дашборду</a>
            <div class="chart-topbar-actions">
                <button type="button" class="chart-btn" id="copyLinkBtn" onclick="copyHotelLink()">📋 Скопировать ссылку</button>
                {offer_btn}
            </div>
        </div>
        <section class="chart-hero">
            <div class="chart-hero-img">{img_html}</div>
            <div class="chart-hero-body">
                <h1>{hotel_name_html}</h1>
                <p class="chart-hero-meta">🗓️ {dates} · ⏱️ {duration}</p>
                <div class="chart-hero-price-row">
                    <span class="chart-current-price">{current_p:.0f} PLN</span>
                    <span class="deal-pill {deal_class}">Deal {deal_score} · {html_lib.escape(deal_label)}</span>
                    {min_badge}
                    {ceiling_badge}
                    {offer_btn}
                </div>
            </div>
        </section>
        <section class="chart-kpis">
            <div class="chart-kpi" title="Изменение цены за последние 48 часов"><div class="v {'drop' if delta48_str.startswith('-') else ('up' if delta48_str.startswith('+') else '')}">{html_lib.escape(delta48_str)}</div><div class="l">Δ за 48ч</div></div>
            <div class="chart-kpi" title="Отклонение от средней цены этого отеля за всё время наблюдений"><div class="v {'drop' if delta_avg_str.startswith('-') else ('up' if delta_avg_str.startswith('+') else '')}">{html_lib.escape(delta_avg_str)}</div><div class="l">Δ к своей средней</div></div>
            <div class="chart-kpi" title="Самая низкая цена зафиксированная для этого отеля"><div class="v">{min_p:.0f} PLN</div><div class="l">Минимум истории</div></div>
            <div class="chart-kpi" title="Медианное значение цены за время мониторинга"><div class="v">{median_p:.0f} PLN</div><div class="l">Ср. по времени</div></div>
            <div class="chart-kpi" title="Самая высокая цена зафиксированная для этого отеля"><div class="v">{max_p:.0f} PLN</div><div class="l">Максимум</div></div>
            <div class="chart-kpi" title="Количество проверок и степень уверенности оценки (Low/Medium/High)"><div class="v">{samples}</div><div class="l">Замеров · {html_lib.escape(confidence)}</div></div>
        </section>
        <section class="chart-panel">
            <h2>История цен</h2>
            <p class="chart-legend-note">Сплошная линия — история цены отеля в этом фильтре. Зелёная зона снизу — область хорошей выгоды (ниже средней цены).</p>
            <div id="chart"></div>
        </section>
        <section class="chart-recent">
            <h3>Последние замеры</h3>
            <table>
                <thead><tr><th>Время</th><th>Цена</th><th>Δ к прошлому</th></tr></thead>
                <tbody>{recent_rows}</tbody>
            </table>
        </section>
    </div>
    <script>
      function copyHotelLink() {{
        const btn = document.getElementById('copyLinkBtn');
        navigator.clipboard.writeText(window.location.href).then(function() {{
          if (btn) {{
            const orig = btn.textContent;
            btn.textContent = '✅ Ссылка скопирована!';
            setTimeout(function() {{ btn.textContent = orig; }}, 2000);
          }}
        }}).catch(function(e) {{
          prompt('Скопируйте ссылку:', window.location.href);
        }});
      }}

      const x = {json.dumps(chart_x, ensure_ascii=False)};
      const y = {json.dumps(chart_y, ensure_ascii=False)};
      const text = {json.dumps(enriched_hover, ensure_ascii=False)};
      const mainTrace = {{
        x, y, text,
        type: 'scatter',
        mode: 'lines+markers',
        name: 'Цена отеля',
        line: {{ color: '#4f46e5', width: 2.5 }},
        marker: {{ size: 6, color: '#4f46e5', line: {{ width: 1, color: '#fff' }} }},
        connectgaps: true,
        hovertemplate: '<b>%{{y:.0f}} PLN</b><br>%{{text}}<extra></extra>'
      }};
      const tripDatesLabel = {json.dumps(str(trip_dates_label or '—'), ensure_ascii=False)};
      const medianP = {median_p:.0f};
      const yDataMin = y.length ? Math.min(...y) : 0;
      const yDataMax = y.length ? Math.max(...y) : 0;
      const ySpan = Math.max(yDataMax - yDataMin, 1);
      const yPad = Math.max(ySpan * 0.08, 80);

      // Линия средней цены
      const medianTrace = {{
        x: x.length ? [x[0], x[x.length - 1]] : [],
        y: [medianP, medianP],
        type: 'scatter',
        mode: 'lines',
        name: 'Средняя цена',
        line: {{ color: 'rgba(148,163,184,0.7)', width: 1.5, dash: 'dot' }},
        hovertemplate: 'Средняя: <b>%{{y:.0f}} PLN</b><extra></extra>'
      }};

      // Зелёная зона хорошей выгоды (ниже средней цены)
      const shapes = [];
      if (y.length && medianP > yDataMin) {{
        shapes.push({{
          type: 'rect',
          xref: 'paper',
          yref: 'y',
          x0: 0,
          x1: 1,
          y0: yDataMin - yPad,
          y1: medianP,
          fillcolor: 'rgba(16, 185, 129, 0.08)',
          line: {{ width: 0 }}
        }});
      }}

      // Аннотация последней точки
      const annotations = [];
      if (y.length) {{
        const lastY = y[y.length - 1];
        const lastX = x[x.length - 1];
        annotations.push({{
          x: lastX,
          y: lastY,
          text: `<b>${{lastY.toFixed(0)}} PLN</b>`,
          showarrow: true,
          arrowhead: 2,
          arrowsize: 1,
          arrowwidth: 1.5,
          arrowcolor: '#4f46e5',
          ax: 0,
          ay: -32,
          font: {{ size: 12, color: '#4f46e5', family: 'Inter, sans-serif' }},
          bgcolor: 'rgba(255,255,255,0.85)',
          borderpad: 3,
          bordercolor: '#4f46e5',
          borderwidth: 1
        }});
      }}

      const layout = {{
        margin: {{ t: 24, r: 20, b: 75, l: 70 }},
        paper_bgcolor: 'rgba(0,0,0,0)',
        plot_bgcolor: 'rgba(0,0,0,0)',
        shapes: shapes,
        xaxis: {{
          title: {{ text: 'Время проверки (обновляется каждый час) · Даты поездки: ' + tripDatesLabel, standoff: 16, font: {{ size: 11, color: '#64748b' }} }},
          type: 'date',
          tickformat: '%d.%m',
          hoverformat: '%d.%m.%Y %H:%M',
          gridcolor: 'rgba(148,163,184,0.2)',
          tickangle: -20
        }},
        yaxis: {{
          title: {{ text: 'Цена (PLN) · 2 взрослых + 1 ребёнок', standoff: 10, font: {{ size: 11, color: '#64748b' }} }},
          gridcolor: 'rgba(148,163,184,0.2)',
          range: [yDataMin - yPad, yDataMax + yPad],
          fixedrange: false
        }},
        showlegend: true,
        legend: {{ orientation: 'h', x: 0, y: -0.22, font: {{ size: 11, color: '#64748b' }} }},
        hovermode: 'closest',
        annotations
      }};
      Plotly.newPlot('chart', [mainTrace, medianTrace], layout, {{ responsive: true, displayModeBar: true }});
    </script>
</body>
</html>"""


def generate_inline_charts_dashboard(data_file: str = 'data/travel_prices.csv', output_file: str = 'index.html', title: str = 'Travel Price Monitor • Расширенный дашборд', charts_subdir: str = 'hotel-charts', tz: str = 'Europe/Warsaw', alerts_file: str = None, all_airports_data_file: str = None, disappeared_after_runs: int = 2, display_price_ceiling: float = None, history_price_ceiling: float = None, write_legacy_hotel_html: bool = False, config_file: str = None):
    """Генерирует дашборд с встроенными графиками"""
    
    # Загружаем данные (текущий месяц + архивные месяцы для полной истории сезона)
    try:
        frames = []
        data_file_abs = os.path.abspath(data_file)
        data_dir = os.path.dirname(data_file_abs)
        base_name = os.path.splitext(os.path.basename(data_file_abs))[0]  # e.g. 'travel_prices'
        archive_dir = os.path.join(data_dir, 'archive')
        # Читаем архивные файлы прошлых месяцев (отсортированы хронологически)
        if os.path.isdir(archive_dir):
            archive_files = sorted(
                f for f in os.listdir(archive_dir)
                if f.startswith(f"{base_name}_") and f.endswith('.csv')
            )
            for af in archive_files:
                try:
                    frames.append(pd.read_csv(os.path.join(archive_dir, af), quoting=csv.QUOTE_ALL, on_bad_lines='skip'))
                except Exception as ae:
                    print(f"⚠️ Ошибка чтения архива {af}: {ae}")
        # Текущий месяц
        frames.append(pd.read_csv(data_file, quoting=csv.QUOTE_ALL, on_bad_lines='skip'))
        df = pd.concat(frames, ignore_index=True, sort=False) if len(frames) > 1 else frames[0]
        if archive_files if os.path.isdir(archive_dir) else []:
            print(f"📦 Загружено архивных файлов: {len(archive_files)}, итого строк: {len(df)}")
        # Нормализуем время: аккуратно обрабатываем смешанные строки (с/без таймзоны)
        raw = df['scraped_at'].astype(str)
        mask_tz = raw.str.contains(r"Z$|[+-]\d{2}:\d{2}$", regex=True)
        tz_series = pd.to_datetime(raw.where(mask_tz), errors='coerce', utc=True)
        tz_series = tz_series.dt.tz_convert(tz)
        naive_series = pd.to_datetime(raw.where(~mask_tz), errors='coerce')
        try:
            naive_series = naive_series.dt.tz_localize(tz)
        except Exception:
            # Если часть уже осознанно tz-aware/NaT — оставим как есть
            pass
        df['scraped_at_local'] = tz_series.combine_first(naive_series)
        # Убираем строки с некорректной датой
        df = df.dropna(subset=['scraped_at_local'])
        # Используем локализованное время без дополнительных сдвигов
        df['scraped_at_display'] = df['scraped_at_local']
        for col in ('ta_rating', 'ta_review_count', 'ta_source', 'duration_bucket'):
            if col not in df.columns:
                df[col] = ''
        print(f"✅ Загружено {len(df)} записей")
    except Exception as e:
        print(f"❌ Ошибка загрузки данных: {e}")
        return

    # Загружаем данные альтернативных аэропортов для сравнения, если переданы или обнаружены
    df_all_airports = None
    alt_file_to_load = None
    if all_airports_data_file and os.path.exists(all_airports_data_file):
        alt_file_to_load = all_airports_data_file
    else:
        auto_alt_csv = os.path.join(data_dir, "travel_prices_any_airports.csv")
        if os.path.exists(auto_alt_csv):
            alt_file_to_load = auto_alt_csv

    if alt_file_to_load:
        try:
            df_all_airports = pd.read_csv(alt_file_to_load, quoting=csv.QUOTE_ALL, on_bad_lines='skip')
            print(f"✈️ Загружено {len(df_all_airports)} записей альтернативных аэропортов из {alt_file_to_load}")
        except Exception as e:
            print(f"⚠️ Ошибка чтения файла альтернативных аэропортов {alt_file_to_load}: {e}")
            df_all_airports = None

    filter_config = load_filter_config(data_file, config_file)
    group_cols, trip_buckets, use_trip_buckets = _prepare_trip_duration_columns(df, filter_config or {})
    df_all_durations = df.copy()
    default_trip_duration_bucket = ''
    if use_trip_buckets:
        bucket_ids = [str(b['id']) for b in trip_buckets]
        default_trip_duration_bucket = str(
            (filter_config or {}).get('default_trip_duration_bucket') or bucket_ids[-1]
        ).strip()
        if default_trip_duration_bucket not in bucket_ids:
            default_trip_duration_bucket = bucket_ids[-1]
        df = df[df['duration_bucket'].astype(str) == default_trip_duration_bucket].copy()
        group_cols = ['hotel_name']
        default_label = next(
            (b['label'] for b in trip_buckets if str(b['id']) == default_trip_duration_bucket),
            default_trip_duration_bucket,
        )
        print(f"📏 Корзины длительности: {', '.join(b['label'] for b in trip_buckets)}")
        print(f"📏 Стартовый фильтр (как отдельная страница): {default_label}")
    
    ceiling_val = _parse_price_ceiling(display_price_ceiling)
    history_val = _resolve_history_ceiling(ceiling_val, history_price_ceiling)
    df_canonical = collapse_canonical_per_run(df, ceiling_val, group_cols=group_cols)
    df_history = collapse_canonical_per_run(df, history_val, group_cols=group_cols)
    df_full = collapse_canonical_per_run(df, None, group_cols=group_cols)
    if ceiling_val is not None:
        print(f"📊 Показ ≤{ceiling_val:.0f} PLN (таблица, алерты): {len(df_canonical)} записей")
    if history_val is not None:
        print(f"📈 История ≤{history_val:.0f} PLN (графики, выпавшие): {len(df_history)} записей")
    elif len(df_history) > len(df_canonical):
        print(f"📈 Расширенная история: {len(df_history)} записей")
    if len(df_full) > len(df_history):
        print(f"📉 Полная история (без потолка): {len(df_full)} записей")

    try:
        offers_count_timeline = build_daily_offers_count_timeline(
            df,
            ceiling_val=ceiling_val,
            group_cols=group_cols,
            tz=tz,
            pick='last',
        )
        offers_count_dates = offers_count_timeline['dates']
        offers_count_values = offers_count_timeline['counts']
        offers_count_meta = offers_count_timeline['meta']
        if offers_count_dates:
            print(
                f"📈 Предложений по дням: {len(offers_count_dates)} точек "
                f"(последний ран дня, последнее: {offers_count_values[-1]})"
            )
    except Exception as e:
        print(f"⚠️ Не удалось посчитать динамику количества предложений: {e}")
        offers_count_dates = []
        offers_count_values = []
        offers_count_meta = []

    # Модель данных:
    # • df_canonical — ≤ display (10k): таблица, карточки, алерты.
    # • df_history — ≤ history (20k): графики, выпавшие, контекст «было дороже».
    # • df_full — вся история: типичная средняя (Δ к средней, Deal Score).

    # Анализ «когда покупать»: статистика снижения цен по часу/дню недели/части месяца/месяцу
    try:
        timing_analysis = analyze_purchase_timing(df, tz=tz)
    except Exception as e:
        print(f"⚠️ Не удалось посчитать timing-аналитику: {e}")
        timing_analysis = {"available": False, "status": "error", "recommendation": "", "dimensions": {}}
    timing_json = json.dumps(timing_analysis, ensure_ascii=False, default=str)

    # Вычисляем статистику (заполним после сборки таблицы — см. history_* ниже)
    total_offers = 0
    unique_hotels = 0
    avg_price = 0.0
    history_min_price = 0.0
    history_max_price = 0.0
    current_table_hotels = 0

    # Функция для генерации hover-данных с использованием встроенных возможностей Plotly
    def generate_hover_data(detailed_data):
        """Генерирует данные для hover с детальной информацией о ране"""
        hover_data = {
            'title': f"📊 ТОП-10 ({detailed_data['run_time']})",
            'avg_price': detailed_data.get('avg_price', 0),
            'avg_change': None,
            'price_changes': [],
            'new_hotels': [],
            'removed_hotels': [],
            'no_changes': False
        }
        
        # Изменение средней цены
        if detailed_data.get('avg_price_change', 0) != 0:
            change = detailed_data['avg_price_change']
            change_percent = detailed_data.get('avg_price_change_percent', 0)
            arrow = "↗️" if change > 0 else "↘️"
            sign = "+" if change > 0 else ""
            
            hover_data['avg_change'] = {
                'arrow': arrow,
                'change': change,
                'change_percent': change_percent,
                'sign': sign
            }
        
        # Изменения цен отелей
        if detailed_data.get('price_changes') and len(detailed_data['price_changes']) > 0:
            for change in detailed_data['price_changes']:
                arrow = "↗️" if change['change'] > 0 else "↘️"
                sign = "+" if change['change'] > 0 else ""
                
                hover_data['price_changes'].append({
                    'name': change['name'],
                    'old_price': change['old_price'],
                    'new_price': change['new_price'],
                    'change': change['change'],
                    'change_percent': change['change_percent'],
                    'arrow': arrow,
                    'sign': sign
                })
        
        # Новые отели в ТОП-10
        if detailed_data.get('new_hotels') and len(detailed_data['new_hotels']) > 0:
            for hotel in detailed_data['new_hotels']:
                hover_data['new_hotels'].append({
                    'name': hotel['name'],
                    'price': hotel['price'],
                    'position': hotel['position']
                })
        
        # Отели, покинувшие ТОП-10
        if detailed_data.get('removed_hotels') and len(detailed_data['removed_hotels']) > 0:
            for hotel in detailed_data['removed_hotels']:
                hover_data['removed_hotels'].append({
                    'name': hotel['name'],
                    'price': hotel['price'],
                    'position': hotel['position']
                })
        
        # Если нет изменений
        if (not detailed_data.get('price_changes') or len(detailed_data['price_changes']) == 0) and \
           (not detailed_data.get('new_hotels') or len(detailed_data['new_hotels']) == 0) and \
           (not detailed_data.get('removed_hotels') or len(detailed_data['removed_hotels']) == 0) and \
           detailed_data.get('avg_price_change', 0) == 0:
            hover_data['no_changes'] = True
        
        return hover_data

    def extract_airport_from_url(url):
        """Извлекает аэропорт вылета из URL"""
        try:
            if pd.isna(url) or not url:
                return None
            
            # Парсим URL и извлекаем параметры
            parsed_url = urlparse(url)
            query_params = parse_qs(parsed_url.query)
            
            # Ищем параметр filter[from]
            filter_from = query_params.get('filter[from]', [None])[0]
            if filter_from:
                # Разделяем по запятой и берем первый аэропорт
                airports = [airport.strip() for airport in filter_from.split(',')]
                return airports[0] if airports else None
            
            return None
        except Exception as e:
            print(f"Ошибка при извлечении аэропорта из URL: {e}")
            return None

    def normalize_text(value: str) -> str:
        try:
            return ' '.join(str(value).strip().lower().split())
        except Exception:
            return str(value)

    def normalize_dates(value: str) -> str:
        """Нормализует строку дат к формату YYYY-MM-DD|YYYY-MM-DD для устойчивого сравнения."""
        try:
            import re
            s = str(value)
            # Ищем две даты вида dd.mm.yyyy или dd-mm-yyyy
            m = re.findall(r"(\d{1,2})[\.-](\d{1,2})[\.-](\d{4})", s)
            if len(m) >= 2:
                def to_iso(t):
                    d, mth, y = t
                    return f"{int(y):04d}-{int(mth):02d}-{int(d):02d}"
                return f"{to_iso(m[0])}|{to_iso(m[1])}"
        except Exception:
            pass
        return str(value).strip()

    def find_cheaper_airport_alternatives(df_source, hotel_name, dates, current_price, current_airport):
        """Находит более дешевые предложения того же отеля на те же даты из других аэропортов"""
        try:
            # Нормализация
            hotel_name_norm = normalize_text(hotel_name)
            dates_norm = normalize_dates(dates)
            current_airport_norm = (str(current_airport).strip() if current_airport else '') or 'Warszawa'

            # Фильтруем данные по отелю и датам
            df_src = df_source.copy()
            df_src['__hotel_norm'] = df_src['hotel_name'].astype(str).map(normalize_text)
            df_src['__dates_norm'] = df_src['dates'].astype(str).map(normalize_dates)
            same_hotel_dates = df_src[(df_src['__hotel_norm'] == hotel_name_norm) & (df_src['__dates_norm'] == dates_norm)].copy()
            
            if len(same_hotel_dates) == 0:
                return []
            
            # Извлекаем аэропорт: сначала 'departure_airport', затем 'from_airport', затем из URL
            same_hotel_dates['airport'] = None
            if 'departure_airport' in same_hotel_dates.columns:
                same_hotel_dates['airport'] = same_hotel_dates['departure_airport']
            if 'from_airport' in same_hotel_dates.columns:
                same_hotel_dates['airport'] = same_hotel_dates['airport'].fillna(same_hotel_dates['from_airport'])
            
            same_hotel_dates['airport'] = same_hotel_dates['airport'].where(
                same_hotel_dates['airport'].astype(str).str.strip().ne('') &
                same_hotel_dates['airport'].astype(str).str.strip().ne('Все аэропорты') &
                same_hotel_dates['airport'].astype(str).str.strip().ne('None'),
                None
            )
            same_hotel_dates['airport'] = same_hotel_dates['airport'].fillna(
                same_hotel_dates['url'].apply(extract_airport_from_url)
            )
            same_hotel_dates['airport'] = same_hotel_dates['airport'].fillna('Другой город')
            same_hotel_dates.loc[same_hotel_dates['airport'].astype(str).str.strip()=='', 'airport'] = 'Другой город'
            
            # Для каждого аэропорта выбираем запись с минимальной ценой и её offer_url (если есть)
            idx_min_by_airport = same_hotel_dates.groupby('airport')['price'].idxmin()
            airport_prices = same_hotel_dates.loc[
                idx_min_by_airport, ['airport', 'price', 'offer_url', 'url']
            ].reset_index(drop=True)
            
            # Нормализация текущего аэропорта для фильтрации совпадений (например, если текущий - Варшава)
            cur_ap_lower = current_airport_norm.lower()
            is_cur_warsaw = 'warszaw' in cur_ap_lower or 'waw' in cur_ap_lower or 'wmi' in cur_ap_lower or 'rdo' in cur_ap_lower or not cur_ap_lower
            
            def is_same_airport(alt_ap: str) -> bool:
                alt_lower = str(alt_ap).strip().lower()
                if is_cur_warsaw and ('warszaw' in alt_lower or 'waw' in alt_lower or 'wmi' in alt_lower or 'rdo' in alt_lower):
                    return True
                return alt_lower == cur_ap_lower

            # Фильтруем аэропорты с ценами дешевле текущей и не из того же города вылета
            mask_cheaper = (airport_prices['price'] < current_price) & (~airport_prices['airport'].apply(is_same_airport))
            cheaper_alternatives = airport_prices[mask_cheaper].sort_values('price')
            
            alternatives = []
            for _, row in cheaper_alternatives.iterrows():
                savings = current_price - row['price']
                savings_percent = (savings / current_price) * 100
                
                # Предпочитаем ссылку на конкретное предложение, иначе fallback на URL поиска
                alt_url = None
                try:
                    alt_url = (row.get('offer_url') or '').strip()
                except Exception:
                    alt_url = ''
                if not alt_url:
                    alt_url = (row.get('url') or '').strip()

                alternatives.append({
                    'airport': str(row['airport']).strip(),
                    'price': float(row['price']),
                    'savings': float(savings),
                    'savings_percent': float(savings_percent),
                    'url': alt_url
                })
            
            return alternatives
            
        except Exception as e:
            print(f"Ошибка при поиске альтернативных аэропортов: {e}")
            return []

    # Средняя цена ТОП-10 дешёвых предложений по ранам с детальной информацией
    run_slices = list(iter_scrape_runs(df_canonical))
    try:
        run_data = []
        top10_detailed_data = []  # Детальная информация для hover
        
        print(f"🔍 Найдено {len(run_slices)} ранов")
        
        # Обрабатываем каждый ран
        for i, (_, _, run_data_slice) in enumerate(run_slices):
            if len(run_data_slice) == 0:
                continue
                
            run_time = run_data_slice['scraped_at_display'].iloc[0]  # Время начала рана
            
            # Для каждого рана берем последние данные по каждому отелю в этом ране
            latest_prices = []
            hotel_prices = {}  # Словарь отель -> цена для этого рана
            
            # Берем последние данные по каждому отелю в этом ране
            for hotel_name, hotel_grp in run_data_slice.groupby('hotel_name'):
                if not hotel_grp.empty:
                    latest_price = float(hotel_grp['price'].astype(float).min())
                    latest_prices.append(latest_price)
                    hotel_prices[hotel_name] = latest_price
            
            if len(latest_prices) >= 10:
                # Берем ТОП-10 дешевых из всех отелей на этот ран
                sorted_prices = sorted(latest_prices)
                top10_prices = sorted_prices[:10]
                avg_price = sum(top10_prices) / len(top10_prices)
                min_price = top10_prices[0]
                max_price = top10_prices[-1]
                
                # Находим отели, которые попали в ТОП-10
                top10_hotels = []
                for hotel_name, price in hotel_prices.items():
                    if price in top10_prices:
                        top10_hotels.append({
                            'name': hotel_name,
                            'price': price,
                            'position': sorted_prices.index(price) + 1
                        })
                
                # Сортируем по позиции в ТОП-10
                top10_hotels.sort(key=lambda x: x['position'])
                
                # Добавляем точку для каждого рана (убираем фильтрацию по одинаковым ценам)
                run_data.append((run_time, avg_price))
                top10_detailed_data.append({
                    'run_time': run_time,
                    'avg_price': avg_price,
                    'min_price': min_price,
                    'max_price': max_price,
                    'top10_hotels': top10_hotels
                })
            elif len(latest_prices) > 0:
                # Если отелей меньше 10, берем все
                sorted_prices = sorted(latest_prices)
                avg_price = sum(sorted_prices) / len(sorted_prices)
                min_price = sorted_prices[0]
                max_price = sorted_prices[-1]
                
                # Все отели попадают в "ТОП"
                top_hotels = []
                for hotel_name, price in hotel_prices.items():
                    top_hotels.append({
                        'name': hotel_name,
                        'price': price,
                        'position': sorted_prices.index(price) + 1
                    })
                
                # Добавляем точку для каждого рана (убираем фильтрацию по одинаковым ценам)
                run_data.append((run_time, avg_price))
                top10_detailed_data.append({
                    'run_time': run_time,
                    'avg_price': avg_price,
                    'min_price': min_price,
                    'max_price': max_price,
                    'top10_hotels': top_hotels
                })
        
        if run_data:
            top10_x_values = [pd.Timestamp(ts).isoformat() for ts, _ in run_data]
            top10_y_values = [float(price) for _, price in run_data]
            top10_min_values = [float(d.get('min_price', y)) for d, y in zip(top10_detailed_data, top10_y_values)]
            top10_max_values = [float(d.get('max_price', y)) for d, y in zip(top10_detailed_data, top10_y_values)]
            
            # Добавляем информацию об изменениях цен для каждого рана
            for i, detailed in enumerate(top10_detailed_data):
                if i == 0:
                    # Первый ран - нет изменений
                    detailed['price_changes'] = []
                    detailed['new_hotels'] = []
                    detailed['removed_hotels'] = []
                else:
                    # Сравниваем с предыдущим раном
                    prev_detailed = top10_detailed_data[i-1]
                    current_hotels = {h['name']: h for h in detailed['top10_hotels']}
                    prev_hotels = {h['name']: h for h in prev_detailed['top10_hotels']}
                    
                    # Находим изменения цен
                    price_changes = []
                    for hotel_name, current_hotel in current_hotels.items():
                        if hotel_name in prev_hotels:
                            prev_price = prev_hotels[hotel_name]['price']
                            current_price = current_hotel['price']
                            if prev_price != current_price:
                                price_changes.append({
                                    'name': hotel_name,
                                    'old_price': prev_price,
                                    'new_price': current_price,
                                    'change': current_price - prev_price,
                                    'change_percent': ((current_price - prev_price) / prev_price) * 100,
                                    'position': current_hotel['position']
                                })
                    
                    # Находим новые и удаленные отели
                    new_hotels = []
                    removed_hotels = []
                    
                    for hotel_name in current_hotels:
                        if hotel_name not in prev_hotels:
                            new_hotels.append({
                                'name': hotel_name,
                                'price': current_hotels[hotel_name]['price'],
                                'position': current_hotels[hotel_name]['position']
                            })
                    
                    for hotel_name in prev_hotels:
                        if hotel_name not in current_hotels:
                            removed_hotels.append({
                                'name': hotel_name,
                                'price': prev_hotels[hotel_name]['price'],
                                'position': prev_hotels[hotel_name]['position']
                            })
                    
                    detailed['price_changes'] = price_changes
                    detailed['new_hotels'] = new_hotels
                    detailed['removed_hotels'] = removed_hotels
                
                # Добавляем информацию об изменении средней цены
                if i > 0:
                    prev_avg = top10_detailed_data[i-1]['avg_price']
                    current_avg = detailed['avg_price']
                    detailed['avg_price_change'] = current_avg - prev_avg
                    detailed['avg_price_change_percent'] = ((current_avg - prev_avg) / prev_avg) * 100
                else:
                    detailed['avg_price_change'] = 0
                    detailed['avg_price_change_percent'] = 0
                
                # Создаем данные для hover с использованием встроенных возможностей Plotly
                detailed['hover_data'] = generate_hover_data(detailed)
            
            print(f"🔍 Отладка ТОП-10: {len(run_data)} точек данных")
            if run_data:
                print(f"   Последняя точка: {run_data[-1][1]:.2f} PLN")
        else:
            top10_x_values, top10_y_values = [], []
            top10_min_values, top10_max_values = [], []
            top10_detailed_data = []
            print("❌ Нет данных для ТОП-10 графика")
            
    except Exception as e:
        print(f"Ошибка расчета ТОП-10: {e}")
        top10_x_values, top10_y_values = [], []
        top10_min_values, top10_max_values = [], []
        top10_detailed_data = []
        run_slices = []
    
    # Индекс ценовой динамики (Price Trend Index)
    try:
        print("📊 Расчет индекса ценовой динамики...")
        trend_index_x_values, trend_index_y_values = [], []
        trend_index_detailed_data = []
        
        # Словарь для хранения предыдущих цен каждого отеля
        prev_hotel_prices = {}
        
        # Обрабатываем каждый ран
        for i, (_, _, run_data_slice) in enumerate(run_slices):
            if len(run_data_slice) == 0:
                continue
                
            run_time = run_data_slice['scraped_at_display'].iloc[0]  # Время начала рана
            
            # Собираем текущие цены отелей в этом ране
            current_hotel_prices = {}
            for hotel_name, hotel_grp in run_data_slice.groupby('hotel_name'):
                if not hotel_grp.empty:
                    latest_price = float(hotel_grp['price'].astype(float).min())
                    current_hotel_prices[hotel_name] = latest_price
            
            # Рассчитываем индекс ценовой динамики
            total_price_change = 0
            hotels_with_changes = 0
            price_changes = []
            
            for hotel_name, current_price in current_hotel_prices.items():
                if hotel_name in prev_hotel_prices:
                    prev_price = prev_hotel_prices[hotel_name]
                    if prev_price > 0:  # Избегаем деления на ноль
                        price_change_pct = ((current_price - prev_price) / prev_price) * 100
                        total_price_change += price_change_pct
                        hotels_with_changes += 1
                        price_changes.append({
                            'hotel': hotel_name,
                            'prev_price': prev_price,
                            'current_price': current_price,
                            'change_pct': price_change_pct
                        })
            
            # Рассчитываем средний индекс (если есть изменения)
            if hotels_with_changes > 0:
                avg_price_change = total_price_change / hotels_with_changes
                
                # Добавляем точку для каждого рана
                trend_index_x_values.append(pd.Timestamp(run_time).isoformat())
                trend_index_y_values.append(avg_price_change)
                trend_index_detailed_data.append({
                    'run_time': run_time.strftime('%Y-%m-%d %H:%M'),
                    'avg_change_pct': avg_price_change,
                    'hotels_with_changes': hotels_with_changes,
                    'total_hotels': len(current_hotel_prices),
                    'price_changes': price_changes
                })
            
            # Обновляем предыдущие цены для следующего рана
            prev_hotel_prices = current_hotel_prices.copy()
        
        print(f"🔍 Отладка индекса тренда: {len(trend_index_x_values)} точек данных")
        if trend_index_x_values:
            print(f"   Последняя точка: {trend_index_y_values[-1]:.2f}%")
    except Exception as e:
        print(f"Ошибка расчета индекса тренда: {e}")
        trend_index_x_values, trend_index_y_values = [], []
        trend_index_detailed_data = []
    
    
    # --- Таблица: только последний ран (актуальные цены «сейчас») ---
    # Канонизация: 1 строка / отель / ран = min цена (≤ display_price_ceiling).
    latest_run_slice = _last_run_slice(df_canonical)
    full_latest_run_slice = _last_run_slice(df)

    df_sorted_all = latest_run_slice.sort_values(group_cols + ['scraped_at_display'])
    latest_rows = []
    skipped_above_ceiling = 0
    if ceiling_val is not None and not full_latest_run_slice.empty:
        for group_key, grp in full_latest_run_slice.groupby(group_cols):
            if grp[grp['price'].astype(float) <= ceiling_val].empty:
                skipped_above_ceiling += 1
    for group_key, grp in df_sorted_all.groupby(group_cols):
        hotel_name, bucket, row_id = _unpack_table_group_key(group_key, use_trip_buckets)
        # В каноническом ране — одна строка на отель; берём актуальную, не мин. по всей истории.
        last = grp.sort_values('scraped_at_display').iloc[-1]
        latest_rows.append({
            'hotel_name': hotel_name,
            'duration_bucket': bucket,
            'row_id': row_id,
            'price': float(last['price']),
            'dates': last.get('dates', None),
            'duration': last.get('duration', None),
            'scraped_at_local': last['scraped_at_local'],
            'url': last.get('url', None),
            'from_airport': last.get('from_airport', None),
            'offer_url': last.get('offer_url', None),
            'image_url': last.get('image_url', None),
            'ta_rating': last.get('ta_rating', ''),
            'ta_review_count': last.get('ta_review_count', ''),
            'ta_source': last.get('ta_source', ''),
            'departure_date': last.get('departure_date', None),
            'departure_key': last.get('departure_key', None),
        })
    _backfill_ta_for_latest_rows(latest_rows, df, config_file, data_file)
    if latest_rows:
        all_hotels = pd.DataFrame(latest_rows).sort_values('price').reset_index(drop=True)
    else:
        all_hotels = pd.DataFrame(columns=[
            'hotel_name', 'duration_bucket', 'row_id', 'price', 'dates', 'duration',
            'scraped_at_local', 'url', 'from_airport', 'offer_url', 'image_url',
            'ta_rating', 'ta_review_count', 'ta_source', 'departure_date', 'departure_key',
        ])
    # Актуальная цена для таблицы — только последний ран; дельты — vs вся df_canonical.
    table_prices = {row['row_id']: float(row['price']) for row in latest_rows}

    total_offers = len(df_canonical)
    unique_hotels = int(df_canonical['hotel_name'].nunique()) if not df_canonical.empty else 0
    if not df_canonical.empty:
        avg_price = float(df_canonical['price'].mean())
        history_min_price = float(df_canonical['price'].min())
        history_max_price = float(df_canonical['price'].max())
    current_table_hotels = len(all_hotels)
    if ceiling_val is not None and skipped_above_ceiling:
        print(f"💎 Дороже потолка показа (>{ceiling_val:.0f} PLN, только в истории): {skipped_above_ceiling}")
    print(f"🧹 Таблица отфильтрована по последнему рану: {len(all_hotels)} актуальных отелей")

    #
    # Откат: отключаем блок "до 8000 из любого вылета, отсутствующие из Варшавы"
    missing_hotels_under_8000 = []
    if False:
        try:
            warsaw_hotel_names = set(df['hotel_name'].dropna().unique())
            # Определяем slug направления (например, 'egipt') на основе URL текущего набора
            import re
            def dest_slug_from_url(u: str):
                try:
                    s = str(u or '')
                    m = re.search(r"/kierunek/([^/?#]+)/?", s)
                    if m:
                        return m.group(1).lower()
                    m = re.search(r"/wycieczka/([^,/?#]+),", s)
                    if m:
                        return m.group(1).lower()
                except Exception:
                    pass
                return ''

            current_dest_slug = ''
            for candidate in (df.get('offer_url'), df.get('url')):
                if candidate is not None:
                    for v in candidate.dropna().astype(str).values.tolist():
                        current_dest_slug = dest_slug_from_url(v)
                        if current_dest_slug:
                            break
                if current_dest_slug:
                    break

            # Набор дат (нормализованных) из варшавского датасета для согласованности выборки
            warsaw_dates_norm = set(df['dates'].astype(str).map(normalize_dates).dropna().unique().tolist())

            df_gen = df_all_airports.dropna(subset=['hotel_name', 'price']).copy()
            # Фильтруем только по текущему направлению (например, только Egipt)
            def row_dest_slug(row):
                u1 = row.get('offer_url', '')
                u2 = row.get('url', '')
                return dest_slug_from_url(u1) or dest_slug_from_url(u2)
            df_gen['__dest'] = df_gen.apply(row_dest_slug, axis=1)
            if current_dest_slug:
                df_gen = df_gen[df_gen['__dest'] == current_dest_slug]

            # Оставляем строки только с подходящими датами
            if len(warsaw_dates_norm) > 0:
                df_gen['__dates_norm'] = df_gen['dates'].astype(str).map(normalize_dates)
                df_gen = df_gen[df_gen['__dates_norm'].isin(warsaw_dates_norm)]

            # Ищем минимальную цену по каждому отелю и берем соответствующую строку
            idx_min = df_gen.groupby('hotel_name')['price'].idxmin()
            gen_best = df_gen.loc[idx_min].copy()
            gen_best = gen_best[gen_best['price'] <= 8000]
            # Отбрасываем те, что уже есть в варшавском датасете
            gen_best = gen_best[~gen_best['hotel_name'].isin(warsaw_hotel_names)]
            # Аэропорт: сначала берем from_airport, потом fallback к извлечению из URL
            if 'from_airport' in gen_best.columns:
                gen_best['airport'] = gen_best['from_airport']
                gen_best['airport'] = gen_best['airport'].where(
                    gen_best['airport'].astype(str).str.strip().ne(''), None
                )
                gen_best['airport'] = gen_best['airport'].fillna(gen_best['url'].apply(extract_airport_from_url))
            else:
                gen_best['airport'] = gen_best['url'].apply(extract_airport_from_url)
            # Собираем элементы для вывода (ограничим до 20 для компактности)
            gen_best = gen_best.sort_values('price').head(20)
            for _, row in gen_best.iterrows():
                missing_hotels_under_8000.append({
                    'hotel_name': row['hotel_name'],
                    'price': float(row['price']),
                    'dates': row.get('dates', None),
                    'airport': row.get('airport', None),
                    'offer_url': row.get('offer_url', None)
                })
            print(f"🛫 Отели до 8000 (любой вылет), отсутствующие из Варшавы: {len(missing_hotels_under_8000)}")
        except Exception as e:
            print(f"Ошибка вычисления блока 'до 8000 из любого вылета, нет из Варшавы': {e}")
    
    # Дельты: «текущая» цена = таблица (последний ран), база = каноническая история (все раны).
    df_sorted = df_canonical.sort_values(group_cols + ['scraped_at_display'])
    df_sorted_full = df_full.sort_values(group_cols + ['scraped_at_display'])
    ref_time_series = df_canonical['scraped_at_display'] if not df_canonical.empty else df['scraped_at_display']

    def compute_changes(window_hours: int):
        cutoff = (ref_time_series.max() or datetime.now()) - timedelta(hours=window_hours)
        changes = []
        deltas_map = {}
        for group_key, grp in df_sorted.groupby(group_cols):
            hotel_name, _, row_id = _unpack_table_group_key(group_key, use_trip_buckets)
            if row_id not in table_prices:
                continue
            grp = grp.sort_values('scraped_at_display')
            latest_price = table_prices[row_id]
            latest_time = grp.iloc[-1]['scraped_at_display']
            win = grp[grp['scraped_at_display'] >= cutoff]
            if len(win) >= 2:
                baseline_row = win.iloc[0]
            elif len(grp) >= 2:
                baseline_row = grp.iloc[-2]
            else:
                deltas_map[row_id] = None
                continue
            baseline_price = float(baseline_row['price'])
            if baseline_price == 0:
                deltas_map[row_id] = None
                continue
            change = latest_price - baseline_price
            if change == 0:
                deltas_map[row_id] = None
                continue
            change_percent = (change / baseline_price) * 100.0
            changes.append({
                'hotel_name': hotel_name,
                'old_price': baseline_price,
                'new_price': latest_price,
                'change': change,
                'change_percent': change_percent,
                'timestamp': str(latest_time)
            })
            deltas_map[row_id] = (change, change_percent)
        decreases = sorted([h for h in changes if h['change'] < 0], key=lambda x: x['change'])[:5]
        increases = sorted([h for h in changes if h['change'] > 0], key=lambda x: x['change'], reverse=True)[:5]
        return decreases, increases, deltas_map

    # Для таблицы оставляем 48ч, для блоков добавим 24ч и 7д
    decreases_48h, increases_48h, deltas_by_hotel = compute_changes(48)
    decreases_24h, increases_24h, _ = compute_changes(24)
    decreases_7d, increases_7d, _ = compute_changes(24 * 7)

    # Метки нового минимума/максимума за 7д и 30д
    ref_time = ref_time_series.max() or datetime.now()
    minmax_labels_by_hotel = {}
    for group_key, grp in df_sorted.groupby(group_cols):
        _, _, row_id = _unpack_table_group_key(group_key, use_trip_buckets)
        if row_id not in table_prices:
            continue
        grp = grp.sort_values('scraped_at_display')
        latest_price = table_prices[row_id]
        labels = []
        for days in (7, 30):
            cutoff_d = ref_time - timedelta(days=days)
            window = grp[grp['scraped_at_local'] >= cutoff_d]
            if len(window) == 0:
                continue
            win_min = float(window['price'].min())
            win_max = float(window['price'].max())
            if latest_price <= win_min:
                labels.append(f"Новый минимум {days}д")
            if latest_price >= win_max:
                labels.append(f"Новый максимум {days}д")
        minmax_labels_by_hotel[row_id] = labels

    # Отклонение от "типичной" цены отеля:
    # baseline = time-weighted mean по всей истории (без потолка показа).
    avg_baseline_delta = {}
    for row_id, last_price in table_prices.items():
        if use_trip_buckets:
            hotel_name, bucket, _ = _unpack_table_group_key(
                tuple(row_id.split('|', 1)) if '|' in row_id else (row_id, ''),
                True,
            )
            mask = (df_sorted_full['hotel_name'] == hotel_name)
            if bucket:
                mask &= (df_sorted_full['duration_bucket'].astype(str) == bucket)
            grp = df_sorted_full[mask]
        else:
            grp = df_sorted_full[df_sorted_full['hotel_name'] == row_id]
        if grp.empty:
            avg_baseline_delta[row_id] = None
            continue
        grp = grp.sort_values('scraped_at_display')
        baseline = _time_weighted_price_baseline(grp)
        if baseline is None or baseline == 0:
            avg_baseline_delta[row_id] = None
            continue
        change_abs = float(last_price) - baseline
        change_pct = (change_abs / baseline) * 100.0
        avg_baseline_delta[row_id] = (change_abs, change_pct)

    # Deal Score: насколько предложение выгодно относительно своей исторической цены
    if use_trip_buckets:
        premium_history_by_hotel = _build_premium_history_index(df_history, ceiling_val, group_cols)
    else:
        premium_history_by_hotel = build_premium_history_by_hotel(
            df_history, ceiling_val, time_col='scraped_at_display', price_col='price'
        )

    def _clamp(v, lo, hi):
        return max(lo, min(hi, v))

    deal_score_by_hotel = {}
    entry_candidates = []
    ta_by_hotel = {
        str(row.get('row_id') or row['hotel_name']): {
            'ta_rating': row.get('ta_rating', ''),
            'ta_review_count': row.get('ta_review_count', ''),
        }
        for _, row in all_hotels.iterrows()
    }

    for group_key, grp in df_sorted.groupby(group_cols):
        hotel_name, bucket, row_id = _unpack_table_group_key(group_key, use_trip_buckets)
        grp = grp.sort_values('scraped_at_display')
        if use_trip_buckets:
            mask = (df_sorted_full['hotel_name'] == hotel_name)
            if bucket:
                mask &= (df_sorted_full['duration_bucket'].astype(str) == bucket)
            grp_full = df_sorted_full[mask].sort_values('scraped_at_display')
        else:
            grp_full = df_sorted_full[df_sorted_full['hotel_name'] == hotel_name].sort_values('scraped_at_display')
        hist_grp = grp_full if not grp_full.empty else grp
        prices = hist_grp['price'].astype(float).tolist()
        if not prices:
            continue

        latest = float(table_prices.get(row_id, prices[-1]))
        series = pd.Series(prices, dtype='float64')
        samples = len(series)
        typical = _time_weighted_price_baseline(hist_grp) or latest
        median = float(typical)
        p25 = _time_weighted_price_quantile(hist_grp, 0.25) or latest
        p10 = _time_weighted_price_quantile(hist_grp, 0.10) or latest
        plateau_segments = _price_plateau_segments(hist_grp)
        min_p = float(min(p for p, _ in plateau_segments)) if plateau_segments else latest

        # Насколько цена ниже своей типичной (взвешенной по времени) истории
        rel_discount = 0.0
        if median > 0:
            rel_discount = (median - latest) / median
        score_discount = _clamp(50 + rel_discount * 200, 0, 100)

        # Редкость: чем ближе к историческим минимумам, тем выше балл
        if latest <= p10:
            score_rarity = 100
        elif latest <= p25:
            score_rarity = 80
        elif latest <= median:
            score_rarity = 50
        else:
            score_rarity = 35

        # Моментум: короткий нисходящий тренд добавляет баллы
        recent = prices[-3:] if len(prices) >= 3 else prices
        score_momentum = 50
        if len(recent) >= 3 and (recent[-1] <= recent[-2] <= recent[-3]):
            score_momentum = 85
        elif len(recent) >= 2 and recent[-1] < recent[-2]:
            score_momentum = 70
        elif len(recent) >= 2 and recent[-1] > recent[-2]:
            score_momentum = 35

        # Стабильность: низкий шум без тренда = нейтральный балл (CV по времени, не по точкам)
        score_stability = 50
        cv = _time_weighted_price_volatility(hist_grp)
        if cv is not None:
            if cv < 0.01:
                score_stability = 50
            else:
                score_stability = _clamp(70 - cv * 120, 20, 85)

        raw_deal_score = (
            score_discount * 0.40 +
            score_rarity * 0.30 +
            score_momentum * 0.20 +
            score_stability * 0.10
        )
        raw_deal_score = float(_clamp(raw_deal_score, 0, 100))

        # Confidence-aware shrinkage: на короткой истории тянем скор к нейтральному 50
        confidence_weight = _clamp(samples / 20.0, 0.15, 1.0)
        adj_deal_score = 50.0 + (raw_deal_score - 50.0) * confidence_weight
        deal_score = int(round(_clamp(adj_deal_score, 0, 100)))

        if samples < 8:
            confidence_level = "Low"
        elif samples < 20:
            confidence_level = "Medium"
        else:
            confidence_level = "High"

        delta48_info = deltas_by_hotel.get(row_id)
        avg_info = avg_baseline_delta.get(row_id)
        d48_pct = float(delta48_info[1]) if delta48_info is not None else None
        d_avg_pct = float(avg_info[1]) if avg_info is not None else None

        comeback = comeback_from_premium(
            latest, premium_history_by_hotel.get(row_id), ceiling_val
        )
        comeback_drop_pct = float(comeback['drop_from_peak_pct']) if comeback else None
        if comeback_drop_pct is not None:
            comeback_floor = _clamp(55 + comeback_drop_pct * 1.1, 55, 92)
            deal_score = int(max(deal_score, round(comeback_floor)))

        is_flat = (
            comeback_drop_pct is None
            and (d48_pct is None or abs(d48_pct) < 0.5)
            and (d_avg_pct is None or abs(d_avg_pct) < 0.5)
            and abs(rel_discount) < 0.02
        )
        if is_flat:
            deal_score = int(round(50 + (deal_score - 50) * 0.2))

        is_bad = (
            d48_pct is not None and d48_pct > 0
            and d_avg_pct is not None and d_avg_pct > 0
        )
        if is_bad and comeback_drop_pct is None:
            penalty = (d48_pct + d_avg_pct) / 2.0
            deal_score = int(_clamp(50 - penalty * 1.2, 5, 42))

        ta_info = ta_by_hotel.get(str(row_id), {})
        deal_score, ta_weight = blend_tripadvisor_into_deal_score(
            deal_score,
            ta_info.get('ta_rating'),
            ta_info.get('ta_review_count'),
        )

        deal_score_by_hotel[row_id] = {
            'score': deal_score,
            'raw_score': int(round(raw_deal_score)),
            'confidence': confidence_level,
            'samples': samples,
            'latest': latest,
            'median': median,
            'p25': p25,
            'min': min_p,
            'is_bad': is_bad,
            'comeback_drop_pct': comeback_drop_pct,
            'typical_price': median,
            'ta_rating': _parse_ta_rating_value(ta_info.get('ta_rating')),
            'ta_review_count': _parse_ta_review_count(ta_info.get('ta_review_count')),
            'ta_weight': ta_weight,
        }

        delta48 = delta48_info

        # Кандидаты для "раннего входа" (цена + TA при достаточных отзывах)
        ta_rating_val = _parse_ta_rating_value(ta_info.get('ta_rating'))
        ta_reviews_val = _parse_ta_review_count(ta_info.get('ta_review_count'))
        ta_ok = (
            ta_rating_val is None
            or ta_reviews_val < 15
            or ta_rating_val >= 3.8
        )
        if (
            delta48 is not None and delta48[1] <= -2.0 and latest <= p25
            and deal_score >= 72 and confidence_level != "Low" and ta_ok
        ):
            entry_candidates.append({
                'hotel_name': hotel_name,
                'deal_score': deal_score,
                'latest': latest,
                'discount_pct': ((median - latest) / median * 100.0) if median > 0 else 0.0,
                'delta48_pct': float(delta48[1]),
                'confidence': confidence_level,
            })

    entry_candidates = sorted(
        entry_candidates,
        key=lambda x: (x['deal_score'], x['discount_pct'], -x['latest']),
        reverse=True
    )
    entry_top = entry_candidates[:5]
    # Market breadth (48ч): доля отелей, которые подешевели среди отелей с валидной базовой точкой.
    # Считаем по актуальным отелям из последнего рана, чтобы метрика отражала "сейчас".
    current_hotels_for_breadth = (
        set(all_hotels['row_id'].astype(str).tolist()) if not all_hotels.empty else set()
    )
    breadth_total = 0
    breadth_down = 0
    for group_key, grp in df_sorted.groupby(group_cols):
        _, _, row_id = _unpack_table_group_key(group_key, use_trip_buckets)
        if current_hotels_for_breadth and row_id not in current_hotels_for_breadth:
            continue
        grp = grp.sort_values('scraped_at_display')
        if len(grp) < 2:
            continue
        latest_row = grp.iloc[-1]
        cutoff = (df['scraped_at_display'].max() or datetime.now()) - timedelta(hours=48)
        win = grp[grp['scraped_at_display'] >= cutoff]
        if len(win) >= 2:
            baseline_row = win.iloc[0]
        else:
            baseline_row = grp.iloc[-2]
        latest_price = float(latest_row['price'])
        baseline_price = float(baseline_row['price'])
        if baseline_price <= 0:
            continue
        breadth_total += 1
        if latest_price < baseline_price:
            breadth_down += 1
    market_breadth = (breadth_down / breadth_total) if breadth_total > 0 else 0.0

    # --- Журнал «выпавших»: когда-либо в зоне отслеживания, сейчас не в основной таблице ---
    def _dest_token_from_offer_url(url: str) -> str:
        # fly.pl: .../wycieczka/<страна>,<регион>,.../...  -> возвращаем токен страны
        try:
            s = str(url or '').lower()
            marker = '/wycieczka/'
            i = s.find(marker)
            if i < 0:
                return ''
            tail = s[i + len(marker):]
            seg = tail.split('/', 1)[0]
            return seg.split(',', 1)[0].strip()
        except Exception:
            return ''

    disappeared_events = []
    try:
        canonical_runs = list(iter_scrape_runs(df_canonical))
        full_runs = list(iter_scrape_runs(df_history))
        if canonical_runs and table_prices is not None:
            all_seen_hotels = set(df_canonical['hotel_name'].astype(str).tolist())
            current_hotel_names = set()
            for key in table_prices.keys():
                key_str = str(key)
                current_hotel_names.add(key_str.split('|', 1)[0] if '|' in key_str else key_str)
            gone_hotels = all_seen_hotels - current_hotel_names

            latest_full_hotels = set()
            if full_runs:
                _, _, latest_full_slice = full_runs[-1]
                latest_full_hotels = set(latest_full_slice['hotel_name'].astype(str).tolist())

            valid_dests = set()
            if 'offer_url' in df_canonical.columns:
                _, _, latest_canonical = canonical_runs[-1]
                for u in latest_canonical['offer_url'].astype(str).tolist():
                    tok = _dest_token_from_offer_url(u)
                    if tok:
                        valid_dests.add(tok)

            typical_prices = [
                float(v['median']) for v in deal_score_by_hotel.values()
                if v.get('median') is not None and float(v.get('median') or 0) > 0
            ]
            expensive_threshold = (
                float(pd.Series(typical_prices, dtype='float64').quantile(0.70))
                if len(typical_prices) >= 4 else (max(typical_prices) if typical_prices else 0.0)
            )

            for name in gone_hotels:
                hist = df_canonical[df_canonical['hotel_name'].astype(str) == name].dropna(subset=['scraped_at_display']).sort_values('scraped_at_display')
                if hist.empty:
                    continue
                full_hist = df_history[df_history['hotel_name'].astype(str) == name].dropna(subset=['scraped_at_display']).sort_values('scraped_at_display')
                prices = hist['price'].astype(float)
                if len(prices) == 0:
                    continue
                last_row = hist.iloc[-1]
                still_in_scrape = name in latest_full_hotels
                current_raw_price = None
                current_raw_dates = None
                current_raw_seen = None
                if not full_hist.empty:
                    raw_last = full_hist.iloc[-1]
                    current_raw_price = float(raw_last['price'])
                    current_raw_seen = raw_last['scraped_at_display']
                    current_raw_dates = raw_last.get('dates') if pd.notna(raw_last.get('dates')) else None
                if valid_dests:
                    hotel_dest = ''
                    for u in hist['offer_url'].astype(str).tolist():
                        hotel_dest = _dest_token_from_offer_url(u)
                        if hotel_dest:
                            break
                    if hotel_dest and hotel_dest not in valid_dests:
                        continue
                first_seen = hist['scraped_at_display'].iloc[0]
                last_seen = hist['scraped_at_display'].iloc[-1]
                try:
                    visible_hours = max(0.0, (last_seen - first_seen).total_seconds() / 3600.0)
                except Exception:
                    visible_hours = 0.0
                last_price = float(prices.iloc[-1])
                hotel_min_price = float(prices.min())
                hotel_max_price = float(prices.max())

                deal_info = deal_score_by_hotel.get(name, {})
                typical_price = float(
                    deal_info.get('typical_price')
                    or deal_info.get('median')
                    or 0.0
                )
                if typical_price <= 0 and not full_hist.empty:
                    typical_price = _time_weighted_price_baseline(full_hist) or 0.0
                if typical_price <= 0:
                    typical_price = float(prices.median())

                avg_info = avg_baseline_delta.get(name)
                baseline_pct = float(avg_info[1]) if avg_info else None
                if len(prices) < 3 and not full_hist.empty:
                    if current_raw_price and typical_price > 0:
                        baseline_pct = (current_raw_price - typical_price) / typical_price * 100.0
                elif baseline_pct is None and not full_hist.empty and typical_price > 0 and current_raw_price:
                    baseline_pct = (current_raw_price - typical_price) / typical_price * 100.0
                # Глубина падения к своей норме (положительное число = насколько ниже базы).
                drop_depth = max(0.0, -baseline_pct) if baseline_pct is not None else 0.0
                # Насколько дешевле своей нормы он опускался в минимуме.
                min_below_pct = ((hotel_min_price - typical_price) / typical_price * 100.0) if typical_price > 0 else 0.0

                # Последнее движение цены перед исчезновением.
                last_move_pct = 0.0
                if len(prices) >= 2:
                    prev_price = float(prices.iloc[-2])
                    if prev_price > 0:
                        last_move_pct = (last_price - prev_price) / prev_price * 100.0

                is_expensive = typical_price >= expensive_threshold and expensive_threshold > 0

                # Классификация причины исчезновения.
                if still_in_scrape and current_raw_price is not None:
                    delta_from_last = (
                        (current_raw_price - last_price) / last_price * 100.0
                        if last_price > 0 else 0.0
                    )
                    if ceiling_val is not None and current_raw_price > ceiling_val:
                        reason_code = 'above_ceiling'
                        band_hi = history_val if history_val else current_raw_price
                        reason_text = (
                            f'Сейчас {current_raw_price:.0f} PLN — выше потолка показа'
                            f' ({ceiling_val:.0f}–{band_hi:.0f})'
                            f' ({delta_from_last:+.0f}% к последней цене в фильтре)'
                        )
                        last_dates = str(last_row.get('dates') or '')
                        raw_dates = str(current_raw_dates or '')
                        if raw_dates and raw_dates != last_dates:
                            reason_text += '; другие даты поездки'
                    elif delta_from_last > 2.0 or last_move_pct > 0.5:
                        reason_code = 'up'
                        reason_text = f'Подорожал до {current_raw_price:.0f} PLN и вышел из фильтра'
                    else:
                        reason_code = 'flat'
                        reason_text = f'Сейчас {current_raw_price:.0f} PLN — в выдаче, но вне фильтра по цене'
                elif last_move_pct > 0.5:
                    reason_code = 'up'
                    reason_text = 'Вышел вверх из диапазона (цена росла)'
                elif baseline_pct is not None and baseline_pct <= -3.0 and last_move_pct <= 0.5:
                    reason_code = 'sold'
                    reason_text = 'Похоже на распроданный дил (был заметно ниже своей нормы)'
                elif last_move_pct < -0.5:
                    reason_code = 'sold'
                    reason_text = 'Цена падала перед исчезновением (возможно, раскупили)'
                else:
                    reason_code = 'flat'
                    reason_text = 'Пропал без явного движения цены'

                # Значимость: глубина падения + бонус за дорогой сегмент.
                significance = drop_depth + (drop_depth * 0.5 if is_expensive else 0.0)
                # "Заметный дил": дорогой отель, который заметно подешевел и пропал.
                notable = bool(is_expensive and drop_depth >= 5.0 and reason_code == 'sold')

                disappeared_events.append({
                    'hotel_name': name,
                    'dates': last_row.get('dates') if pd.notna(last_row.get('dates')) else '—',
                    'duration': last_row.get('duration') if pd.notna(last_row.get('duration')) else '—',
                    'airport': last_row.get('departure_airport') if pd.notna(last_row.get('departure_airport')) else '',
                    'offer_url': last_row.get('offer_url') if pd.notna(last_row.get('offer_url')) else '',
                    'first_seen': first_seen,
                    'last_seen': last_seen,
                    'visible_hours': visible_hours,
                    'observations': int(len(prices)),
                    'observations_full': int(len(full_hist)) if not full_hist.empty else int(len(prices)),
                    'last_price': last_price,
                    'current_raw_price': current_raw_price,
                    'current_raw_dates': current_raw_dates,
                    'current_raw_seen': current_raw_seen,
                    'still_in_scrape': still_in_scrape,
                    'min_price': hotel_min_price,
                    'max_price': hotel_max_price,
                    'typical_price': typical_price,
                    'baseline_pct': baseline_pct,
                    'min_below_pct': min_below_pct,
                    'deal_score': int(deal_info.get('score', 0)) if deal_info else 0,
                    'confidence': deal_info.get('confidence', 'Low') if deal_info else 'Low',
                    'is_expensive': is_expensive,
                    'reason_code': reason_code,
                    'reason_text': reason_text,
                    'significance': significance,
                    'notable': notable,
                })

            disappeared_events.sort(
                key=lambda x: (x['notable'], x['significance'], x['typical_price']),
                reverse=True
            )
    except Exception as e:
        print(f"⚠️ Не удалось посчитать журнал выпавших отелей: {e}")
        disappeared_events = []
    vanished_notable_count = sum(1 for e in disappeared_events if e['notable'])
    print(f"🫥 Выпавших отелей: {len(disappeared_events)} (заметных: {vanished_notable_count})")

    if len(entry_candidates) >= 5 and market_breadth >= 0.45:
        entry_signal_level = "high"
        entry_signal_title = "🔥 Сильный сигнал раннего входа"
        entry_signal_note = "Много отелей одновременно дешевеют и уже торгуются в нижнем квартиле своих цен."
    elif len(entry_candidates) >= 2 and market_breadth >= 0.30:
        entry_signal_level = "medium"
        entry_signal_title = "⚡ Умеренный сигнал раннего входа"
        entry_signal_note = "Есть несколько сильных кандидатов; рынок начинает смещаться в сторону более выгодных цен."
    else:
        entry_signal_level = "low"
        entry_signal_title = "🟢 Нейтральный сигнал"
        entry_signal_note = "Явного массового снижения пока нет, но отдельные выгодные точки могут появляться."
    
    # Загружаем историю алертов (если есть)
    alerts = []
    # Автоматически определяем файл алертов на основе файла данных
    if alerts_file is None:
        if 'egypt' in data_file:
            alerts_file = 'data/egypt_travel_prices_alerts.json'
        elif 'turkey' in data_file:
            alerts_file = 'data/turkey_travel_prices_alerts.json'
        else:
            alerts_file = 'data/travel_prices_alerts.json'
    
    alerts_path = alerts_file
    if os.path.exists(alerts_path):
        try:
            with open(alerts_path, 'r', encoding='utf-8') as f:
                alerts_data = json.load(f)
                # Поддерживаем как старый формат {"alerts": [...]}, так и новый формат [...]
                if isinstance(alerts_data, dict) and 'alerts' in alerts_data:
                    alerts = alerts_data.get('alerts', [])
                elif isinstance(alerts_data, list):
                    alerts = alerts_data
                else:
                    alerts = []
        except Exception:
            alerts = []

    # Сортируем алерты по времени (новые сверху)
    def parse_iso(ts):
        try:
            dt = datetime.fromisoformat(ts)
            # Если datetime naive, делаем его UTC
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            return datetime.min.replace(tzinfo=timezone.utc)

    # Сортируем по времени создания (created_at) если есть, иначе по timestamp
    alerts.sort(key=lambda a: parse_iso(a.get('created_at') or a.get('timestamp') or a.get('time') or ''), reverse=True)
    alerts = [a for a in alerts if _should_show_alert(a)]

    _data_dir = os.path.dirname(data_file) or "data"

    def _ingest_images_source(target: dict, path: str) -> None:
        if not os.path.exists(path):
            return
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f) or {}
            if isinstance(data, dict):
                target.update(data)
        except Exception:
            pass

    # Карта изображений: сначала общий fallback, затем региональный файл фильтра.
    images_map: dict = {}
    _ingest_images_source(images_map, os.path.join('data', 'hotel_images.json'))
    _ingest_images_source(images_map, os.path.join(_data_dir, 'hotel_images.json'))

    def normalize_image_url(raw_url: str) -> str:
        if raw_url is None:
            return ""
        url = str(raw_url).strip()
        if not url:
            return ""
        url = html_lib.unescape(url)

        # Приводим типичные относительные форматы.
        if url.startswith("//"):
            url = f"https:{url}"
        elif url.startswith("/"):
            url = f"https://fly.pl{url}"

        u = url.lower()

        # Отбрасываем заведомо нерабочие или "пиксельные" плейсхолдеры.
        if u.startswith("blob:"):
            return ""
        if u.startswith("data:image"):
            return ""
        if u.startswith("https://fly.pl/data:image"):
            return ""
        if "ivborw0kggoaaaansuheugaaaaeaaaab" in u and len(u) < 220:
            return ""
        if not (u.startswith("http://") or u.startswith("https://")):
            return ""
        return url

    # Имя каталога данных (filter_greece_7_10_days) — путь к JSON на Pages.
    _filter_data_id = os.path.basename(os.path.normpath(_data_dir))
    _hotel_series_dir = os.path.join(_data_dir, "hotel_series")
    _back_dashboard_href = filter_href_by_charts_subdir((charts_subdir or "").rstrip("/"))

    # Карточки отелей (визуальный режим по умолчанию)
    hotel_cards = []
    for _, hotel in all_hotels.head(200).iterrows():
        hotel_name = hotel['hotel_name']
        row_id = str(hotel.get('row_id') or hotel_name)
        duration_bucket = str(hotel.get('duration_bucket') or '')
        price = float(hotel['price'])
        dates = hotel['dates'] if pd.notna(hotel['dates']) else '—'
        duration = hotel['duration'] if pd.notna(hotel['duration']) else '—'
        offer_url = hotel.get('offer_url', '')
        image_url = ""
        for candidate in (hotel.get('image_url', ''), images_map.get(hotel_name, '')):
            normalized = normalize_image_url(candidate)
            if normalized:
                image_url = normalized
                break
        hotel_slug = slugify(hotel_name)
        chart_href = _hotel_chart_viewer_href(_filter_data_id, hotel_slug)

        delta_info = deltas_by_hotel.get(row_id)
        delta48 = f"{delta_info[1]:+.1f}%" if delta_info is not None else "—"
        avg_info = avg_baseline_delta.get(row_id)
        delta_avg = f"{avg_info[1]:+.1f}%" if avg_info is not None else "—"
        deal_info = deal_score_by_hotel.get(row_id, {'score': 0, 'confidence': 'Low'})
        deal_score = int(deal_info.get('score', 0))
        confidence = deal_info.get('confidence', 'Low')
        d48_for_badge = float(delta_info[1]) if delta_info is not None else None
        d_avg_for_badge = float(avg_info[1]) if avg_info is not None else None
        comeback_drop = deal_info.get('comeback_drop_pct')
        deal_label, deal_class, _ = classify_deal_badge(
            deal_score, confidence, d48_for_badge, d_avg_for_badge, comeback_drop
        )
        comeback = comeback_from_premium(
            price, premium_history_by_hotel.get(row_id), ceiling_val
        )
        comeback_html = (
            f'<span class="comeback-badge">{comeback["badge_html"]}</span>'
            if comeback else ''
        )
        
        forecast = determine_price_forecast(
            deal_score, confidence, d_avg_for_badge, d48_for_badge, comeback_drop,
        )

        # Альтернативные аэропорты для карточного режима
        card_cheaper_alt_html = ""
        if df_all_airports is not None and not df_all_airports.empty:
            cur_airport = hotel.get('from_airport')
            if not cur_airport or pd.isna(cur_airport) or not str(cur_airport).strip():
                cur_airport = extract_airport_from_url(hotel.get('offer_url') or hotel.get('url', ''))
            alts = find_cheaper_airport_alternatives(
                df_all_airports,
                hotel_name,
                dates,
                price,
                cur_airport,
            )
            if alts:
                best_alt = alts[0]
                alt_url = best_alt.get('url', '')
                alt_airport = html_lib.escape(str(best_alt['airport']))
                alt_price = best_alt['price']
                alt_savings = best_alt['savings']
                alt_savings_pct = best_alt['savings_percent']
                alt_title = html_lib.escape(
                    f"Вылет из {best_alt['airport']}: {alt_price:.0f} PLN (дешевле на {alt_savings:.0f} PLN / {alt_savings_pct:.1f}%)",
                    quote=True,
                )
                if alt_url:
                    card_cheaper_alt_html = f'<a href="{html_lib.escape(str(alt_url), quote=True)}" target="_blank" class="cheaper-alt-badge" title="{alt_title}">✈️ <span class="alt-label">{alt_airport}: {alt_price:.0f} PLN</span> <span class="alt-savings">(−{alt_savings:.0f} PLN)</span></a>'
                else:
                    card_cheaper_alt_html = f'<span class="cheaper-alt-badge" title="{alt_title}">✈️ <span class="alt-label">{alt_airport}: {alt_price:.0f} PLN</span> <span class="alt-savings">(−{alt_savings:.0f} PLN)</span></span>'

        hotel_cards.append({
            "hotel_name": hotel_name,
            "row_id": row_id,
            "duration_bucket": duration_bucket,
            "hotel_name_html": html_lib.escape(str(hotel_name)),
            "price": price,
            "dates": str(dates),
            "duration": str(duration),
            "offer_url": str(offer_url) if offer_url and pd.notna(offer_url) else "",
            "image_url": str(image_url) if image_url and pd.notna(image_url) else "",
            "chart_href": chart_href,
            "delta48": delta48,
            "delta_avg": delta_avg,
            "deal_score": deal_score,
            "deal_label": deal_label,
            "deal_class": deal_class,
            "confidence": confidence,
            "comeback_html": comeback_html,
            "cheaper_alt_html": card_cheaper_alt_html,
            "departure_date": str(hotel.get('departure_date') or ''),
            "departure_key": str(hotel.get('departure_key') or ''),
            "forecast_text": forecast["text"],
            "forecast_class": forecast["class"],
            "forecast_icon": forecast["icon"],
        })

    hotel_meta_by_name = {c["hotel_name"]: c for c in hotel_cards}
    for hotel_name, grp in df.sort_values('scraped_at_display').groupby('hotel_name', sort=False):
        name = str(hotel_name)
        if name in hotel_meta_by_name:
            continue
        pick = grp.sort_values('scraped_at_display').iloc[-1]
        image_url = ""
        for candidate in (pick.get('image_url', ''), images_map.get(name, '')):
            normalized = normalize_image_url(candidate)
            if normalized:
                image_url = normalized
                break
        hotel_slug = slugify(name)
        chart_href = _hotel_chart_viewer_href(_filter_data_id, hotel_slug)
        offer_url = pick.get('offer_url', '')
        hotel_meta_by_name[name] = {
            "hotel_name": name,
            "hotel_name_html": html_lib.escape(name),
            "dates": str(pick['dates']) if pd.notna(pick.get('dates')) else '—',
            "duration": str(pick['duration']) if pd.notna(pick.get('duration')) else '—',
            "offer_url": str(offer_url) if offer_url and pd.notna(offer_url) else "",
            "image_url": image_url,
            "chart_href": chart_href,
        }

    for name, meta in hotel_meta_by_name.items():
        normalized = normalize_image_url(meta.get("image_url") or images_map.get(name, ""))
        if normalized:
            images_map[str(name)] = normalized
    for name, url in list(images_map.items()):
        normalized = normalize_image_url(url)
        if normalized:
            images_map[name] = normalized
        else:
            images_map.pop(name, None)

    os.makedirs(_hotel_series_dir, exist_ok=True)
    if write_legacy_hotel_html and charts_subdir:
        os.makedirs(charts_subdir, exist_ok=True)

    from price_alerts_v2 import ALERT_THRESHOLD_PERCENT

    # График отеля — JSON-серии + опционально legacy HTML
    chart_href_lookup: dict[str, str] = {}
    series_manifest = _load_hotel_series_manifest(_hotel_series_dir)
    series_written = 0
    series_skipped = 0
    chart_hotel_names = sorted(set(df_history["hotel_name"].astype(str).unique()))
    history_by_hotel = {
        str(name): grp
        for name, grp in df_history.groupby("hotel_name", sort=False)
    }
    for hotel_name in chart_hotel_names:
        hotel_ts = history_by_hotel.get(str(hotel_name), pd.DataFrame())
        if not hotel_ts.empty:
            hotel_ts = hotel_ts.dropna(subset=['scraped_at_display']).sort_values('scraped_at_display')
        x_values = [pd.to_datetime(t).isoformat() for t in hotel_ts['scraped_at_display'].tolist()]
        x_display = [pd.to_datetime(t).strftime('%d.%m.%Y %H:%M') for t in hotel_ts['scraped_at_display'].tolist()]
        y_values = [float(p) for p in hotel_ts['price'].tolist()]
        dates_list = hotel_ts['dates'].fillna('Неизвестно').tolist()
        
        text_values = []
        for x_val, trip_dates in zip(x_display, dates_list):
            text_values.append(f"Проверка: {x_val}<br>Даты поездки: {trip_dates}")

        hotel_slug = slugify(hotel_name)
        viewer_href = _hotel_chart_viewer_href(_filter_data_id, hotel_slug)

        meta = hotel_meta_by_name.get(hotel_name, {})
        if not hotel_ts.empty:
            pick_meta = hotel_ts.iloc[-1]
            if not meta.get('hotel_name_html'):
                meta = {
                    'hotel_name_html': html_lib.escape(str(hotel_name)),
                    'dates': '—',
                    'duration': '—',
                    'offer_url': '',
                    'image_url': '',
                }
            meta = dict(meta)
            meta['dates'] = str(pick_meta['dates']) if pd.notna(pick_meta.get('dates')) else meta.get('dates', '—')
            meta['duration'] = str(pick_meta['duration']) if pd.notna(pick_meta.get('duration')) else meta.get('duration', '—')
            offer_from_full = pick_meta.get('offer_url', '')
            if offer_from_full and pd.notna(offer_from_full):
                meta['offer_url'] = str(offer_from_full)
        elif not meta.get('hotel_name_html'):
            meta = {
                'hotel_name_html': html_lib.escape(str(hotel_name)),
                'dates': '—',
                'duration': '—',
                'offer_url': '',
                'image_url': '',
            }

        delta_info = deltas_by_hotel.get(hotel_name)
        delta48_str = f"{delta_info[1]:+.1f}%" if delta_info is not None else "—"
        avg_info = avg_baseline_delta.get(hotel_name)
        delta_avg_str = f"{avg_info[1]:+.1f}%" if avg_info is not None else "—"
        deal_info = deal_score_by_hotel.get(hotel_name, {'score': 0, 'confidence': 'Low'})
        deal_score = int(deal_info.get('score', 0))
        confidence = deal_info.get('confidence', 'Low')
        d48_for_badge = float(delta_info[1]) if delta_info is not None else None
        d_avg_for_badge = float(avg_info[1]) if avg_info is not None else None
        comeback_drop_chart = deal_info.get('comeback_drop_pct')
        deal_label, deal_class, _ = classify_deal_badge(
            deal_score, confidence, d48_for_badge, d_avg_for_badge, comeback_drop_chart
        )

        if y_values:
            price_series = pd.Series(y_values, dtype='float64')
            min_p = float(price_series.min())
            max_p = float(price_series.max())
        else:
            min_p = max_p = 0.0
        median_p = float(deal_info.get('typical_price') or deal_info.get('median') or 0.0)
        hist_grp_chart = df_sorted_full[df_sorted_full['hotel_name'] == hotel_name]
        if median_p <= 0 and not hist_grp_chart.empty:
            median_p = _time_weighted_price_baseline(hist_grp_chart) or 0.0

        trip_dates_label = str(meta.get('dates') or (dates_list[-1] if dates_list else '—'))

        series_payload = {
            "version": 1,
            "filter_id": _filter_data_id,
            "slug": hotel_slug,
            "hotel_name": str(hotel_name),
            "x": x_values,
            "y": y_values,
            "hover": text_values,
            "meta": {
                "dates": str(meta.get("dates") or "—"),
                "duration": str(meta.get("duration") or "—"),
                "offer_url": str(meta.get("offer_url") or ""),
                "image_url": str(meta.get("image_url") or ""),
            },
            "deal_score": deal_score,
            "deal_label": deal_label,
            "deal_class": deal_class,
            "delta48": delta48_str,
            "delta_avg": delta_avg_str,
            "confidence": confidence,
            "median_p": median_p,
            "min_p": min_p,
            "max_p": max_p,
            "samples": len(y_values),
            "alert_threshold": ALERT_THRESHOLD_PERCENT,
            "trip_dates_label": trip_dates_label,
            "display_price_ceiling": ceiling_val,
            "history_price_ceiling": history_val,
            "back_href": _back_dashboard_href,
        }
        content_hash = _hotel_series_payload_hash(series_payload)
        if series_manifest.get(hotel_slug) != content_hash:
            series_path = os.path.join(_hotel_series_dir, f"{hotel_slug}.json")
            with open(series_path, "w", encoding="utf-8") as f:
                json.dump(series_payload, f, ensure_ascii=False, indent=2)
            series_manifest[hotel_slug] = content_hash
            series_written += 1
        else:
            series_skipped += 1

        if write_legacy_hotel_html and charts_subdir:
            charts_dir = charts_subdir
            hotel_html_path = os.path.join(charts_dir, f"{hotel_slug}.html")
            back_href = os.path.relpath(_back_dashboard_href, start=os.path.dirname(hotel_html_path))
            favicon_href = os.path.relpath("favicon.svg", start=os.path.dirname(hotel_html_path))
            chart_html = _render_hotel_chart_page(
                hotel_name=str(hotel_name),
                hotel_name_html=meta.get('hotel_name_html') or html_lib.escape(str(hotel_name)),
                x_values=x_values,
                y_values=y_values,
                hover_lines=text_values,
                meta=meta,
                back_href=back_href,
                deal_score=deal_score,
                deal_label=deal_label,
                deal_class=deal_class,
                delta48_str=delta48_str,
                delta_avg_str=delta_avg_str,
                confidence=confidence,
                median_p=median_p,
                min_p=min_p,
                max_p=max_p,
                samples=len(y_values),
                alert_threshold=ALERT_THRESHOLD_PERCENT,
                trip_dates_label=trip_dates_label,
                display_price_ceiling=ceiling_val,
                history_price_ceiling=history_val,
                favicon_href=favicon_href,
            )
            with open(hotel_html_path, 'w', encoding='utf-8') as f:
                f.write(chart_html)

        _register_chart_href(chart_href_lookup, hotel_name, viewer_href)

    _save_hotel_series_manifest(_hotel_series_dir, series_manifest)
    print(f"📈 Hotel series: записано {series_written}, без изменений {series_skipped} → {_hotel_series_dir}")

    for name, meta in hotel_meta_by_name.items():
        href = meta.get("chart_href")
        if href:
            _register_chart_href(chart_href_lookup, str(name), str(href))

    # HTML шаблон
    # Готовим HTML блок изменений, выводим только если есть хотя бы один список
    changes_html = ""
    if decreases_24h or increases_24h:
        changes_html += """
        <div class=\"changes-section\">"""
        if decreases_24h:
            changes_html += """
            <div class=\"changes-block\">
                <h3>📉 Наиболее подешевевшие (24ч)</h3>"""
            for change in decreases_24h:
                changes_html += f"""
                <div class=\"change-item change-decrease\">
                    <div>
                        <div class=\"hotel-name\">{change['hotel_name']}</div>
                        <div class=\"change-percent\">{change['change']:+.0f} PLN ({change['change_percent']:+.1f}%)</div>
                    </div>
                    <div class=\"change-price\">{change['old_price']:.0f} → {change['new_price']:.0f} PLN</div>
                </div>"""
            changes_html += """
            </div>"""
        if increases_24h:
            changes_html += """
            <div class=\"changes-block\">
                <h3>📈 Наиболее подорожавшие (24ч)</h3>"""
            for change in increases_24h:
                changes_html += f"""
                <div class=\"change-item change-increase\">
                    <div>
                        <div class=\"hotel-name\">{change['hotel_name']}</div>
                        <div class=\"change-percent\">{change['change']:+.0f} PLN ({change['change_percent']:+.1f}%)</div>
                    </div>
                    <div class=\"change-price\">{change['old_price']:.0f} → {change['new_price']:.0f} PLN</div>
                </div>"""
            changes_html += """
            </div>"""
        changes_html += """
        </div>"""

    if decreases_7d or increases_7d:
        changes_html += """
        <div class=\"changes-section\">"""
        if decreases_7d:
            changes_html += """
            <div class=\"changes-block\">
                <h3>📉 Наиболее подешевевшие (7д)</h3>"""
            for change in decreases_7d:
                changes_html += f"""
                <div class=\"change-item change-decrease\">
                    <div>
                        <div class=\"hotel-name\">{change['hotel_name']}</div>
                        <div class=\"change-percent\">{change['change']:+.0f} PLN ({change['change_percent']:+.1f}%)</div>
                    </div>
                    <div class=\"change-price\">{change['old_price']:.0f} → {change['new_price']:.0f} PLN</div>
                </div>"""
            changes_html += """
            </div>"""
        if increases_7d:
            changes_html += """
            <div class=\"changes-block\">
                <h3>📈 Наиболее подорожавшие (7д)</h3>"""
            for change in increases_7d:
                changes_html += f"""
                <div class=\"change-item change-increase\">
                    <div>
                        <div class=\"hotel-name\">{change['hotel_name']}</div>
                        <div class=\"change-percent\">{change['change']:+.0f} PLN ({change['change_percent']:+.1f}%)</div>
                    </div>
                    <div class=\"change-price\">{change['old_price']:.0f} → {change['new_price']:.0f} PLN</div>
                </div>"""
            changes_html += """
            </div>"""
        changes_html += """
        </div>"""

    # Game-changer блок: сигнал раннего входа + top opportunities
    signal_class = f"entry-signal entry-{entry_signal_level}"
    signal_items_html = ""
    if entry_top:
        for item in entry_top:
            signal_items_html += f"""
            <div class="entry-item">
                <div>
                    <div class="hotel-name">{item['hotel_name']}</div>
                    <div class="change-percent">Deal Score: {item['deal_score']} ({item.get('confidence','')}) • Δ48ч {item['delta48_pct']:+.1f}%</div>
                </div>
                <div class="change-price">{item['latest']:.0f} PLN</div>
            </div>
            """
    else:
        signal_items_html = "<div class='alerts-empty'>Пока нет кандидатов под строгие критерии раннего входа.</div>"

    entry_signal_html = f"""
    <div class="{signal_class}">
        <div class="entry-title">{entry_signal_title}</div>
        <div class="entry-note">{entry_signal_note}</div>
        <div class="entry-stats">Кандидаты: {len(entry_candidates)} • Доля отелей со снижением (48ч): {market_breadth*100:.1f}%</div>
        <div class="entry-list">{signal_items_html}</div>
    </div>
    """

    # Компактный блок региональных вылетов: "самолёт" выводим через регион + даты,
    # потому что курорты одного региона прилетают в один аэропорт.
    departure_block_html = ""
    departure_history_html = ""
    departure_offers_json = "{}"
    departure_price_curves_json = "{}"
    try:
        data_dir = os.path.dirname(data_file) or "."
        current_cohorts, history_cohorts, stored_hot_history = _load_departure_cohort_frames(
            data_dir, data_file
        )

        if not current_cohorts.empty or not history_cohorts.empty:
            history_source = history_cohorts if not history_cohorts.empty else current_cohorts
            current_source = current_cohorts if not current_cohorts.empty else history_cohorts
            group_by_airport = should_group_by_arrival_airport(
                current_source["country"].iloc[0] if not current_source.empty else "",
                active_filter_id(charts_subdir),
            )
            if group_by_airport:
                current_source = aggregate_cohorts_by_arrival_airport(current_source)
            work = _prepare_departure_cohorts(history_source)
            current_work = _prepare_departure_cohorts(current_source)

            def _region_label(value):
                return str(value or 'region').replace('-', ' ').title()

            def _score_class(score):
                score = float(score or 0)
                if score >= 70:
                    return 'hot'
                if score >= 45:
                    return 'warm'
                return 'calm'

            def _days_until_label(value):
                try:
                    days = int(value)
                except (TypeError, ValueError):
                    return "вылет ?"
                if days <= 0:
                    return "вылет сегодня"
                if days == 1:
                    return "вылет завтра"
                return f"вылет через {days} дн."

            latest_dep_ts = current_work['_run_ts'].max() if not current_work.empty else None
            hot_history = (
                stored_hot_history
                if stored_hot_history is not None
                else build_hot_departure_history(work)
            )
            history_rows_html = ""
            for item in hot_history[:8]:
                region = html_lib.escape(_region_label(item.get('region')))
                dep_date = html_lib.escape(str(item.get('departure_date') or '—'))
                nights = item.get('nights')
                nights_label = f"{int(nights)}н" if str(nights) != '' else "?н"
                best_days = item.get('days_to_departure_at_best')
                best_days_label = _days_until_label(best_days)
                p10_drop = float(item.get('best_p10_change_pct') or item.get('max_p10_drop_pct') or 0)
                med_drop = float(item.get('best_median_change_pct') or item.get('max_median_drop_pct') or 0)
                prev_median = float(item.get('best_prev_median_price') or 0)
                curr_median = float(item.get('best_median_price') or 0)
                prev_p10 = float(item.get('best_prev_p10_price') or 0)
                curr_p10 = float(item.get('best_p10_price') or 0)
                median_drop_pln = curr_median - prev_median if prev_median > 0 else 0.0
                p10_drop_pln = curr_p10 - prev_p10 if prev_p10 > 0 else 0.0
                if prev_median > 0 and med_drop <= -0.5:
                    drop_text = (
                        f"Типичная цена: {prev_median:.0f} → {curr_median:.0f} PLN "
                        f"({median_drop_pln:.0f} PLN, {med_drop:+.1f}%)"
                    )
                    drop_subtext = (
                        f"Дешёвые 10%: {prev_p10:.0f} → {curr_p10:.0f} PLN "
                        f"({p10_drop_pln:.0f} PLN, {p10_drop:+.1f}%)"
                        if prev_p10 > 0 and p10_drop <= -0.5 else ''
                    )
                elif prev_p10 > 0 and p10_drop <= -0.5:
                    drop_text = (
                        f"Дешёвые 10%: {prev_p10:.0f} → {curr_p10:.0f} PLN "
                        f"({p10_drop_pln:.0f} PLN, {p10_drop:+.1f}%)"
                    )
                    drop_subtext = ''
                else:
                    drop_text = "Падение цены"
                    drop_subtext = ''
                drop_sub_html = f'<br><span>{html_lib.escape(drop_subtext)}</span>' if drop_subtext else ''
                score_cls = _score_class(item.get('max_hot_score') or 0)
                score = int(item.get('max_hot_score') or 0)
                hotels = int(item.get('max_hotel_count') or 0)
                history_rows_html += f"""
                    <tr class="departure-history-row" data-departure-key="{html_lib.escape(str(item.get('departure_key') or ''))}" role="button" tabindex="0" title="Показать отели по этому вылету">
                        <td><strong>{region}</strong><br><span>{dep_date} · {nights_label}</span></td>
                        <td><strong>{best_days_label}</strong></td>
                        <td><strong>{html_lib.escape(drop_text)}</strong>{drop_sub_html}<br><span>{html_lib.escape(str(item.get('best_seen_at') or ''))[:16]}</span></td>
                        <td><span class="departure-score mini {score_cls}">{score}</span> деш.10% <strong>{float(item.get('best_p10_price') or 0):.0f}</strong> PLN<br><span>мин. {float(item.get('best_min_price') or 0):.0f} · середина {float(item.get('best_median_price') or 0):.0f} · {hotels} отелей</span></td>
                    </tr>
                """
            if history_rows_html:
                departure_history_html = f"""
        <details class="dashboard-fold departure-history-fold" id="departureHistoryFold">
            <summary>
                <span>История горячих вылетов ({len(hot_history)})</span>
                <span class="fold-title-meta">Только прошедшие hot/late-buy кейсы</span>
                <span class="fold-chevron">⌄</span>
            </summary>
            <div class="fold-content">
                <p class="departure-history-hint">Архив уже наступивших вылетов, где за последнюю неделю до старта заметно падали цены. Нажмите на строку — график цен D-{HOT_DEPARTURE_CHART_DAYS_MAX}…D-0 и список отелей.</p>
                <div class="table-container">
                    <table class="hotels-table departure-history-table">
                        <thead>
                            <tr>
                                <th>Вылет</th>
                                <th>За сколько</th>
                                <th>Падение</th>
                                <th>Цена/сигнал</th>
                            </tr>
                        </thead>
                        <tbody>{history_rows_html}
                        </tbody>
                    </table>
                </div>
            </div>
        </details>
                """
            latest_departures = current_work[current_work['_run_ts'] == latest_dep_ts].copy() if latest_dep_ts is not None else pd.DataFrame()
            latest_departures = latest_departures[latest_departures['hotel_count'].fillna(0) >= 2]
            latest_departures = latest_departures[latest_departures['days_to_departure'].fillna(9999) <= 8]
            nearby_departures_count = int(latest_departures['departure_key'].nunique()) if not latest_departures.empty else 0

            if not latest_departures.empty:
                top_departures = latest_departures.sort_values(
                    ['days_to_departure', 'hot_score', 'p10_change_pct', 'median_change_pct', 'p10_price'],
                    ascending=[True, False, True, True, True]
                )
                active_departures = int(latest_departures['departure_key'].nunique())
                hot_departures = int((latest_departures['hot_score'].fillna(0) >= 70).sum())
                active_hubs = active_departures
                active_regions = int(latest_departures['region'].fillna('').astype(str).nunique())
                best_p10 = float(latest_departures['p10_price'].min())
                hub_stats_label = "аэропортов" if group_by_airport else "регионов"
                hub_stats_value = active_hubs if group_by_airport else active_regions

                def _region_label(value):
                    return str(value or 'region').replace('-', ' ').title()

                def _score_class(score):
                    score = float(score or 0)
                    if score >= 70:
                        return 'hot'
                    if score >= 45:
                        return 'warm'
                    return 'calm'

                def _change_label(value, label):
                    try:
                        pct = float(value or 0)
                    except (TypeError, ValueError):
                        pct = 0.0
                    if abs(pct) < 0.5:
                        return ''
                    cls = 'drop' if pct < 0 else 'up'
                    arrow = '↓' if pct < 0 else '↑'
                    return f'<span class="departure-change {cls}">{label} {arrow} {pct:+.1f}%</span>'

                def _days_until_label(value):
                    try:
                        days = int(value)
                    except (TypeError, ValueError):
                        return "вылет ?"
                    if days <= 0:
                        return "вылет сегодня"
                    if days == 1:
                        return "вылет завтра"
                    return f"вылет через {days} дн."

                rows_html = ""
                for _, dep in top_departures.iterrows():
                    score = _hot_score(dep)
                    score_cls = _score_class(score)
                    card_hot_cls = ' is-hot' if score >= 70 else (' is-warm' if score >= 45 else '')
                    hot_icon = '🔥 ' if score >= 70 else ''
                    status_label = departure_status_label(dep)
                    days_left = dep.get('days_to_departure')
                    days_label = _days_until_label(days_left)
                    nights = dep.get('nights')
                    nights_label = f"{int(nights)}н" if pd.notna(nights) and str(nights) != '' else "?н"
                    region = html_lib.escape(str(dep.get("hub_label") or _region_label(dep.get("region"))))
                    hub_regions_raw = str(dep.get("hub_regions") or "")
                    hub_regions_html = ""
                    if hub_regions_raw:
                        hub_regions_html = (
                            f'<div class="departure-hub-resorts">'
                            f'{html_lib.escape(hub_regions_subtitle(hub_regions_raw.split(",")))}'
                            f'</div>'
                        )
                    departure_date = html_lib.escape(str(dep.get('departure_date') or '—'))
                    return_date = str(dep.get('return_date') or '').strip()
                    return_label = (
                        f"→ {html_lib.escape(return_date)}"
                        if return_date else ''
                    )
                    delta_bits = []
                    common_n = int(dep.get("common_hotel_count") or 0)
                    if common_n >= MIN_COMMON_HOTELS:
                        lookback_lbl = f"за {COHORT_LOOKBACK_TARGET_HOURS}ч"
                        delta_bits.extend([
                            _change_label(
                                dep.get("p10_change_pct"),
                                f"{cheap_tier_label(common_n)} ({lookback_lbl})",
                            ),
                            _change_label(
                                dep.get("median_change_pct"),
                                f"середина ({lookback_lbl})",
                            ),
                        ])
                    try:
                        mean_avg_delta = float(dep.get("mean_avg_delta_pct") or 0)
                    except (TypeError, ValueError):
                        mean_avg_delta = 0.0
                    avg_deal = int(dep.get("avg_deal_score") or 0)
                    hot_deals = int(dep.get("hot_deal_count") or 0)
                    hotel_n = int(dep.get("hotel_count") or 0)
                    if hotel_n >= MIN_DEAL_HOTELS and abs(mean_avg_delta) >= 0.5:
                        delta_bits.append(
                            _change_label(mean_avg_delta, "ниже типичной")
                        )
                    if hotel_n >= MIN_DEAL_HOTELS and avg_deal >= 65:
                        deal_cls = "drop" if avg_deal >= 75 else "warm"
                        delta_bits.append(
                            f'<span class="departure-change {deal_cls}">Deal {avg_deal}'
                            f'{f" · {hot_deals} Hot" if hot_deals else ""}</span>'
                        )
                    delta_html = ''.join(bit for bit in delta_bits if bit) or '<span class="departure-change muted">без сильного движения</span>'
                    rows_html += f"""
                    <div class="departure-card departure-card-clickable{card_hot_cls}" data-departure-key="{html_lib.escape(str(dep.get('departure_key') or ''))}" role="button" tabindex="0" title="Показать отели по этому вылету">
                        <div class="departure-card-head">
                            <div class="departure-title">{hot_icon}{region}</div>
                            <span class="departure-status {score_cls}">{status_label}</span>
                        </div>
                        {hub_regions_html}
                        <div class="departure-facts">
                            <span>{departure_date}</span>
                            {f'<span>{return_label}</span>' if return_label else ''}
                            <span>{nights_label}</span>
                            <span>{days_label}</span>
                        </div>
                        <div class="departure-delta">{delta_html}</div>
                        <div class="departure-price-line">
                            <span>типичная <strong>{float(dep.get('median_price') or 0):.0f}</strong> PLN</span>
                            <span>дешёвые 10% <strong>{float(dep.get('p10_price') or 0):.0f}</strong> PLN</span>
                        </div>
                    </div>
                    """

                departure_block_html = f"""
        <div class="departures-strip">
            <div class="departures-head">
                <div>
                    <h3>🛫 Ближайшие вылеты</h3>
                    <p>Все вылеты на ближайшие 8 дней.{' В Турции карточка = аэропорт прилёта (не отдельный курорт).' if group_by_airport else ''} Горячие, где цены реально падают, подсвечены огоньком и ярким фоном.</p>
                </div>
                <div class="departure-mini-stats">
                    <span>{active_departures} активных</span>
                    <span>{hot_departures} горячих</span>
                    <span>{hub_stats_value} {hub_stats_label}</span>
                    <span>лучшие 10% от {best_p10:.0f} PLN</span>
                </div>
            </div>
            <div class="departure-legend">{'Курорты одного аэропорта объединены: Side/Kemer/Alanya/Belek → Анталия (AYT). ' if group_by_airport else ''}«Горит» / «Снижается» — падение когорты за ~{COHORT_LOOKBACK_TARGET_HOURS}ч (≥{MIN_COMMON_HOTELS} отелей). «Выгодно» — отели ниже типичной цены (≥{MIN_DEAL_HOTELS}). В модалке те же Deal и Δ типичной.</div>
            <div class="departure-grid">{rows_html}</div>
        </div>
                """
            elif nearby_departures_count:
                departure_block_html = f"""
        <div class="departures-strip">
            <div class="departures-head">
                <div>
                    <h3>🛫 Ближайшие вылеты</h3>
                    <p>Смотрим все ближайшие вылеты до 8 дней и ждём заметного падения цен.</p>
                </div>
                <div class="departure-mini-stats">
                    <span>{nearby_departures_count} ближайших</span>
                    <span>0 горящих</span>
                </div>
            </div>
            <div class="departure-legend">Ближайших вылетов в данных сейчас нет.</div>
        </div>
                """

            departure_keys: list[str] = []
            curve_keys: list[str] = []
            preferred_runs: dict[str, str] = {}
            if latest_dep_ts is not None and not current_work.empty:
                latest_rows = current_work[current_work["_run_ts"] == latest_dep_ts]
                if not latest_rows.empty:
                    latest_run = str(latest_rows.iloc[0].get("run_started_at") or "")
                    for _, dep in latest_rows.iterrows():
                        key = str(dep.get("departure_key") or "")
                        if key:
                            departure_keys.append(key)
                            preferred_runs[key] = latest_run
            for item in hot_history:
                key = str(item.get("departure_key") or "")
                if key and key not in preferred_runs:
                    departure_keys.append(key)
                    # Archived departures: hotel table = last prices on departure day (D-0).
                    preferred_runs[key] = str(
                        item.get("departure_day_at")
                        or item.get("last_seen_at")
                        or item.get("best_seen_at")
                        or ""
                    )
                if key:
                    curve_keys.append(key)
            for _, dep in (latest_departures.iterrows() if not latest_departures.empty else []):
                key = str(dep.get("departure_key") or "")
                if key:
                    curve_keys.append(key)
            if curve_keys:
                departure_price_curves_json = json.dumps(
                    build_departure_price_curves(work, list(dict.fromkeys(curve_keys))),
                    ensure_ascii=False,
                )
            if departure_keys:
                offers_df = load_combined_departure_offers(data_dir, data_file)
                departure_offers_payload = build_departure_offers_index(
                    offers_df, departure_keys, preferred_runs
                )
                departure_hotel_histories = build_departure_hotel_histories(offers_df)
                for payload in departure_offers_payload.values():
                    for offer in payload.get("offers", []):
                        hotel_name = str(offer.get("hotel_name") or "")
                        offer["chart_href"] = _resolve_chart_href(
                            hotel_name, chart_href_lookup, charts_subdir, slugify
                        )
                        hist = departure_hotel_histories.get(hotel_name)
                        try:
                            offer_price = float(offer.get("price"))
                        except (TypeError, ValueError):
                            offer["deal_has_data"] = False
                            offer["deal_score"] = None
                            offer["deal_label"] = ""
                            offer["deal_class"] = ""
                            offer["delta_avg"] = "—"
                            continue
                        if hist is None or len(hist) < 2:
                            offer["deal_has_data"] = False
                            offer["deal_score"] = None
                            offer["deal_label"] = ""
                            offer["deal_class"] = ""
                            offer["delta_avg"] = "—"
                            continue
                        metrics = compute_hotel_deal_metrics(
                            hist,
                            offer_price,
                            ta_rating=offer.get("ta_rating"),
                            ta_review_count=offer.get("ta_review_count"),
                        )
                        deal_score = int(metrics["deal_score"])
                        confidence = metrics["confidence"]
                        d_avg = float(metrics["avg_delta_pct"])
                        deal_label, deal_class, _ = classify_deal_badge(
                            deal_score, confidence, None, d_avg, None
                        )
                        offer["deal_has_data"] = True
                        offer["deal_score"] = deal_score
                        offer["deal_label"] = deal_label
                        offer["deal_class"] = deal_class
                        offer["delta_avg"] = f"{d_avg:+.1f}%"
                departure_offers_json = json.dumps(departure_offers_payload, ensure_ascii=False)
    except Exception as e:
        print(f"⚠️ Не удалось построить блок вылетов: {e}")

    # Время последнего обновления для шапки
    try:
        _updated_ts = df['scraped_at_display'].max()
        updated_date = _updated_ts.strftime('%d.%m.%Y')
        updated_time = _updated_ts.strftime('%H:%M')
        updated_iso = _updated_ts.isoformat()
        # Статус свежести данных (зелёный / жёлтый / красный)
        _now_ts = datetime.now(tz=_updated_ts.tzinfo) if _updated_ts.tzinfo else datetime.now()
        _age_hours = (_now_ts - _updated_ts).total_seconds() / 3600
        if _age_hours < 3:
            update_status_cls = 'update-status--ok'
            update_status_icon = '🟢'
        elif _age_hours < 12:
            update_status_cls = 'update-status--warn'
            update_status_icon = '🟡'
        else:
            update_status_cls = 'update-status--err'
            update_status_icon = '🔴'
    except Exception:
        _updated_ts = datetime.now()
        updated_date = _updated_ts.strftime('%d.%m.%Y')
        updated_time = _updated_ts.strftime('%H:%M')
        updated_iso = _updated_ts.isoformat()
        update_status_cls = 'update-status--ok'
        update_status_icon = '🟢'

    filter_params_html = render_filter_params_html(
        filter_config,
        display_price_ceiling=ceiling_val,
        history_price_ceiling=history_val,
        escape=html_lib.escape,
    )
    global_duration_switch_html = (
        render_global_duration_switch_html(
            trip_buckets,
            default_bucket_id=default_trip_duration_bucket,
            escape=html_lib.escape,
        )
        if use_trip_buckets else ""
    )
    duration_views_json_embed = ""
    cfg_path = resolve_config_path(data_file, config_file)
    if filter_config and cfg_path:
        print(f"📋 Параметры фильтра из {cfg_path}")

    _current_filter = active_filter_id(charts_subdir)
    _sidebar_nav_parts = [
        '<a href="index.html" class="nav-item">'
        '<span class="flag">🏠</span>'
        '<span class="country-name">Главная</span></a>'
    ]
    for group in active_filter_groups():
        _sidebar_nav_parts.append(
            f'<div class="nav-group-label"><span>{group["icon"]}</span>{group["label"]}</div>'
        )
        for flt in group['filters']:
            active = ' active' if flt['id'] == _current_filter else ''
            _sidebar_nav_parts.append(
                f'<a href="{flt["href"]}" class="nav-item{active}">'
                f'<span class="country-name">{flt["title"]}</span></a>'
            )
    sidebar_nav_html = ''.join(_sidebar_nav_parts)
    
    # Детекция страны назначения для плашки "Куда"
    COUNTRY_MAP = {
        "egipt": "Египет",
        "egypt": "Египет",
        "grecja": "Греция",
        "greece": "Греция",
        "turcja": "Турция",
        "turkey": "Турция",
        "hiszpania": "Испания",
        "spain": "Испания",
        "cypr": "Кипр",
        "cyprus": "Кипр",
        "wlochy": "Италия",
        "italy": "Италия",
        "bulgaria": "Болгария",
        "albania": "Албания",
        "tunezja": "Тунис",
        "maroko": "Марокко",
    }
    combined_dest_str = f"{title} {output_file} {data_file} {filter_config or {}}".lower()
    dest_display_name = "Все страны"
    for k, v in COUNTRY_MAP.items():
        if k in combined_dest_str or v.lower() in combined_dest_str:
            dest_display_name = v
            break
    if dest_display_name == "Все страны" and not df_canonical.empty and 'offer_url' in df_canonical.columns:
        for url in df_canonical['offer_url'].dropna().head(30):
            path_info = parse_offer_path(str(url))
            c_key = (path_info.get('country') or '').lower()
            if c_key in COUNTRY_MAP:
                dest_display_name = COUNTRY_MAP[c_key]
                break

    entry_candidates_count = len(entry_candidates)
    market_breadth_pct_str = f"{market_breadth*100:.0f}%"
    best_deal_score_val = max((v['score'] for v in deal_score_by_hotel.values()), default=0)

    html_template = f"""<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800;900&display=swap" rel="stylesheet">
    <title>{title}</title>
    <link rel="icon" href="favicon.svg" type="image/svg+xml">
    <link rel="apple-touch-icon" href="favicon.svg">
    <style>
        :root {{
            --primary-color: #4f46e5;
            --primary-dark: #3730a3;
            --secondary-color: #0891b2;
            --accent-color: #f59e0b;
            --success-color: #10b981;
            --danger-color: #ef4444;
            --warning-color: #f59e0b;
            --info-color: #3b82f6;
            
            --gradient-primary: linear-gradient(135deg, #4f46e5 0%, #0ea5e9 100%);
            --gradient-success: linear-gradient(135deg, #10b981 0%, #22d3ee 100%);
            --gradient-danger: linear-gradient(135deg, #ef4444 0%, #fb7185 100%);
            --gradient-card: linear-gradient(145deg, rgba(255,255,255,0.88) 0%, rgba(250,252,255,0.80) 100%);

            --shadow-sm: 0 1px 2px rgba(15, 23, 42, 0.05);
            --shadow-md: 0 8px 24px rgba(15, 23, 42, 0.08);
            --shadow-lg: 0 14px 34px rgba(15, 23, 42, 0.12);
            --shadow-xl: 0 24px 52px rgba(15, 23, 42, 0.16);
            --section-gap: 1.35rem;
            --section-inner-x: 1.5rem;
            --panel-shell-radius: 16px;
            --table-block-max-width: 100%;
            --content-max-width: 1880px;
            --page-gutter: clamp(10px, 1.35vw, 24px);
            --container-padding: clamp(1rem, 1.5vw, 2rem);

            --radius-sm: 10px;
            --radius-md: 14px;
            --radius-lg: 18px;
            --radius-xl: 24px;

            --transition-fast: .15s ease;
            --transition-normal: .25s ease;
            --transition-slow: .45s ease;

            --surface: rgba(255,255,255,0.78);
            --surface-strong: rgba(255,255,255,0.92);
            --border-soft: rgba(148, 163, 184, 0.28);
            --text-main: #0f172a;
            --text-muted: #64748b;
        }}
        
        * {{
            box-sizing: border-box;
        }}
        
        body {{
            font-family: 'Inter', 'Segoe UI', 'Roboto', 'Helvetica Neue', Arial, sans-serif;
            margin: 0;
            padding: 0;
            background: #f0f6fc;
            min-height: 100vh;
            line-height: 1.6;
            color: var(--text-main);
            position: relative;
            overflow-x: hidden;
        }}

        body::before {{
            content: "";
            position: fixed;
            inset: 0;
            z-index: -2;
            background: linear-gradient(180deg, #e8f3fc 0%, #f0f7ff 60%, #e8f3fc 100%);
        }}

        body::after {{
            content: "";
            position: fixed;
            inset: 0;
            z-index: -1;
            background:
                radial-gradient(1400px 700px at -10% -25%, rgba(186, 230, 253, 0.35), transparent 58%),
                radial-gradient(1200px 800px at 120% -10%, rgba(224, 242, 254, 0.4), transparent 56%),
                radial-gradient(900px 600px at 50% 120%, rgba(199, 210, 254, 0.25), transparent 60%);
            animation: gradientDrift 18s ease-in-out infinite alternate;
        }}
        
        .container {{
            width: min(var(--content-max-width), calc(100% - (var(--page-gutter) * 2)));
            margin: 0 auto;
            background: transparent;
            padding: var(--container-padding);
            border-radius: 0;
            box-shadow: none;
            margin-top: 1rem;
            margin-bottom: 2rem;
            border: none;
            animation: sectionFadeIn .65s ease both;
        }}
        
        .header {{
            text-align: center;
            margin-bottom: 3rem;
            padding: 3rem 2rem;
            background: var(--gradient-primary);
            color: white;
            border-radius: var(--radius-xl);
            position: relative;
            overflow: hidden;
        }}
        
        .header::before {{
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background: url('data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><defs><pattern id="grain" width="100" height="100" patternUnits="userSpaceOnUse"><circle cx="25" cy="25" r="1" fill="white" opacity="0.1"/><circle cx="75" cy="75" r="1" fill="white" opacity="0.1"/><circle cx="50" cy="10" r="0.5" fill="white" opacity="0.1"/><circle cx="10" cy="60" r="0.5" fill="white" opacity="0.1"/><circle cx="90" cy="40" r="0.5" fill="white" opacity="0.1"/></pattern></defs><rect width="100" height="100" fill="url(%23grain)"/></svg>');
            opacity: 0.3;
        }}
        
        .header h1 {{
            font-size: 3rem;
            font-weight: 800;
            margin: 0 0 1rem 0;
            text-shadow: 0 2px 4px rgba(0, 0, 0, 0.1);
            position: relative;
            z-index: 1;
        }}
        
        .header p {{
            font-size: 1.125rem;
            opacity: 0.9;
            margin: 0;
            position: relative;
            z-index: 1;
        }}
        
        /* Темная тема */
        .dark-theme {{
            --bg-primary: #0f172a;
            --bg-secondary: #1e293b;
            --text-primary: #f1f5f9;
            --text-secondary: #cbd5e1;
            --border-color: #334155;
        }}
        
        .dark-theme body {{
            background: #0f172a !important;
            color: #f1f5f9 !important;
        }}
        .dark-theme body::before {{
            background: linear-gradient(180deg, #0b1329 0%, #0f172a 100%) !important;
        }}
        
        .dark-theme .main-content {{
            background: #0f172a !important;
        }}
        
        .dark-theme .container {{
            background: transparent;
            color: #f1f5f9;
        }}
        
        .dark-theme .metric {{
            background: #1e293b;
            border: 1px solid #334155;
            box-shadow: 0 4px 14px rgba(0,0,0,0.3);
        }}
        
        .dark-theme .hotels-section {{
            background: transparent;
            border: none;
        }}
        
        .dark-theme .hotels-table th {{
            background: transparent;
            color: #38bdf8;
            border: none;
        }}
        
        .dark-theme .hotels-table th:hover {{
            background: transparent;
            color: #7dd3fc;
        }}
        
        .dark-theme .hotels-table tbody tr {{
            background: #1e293b;
            border-color: #334155;
            box-shadow: 0 4px 12px rgba(0, 0, 0, 0.2);
        }}
        
        .dark-theme .hotels-table tbody tr:hover {{
            background: #1e293b;
            border-color: #38bdf8;
            box-shadow: 0 8px 24px rgba(56, 189, 248, 0.15);
        }}
        
        .dark-theme .airport {{
            background: #1e293b;
            color: #e2e8f0;
            border: 1px solid #475569;
        }}
        
        .dark-theme .airport-alt {{
            background: linear-gradient(135deg, #064e3b 0%, #065f46 100%);
            border: 1px solid #10b981;
            color: #ecfdf5;
        }}
        
        .dark-theme .airport-alt:hover {{
            background: linear-gradient(135deg, #065f46 0%, #047857 100%);
        }}
        
        .dark-theme .airport-alt small {{
            color: #6ee7b7;
        }}
        
        .dark-theme .filter-input,
        .dark-theme .filter-select {{
            background: #1e293b;
            border-color: #475569;
            color: #f1f5f9;
        }}
        
        .dark-theme .filter-input:focus,
        .dark-theme .filter-select:focus {{
            border-color: #3b82f6;
            box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.1);
        }}
        
        .dark-theme .sidebar {{
            background: linear-gradient(145deg, #1e293b 0%, #334155 100%);
        }}
        
        .dark-theme .nav-item {{
            color: #cbd5e1;
        }}
        
        .dark-theme .nav-item:hover {{
            background: #334155;
            color: #3b82f6;
        }}
        
        .dark-theme .nav-item.active {{
            background: linear-gradient(90deg, #1e40af 0%, #3b82f6 100%);
            color: white;
        }}
        
        .dark-theme .avg-top10-section {{
            background: linear-gradient(145deg, #1e293b 0%, #334155 100%);
            border: 1px solid #475569;
        }}
        
        .dark-theme .avg-top10-section h3 {{
            color: #f1f5f9;
        }}
        
        .dark-theme .trend-section {{
            background: linear-gradient(145deg, #1e293b 0%, #334155 100%);
            border: 1px solid #475569;
        }}
        
        .dark-theme .trend-section h3 {{
            color: #f1f5f9;
        }}
        
        .dark-theme .footer {{
            background: #1e293b;
            color: #cbd5e1;
        }}
        
        .dark-theme .pagination button {{
            background: #1e293b;
            border-color: #475569;
            color: #cbd5e1;
        }}
        
        .dark-theme .pagination button:hover:not(:disabled) {{
            background: var(--gradient-primary);
            color: white;
        }}
        
        .dark-theme .pagination button.active {{
            background: var(--gradient-primary);
            color: white;
        }}
        
        .dark-theme .pagination-info {{
            color: #cbd5e1;
        }}
        
        .theme-toggle {{
            position: static;
            background: var(--gradient-primary);
            border: none;
            border-radius: 50%;
            width: 2.25rem;
            height: 2.25rem;
            color: white;
            cursor: pointer;
            box-shadow: var(--shadow-sm);
            transition: var(--transition-normal);
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 1rem;
            flex-shrink: 0;
        }}
        
        .theme-toggle:hover {{
            transform: scale(1.1);
            box-shadow: var(--shadow-xl);
        }}
        .metrics {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
            gap: 1.5rem;
            margin-bottom: 3rem;
        }}
        
        .metric {{
            background: var(--gradient-card);
            padding: 2rem;
            border-radius: var(--radius-xl);
            text-align: center;
            box-shadow: var(--shadow-md);
            transition: var(--transition-normal);
            border: 1px solid var(--border-soft);
            position: relative;
            overflow: hidden;
            animation: floatSoft 6s ease-in-out infinite;
        }}
        
        .metric::before {{
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            right: 0;
            height: 4px;
            background: var(--gradient-primary);
        }}
        
        .metric:hover {{
            transform: translateY(-4px);
            box-shadow: var(--shadow-lg);
        }}
        
        .metric-value {{
            font-size: 2.5rem;
            font-weight: 800;
            background: var(--gradient-primary);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
            margin: 0.5rem 0;
        }}
        
        .metric-label {{
            font-size: 0.875rem;
            font-weight: 600;
            color: #6b7280;
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }}

        .metrics.metrics-compact {{
            grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
            gap: .6rem;
            margin-bottom: 1rem;
        }}
        .metrics.metrics-compact .metric {{
            padding: .62rem .78rem;
            border-radius: 12px;
            text-align: left;
            display: flex;
            align-items: center;
            justify-content: space-between;
            min-height: 54px;
            animation: none;
        }}
        .metrics.metrics-compact .metric::before {{
            height: 2px;
        }}
        .metrics.metrics-compact .metric:hover {{
            transform: translateY(-1px);
        }}
        .metrics.metrics-compact .metric-value {{
            font-size: 1.55rem;
            margin: 0;
            line-height: 1;
            white-space: nowrap;
            flex: 0 0 auto;
            margin-right: .7rem;
        }}
        .metrics.metrics-compact .metric > div:last-child,
        .metrics.metrics-compact .metric-label {{
            font-size: .77rem;
            font-weight: 600;
            color: #475569;
            line-height: 1.2;
        }}
        .metric.metric-tip {{
            cursor: help;
        }}
        .metric.metric-tip:hover {{
            border-color: rgba(79, 70, 229, 0.28);
            box-shadow: var(--shadow-md);
        }}
        
        .avg-top10-section {{
            background: var(--gradient-card);
            padding: 1.35rem;
            border-radius: var(--radius-xl);
            margin-top: var(--section-gap);
            margin-bottom: 1.1rem;
            box-shadow: var(--shadow-md);
            border: 1px solid var(--border-soft);
        }}
        
        .avg-top10-section h3 {{
            font-size: 1.5rem;
            font-weight: 700;
            margin: 0 0 1.5rem 0;
            color: #1f2937;
            display: flex;
            align-items: center;
            gap: 0.5rem;
        }}

        .chart-section-note {{
            margin: -0.85rem 0 1rem;
            font-size: 0.82rem;
            color: var(--text-muted);
            line-height: 1.4;
        }}
        
        .trend-section {{
            background: var(--gradient-card);
            padding: 2rem;
            border-radius: var(--radius-xl);
            margin-bottom: 3rem;
            box-shadow: var(--shadow-md);
            border: 1px solid var(--border-soft);
        }}
        
        .trend-index-section {{
            background: var(--gradient-card);
            padding: 1.25rem;
            border-radius: var(--radius-xl);
            margin-bottom: .75rem;
            box-shadow: var(--shadow-md);
            border: 1px solid var(--border-soft);
            border-top: 3px solid #7C3AED;
        }}
        details.dashboard-fold {{
            margin: 0 0 1rem 0;
            background: var(--gradient-card);
            border: 1px solid var(--border-soft);
            border-radius: var(--radius-lg);
            box-shadow: var(--shadow-sm);
            overflow: hidden;
        }}
        details.dashboard-fold > summary {{
            list-style: none;
            cursor: pointer;
            padding: .8rem 1rem;
            font-weight: 700;
            color: #1e293b;
            display: grid;
            grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr);
            align-items: center;
            gap: .75rem;
            user-select: none;
        }}
        details.dashboard-fold > summary::-webkit-details-marker {{
            display: none;
        }}
        details.dashboard-fold > summary > span:first-child {{
            justify-self: start;
            min-width: 0;
        }}
        .fold-title-meta {{
            justify-self: center;
            text-align: center;
            font-size: .78rem;
            color: var(--text-muted);
            font-weight: 500;
            white-space: nowrap;
        }}
        .fold-chevron {{
            justify-self: end;
            font-size: .85rem;
            opacity: .75;
            transition: transform .16s ease;
        }}
        details.dashboard-fold[open] .fold-chevron {{
            transform: rotate(180deg);
        }}
        .fold-content {{
            padding: 0 1rem .9rem;
        }}
        .fold-content .trend-index-section,
        .fold-content .metrics,
        .fold-content .changes-section,
        .fold-content .entry-signal {{
            margin-top: 0;
        }}
        .fold-content .metrics:last-child,
        .fold-content .entry-signal:last-child {{
            margin-bottom: 0;
        }}

        /* --- Секция «Когда покупать» --- */
        .timing-banner {{
            background: rgba(255,255,255,.55);
            border: 1px solid var(--border-soft);
            border-radius: 12px;
            padding: .7rem .9rem;
            margin: .2rem 0 .8rem;
        }}
        .timing-banner-row {{
            display: flex;
            justify-content: space-between;
            gap: 1rem;
            font-size: .85rem;
            padding: .15rem 0;
        }}
        .timing-banner-label {{ color: var(--text-muted); }}
        .timing-banner-value {{ font-weight: 700; color: #1e293b; }}
        .timing-reco {{
            background: linear-gradient(135deg, rgba(6,182,212,.14), rgba(99,102,241,.12));
            border: 1px solid rgba(6,182,212,.25);
            border-radius: 12px;
            padding: .7rem .9rem;
            font-size: .92rem;
            color: #0f172a;
            margin-bottom: .8rem;
            line-height: 1.45;
        }}
        .timing-badges {{
            display: flex;
            flex-wrap: wrap;
            gap: .5rem;
            margin-bottom: 1rem;
        }}
        .timing-badge {{
            display: flex;
            align-items: center;
            gap: .4rem;
            background: rgba(255,255,255,.6);
            border: 1px solid var(--border-soft);
            border-radius: 999px;
            padding: .3rem .6rem;
            font-size: .76rem;
        }}
        .timing-badge-name {{ font-weight: 700; color: #1e293b; }}
        .timing-badge-prog {{ color: var(--text-muted); }}
        .timing-pill {{
            font-weight: 700;
            border-radius: 999px;
            padding: .1rem .45rem;
            font-size: .72rem;
        }}
        .timing-pill-collecting {{ background: rgba(148,163,184,.25); color: #475569; }}
        .timing-pill-prelim {{ background: rgba(245,158,11,.22); color: #b45309; }}
        .timing-pill-reliable {{ background: rgba(16,185,129,.22); color: #047857; }}
        .timing-grid {{
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 1rem;
        }}
        .timing-chart-card {{
            background: rgba(255,255,255,.5);
            border: 1px solid var(--border-soft);
            border-radius: 12px;
            padding: .7rem .8rem;
        }}
        .timing-chart-card.timing-chart-wide {{ grid-column: 1 / -1; }}
        .timing-chart-card h4 {{
            margin: 0 0 .5rem;
            font-size: .92rem;
            color: #1e293b;
            display: flex;
            align-items: center;
            gap: .4rem;
        }}
        .timing-info-tag {{
            font-size: .68rem;
            font-weight: 700;
            background: rgba(148,163,184,.25);
            color: #475569;
            border-radius: 999px;
            padding: .08rem .4rem;
        }}
        .timing-hint {{
            margin: .5rem 0 0;
            font-size: .76rem;
            color: var(--text-muted);
            line-height: 1.4;
        }}
        @media (max-width: 768px) {{
            .timing-grid {{ grid-template-columns: 1fr; }}
        }}

        /* --- Секция "Выпавшие отели" --- */
        .vanished-hint {{
            margin: .2rem 0 .8rem;
            font-size: .82rem;
            color: var(--text-muted);
            line-height: 1.45;
        }}
        .vanished-table th, .vanished-table td {{
            vertical-align: top;
        }}
        .vanished-badge {{
            display: inline-block;
            margin-left: .4rem;
            font-size: .7rem;
            font-weight: 700;
            color: #b45309;
            background: rgba(245,158,11,.18);
            border-radius: 999px;
            padding: .08rem .45rem;
            white-space: nowrap;
        }}
        .vanished-reason {{
            display: inline-block;
            font-size: .76rem;
            font-weight: 600;
            border-radius: 8px;
            padding: .2rem .5rem;
            line-height: 1.3;
        }}
        .vanished-reason-sold {{ background: rgba(16,185,129,.16); color: #047857; }}
        .vanished-reason-up {{ background: rgba(239,68,68,.14); color: #b91c1c; }}
        .vanished-reason-flat {{ background: rgba(148,163,184,.2); color: #475569; }}
        
        .trend-section h3 {{
            font-size: 1.5rem;
            font-weight: 700;
            margin: 0 0 1.5rem 0;
            color: #1f2937;
            display: flex;
            align-items: center;
            gap: 0.5rem;
        }}
        
        .changes-section {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 20px;
            margin-bottom: 30px;
        }}
        .changes-block {{
            background: var(--surface-strong);
            padding: 20px;
            border-radius: var(--radius-md);
            border: 1px solid var(--border-soft);
            box-shadow: var(--shadow-sm);
        }}
        .changes-block h3 {{
            margin-top: 0;
            text-align: center;
        }}
        .change-item {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 10px;
            margin: 5px 0;
            background: rgba(255,255,255,0.95);
            border-radius: 8px;
            border-left: 4px solid;
        }}
        .change-decrease {{
            border-left-color: #28a745;
        }}
        .change-increase {{
            border-left-color: #dc3545;
        }}
        .change-price {{
            font-weight: bold;
        }}
        .change-percent {{
            font-size: 0.9em;
            opacity: 0.8;
        }}
        .entry-signal {{
            margin: 0 0 2rem 0;
            border-radius: var(--radius-lg);
            padding: 1rem;
            border: 1px solid var(--border-soft);
            box-shadow: var(--shadow-sm);
        }}
        .entry-high {{
            background: linear-gradient(135deg, rgba(16,185,129,.12), rgba(34,197,94,.08));
            border-color: rgba(16,185,129,.35);
        }}
        .entry-medium {{
            background: linear-gradient(135deg, rgba(245,158,11,.12), rgba(234,179,8,.08));
            border-color: rgba(245,158,11,.35);
        }}
        .entry-low {{
            background: linear-gradient(135deg, rgba(59,130,246,.10), rgba(14,165,233,.08));
            border-color: rgba(59,130,246,.28);
        }}
        .entry-title {{
            font-size: 1.05rem;
            font-weight: 700;
            margin-bottom: 4px;
        }}
        .entry-note {{
            color: var(--text-muted);
            font-size: .9rem;
            margin-bottom: 8px;
        }}
        .entry-stats {{
            font-size: .84rem;
            color: var(--text-muted);
            margin-bottom: 8px;
        }}
        .entry-list {{
            display: grid;
            gap: 8px;
        }}
        .entry-item {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: .65rem .75rem;
            border-radius: 10px;
            background: rgba(255,255,255,.78);
            border: 1px solid rgba(148,163,184,.20);
            transition: transform .16s ease;
        }}
        .entry-item:hover {{
            transform: translateY(-2px);
        }}
        .departures-strip {{
            margin: 0 0 1rem 0;
            padding: .9rem 1rem;
            border-radius: var(--radius-lg);
            background: linear-gradient(135deg, rgba(59,130,246,.11), rgba(14,165,233,.08));
            border: 1px solid rgba(59,130,246,.24);
            box-shadow: var(--shadow-sm);
        }}
        .departures-head {{
            display: flex;
            justify-content: space-between;
            gap: 1rem;
            align-items: flex-start;
            margin-bottom: .65rem;
        }}
        .departures-head h3 {{
            margin: 0;
            font-size: 1.05rem;
            font-weight: 800;
            color: #0f172a;
        }}
        .departures-head p {{
            margin: .18rem 0 0;
            font-size: .82rem;
            color: var(--text-muted);
        }}
        .departure-mini-stats {{
            display: flex;
            flex-wrap: wrap;
            justify-content: flex-end;
            gap: .35rem;
            max-width: 48%;
        }}
        .departure-mini-stats span {{
            font-size: .72rem;
            font-weight: 700;
            color: #1d4ed8;
            background: rgba(255,255,255,.7);
            border: 1px solid rgba(59,130,246,.18);
            border-radius: 999px;
            padding: .16rem .48rem;
            white-space: nowrap;
        }}
        .departure-legend {{
            margin: -.15rem 0 .65rem;
            color: var(--text-muted);
            font-size: .76rem;
            line-height: 1.35;
        }}
        .departure-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
            gap: .55rem;
        }}
        .departure-card {{
            display: flex;
            flex-direction: column;
            gap: .42rem;
            padding: .68rem .72rem;
            border-radius: 13px;
            background: rgba(255,255,255,.86);
            border: 1px solid rgba(148,163,184,.20);
            min-width: 0;
            box-shadow: 0 8px 20px rgba(15,23,42,.04);
        }}
        .departure-card.is-warm {{
            background: linear-gradient(135deg, rgba(255,247,237,.95), rgba(255,255,255,.82));
            border-color: rgba(245,158,11,.34);
        }}
        .departure-card.is-hot {{
            background: linear-gradient(135deg, rgba(254,226,226,.98), rgba(255,247,237,.88));
            border-color: rgba(239,68,68,.42);
            box-shadow: 0 10px 24px rgba(239,68,68,.12);
        }}
        .departure-card-head {{
            display: flex;
            align-items: flex-start;
            justify-content: space-between;
            gap: .5rem;
        }}
        .departure-title {{
            font-size: .98rem;
            font-weight: 800;
            color: #0f172a;
            white-space: normal;
            overflow: hidden;
            display: -webkit-box;
            -webkit-line-clamp: 2;
            -webkit-box-orient: vertical;
            line-height: 1.12;
        }}
        .departure-hub-resorts {{
            font-size: .68rem;
            color: var(--text-muted);
            line-height: 1.35;
            font-weight: 700;
        }}
        .departure-facts {{
            display: flex;
            flex-wrap: wrap;
            gap: .24rem;
        }}
        .departure-facts span {{
            display: inline-flex;
            align-items: center;
            border-radius: 999px;
            padding: .12rem .38rem;
            background: rgba(59,130,246,.08);
            color: #1e3a8a;
            font-size: .68rem;
            font-weight: 800;
            line-height: 1.25;
        }}
        .departure-delta {{
            display: flex;
            flex-wrap: wrap;
            gap: .25rem;
        }}
        .departure-change {{
            display: inline-flex;
            align-items: center;
            border-radius: 999px;
            padding: .14rem .4rem;
            font-size: .68rem;
            font-weight: 800;
            white-space: normal;
        }}
        .departure-change.drop {{
            color: #047857;
            background: rgba(16,185,129,.16);
        }}
        .departure-change.up {{
            color: #b91c1c;
            background: rgba(239,68,68,.14);
        }}
        .departure-change.muted {{
            color: var(--text-muted);
        }}
        .hero {{
            margin-bottom: 1.25rem;
            border-radius: var(--radius-xl);
            overflow: hidden;
            box-shadow: var(--shadow-lg);
            border: 1px solid var(--border-soft);
            background:
                linear-gradient(180deg, rgba(2,6,23,.18), rgba(2,6,23,.48)),
                url('https://images.unsplash.com/photo-1507525428034-b723cf961d3e?auto=format&fit=crop&w=1800&q=80') center/cover no-repeat;
            min-height: 180px;
            position: relative;
            animation: sectionFadeIn .7s ease both;
        }}
        .hero-content {{
            position: relative;
            z-index: 1;
            color: #fff;
            padding: 1.4rem 1.35rem;
        }}

        /* Mockup Hero Banner Styling matching Image 2 */
        .hero--mockup {{
            margin-bottom: 1.5rem;
            padding: 1.75rem;
            border-radius: 20px;
            background: linear-gradient(135deg, #e0f2fe 0%, #f0f9ff 50%, #e0f2fe 100%);
            border: 1px solid #bae6fd;
            box-shadow: 0 10px 30px rgba(2, 132, 199, 0.08);
        }}
        .dark-theme .hero--mockup {{
            background: linear-gradient(135deg, #0f172a 0%, #1e293b 100%);
            border-color: #334155;
            box-shadow: 0 10px 30px rgba(0, 0, 0, 0.3);
        }}

        .hero-header-mockup {{
            margin-bottom: 1.25rem;
        }}
        .hero-brand-tag {{
            font-size: 0.95rem;
            font-weight: 800;
            color: #0284c7;
            margin-bottom: 0.2rem;
            display: flex;
            align-items: center;
            gap: 6px;
        }}
        .dark-theme .hero-brand-tag {{
            color: #38bdf8;
        }}
        .hero-subtitle-tag {{
            font-size: 0.85rem;
            color: #64748b;
            margin-bottom: 0.4rem;
        }}
        .dark-theme .hero-subtitle-tag {{
            color: #94a3b8;
        }}
        .hero-title-mockup {{
            margin: 0;
            font-size: clamp(1.4rem, 2.5vw, 1.9rem);
            font-weight: 800;
            color: #0f172a;
        }}
        .dark-theme .hero-title-mockup {{
            color: #f8fafc;
        }}

        /* Floating White Search Control Bar */
        .search-pill-bar {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            background: #ffffff;
            border-radius: 16px;
            padding: 10px 16px;
            box-shadow: 0 4px 20px rgba(0, 0, 0, 0.05);
            border: 1px solid rgba(226, 232, 240, 0.8);
            margin-bottom: 1rem;
            gap: 8px;
            overflow-x: auto;
        }}
        .dark-theme .search-pill-bar {{
            background: #1e293b;
            border-color: #334155;
            box-shadow: 0 4px 20px rgba(0, 0, 0, 0.2);
        }}

        .search-pill-item {{
            display: flex;
            align-items: center;
            gap: 10px;
            padding: 4px 8px;
            flex: 1;
            min-width: max-content;
        }}
        .search-pill-icon {{
            font-size: 1.2rem;
        }}
        .search-pill-content {{
            display: flex;
            flex-direction: column;
        }}
        .search-pill-label {{
            font-size: 0.72rem;
            color: #64748b;
            font-weight: 600;
            display: flex;
            align-items: center;
            gap: 4px;
        }}
        .dark-theme .search-pill-label {{
            color: #94a3b8;
        }}
        .pill-arrow {{
            font-size: 0.65rem;
            opacity: 0.7;
        }}
        .search-pill-val {{
            font-size: 0.88rem;
            font-weight: 700;
            color: #0f172a;
            white-space: nowrap;
        }}
        .dark-theme .search-pill-val {{
            color: #f8fafc;
        }}

        .search-pill-divider {{
            width: 1px;
            height: 28px;
            background: #cbd5e1;
            flex-shrink: 0;
        }}
        .dark-theme .search-pill-divider {{
            background: #334155;
        }}

        /* 4 White KPI Cards */
        .kpi-cards-grid {{
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 14px;
        }}
        @media (max-width: 900px) {{
            .kpi-cards-grid {{
                grid-template-columns: repeat(2, 1fr);
            }}
        }}
        @media (max-width: 500px) {{
            .kpi-cards-grid {{
                grid-template-columns: 1fr;
            }}
        }}

        .kpi-card-white {{
            background: #ffffff;
            border-radius: 16px;
            padding: 14px 18px;
            box-shadow: 0 4px 16px rgba(0, 0, 0, 0.04);
            border: 1px solid rgba(226, 232, 240, 0.8);
            display: flex;
            align-items: center;
            gap: 14px;
        }}
        .dark-theme .kpi-card-white {{
            background: #1e293b;
            border-color: #334155;
            box-shadow: 0 4px 16px rgba(0, 0, 0, 0.2);
        }}

        .kpi-card-icon-box {{
            width: 44px;
            height: 44px;
            border-radius: 12px;
            display: flex;
            align-items: center;
            justify-content: center;
            flex-shrink: 0;
            background: #e0f2fe;
            color: #0284c7;
        }}
        .dark-theme .kpi-card-icon-box {{
            background: rgba(56, 189, 248, 0.15);
            color: #38bdf8;
        }}

        .kpi-svg {{
            width: 22px;
            height: 22px;
        }}

        .kpi-card-info {{
            display: flex;
            flex-direction: column;
            min-width: 0;
        }}
        .kpi-card-number {{
            font-size: 1.45rem;
            font-weight: 800;
            color: #0f172a;
            line-height: 1.1;
        }}
        .dark-theme .kpi-card-number {{
            color: #f8fafc;
        }}
        .kpi-card-title {{
            font-size: 0.78rem;
            font-weight: 600;
            color: #64748b;
            margin-top: 2px;
            white-space: nowrap;
        }}
        .dark-theme .kpi-card-title {{
            color: #94a3b8;
        }}

        .kpi-status-tag {{
            font-size: 0.75rem;
            font-weight: 700;
            color: #16a34a;
            display: flex;
            align-items: center;
            gap: 4px;
        }}
        .dark-theme .kpi-status-tag {{
            color: #4ade80;
        }}
        .departure-price-line {{
            display: flex;
            flex-wrap: wrap;
            gap: .38rem .65rem;
            border-radius: 10px;
            padding: .42rem .5rem;
            background: rgba(248,250,252,.9);
            border: 1px solid rgba(226,232,240,.8);
            color: var(--text-muted);
            font-size: .72rem;
            font-weight: 800;
        }}
        .departure-price-line strong {{
            color: #0f172a;
            font-weight: 900;
            font-size: .9rem;
        }}
        .departure-status {{
            flex: 0 0 auto;
            display: inline-flex;
            align-items: center;
            border-radius: 999px;
            padding: .22rem .5rem;
            font-size: .72rem;
            font-weight: 900;
            white-space: nowrap;
        }}
        .departure-status.hot {{ background: rgba(239,68,68,.16); color: #b91c1c; }}
        .departure-status.warm {{ background: rgba(245,158,11,.18); color: #b45309; }}
        .departure-status.calm {{ background: rgba(59,130,246,.14); color: #1d4ed8; }}
        .departure-score.hot {{ background: rgba(239,68,68,.16); color: #b91c1c; }}
        .departure-score.warm {{ background: rgba(245,158,11,.18); color: #b45309; }}
        .departure-score.calm {{ background: rgba(59,130,246,.14); color: #1d4ed8; }}
        .departure-score.mini {{
            width: 30px;
            height: 30px;
            font-size: .78rem;
        }}
        .departure-foot {{
            display: flex;
            justify-content: flex-end;
            font-size: .66rem;
            font-weight: 700;
            color: var(--text-muted);
        }}
        .departure-history-hint {{
            margin: .2rem 0 .8rem;
            font-size: .82rem;
            color: var(--text-muted);
            line-height: 1.45;
        }}
        .departure-history-table th,
        .departure-history-table td {{
            vertical-align: top;
        }}
        .departure-history-table td span {{
            color: var(--text-muted);
            font-size: .78rem;
        }}
        .departure-card-clickable,
        .departure-history-row {{
            cursor: pointer;
            transition: transform var(--transition-fast), box-shadow var(--transition-fast), border-color var(--transition-fast);
        }}
        .departure-card-clickable:hover,
        .departure-card-clickable:focus-visible,
        .departure-history-row:hover,
        .departure-history-row:focus-visible {{
            transform: translateY(-1px);
            box-shadow: 0 12px 28px rgba(15,23,42,.10);
            outline: none;
        }}
        .departure-card-clickable:focus-visible,
        .departure-history-row:focus-visible {{
            border-color: rgba(79,70,229,.45);
            box-shadow: 0 0 0 3px rgba(79,70,229,.18);
        }}
        .departure-history-row:hover td {{
            background: rgba(79,70,229,.04);
        }}
        .departure-modal {{
            position: fixed;
            inset: 0;
            z-index: 1200;
            display: none;
            align-items: center;
            justify-content: center;
            padding: 1rem;
        }}
        .departure-modal.open {{
            display: flex;
        }}
        .departure-modal-backdrop {{
            position: absolute;
            inset: 0;
            background: rgba(15,23,42,.58);
            backdrop-filter: blur(2px);
        }}
        .departure-modal-dialog {{
            position: relative;
            width: min(920px, 100%);
            max-height: min(82vh, 860px);
            display: flex;
            flex-direction: column;
            background: #fff;
            border-radius: 16px;
            box-shadow: var(--shadow-xl);
            overflow: hidden;
        }}
        .departure-modal-header {{
            display: flex;
            align-items: flex-start;
            justify-content: space-between;
            gap: .75rem;
            padding: 1rem 1.1rem .55rem;
            border-bottom: 1px solid var(--border-soft);
        }}
        .departure-modal-header h3 {{
            margin: 0;
            font-size: 1.05rem;
            line-height: 1.25;
        }}
        .departure-modal-close {{
            border: 0;
            background: rgba(148,163,184,.16);
            color: #334155;
            width: 34px;
            height: 34px;
            border-radius: 10px;
            font-size: 1.35rem;
            line-height: 1;
            cursor: pointer;
        }}
        .departure-modal-meta {{
            margin: 0;
            padding: 0 1.1rem .75rem;
            color: var(--text-muted);
            font-size: .82rem;
        }}
        .departure-modal-chart-title {{
            margin: 0;
            padding: 0 1.1rem .35rem;
            color: var(--text-muted);
            font-size: .82rem;
            font-weight: 600;
        }}
        .departure-modal-chart {{
            display: none;
            flex-shrink: 0;
            width: calc(100% - 2.2rem);
            height: 300px;
            min-height: 300px;
            margin: 0 1.1rem 12px;
            border: 1px solid rgba(226,232,240,.95);
            border-radius: 12px;
            background: #f8fafc;
            overflow: visible;
        }}
        .departure-modal-body {{
            flex: 1 1 auto;
            min-height: 0;
            overflow: auto;
            padding: 0 1.1rem 1rem;
            -webkit-overflow-scrolling: touch;
        }}
        .departure-modal-table-scroll {{
            overflow-x: auto;
            -webkit-overflow-scrolling: touch;
            max-width: 100%;
            margin: 0 -.15rem;
            padding: 0 .15rem .15rem;
        }}
        .departure-offers-table {{
            width: 100%;
            min-width: 520px;
            border-collapse: collapse;
        }}
        .departure-offers-table th,
        .departure-offers-table td {{
            padding: .55rem .45rem;
            border-bottom: 1px solid rgba(226,232,240,.9);
            text-align: left;
            vertical-align: middle;
            font-size: .86rem;
        }}
        .departure-offers-table th {{
            position: sticky;
            top: 0;
            background: #fff;
            z-index: 1;
            font-size: .74rem;
            text-transform: uppercase;
            letter-spacing: .03em;
            color: var(--text-muted);
        }}
        .departure-offers-table td.price {{
            font-weight: 800;
            white-space: nowrap;
        }}
        .departure-offers-table td.delta-drop {{
            color: #047857;
            font-weight: 800;
            white-space: nowrap;
        }}
        .departure-offers-table td.delta-up {{
            color: #b91c1c;
            font-weight: 800;
            white-space: nowrap;
        }}
        .departure-offers-table td.delta-flat {{
            color: var(--text-muted);
            white-space: nowrap;
        }}
        .departure-offers-table .deal-pill {{
            display: inline-flex;
            align-items: center;
            border-radius: 999px;
            padding: .18rem .48rem;
            font-size: .72rem;
            font-weight: 800;
            border: 1px solid transparent;
            white-space: nowrap;
        }}
        .departure-offers-table .deal-pill.hot {{ background: rgba(245,158,11,.18); color: #92400e; border-color: rgba(245,158,11,.32); }}
        .departure-offers-table .deal-pill.good {{ background: rgba(16,185,129,.17); color: #065f46; border-color: rgba(16,185,129,.32); }}
        .departure-offers-table .deal-pill.normal {{ background: rgba(148,163,184,.18); color: #334155; border-color: rgba(148,163,184,.35); }}
        .departure-offers-table .deal-pill.bad {{ background: rgba(239,68,68,.15); color: #991b1b; border-color: rgba(239,68,68,.32); }}
        .departure-offers-table .deal-pill.warm {{ background: rgba(14,165,233,.16); color: #0c4a6e; border-color: rgba(14,165,233,.35); }}
        .departure-offers-link {{
            display: inline-flex;
            align-items: center;
            justify-content: center;
            padding: .32rem .62rem;
            border-radius: 999px;
            background: var(--gradient-primary);
            color: #fff;
            text-decoration: none;
            font-size: .76rem;
            font-weight: 700;
            white-space: nowrap;
        }}
        .departure-offers-actions {{
            display: flex;
            flex-wrap: wrap;
            gap: .35rem;
        }}
        .departure-offers-link.secondary {{
            background: #fff;
            color: #4f46e5;
            border: 1px solid rgba(79,70,229,.28);
        }}
        .departure-modal-empty {{
            padding: 1rem 0;
            color: var(--text-muted);
            font-size: .88rem;
        }}
        @media (max-width: 760px) {{
            .departures-head {{ display: block; }}
            .departure-mini-stats {{ max-width: none; justify-content: flex-start; margin-top: .55rem; }}
            .departure-grid {{ grid-template-columns: 1fr; }}
        }}
        .deal-legend {{
            margin: 0 0 1rem 0;
            padding: .85rem 1rem;
            border-radius: 10px;
            background: rgba(255,255,255,.82);
            border: 1px solid var(--border-soft);
            color: var(--text-muted);
            font-size: .88rem;
            line-height: 1.45;
        }}
        .deal-badge-hot {{ color: #b45309; font-weight: 700; }}
        .deal-badge-good {{ color: #166534; font-weight: 700; }}
        .deal-badge-normal {{ color: #64748b; font-weight: 600; }}
        .deal-badge-bad {{ color: #b91c1c; font-weight: 700; }}
        .deal-badge-warm {{ color: #0369a1; font-weight: 700; }}
        .hero {{
            margin-bottom: 1.25rem;
            border-radius: var(--radius-xl);
            overflow: hidden;
            box-shadow: var(--shadow-lg);
            border: 1px solid var(--border-soft);
            background:
                linear-gradient(180deg, rgba(2,6,23,.18), rgba(2,6,23,.48)),
                url('https://images.unsplash.com/photo-1507525428034-b723cf961d3e?auto=format&fit=crop&w=1800&q=80') center/cover no-repeat;
            min-height: 180px;
            position: relative;
            animation: sectionFadeIn .7s ease both;
        }}
        .hero-content {{
            position: relative;
            z-index: 1;
            color: #fff;
            padding: 1.4rem 1.35rem;
        }}
        .hero-kpis {{
            display: grid;
            grid-template-columns: repeat(4, minmax(0, 1fr));
            gap: 12px;
            margin-top: 1rem;
        }}
        .hero-kpi {{
            display: flex;
            align-items: flex-start;
            gap: .65rem;
            background: rgba(255,255,255,.14);
            border: 1px solid rgba(255,255,255,.26);
            border-radius: 14px;
            padding: .7rem .75rem;
            backdrop-filter: blur(10px);
            box-shadow: 0 4px 18px rgba(2,6,23,.12);
        }}
        .hero-kpi__badge {{
            flex-shrink: 0;
            width: 2.35rem;
            height: 2.35rem;
            border-radius: 10px;
            display: flex;
            align-items: center;
            justify-content: center;
            color: #fff;
        }}
        .hero-kpi__badge--blue {{ background: linear-gradient(145deg, #3b82f6, #2563eb); }}
        .hero-kpi__badge--green {{ background: linear-gradient(145deg, #22c55e, #16a34a); }}
        .hero-kpi__badge--purple {{ background: linear-gradient(145deg, #a855f7, #7c3aed); }}
        .hero-kpi__badge--gold {{ background: linear-gradient(145deg, #fbbf24, #d97706); }}
        .hero-kpi__svg {{
            width: 1.15rem;
            height: 1.15rem;
        }}
        .hero-kpi__body {{
            min-width: 0;
            flex: 1;
        }}
        .hero-kpi__v {{
            font-size: 1.35rem;
            font-weight: 800;
            line-height: 1.1;
            letter-spacing: -.02em;
            min-width: 0;
            overflow-wrap: anywhere;
        }}
        .hero-kpi__datetime {{
            display: flex;
            flex-direction: column;
            gap: .08rem;
            line-height: 1.15;
        }}
        .hero-kpi__date {{
            font-size: clamp(.82rem, 3vw, 1.1rem);
            font-weight: 800;
            white-space: nowrap;
        }}
        .hero-kpi__time {{
            font-size: clamp(.74rem, 2.6vw, .92rem);
            font-weight: 700;
            opacity: .9;
        }}
        .hero-kpi__l {{
            margin-top: .12rem;
            font-size: .74rem;
            font-weight: 700;
            opacity: .95;
        }}
        .hero-kpi__s {{
            margin-top: .18rem;
            font-size: .66rem;
            line-height: 1.3;
            opacity: .82;
        }}
        /* —— Premium filter bar (mockup layout) —— */
        .filter-bar--premium {{
            margin-top: .55rem;
            padding: .7rem .85rem .75rem;
            background: rgba(15,23,42,.42);
            border: 1px solid rgba(255,255,255,.22);
            border-radius: 16px;
            backdrop-filter: blur(14px);
            box-shadow: 0 8px 28px rgba(2,6,23,.18);
            animation: filterBarIn .45s ease both;
        }}
        @keyframes filterBarIn {{
            from {{ opacity: 0; transform: translateY(6px); }}
            to {{ opacity: 1; transform: translateY(0); }}
        }}
        .filter-bar__track {{
            display: flex;
            align-items: flex-end;
            justify-content: space-between;
            gap: .35rem;
            width: 100%;
            overflow-x: auto;
            scrollbar-width: thin;
        }}
        .duration-global-switch {{
            margin-top: .65rem;
            display: flex;
            align-items: center;
            gap: .75rem;
            flex-wrap: wrap;
            padding: .55rem .7rem;
            border-radius: 14px;
            background: rgba(15,23,42,.34);
            border: 1px solid rgba(255,255,255,.18);
        }}
        .duration-global-label {{
            font-size: .78rem;
            font-weight: 700;
            letter-spacing: .04em;
            text-transform: uppercase;
            opacity: .9;
        }}
        .duration-global-options {{
            display: flex;
            flex-wrap: wrap;
            gap: .45rem;
        }}
        .duration-global-btn {{
            border: 1px solid rgba(255,255,255,.24);
            background: rgba(255,255,255,.08);
            color: inherit;
            border-radius: 999px;
            padding: .42rem .9rem;
            font-size: .82rem;
            font-weight: 700;
            cursor: pointer;
            transition: background .15s ease, border-color .15s ease, transform .15s ease;
        }}
        .duration-global-btn:hover {{
            background: rgba(255,255,255,.16);
        }}
        .duration-global-btn.active {{
            background: linear-gradient(135deg, #f59e0b 0%, #fbbf24 100%);
            color: #1f2937;
            border-color: transparent;
            box-shadow: 0 4px 14px rgba(245,158,11,.35);
        }}
        .fb-route-group {{
            display: flex;
            align-items: flex-end;
            gap: .45rem;
            flex-shrink: 0;
        }}
        .fb-route-arrow {{
            font-size: 1.1rem;
            opacity: .55;
            padding-bottom: 1.1rem;
            flex-shrink: 0;
        }}
        .fb-slot {{
            display: flex;
            flex-direction: column;
            align-items: center;
            gap: .35rem;
            min-width: 4.5rem;
            max-width: 7.5rem;
            flex-shrink: 0;
            text-align: center;
        }}
        .fb-badge {{
            width: 2.5rem;
            height: 2.5rem;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            flex-shrink: 0;
            box-shadow: 0 4px 14px rgba(0,0,0,.2);
        }}
        .fb-badge--blue {{
            background: linear-gradient(145deg, #3b82f6, #1d4ed8);
            color: #fff;
        }}
        .fb-badge--gold {{
            background: linear-gradient(145deg, #fbbf24, #d97706);
            color: #fff;
        }}
        .fb-badge--neutral {{
            background: rgba(255,255,255,.16);
            border: 1px solid rgba(255,255,255,.22);
            color: #fff;
        }}
        .fb-badge--flag {{
            background: rgba(255,255,255,.12);
            border: 1px solid rgba(255,255,255,.2);
        }}
        .fb-flag-emoji {{
            font-size: 1.35rem;
            line-height: 1;
        }}
        .fb-svg {{
            width: 1.15rem;
            height: 1.15rem;
        }}
        .fb-slot-body {{
            display: flex;
            flex-direction: column;
            gap: .1rem;
            width: 100%;
            min-width: 0;
        }}
        .fb-kicker {{
            font-size: .58rem;
            font-weight: 700;
            letter-spacing: .09em;
            text-transform: uppercase;
            opacity: .72;
            line-height: 1.1;
        }}
        .fb-value {{
            font-size: .78rem;
            font-weight: 800;
            line-height: 1.2;
            color: #fff;
            max-width: 100%;
            overflow-wrap: anywhere;
            word-break: break-word;
        }}
        .fb-value--gold {{
            color: #fde68a;
        }}
        .fb-chev {{
            display: inline-block;
            margin-left: .15rem;
            font-size: .62rem;
            opacity: .75;
            vertical-align: middle;
        }}
        .fb-sub {{
            font-size: .6rem;
            opacity: .78;
            line-height: 1.15;
            font-weight: 500;
        }}
        .fb-vdiv {{
            width: 1px;
            align-self: stretch;
            min-height: 3.2rem;
            margin: 0 .15rem;
            background: linear-gradient(180deg, transparent, rgba(255,255,255,.32), transparent);
            flex-shrink: 0;
        }}
        @media (max-width: 1100px) {{
            .filter-bar__track {{
                justify-content: flex-start;
            }}
        }}
        @media (max-width: 720px) {{
            .hero-kpis {{
                grid-template-columns: 1fr 1fr;
            }}
            .fb-vdiv {{
                display: none;
            }}
            .filter-bar__track {{
                flex-wrap: wrap;
                row-gap: .65rem;
            }}
            .fb-kicker {{
                font-size: .68rem;
            }}
            .fb-sub {{
                font-size: .72rem;
            }}
            .fb-value {{
                font-size: .84rem;
            }}
            .duration-global-switch {{
                flex-direction: column;
                align-items: stretch;
                gap: .55rem;
            }}
            .duration-global-options {{
                width: 100%;
            }}
            .duration-global-btn {{
                flex: 1;
                text-align: center;
                min-width: 0;
            }}
        }}
        .mode-switch {{
            display: inline-flex;
            position: relative;
            border: none;
            border-radius: 9px;
            margin: .35rem 0 .85rem;
            background: rgba(118,118,128,.14);
            padding: 2px;
            isolation: isolate;
            touch-action: manipulation;
            -webkit-tap-highlight-color: transparent;
        }}
        .mode-switch::before {{
            content: "";
            position: absolute;
            top: 2px;
            bottom: 2px;
            left: 2px;
            width: calc(50% - 2px);
            border-radius: 7px;
            background: #fff;
            box-shadow: 0 1px 3px rgba(0,0,0,.14), 0 1px 1px rgba(0,0,0,.05);
            transition: transform .24s cubic-bezier(.4,0,.2,1);
            transform: translateX(0);
            z-index: 0;
            pointer-events: none;
        }}
        .mode-switch[data-mode="table"]::before {{
            transform: translateX(100%);
        }}
        .table-toolbar {{
            display: inline-flex;
            align-items: center;
            gap: .5rem;
            margin-left: auto;
        }}
        .table-toolbar-title {{
            font-size: .76rem;
            color: var(--text-muted);
            font-weight: 600;
            white-space: nowrap;
        }}
        .table-mode-switch {{
            margin: 0;
            transform: scale(.92);
            transform-origin: right center;
        }}
        .table-header-row {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: .8rem;
            margin-bottom: .65rem;
        }}
        .table-header-row h3 {{
            margin: 0;
        }}
        .mode-btn {{
            position: relative;
            z-index: 1;
            flex: 1;
            border: none;
            background: transparent;
            padding: .42rem 1.05rem;
            font-size: .85rem;
            cursor: pointer;
            color: #6b7280;
            font-weight: 600;
            white-space: nowrap;
            text-align: center;
            transition: color .18s ease;
        }}
        .mode-btn.active {{
            color: #111827;
            background: transparent;
            box-shadow: none;
        }}
        .dark-theme .mode-switch {{
            background: rgba(255,255,255,.12);
        }}
        .dark-theme .mode-btn {{
            color: #cbd5e1;
        }}
        .dark-theme .mode-btn.active {{
            color: #0f172a;
        }}
        .cards-section {{
            margin: 0 0 1.2rem 0;
        }}
        .cards-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(260px,1fr));
            gap: 12px;
        }}
        .cards-pagination {{
            margin-top: .85rem;
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: .65rem;
            flex-wrap: wrap;
        }}
        .cards-pagination-info {{
            font-size: .84rem;
            color: var(--text-muted);
        }}
        .cards-pagination button {{
            padding: .48rem .85rem;
            border-radius: 999px;
            border: 1px solid var(--border-soft);
            background: rgba(255,255,255,.9);
            color: #1e293b;
            cursor: pointer;
            font-weight: 600;
        }}
        .cards-pagination button:disabled {{
            opacity: .45;
            cursor: not-allowed;
        }}
        .hotel-card {{
            background: rgba(255,255,255,.88);
            border: 1px solid var(--border-soft);
            border-radius: 14px;
            overflow: hidden;
            box-shadow: var(--shadow-sm);
            transition: transform .16s ease, box-shadow .16s ease, border-color .16s ease;
            animation: sectionFadeIn .45s ease both;
            display: flex;
            flex-direction: column;
            height: 100%;
        }}
        .hotel-card:hover {{
            transform: translateY(-2px);
            box-shadow: var(--shadow-md);
            border-color: rgba(79,70,229,.28);
        }}
        .hotel-card-img {{
            height: 120px;
            background: linear-gradient(135deg, rgba(79,70,229,.16), rgba(14,165,233,.16));
            display: flex;
            align-items: center;
            justify-content: center;
            color: #334155;
            font-size: .9rem;
        }}
        .hotel-card-img img {{
            width: 100%;
            height: 100%;
            object-fit: cover;
            display: block;
        }}
        .hotel-card-body {{
            padding: .72rem .8rem .82rem;
            flex: 1;
            display: flex;
            flex-direction: column;
        }}
        .hotel-card-title {{
            margin: 0 0 .45rem 0;
            font-size: .96rem;
            font-weight: 700;
            color: #1e293b;
            line-height: 1.25;
            min-height: 2.5em;
        }}
        .hotel-card-meta {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: .5rem;
        }}
        .hotel-card-price {{
            font-size: 1.12rem;
            font-weight: 800;
            color: #111827;
        }}
        .deal-pill {{
            border-radius: 999px;
            padding: .18rem .5rem;
            font-size: .72rem;
            font-weight: 700;
            border: 1px solid transparent;
            white-space: nowrap;
        }}
        .deal-pill.hot {{ background: rgba(245,158,11,.18); color: #92400e; border-color: rgba(245,158,11,.32); }}
        .deal-pill.good {{ background: rgba(16,185,129,.17); color: #065f46; border-color: rgba(16,185,129,.32); }}
        .deal-pill.normal {{ background: rgba(148,163,184,.18); color: #334155; border-color: rgba(148,163,184,.35); }}
        .deal-pill.bad {{ background: rgba(239,68,68,.15); color: #991b1b; border-color: rgba(239,68,68,.32); }}
        .deal-pill.warm {{ background: rgba(14,165,233,.16); color: #0c4a6e; border-color: rgba(14,165,233,.35); }}
        .comeback-badge {{
            display: inline-block;
            margin-top: .25rem;
            font-size: .72rem;
            font-weight: 700;
            color: #065f46;
            background: rgba(16,185,129,.14);
            border: 1px solid rgba(16,185,129,.28);
            border-radius: 999px;
            padding: .12rem .45rem;
        }}
        .cheaper-alt-badge {{
            display: inline-flex;
            flex-wrap: wrap;
            align-items: center;
            gap: 1px 4px;
            white-space: normal;
            margin-top: 3px;
            font-size: 0.68rem;
            line-height: 1.25;
            color: #475569;
            background: #f0f9ff;
            border: 1px solid #bae6fd;
            border-radius: 6px;
            padding: 2px 6px;
            text-decoration: none;
            max-width: 100%;
            transition: all 0.15s ease;
        }}
        .cheaper-alt-badge:hover {{
            background: #e0f2fe;
            border-color: #7dd3fc;
            color: #0369a1;
            text-decoration: none;
        }}
        .cheaper-alt-badge .alt-label {{
            color: #475569;
            font-weight: 600;
            white-space: nowrap;
        }}
        .cheaper-alt-badge .alt-savings {{
            color: #059669;
            font-weight: 700;
            white-space: nowrap;
        }}
        .dark-theme .cheaper-alt-badge {{
            background: rgba(56, 189, 248, 0.1);
            border-color: #334155;
            color: #cbd5e1;
        }}
        .dark-theme .cheaper-alt-badge .alt-label {{
            color: #cbd5e1;
        }}
        .dark-theme .cheaper-alt-badge .alt-savings {{
            color: #34d399;
        }}
        .hotel-card-stats {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 6px;
            margin-bottom: .6rem;
            color: #475569;
            font-size: .79rem;
        }}
        .hotel-card-actions {{
            display: flex;
            gap: 8px;
            margin-top: auto;
            padding-top: .6rem;
        }}
        .card-btn {{
            flex: 1;
            text-align: center;
            text-decoration: none;
            border-radius: 10px;
            padding: .45rem .5rem;
            font-size: .8rem;
            font-weight: 700;
            border: 1px solid var(--border-soft);
            color: #1e293b;
            background: rgba(255,255,255,.92);
            transition: transform .14s ease, box-shadow .14s ease;
        }}
        .card-btn.primary {{
            background: var(--gradient-primary);
            color: #fff;
            border-color: transparent;
        }}
        .card-btn:hover {{
            transform: translateY(-1px);
            box-shadow: var(--shadow-sm);
        }}
        .alerts-section {{
            margin-top: var(--section-gap);
            margin-bottom: var(--section-gap);
            background: var(--gradient-card);
            border-radius: var(--radius-xl);
            box-shadow: var(--shadow-md);
            border: 1px solid var(--border-soft);
            overflow: hidden;
        }}
        .alerts-header {{
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            gap: 1rem;
            cursor: pointer;
            user-select: none;
            padding: 1rem var(--section-inner-x);
            border-bottom: 1px solid var(--border-soft);
        }}
        .alerts-header:hover {{
            background: rgba(248,250,252,.85);
        }}
        .alerts-header-main {{
            flex: 1;
            min-width: 0;
        }}
        .alerts-header h3 {{
            margin: 0 0 .35rem 0;
            font-size: 1.12rem;
            color: #1e293b;
        }}
        .alerts-lead {{
            margin: 0 0 .55rem 0;
            font-size: .84rem;
            line-height: 1.45;
            color: var(--text-muted);
            max-width: 62rem;
        }}
        .alerts-summary-chips {{
            display: flex;
            flex-wrap: wrap;
            gap: .45rem;
        }}
        .alert-chip {{
            display: inline-flex;
            align-items: center;
            border-radius: 999px;
            padding: .18rem .55rem;
            font-size: .74rem;
            font-weight: 700;
            border: 1px solid transparent;
        }}
        .alert-chip.drop {{
            background: rgba(16,185,129,.14);
            color: #065f46;
            border-color: rgba(16,185,129,.28);
        }}
        .alert-chip.up {{
            background: rgba(239,68,68,.12);
            color: #991b1b;
            border-color: rgba(239,68,68,.26);
        }}
        .alert-chip.missing {{
            background: rgba(100,116,139,.14);
            color: #334155;
            border-color: rgba(100,116,139,.28);
        }}
        .alerts-content {{
            max-height: 680px;
            overflow-y: auto;
            transition: max-height 0.3s ease;
            background: rgba(248,250,252,.55);
        }}
        .alerts-content.collapsed {{
            max-height: 0;
            overflow: hidden;
            padding: 0;
        }}
        .alerts-section-label {{
            margin: 0;
            padding: .85rem var(--section-inner-x) .35rem;
            font-size: .78rem;
            font-weight: 700;
            color: var(--text-muted);
            text-transform: uppercase;
            letter-spacing: .04em;
        }}
        .alerts-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
            gap: 8px;
            padding: 0 var(--section-inner-x) .85rem;
        }}
        .alert-card {{
            display: flex;
            flex-direction: row;
            align-items: stretch;
            height: 100%;
            background: rgba(255,255,255,.95);
            border: 1px solid var(--border-soft);
            border-radius: 10px;
            overflow: hidden;
            box-shadow: var(--shadow-sm);
            transition: transform .14s ease, box-shadow .14s ease, border-color .14s ease;
        }}
        .alert-card:hover {{
            transform: translateY(-1px);
            box-shadow: var(--shadow-md);
        }}
        .alert-card.drop {{
            border-left: 3px solid #10b981;
        }}
        .alert-card.up {{
            border-left: 3px solid #ef4444;
        }}
        .alert-card.missing {{
            border-left: 3px solid #64748b;
        }}
        .alert-card-img {{
            width: 62px;
            flex-shrink: 0;
            background: linear-gradient(135deg, rgba(79,70,229,.12), rgba(14,165,233,.12));
            display: flex;
            align-items: center;
            justify-content: center;
            color: #64748b;
            font-size: .75rem;
        }}
        .alert-card-img img {{
            width: 100%;
            height: 100%;
            object-fit: cover;
            display: block;
        }}
        .alert-card-body {{
            padding: .45rem .5rem .5rem;
            display: flex;
            flex-direction: column;
            flex: 1;
            min-width: 0;
        }}
        .alert-card-top {{
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            gap: .3rem;
            margin-bottom: .25rem;
        }}
        .alert-badge {{
            border-radius: 999px;
            padding: .1rem .38rem;
            font-size: .62rem;
            font-weight: 800;
            white-space: nowrap;
            flex: 0 0 auto;
            line-height: 1.2;
        }}
        .alert-badge.drop {{ background: rgba(16,185,129,.16); color: #065f46; }}
        .alert-badge.up {{ background: rgba(239,68,68,.14); color: #991b1b; }}
        .alert-badge.missing {{ background: rgba(100,116,139,.16); color: #334155; }}
        .alert-card-title {{
            margin: 0;
            font-size: .78rem;
            font-weight: 700;
            line-height: 1.2;
            min-height: 2.4em;
        }}
        .alert-card-title a {{
            color: #1e293b;
            text-decoration: none;
        }}
        .alert-card-title a:hover {{
            color: #4f46e5;
            text-decoration: underline;
        }}
        .alert-price-row {{
            display: flex;
            align-items: baseline;
            flex-wrap: wrap;
            gap: .25rem;
            margin-bottom: .2rem;
        }}
        .alert-price-old {{
            font-size: .72rem;
            color: #64748b;
            text-decoration: line-through;
        }}
        .alert-price-new {{
            font-size: .88rem;
            font-weight: 800;
            color: #111827;
        }}
        .alert-price-new.up {{
            color: #b91c1c;
        }}
        .alert-price-new.drop {{
            color: #047857;
        }}
        .alert-change-pct {{
            font-size: .72rem;
            font-weight: 800;
        }}
        .alert-change-pct.drop {{ color: #047857; }}
        .alert-change-pct.up {{ color: #b91c1c; }}
        .alert-card-meta {{
            font-size: .68rem;
            color: #64748b;
            line-height: 1.25;
            margin-bottom: .35rem;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }}
        .alert-card-actions {{
            display: flex;
            gap: 5px;
            margin-top: auto;
        }}
        .alert-card-actions .card-btn {{
            flex: 1;
            font-size: .68rem;
            padding: .28rem .35rem;
            border-radius: 8px;
        }}
        .alerts-empty {{
            color: #64748b;
            font-style: italic;
            padding: 1rem var(--section-inner-x) .5rem;
            font-size: .84rem;
        }}
        .alerts-history-fold {{
            margin: 0 var(--section-inner-x) .85rem;
            border: 1px solid var(--border-soft);
            border-radius: 10px;
            background: rgba(255,255,255,.72);
            overflow: hidden;
        }}
        .alerts-history-fold > summary {{
            list-style: none;
            cursor: pointer;
            padding: .55rem .75rem;
            font-size: .82rem;
            font-weight: 700;
            color: #475569;
            user-select: none;
        }}
        .alerts-history-fold > summary::-webkit-details-marker {{
            display: none;
        }}
        .alerts-history-fold > summary:hover {{
            background: rgba(248,250,252,.9);
        }}
        .alert-history-list {{
            border-top: 1px solid var(--border-soft);
            max-height: 320px;
            overflow-y: auto;
        }}
        .alert-history-row {{
            display: grid;
            grid-template-columns: auto minmax(0, 1fr) auto auto;
            align-items: center;
            gap: .55rem .65rem;
            padding: .45rem .75rem;
            border-bottom: 1px solid rgba(148,163,184,.16);
            font-size: .78rem;
            background: rgba(255,255,255,.82);
        }}
        .alert-history-row:last-child {{
            border-bottom: none;
        }}
        .alert-history-row.drop {{ border-left: 3px solid #10b981; }}
        .alert-history-row.up {{ border-left: 3px solid #ef4444; }}
        .alert-history-row.missing {{ border-left: 3px solid #64748b; }}
        .alert-history-badge {{
            width: 1.35rem;
            text-align: center;
            font-weight: 800;
            font-size: .82rem;
            flex-shrink: 0;
        }}
        .alert-history-badge.drop {{ color: #047857; }}
        .alert-history-badge.up {{ color: #b91c1c; }}
        .alert-history-badge.missing {{ color: #64748b; }}
        .alert-history-info {{
            min-width: 0;
        }}
        .alert-history-name {{
            display: block;
            font-weight: 700;
            color: #1e293b;
            text-decoration: none;
            line-height: 1.25;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }}
        .alert-history-name:hover {{
            color: #4f46e5;
            text-decoration: underline;
        }}
        .alert-history-sub {{
            display: block;
            margin-top: .12rem;
            font-size: .68rem;
            color: #64748b;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }}
        .alert-history-price {{
            white-space: nowrap;
            text-align: right;
            font-size: .76rem;
        }}
        .alert-history-old {{
            color: #94a3b8;
            text-decoration: line-through;
        }}
        .alert-history-arrow {{
            opacity: .55;
            margin: 0 .12rem;
        }}
        .alert-history-new {{
            font-weight: 800;
            color: #111827;
        }}
        .alert-history-new.drop {{ color: #047857; }}
        .alert-history-new.up {{ color: #b91c1c; }}
        .alert-history-pct {{
            margin-left: .35rem;
            font-weight: 800;
            font-size: .72rem;
        }}
        .alert-history-pct.drop {{ color: #047857; }}
        .alert-history-pct.up {{ color: #b91c1c; }}
        .alert-history-chart,
        .alert-history-offer {{
            font-size: .72rem;
            font-weight: 700;
            color: #4f46e5;
            text-decoration: none;
            white-space: nowrap;
        }}
        .alert-history-links {{
            display: flex;
            align-items: center;
            gap: .45rem;
            justify-content: flex-end;
        }}
        .alert-history-chart:hover,
        .alert-history-offer:hover {{
            text-decoration: underline;
        }}
        .expand-icon {{
            flex: 0 0 auto;
            margin-top: .15rem;
            font-size: .85rem;
            opacity: .75;
            transition: transform .16s ease;
        }}
        .expand-icon.collapsed {{
            transform: rotate(-90deg);
        }}
        .delta {{ font-weight: bold; }}
        .delta.up {{ color: #dc3545; }}
        .delta.down {{ color: #28a745; }}
        .delta.flat {{ color: #6c757d; }}
        .hotels-section {{
            margin-top: var(--section-gap);
            background: var(--gradient-card);
            border-radius: var(--radius-xl);
            padding: 2rem;
            box-shadow: var(--shadow-md);
            border: 1px solid var(--border-soft);
            animation: sectionFadeIn .75s ease both;
        }}
        .hotels-section.full-width-table-section {{
            padding: 2rem;
            overflow: visible;
            width: 100%;
            max-width: 100%;
            margin: var(--section-gap) 0 0 0;
        }}
        .hotels-section.full-width-table-section h3,
        .hotels-section.full-width-table-section .deal-legend,
        .hotels-section.full-width-table-section .table-filters,
        .hotels-section.full-width-table-section .mobile-sort-bar,
        .hotels-section.full-width-table-section .pagination,
        .hotels-section.full-width-table-section .pagination-info {{
            margin-left: 0;
            margin-right: 0;
            width: 100%;
            max-width: var(--table-block-max-width);
            margin-inline: auto;
        }}
        .hotels-section.full-width-table-section .table-container {{
            border-radius: var(--panel-shell-radius);
            margin-top: .45rem;
            width: 100%;
            max-width: var(--table-block-max-width);
            margin-left: 0;
            margin-right: 0;
        }}
        
        .hotels-section h3 {{
            font-size: 1.5rem;
            font-weight: 700;
            margin: 0 0 1.5rem 0;
            color: #1f2937;
            display: flex;
            align-items: center;
            gap: 0.5rem;
        }}
        
        .table-container {{
            overflow-x: auto;
            overflow-y: hidden;
            -webkit-overflow-scrolling: touch;
            width: 100%;
            max-width: 100%;
            border-radius: 16px;
            box-shadow: none;
            background: #eef6fc;
            border: 1px solid #e2eef8;
            padding: 8px 12px;
        }}
        .dark-theme .table-container {{
            background: #0f172a;
            border-color: #1e293b;
        }}
        .table-container--hotels {{
            overflow-x: auto;
        }}
        
        .hotels-table {{
            width: 100%;
            min-width: 1020px;
            table-layout: fixed;
            border-collapse: separate;
            border-spacing: 0 6px;
            margin: 0;
            font-size: 0.85rem;
        }}

        .hotels-table col.col-w-hotel {{ width: 22.0%; }}
        .hotels-table col.col-w-price {{ width: 12.0%; }}
        .hotels-table col.col-w-deal {{ width: 8.0%; }}
        .hotels-table col.col-w-forecast {{ width: 9.0%; }}
        .hotels-table col.col-w-ta {{ width: 7.0%; }}
        .hotels-table col.col-w-d48 {{ width: 5.5%; }}
        .hotels-table col.col-w-davg {{ width: 8.0%; }}
        .hotels-table col.col-w-region {{ width: 9.5%; }}
        .hotels-table col.col-w-dates {{ width: 11.0%; }}
        .hotels-table col.col-w-dur {{ width: 4.0%; }}
        .hotels-table col.col-w-link {{ width: 4.0%; }}

        .hotels-table td.col-w-d48-td,
        .hotels-table td.col-w-davg-td,
        .hotels-table td.col-w-dur-td,
        .hotels-table td.col-duration,
        .hotels-table td.offer-link-cell {{
            text-align: center !important;
        }}

        .hero-title-clean {{
            font-size: 1.75rem;
            font-weight: 700;
            color: #0f172a;
            letter-spacing: -0.02em;
            margin: 0 0 1rem 0;
            line-height: 1.25;
        }}
        .dark-theme .hero-title-clean {{
            color: #f8fafc;
        }}

        .hotels-table td.col-tight,
        .hotels-table th.col-tight {{
            white-space: nowrap;
            vertical-align: middle;
            padding: 0.55rem 0.5rem;
        }}

        .hotels-table th.col-tight {{
            font-size: 0.76rem;
            letter-spacing: 0.04em;
        }}

        .hotels-table .col-dates {{
            white-space: nowrap;
            font-size: .8rem;
        }}

        .table-scroll-hint {{
            display: none;
            margin: 0 0 .45rem;
            font-size: .74rem;
            color: var(--text-muted);
            line-height: 1.35;
        }}
        
        .hotels-table thead tr {{
            background: transparent !important;
            border-radius: 0;
        }}
        .dark-theme .hotels-table thead tr {{
            background: transparent !important;
        }}
        
        .hotels-table th {{
            background: transparent !important;
            color: #0284c7;
            font-weight: 800;
            font-size: 0.76rem;
            text-transform: uppercase;
            letter-spacing: 0.04em;
            padding: 0.6rem 0.5rem;
            text-align: left;
            cursor: pointer;
            user-select: none;
            position: sticky;
            top: 0;
            z-index: 10;
            border: none !important;
            box-shadow: none !important;
            white-space: nowrap;
            transition: var(--transition-fast);
        }}
        .dark-theme .hotels-table th {{
            color: #38bdf8;
        }}
        
        .hotels-table th:hover {{
            background: transparent !important;
            color: #0369a1;
            transform: none;
            box-shadow: none;
        }}
        
        .hotels-table th.sortable::after {{
            content: ' ↕';
            opacity: 0.55;
            margin-left: 0.35rem;
            font-size: 0.8rem;
        }}
        
        .hotels-table th.sort-asc::after {{
            content: ' ↑';
            opacity: 1;
            color: #0284c7;
        }}
        
        .hotels-table th.sort-desc::after {{
            content: ' ↓';
            opacity: 1;
            color: #0284c7;
        }}
        
        .hotels-table td {{
            height: auto;
            padding: 0.75rem 0.65rem;
            border: none;
            vertical-align: top;
            background: transparent;
            box-sizing: border-box;
            line-height: 1.3;
            transition: all 0.18s cubic-bezier(0.4, 0, 0.2, 1);
        }}

        /* ── Alternating White Volumetric Floating Cards & Sky Blue Rows ── */
        .hotels-table tbody tr {{
            transition: all 0.18s cubic-bezier(0.4, 0, 0.2, 1);
        }}

        .hotels-table tbody tr:nth-child(odd) td,
        .hotels-table tbody tr.row-odd td {{
            background: #ffffff;
            border-top: 1px solid #e2eef8;
            border-bottom: 1px solid #e2eef8;
        }}

        .hotels-table tbody tr:nth-child(odd) td:first-child,
        .hotels-table tbody tr.row-odd td:first-child {{
            border-left: 1px solid #e2eef8;
            border-top-left-radius: 14px;
            border-bottom-left-radius: 14px;
        }}

        .hotels-table tbody tr:nth-child(odd) td:last-child,
        .hotels-table tbody tr.row-odd td:last-child {{
            border-right: 1px solid #e2eef8;
            border-top-right-radius: 14px;
            border-bottom-right-radius: 14px;
        }}

        .hotels-table tbody tr:nth-child(odd),
        .hotels-table tbody tr.row-odd {{
            box-shadow: 0 3px 10px rgba(0, 50, 100, 0.05);
        }}

        .hotels-table tbody tr:nth-child(even) td,
        .hotels-table tbody tr.row-even td {{
            background: transparent;
            border-top: 1px solid transparent;
            border-bottom: 1px solid transparent;
            border-left: 1px solid transparent;
            border-right: 1px solid transparent;
        }}

        .hotels-table tbody tr:nth-child(even) td:first-child,
        .hotels-table tbody tr.row-even td:first-child {{
            border-top-left-radius: 14px;
            border-bottom-left-radius: 14px;
        }}
        .hotels-table tbody tr:nth-child(even) td:last-child,
        .hotels-table tbody tr.row-even td:last-child {{
            border-top-right-radius: 14px;
            border-bottom-right-radius: 14px;
        }}

        .hotels-table tbody tr:hover td {{
            background: #ffffff !important;
            border-top: 1px solid #7dd3fc !important;
            border-bottom: 1px solid #7dd3fc !important;
        }}
        .hotels-table tbody tr:hover td:first-child {{
            border-left: 1px solid #7dd3fc !important;
            border-top-left-radius: 14px;
            border-bottom-left-radius: 14px;
        }}
        .hotels-table tbody tr:hover td:last-child {{
            border-right: 1px solid #7dd3fc !important;
            border-top-right-radius: 14px;
            border-bottom-right-radius: 14px;
        }}
        .hotels-table tbody tr:hover {{
            transform: translateY(-1px);
            box-shadow: 0 6px 18px -2px rgba(2, 132, 199, 0.14) !important;
        }}

        .dark-theme .hotels-table tbody tr:nth-child(odd) td,
        .dark-theme .hotels-table tbody tr.row-odd td {{
            background: #1e293b;
            border-top: 1px solid #334155;
            border-bottom: 1px solid #334155;
        }}
        .dark-theme .hotels-table tbody tr:nth-child(odd) td:first-child,
        .dark-theme .hotels-table tbody tr.row-odd td:first-child {{
            border-left: 1px solid #334155;
        }}
        .dark-theme .hotels-table tbody tr:nth-child(odd) td:last-child,
        .dark-theme .hotels-table tbody tr.row-odd td:last-child {{
            border-right: 1px solid #334155;
        }}

        .dark-theme .hotels-table tbody tr:hover td {{
            background: #1e293b !important;
            border-top-color: #38bdf8 !important;
            border-bottom-color: #38bdf8 !important;
        }}
        .dark-theme .hotels-table tbody tr:hover td:first-child {{
            border-left-color: #38bdf8 !important;
        }}
        .dark-theme .hotels-table tbody tr:hover td:last-child {{
            border-right-color: #38bdf8 !important;
        }}

        /* ── Price formatting ── */
        .hotels-table td.price {{
            color: #059669 !important;
            font-weight: 800;
            font-size: 1.02rem;
            line-height: 1.2;
            white-space: nowrap;
        }}
        .hotels-table td.price .price-main {{
            display: inline;
            font-weight: 800;
            color: #059669;
            vertical-align: middle;
        }}
        .hotels-table .comeback-badge {{
            display: inline-flex !important;
            align-items: center;
            font-size: 0.68rem;
            font-weight: 700;
            margin-left: 6px;
            padding: 1px 6px;
            border-radius: 999px;
            vertical-align: middle;
            line-height: 1.2;
            color: #065f46;
            background: rgba(16, 185, 129, 0.16);
            border: 1px solid rgba(16, 185, 129, 0.32);
        }}
        .hotels-table .cheaper-alt-badge {{
            display: inline-flex !important;
            align-items: center;
            font-size: 0.65rem;
            font-weight: 600;
            margin-left: 6px;
            padding: 1px 6px;
            border-radius: 6px;
            vertical-align: middle;
            line-height: 1.2;
            color: #0284c7;
            background: #e0f2fe;
            border: 1px solid #bae6fd;
            text-decoration: none;
            white-space: nowrap;
        }}
        .dark-theme .hotels-table td.price {{
            color: #34d399 !important;
        }}
        .dark-theme .hotels-table .comeback-badge {{
            color: #34d399;
            background: rgba(16, 185, 129, 0.2);
            border-color: rgba(52, 211, 153, 0.4);
        }}
        .dark-theme .hotels-table .cheaper-alt-badge {{
            color: #38bdf8;
            background: rgba(56, 189, 248, 0.15);
            border-color: rgba(56, 189, 248, 0.3);
        }}

        /* ── Link Button ── */
        .col-link-btn {{
            display: inline-flex;
            align-items: center;
            justify-content: center;
            width: 32px;
            height: 32px;
            border-radius: 50%;
            background: #e0f2fe;
            color: #0284c7;
            text-decoration: none;
            font-weight: 700;
            transition: all 0.18s ease;
            box-shadow: 0 2px 6px rgba(2, 132, 199, 0.12);
        }}
        .col-link-btn:hover {{
            background: #0284c7;
            color: #ffffff;
            transform: scale(1.08);
            box-shadow: 0 4px 12px rgba(2, 132, 199, 0.3);
        }}
        .dark-theme .col-link-btn {{
            background: rgba(56, 189, 248, 0.15);
            color: #38bdf8;
        }}
        .dark-theme .col-link-btn:hover {{
            background: #38bdf8;
            color: #0f172a;
        }}

        /* ── Tooltip на заголовках таблицы ── */
        .th-tip {{
            display: inline-flex;
            align-items: center;
            justify-content: center;
            width: 1.1em;
            height: 1.1em;
            font-size: 0.7rem;
            background: rgba(255,255,255,.28);
            border-radius: 50%;
            cursor: help;
            position: relative;
            vertical-align: middle;
            margin-left: 2px;
            font-style: normal;
            font-weight: 700;
            line-height: 1;
            transition: background .15s;
            border: 1px solid rgba(255,255,255,.45);
        }}
        .th-tip:hover {{
            background: rgba(255,255,255,.55);
        }}
        .th-tip::after {{
            content: attr(data-tip);
            position: absolute;
            top: calc(100% + 8px);
            bottom: auto;
            left: 50%;
            transform: translateX(-50%);
            background: #1e293b;
            color: #f1f5f9;
            font-size: 0.78rem;
            font-weight: 400;
            text-transform: none;
            letter-spacing: 0;
            padding: .55rem .85rem;
            border-radius: 8px;
            white-space: normal;
            width: 220px;
            max-width: 90vw;
            box-shadow: 0 8px 24px rgba(0,0,0,.4);
            pointer-events: none;
            opacity: 0;
            transition: opacity .18s ease;
            z-index: 9999;
        }}
        .th-tip::before {{
            content: "";
            position: absolute;
            top: calc(100% + 2px);
            left: 50%;
            transform: translateX(-50%);
            border-width: 0 6px 6px 6px;
            border-style: solid;
            border-color: transparent transparent #1e293b transparent;
            opacity: 0;
            transition: opacity .18s ease;
            z-index: 10000;
            pointer-events: none;
        }}
        .th-tip:hover::after,
        .th-tip:hover::before {{
            opacity: 1;
        }}

        /* ── Статус обновления ── */
        .update-status {{
            display: inline-flex;
            align-items: center;
            gap: .35rem;
            font-size: 0.92rem;
            font-weight: 600;
            white-space: nowrap;
        }}
        .update-status--ok    {{ color: #10b981; }}
        .update-status--warn  {{ color: #f59e0b; }}
        .update-status--err   {{ color: #ef4444; }}

        /* ── Top Movers Widget ── */
        .top-movers-section {{
            background: #fff;
            border: 1px solid var(--border-soft);
            border-radius: var(--radius-xl);
            padding: 1.1rem 1.3rem;
            margin-bottom: 1.5rem;
            box-shadow: var(--shadow-sm);
        }}
        .top-movers-head {{
            display: flex;
            align-items: baseline;
            justify-content: space-between;
            flex-wrap: wrap;
            gap: .4rem;
            margin-bottom: .85rem;
        }}
        .top-movers-head h3 {{
            margin: 0;
            font-size: 1.05rem;
            font-weight: 800;
            color: #1e293b;
        }}
        .top-movers-sub {{
            font-size: .8rem;
            color: var(--text-muted);
        }}
        .top-movers-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
            gap: .85rem;
        }}
        .top-movers-card {{
            background: #f8fafc;
            border-radius: 12px;
            padding: .85rem 1rem;
            border: 1px solid #e2e8f0;
        }}
        .top-movers-card--drops {{ border-left: 4px solid #10b981; }}
        .top-movers-card--rises {{ border-left: 4px solid #ef4444; }}
        .top-movers-card-title {{
            font-size: .85rem;
            font-weight: 700;
            margin-bottom: .6rem;
            color: #334155;
        }}
        .top-mover-empty {{
            font-size: .8rem;
            color: var(--text-muted);
            font-style: italic;
            padding: .4rem 0;
        }}
        .top-mover-item {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: .4rem;
            padding: .45rem .6rem;
            border-radius: 8px;
            text-decoration: none;
            color: inherit;
            background: #fff;
            margin-bottom: .35rem;
            border: 1px solid #edf2f7;
            transition: background .15s ease, border-color .15s ease;
        }}
        .top-mover-item:hover {{
            background: #f1f5f9;
            border-color: #cbd5e1;
        }}
        .top-mover-name {{
            font-size: .84rem;
            font-weight: 600;
            color: #1e293b;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            max-width: 150px;
        }}
        .top-mover-prices {{
            font-size: .78rem;
            color: #64748b;
        }}
        .top-mover-badge {{
            font-size: .75rem;
            font-weight: 700;
            padding: .12rem .4rem;
            border-radius: 999px;
            white-space: nowrap;
        }}
        .top-mover-badge.drop {{
            background: rgba(16,185,129,.15);
            color: #047857;
        }}
        .top-mover-badge.up {{
            background: rgba(239,68,68,.15);
            color: #b91c1c;
        }}

        @keyframes sectionFadeIn {{
            from {{ opacity: 0; transform: translateY(8px); }}
            to {{ opacity: 1; transform: translateY(0); }}
        }}
        @keyframes floatSoft {{
            0%, 100% {{ transform: translateY(0); }}
            50% {{ transform: translateY(-4px); }}
        }}
        @keyframes pulseGlow {{
            0%, 100% {{ box-shadow: 0 0 0 rgba(255,255,255,0); }}
            50% {{ box-shadow: 0 8px 22px rgba(255,255,255,.12); }}
        }}
        @keyframes gradientDrift {{
            0% {{ transform: translate3d(0,0,0) scale(1); }}
            100% {{ transform: translate3d(-10px, 8px, 0) scale(1.02); }}
        }}
        @keyframes taStarPulse {{
            0%, 100% {{ transform: scale(1); filter: brightness(1); }}
            50% {{ transform: scale(1.08); filter: brightness(1.15); }}
        }}
        @keyframes taGlow {{
            0%, 100% {{ box-shadow: 0 0 0 rgba(245, 158, 11, 0); }}
            50% {{ box-shadow: 0 0 12px rgba(245, 158, 11, 0.35); }}
        }}

        .ta-rating {{
            display: inline-flex;
            flex-direction: column;
            align-items: center;
            gap: 0.1rem;
            line-height: 1.1;
            animation: taGlow 4s ease-in-out infinite;
        }}
        .ta-rating--empty {{
            opacity: 0.55;
            animation: none;
        }}
        .ta-stars {{
            display: inline-flex;
            gap: 0.05rem;
            letter-spacing: -0.05em;
        }}
        .ta-star {{
            font-size: 0.82rem;
            line-height: 1;
        }}
        .ta-star--full, .ta-star--half {{
            color: #f59e0b;
            text-shadow: 0 0 8px rgba(245, 158, 11, 0.35);
            animation: taStarPulse 2.8s ease-in-out infinite;
        }}
        .ta-star--half {{
            opacity: 0.72;
        }}
        .ta-star--empty {{
            color: #cbd5e1;
        }}
        .ta-meta {{
            display: inline-flex;
            align-items: baseline;
            gap: 0.35rem;
            font-size: 0.72rem;
            color: #64748b;
            white-space: nowrap;
        }}
        .ta-score {{
            font-weight: 800;
            color: #b45309;
            font-variant-numeric: tabular-nums;
        }}
        .ta-reviews {{
            opacity: 0.9;
        }}
        .ta-reviews--new {{
            font-style: italic;
            opacity: 0.75;
        }}
        .hotels-table th.th-ta {{
            text-align: center;
        }}
        .th-ta-icon {{
            display: inline-flex;
            align-items: center;
            justify-content: center;
            width: 1.75rem;
            height: 1.2rem;
            border-radius: 0.25rem;
            background: #fff;
            padding: 0.08rem 0.12rem;
            box-shadow: 0 1px 3px rgba(15, 23, 42, 0.18);
            vertical-align: middle;
        }}
        .th-ta-svg {{
            display: block;
            width: 100%;
            height: auto;
        }}
        .hotels-table td.col-w-ta-td {{
            font-size: 0.78rem;
            vertical-align: top;
            text-align: center;
        }}
        
        .hotels-table .col-hotel {{
            width: auto;
            max-width: 100%;
            padding: 0.75rem 0.65rem;
            overflow: visible;
        }}

        .hotel-name {{
            color: #0f172a;
            font-weight: 700;
            font-size: 0.88rem;
            line-height: 1.35;
            max-width: 100%;
            white-space: normal;
            overflow-wrap: break-word;
            word-break: break-word;
            display: block;
            overflow: visible;
        }}
        
        .hotel-name a,
        .open-chart-link {{
            color: #0f172a !important;
            text-decoration: none;
            transition: var(--transition-fast);
        }}
        
        .hotel-name a:hover,
        .open-chart-link:hover {{
            color: #0284c7 !important;
            text-decoration: underline;
        }}

        .dark-theme .hotel-name,
        .dark-theme .hotel-name a,
        .dark-theme .open-chart-link {{
            color: #f1f5f9 !important;
        }}
        .dark-theme .hotel-name a:hover,
        .dark-theme .open-chart-link:hover {{
            color: #38bdf8 !important;
        }}

        .watchlist-star-btn {{
            background: none;
            border: none;
            color: #94a3b8;
            cursor: pointer;
            font-size: 0.95rem;
            margin-right: 6px;
            padding: 0;
            vertical-align: middle;
            transition: color 0.15s ease;
        }}
        .watchlist-star-btn:hover {{
            color: #f59e0b;
        }}

        .arrival-hub {{
            font-weight: 700;
            font-size: 0.86rem;
            color: #0f172a;
            white-space: nowrap;
        }}
        .dark-theme .arrival-hub {{
            color: #f1f5f9;
        }}

        .deal-cell-inline {{
            display: inline-block;
            white-space: nowrap;
            line-height: 1.25;
            font-size: 0.82rem;
        }}

        .deal-cell-inline .deal-conf-short {{
            opacity: 0.6;
            font-size: 0.7rem;
            font-weight: 500;
            margin-left: 0.1rem;
        }}

        .hotels-table .col-w-deal-td {{
            font-size: 0.78rem;
        }}

        .hotels-table .col-w-dur-td {{
            font-size: 0.78rem;
        }}

        .col-dates {{
            font-size: 0.82rem;
            font-variant-numeric: tabular-nums;
        }}

        .col-duration {{
            font-size: 0.82rem;
        }}
        
        .price {{
            font-weight: 800;
            font-size: 1.05rem;
            color: var(--success-color);
            white-space: nowrap;
        }}


        
        .airport {{
            font-weight: 600;
            font-size: 0.9rem;
            color: var(--text-color);
            background: #f0f9ff;
            border-radius: var(--radius-sm);
            padding: 0.5rem;
            text-align: center;
        }}
        
        .alternatives {{
            font-size: 0.8rem;
            max-width: 200px;
        }}
        
        .alternatives-container {{
            display: flex;
            flex-direction: column;
            gap: 0.25rem;
        }}
        
        .airport-alt {{
            background: linear-gradient(135deg, #dcfce7 0%, #bbf7d0 100%);
            border: 1px solid #16a34a;
            border-radius: var(--radius-sm);
            padding: 0.5rem;
            margin: 0.125rem 0;
            cursor: pointer;
            transition: var(--transition-fast);
        }}
        
        .airport-alt:hover {{
            background: linear-gradient(135deg, #bbf7d0 0%, #86efac 100%);
            transform: translateY(-1px);
            box-shadow: var(--shadow-sm);
        }}
        
        .airport-alt small {{
            color: #15803d;
            font-weight: 600;
        }}
        
        .hotels-table .delta {{
            font-weight: 700;
            font-size: 0.8rem;
            padding: 0.18rem 0.4rem;
            border-radius: var(--radius-sm);
            display: inline-block;
            white-space: nowrap;
            line-height: 1.15;
            text-align: center;
            box-sizing: border-box;
        }}

        .hotels-table .col-w-davg-td,
        .hotels-table .col-w-d48-td {{
            font-size: 0.78rem;
        }}

        .delta {{
            font-weight: 700;
            font-size: 0.9rem;
            padding: 0.25rem 0.5rem;
            border-radius: var(--radius-sm);
            display: inline-block;
            min-width: 3rem;
            text-align: center;
        }}
        
        .delta.up {{
            background: #fff1f2;
            color: #be123c;
            border: 1px solid #fecdd3;
            border-radius: 999px;
            padding: 0.15rem 0.6rem;
            font-size: 0.78rem;
            font-weight: 700;
            display: inline-flex;
            align-items: center;
            justify-content: center;
        }}
        
        .delta.down {{
            background: #ecfdf5;
            color: #047857;
            border: 1px solid #a7f3d0;
            border-radius: 999px;
            padding: 0.15rem 0.6rem;
            font-size: 0.78rem;
            font-weight: 700;
            display: inline-flex;
            align-items: center;
            justify-content: center;
        }}
        
        .delta.flat {{
            background: #f1f5f9;
            color: #94a3b8;
            border-radius: 999px;
            padding: 0.15rem 0.6rem;
            min-width: 2.2rem;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            font-size: 0.78rem;
            font-weight: 600;
        }}
        
        .offer-link {{
            color: var(--primary-color);
            text-decoration: none;
            font-size: 1.2rem;
            padding: 0.5rem 0.75rem;
            background: var(--gradient-card);
            border-radius: var(--radius-md);
            border: 1px solid #e2e8f0;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            transition: var(--transition-normal);
            box-shadow: var(--shadow-sm);
        }}
        
        .offer-link:hover {{
            background: var(--gradient-primary);
            color: white;
            transform: scale(1.1);
            box-shadow: var(--shadow-md);
            text-decoration: none;
        }}
        
        .offer-link-cell {{
            text-align: center;
            width: 80px;
        }}
        
        /* Пагинация */
        .pagination {{
            display: flex;
            justify-content: center;
            align-items: center;
            gap: 0.5rem;
            margin-top: 2rem;
            padding: 1rem;
        }}
        
        .pagination button {{
            padding: 0.5rem 1rem;
            border: 1px solid #e2e8f0;
            background: white;
            color: #64748b;
            border-radius: var(--radius-md);
            cursor: pointer;
            transition: var(--transition-fast);
            font-weight: 600;
        }}
        
        .pagination button:hover:not(:disabled) {{
            background: var(--gradient-primary);
            color: white;
            border-color: transparent;
        }}
        
        .pagination button:disabled {{
            opacity: 0.5;
            cursor: not-allowed;
        }}
        
        .pagination button.active {{
            background: var(--gradient-primary);
            color: white;
            border-color: transparent;
        }}
        
        .pagination-info {{
            color: #64748b;
            font-size: 0.875rem;
            margin: 0 1rem;
        }}
        
        /* Фильтры */
        .table-filters {{
            display: flex;
            gap: 1rem;
            margin-bottom: 1.5rem;
            flex-wrap: wrap;
            align-items: center;
        }}
        
        .filter-input {{
            padding: 0.75rem 1rem;
            border: 1px solid #e2e8f0;
            border-radius: var(--radius-md);
            background: white;
            font-size: 0.875rem;
            transition: var(--transition-fast);
            min-width: 200px;
        }}
        
        .filter-input:focus {{
            outline: none;
            border-color: var(--primary-color);
            box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.1);
        }}
        
        .filter-select {{
            padding: 0.75rem 1rem;
            border: 1px solid #e2e8f0;
            border-radius: var(--radius-md);
            background: white;
            font-size: 0.875rem;
            cursor: pointer;
            transition: var(--transition-fast);
        }}
        
        .filter-select:focus {{
            outline: none;
            border-color: var(--primary-color);
            box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.1);
        }}

        .mobile-sort-bar {{
            display: none;
        }}
        
        /* Sidebar Navigation */
        .app-topbar {{
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            height: 52px;
            z-index: 1002;
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 0 0.75rem;
            background: rgba(255,255,255,0.94);
            backdrop-filter: blur(14px);
            border-bottom: 1px solid var(--border-soft);
        }}

        .dark-theme .app-topbar {{
            background: rgba(15, 23, 42, 0.94);
            border-bottom-color: #334155;
        }}

        .sidebar {{
            position: fixed;
            top: 52px;
            left: 0;
            width: 220px;
            height: calc(100vh - 52px);
            background: rgba(255,255,255,.92);
            backdrop-filter: blur(18px);
            box-shadow: var(--shadow-xl);
            z-index: 1000;
            transform: translateX(-100%);
            transition: var(--transition-normal);
            overflow-y: auto;
        }}
        
        .sidebar.open {{
            transform: translateX(0);
        }}
        
        .sidebar-header {{
            padding: 0.65rem 0.85rem;
            border-bottom: 1px solid #e2e8f0;
            background: var(--gradient-primary);
            color: white;
        }}
        
        .sidebar-header h2 {{
            margin: 0;
            font-size: 0.92rem;
            font-weight: 800;
            letter-spacing: .01em;
        }}
        
        .sidebar-nav {{
            padding: 0.35rem 0 0.5rem;
        }}
        
        .nav-item {{
            display: flex;
            align-items: center;
            gap: 0.45rem;
            padding: 0.42rem 0.85rem;
            color: var(--text-muted);
            text-decoration: none;
            transition: var(--transition-fast);
            border-left: 3px solid transparent;
            margin: 1px 6px 1px 0;
            border-radius: 0 8px 8px 0;
        }}

        .nav-group-label {{
            padding: 0.55rem 0.85rem 0.18rem;
            font-size: 0.92rem;
            font-weight: 700;
            letter-spacing: .01em;
            color: #334155;
            display: flex;
            align-items: center;
            gap: 0.5rem;
        }}

        .nav-group-label span:first-child {{
            font-size: 1.35rem;
            line-height: 1;
        }}
        
        .dark-theme .nav-group-label {{
            color: #cbd5e1;
        }}

        .nav-group-label {{
            padding: .85rem 1.5rem .35rem;
            font-size: .68rem;
            font-weight: 800;
            letter-spacing: .08em;
            text-transform: uppercase;
            color: #94a3b8;
            display: flex;
            align-items: center;
            gap: .4rem;
        }}
        
        .nav-item:hover {{
            background: rgba(79,70,229,.08);
            color: var(--primary-color);
            border-left-color: var(--primary-color);
        }}
        
        .nav-item.active {{
            background: linear-gradient(90deg, #f0f9ff 0%, #e0f2fe 100%);
            color: var(--primary-color);
            border-left-color: var(--primary-color);
            font-weight: 700;
        }}
        
        .nav-item .flag {{
            font-size: 0.95rem;
            width: 1.1rem;
            text-align: center;
            flex-shrink: 0;
        }}
        
        .nav-item .country-name {{
            font-weight: 600;
            font-size: 0.82rem;
            line-height: 1.25;
        }}
        
        .sidebar-toggle {{
            position: static;
            background: var(--gradient-primary);
            border: none;
            border-radius: 10px;
            width: 2.25rem;
            height: 2.25rem;
            color: white;
            cursor: pointer;
            box-shadow: var(--shadow-sm);
            transition: var(--transition-normal);
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 1rem;
            flex-shrink: 0;
        }}
        
        .sidebar-toggle:hover {{
            transform: scale(1.05);
            box-shadow: var(--shadow-xl);
        }}
        
        .sidebar-overlay {{
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background: rgba(0, 0, 0, 0.5);
            z-index: 999;
            opacity: 0;
            visibility: hidden;
            pointer-events: none;
            transition: var(--transition-normal);
        }}
        
        .sidebar-overlay.open {{
            opacity: 1;
            visibility: visible;
            pointer-events: auto;
        }}
        
        .main-content {{
            transition: var(--transition-normal);
            margin-left: 0;
            padding-top: 52px;
        }}
        
        .main-content.sidebar-open {{
            margin-left: 220px;
        }}
        
        /* Responsive */
        @media (max-width: 1024px) {{
            :root {{
                --container-padding: 1.25rem;
                --page-gutter: 1rem;
            }}
            .header {{
                padding: 2rem 1.25rem;
            }}
            .header h1 {{
                font-size: 2.25rem;
            }}
            .metrics {{
                grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
            }}
            .changes-section {{
                grid-template-columns: 1fr;
            }}
        }}

        @media (max-width: 768px) {{
            :root {{
                --section-gap: 1.25rem;
                --section-inner-x: 1rem;
                --panel-shell-radius: 12px;
                --table-block-max-width: 100%;
                --container-padding: 1rem;
                --page-gutter: .75rem;
            }}
            .sidebar {{
                width: 100%;
            }}
            
            .main-content.sidebar-open {{
                margin-left: 0;
            }}
            
            .header h1 {{
                font-size: 2rem;
            }}
            
            .metrics {{
                grid-template-columns: 1fr;
            }}
            .metrics.metrics-compact {{
                gap: .5rem;
            }}
            .metrics.metrics-compact .metric {{
                min-height: 50px;
            }}
            .metric {{
                padding: 1.25rem;
            }}
            .metric-value {{
                font-size: 2rem;
            }}
            
            .table-filters {{
                flex-direction: column;
                align-items: stretch;
            }}
            .mobile-sort-bar {{
                display: flex;
                flex-direction: column;
                gap: .45rem;
                margin: 0 0 1rem;
            }}
            .mobile-sort-bar-label {{
                font-size: .78rem;
                font-weight: 700;
                text-transform: uppercase;
                letter-spacing: .03em;
                color: var(--text-muted);
            }}
            .mobile-sort-controls {{
                display: flex;
                gap: .5rem;
                align-items: stretch;
            }}
            .mobile-sort-select {{
                flex: 1;
                min-width: 0;
                min-height: 44px;
            }}
            .mobile-sort-dir-btn {{
                flex: 0 0 44px;
                min-height: 44px;
                padding: 0;
                border: 1px solid #e2e8f0;
                border-radius: var(--radius-md);
                background: white;
                font-size: 1.1rem;
                font-weight: 700;
                line-height: 1;
                cursor: pointer;
                color: var(--primary-color);
                transition: var(--transition-fast);
            }}
            .mobile-sort-dir-btn:disabled {{
                opacity: .45;
                cursor: default;
            }}
            .mobile-sort-dir-btn:not(:disabled):hover {{
                border-color: var(--primary-color);
                background: rgba(79,70,229,.06);
            }}
            .dark-theme .mobile-sort-dir-btn {{
                background: rgba(30,41,59,.85);
                border-color: rgba(148,163,184,.35);
                color: #93c5fd;
            }}
            
            .filter-input, .filter-select {{
                min-width: auto;
            }}
            .hotels-section, .avg-top10-section, .trend-index-section, .alerts-section {{
                padding: 1rem;
                border-radius: 14px;
            }}
            .hotels-section.full-width-table-section {{
                padding: 1rem;
            }}
            .hotels-section.full-width-table-section h3,
            .hotels-section.full-width-table-section .deal-legend,
            .hotels-section.full-width-table-section .table-filters,
            .hotels-section.full-width-table-section .pagination,
            .hotels-section.full-width-table-section .pagination-info {{
                margin-left: 0;
                margin-right: 0;
            }}
            .hotels-table th, .hotels-table td {{
                padding: .7rem .55rem;
                font-size: .82rem;
            }}
            .table-container--hotels {{
                overflow: visible;
                background: transparent;
                border: none;
                box-shadow: none;
            }}
            #hotelsTable {{
                width: 100%;
                min-width: 0;
                table-layout: auto;
            }}
            #hotelsTable thead,
            #hotelsTable colgroup {{
                display: none;
            }}
            #hotelsTable tbody tr {{
                display: block;
                background: rgba(255,255,255,.96);
                border: 1px solid var(--border-soft);
                border-radius: 12px;
                margin-bottom: .65rem;
                padding: .55rem .75rem;
                box-shadow: var(--shadow-sm);
            }}
            #hotelsTable tbody tr:hover {{
                transform: none;
                box-shadow: var(--shadow-md);
            }}
            #hotelsTable tbody td {{
                display: grid;
                grid-template-columns: minmax(5.2rem, 32%) 1fr;
                gap: .25rem .55rem;
                align-items: start;
                padding: .4rem 0;
                border: none;
                white-space: normal;
                text-align: left;
            }}
            #hotelsTable tbody td::before {{
                content: attr(data-label);
                font-size: .7rem;
                font-weight: 700;
                text-transform: uppercase;
                letter-spacing: .03em;
                color: var(--text-muted);
                line-height: 1.35;
            }}
            #hotelsTable tbody td.col-hide-sm {{
                display: none;
            }}
            #hotelsTable tbody td.col-hotel {{
                display: block;
                padding: 0 0 .5rem;
                margin-bottom: .4rem;
                border-bottom: 1px solid rgba(226,232,240,.95);
            }}
            #hotelsTable tbody td.col-hotel::before {{
                display: none;
            }}
            #hotelsTable tbody td.col-hotel .hotel-hover-link {{
                font-size: 1rem;
                font-weight: 700;
                line-height: 1.25;
            }}
            #hotelsTable tbody td.price .price-main {{
                font-size: 1.05rem;
            }}
            #hotelsTable tbody td.offer-link-cell {{
                padding-top: .15rem;
            }}
            #hotelsTable tbody td.offer-link-cell::before {{
                align-self: center;
            }}
            .hover-thumb {{
                display: none !important;
            }}
            .table-scroll-hint {{
                display: none;
            }}
            .table-container {{
                border-radius: var(--panel-shell-radius);
            }}
            .footer {{
                font-size: .84rem;
                padding: 14px;
            }}
            .alert-card-actions {{
                flex-direction: column;
            }}
            .alert-history-row {{
                grid-template-columns: auto minmax(0, 1fr);
                grid-template-rows: auto auto;
                gap: .25rem .45rem;
            }}
            .alert-history-price {{
                grid-column: 2;
                text-align: left;
            }}
            .alert-history-links {{
                grid-column: 2;
                justify-content: flex-start;
            }}
            .alerts-header {{
                flex-direction: column;
                align-items: stretch;
            }}
            .alerts-grid {{
                grid-template-columns: 1fr;
            }}
            .hero {{
                min-height: 180px;
            }}
            details.dashboard-fold > summary {{
                padding: .72rem .85rem;
                font-size: .94rem;
            }}
            .fold-title-meta {{
                display: none;
            }}
            .fold-content {{
                padding: 0 .85rem .8rem;
            }}
            .hero-kpis {{
                grid-template-columns: repeat(2, minmax(0,1fr));
            }}
            .cards-grid {{
                grid-template-columns: 1fr;
            }}
            .hotel-card-img {{
                height: 106px;
            }}
            .mode-switch {{
                width: 100%;
                justify-content: space-between;
            }}
            #modeSwitchRow.table-toolbar {{
                flex-direction: column;
                align-items: stretch;
                gap: .45rem;
            }}
            #modeSwitchRow .table-mode-switch {{
                transform: none;
                width: 100%;
            }}
            #modeSwitchRow .mode-btn {{
                min-height: 44px;
                padding: .5rem 1rem;
            }}
            .table-header-row {{
                flex-direction: column;
                align-items: stretch;
                gap: .55rem;
            }}
            .table-toolbar {{
                margin-left: 0;
                width: 100%;
                justify-content: space-between;
            }}
            .departure-modal {{
                padding: .5rem;
                align-items: flex-end;
            }}
            .departure-modal-dialog {{
                width: 100%;
                max-height: 92vh;
                border-radius: 16px 16px 0 0;
            }}
            .departure-modal-chart {{
                width: calc(100% - 1.4rem);
                height: 240px;
                min-height: 240px;
                margin: 0 .7rem 10px;
            }}
            .departure-offers-table {{
                min-width: 460px;
            }}
            .mode-btn {{
                flex: 1;
            }}
        }}

        @media (max-width: 480px) {{
            :root {{
                --container-padding: .75rem;
                --page-gutter: .5rem;
            }}
            .container {{
                border-radius: 14px;
            }}
            .header {{
                border-radius: 14px;
                margin-bottom: 1.25rem;
            }}
            .header h1 {{
                font-size: 1.45rem;
                line-height: 1.2;
            }}
            .header p {{
                font-size: .9rem;
            }}
            .hero-content {{
                padding: 1rem .9rem;
            }}
            .hero-kpis {{
                grid-template-columns: 1fr;
            }}
            .pagination {{
                flex-wrap: wrap;
                gap: .35rem;
                padding: .75rem .5rem;
            }}
            .pagination button {{
                padding: .45rem .75rem;
                font-size: .82rem;
            }}
            .country-flag {{
                font-size: 1.35rem;
                margin-right: .35rem;
            }}
            .hover-thumb {{
                width: 180px;
                height: 118px;
            }}
        }}

        @media (prefers-reduced-motion: reduce) {{
            * {{
                animation: none !important;
                transition: none !important;
            }}
        }}
        
        /* Country Flags */
        .country-flag {{
            font-size: 2rem;
            margin-right: 0.5rem;
            display: inline-block;
            vertical-align: middle;
        }}
        
        .header .country-flag {{
            font-size: 3rem;
            margin-right: 1rem;
        }}
        .footer {{
            text-align: center;
            margin-top: 30px;
            padding: 20px;
            background: #f8f9fa;
            border-radius: 8px;
        }}
        /* Hover preview */
        .hover-thumb {{ position: absolute; display: none; width: 240px; height: 160px; background: #fff; border: 1px solid #ddd; box-shadow: 0 2px 8px rgba(0,0,0,.15); border-radius: 6px; padding: 4px; z-index: 9999; }}
        .hover-thumb img {{ width: 100%; height: 100%; object-fit: cover; border-radius: 4px; }}
        
        /* Watchlist Star Button */
        .watchlist-star-btn {{
            background: none;
            border: none;
            color: #94a3b8;
            font-size: 1.25rem;
            cursor: pointer;
            padding: 0 4px;
            transition: transform 0.2s ease, color 0.2s ease;
            vertical-align: middle;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            outline: none;
        }}
        .watchlist-star-btn:hover {{
            transform: scale(1.25);
            color: #f59e0b;
        }}
        .watchlist-star-btn.starred {{
            color: #f59e0b;
        }}
        
        .hotel-card-img {{
            position: relative;
        }}
        .watchlist-star-btn.card-star {{
            position: absolute;
            top: 8px;
            right: 8px;
            background: rgba(255, 255, 255, 0.75);
            backdrop-filter: blur(4px);
            -webkit-backdrop-filter: blur(4px);
            border-radius: 50%;
            width: 32px;
            height: 32px;
            box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1);
            z-index: 10;
            padding: 0;
            color: #94a3b8;
        }}
        .watchlist-star-btn.card-star.starred {{
            color: #f59e0b;
            background: rgba(255, 255, 255, 0.9);
        }}
        .dark-theme .watchlist-star-btn.card-star {{
            background: rgba(15, 23, 42, 0.75);
            color: #64748b;
        }}
        .dark-theme .watchlist-star-btn.card-star.starred {{
            background: rgba(15, 23, 42, 0.9);
            color: #f59e0b;
        }}

        /* Price Forecast Pills */
        .forecast-pill {{
            display: inline-flex;
            align-items: center;
            font-size: 0.7rem;
            font-weight: 700;
            padding: 2px 8px;
            border-radius: 12px;
            text-transform: uppercase;
            letter-spacing: 0.025em;
            white-space: nowrap;
            width: fit-content;
        }}
        .forecast-buy {{
            background-color: #dcfce7;
            color: #15803d;
        }}
        .forecast-wait {{
            background-color: #fef9c3;
            color: #a16207;
        }}
        .forecast-expensive {{
            background-color: #fee2e2;
            color: #b91c1c;
        }}
        .forecast-nodata {{
            background-color: #f1f5f9;
            color: #475569;
        }}
        
        .dark-theme .forecast-buy {{
            background-color: rgba(21, 128, 61, 0.2);
            color: #4ade80;
        }}
        .dark-theme .forecast-wait {{
            background-color: rgba(161, 98, 7, 0.2);
            color: #facc15;
        }}
        .dark-theme .forecast-expensive {{
            background-color: rgba(185, 28, 28, 0.2);
            color: #f87171;
        }}
        .dark-theme .forecast-nodata {{
            background-color: rgba(71, 85, 105, 0.2);
            color: #94a3b8;
        }}

        /* Calendar Heatmap Section */
        .calendar-section h3 {{
            margin-top: 0;
            margin-bottom: 0.25rem;
            font-size: 1.1rem;
            color: var(--text-primary);
        }}
        .calendar-month-container {{
            margin-bottom: 1.5rem;
            background: rgba(255,255,255,0.7);
            border-radius: var(--radius-md);
            padding: 1rem;
            border: 1px solid var(--border-soft);
        }}
        .dark-theme .calendar-month-container {{
            background: rgba(15, 23, 42, 0.4);
        }}
        .calendar-month-title {{
            font-size: 0.95rem;
            font-weight: 700;
            margin-bottom: 0.75rem;
            text-transform: capitalize;
            color: var(--text-primary);
            border-left: 3px solid var(--color-primary);
            padding-left: 8px;
        }}
        .calendar-grid-container {{
            display: grid;
            grid-template-columns: repeat(7, 1fr);
            gap: 6px;
            min-width: 280px;
        }}
        .calendar-day-header {{
            text-align: center;
            font-weight: 700;
            font-size: 0.75rem;
            color: var(--text-secondary);
            padding: 4px 0;
            text-transform: uppercase;
        }}
        .calendar-day-cell {{
            aspect-ratio: 1.25;
            border-radius: var(--radius-sm);
            padding: 6px;
            display: flex;
            flex-direction: column;
            justify-content: space-between;
            font-size: 0.7rem;
            cursor: pointer;
            transition: all 0.2s ease;
            border: 1px solid var(--border-soft);
            position: relative;
            background-color: var(--panel-bg);
            color: var(--text-primary);
        }}
        @media (max-width: 600px) {{
            .calendar-day-cell {{
                aspect-ratio: 1;
                padding: 4px;
            }}
        }}
        .calendar-day-cell:hover {{
            transform: translateY(-2px);
            box-shadow: 0 4px 12px rgba(0, 0, 0, 0.1);
            border-color: var(--color-primary);
        }}
        .calendar-day-cell.empty {{
            background: rgba(241, 245, 249, 0.5) !important;
            color: #cbd5e1;
            cursor: default;
            border: 1px dashed var(--border-soft);
        }}
        .dark-theme .calendar-day-cell.empty {{
            background: rgba(30, 41, 59, 0.3) !important;
            color: #475569;
        }}
        .calendar-day-cell.empty:hover {{
            transform: none;
            box-shadow: none;
            border-color: var(--border-soft);
        }}
        .calendar-day-number {{
            font-weight: 700;
        }}
        .calendar-day-price {{
            font-weight: 800;
            align-self: flex-end;
            font-size: 0.75rem;
        }}
        .calendar-day-cell.selected {{
            box-shadow: 0 0 0 3px var(--color-primary) !important;
            border-color: var(--color-primary) !important;
        }}
    </style>
    <!-- Cloudflare Web Analytics --><script defer src='https://static.cloudflareinsights.com/beacon.min.js' data-cf-beacon='&#123;&quot;token&quot;: &quot;1b9c3c0ee6164106a1cb5eda9e61a045&quot;&#125;'></script><!-- End Cloudflare Web Analytics -->
</head>
<body>
    <div class="app-topbar">
        <button class="sidebar-toggle" id="sidebarToggle" aria-label="Меню">☰</button>
        <button class="theme-toggle" id="themeToggle" aria-label="Тема">🌙</button>
    </div>
    <!-- Sidebar Navigation -->
    <div class="sidebar" id="sidebar">
        <div class="sidebar-header">
            <h2>🌍 Travel Monitor</h2>
        </div>
        <nav class="sidebar-nav">
            {sidebar_nav_html}
        </nav>
    </div>
    
    <!-- Sidebar Overlay -->
    <div class="sidebar-overlay" id="sidebarOverlay"></div>
    
    <!-- Main Content -->
    <div class="main-content" id="mainContent">
    <div class="container">
        <div class="hero--mockup">
            <div class="hero-header-mockup">
                <h1 class="hero-title-clean">{title}</h1>
            </div>

            <!-- Floating White Search Control Bar -->
            <div class="search-pill-bar">
                <div class="search-pill-item">
                    <span class="search-pill-icon">🧭</span>
                    <div class="search-pill-content">
                        <div class="search-pill-label">Откуда <span class="pill-arrow">⌄</span></div>
                        <div class="search-pill-val">RDO • WAW • WMI</div>
                    </div>
                </div>
                <div class="search-pill-divider"></div>
                <div class="search-pill-item">
                    <span class="search-pill-icon">📍</span>
                    <div class="search-pill-content">
                        <div class="search-pill-label">Куда <span class="pill-arrow">⌄</span></div>
                        <div class="search-pill-val">{dest_display_name}</div>
                    </div>
                </div>
                <div class="search-pill-divider"></div>
                <div class="search-pill-item">
                    <span class="search-pill-icon">📅</span>
                    <div class="search-pill-content">
                        <div class="search-pill-label">Даты <span class="pill-arrow">⌄</span></div>
                        <div class="search-pill-val">7-10 дней</div>
                    </div>
                </div>
                <div class="search-pill-divider"></div>
                <div class="search-pill-item">
                    <span class="search-pill-icon">👥</span>
                    <div class="search-pill-content">
                        <div class="search-pill-label">Туристы <span class="pill-arrow">⌄</span></div>
                        <div class="search-pill-val">2 взр. + 1 реб.</div>
                    </div>
                </div>
                <div class="search-pill-divider"></div>
                <div class="search-pill-item">
                    <span class="search-pill-icon">🏨</span>
                    <div class="search-pill-content">
                        <div class="search-pill-label">Отель <span class="pill-arrow">⌄</span></div>
                        <div class="search-pill-val">5★ + с питанием</div>
                    </div>
                </div>
                <div class="search-pill-divider"></div>
                <div class="search-pill-item">
                    <span class="search-pill-icon">👛</span>
                    <div class="search-pill-content">
                        <div class="search-pill-label">Бюджет <span class="pill-arrow">⌄</span></div>
                        <div class="search-pill-val">0-11k PLN</div>
                    </div>
                </div>
            </div>

            <!-- 4 White KPI Cards -->
            <div class="kpi-cards-grid">
                <div class="kpi-card-white">
                    <div class="kpi-card-icon-box">
                        <svg class="kpi-svg" viewBox="0 0 24 24"><path d="M16 21v-2a4 4 0 00-4-4H6a4 4 0 00-4 4v2M9 11a4 4 0 100-8 4 4 0 000 8z" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>
                    </div>
                    <div class="kpi-card-info">
                        <div class="kpi-card-number" id="heroKpiEntryCount">{entry_candidates_count}</div>
                        <div class="kpi-card-title">Кандидаты входа</div>
                    </div>
                </div>
                <div class="kpi-card-white">
                    <div class="kpi-card-icon-box">
                        <svg class="kpi-svg" viewBox="0 0 24 24"><path d="M23 18l-9.5-9.5-5 5L1 6" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><path d="M17 6h6v6" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>
                    </div>
                    <div class="kpi-card-info">
                        <div class="kpi-card-number" id="heroKpiBreadthPct">{market_breadth_pct_str}</div>
                        <div class="kpi-card-title">Рынок дешевеет</div>
                    </div>
                </div>
                <div class="kpi-card-white">
                    <div class="kpi-card-icon-box">
                        <svg class="kpi-svg" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" stroke-width="2"/><circle cx="12" cy="12" r="3" fill="currentColor"/></svg>
                    </div>
                    <div class="kpi-card-info">
                        <div class="kpi-card-number" id="heroKpiBestDeal">{best_deal_score_val}</div>
                        <div class="kpi-card-title">Лучший Deal Score</div>
                    </div>
                </div>
                <div class="kpi-card-white">
                    <div class="kpi-card-icon-box">
                        <svg class="kpi-svg" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" stroke-width="2"/><path d="M12 7v5l3 2" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>
                    </div>
                    <div class="kpi-card-info">
                        <div class="kpi-status-tag">
                            <span class="update-status {update_status_cls}" id="updateStatusBadge" title="{updated_date} {updated_time}" data-iso="{updated_iso}">
                                {update_status_icon} Обновлено
                            </span>
                        </div>
                        <div class="kpi-card-number kpi-card-time" id="updateAgoText">{updated_date} {updated_time}</div>
                    </div>
                </div>
            </div>
        </div>
"""

    from price_alerts_v2 import ALERT_THRESHOLD_PERCENT

    scope_hotel_names = set(df_canonical['hotel_name'].astype(str).tolist()) if not df_canonical.empty else set()
    alert_chips_html, alerts_content_html = _build_alerts_panel_html(
        alerts=alerts,
        table_prices=table_prices,
        premium_history_by_hotel=premium_history_by_hotel,
        scope_hotel_names=scope_hotel_names,
        hotel_meta_by_name=hotel_meta_by_name,
        latest_run_ts=df_canonical['scraped_at_display'].max() if not df_canonical.empty else None,
        ceiling_val=ceiling_val,
        alert_threshold_percent=ALERT_THRESHOLD_PERCENT,
        parse_iso_fn=parse_iso,
        scope_duration_bucket=default_trip_duration_bucket if use_trip_buckets else '',
    )

    alerts_html = f"""
        <div class="alerts-section" id="alertsSection">
            <div class="alerts-header" onclick="toggleAlerts()">
                <div class="alerts-header-main">
                    <h3>📊 Заметные изменения цен</h3>
                    <p class="alerts-lead">Отели, у которых <strong>заметно изменилась цена</strong> (от {ALERT_THRESHOLD_PERCENT:.0f}% между проверками или возврат из дорогого сегмента) и <strong>эта цена всё ещё актуальна</strong> — в последнем обновлении она не менялась. Прошлые события — в «Истории».</p>
                    <div class="alerts-summary-chips" id="alertsSummaryChips">{alert_chips_html}</div>
                </div>
                <span class="expand-icon collapsed" id="alertsExpandIcon">►</span>
            </div>
            <div class="alerts-content collapsed" id="alertsContent">
{alerts_content_html}
            </div>
        </div>
"""

    # Карточки отелей (визуальный режим)
    cards_inner_html = ""
    cards_html = """
        <div class="cards-section" id="cardsSection">
            <div class="cards-grid" id="cardsGrid">
"""
    for c in hotel_cards:
        img_html = (
            f'<img src="{html_lib.escape(c["image_url"], quote=True)}" alt="hotel image" loading="lazy" '
            f'onerror="this.onerror=null;this.parentElement.innerHTML=\'<div>Фото отеля</div>\';" />'
        ) if c["image_url"] else '<div>Фото отеля</div>'
        offer_btn = f'<a class="card-btn" href="{html_lib.escape(c["offer_url"], quote=True)}" target="_blank">Открыть оффер</a>' if c["offer_url"] else '<span class="card-btn" style="opacity:.6;">Оффер недоступен</span>'
        card_article = f"""
            <article class="hotel-card" data-duration-bucket="{html_lib.escape(c.get('duration_bucket', ''), quote=True)}" data-departure-date="{html_lib.escape(c.get('departure_date', ''), quote=True)}" data-departure-key="{html_lib.escape(c.get('departure_key', ''), quote=True)}">
                <div class="hotel-card-img">{img_html}<button class="watchlist-star-btn card-star" data-hotel-name="{html_lib.escape(c['hotel_name'], quote=True)}" title="Добавить в избранное">☆</button></div>
                <div class="hotel-card-body">
                    <h4 class="hotel-card-title">{c["hotel_name_html"]}</h4>
                    <div class="hotel-card-meta">
                        <div class="hotel-card-price">{c["price"]:.0f} PLN</div>
                        <span class="deal-pill {c["deal_class"]}">Deal {c["deal_score"]} • {c["deal_label"]}</span>
                        <span class="forecast-pill {c['forecast_class']}">{c['forecast_icon']} {c['forecast_text']}</span>
                    </div>
                    {c["comeback_html"]}
                    {c.get("cheaper_alt_html", "")}
                    <div class="hotel-card-stats">
                        <div>Δ48ч: <strong>{c["delta48"]}</strong></div>
                        <div>Δср: <strong>{c["delta_avg"]}</strong></div>
                        <div>{html_lib.escape(c["duration"])}</div>
                        <div>{html_lib.escape(c["confidence"])} confidence</div>
                    </div>
                    <div class="hotel-card-actions">
                        <a class="card-btn primary" href="{html_lib.escape(c["chart_href"], quote=True)}" target="_blank">График</a>
                        {offer_btn}
                    </div>
                </div>
            </article>
"""
        cards_html += card_article
        cards_inner_html += card_article

    cards_html += """
        </div>
            <div class="cards-pagination" id="cardsPagination">
                <button id="cardsPrevPage" disabled>← Предыдущая</button>
                <div class="cards-pagination-info">
                    Показано <span id="cardsShowingFrom">1</span>-<span id="cardsShowingTo">24</span> из <span id="cardsTotalItems">0</span>
                </div>
                <button id="cardsNextPage">Следующая →</button>
            </div>
        </div>
"""

    _t = timing_analysis if isinstance(timing_analysis, dict) else {}
    _conf_label = {"collecting": "Накопление", "preliminary": "Предварительно", "reliable": "Надёжно"}
    _conf_class = {"collecting": "timing-pill-collecting", "preliminary": "timing-pill-prelim", "reliable": "timing-pill-reliable"}
    _hist = _t.get("history", {}) or {}
    _reco = html_lib.escape(str(_t.get("recommendation", "") or ""))
    _dims = _t.get("dimensions", {}) or {}

    def _dim_badge(dim_key, name):
        dim = _dims.get(dim_key) or {}
        conf = dim.get("reached_confidence", "collecting")
        prog = int(round((dim.get("progress", 0) or 0) * 100))
        unit = dim.get("unit", "")
        return (
            f'<div class="timing-badge">'
            f'<span class="timing-badge-name">{html_lib.escape(name)}</span>'
            f'<span class="timing-pill {_conf_class.get(conf, "timing-pill-collecting")}">{_conf_label.get(conf, conf)}</span>'
            f'<span class="timing-badge-prog">{prog}% до надёжного ({unit})</span>'
            f'</div>'
        )

    if _t.get("available"):
        _span_txt = f'{_hist.get("first", "—")} → {_hist.get("last", "—")}'
        _days_txt = f'{_hist.get("days", 0)} дн., {_hist.get("weeks", 0)} нед.'
        _baseline_txt = f'{(_t.get("baseline_p_drop", 0) or 0) * 100:.1f}%'
        timing_inner_html = f"""
                <div class="timing-banner">
                    <div class="timing-banner-row">
                        <span class="timing-banner-label">История наблюдений</span>
                        <span class="timing-banner-value">{html_lib.escape(_span_txt)} ({html_lib.escape(_days_txt)})</span>
                    </div>
                    <div class="timing-banner-row">
                        <span class="timing-banner-label">Базовая вероятность снижения за интервал</span>
                        <span class="timing-banner-value">{_baseline_txt}</span>
                    </div>
                </div>
                <div class="timing-reco">💡 {_reco}</div>
                <div class="timing-badges">
                    {_dim_badge("hour", "Час дня")}
                    {_dim_badge("dow", "День недели")}
                    {_dim_badge("part", "Часть месяца")}
                    {_dim_badge("month", "Месяц")}
                </div>
                <div class="timing-grid">
                    <div class="timing-chart-card">
                        <h4>Вероятность снижения по часам дня (время Варшавы)</h4>
                        <div id="timingHourChart" style="height:300px;"></div>
                        <p class="timing-hint">Столбцы — доля интервалов со снижением цены; усы — 95% интервал Уилсона; пунктир — средний уровень. Бар «зажигается», только когда нижняя граница интервала выше среднего.</p>
                    </div>
                    <div class="timing-chart-card">
                        <h4>Вероятность снижения по дням недели</h4>
                        <div id="timingDowChart" style="height:300px;"></div>
                    </div>
                    <div class="timing-chart-card timing-chart-wide">
                        <h4>Интенсивность снижения: день недели × час</h4>
                        <div id="timingHeatmap" style="height:360px;"></div>
                        <p class="timing-hint">Ожидаемое снижение за интервал (вероятность × средний размер падения). Тёмные клетки — мало данных.</p>
                    </div>
                    <div class="timing-chart-card">
                        <h4>Вероятность снижения по части месяца</h4>
                        <div id="timingPartChart" style="height:280px;"></div>
                    </div>
                    <div class="timing-chart-card">
                        <h4>По месяцам <span class="timing-info-tag">справочно</span></h4>
                        <div id="timingMonthChart" style="height:280px;"></div>
                        <p class="timing-hint">Один сезон не доказывает месячную сезонность — нужны данные за несколько лет. Показано для общей картины.</p>
                    </div>
                </div>
"""
    else:
        timing_inner_html = f"""
                <div class="timing-reco">💡 {_reco or 'Накапливаем данные для анализа лучшего времени покупки.'}</div>
                <p class="timing-hint">Нужно минимум 2 последовательных замера одного и того же тура. По мере накопления ежечасной истории здесь появятся графики по часам дня, дням недели, частям месяца и месяцам.</p>
"""

    timing_section_html = f"""
        <details class="dashboard-fold" id="timingFold">
            <summary>
                <span>Когда покупать: статистика снижения цен</span>
                <span class="fold-title-meta">Время суток · день недели · часть месяца</span>
                <span class="fold-chevron">⌄</span>
            </summary>
            <div class="fold-content">
{timing_inner_html}
            </div>
        </details>
"""

    _price_scope_tip = (
        f"Таблица и алерты — только ≤{ceiling_val:.0f} PLN. "
        f"История и графики — до {history_val:.0f} PLN "
        f"(расширенная история {ceiling_val:.0f}–{history_val:.0f} PLN — на графиках и в выпавших)."
        if ceiling_val is not None and history_val is not None and history_val > ceiling_val
        else (
            f"Учитываются только цены до {ceiling_val:.0f} PLN — по одной самой дешёвой оферте отеля за каждый запуск проверки."
            if ceiling_val is not None
            else "По одной самой дешёвой оферте каждого отеля за каждый запуск проверки."
        )
    )
    avg_deal_score = (
        int(round(sum(v['score'] for v in deal_score_by_hotel.values()) / len(deal_score_by_hotel)))
        if deal_score_by_hotel else 0
    )
    best_deal_score = max((v['score'] for v in deal_score_by_hotel.values()), default=0)
    stats_metrics_row1 = "".join([
        _metric_card(
            f"{total_offers:,}",
            "Проверок цен",
            "Сколько раз мы замерили цены: одна запись = самая дешёвая оферта отеля за один автоматический опрос.",
        ),
        _metric_card(
            str(unique_hotels),
            "Отелей отслеживали",
            "Сколько разных отелей хотя бы раз попадали в этот фильтр (даты, цена, направление).",
        ),
        _metric_card(
            str(current_table_hotels),
            "Сейчас в списке",
            "Сколько отелей видно в таблице и карточках — только последний замер, актуальные предложения.",
        ),
        _metric_card(
            f"{avg_price:.0f} PLN",
            "Средняя цена",
            f"Средняя цена по всей накопленной истории. {_price_scope_tip}",
        ),
        _metric_card(
            f"{history_min_price:.0f} PLN",
            "Самая низкая",
            f"Минимальная цена за всю историю наблюдений. {_price_scope_tip}",
        ),
        _metric_card(
            f"{history_max_price:.0f} PLN",
            "Самая высокая",
            f"Максимальная цена за всю историю наблюдений. {_price_scope_tip}",
        ),
    ])
    stats_metrics_row2 = "".join([
        _metric_card(
            str(avg_deal_score),
            "Средняя выгодность",
            "Deal Score 0–100: насколько цены выгодны относительно истории каждого отеля. ~50 — обычный уровень, выше — лучше.",
        ),
        _metric_card(
            str(best_deal_score),
            "Лучший шанс",
            "Максимальный Deal Score среди отелей — самая сильная возможность купить выгодно прямо сейчас.",
        ),
        _metric_card(
            str(len(entry_candidates)),
            "Горячие точки",
            "Отели, которые заметно подешевели за ~48 часов и сейчас в нижней четверти своей исторической цены.",
        ),
        _metric_card(
            f"{market_breadth * 100:.0f}%",
            "Подешевело за 2 дня",
            "Доля отелей, у которых цена упала за последние ~48 часов. Высокий процент — рынок в целом дешевеет.",
        ),
    ])

    # 1. График ТОП-10 дешёвых предложений (всегда виден на главной странице)
    html_template += f"""
        <div class="avg-top10-section" style="margin-top:1.5rem; margin-bottom:1.5rem;">
            <h3>📉 Средняя цена ТОП‑10 дешёвых предложений</h3>
            <div id="avgTop10" style="height:300px;"></div>
        </div>

        <!-- --- Единая сворачиваемая панель расширенной аналитики (полноширинный fold) --- -->
        <details class="dashboard-fold" id="analyticsFold">
            <summary>
                <span>📊 Расширенная аналитика и тренды рынка</span>
                <span class="fold-title-meta">Вылеты · Алерты · Календарь · Тренды</span>
                <span class="fold-chevron">⌄</span>
            </summary>
            <div class="fold-content" style="padding-top: 1rem;">
                {departure_block_html}
                {departure_history_html}
                {alerts_html}
                <details class="dashboard-fold" id="calendarFold">
                    <summary>
                        <span>📅 Ценовой календарь по датам вылета</span>
                        <span class="fold-title-meta">Тепловая карта цен</span>
                        <span class="fold-chevron">⌄</span>
                    </summary>
                    <div class="fold-content">
                        <div class="calendar-section">
                            <h3>📅 Календарь минимальных цен по датам вылета</h3>
                            <p class="chart-section-note">Показывает минимальную цену предложения на каждую дату вылета. Зелёные ячейки соответствуют наиболее дешёвым датам. Нажмите на дату, чтобы отфильтровать отели ниже.</p>
                            <div id="calendarHeatmapWrapper" style="margin-top: 1rem; overflow-x: auto;"></div>
                        </div>
                    </div>
                </details>
                <details class="dashboard-fold" id="offersCountFold">
                    <summary>
                        <span>📈 Количество предложений по дням</span>
                        <span class="fold-title-meta">Доп. аналитика</span>
                        <span class="fold-chevron">⌄</span>
                    </summary>
                    <div class="fold-content">
                        <div class="avg-top10-section offers-count-section">
                            <h3>📈 Количество предложений по дням</h3>
                            <p class="chart-section-note">Одна точка в день — последний скрап за сутки{f" • видимая зона ≤{ceiling_val:.0f} PLN" if ceiling_val is not None else ""}</p>
                            <div id="offersCountChart" style="height:280px;"></div>
                        </div>
                    </div>
                </details>
                {timing_section_html}
                <details class="dashboard-fold" id="statsFold">
                    <summary>
                        <span>Статистика и сигналы</span>
                        <span class="fold-title-meta">Скрыто по умолчанию</span>
                        <span class="fold-chevron">⌄</span>
                    </summary>
                    <div class="fold-content">
                        <div class="metrics metrics-compact" id="statsMetricsRow1">
                            {stats_metrics_row1}
                        </div>
                        <div class="metrics metrics-compact" id="statsMetricsRow2">
                            {stats_metrics_row2}
                        </div>
                        <div id="durationScopedChanges">{changes_html}</div>
                        <div id="durationScopedEntry">{entry_signal_html}</div>
                    </div>
                </details>
            </div>
        </details>
"""

    # Блок выбора вида списка предложений и фильтры (всегда видны)
    top_movers_html = _render_top_movers_html(decreases_48h, increases_48h, slugify, _filter_data_id)

    # Адаптивные диапазоны фильтра по цене на основе фактических цен таблицы
    try:
        _pr = pd.to_numeric(all_hotels['price'], errors='coerce').dropna()
        _pr = _pr[_pr > 0]
    except Exception:
        _pr = pd.Series([], dtype='float64')
    arrival_hub_by_hotel = {}
    arrival_hub_labels = set()
    for _, _hotel_row in all_hotels.iterrows():
        _path = parse_offer_path(str(_hotel_row.get("offer_url") or ""))
        _hub_label = arrival_hub_label(_path.get("country"), _path.get("region"))
        _hotel_key = str(_hotel_row.get("row_id") or _hotel_row.get("hotel_name") or "")
        arrival_hub_by_hotel[_hotel_key] = _hub_label
        if _hub_label and _hub_label != "—":
            arrival_hub_labels.add(_hub_label)
    region_filter_options_html = "".join(
        f'<option value="{html_lib.escape(label)}">{html_lib.escape(label)}</option>'
        for label in sorted(arrival_hub_labels)
    )

    if len(_pr) >= 2:
        import math as _math
        _lo = float(_pr.quantile(0.02))
        _hi = float(_pr.quantile(0.98))
        if _hi - _lo < 500:
            _lo, _hi = float(_pr.min()), float(_pr.max())
        _step_base = 500
        _lo_r = int(_lo // _step_base) * _step_base
        _hi_r = int(_math.ceil(_hi / _step_base)) * _step_base
        if _hi_r <= _lo_r:
            _hi_r = _lo_r + _step_base
        _span = _hi_r - _lo_r
        _step = max(_step_base, int(round((_span / 4.0) / _step_base)) * _step_base)
        _edges = list(range(_lo_r, _hi_r + 1, _step))
        if len(_edges) < 2:
            _edges = [_lo_r, _hi_r]

        def _fmt(v):
            return f"{v:,}".replace(",", " ")

        _opts = ['<option value="">Все цены</option>']
        _opts.append(f'<option value="0-{_edges[1]}">До {_fmt(_edges[1])} PLN</option>')
        for _a, _b in zip(_edges[1:-1], _edges[2:]):
            _opts.append(f'<option value="{_a}-{_b}">{_fmt(_a)}–{_fmt(_b)} PLN</option>')
        _opts.append(f'<option value="{_edges[-1]}+">От {_fmt(_edges[-1])} PLN</option>')
        price_filter_options_html = "\n                    ".join(_opts)
    else:
        price_filter_options_html = '<option value="">Все цены</option>'

    html_template += f"""
        <div class="table-toolbar" id="modeSwitchRow">
            <div class="table-toolbar-title">Вид</div>
            <div class="mode-switch table-mode-switch" id="modeSwitch" data-mode="cards">
                <button type="button" class="mode-btn active" data-mode="cards">Карточки</button>
                <button type="button" class="mode-btn" data-mode="table">Таблица</button>
            </div>
        </div>

        <!-- Table & Cards Universal Filters -->
        <div class="table-filters" id="globalFilters" style="margin-bottom: 1.25rem;">
            <input type="text" class="filter-input" id="searchInput" placeholder="🔍 Поиск по отелям..." />
            <select class="filter-select" id="priceFilter">
                {price_filter_options_html}
            </select>
            <select class="filter-select" id="taFilter">
                <option value="">Все рейтинги TripAdvisor</option>
                <option value="4.5">Рейтинг ≥ 4.5</option>
                <option value="4.0">Рейтинг ≥ 4.0</option>
                <option value="3.5">Рейтинг ≥ 3.5</option>
                <option value="none">Без оценки</option>
            </select>
            <select class="filter-select" id="changeFilter">
                <option value="">Все изменения</option>
                <option value="decrease">Снижение цен</option>
                <option value="increase">Рост цен</option>
                <option value="stable">Стабильные</option>
            </select>
            <select class="filter-select" id="regionFilter">
                <option value="">Все регионы</option>
                {region_filter_options_html}
            </select>
            <button class="filter-button" id="watchlistToggle" style="padding: 0.75rem 1rem; background: #cbd5e1; color: #1e293b; border: none; border-radius: var(--radius-md); cursor: pointer; font-weight: 600; display: inline-flex; align-items: center; gap: 6px; transition: all 0.2s ease;">⭐ Избранные</button>
            <button class="filter-button" id="clearFilters" style="padding: 0.75rem 1rem; background: var(--gradient-primary); color: white; border: none; border-radius: var(--radius-md); cursor: pointer; font-weight: 600;">Очистить</button>
        </div>

        {cards_html}

        <div class="hotels-section full-width-table-section" id="tableSection" style="display:none;">
            <div class="table-header-row">
            <h3>🏨 Все отели • клик по отелю откроет график на отдельной странице</h3>
            </div>
            <div class="deal-legend">
                <strong>Как читать Deal Score:</strong>
                <span class="deal-badge-hot">🔥 Hot</span> = очень сильная возможность,
                <span class="deal-badge-good">✅ Good</span> = хороший момент,
                <span class="deal-badge-normal">↔️ Normal</span> = обычный уровень (~50),
                <span class="deal-badge-bad">📈 Bad</span> = подорожало за 48ч и vs средней,
                <span class="deal-badge-warm">⏳ Warm-up</span> = пока мало истории.
                Confidence: Low / Medium / High — степень надежности оценки.
                TripAdvisor слегка сдвигает Deal Score; без отзывов влияние минимально.
            </div>

            <div class="mobile-sort-bar" id="mobileSortBar" aria-label="Сортировка таблицы">
                <span class="mobile-sort-bar-label" id="mobileSortLabel">Сортировать по</span>
                <div class="mobile-sort-controls">
                    <select class="filter-select mobile-sort-select" id="mobileSortSelect" aria-labelledby="mobileSortLabel">
                        <option value="">Выберите поле…</option>
                        <option value="price">Цена</option>
                        <option value="deal">Deal Score</option>
                        <option value="delta48">Δ 48ч</option>
                        <option value="deltaavg">Δ к средней</option>
                        <option value="hotel">Отель</option>
                        <option value="region">Регион</option>
                        <option value="dates">Даты</option>
                        <option value="duration">Длительность</option>
                        <option value="ta">TripAdvisor</option>
                    </select>
                    <button type="button" class="mobile-sort-dir-btn" id="mobileSortDirection" disabled aria-label="Направление сортировки" title="По возрастанию">↑</button>
                </div>
            </div>
            
            <div class="table-scroll-hint">↔ Прокрутите таблицу вбок, чтобы увидеть все колонки</div>
            <div class="table-container table-container--hotels">
            <table class="hotels-table" id="hotelsTable">
                <colgroup>
                    <col class="col-w-hotel">
                    <col class="col-w-price">
                    <col class="col-w-deal">
                    <col class="col-w-forecast">
                    <col class="col-w-ta">
                    <col class="col-w-d48">
                    <col class="col-w-davg">
                    <col class="col-w-region">
                    <col class="col-w-dates">
                    <col class="col-w-dur">
                    <col class="col-w-link">
                </colgroup>
                <thead>
                    <tr>
                        <th class="sortable col-hotel" data-sort="hotel">Отель</th>
                        <th class="sortable col-tight" data-sort="price">Цена <span class="th-tip" data-tip="Стоимость тура за 2 взрослых + 1 ребёнок (PLN). Ниже — лучше.">ℹ</span></th>
                        <th class="sortable col-tight" data-sort="deal">Deal Score <span class="th-tip" data-tip="Оценка выгодности от 0 до 100. Учитывает историческую цену, тренд и рейтинг отеля. 80+ — сигнал к покупке.">ℹ</span></th>
                        <th class="sortable col-tight" data-sort="forecast">Прогноз <span class="th-tip" data-tip="Ожидаемое направление цены на ближайшие дни: рост, снижение или стабильно.">ℹ</span></th>
                        <th class="sortable col-tight th-ta col-hide-sm" data-sort="ta" title="Рейтинг на TripAdvisor">{TRIPADVISOR_HEADER_ICON_HTML}</th>
                        <th class="sortable col-tight th-col-d48" data-sort="delta48" style="text-align:center;">Δ 48ч <span class="th-tip" data-tip="Изменение цены за последние 48 часов. ↑ подорожал, ↓ подешевел, → без изменений.">ℹ</span></th>
                        <th class="sortable col-tight col-hide-sm th-col-davg" data-sort="deltaavg" style="text-align:center;" title="Отклонение от средней, взвешенной по длительности удержания каждой цены">Δ к средней <span class="th-tip" data-tip="Насколько текущая цена отличается от среднеисторической для этого отеля. Минус — дешевле обычного.">ℹ</span></th>
                        <th class="sortable col-tight" data-sort="region">Регион</th>
                        <th class="sortable col-tight col-dates" data-sort="dates">Даты</th>
                        <th class="sortable col-tight col-duration th-col-dur" data-sort="duration" style="text-align:center;">Длит.</th>
                        <th class="col-tight th-col-link" style="text-align:center;">Ссылка</th>
                    </tr>
                </thead>
                <tbody>"""

    # Добавляем строки таблицы
    table_rows_html_parts = []
    for i, (_, hotel) in enumerate(all_hotels.iterrows()):
        hotel_name = hotel['hotel_name']
        row_id = str(hotel.get('row_id') or hotel_name)
        duration_bucket = str(hotel.get('duration_bucket') or '')
        price = hotel['price']
        dates = hotel['dates'] if pd.notna(hotel['dates']) else '20-09-2025 - 04-10-2025'
        duration = hotel['duration'] if pd.notna(hotel['duration']) else '6-15 дней'
        
        # Δ 48ч
        delta_display = "—"
        delta_class = "delta flat"
        delta_info = deltas_by_hotel.get(row_id)
        if delta_info is not None:
            delta_abs, delta_pct = delta_info
            arrow = '↑' if delta_abs > 0 else ('↓' if delta_abs < 0 else '→')
            delta_class = 'delta up' if delta_abs > 0 else ('delta down' if delta_abs < 0 else 'delta flat')
            sign = '+' if delta_abs > 0 else ('' if delta_abs < 0 else '')
            delta_display = f"{arrow} {sign}{delta_pct:.1f}%"

        # Δ к time-weighted средней по полной истории отеля (без потолка показа)
        avg_display = "—"
        avg_info = avg_baseline_delta.get(row_id)
        avg_sort_value = 0
        if avg_info is not None:
            avg_abs, avg_pct = avg_info
            arrow2 = '↑' if avg_abs > 0 else ('↓' if avg_abs < 0 else '→')
            sign2 = '+' if avg_abs > 0 else ('' if avg_abs < 0 else '')
            avg_display = f"{arrow2} {sign2}{avg_pct:.1f}%"
            avg_sort_value = avg_pct

        hotel_slug = slugify(hotel_name)
        chart_href = _hotel_chart_viewer_href(_filter_data_id, hotel_slug)

        # Альтернативные аэропорты (если доступен датасет df_all_airports)
        cheaper_alt_html = ""
        if df_all_airports is not None and not df_all_airports.empty:
            cur_airport = hotel.get('from_airport')
            if not cur_airport or pd.isna(cur_airport) or not str(cur_airport).strip():
                cur_airport = extract_airport_from_url(hotel.get('offer_url') or hotel.get('url', ''))
            alts = find_cheaper_airport_alternatives(
                df_all_airports,
                hotel_name,
                dates,
                price,
                cur_airport,
            )
            if alts:
                best_alt = alts[0]
                alt_url = best_alt.get('url', '')
                alt_airport = html_lib.escape(str(best_alt['airport']))
                alt_price = best_alt['price']
                alt_savings = best_alt['savings']
                alt_savings_pct = best_alt['savings_percent']
                alt_title = html_lib.escape(
                    f"Вылет из {best_alt['airport']}: {alt_price:.0f} PLN (дешевле на {alt_savings:.0f} PLN / {alt_savings_pct:.1f}%)",
                    quote=True,
                )
                if alt_url:
                    cheaper_alt_html = f'<a href="{html_lib.escape(str(alt_url), quote=True)}" target="_blank" class="cheaper-alt-badge" title="{alt_title}">✈️ <span class="alt-label">{alt_airport}: {alt_price:.0f} PLN</span> <span class="alt-savings">(−{alt_savings:.0f} PLN)</span></a>'
                else:
                    cheaper_alt_html = f'<span class="cheaper-alt-badge" title="{alt_title}">✈️ <span class="alt-label">{alt_airport}: {alt_price:.0f} PLN</span> <span class="alt-savings">(−{alt_savings:.0f} PLN)</span></span>'

        # Ссылка на предложение
        offer_url = hotel.get('offer_url', '')
        offer_link_html = ""
        if offer_url and pd.notna(offer_url) and offer_url.strip():
            offer_link_html = f'<a href="{offer_url}" target="_blank" class="col-link-btn" title="Открыть предложение"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg></a>'
        else:
            offer_link_html = "—"
        
        deal_info = deal_score_by_hotel.get(row_id, {'score': 0, 'confidence': 'Low', 'samples': 0})
        deal_score = int(deal_info.get('score', 0))
        confidence_level = deal_info.get('confidence', 'Low')
        d48_tbl = float(delta_info[1]) if delta_info is not None else None
        d_avg_tbl = float(avg_info[1]) if avg_info is not None else None
        comeback_drop_tbl = deal_info.get('comeback_drop_pct')
        _, _, deal_badge = classify_deal_badge(
            deal_score, confidence_level, d48_tbl, d_avg_tbl, comeback_drop_tbl
        )
        confidence_short = (
            "Low" if confidence_level == "Low"
            else ("Med" if confidence_level == "Medium" else "High")
        )
        deal_title = html_lib.escape(f"{deal_score} {deal_badge} · {confidence_level}")
        duration_display = _table_duration_compact(duration)
        duration_title = html_lib.escape(str(duration))
        comeback = comeback_from_premium(price, premium_history_by_hotel.get(row_id), ceiling_val)
        if comeback:
            peak = float(comeback["peak_price"])
            drop = float(comeback["drop_from_peak_pct"])
            comeback_title = html_lib.escape(
                f"Было до {peak:.0f} PLN (−{drop:.0f}%)", quote=True
            )
            comeback_cell = (
                f'<span class="comeback-badge" title="{comeback_title}">'
                f"↩ −{drop:.0f}%</span>"
            )
        else:
            comeback_cell = ""
        arrival_hub = arrival_hub_by_hotel.get(row_id, "—")
        hotel_name_html = html_lib.escape(str(hotel_name))
        hotel_name_attr = html_lib.escape(str(hotel_name), quote=True)
        arrival_hub_html = html_lib.escape(str(arrival_hub))
        duration_bucket_attr = html_lib.escape(duration_bucket, quote=True)
        ta_rating_html = _render_ta_rating_html(
            hotel.get('ta_rating', ''),
            hotel.get('ta_review_count', ''),
        )
        ta_sort_val = _parse_ta_rating_value(hotel.get('ta_rating', ''))
        ta_data_attr = f"{ta_sort_val:.2f}" if ta_sort_val is not None else "-1"
        
        forecast = determine_price_forecast(
            deal_score, confidence_level, d_avg_tbl, d48_tbl, comeback_drop_tbl,
        )
        departure_date_attr = html_lib.escape(str(hotel.get('departure_date') or ''), quote=True)
        departure_key_attr = html_lib.escape(str(hotel.get('departure_key') or ''), quote=True)

        # Color-coded класс строки
        _d48 = float(delta_info[1]) if delta_info is not None else 0.0
        if _d48 > 8.0:
            row_color_class = ' row--rising'
        elif deal_score >= 80 and confidence_level != 'Low' and _d48 <= 0:
            row_color_class = ' row--at-min'
        else:
            row_color_class = ''

        table_rows_html_parts.append(f"""
                    <tr class="hotel-row {'row-odd' if i % 2 == 0 else 'row-even'}{row_color_class}" data-region="{arrival_hub_html}" data-ta-rating="{ta_data_attr}" data-duration-bucket="{duration_bucket_attr}" data-departure-date="{departure_date_attr}" data-departure-key="{departure_key_attr}">
                        <td class="hotel-name col-hotel" data-label="Отель"><button class="watchlist-star-btn" data-hotel-name="{hotel_name_attr}" title="Добавить в избранное">☆</button><a class="open-chart-link hotel-hover-link" href="{chart_href}" target="_blank" data-hotel-name="{hotel_name_attr}">{hotel_name_html}</a></td>
                        <td class="price col-tight" data-label="Цена" data-sort-value="{price}"><span class="price-main">{price:.0f} PLN</span>{comeback_cell}{cheaper_alt_html}</td>
                        <td class="col-tight col-w-deal-td" data-label="Deal" data-sort-value="{deal_score}" title="{deal_title}"><span class="deal-cell-inline">{deal_score} <span style="opacity:.85;">{deal_badge}</span> <span class="deal-conf-short">{confidence_short}</span></span></td>
                        <td class="col-tight col-w-forecast-td" data-label="Прогноз" data-sort-value="{forecast['text']}"><span class="forecast-pill {forecast['class']}">{forecast['icon']} {forecast['text']}</span></td>
                        <td class="col-tight col-w-ta-td col-hide-sm" data-label="TripAdvisor">{ta_rating_html}</td>
                        <td class="col-tight col-w-d48-td" data-label="Δ 48ч" data-sort-value="{delta_info[1] if delta_info else 0}"><span class="{delta_class}">{delta_display}</span></td>
                        <td class="col-tight col-w-davg-td col-hide-sm" data-label="Δ к средней" data-sort-value="{avg_sort_value}">{avg_display}</td>
                        <td class="arrival-hub col-tight" data-label="Регион" data-sort-value="{arrival_hub_html}">{arrival_hub_html}</td>
                        <td class="col-tight col-dates" data-label="Даты" data-sort-value="{dates}">{dates}</td>
                        <td class="col-tight col-w-dur-td col-duration" data-label="Длительность" data-sort-value="{duration}" title="{duration_title}">{duration_display}</td>
                        <td class="offer-link-cell col-tight" data-label="Ссылка">{offer_link_html}</td>
                    </tr>""")

    table_rows_html = "".join(table_rows_html_parts)
    html_template += table_rows_html

    if use_trip_buckets:
        from duration_view_bundle import (
            build_duration_view_bundle,
            duration_views_json,
        )

        duration_views_payload = {}
        for bucket in trip_buckets:
            bucket_id = str(bucket['id'])
            df_bucket = df_all_durations[
                df_all_durations['duration_bucket'].astype(str) == bucket_id
            ].copy()
            print(f"📏 Сбор представления: {bucket.get('label', bucket_id)}")
            bundle = build_duration_view_bundle(
                df_bucket,
                group_cols=['hotel_name'],
                use_trip_buckets=False,
                ceiling_val=ceiling_val,
                history_val=history_val,
                data_file=data_file,
                config_file=config_file,
                filter_data_id=_filter_data_id,
                price_scope_tip=_price_scope_tip,
                skip_ta_backfill=True,
                alerts=alerts,
                hotel_meta_by_name=hotel_meta_by_name,
                alert_threshold_percent=ALERT_THRESHOLD_PERCENT,
                parse_iso_fn=parse_iso,
                duration_bucket=bucket_id,
            )
            bundle['label'] = str(bucket.get('label') or bucket_id)
            duration_views_payload[bucket_id] = bundle

        duration_views_json_embed = (
            '<script type="application/json" id="durationViewsData">'
            + duration_views_json(duration_views_payload)
            + '</script>'
        )
        print(f"📏 Виртуальные фильтры: {len(duration_views_payload)}")

    # --- HTML секции "Выпавшие отели" ---
    def _fmt_dt(ts):
        try:
            return pd.to_datetime(ts).strftime('%d.%m.%Y %H:%M')
        except Exception:
            return '—'

    def _fmt_duration(hours):
        try:
            h = float(hours)
        except Exception:
            return '—'
        if h < 1:
            return '<1ч'
        if h < 48:
            return f'{int(round(h))}ч'
        return f'{h / 24:.1f}д'

    _reason_class = {
        'sold': 'vanished-reason-sold',
        'up': 'vanished-reason-up',
        'flat': 'vanished-reason-flat',
        'above_ceiling': 'vanished-reason-up',
    }
    vanished_rows_html = ""
    for ev in disappeared_events:
        ev_name = ev['hotel_name']
        ev_slug = slugify(ev_name)
        ev_chart_href = _hotel_chart_viewer_href(_filter_data_id, ev_slug)
        notable_badge = '<span class="vanished-badge">🔥 заметный дил</span>' if ev['notable'] else ''
        # Δ к своей средней (или к последней цене в фильтре, если отель сейчас дороже потолка)
        raw_price = ev.get('current_raw_price')
        if raw_price is not None and abs(float(raw_price) - ev['last_price']) > 1:
            raw_vs_filter = (float(raw_price) - ev['last_price']) / ev['last_price'] * 100.0 if ev['last_price'] else 0.0
            arrow = '↑' if raw_vs_filter > 0 else ('↓' if raw_vs_filter < 0 else '→')
            delta_cls = 'delta up' if raw_vs_filter > 0 else ('delta down' if raw_vs_filter < 0 else 'delta flat')
            avg_cell = f'<span class="{delta_cls}">{arrow} {raw_vs_filter:+.1f}%</span>'
            avg_cell += '<br><span style="opacity:.6;font-size:.78em;">vs последняя в фильтре</span>'
        elif ev['baseline_pct'] is not None:
            bp = ev['baseline_pct']
            arrow = '↓' if bp < 0 else ('↑' if bp > 0 else '→')
            delta_cls = 'delta down' if bp < 0 else ('delta up' if bp > 0 else 'delta flat')
            avg_cell = f'<span class="{delta_cls}">{arrow} {bp:+.1f}%</span>'
        else:
            avg_cell = '<span class="delta flat">—</span>'
        min_note = ''
        if ev['min_below_pct'] < -0.5:
            min_note = f'<br><span style="opacity:.6;font-size:.78em;">мин в фильтре: {ev["min_price"]:.0f} ({ev["min_below_pct"]:+.0f}%)</span>'
        if raw_price is not None and abs(float(raw_price) - ev['last_price']) > 1:
            raw_pct = (float(raw_price) - ev['last_price']) / ev['last_price'] * 100.0 if ev['last_price'] else 0.0
            raw_cls = 'up' if raw_pct > 0 else 'drop'
            price_cell = (
                f'<span style="opacity:.75">{ev["last_price"]:.0f}</span>'
                f' → <strong class="{raw_cls}">{float(raw_price):.0f}</strong> PLN'
                f'<br><span style="opacity:.6;font-size:.78em;">сейчас в выдаче ({raw_pct:+.0f}%)</span>'
                f'{min_note}'
            )
        else:
            price_cell = f'{ev["last_price"]:.0f} PLN{min_note}'
        airport_disp = html_lib.escape(str(ev['airport']).replace('%20', ' ')) if ev['airport'] else ''
        airport_html = f' <span style="opacity:.6;font-size:.78em;">{airport_disp}</span>' if airport_disp else ''
        first_seen_str = _fmt_dt(ev["first_seen"])
        last_seen_str = _fmt_dt(ev["last_seen"])
        visible_str = _fmt_duration(ev["visible_hours"])
        hotel_name_esc = html_lib.escape(str(ev_name))
        confidence_esc = html_lib.escape(str(ev['confidence']))
        reason_esc = html_lib.escape(str(ev['reason_text']))
        reason_cls = _reason_class.get(ev['reason_code'], 'vanished-reason-flat')
        ev_offer_url = str(ev.get('offer_url') or '').strip()
        if ev_offer_url:
            offer_url_esc = html_lib.escape(ev_offer_url, quote=True)
            offer_cell = f'<a href="{offer_url_esc}" target="_blank" rel="noopener" class="offer-link" title="Открыть оффер (может быть уже недоступен)">🔗</a>'
        else:
            offer_cell = '—'
        full_obs = ev.get('observations_full', ev['observations'])
        full_obs_suffix = f' · {full_obs} всего' if full_obs > ev['observations'] else ''
        seen_cell = (
            f'{first_seen_str} → {last_seen_str}'
            f'<br><span style="opacity:.6;font-size:.78em;">в фильтре ~{visible_str} · {ev["observations"]} набл.{full_obs_suffix}</span>'
        )
        vanished_rows_html += f"""
                    <tr>
                        <td class="hotel-name"><a class="open-chart-link" href="{ev_chart_href}" target="_blank">{hotel_name_esc}</a>{notable_badge}{airport_html}</td>
                        <td>{seen_cell}</td>
                        <td class="price">{price_cell}</td>
                        <td>{avg_cell}</td>
                        <td>{ev['deal_score']} <span style="opacity:.65;font-size:.78em;">{confidence_esc}</span></td>
                        <td><span class="vanished-reason {reason_cls}">{reason_esc}</span></td>
                        <td class="offer-link-cell">{offer_cell}</td>
                    </tr>"""

    if disappeared_events:
        if vanished_notable_count:
            vanished_summary_meta = f"заметных: {vanished_notable_count}"
        else:
            vanished_summary_meta = ""
        vanished_hint_text = (
            f"Все отели, которые хотя бы раз были в зоне отслеживания (≤{ceiling_val:.0f} PLN), но сейчас не в основной таблице. "
            f"Графики показывают полную историю до {history_val:.0f} PLN; если отель всё ещё в выдаче выше потолка — показываем актуальную цену."
            if ceiling_val is not None and history_val is not None
            else "Все отели, которые хотя бы раз были в выдаче, но сейчас не в основной таблице."
        )
        vanished_inner_html = f"""
                <p class="vanished-hint">{vanished_hint_text}</p>
                <div class="table-container">
                    <table class="hotels-table vanished-table">
                        <thead>
                            <tr>
                                <th>Отель</th>
                                <th>Был виден</th>
                                <th>Цена в фильтре → сейчас</th>
                                <th>Δ к своей средней</th>
                                <th>Deal Score</th>
                                <th>Что произошло</th>
                                <th>Оффер</th>
                            </tr>
                        </thead>
                        <tbody>{vanished_rows_html}
                        </tbody>
                    </table>
                </div>
"""
    else:
        vanished_empty_text = (
            f"Пока нет отелей вне основной таблицы: все, кто когда-либо был в зоне ≤{ceiling_val:.0f} PLN, сейчас в ней же."
            if ceiling_val is not None
            else "Пока нет отелей вне основной таблицы."
        )
        vanished_inner_html = f"""
                <p class="vanished-hint">{vanished_empty_text}</p>
"""
        vanished_summary_meta = "пусто"

    vanished_meta_html = f'<span class="fold-title-meta">{vanished_summary_meta}</span>'
    vanished_section_html = f"""
        <details class="dashboard-fold" id="vanishedFold">
            <summary>
                <span>Выпавшие отели — были в зоне, сейчас не в таблице ({len(disappeared_events)})</span>
                {vanished_meta_html}
                <span class="fold-chevron">⌄</span>
            </summary>
            <div class="fold-content">
{vanished_inner_html}
            </div>
        </details>
"""

    # Завершаем таблицу и добавляем секцию для графика
    html_template += f"""
                </tbody>
            </table>
            </div>
            
            <!-- Pagination -->
            <div class="pagination" id="pagination">
                <button id="prevPage" disabled>← Предыдущая</button>
                <div class="pagination-info">
                    Показано <span id="showingFrom">1</span>-<span id="showingTo">50</span> из <span id="totalItems">{len(all_hotels)}</span> отелей
                </div>
                <button id="nextPage">Следующая →</button>
            </div>
        </div>
{alerts_html}
{vanished_section_html}
{duration_views_json_embed}
        <div class="footer">
            <p>🤖 Автоматически обновляется каждый час • Powered by GitHub Actions</p>
        </div>
    </div>
    <div id="hoverThumb" class="hover-thumb"><img id="hoverImg" src="" alt="preview"/></div>
    <div id="departureOffersModal" class="departure-modal" aria-hidden="true">
        <div class="departure-modal-backdrop" id="departureModalBackdrop"></div>
        <div class="departure-modal-dialog" role="dialog" aria-modal="true" aria-labelledby="departureModalTitle">
            <div class="departure-modal-header">
                <h3 id="departureModalTitle">Отели по вылету</h3>
                <button type="button" class="departure-modal-close" id="departureModalClose" aria-label="Закрыть">×</button>
            </div>
            <p class="departure-modal-meta" id="departureModalMeta"></p>
            <p class="departure-modal-chart-title" id="departureModalChartTitle" style="display:none;">Динамика цен по вылету (D-14 → день вылета)</p>
            <div id="departureModalChart" class="departure-modal-chart" aria-hidden="true"></div>
            <div class="departure-modal-body" id="departureModalBody"></div>
        </div>
    </div>
"""

    # Вставляем скрипт превью слиянием JSON вне f-строки, чтобы избежать конфликтов с фигурными скобками
    html_template += """
    <script>
      (function(){
        const X = """ + json.dumps(top10_x_values, ensure_ascii=False) + """;
        const Y = """ + json.dumps(top10_y_values, ensure_ascii=False) + """;
        const minY = """ + json.dumps(top10_min_values, ensure_ascii=False) + """;
        const maxY = """ + json.dumps(top10_max_values, ensure_ascii=False) + """;
        const detailedData = """ + json.dumps(top10_detailed_data, ensure_ascii=False, default=str) + """;
        
        if (Array.isArray(X) && Array.isArray(Y) && X.length > 0 && Y.length > 0 && window.Plotly) {
          // Подготавливаем данные для hover
          const hoverData = detailedData.map(data => data.hover_data || {});
          
          // Создаем простой текст для hover с правильными переносами строк
          const hoverTexts = detailedData.map((data, index) => {
            const hover = data.hover_data || {};
            let text = hover.title || '';
            
            // Добавляем среднюю цену и диапазон
            if (hover.avg_price) {
              const minVal = Math.round(data.min_price || hover.avg_price);
              const maxVal = Math.round(data.max_price || hover.avg_price);
              const spread = maxVal - minVal;
              text += '<br><br><b>Средняя цена ТОП-10:</b> ' + Math.round(hover.avg_price) + ' PLN';
              text += `<br><b>Диапазон (№1 — №10):</b> ${minVal} — ${maxVal} PLN <span style="opacity:.7;font-size:.88em;">(разброс ${spread} PLN)</span>`;
            }
            
            if (hover.avg_change) {
              text += '<br><br><b>Изменение средней цены:</b><br>';
              text += `${hover.avg_change.arrow} ${hover.avg_change.sign}${Math.round(hover.avg_change.change)} PLN (${hover.avg_change.sign}${hover.avg_change.change_percent.toFixed(1)}%)`;
            }
            
            if (hover.price_changes && hover.price_changes.length > 0) {
              text += '<br><br><b>🏨 Изменения цен:</b><br>';
              hover.price_changes.forEach(change => {
                text += `• ${change.name}<br>  ${Math.round(change.old_price)} → ${Math.round(change.new_price)} PLN<br>  ${change.arrow} ${change.sign}${Math.round(change.change)} PLN (${change.sign}${change.change_percent.toFixed(1)}%)<br>`;
              });
            }
            
            if (hover.new_hotels && hover.new_hotels.length > 0) {
              text += '<br><b>🆕 Новые в ТОП-10:</b><br>';
              hover.new_hotels.forEach(hotel => {
                text += `• ${hotel.name}<br>  Цена: ${Math.round(hotel.price)} PLN (позиция ${hotel.position})<br>`;
              });
            }
            
            if (hover.removed_hotels && hover.removed_hotels.length > 0) {
              text += '<br><b>❌ Покинули ТОП-10:</b><br>';
              hover.removed_hotels.forEach(hotel => {
                text += `• ${hotel.name}<br>  Цена: ${Math.round(hotel.price)} PLN (была позиция ${hotel.position})<br>`;
              });
            }
            
            if (hover.no_changes) {
              text += '<br><br><i>Нет изменений в этом ране</i>';
            }
            
            return text;
          });

          const traceMin = {
            x: X,
            y: minY.length === X.length ? minY : Y,
            type: 'scatter',
            mode: 'lines',
            line: { width: 0 },
            showlegend: false,
            hoverinfo: 'skip'
          };

          const traceMax = {
            x: X,
            y: maxY.length === X.length ? maxY : Y,
            type: 'scatter',
            mode: 'lines',
            fill: 'tonexty',
            fillcolor: 'rgba(162, 59, 114, 0.14)',
            line: { width: 0 },
            name: 'Диапазон ТОП-10 (мин - макс)',
            hoverinfo: 'skip'
          };
          
          const traceAvg = { 
            x: X, 
            y: Y, 
            type: 'scatter', 
            mode: 'lines+markers', 
            line: { color: '#A23B72', width: 3 }, 
            marker: { size: 7, color: '#A23B72' },
            name: 'Средняя цена ТОП-10',
            text: hoverTexts,
            hovertemplate: '%{text}<extra></extra>',
            hoverinfo: 'text',
            hoverlabel: {
              bgcolor: 'rgba(248, 249, 250, 0.98)',
              bordercolor: '#A23B72',
              font: {
                family: 'Inter, sans-serif',
                size: 12,
                color: '#333'
              },
              align: 'left',
              namelength: -1
            }
          };
          
          const layout = { 
            margin: { t: 10, r: 10, b: 40, l: 50 }, 
            xaxis: { title: 'Время', type: 'date' }, 
            yaxis: { title: 'Цена (PLN)' },
            hovermode: 'closest',
            showlegend: false
          };
          
          Plotly.newPlot('avgTop10', [traceMin, traceMax, traceAvg], layout, { responsive: true, displayModeBar: false });
        }
      })();
      
      // График индекса ценовой динамики
      (function(){
        const trendIndexX = """ + json.dumps(trend_index_x_values, ensure_ascii=False) + """;
        const trendIndexY = """ + json.dumps(trend_index_y_values, ensure_ascii=False) + """;
        const trendIndexDetailedData = """ + json.dumps(trend_index_detailed_data, ensure_ascii=False, default=str) + """;
        
        if (document.getElementById('trendIndexChart') && Array.isArray(trendIndexX) && Array.isArray(trendIndexY) && trendIndexX.length > 0 && trendIndexY.length > 0 && window.Plotly) {
          // Создаем hover текст для каждой точки
          const trendIndexHoverTexts = trendIndexDetailedData.map((data, index) => {
            let text = `<b>📊 Индекс ценовой динамики</b><br>`;
            text += `<b>Время:</b> ${data.run_time}<br>`;
            text += `<b>Среднее изменение:</b> ${data.avg_change_pct.toFixed(2)}%<br>`;
            text += `<b>Отелей с изменениями:</b> ${data.hotels_with_changes}/${data.total_hotels}<br><br>`;
            
            if (data.price_changes && data.price_changes.length > 0) {
              text += `<b>Изменения по отелям:</b><br>`;
              data.price_changes.slice(0, 10).forEach(change => {
                const arrow = change.change_pct > 0 ? '↗️' : change.change_pct < 0 ? '↘️' : '➡️';
                const color = change.change_pct > 0 ? '#ef4444' : change.change_pct < 0 ? '#22c55e' : '#6b7280';
                text += `${arrow} <span style="color: ${color}">${change.hotel}: ${change.change_pct.toFixed(1)}%</span><br>`;
              });
              if (data.price_changes.length > 10) {
                text += `... и еще ${data.price_changes.length - 10} отелей`;
              }
            }
            
            return text;
          });
          
          const trendIndexTrace = {
            x: trendIndexX,
            y: trendIndexY,
            type: 'scatter',
            mode: 'lines+markers',
            name: 'Индекс ценовой динамики',
            line: { color: '#7C3AED', width: 3 },
            marker: { size: 6, color: '#7C3AED' },
            text: trendIndexHoverTexts,
            hovertemplate: '%{text}<extra></extra>',
            hoverinfo: 'text',
            hoverlabel: {
              bgcolor: 'rgba(248, 249, 250, 0.98)',
              bordercolor: '#7C3AED',
              font: { size: 12, color: '#333' },
              align: 'left',
              namelength: -1
            }
          };
          
          const trendIndexLayout = {
            title: {
              text: 'Индекс ценовой динамики (%)',
              font: { size: 16, color: '#374151' }
            },
            xaxis: {
              title: 'Время',
              type: 'date',
              gridcolor: '#e5e7eb',
              showgrid: true
            },
            yaxis: {
              title: 'Изменение цен (%)',
              gridcolor: '#e5e7eb',
              showgrid: true,
              zeroline: true,
              zerolinecolor: '#6b7280',
              zerolinewidth: 2
            },
            plot_bgcolor: 'rgba(0,0,0,0)',
            paper_bgcolor: 'rgba(0,0,0,0)',
            font: { family: 'Inter, sans-serif' },
            margin: { t: 50, b: 50, l: 60, r: 30 }
          };
          
          Plotly.newPlot('trendIndexChart', [trendIndexTrace], trendIndexLayout, { responsive: true, displayModeBar: false });
        }
      })();

        window.toggleAnalyticsDrawer = function() {
          const content = document.getElementById('analyticsDrawerContent');
          const arrow = document.getElementById('analyticsDrawerArrow');
          if (!content) return;
          if (content.style.display === 'none' || content.classList.contains('collapsed')) {
            content.style.display = 'block';
            content.classList.remove('collapsed');
            if (arrow) arrow.textContent = '▲ Скрыть аналитику';
          } else {
            content.style.display = 'none';
            content.classList.add('collapsed');
            if (arrow) arrow.textContent = '▼ Показать аналитику';
          }
        };

        (function(){
        const offersDates = """ + json.dumps(offers_count_dates, ensure_ascii=False) + """;
        const offersCounts = """ + json.dumps(offers_count_values, ensure_ascii=False) + """;
        const offersMeta = """ + json.dumps(offers_count_meta, ensure_ascii=False, default=str) + """;

        function buildOffersCountHoverTexts(meta) {
          return (meta || []).map((row) => {
            let text = '<b>' + (row.day || '') + '</b><br>';
            text += 'Предложений: <b>' + (row.count || 0) + '</b><br>';
            if (row.run_time) {
              text += 'Скрап: ' + row.run_time;
            }
            return text;
          });
        }

        window.renderOffersCountChart = function(dates, counts, meta) {
          const chartEl = document.getElementById('offersCountChart');
          if (!chartEl || !window.Plotly) return;
          const x = Array.isArray(dates) ? dates : [];
          const y = Array.isArray(counts) ? counts : [];
          if (!x.length || !y.length) {
            try { Plotly.purge(chartEl); } catch (e) {}
            chartEl.innerHTML = '<div style="padding:1rem;color:#64748b;font-size:.9rem;">Пока недостаточно истории по дням</div>';
            return;
          }
          try { Plotly.purge(chartEl); } catch (e) {}
          chartEl.innerHTML = '';

          const yNums = y.filter(function(v) { return v != null && !isNaN(v) && v > 0; });
          let yMin = yNums.length ? Math.min.apply(null, yNums) : 0;
          let yMax = yNums.length ? Math.max.apply(null, yNums) : 100;
          let ySpan = yMax - yMin;
          let yPad = Math.max(ySpan * 0.35, Math.ceil(yMax * 0.05), 3);
          const yRange = [Math.max(0, Math.floor(yMin - yPad)), Math.ceil(yMax + yPad)];

          const hoverTexts = buildOffersCountHoverTexts(meta);
          const trace = {
            x: x,
            y: y,
            type: 'scatter',
            mode: 'lines+markers',
            line: { color: '#059669', width: 2.5 },
            marker: { size: 7, color: '#059669' },
            text: hoverTexts,
            hovertemplate: '%{text}<extra></extra>',
            hoverinfo: 'text',
          };
          const layout = {
            margin: { t: 15, r: 15, b: 45, l: 52 },
            xaxis: { title: 'День', type: 'date', tickformat: '%d.%m' },
            yaxis: { title: 'Предложений', range: yRange, gridcolor: '#e5e7eb' },
            hovermode: 'closest',
            paper_bgcolor: 'rgba(0,0,0,0)',
            plot_bgcolor: 'rgba(0,0,0,0)',
          };
          const config = { responsive: true, displayModeBar: false };
          Plotly.newPlot('offersCountChart', [trace], layout, config);
        };

        window.renderOffersCountChart(offersDates, offersCounts, offersMeta);
      })();

      // Секция «Когда покупать»: статистика снижения цен по времени
      (function(){
        const TIMING = """ + timing_json + """;
        if (!TIMING || !TIMING.available || !window.Plotly) { return; }
        const baseline = (TIMING.baseline_p_drop || 0) * 100;
        const confColor = { reliable: '#10b981', preliminary: '#f59e0b', collecting: '#94a3b8' };
        const baseLayout = {
          plot_bgcolor: 'rgba(0,0,0,0)', paper_bgcolor: 'rgba(0,0,0,0)',
          font: { family: 'Inter, sans-serif', size: 11 },
          margin: { t: 10, r: 12, b: 40, l: 48 }
        };

        function drawProbBar(elId, dim, axisTitle) {
          if (!dim || !document.getElementById(elId)) { return; }
          const b = dim.buckets || [];
          const x = b.map(d => d.label);
          const y = b.map(d => (d.p_drop || 0) * 100);
          // Усы 95% Уилсона
          const errPlus = b.map(d => Math.max(0, (d.wilson_hi - d.p_drop)) * 100);
          const errMinus = b.map(d => Math.max(0, (d.p_drop - d.wilson_lo)) * 100);
          // Значимые бакеты «зажигаются» цветом достоверности, остальные приглушены
          const colors = b.map(d => d.significant ? confColor[d.confidence] : 'rgba(148,163,184,.45)');
          const text = b.map(d => d.n > 0 ? (d.n + ' набл.') : 'нет данных');
          const trace = {
            x: x, y: y, type: 'bar', marker: { color: colors },
            error_y: { type: 'data', symmetric: false, array: errPlus, arrayminus: errMinus, color: 'rgba(71,85,105,.55)', thickness: 1, width: 2 },
            text: text, textposition: 'none',
            hovertemplate: '%{x}<br>Снижение: %{y:.1f}%<br>%{text}<extra></extra>'
          };
          const layout = Object.assign({}, baseLayout, {
            yaxis: { title: 'P(снижение), %', gridcolor: '#e5e7eb' },
            xaxis: { title: axisTitle || '', tickangle: x.length > 8 ? -45 : 0 },
            shapes: [{ type: 'line', xref: 'paper', x0: 0, x1: 1, y0: baseline, y1: baseline, line: { color: '#ef4444', width: 1, dash: 'dot' } }],
            annotations: [{ xref: 'paper', x: 1, y: baseline, xanchor: 'right', yanchor: 'bottom', text: 'средн. ' + baseline.toFixed(1) + '%', showarrow: false, font: { size: 9, color: '#ef4444' } }]
          });
          Plotly.newPlot(elId, [trace], layout, { displayModeBar: false, responsive: true });
        }

        drawProbBar('timingHourChart', TIMING.dimensions.hour, 'Час (Варшава)');
        drawProbBar('timingDowChart', TIMING.dimensions.dow, 'День недели');
        drawProbBar('timingPartChart', TIMING.dimensions.part, 'Часть месяца');
        drawProbBar('timingMonthChart', TIMING.dimensions.month, 'Месяц');

        // Теплокарта: день недели × час
        const hm = TIMING.heatmap;
        if (hm && document.getElementById('timingHeatmap')) {
          const z = (hm.intensity || []).map(row => row.map(v => v === null ? null : v * 100));
          const customN = hm.n || [];
          const heat = {
            z: z, x: hm.hours.map(h => (h < 10 ? '0' + h : '' + h)), y: hm.dow_labels,
            type: 'heatmap', colorscale: 'YlGnBu', reversescale: true,
            customdata: customN,
            hovertemplate: '%{y}, %{x}:00<br>Ожид. снижение: %{z:.2f}%<br>Набл.: %{customdata}<extra></extra>',
            colorbar: { title: '%', thickness: 12 }
          };
          const layout = Object.assign({}, baseLayout, {
            margin: { t: 10, r: 12, b: 40, l: 48 },
            xaxis: { title: 'Час (Варшава)', dtick: 2 }, yaxis: { autorange: 'reversed' }
          });
          Plotly.newPlot('timingHeatmap', [heat], layout, { displayModeBar: false, responsive: true });
        }
      })();

      (function(){
        const map = """ + json.dumps(images_map, ensure_ascii=False) + """;
        try { Object.assign(map, JSON.parse(localStorage.getItem('hotel_images')||'{}')); } catch(e) {}
        const hover = document.getElementById('hoverThumb');
        const img = document.getElementById('hoverImg');
        let activeLink = null;
        function show(e, name){
          const url = map[name];
          if(!url || String(url).indexOf('data:image') === 0){ return; }
          img.src = url;
          hover.style.display = 'block';
          hover.style.left = ((e.pageX||0)+12) + 'px';
          hover.style.top = ((e.pageY||0)+12) + 'px';
        }
        function move(e){
          if(hover.style.display !== 'block'){ return; }
          hover.style.left = ((e.pageX||0)+12) + 'px';
          hover.style.top = ((e.pageY||0)+12) + 'px';
        }
        function hide(){ hover.style.display = 'none'; img.src = ''; activeLink = null; }
        document.addEventListener('mouseover', (e) => {
          const link = e.target.closest('.hotel-hover-link');
          if(!link || link === activeLink){ return; }
          activeLink = link;
          const name = link.getAttribute('data-hotel-name');
          if(name){ show(e, name); }
        });
        document.addEventListener('mouseout', (e) => {
          const link = e.target.closest('.hotel-hover-link');
          if(!link){ return; }
          const to = e.relatedTarget;
          if(to && link.contains(to)){ return; }
          hide();
        });
        document.addEventListener('mousemove', move);
        window._hoverPreview = { show, hide };
      })();
      
      function toggleAlerts() {
        const content = document.getElementById('alertsContent');
        const icon = document.getElementById('alertsExpandIcon');
        if (content.classList.contains('collapsed')) {
          content.classList.remove('collapsed');
          icon.classList.remove('collapsed');
        } else {
          content.classList.add('collapsed');
          icon.classList.add('collapsed');
        }
      }
      
      // Таблица сортировки
      let currentSort = { column: null, direction: 'asc' };
      
      function getColumnIndex(column) {
        const columnMap = { 'hotel': 0, 'price': 1, 'deal': 2, 'forecast': 3, 'ta': 4, 'delta48': 5, 'deltaavg': 6, 'region': 7, 'dates': 8, 'duration': 9, 'offer': 10 };
        return columnMap[column];
      }

      function normalizeSortValue(row, column) {
        const idx = getColumnIndex(column);
        const cell = row.cells[idx];
        const raw = (cell && (cell.dataset.sortValue || cell.textContent) || '').trim();
        if (column === 'forecast') {
          return raw === 'Покупать' ? 3 : (raw === 'Наблюдать' ? 2 : (raw === 'Дорого' ? 1 : 0));
        }
        if (column === 'hotel' || column === 'region') {
          return (row.cells[getColumnIndex(column)].textContent || '').trim().toLowerCase();
        }
        if (column === 'dates') {
          const m = raw.match(/(\d{2})\.(\d{2})\.(\d{4})/);
          return m ? `${m[3]}-${m[2]}-${m[1]}` : raw.toLowerCase();
        }
        if (column === 'duration') {
          const m = raw.match(/(\d+)/);
          return m ? parseFloat(m[1]) : 0;
        }
        if (column === 'ta') {
          const cellRating = cell ? cell.querySelector('.ta-rating') : null;
          const ds = cellRating ? cellRating.getAttribute('data-sort-value') : raw;
          const val = parseFloat(ds);
          return Number.isFinite(val) ? val : -1;
        }
        return parseFloat(raw) || 0;
      }

      function compareHotelRows(a, b) {
        const column = currentSort.column;
        const aVal = normalizeSortValue(a, column);
        const bVal = normalizeSortValue(b, column);
        let cmp;
        if (typeof aVal === 'string' || typeof bVal === 'string') {
          cmp = String(aVal).localeCompare(String(bVal));
        } else {
          cmp = aVal - bVal;
        }
        return currentSort.direction === 'asc' ? cmp : -cmp;
      }
      
      function syncMobileSortBar() {
        const sel = document.getElementById('mobileSortSelect');
        const dirBtn = document.getElementById('mobileSortDirection');
        if (!sel) return;
        sel.value = currentSort.column || '';
        if (dirBtn) {
          const asc = currentSort.direction !== 'desc';
          dirBtn.textContent = asc ? '↑' : '↓';
          dirBtn.title = asc ? 'По возрастанию — нажмите для убывания' : 'По убыванию — нажмите для возрастания';
          dirBtn.setAttribute('aria-label', asc ? 'По возрастанию' : 'По убыванию');
          dirBtn.disabled = !currentSort.column;
        }
      }

      function applyTableSort() {
        if (!currentSort.column) return;
        if (window._hotelTableSortAll) {
          window._hotelTableSortAll();
        } else {
          const table = document.getElementById('hotelsTable');
          if (!table) return;
          const tbody = table.querySelector('tbody');
          if (!tbody) return;
          const rows = Array.from(tbody.querySelectorAll('tr'));
          rows.sort(compareHotelRows);
          rows.forEach(row => tbody.appendChild(row));
        }
        updateSortIndicators();
      }

      function setTableSort(column, direction) {
        if (!column) {
          currentSort.column = null;
          currentSort.direction = 'asc';
          syncMobileSortBar();
          return;
        }
        currentSort.column = column;
        currentSort.direction = direction === 'desc' ? 'desc' : 'asc';
        applyTableSort();
        syncMobileSortBar();
      }
      
      function sortTable(column) {
        if (currentSort.column === column) {
          currentSort.direction = currentSort.direction === 'asc' ? 'desc' : 'asc';
        } else {
          currentSort.direction = 'asc';
        }
        currentSort.column = column;
        applyTableSort();
      }
      
      function updateSortIndicators() {
        const headers = document.querySelectorAll('#hotelsTable th.sortable');
        headers.forEach(header => {
          header.classList.remove('sort-asc', 'sort-desc');
          if (header.dataset.sort === currentSort.column) {
            header.classList.add(currentSort.direction === 'asc' ? 'sort-asc' : 'sort-desc');
          }
        });
        syncMobileSortBar();
      }
      
      function bindFoldPersistence(foldId, key, defaultOpen = false) {
        const el = document.getElementById(foldId);
        if (!el) return;
        let isOpen = defaultOpen;
        try {
          const saved = localStorage.getItem(key);
          if (saved === '1') isOpen = true;
          if (saved === '0') isOpen = false;
        } catch (e) {}
        el.open = isOpen;

        function triggerChartResize() {
          [40, 150, 350].forEach(delay => {
            setTimeout(() => {
              window.dispatchEvent(new Event('resize'));
              if (window.Plotly && window.Plotly.Plots) {
                document.querySelectorAll('.js-plotly-plot, #avgTop10, #offersCountChart, #timingHourChart, #timingDowChart, #timingHeatmap, #timingPartChart, #timingMonthChart, #departurePriceChart').forEach(p => {
                  try { window.Plotly.Plots.resize(p); } catch (e) {}
                });
              }
              if (typeof window.updateCalendarHeatmap === 'function') {
                window.updateCalendarHeatmap();
              }
            }, delay);
          });
        }

        if (isOpen) triggerChartResize();

        el.addEventListener('toggle', () => {
          try { localStorage.setItem(key, el.open ? '1' : '0'); } catch (e) {}
          if (el.open) {
            triggerChartResize();
          }
        });
      }

      document.addEventListener('toggle', function(event) {
        if (event.target && event.target.tagName === 'DETAILS' && event.target.open) {
          [40, 150, 350].forEach(function(delay) {
            setTimeout(function() {
              window.dispatchEvent(new Event('resize'));
              if (window.Plotly && window.Plotly.Plots) {
                document.querySelectorAll('.js-plotly-plot, #avgTop10, #offersCountChart, #timingHourChart, #timingDowChart, #timingHeatmap, #timingPartChart, #timingMonthChart, #departurePriceChart').forEach(function(p) {
                  try { window.Plotly.Plots.resize(p); } catch (e) {}
                });
              }
              if (typeof window.updateCalendarHeatmap === 'function') {
                window.updateCalendarHeatmap();
              }
            }, delay);
          });
        }
      }, true);
      
      function runOnReady(fn) {
        if (document.readyState === 'loading') {
          document.addEventListener('DOMContentLoaded', fn);
        } else {
          fn();
        }
      }

      // Добавляем обработчики кликов на заголовки
      runOnReady(function() {
        const headers = document.querySelectorAll('#hotelsTable th.sortable');
        headers.forEach(header => {
          header.addEventListener('click', () => sortTable(header.dataset.sort));
        });

        const mobileSortSelect = document.getElementById('mobileSortSelect');
        const mobileSortDirection = document.getElementById('mobileSortDirection');
        if (mobileSortSelect) {
          mobileSortSelect.addEventListener('change', function() {
            const col = mobileSortSelect.value;
            if (!col) {
              setTableSort(null);
              return;
            }
            const direction = currentSort.column === col ? currentSort.direction : 'asc';
            setTableSort(col, direction);
          });
        }
        if (mobileSortDirection) {
          mobileSortDirection.addEventListener('click', function() {
            if (!currentSort.column) return;
            setTableSort(
              currentSort.column,
              currentSort.direction === 'asc' ? 'desc' : 'asc'
            );
          });
        }
        syncMobileSortBar();
        
        // Sidebar functionality
        const sidebar = document.getElementById('sidebar');
        const sidebarOverlay = document.getElementById('sidebarOverlay');
        const mainContent = document.getElementById('mainContent');

        function toggleSidebar() {
          if (!sidebar || !sidebarOverlay || !mainContent) return;
          sidebar.classList.toggle('open');
          sidebarOverlay.classList.toggle('open');
          mainContent.classList.toggle('sidebar-open');
        }
        window.toggleSidebar = toggleSidebar;
        
        const sidebarToggle = document.getElementById('sidebarToggle');
        if (sidebarToggle) {
          sidebarToggle.onclick = function(e) {
            if (e) { e.preventDefault(); e.stopPropagation(); }
            toggleSidebar();
          };
        }
        if (sidebarOverlay) {
          sidebarOverlay.onclick = function(e) {
            if (e) { e.preventDefault(); e.stopPropagation(); }
            toggleSidebar();
          };
        }

        // Cards/Table view mode
        let refreshTableView = null;

        function setMode(mode) {
          const normalized = mode === 'table' ? 'table' : 'cards';
          const cardsMode = normalized !== 'table';
          const cardsEl = document.getElementById('cardsSection');
          const tableEl = document.getElementById('tableSection');
          const alertsEl = document.getElementById('alertsSection');
          const modeSwEl = document.getElementById('modeSwitch');

          if (cardsEl) cardsEl.style.display = cardsMode ? 'block' : 'none';
          if (tableEl) tableEl.style.display = cardsMode ? 'none' : 'block';
          if (alertsEl) alertsEl.style.display = 'block';
          if (modeSwEl) {
            modeSwEl.dataset.mode = normalized;
            modeSwEl.querySelectorAll('.mode-btn').forEach((btn) => {
              btn.classList.toggle('active', btn.dataset.mode === normalized);
            });
          }
          try { localStorage.setItem('dashboard_mode', normalized); } catch(e) {}
          if (!cardsMode && typeof refreshTableView === 'function') {
            refreshTableView();
          }
        }
        window.setDashboardMode = setMode;

        const modeSwitch = document.getElementById('modeSwitch');
        if (modeSwitch) {
          modeSwitch.querySelectorAll('.mode-btn').forEach((btn) => {
            btn.onclick = function(e) {
              if (e) { e.preventDefault(); e.stopPropagation(); }
              setMode(btn.dataset.mode || 'cards');
            };
          });
        }
        bindFoldPersistence('analyticsFold', 'dashboard_fold_analytics', false);
        bindFoldPersistence('calendarFold', 'dashboard_fold_calendar', false);
        bindFoldPersistence('offersCountFold', 'dashboard_fold_offers_count', false);
        bindFoldPersistence('trendFold', 'dashboard_fold_trend', false);
        bindFoldPersistence('timingFold', 'dashboard_fold_timing', false);
        bindFoldPersistence('statsFold', 'dashboard_fold_stats', false);
        bindFoldPersistence('departureHistoryFold', 'dashboard_fold_departure_history', false);
        bindFoldPersistence('vanishedFold', 'dashboard_fold_vanished', false);
        
        // Theme toggle functionality
        const themeToggle = document.getElementById('themeToggle');
        const body = document.body;
        
        // Load saved theme
        const savedTheme = localStorage.getItem('theme') || 'light';
        if (savedTheme === 'dark') {
          body.classList.add('dark-theme');
          if (themeToggle) themeToggle.textContent = '☀️';
        }
        
        if (themeToggle) themeToggle.addEventListener('click', function() {
          body.classList.toggle('dark-theme');
          const isDark = body.classList.contains('dark-theme');
          themeToggle.textContent = isDark ? '☀️' : '🌙';
          localStorage.setItem('theme', isDark ? 'dark' : 'light');
        });

        // ── Relative update time ──────────────────────────────────────────────
        function _updateRelativeTime() {
          const badge = document.getElementById('updateStatusBadge');
          const span  = document.getElementById('updateAgoText');
          if (!badge || !span) return;
          const iso = badge.getAttribute('data-iso');
          if (!iso) return;
          const diff = Math.max(0, Math.floor((Date.now() - new Date(iso).getTime()) / 1000));
          let label;
          if (diff < 60)        label = 'только что';
          else if (diff < 3600) label = Math.floor(diff / 60) + ' мин назад';
          else if (diff < 86400) {
            const h = Math.floor(diff / 3600);
            label = h + ' ч назад';
          } else {
            const d = Math.floor(diff / 86400);
            label = d + ' д назад';
          }
          span.textContent = label;
        }
        _updateRelativeTime();
        setInterval(_updateRelativeTime, 60000);

        // Table filtering and pagination
        const searchInput = document.getElementById('searchInput');
        const priceFilter = document.getElementById('priceFilter');
        const changeFilter = document.getElementById('changeFilter');
        const regionFilter = document.getElementById('regionFilter');
        const taFilter = document.getElementById('taFilter');
        const clearFilters = document.getElementById('clearFilters');
        const table = document.getElementById('hotelsTable');
        if (!table) {
          let initialMode = 'cards';
          try { initialMode = localStorage.getItem('dashboard_mode') || 'cards'; } catch(e) {}
          setMode(initialMode);
          return;
        }
        const tbody = table.querySelector('tbody');
        if (!tbody) {
          let initialMode = 'cards';
          try { initialMode = localStorage.getItem('dashboard_mode') || 'cards'; } catch(e) {}
          setMode(initialMode);
          return;
        }
        const rows = Array.from(tbody.querySelectorAll('tr'));
        const prevPage = document.getElementById('prevPage');
        const nextPage = document.getElementById('nextPage');
        const showingFrom = document.getElementById('showingFrom');
        const showingTo = document.getElementById('showingTo');
        const totalItems = document.getElementById('totalItems');
        
        let currentPage = 1;
        const itemsPerPage = 50;
        let filteredRows = [...rows];

        // Cards pagination
        const cardsGrid = document.querySelector('#cardsSection .cards-grid');
        const cardItems = cardsGrid ? Array.from(cardsGrid.querySelectorAll('.hotel-card')) : [];
        let filteredCards = [...cardItems];
        const cardsPrevPage = document.getElementById('cardsPrevPage');
        const cardsNextPage = document.getElementById('cardsNextPage');
        const cardsShowingFrom = document.getElementById('cardsShowingFrom');
        const cardsShowingTo = document.getElementById('cardsShowingTo');
        const cardsTotalItems = document.getElementById('cardsTotalItems');
        let cardsPage = 1;
        const cardsPerPage = 24;

        function updateCardsPagination() {
          const total = filteredCards.length;
          const totalPages = Math.max(1, Math.ceil(total / cardsPerPage));
          if (cardsPage > totalPages) cardsPage = totalPages;
          const start = (cardsPage - 1) * cardsPerPage;
          const end = start + cardsPerPage;

          cardItems.forEach((card) => {
            card.style.display = 'none';
          });
          filteredCards.slice(start, end).forEach((card) => {
            card.style.display = '';
          });

          if (cardsTotalItems) cardsTotalItems.textContent = String(total);
          if (cardsShowingFrom) cardsShowingFrom.textContent = total ? String(start + 1) : '0';
          if (cardsShowingTo) cardsShowingTo.textContent = total ? String(Math.min(end, total)) : '0';
          if (cardsPrevPage) cardsPrevPage.disabled = cardsPage <= 1;
          if (cardsNextPage) cardsNextPage.disabled = cardsPage >= totalPages;

          if (typeof window.syncWatchlistUI === 'function') {
            window.syncWatchlistUI();
          }
        }

        function cardsNextPageFunc() {
          const totalPages = Math.max(1, Math.ceil(filteredCards.length / cardsPerPage));
          if (cardsPage < totalPages) {
            cardsPage++;
            updateCardsPagination();
          }
        }

        function cardsPrevPageFunc() {
          if (cardsPage > 1) {
            cardsPage--;
            updateCardsPagination();
          }
        }
        
        function filterRows() {
          const searchTerm = searchInput.value.toLowerCase();
          const priceRange = priceFilter.value;
          const changeType = changeFilter.value;
          const regionValue = regionFilter ? regionFilter.value : '';
          const taValue = taFilter ? taFilter.value : '';
          const watchlistToggle = document.getElementById('watchlistToggle');
          const watchlistOnly = watchlistToggle ? watchlistToggle.classList.contains('active') : false;
          const watchlist = typeof window.getWatchlist === 'function' ? window.getWatchlist() : [];
          
          filteredRows = rows.filter(row => {
            const hotelName = (row.querySelector('.col-hotel a')?.textContent || row.querySelector('.col-hotel')?.textContent || row.cells[0]?.textContent || '').toLowerCase();
            const rawHotelName = (row.querySelector('.col-hotel a')?.getAttribute('data-hotel-name') || '').trim();
            const priceCell = row.querySelector('.price');
            const price = parseFloat((priceCell?.textContent || row.cells[1]?.textContent || '').replace(/[^0-9.-]/g, ''));
            const d48Cell = row.querySelector('.col-w-d48-td');
            const delta48 = (d48Cell?.textContent || row.cells[5]?.textContent || '').trim();
            const regionCell = row.querySelector('.arrival-hub');
            const rowRegion = row.dataset.region || (regionCell?.textContent || row.cells[7]?.textContent || '').trim();
            const rowTa = parseFloat(row.dataset.taRating || '-1');
            const rowDate = row.dataset.departureDate || '';
            
            if (searchTerm && !hotelName.includes(searchTerm)) {
              return false;
            }
            if (watchlistOnly && !watchlist.includes(rawHotelName)) {
              return false;
            }
            if (window.activeCalendarDate && rowDate !== window.activeCalendarDate) {
              return false;
            }
            
            // Price filter
            if (priceRange) {
              if (priceRange.endsWith('+')) {
                if (price < parseFloat(priceRange)) return false;
              } else {
                const parts = priceRange.split('-');
                const min = parseFloat(parts[0]);
                const max = parseFloat(parts[1]);
                if (price < min || price > max) return false;
              }
            }
            
            // Change filter
            if (changeType) {
              if (changeType === 'decrease' && !delta48.includes('-')) return false;
              if (changeType === 'increase' && !delta48.includes('+')) return false;
              if (changeType === 'stable' && delta48 !== '—') return false;
            }

            if (regionValue && rowRegion !== regionValue) {
              return false;
            }

            if (taValue) {
              if (taValue === 'none') {
                if (rowTa > 0) return false;
              } else {
                const minTa = parseFloat(taValue);
                if (!Number.isFinite(rowTa) || rowTa < minTa) return false;
              }
            }
            
            return true;
          });

          filteredCards = cardItems.filter((card) => {
            const title = card.querySelector('.hotel-card-title');
            const hotelName = title ? title.textContent.toLowerCase() : '';
            const rawHotelName = title ? title.textContent.trim() : '';
            const priceText = card.querySelector('.hotel-card-price')?.textContent || '';
            const price = parseFloat(priceText.replace(/[^0-9.-]/g, ''));
            const region = card.dataset.region || '';
            const cardDate = card.dataset.departureDate || '';
            const taValueAttr = card.dataset.taRating || card.dataset.ta || '-1';
            const rowTa = parseFloat(taValueAttr);
            const cardStats = card.querySelector('.hotel-card-stats');
            const delta48Text = cardStats ? cardStats.querySelector('div:first-child strong')?.textContent || '' : '';

            if (searchTerm && !hotelName.includes(searchTerm)) {
              return false;
            }
            if (watchlistOnly && !watchlist.includes(rawHotelName)) {
              return false;
            }
            if (window.activeCalendarDate && cardDate !== window.activeCalendarDate) {
              return false;
            }
            if (priceRange) {
              if (priceRange.endsWith('+')) {
                if (price < parseFloat(priceRange)) return false;
              } else {
                const parts = priceRange.split('-');
                const min = parseFloat(parts[0]);
                const max = parseFloat(parts[1]);
                if (price < min || price > max) return false;
              }
            }
            if (changeType) {
              if (changeType === 'decrease' && !delta48Text.includes('-')) return false;
              if (changeType === 'increase' && !delta48Text.includes('+')) return false;
              if (changeType === 'stable' && delta48Text !== '—') return false;
            }
            if (regionValue && region !== regionValue) {
              return false;
            }
            if (taValue) {
              if (taValue === 'none') {
                if (rowTa > 0) return false;
              } else {
                const minTa = parseFloat(taValue);
                if (isNaN(rowTa) || rowTa < minTa) return false;
              }
            }
            return true;
          });

          if (currentSort.column) {
            filteredRows.sort(compareHotelRows);
          }
          
          currentPage = 1;
          cardsPage = 1;
          updateTable();
          updateCardsPagination();

          if (typeof window.updateCalendarHeatmap === 'function') {
            window.updateCalendarHeatmap();
          }
        }
        
        function updateTable() {
          const startIndex = (currentPage - 1) * itemsPerPage;
          const endIndex = startIndex + itemsPerPage;
          const pageRows = filteredRows.slice(startIndex, endIndex);
          
          // Clear current rows
          tbody.innerHTML = '';
          
          // Add filtered rows with exact zebra striping classes
          pageRows.forEach((row, idx) => {
            row.classList.remove('row-even', 'row-odd');
            row.classList.add(idx % 2 === 1 ? 'row-even' : 'row-odd');
            tbody.appendChild(row);
          });
          
          // Update pagination info
          showingFrom.textContent = filteredRows.length > 0 ? startIndex + 1 : 0;
          showingTo.textContent = Math.min(endIndex, filteredRows.length);
          totalItems.textContent = filteredRows.length;
          
          // Update pagination buttons
          prevPage.disabled = currentPage === 1;
          nextPage.disabled = endIndex >= filteredRows.length;

          if (typeof window.syncWatchlistUI === 'function') {
            window.syncWatchlistUI();
          }
        }
        
        function nextPageFunc() {
          const maxPage = Math.ceil(filteredRows.length / itemsPerPage);
          if (currentPage < maxPage) {
            currentPage++;
            updateTable();
          }
        }
        
        function prevPageFunc() {
          if (currentPage > 1) {
            currentPage--;
            updateTable();
          }
        }

        window._hotelTableSortAll = function() {
          filteredRows.sort(compareHotelRows);
          currentPage = 1;
          updateTable();
        };
        
        window._hotelTableFilterRows = filterRows;
        window._rebindHotelTableRows = function() {
          const freshRows = Array.from(tbody.querySelectorAll('tr'));
          rows.length = 0;
          freshRows.forEach((row) => rows.push(row));
          filteredRows = [...rows];
          const freshCards = cardsGrid ? Array.from(cardsGrid.querySelectorAll('.hotel-card')) : [];
          cardItems.length = 0;
          freshCards.forEach((card) => cardItems.push(card));
          filteredCards = [...cardItems];
          currentPage = 1;
          cardsPage = 1;
          filterRows();
        };

        refreshTableView = function() {
          updateTable();
        };

        // Event listeners
        const watchlistToggle = document.getElementById('watchlistToggle');
        if (watchlistToggle) {
          watchlistToggle.addEventListener('click', function(event) {
            event.preventDefault();
            watchlistToggle.classList.toggle('active');
            if (watchlistToggle.classList.contains('active')) {
              watchlistToggle.style.background = 'var(--gradient-primary)';
              watchlistToggle.style.color = 'white';
            } else {
              watchlistToggle.style.background = '#cbd5e1';
              watchlistToggle.style.color = '#1e293b';
            }
            filterRows();
          });
        }

        if (searchInput) searchInput.addEventListener('input', filterRows);
        if (priceFilter) priceFilter.addEventListener('change', filterRows);
        if (changeFilter) changeFilter.addEventListener('change', filterRows);
        if (regionFilter) regionFilter.addEventListener('change', filterRows);
        if (taFilter) taFilter.addEventListener('change', filterRows);
        if (clearFilters) clearFilters.addEventListener('click', function() {
          if (searchInput) searchInput.value = '';
          if (priceFilter) priceFilter.value = '';
          if (changeFilter) changeFilter.value = '';
          if (regionFilter) regionFilter.value = '';
          if (taFilter) taFilter.value = '';
          if (watchlistToggle) {
            watchlistToggle.classList.remove('active');
            watchlistToggle.style.background = '#cbd5e1';
            watchlistToggle.style.color = '#1e293b';
          }
          window.activeCalendarDate = null;
          filterRows();
        });
        if (nextPage) nextPage.addEventListener('click', nextPageFunc);
        if (prevPage) prevPage.addEventListener('click', prevPageFunc);
        if (cardsNextPage) cardsNextPage.addEventListener('click', cardsNextPageFunc);
        if (cardsPrevPage) cardsPrevPage.addEventListener('click', cardsPrevPageFunc);
        
        // Initialize
        updateTable();
        updateCardsPagination();
        let initialMode = 'cards';
        try { initialMode = localStorage.getItem('dashboard_mode') || 'cards'; } catch(e) {}
        setMode(initialMode);
      });
    </script>
"""
    html_template += """
    <script>
      (function(){
        const departureOffers = """ + departure_offers_json + """;
        const departurePriceCurves = """ + departure_price_curves_json + """;
        window.departureOffers = departureOffers;

        // Watchlist helper functions
        function getWatchlist() {
          try {
            return JSON.parse(localStorage.getItem('watchlist_hotels') || '[]');
          } catch(e) {
            return [];
          }
        }
        function saveWatchlist(list) {
          try {
            localStorage.setItem('watchlist_hotels', JSON.stringify(list));
          } catch(e) {}
        }
        window.getWatchlist = getWatchlist;
        window.saveWatchlist = saveWatchlist;
        
        window.toggleWatchlist = function(hotelName) {
          const list = getWatchlist();
          const idx = list.indexOf(hotelName);
          if (idx > -1) {
            list.splice(idx, 1);
          } else {
            list.push(hotelName);
          }
          saveWatchlist(list);
          window.syncWatchlistUI();
          if (window._hotelTableFilterRows) {
            window._hotelTableFilterRows();
          }
        };

        window.syncWatchlistUI = function() {
          const list = getWatchlist();
          document.querySelectorAll('.watchlist-star-btn').forEach(btn => {
            const hotel = btn.getAttribute('data-hotel-name');
            if (list.includes(hotel)) {
              btn.classList.add('starred');
              btn.textContent = '★';
            } else {
              btn.classList.remove('starred');
              btn.textContent = '☆';
            }
          });
        };

        // Initialize Watchlist events
        window.initWatchlistEvents = function() {
          document.addEventListener('click', function(e) {
            const btn = e.target.closest('.watchlist-star-btn');
            if (btn) {
              e.preventDefault();
              e.stopPropagation();
              const hotel = btn.getAttribute('data-hotel-name');
              if (hotel) {
                window.toggleWatchlist(hotel);
              }
            }
          });
        };
        
        // Run once on load
        window.initWatchlistEvents();

        // Calendar Heatmap Implementation
        function getMonthName(yearMonthStr) {
          const [year, month] = yearMonthStr.split('-');
          const date = new Date(year, parseInt(month) - 1, 1);
          return date.toLocaleString('ru-RU', { month: 'long', year: 'numeric' });
        }

        window.updateCalendarHeatmap = function() {
          const wrapper = document.getElementById('calendarHeatmapWrapper');
          if (!wrapper || !window.departureOffers) return;

          const searchInput = document.getElementById('searchInput');
          const priceFilter = document.getElementById('priceFilter');
          const changeFilter = document.getElementById('changeFilter');
          const regionFilter = document.getElementById('regionFilter');
          const taFilter = document.getElementById('taFilter');
          const watchlistToggle = document.getElementById('watchlistToggle');
          
          const searchTerm = searchInput ? searchInput.value.toLowerCase() : '';
          const priceRange = priceFilter ? priceFilter.value : '';
          const changeType = changeFilter ? changeFilter.value : '';
          const regionValue = regionFilter ? regionFilter.value : '';
          const taValue = taFilter ? taFilter.value : '';
          const watchlistOnly = watchlistToggle ? watchlistToggle.classList.contains('active') : false;
          const watchlist = getWatchlist();

          const dateMinPrices = {};
          let overallMin = Infinity;
          let overallMax = -Infinity;

          for (let key in window.departureOffers) {
            const dep = window.departureOffers[key];
            const dateStr = dep.departure_date;
            if (!dateStr) continue;

            if (regionValue && dep.region !== regionValue) continue;

            dep.offers.forEach(offer => {
              if (searchTerm && !offer.hotel_name.toLowerCase().includes(searchTerm)) return;
              if (watchlistOnly && !watchlist.includes(offer.hotel_name)) return;

              const price = parseFloat(offer.price);
              if (priceRange) {
                if (priceRange.endsWith('+')) {
                  if (price < parseFloat(priceRange)) return;
                } else {
                  const parts = priceRange.split('-');
                  const min = parseFloat(parts[0]);
                  const max = parseFloat(parts[1]);
                  if (price < min || price > max) return;
                }
              }

              const rowTa = parseFloat(offer.ta_rating || '-1');
              if (taValue) {
                if (taValue === 'none') {
                  if (rowTa > 0) return;
                } else {
                  const minTa = parseFloat(taValue);
                  if (isNaN(rowTa) || rowTa < minTa) return;
                }
              }

              const delta48 = offer.delta_avg || '';
              if (changeType) {
                if (changeType === 'decrease' && !delta48.includes('-')) return;
                if (changeType === 'increase' && !delta48.includes('+')) return;
                if (changeType === 'stable' && delta48 !== '—') return;
              }

              if (!dateMinPrices[dateStr]) {
                dateMinPrices[dateStr] = { minPrice: Infinity, count: 0, best: null };
              }
              const dStats = dateMinPrices[dateStr];
              dStats.count++;
              if (price < dStats.minPrice) {
                dStats.minPrice = price;
                dStats.best = offer;
              }
            });
          }

          for (let dStr in dateMinPrices) {
            const price = dateMinPrices[dStr].minPrice;
            if (price < overallMin) overallMin = price;
            if (price > overallMax) overallMax = price;
          }

          const allDates = new Set();
          for (let key in window.departureOffers) {
            if (window.departureOffers[key].departure_date) {
              allDates.add(window.departureOffers[key].departure_date);
            }
          }
          if (allDates.size === 0) {
            wrapper.innerHTML = '<div style="color:var(--text-secondary);text-align:center;padding:1rem;">Нет доступных дат вылета</div>';
            return;
          }

          const sortedDates = Array.from(allDates).sort();
          const minDate = new Date(sortedDates[0]);
          const maxDate = new Date(sortedDates[sortedDates.length - 1]);

          const months = {};
          let curr = new Date(minDate.getFullYear(), minDate.getMonth(), 1);
          const endLimit = new Date(maxDate.getFullYear(), maxDate.getMonth() + 1, 1);

          while (curr < endLimit) {
            const year = curr.getFullYear();
            const month = String(curr.getMonth() + 1).padStart(2, '0');
            const key = `${year}-${month}`;
            
            const firstDay = new Date(year, curr.getMonth(), 1).getDay();
            const startOffset = firstDay === 0 ? 6 : firstDay - 1;
            const totalDays = new Date(year, curr.getMonth() + 1, 0).getDate();

            months[key] = {
              title: getMonthName(key),
              startOffset: startOffset,
              totalDays: totalDays,
              year: year,
              monthIndex: curr.getMonth()
            };

            curr.setMonth(curr.getMonth() + 1);
          }

          let html = '';
          for (let mKey in months) {
            const m = months[mKey];
            html += `<div class="calendar-month-container">`;
            html += `<div class="calendar-month-title">${m.title}</div>`;
            html += `<div class="calendar-grid-container">`;
            
            ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс'].forEach(day => {
              html += `<div class="calendar-day-header">${day}</div>`;
            });

            for (let i = 0; i < m.startOffset; i++) {
              html += `<div class="calendar-day-cell empty"></div>`;
            }

            for (let d = 1; d <= m.totalDays; d++) {
              const dayStr = `${m.year}-${String(m.monthIndex + 1).padStart(2, '0')}-${String(d).padStart(2, '0')}`;
              
              if (allDates.has(dayStr)) {
                const stats = dateMinPrices[dayStr];
                if (stats && stats.minPrice !== Infinity) {
                  let hue = 140;
                  if (overallMax > overallMin) {
                    const pct = (stats.minPrice - overallMin) / (overallMax - overallMin);
                    hue = 140 - Math.round(pct * 100);
                  }
                  
                  const isSelected = window.activeCalendarDate === dayStr ? ' selected' : '';
                  const tooltipText = `Дата: ${dayStr}\\nМинимальная цена: ${Math.round(stats.minPrice)} PLN\\nЛучший отель: ${stats.best.hotel_name}\\nВсего предложений: ${stats.count}`;

                  html += `
                    <div class="calendar-day-cell${isSelected}" 
                         style="background-color: hsla(${hue}, 80%, 40%, 0.7); color: #fff;" 
                         title="${escapeHtml(tooltipText)}"
                         onclick="window.selectCalendarDate('${dayStr}')">
                      <span class="calendar-day-number">${d}</span>
                      <span class="calendar-day-price">${Math.round(stats.minPrice)}</span>
                    </div>`;
                } else {
                  html += `
                    <div class="calendar-day-cell empty" title="Нет предложений по выбранным фильтрам на эту дату">
                      <span class="calendar-day-number">${d}</span>
                      <span style="font-size:0.6rem;align-self:flex-end;">—</span>
                    </div>`;
                }
              } else {
                html += `
                  <div class="calendar-day-cell empty">
                    <span class="calendar-day-number">${d}</span>
                  </div>`;
              }
            }

            const totalCells = m.startOffset + m.totalDays;
            const remaining = (7 - (totalCells % 7)) % 7;
            for (let i = 0; i < remaining; i++) {
              html += `<div class="calendar-day-cell empty"></div>`;
            }

            html += `</div></div>`;
          }

          wrapper.innerHTML = html;
        };

        window.selectCalendarDate = function(dateStr) {
          if (window.activeCalendarDate === dateStr) {
            window.activeCalendarDate = null;
          } else {
            window.activeCalendarDate = dateStr;
          }
          if (window._hotelTableFilterRows) {
            window._hotelTableFilterRows();
          }
        };

        function regionLabel(value) {
          return String(value || 'region').replace(/-/g, ' ').replace(/\\b\\w/g, function(c) { return c.toUpperCase(); });
        }

        function escapeHtml(value) {
          return String(value || '')
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
        }

        function renderDeparturePriceChart(key) {
          const chartEl = document.getElementById('departureModalChart');
          const chartTitleEl = document.getElementById('departureModalChartTitle');
          if (!chartEl) return;
          if (window.Plotly) {
            try { Plotly.purge(chartEl); } catch (e) {}
          }
          chartEl.innerHTML = '';
          const curve = departurePriceCurves[key];
          if (!curve || !curve.days || !curve.days.length || !window.Plotly) {
            chartEl.style.display = 'none';
            if (chartTitleEl) chartTitleEl.style.display = 'none';
            return;
          }
          if (chartTitleEl) chartTitleEl.style.display = 'block';
          const x = (curve.labels && curve.labels.length)
            ? curve.labels
            : curve.days.map(function(d) { return 'D-' + d; });
          const yVals = (curve.median_price || []).concat(curve.p10_price || []).filter(function(v) {
            return v != null && !isNaN(v) && v > 0;
          });
          let yRange = null;
          if (yVals.length) {
            const yMin = Math.min.apply(null, yVals);
            const yMax = Math.max.apply(null, yVals);
            const span = Math.max(yMax - yMin, yMax * 0.05, 500);
            const pad = span * 0.1;
            yRange = [Math.max(0, yMin - pad), yMax + pad];
          }
          const traces = [
            {
              x: x,
              y: curve.median_price,
              name: 'Типичная (медиана)',
              mode: 'lines+markers',
              line: { color: '#2563eb', width: 2.5 },
              marker: { size: 6 },
            },
            {
              x: x,
              y: curve.p10_price,
              name: 'Дешёвый сегмент (~10%)',
              mode: 'lines+markers',
              line: { color: '#16a34a', width: 2, dash: 'dot' },
              marker: { size: 5 },
            },
          ];
          const chartHeight = window.innerWidth <= 480 ? 240 : 300;
          const layout = {
            height: chartHeight,
            autosize: true,
            margin: { l: 52, r: 16, t: 44, b: 52 },
            paper_bgcolor: '#f8fafc',
            plot_bgcolor: '#ffffff',
            xaxis: {
              title: 'Дней до вылета',
              tickangle: -35,
              gridcolor: '#e2e8f0',
              automargin: true,
            },
            yaxis: {
              title: 'PLN',
              gridcolor: '#e2e8f0',
              automargin: true,
              range: yRange,
            },
            legend: { orientation: 'h', y: 1.18, x: 0 },
            hovermode: 'x unified',
          };
          Plotly.newPlot(chartEl, traces, layout, { responsive: true, displayModeBar: false });
          if (window.Plotly && window.Plotly.Plots) {
            window.Plotly.Plots.resize(chartEl);
          }
        }

        function openDepartureOffers(key) {
          const modal = document.getElementById('departureOffersModal');
          const titleEl = document.getElementById('departureModalTitle');
          const metaEl = document.getElementById('departureModalMeta');
          const bodyEl = document.getElementById('departureModalBody');
          if (!modal || !titleEl || !metaEl || !bodyEl) return;

          renderDeparturePriceChart(key);

          const payload = departureOffers[key];
          if (!payload) {
            titleEl.textContent = 'Отели по вылету';
            metaEl.textContent = 'Для этого вылета нет сохранённых предложений в истории scrape.';
            bodyEl.innerHTML = '<div class="departure-modal-empty">Архивный вылет: офферы показываются только если они есть в travel_prices / departure_offers. Для будущих вылетов список появится после ближайших проверок.</div>';
            modal.classList.add('open');
            modal.setAttribute('aria-hidden', 'false');
            return;
          }

          const region = regionLabel(payload.region);
          const nights = payload.nights ? payload.nights + ' ночей' : '';
          titleEl.textContent = region + ' · ' + (payload.departure_date || '—');
          metaEl.textContent = [
            payload.hub_subtitle || '',
            nights,
            payload.offers.length + ' отелей',
            payload.run_started_at ? ('снимок ' + String(payload.run_started_at).slice(0, 16)) : ''
          ].filter(Boolean).join(' · ');

          if (!payload.offers.length) {
            bodyEl.innerHTML = '<div class="departure-modal-empty">Нет предложений для этого вылета в выбранном снимке.</div>';
          } else {
            const rows = payload.offers.map(function(offer) {
              const dealHtml = offer.deal_has_data
                ? '<span class="deal-pill ' + escapeHtml(offer.deal_class || 'normal') + '">' + offer.deal_score + ' · ' + escapeHtml(offer.deal_label || 'Normal') + '</span>'
                : '<span class="departure-modal-empty">—</span>';
              const deltaAvg = offer.delta_avg || '—';
              const deltaCls = deltaAvg.startsWith('-') ? 'delta-drop' : (deltaAvg.startsWith('+') ? 'delta-up' : 'delta-flat');
              const actions = [];
              if (offer.chart_href) {
                actions.push('<a class="departure-offers-link secondary" href="' + escapeHtml(offer.chart_href) + '" target="_blank" rel="noopener">График</a>');
              }
              if (offer.offer_url) {
                actions.push('<a class="departure-offers-link" href="' + escapeHtml(offer.offer_url) + '" target="_blank" rel="noopener">Оффер</a>');
              }
              const actionsHtml = actions.length
                ? '<div class="departure-offers-actions">' + actions.join('') + '</div>'
                : '<span class="departure-modal-empty">—</span>';
              return '<tr>'
                + '<td><strong>' + escapeHtml(offer.hotel_name) + '</strong>'
                + (offer.dates ? '<br><span style="color:#64748b;font-size:.78rem;">' + escapeHtml(offer.dates) + '</span>' : '')
                + '</td>'
                + '<td class="price">' + Math.round(Number(offer.price || 0)) + ' PLN</td>'
                + '<td>' + dealHtml + '</td>'
                + '<td class="' + deltaCls + '">' + escapeHtml(deltaAvg) + '</td>'
                + '<td>' + actionsHtml + '</td>'
                + '</tr>';
            }).join('');
            bodyEl.innerHTML = '<div class="departure-modal-table-scroll"><table class="departure-offers-table"><thead><tr><th>Отель</th><th>Цена</th><th>Deal</th><th title="Отклонение от типичной цены отеля по истории вылетов">Δ типич.</th><th>Ссылки</th></tr></thead><tbody>' + rows + '</tbody></table></div>';
          }

          modal.classList.add('open');
          modal.setAttribute('aria-hidden', 'false');
          setTimeout(function() {
            const chartEl = document.getElementById('departureModalChart');
            if (chartEl && window.Plotly && window.Plotly.Plots) {
              try { window.Plotly.Plots.resize(chartEl); } catch (e) {}
            }
          }, 60);
        }

        function closeDepartureOffers() {
          const modal = document.getElementById('departureOffersModal');
          const chartEl = document.getElementById('departureModalChart');
          const chartTitleEl = document.getElementById('departureModalChartTitle');
          if (chartEl && window.Plotly) {
            try { Plotly.purge(chartEl); } catch (e) {}
            chartEl.innerHTML = '';
            chartEl.style.display = 'none';
          }
          if (chartTitleEl) chartTitleEl.style.display = 'none';
          if (!modal) return;
          modal.classList.remove('open');
          modal.setAttribute('aria-hidden', 'true');
        }

        function bindDepartureOfferClicks() {
          document.querySelectorAll('.departure-card-clickable, .departure-history-row').forEach(function(el) {
            el.addEventListener('click', function() {
              const key = el.getAttribute('data-departure-key');
              if (key) openDepartureOffers(key);
            });
            el.addEventListener('keydown', function(event) {
              if (event.key === 'Enter' || event.key === ' ') {
                event.preventDefault();
                const key = el.getAttribute('data-departure-key');
                if (key) openDepartureOffers(key);
              }
            });
          });

          const closeBtn = document.getElementById('departureModalClose');
          const backdrop = document.getElementById('departureModalBackdrop');
          if (closeBtn) closeBtn.addEventListener('click', closeDepartureOffers);
          if (backdrop) backdrop.addEventListener('click', closeDepartureOffers);
          document.addEventListener('keydown', function(event) {
            if (event.key === 'Escape') closeDepartureOffers();
          });
        }

        document.addEventListener('DOMContentLoaded', function() {
          bindDepartureOfferClicks();
          if (typeof window.updateCalendarHeatmap === 'function') window.updateCalendarHeatmap();
          if (typeof window.syncWatchlistUI === 'function') window.syncWatchlistUI();
        });
        if (document.readyState !== 'loading') {
          bindDepartureOfferClicks();
          if (typeof window.updateCalendarHeatmap === 'function') window.updateCalendarHeatmap();
          if (typeof window.syncWatchlistUI === 'function') window.syncWatchlistUI();
        }
      })();
    </script>
    <script>
      (function() {
        const dataEl = document.getElementById('durationViewsData');
        const switchRoot = document.getElementById('durationGlobalSwitch');
        if (!dataEl || !switchRoot) return;

        let views = {};
        try {
          views = JSON.parse(dataEl.textContent || '{}');
        } catch (err) {
          console.warn('duration views parse failed', err);
          return;
        }

        function buildTop10HoverTexts(detailedData) {
          return (detailedData || []).map((data) => {
            const hover = (data && data.hover_data) || {};
            let text = hover.title || '';
            if (hover.avg_price) {
              text += '<br><br><b>Средняя цена:</b><br>' + Math.round(hover.avg_price) + ' PLN';
            }
            if (hover.avg_change) {
              text += '<br><br><b>Изменение средней цены:</b><br>';
              text += hover.avg_change.arrow + ' ' + hover.avg_change.sign
                + Math.round(hover.avg_change.change) + ' PLN ('
                + hover.avg_change.sign + hover.avg_change.change_percent.toFixed(1) + '%)';
            }
            if (hover.price_changes && hover.price_changes.length) {
              text += '<br><br><b>🏨 Изменения цен:</b><br>';
              hover.price_changes.forEach((change) => {
                text += '• ' + change.name + '<br>  ' + Math.round(change.old_price)
                  + ' → ' + Math.round(change.new_price) + ' PLN<br>  '
                  + change.arrow + ' ' + change.sign + Math.round(change.change) + ' PLN ('
                  + change.sign + change.change_percent.toFixed(1) + '%)<br>';
              });
            }
            if (hover.new_hotels && hover.new_hotels.length) {
              text += '<br><b>🆕 Новые в ТОП-10:</b><br>';
              hover.new_hotels.forEach((hotel) => {
                text += '• ' + hotel.name + '<br>  Цена: ' + Math.round(hotel.price)
                  + ' PLN (позиция ' + hotel.position + ')<br>';
              });
            }
            if (hover.removed_hotels && hover.removed_hotels.length) {
              text += '<br><b>❌ Покинули ТОП-10:</b><br>';
              hover.removed_hotels.forEach((hotel) => {
                text += '• ' + hotel.name + '<br>  Цена: ' + Math.round(hotel.price)
                  + ' PLN (была позиция ' + hotel.position + ')<br>';
              });
            }
            if (hover.no_changes) {
              text += '<br><br><i>Нет изменений в этом ране</i>';
            }
            return text;
          });
        }

        function emptyChartLayout(kind) {
          const annotation = {
            text: 'Нет данных для этого фильтра',
            showarrow: false,
            xref: 'paper',
            yref: 'paper',
            x: 0.5,
            y: 0.5,
            font: { size: 14, color: '#94a3b8' },
          };
          if (kind === 'trend') {
            return {
              margin: { t: 10, r: 10, b: 40, l: 50 },
              xaxis: { title: 'Время', type: 'date' },
              yaxis: { title: 'Изменение, %' },
              hovermode: 'closest',
              annotations: [annotation],
            };
          }
          return {
            margin: { t: 10, r: 10, b: 40, l: 50 },
            xaxis: { title: 'Время', type: 'date' },
            yaxis: { title: 'Цена (PLN)' },
            hovermode: 'closest',
            annotations: [annotation],
          };
        }

        window.renderDashboardDurationCharts = function(charts) {
          if (!window.Plotly || !charts) return;
          const X = charts.top10_x || [];
          const Y = charts.top10_y || [];
          const minY = charts.top10_min || [];
          const maxY = charts.top10_max || [];
          const detailedData = charts.top10_detailed || [];
          if (X.length && Y.length) {
            const hoverTexts = buildTop10HoverTexts(detailedData);
            const traceMin = {
              x: X,
              y: minY.length === X.length ? minY : Y,
              type: 'scatter',
              mode: 'lines',
              line: { width: 0 },
              showlegend: false,
              hoverinfo: 'skip'
            };
            const traceMax = {
              x: X,
              y: maxY.length === X.length ? maxY : Y,
              type: 'scatter',
              mode: 'lines',
              fill: 'tonexty',
              fillcolor: 'rgba(162, 59, 114, 0.14)',
              line: { width: 0 },
              name: 'Диапазон ТОП-10 (мин - макс)',
              hoverinfo: 'skip'
            };
            const traceAvg = {
              x: X,
              y: Y,
              type: 'scatter',
              mode: 'lines+markers',
              line: { color: '#A23B72', width: 3 },
              marker: { size: 7, color: '#A23B72' },
              name: 'Средняя цена ТОП-10',
              text: hoverTexts,
              hovertemplate: '%{text}<extra></extra>',
              hoverinfo: 'text',
            };
            Plotly.react('avgTop10', [traceMin, traceMax, traceAvg], {
              margin: { t: 10, r: 10, b: 40, l: 50 },
              xaxis: { title: 'Время', type: 'date' },
              yaxis: { title: 'Цена (PLN)' },
              hovermode: 'closest',
              showlegend: false
            }, { responsive: true, displayModeBar: false });
          } else {
            Plotly.react('avgTop10', [], emptyChartLayout('top10'));
          }
          const trendEl = document.getElementById('trendIndexChart');
          if (trendEl && window.Plotly) {
            const trendX = charts.trend_x || [];
            const trendY = charts.trend_y || [];
            const trendDetailed = charts.trend_detailed || [];
            if (trendX.length && trendY.length) {
              const trendHoverTexts = trendDetailed.map((data) => {
                let text = '<b>' + (data.run_time || '') + '</b><br>';
                text += 'Среднее изменение: ' + (data.avg_change_pct || 0).toFixed(2) + '%<br>';
                text += 'Отелей с изменением: ' + (data.hotels_with_changes || 0)
                  + ' / ' + (data.total_hotels || 0);
                return text;
              });
              Plotly.react('trendIndexChart', [{
                x: trendX,
                y: trendY,
                type: 'scatter',
                mode: 'lines+markers',
                line: { color: '#2E86AB', width: 2 },
                marker: { size: 7 },
                text: trendHoverTexts,
                hovertemplate: '%{text}<extra></extra>',
                hoverinfo: 'text',
              }], {
                margin: { t: 10, r: 10, b: 40, l: 50 },
                xaxis: { title: 'Время', type: 'date' },
                yaxis: { title: 'Изменение, %' },
                hovermode: 'closest',
              }, { responsive: true, displayModeBar: false });
            } else {
              Plotly.react('trendIndexChart', [], emptyChartLayout('trend'));
            }
          }
          if (typeof window.renderOffersCountChart === 'function') {
            window.renderOffersCountChart(
              charts.offers_count_dates || [],
              charts.offers_count_values || [],
              charts.offers_count_meta || []
            );
          }
        };

        function formatMetricValue(kind, value) {
          if (kind === 'total_offers') return Number(value).toLocaleString('ru-RU');
          if (kind === 'avg_price' || kind === 'history_min_price' || kind === 'history_max_price') {
            return Number(value).toLocaleString('ru-RU') + ' PLN';
          }
          if (kind === 'market_breadth_pct') return Number(value).toFixed(0) + '%';
          return String(value);
        }

        const storageKey = 'tripDuration:' + window.location.pathname;
        const baseTitle = document.title;

        function resolveBucketId(preferred) {
          const candidate = String(preferred || '').trim();
          if (candidate && views[candidate]) return candidate;
          const fallback = String(switchRoot.dataset.defaultBucket || '').trim();
          if (fallback && views[fallback]) return fallback;
          const keys = Object.keys(views);
          return keys.length ? keys[0] : '';
        }

        window.applyDurationView = function(bucketId) {
          const resolvedId = resolveBucketId(bucketId);
          const view = views[resolvedId];
          if (!view) {
            console.warn('duration view missing:', resolvedId);
            switchRoot.querySelectorAll('.duration-global-btn').forEach((btn) => {
              btn.classList.toggle('active', btn.dataset.durationBucket === resolvedId);
            });
            const cardsGrid = document.getElementById('cardsGrid');
            if (cardsGrid) cardsGrid.innerHTML = '';
            const tbody = document.querySelector('#hotelsTable tbody');
            if (tbody) tbody.innerHTML = '';
            window.__activeDurationHotelNames = new Set();
            try {
              localStorage.setItem(storageKey, resolvedId);
            } catch (err) {
              /* ignore */
            }
            if (location.hash !== '#' + resolvedId) {
              history.replaceState(null, '', '#' + resolvedId);
            }
            if (typeof window._hotelTableFilterRows === 'function') {
              window._hotelTableFilterRows();
            }
            window.renderDashboardDurationCharts({});
            return;
          }

          const hero = view.hero || {};
          const stats = view.stats || {};
          const html = view.html || {};
          const entryEl = document.getElementById('heroKpiEntryCount');
          const breadthEl = document.getElementById('heroKpiBreadthPct');
          const bestDealEl = document.getElementById('heroKpiBestDeal');
          if (entryEl) entryEl.textContent = String(hero.entry_candidates ?? '');
          if (breadthEl) breadthEl.textContent = formatMetricValue('market_breadth_pct', hero.market_breadth_pct ?? 0);
          if (bestDealEl) bestDealEl.textContent = String(hero.best_deal_score ?? '');

          const statsRow1 = document.getElementById('statsMetricsRow1');
          const statsRow2 = document.getElementById('statsMetricsRow2');
          if (statsRow1 && view.stats_row1_html) statsRow1.innerHTML = view.stats_row1_html;
          if (statsRow2 && view.stats_row2_html) statsRow2.innerHTML = view.stats_row2_html;

          const changesEl = document.getElementById('durationScopedChanges');
          const entrySignalEl = document.getElementById('durationScopedEntry');
          if (changesEl) changesEl.innerHTML = html.changes || '';
          if (entrySignalEl) entrySignalEl.innerHTML = html.entry_signal || '';

          const alertsChipsEl = document.getElementById('alertsSummaryChips');
          const alertsContentEl = document.getElementById('alertsContent');
          if (alertsChipsEl) alertsChipsEl.innerHTML = html.alerts_chips || '';
          if (alertsContentEl) alertsContentEl.innerHTML = html.alerts_content || '';

          const cardsGrid = document.getElementById('cardsGrid');
          if (cardsGrid) cardsGrid.innerHTML = html.cards || '';

          const tbody = document.querySelector('#hotelsTable tbody');
          if (tbody) {
            tbody.innerHTML = html.table_rows || '';
            if (typeof window._rebindHotelTableRows === 'function') {
              window._rebindHotelTableRows();
            }
          }

          const priceFilter = document.getElementById('priceFilter');
          const regionFilter = document.getElementById('regionFilter');
          if (priceFilter && html.price_filter_options) {
            priceFilter.innerHTML = html.price_filter_options;
          }
          if (regionFilter && html.region_filter_options !== undefined) {
            regionFilter.innerHTML = '<option value="">Все регионы</option>' + (html.region_filter_options || '');
          }

          window.renderDashboardDurationCharts(view.charts || {});
          window.__activeDurationHotelNames = new Set((view.hotel_names || []).map(String));

          switchRoot.querySelectorAll('.duration-global-btn').forEach((btn) => {
            btn.classList.toggle('active', btn.dataset.durationBucket === resolvedId);
          });

          if (view.label) {
            const suffix = ' • ' + view.label;
            document.title = baseTitle.includes(suffix)
              ? baseTitle
              : baseTitle.replace(/ • [^•]+$/, '') + suffix;
          }

          try {
            localStorage.setItem(storageKey, resolvedId);
          } catch (err) {
            /* ignore */
          }
          if (location.hash !== '#' + resolvedId) {
            history.replaceState(null, '', '#' + resolvedId);
          }

          if (typeof window._hotelTableFilterRows === 'function') {
            window._hotelTableFilterRows();
          }
        };

        switchRoot.querySelectorAll('.duration-global-btn').forEach((btn) => {
          btn.addEventListener('click', function() {
            window.applyDurationView(btn.dataset.durationBucket || '');
          });
        });

        const hashBucket = (location.hash || '').replace(/^#/, '');
        let storedBucket = '';
        try {
          storedBucket = localStorage.getItem(storageKey) || '';
        } catch (err) {
          storedBucket = '';
        }
        const initialBucket = resolveBucketId(hashBucket || storedBucket || switchRoot.dataset.defaultBucket);
        if (initialBucket) {
          window.applyDurationView(initialBucket);
        }
      })();

      (function() {
        let resizeTimer;
        function resizeVisibleCharts() {
          if (!window.Plotly || !window.Plotly.Plots) return;
          ['avgTop10', 'trendIndexChart', 'timingHeatmap', 'timingBar', 'departureModalChart'].forEach(function(id) {
            const el = document.getElementById(id);
            if (el && el.offsetParent !== null) {
              window.Plotly.Plots.resize(el);
            }
          });
        }
        window.addEventListener('resize', function() {
          clearTimeout(resizeTimer);
          resizeTimer = setTimeout(resizeVisibleCharts, 150);
        });
        window.addEventListener('orientationchange', function() {
          setTimeout(resizeVisibleCharts, 250);
        });
      })();
    </script>
  </body>
</html>
"""

    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(html_template)
    
    print(f"✅ Дашборд с встроенными графиками сгенерирован: {output_file}")
    print(f"📊 Статистика: {total_offers} наблюдений, {unique_hotels} отелей в истории, {current_table_hotels} актуально в таблице")
    print(f"💰 Цены: {history_min_price:.0f} - {history_max_price:.0f} PLN (средняя: {avg_price:.0f} PLN)")
    print(f"📈 Изменения цен: {len(decreases_48h) + len(increases_48h)} отелей за 48ч")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description='Generate inline charts dashboard')
    parser.add_argument('--data-file', default='data/travel_prices.csv')
    parser.add_argument('--output', default='index.html')
    parser.add_argument('--title', default='Travel Price Monitor • Расширенный дашборд')
    parser.add_argument('--charts-dir', default='hotel-charts')
    parser.add_argument('--tz', default='Europe/Warsaw')
    parser.add_argument('--alerts-file', default=None)
    parser.add_argument('--all-airports-data-file', default=None, help='CSV с общим фильтром (любой аэропорт) для сравнения')
    parser.add_argument('--disappeared-after-runs', type=int, default=2, help='Сколько последних ранов подряд должен отсутствовать отель, чтобы считаться выпавшим')
    parser.add_argument('--display-price-ceiling', type=float, default=None, help='Потолок цены для ПОКАЗА в таблице/карточках (дороже — только в истории/статистике)')
    parser.add_argument('--history-price-ceiling', type=float, default=None, help='Потолок для истории/графиков/выпавших (по умолчанию 20000 при заданном display ceiling)')
    parser.add_argument('--write-legacy-hotel-html', action='store_true', help='Дополнительно писать hotel-charts/*.html (для старых ссылок)')
    parser.add_argument('--config-file', default=None, help='JSON конфиг фильтра (иначе — по data_dir из --data-file)')
    args = parser.parse_args()
    generate_inline_charts_dashboard(
        data_file=args.data_file,
        output_file=args.output,
        title=args.title,
        charts_subdir=args.charts_dir,
        tz=args.tz,
        alerts_file=args.alerts_file,
        all_airports_data_file=args.all_airports_data_file,
        disappeared_after_runs=args.disappeared_after_runs,
        display_price_ceiling=args.display_price_ceiling,
        history_price_ceiling=args.history_price_ceiling,
        write_legacy_hotel_html=args.write_legacy_hotel_html,
        config_file=args.config_file,
    )
