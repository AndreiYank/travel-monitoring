#!/usr/bin/env python3
"""
Дашборд с встроенными графиками вместо модальных окон
"""

import pandas as pd
import json
import csv
from datetime import datetime, timedelta, timezone
import os
import re
import html as html_lib
from urllib.parse import urlparse, parse_qs
from purchase_timing_analysis import analyze_purchase_timing
from departure_analytics import (
    build_cohort_snapshots,
    build_departure_offers_index,
    build_departure_hotel_histories,
    build_hot_departure_history,
    build_departure_price_curves,
    cheap_tier_label,
    departure_status_label,
    HOT_DEPARTURE_CHART_DAYS_MAX,
    load_combined_departure_offers,
    load_departure_offers,
    MIN_COMMON_HOTELS,
    MIN_DEAL_HOTELS,
    COHORT_LOOKBACK_TARGET_HOURS,
)
from hotel_deal_score import compute_hotel_deal_metrics
from departure_airports import (
    aggregate_cohorts_by_arrival_airport,
    hub_regions_subtitle,
    should_group_by_arrival_airport,
)
from filter_registry import FILTER_GROUPS, active_filter_id, filter_href_by_charts_subdir


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
    ]:
        if col in work.columns:
            work[col] = pd.to_numeric(work[col], errors="coerce")
    work = work[work["days_to_departure"].fillna(9999) >= 0]
    return work


def _load_departure_cohort_frames(data_dir: str, data_file: str):
    """Fresh cohorts for nearest cards; merged cohorts for hot history."""
    cohorts_path = os.path.join(data_dir, "departure_cohorts.csv")
    offers_path = os.path.join(data_dir, "departure_offers.csv")

    travel_cohorts = pd.DataFrame()
    if os.path.exists(data_file):
        travel_cohorts = build_cohort_snapshots(load_departure_offers(data_file))

    fresh_cohorts = pd.DataFrame()
    if os.path.exists(offers_path):
        fresh_cohorts = build_cohort_snapshots(load_departure_offers(offers_path))
    elif os.path.exists(cohorts_path):
        fresh_cohorts = pd.read_csv(cohorts_path, quoting=csv.QUOTE_ALL, on_bad_lines="skip")

    current_cohorts = fresh_cohorts if not fresh_cohorts.empty else travel_cohorts
    history_cohorts = _merge_departure_cohorts(travel_cohorts, fresh_cohorts)
    return current_cohorts, history_cohorts


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


def _build_premium_history_by_hotel(df_history, display_ceiling):
    """Peak prices per hotel in the full history band (up to history ceiling)."""
    out = {}
    if df_history.empty:
        return out
    disp = float(display_ceiling) if display_ceiling is not None else None
    for name, grp in df_history.groupby('hotel_name', sort=False):
        prices = grp['price'].astype(float)
        if prices.empty:
            continue
        history_max = float(prices.max())
        premium_peak = None
        if disp is not None:
            above = prices[prices > disp]
            if not above.empty:
                premium_peak = float(above.max())
        out[str(name)] = {
            'history_max': history_max,
            'premium_peak': premium_peak,
        }
    return out


def _comeback_from_premium(current_price, premium_info, display_ceiling, min_drop_pct=8.0):
    """Hotel re-entered display band after being much more expensive in history."""
    if not premium_info or display_ceiling is None:
        return None
    try:
        current = float(current_price)
    except (TypeError, ValueError):
        return None
    if current > float(display_ceiling):
        return None
    peak = premium_info.get('premium_peak')
    if peak is None or peak <= float(display_ceiling):
        return None
    drop = (float(peak) - current) / float(peak) * 100.0
    if drop < min_drop_pct:
        return None
    return {
        'peak_price': float(peak),
        'drop_from_peak_pct': drop,
        'badge_html': (
            f'↩ Было до {peak:.0f} PLN'
            f' <span style="opacity:.85">(−{drop:.0f}%)</span>'
        ),
    }


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


def collapse_canonical_per_run(df, ceiling_val=None, run_gap_minutes=5):
    """One canonical row per hotel per scrape run.

    With a display ceiling, consider only offers at or below it, then pick
    the cheapest. Hotels only seen above the ceiling in a run are omitted
    from that band (they remain in the wider history band up to history ceiling).
    """
    if df.empty:
        return df.copy()

    rows = []
    for _, _, run_slice in iter_scrape_runs(df, gap_minutes=run_gap_minutes):
        for _, grp in run_slice.groupby('hotel_name', sort=False):
            pick = grp.sort_values('scraped_at_display')
            if ceiling_val is not None:
                in_band = pick[pick['price'].astype(float) <= ceiling_val]
                if in_band.empty:
                    continue
                pick = in_band
            rows.append(_lowest_price_row(pick))

    if not rows:
        return pd.DataFrame(columns=df.columns)
    return pd.DataFrame(rows).sort_values(['hotel_name', 'scraped_at_display']).reset_index(drop=True)


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
    return ""


def _alert_is_current(alert, table_prices, tolerance=2.0):
    """Alert is current if the hotel is in the last run at the alert's new price."""
    hotel_name = str(alert.get('hotel_name') or alert.get('hotel') or '')
    alert_type = alert.get('alert_type') or alert.get('type') or ''
    new_price = alert.get('new_price') if 'new_price' in alert else (alert.get('to') or alert.get('current_price'))
    if not hotel_name or alert_type == 'missing' or new_price in (None, '', 'null'):
        return False
    if hotel_name not in table_prices:
        return False
    try:
        current = float(table_prices[hotel_name])
        target = float(new_price)
    except (TypeError, ValueError):
        return False
    return abs(current - target) <= tolerance


def _alert_display_fields(alert, meta, slugify_fn, parse_iso_fn):
    hotel_name = str(alert.get('hotel_name') or alert.get('hotel') or 'Unknown')
    hotel_name_html = meta.get('hotel_name_html') or html_lib.escape(hotel_name)
    chart_href = html_lib.escape(meta.get('chart_href') or f"hotel-charts/{slugify_fn(hotel_name)}.html", quote=True)
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
    <title>{title_esc} — {current_p:.0f} PLN</title>
    <link rel="icon" href="{html_lib.escape(favicon_href)}" type="image/svg+xml">
    <link rel="apple-touch-icon" href="{html_lib.escape(favicon_href)}">
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
    <script src="https://cdn.plot.ly/plotly-latest.min.js"></script>
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
        }}
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
            #chart {{ height: 320px; }}
        }}
    </style>
</head>
<body>
    <div class="page">
        <div class="chart-topbar">
            <a class="chart-back" href="{back_href_esc}">← Назад к дашборду</a>
            {offer_btn}
        </div>
        <section class="chart-hero">
            <div class="chart-hero-img">{img_html}</div>
            <div class="chart-hero-body">
                <h1>{hotel_name_html}</h1>
                <p class="chart-hero-meta">{dates} · {duration}</p>
                <div class="chart-hero-price-row">
                    <span class="chart-current-price">{current_p:.0f} PLN</span>
                    <span class="deal-pill {deal_class}">Deal {deal_score} · {html_lib.escape(deal_label)}</span>
                    {min_badge}
                    {ceiling_badge}
                </div>
            </div>
        </section>
        <section class="chart-kpis">
            <div class="chart-kpi"><div class="v {'drop' if delta48_str.startswith('-') else ('up' if delta48_str.startswith('+') else '')}">{html_lib.escape(delta48_str)}</div><div class="l">Δ за 48ч</div></div>
            <div class="chart-kpi"><div class="v {'drop' if delta_avg_str.startswith('-') else ('up' if delta_avg_str.startswith('+') else '')}">{html_lib.escape(delta_avg_str)}</div><div class="l">Δ к своей средней</div></div>
            <div class="chart-kpi"><div class="v">{min_p:.0f}</div><div class="l">Минимум истории</div></div>
            <div class="chart-kpi"><div class="v">{median_p:.0f}</div><div class="l">Ср. по времени</div></div>
            <div class="chart-kpi"><div class="v">{max_p:.0f}</div><div class="l">Максимум</div></div>
            <div class="chart-kpi"><div class="v">{samples}</div><div class="l">Замеров · {html_lib.escape(confidence)}</div></div>
        </section>
        <section class="chart-panel">
            <h2>История цен</h2>
            <p class="chart-legend-note">Сплошная линия — история цены отеля в этом фильтре. По горизонтали — время проверки, в подсказке — даты поездки оффера.</p>
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
      const x = {json.dumps(chart_x, ensure_ascii=False)};
      const y = {json.dumps(chart_y, ensure_ascii=False)};
      const text = {json.dumps(enriched_hover, ensure_ascii=False)};
      const mainTrace = {{
        x, y, text,
        type: 'scatter',
        mode: 'lines+markers',
        name: 'Цена',
        line: {{ color: '#4f46e5', width: 2.5 }},
        marker: {{ size: 6, color: '#4f46e5', line: {{ width: 1, color: '#fff' }} }},
        connectgaps: true,
        hovertemplate: '<b>%{{y:.0f}} PLN</b><br>%{{text}}<extra></extra>'
      }};
      const tripDatesLabel = {json.dumps(str(trip_dates_label or '—'), ensure_ascii=False)};
      const yDataMin = y.length ? Math.min(...y) : 0;
      const yDataMax = y.length ? Math.max(...y) : 0;
      const ySpan = Math.max(yDataMax - yDataMin, 1);
      const yPad = Math.max(ySpan * 0.08, 80);
      const layout = {{
        margin: {{ t: 10, r: 20, b: 65, l: 60 }},
        paper_bgcolor: 'rgba(0,0,0,0)',
        plot_bgcolor: 'rgba(0,0,0,0)',
        xaxis: {{
          title: {{ text: 'Даты поездки: ' + tripDatesLabel, standoff: 14 }},
          type: 'date',
          tickformat: '%d.%m.%Y',
          hoverformat: '%d.%m.%Y %H:%M',
          gridcolor: 'rgba(148,163,184,0.2)',
          tickangle: -20
        }},
        yaxis: {{
          title: 'Цена (PLN)',
          gridcolor: 'rgba(148,163,184,0.2)',
          range: [yDataMin - yPad, yDataMax + yPad],
          fixedrange: false
        }},
        showlegend: false,
        hovermode: 'closest'
      }};
      Plotly.newPlot('chart', [mainTrace], layout, {{ responsive: true, displayModeBar: true }});
    </script>
</body>
</html>"""


def generate_inline_charts_dashboard(data_file: str = 'data/travel_prices.csv', output_file: str = 'index.html', title: str = 'Travel Price Monitor • Расширенный дашборд', charts_subdir: str = 'hotel-charts', tz: str = 'Europe/Warsaw', alerts_file: str = None, all_airports_data_file: str = None, disappeared_after_runs: int = 2, display_price_ceiling: float = None, history_price_ceiling: float = None):
    """Генерирует дашборд с встроенными графиками"""
    
    # Загружаем данные
    try:
        df = pd.read_csv(data_file, quoting=csv.QUOTE_ALL, on_bad_lines='skip')
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
        print(f"✅ Загружено {len(df)} записей")
    except Exception as e:
        print(f"❌ Ошибка загрузки данных: {e}")
        return
    # Откат фичи сравнения аэропортов: не используем общий датасет
    df_all_airports = None

    ceiling_val = _parse_price_ceiling(display_price_ceiling)
    history_val = _resolve_history_ceiling(ceiling_val, history_price_ceiling)
    df_canonical = collapse_canonical_per_run(df, ceiling_val)
    df_history = collapse_canonical_per_run(df, history_val)
    df_full = collapse_canonical_per_run(df, None)
    if ceiling_val is not None:
        print(f"📊 Показ ≤{ceiling_val:.0f} PLN (таблица, алерты): {len(df_canonical)} записей")
    if history_val is not None:
        print(f"📈 История ≤{history_val:.0f} PLN (графики, выпавшие): {len(df_history)} записей")
    elif len(df_history) > len(df_canonical):
        print(f"📈 Расширенная история: {len(df_history)} записей")
    if len(df_full) > len(df_history):
        print(f"📉 Полная история (без потолка): {len(df_full)} записей")

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
            
            # Добавляем информацию об аэропорте с поэлементным fallback: сначала from_airport, затем извлекаем из URL
            if 'from_airport' in same_hotel_dates.columns:
                same_hotel_dates['airport'] = same_hotel_dates['from_airport']
                same_hotel_dates['airport'] = same_hotel_dates['airport'].where(
                    same_hotel_dates['airport'].astype(str).str.strip().ne(''),
                    None
                )
                same_hotel_dates['airport'] = same_hotel_dates['airport'].fillna(
                    same_hotel_dates['url'].apply(extract_airport_from_url)
                )
            else:
                same_hotel_dates['airport'] = same_hotel_dates['url'].apply(extract_airport_from_url)
            
            # Подставляем плейсхолдер для неизвестного аэропорта, чтобы не терять альтернативы
            same_hotel_dates['airport'] = same_hotel_dates['airport'].fillna('Другой город')
            same_hotel_dates.loc[same_hotel_dates['airport'].astype(str).str.strip()=='', 'airport'] = 'Другой город'
            
            # Для каждого аэропорта выбираем запись с минимальной ценой и её offer_url (если есть)
            idx_min_by_airport = same_hotel_dates.groupby('airport')['price'].idxmin()
            airport_prices = same_hotel_dates.loc[
                idx_min_by_airport, ['airport', 'price', 'offer_url', 'url']
            ].reset_index(drop=True)
            
            # Фильтруем аэропорты с ценами дешевле текущей
            cheaper_alternatives = airport_prices[
                (airport_prices['price'] < current_price) & 
                (airport_prices['airport'] != current_airport_norm)
            ].sort_values('price')
            
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
                    'top10_hotels': top10_hotels
                })
            elif len(latest_prices) > 0:
                # Если отелей меньше 10, берем все
                avg_price = sum(latest_prices) / len(latest_prices)
                
                # Все отели попадают в "ТОП"
                sorted_prices = sorted(latest_prices)
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
                    'top10_hotels': top_hotels
                })
        
        if run_data:
            top10_x_values = [pd.Timestamp(ts).isoformat() for ts, _ in run_data]
            top10_y_values = [float(price) for _, price in run_data]
            
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
            top10_detailed_data = []
            print("❌ Нет данных для ТОП-10 графика")
            
    except Exception as e:
        print(f"Ошибка расчета ТОП-10: {e}")
        top10_x_values, top10_y_values = [], []
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

    df_sorted_all = latest_run_slice.sort_values(['hotel_name', 'scraped_at_display'])
    latest_rows = []
    skipped_above_ceiling = 0
    if ceiling_val is not None and not full_latest_run_slice.empty:
        for hotel_name, grp in full_latest_run_slice.groupby('hotel_name'):
            if grp[grp['price'].astype(float) <= ceiling_val].empty:
                skipped_above_ceiling += 1
    for hotel_name, grp in df_sorted_all.groupby('hotel_name'):
        # В каноническом ране — одна строка на отель; берём актуальную, не мин. по всей истории.
        last = grp.sort_values('scraped_at_display').iloc[-1]
        latest_rows.append({
            'hotel_name': hotel_name,
            'price': float(last['price']),
            'dates': last.get('dates', None),
            'duration': last.get('duration', None),
            'scraped_at_local': last['scraped_at_local'],
            'url': last.get('url', None),
            'from_airport': last.get('from_airport', None),
            'offer_url': last.get('offer_url', None),
            'image_url': last.get('image_url', None)
        })
    all_hotels = pd.DataFrame(latest_rows).sort_values('price').reset_index(drop=True)
    # Актуальная цена для таблицы — только последний ран; дельты — vs вся df_canonical.
    table_prices = {row['hotel_name']: float(row['price']) for row in latest_rows}

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
    df_sorted = df_canonical.sort_values(['hotel_name', 'scraped_at_display'])
    df_sorted_full = df_full.sort_values(['hotel_name', 'scraped_at_display'])
    ref_time_series = df_canonical['scraped_at_display'] if not df_canonical.empty else df['scraped_at_display']

    def compute_changes(window_hours: int):
        cutoff = (ref_time_series.max() or datetime.now()) - timedelta(hours=window_hours)
        changes = []
        deltas_map = {}
        for hotel_name, grp in df_sorted.groupby('hotel_name'):
            if hotel_name not in table_prices:
                continue
            grp = grp.sort_values('scraped_at_display')
            latest_price = table_prices[hotel_name]
            latest_time = grp.iloc[-1]['scraped_at_display']
            win = grp[grp['scraped_at_display'] >= cutoff]
            if len(win) >= 2:
                baseline_row = win.iloc[0]
            elif len(grp) >= 2:
                baseline_row = grp.iloc[-2]
            else:
                deltas_map[hotel_name] = None
                continue
            baseline_price = float(baseline_row['price'])
            if baseline_price == 0:
                deltas_map[hotel_name] = None
                continue
            change = latest_price - baseline_price
            if change == 0:
                deltas_map[hotel_name] = None
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
            deltas_map[hotel_name] = (change, change_percent)
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
    for hotel_name, grp in df_sorted.groupby('hotel_name'):
        if hotel_name not in table_prices:
            continue
        grp = grp.sort_values('scraped_at_display')
        latest_price = table_prices[hotel_name]
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
        minmax_labels_by_hotel[hotel_name] = labels

    # Отклонение от "типичной" цены отеля:
    # baseline = time-weighted mean по всей истории (без потолка показа).
    avg_baseline_delta = {}
    for hotel_name, last_price in table_prices.items():
        grp = df_sorted_full[df_sorted_full['hotel_name'] == hotel_name]
        if grp.empty:
            avg_baseline_delta[hotel_name] = None
            continue
        grp = grp.sort_values('scraped_at_display')
        baseline = _time_weighted_price_baseline(grp)
        if baseline is None or baseline == 0:
            avg_baseline_delta[hotel_name] = None
            continue
        change_abs = float(last_price) - baseline
        change_pct = (change_abs / baseline) * 100.0
        avg_baseline_delta[hotel_name] = (change_abs, change_pct)

    # Deal Score: насколько предложение выгодно относительно своей исторической цены
    premium_history_by_hotel = _build_premium_history_by_hotel(df_history, ceiling_val)

    def _clamp(v, lo, hi):
        return max(lo, min(hi, v))

    deal_score_by_hotel = {}
    entry_candidates = []

    for hotel_name, grp in df_sorted.groupby('hotel_name'):
        grp = grp.sort_values('scraped_at_display')
        grp_full = df_sorted_full[df_sorted_full['hotel_name'] == hotel_name].sort_values('scraped_at_display')
        hist_grp = grp_full if not grp_full.empty else grp
        prices = hist_grp['price'].astype(float).tolist()
        if not prices:
            continue

        latest = float(table_prices.get(hotel_name, prices[-1]))
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

        delta48_info = deltas_by_hotel.get(hotel_name)
        avg_info = avg_baseline_delta.get(hotel_name)
        d48_pct = float(delta48_info[1]) if delta48_info is not None else None
        d_avg_pct = float(avg_info[1]) if avg_info is not None else None

        comeback = _comeback_from_premium(
            latest, premium_history_by_hotel.get(hotel_name), ceiling_val
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

        deal_score_by_hotel[hotel_name] = {
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
        }

        delta48 = delta48_info

        # Кандидаты для "раннего входа"
        if delta48 is not None and delta48[1] <= -2.0 and latest <= p25 and deal_score >= 72 and confidence_level != "Low":
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
    current_hotels_for_breadth = set(all_hotels['hotel_name'].astype(str).tolist()) if not all_hotels.empty else set()
    breadth_total = 0
    breadth_down = 0
    for hotel_name, grp in df_sorted.groupby('hotel_name'):
        if current_hotels_for_breadth and hotel_name not in current_hotels_for_breadth:
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
            current_table_hotels = set(table_prices.keys())
            gone_hotels = all_seen_hotels - current_table_hotels

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

    # Загружаем карту изображений (если есть)
    images_map = {}
    images_path = os.path.join('data', 'hotel_images.json')
    if os.path.exists(images_path):
        try:
            with open(images_path, 'r', encoding='utf-8') as f:
                images_map = json.load(f) or {}
        except Exception:
            images_map = {}

    # Функция для слуг-имени файла по названию отеля
    def slugify(text: str) -> str:
        import re  # локальный импорт во избежание проблем со scope в CI
        text = text.lower().strip()
        text = re.sub(r"[^a-z0-9]+", "-", text)
        text = re.sub(r"-+", "-", text).strip('-')
        return text or "hotel"

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

    # Карточки отелей (визуальный режим по умолчанию)
    hotel_cards = []
    for _, hotel in all_hotels.head(200).iterrows():
        hotel_name = hotel['hotel_name']
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
        if charts_subdir:
            chart_href = f"{charts_subdir.rstrip('/')}/{hotel_slug}.html"
        else:
            chart_href = f"hotel-charts/{hotel_slug}.html"

        delta_info = deltas_by_hotel.get(hotel_name)
        delta48 = f"{delta_info[1]:+.1f}%" if delta_info is not None else "—"
        avg_info = avg_baseline_delta.get(hotel_name)
        delta_avg = f"{avg_info[1]:+.1f}%" if avg_info is not None else "—"
        deal_info = deal_score_by_hotel.get(hotel_name, {'score': 0, 'confidence': 'Low'})
        deal_score = int(deal_info.get('score', 0))
        confidence = deal_info.get('confidence', 'Low')
        d48_for_badge = float(delta_info[1]) if delta_info is not None else None
        d_avg_for_badge = float(avg_info[1]) if avg_info is not None else None
        comeback_drop = deal_info.get('comeback_drop_pct')
        deal_label, deal_class, _ = classify_deal_badge(
            deal_score, confidence, d48_for_badge, d_avg_for_badge, comeback_drop
        )
        comeback = _comeback_from_premium(
            price, premium_history_by_hotel.get(hotel_name), ceiling_val
        )
        comeback_html = (
            f'<span class="comeback-badge">{comeback["badge_html"]}</span>'
            if comeback else ''
        )

        hotel_cards.append({
            "hotel_name": hotel_name,
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
        if charts_subdir:
            chart_href = f"{charts_subdir.rstrip('/')}/{hotel_slug}.html"
        else:
            chart_href = f"hotel-charts/{hotel_slug}.html"
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

    # Создаём директорию для страниц графиков
    charts_dir = os.path.join(charts_subdir)
    os.makedirs(charts_dir, exist_ok=True)

    from price_alerts_v2 import ALERT_THRESHOLD_PERCENT

    # График отеля — история до history ceiling (10–20k видны на графике)
    chart_href_lookup: dict[str, str] = {}
    chart_hotel_names = sorted(set(df_history["hotel_name"].astype(str).unique()))
    for hotel_name in chart_hotel_names:
        hotel_ts = df_history[df_history['hotel_name'] == hotel_name].dropna(subset=['scraped_at_display']).sort_values('scraped_at_display')
        x_values = [pd.to_datetime(t).isoformat() for t in hotel_ts['scraped_at_display'].tolist()]
        x_display = [pd.to_datetime(t).strftime('%d.%m.%Y %H:%M') for t in hotel_ts['scraped_at_display'].tolist()]
        y_values = [float(p) for p in hotel_ts['price'].tolist()]
        dates_list = hotel_ts['dates'].fillna('Неизвестно').tolist()

        text_values = []
        for x_val, trip_dates in zip(x_display, dates_list):
            text_values.append(f"Проверка: {x_val}<br>Даты поездки: {trip_dates}")

        hotel_slug = slugify(hotel_name)
        hotel_html_path = os.path.join(charts_dir, f"{hotel_slug}.html")

        subdir = (charts_subdir or '').rstrip('/')
        back_target = filter_href_by_charts_subdir(subdir)
        back_href = os.path.relpath(back_target, start=os.path.dirname(hotel_html_path))
        favicon_href = os.path.relpath("favicon.svg", start=os.path.dirname(hotel_html_path))

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
        if charts_subdir:
            _register_chart_href(
                chart_href_lookup,
                hotel_name,
                f"{charts_subdir.rstrip('/')}/{hotel_slug}.html",
            )

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
        current_cohorts, history_cohorts = _load_departure_cohort_frames(data_dir, data_file)

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
            hot_history = build_hot_departure_history(work)
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
                    score = int(dep.get('hot_score') or 0)
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
                    preferred_runs[key] = str(item.get("best_seen_at") or "")
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
                        metrics = compute_hotel_deal_metrics(hist, offer_price)
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
        updated_str = df['scraped_at_display'].max().strftime('%d.%m.%Y %H:%M')
    except Exception:
        updated_str = datetime.now().strftime('%d.%m.%Y %H:%M')

    _current_filter = active_filter_id(charts_subdir)
    _sidebar_nav_parts = [
        '<a href="index.html" class="nav-item">'
        '<span class="flag">🏠</span>'
        '<span class="country-name">Главная</span></a>'
    ]
    for group in FILTER_GROUPS:
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

    html_template = f"""<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <script src="https://cdn.plot.ly/plotly-latest.min.js"></script>
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
            background: #0f172a;
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
            background:
                linear-gradient(180deg, rgba(2,6,23,.42), rgba(2,6,23,.66)),
                url('https://images.unsplash.com/photo-1507525428034-b723cf961d3e?auto=format&fit=crop&w=2200&q=80') center/cover no-repeat;
            transform: scale(1.03);
        }}

        body::after {{
            content: "";
            position: fixed;
            inset: 0;
            z-index: -1;
            background:
                radial-gradient(1400px 700px at -10% -25%, rgba(99,102,241,.24), transparent 58%),
                radial-gradient(1200px 800px at 120% -10%, rgba(6,182,212,.20), transparent 56%),
                radial-gradient(900px 600px at 50% 120%, rgba(56,189,248,.16), transparent 60%);
            animation: gradientDrift 18s ease-in-out infinite alternate;
        }}
        
        .container {{
            width: min(var(--content-max-width), calc(100% - (var(--page-gutter) * 2)));
            margin: 0 auto;
            background: var(--surface);
            backdrop-filter: blur(16px);
            padding: var(--container-padding);
            border-radius: var(--radius-xl);
            box-shadow: var(--shadow-xl);
            margin-top: 2rem;
            margin-bottom: 2rem;
            border: 1px solid rgba(255,255,255,0.7);
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
            background: linear-gradient(135deg, #1e293b 0%, #0f172a 100%) !important;
            color: #f1f5f9 !important;
        }}
        
        .dark-theme .main-content {{
            background: linear-gradient(135deg, #1e293b 0%, #0f172a 100%) !important;
        }}
        
        .dark-theme .container {{
            background: rgba(30, 41, 59, 0.95);
            color: #f1f5f9;
        }}
        
        .dark-theme .metric {{
            background: linear-gradient(145deg, #1e293b 0%, #334155 100%);
            border: 1px solid #475569;
        }}
        
        .dark-theme .hotels-section {{
            background: linear-gradient(145deg, #1e293b 0%, #334155 100%);
            border: 1px solid #475569;
        }}
        
        .dark-theme .hotels-table th {{
            background: linear-gradient(135deg, #334155 0%, #475569 100%);
            color: #f1f5f9;
            border-bottom: 2px solid #475569;
            border-top: 1px solid #475569;
        }}
        
        .dark-theme .hotels-table th:hover {{
            background: linear-gradient(135deg, #475569 0%, #64748b 100%);
        }}
        
        .dark-theme .hotels-table tbody tr:nth-child(even) {{
            background: #1e293b;
        }}
        
        .dark-theme .hotels-table tbody tr:nth-child(odd) {{
            background: #0f172a;
        }}
        
        .dark-theme .hotels-table tbody tr:hover {{
            background: linear-gradient(90deg, #1e40af 0%, #3b82f6 100%);
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
            background: rgba(148,163,184,.15);
            font-weight: 700;
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
        }}
        .departure-offers-table {{
            width: 100%;
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
            min-height: 200px;
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
            grid-template-columns: repeat(4, minmax(0,1fr));
            gap: 10px;
            margin-top: .9rem;
        }}
        .hero-kpi {{
            background: rgba(255,255,255,.16);
            border: 1px solid rgba(255,255,255,.28);
            border-radius: 12px;
            padding: .55rem .65rem;
            backdrop-filter: blur(8px);
            animation: pulseGlow 3.6s ease-in-out infinite;
        }}
        .hero-kpi .v {{
            font-size: 1.05rem;
            font-weight: 800;
            line-height: 1.1;
        }}
        .hero-kpi .l {{
            font-size: .74rem;
            opacity: .92;
        }}
        .hero-kpi .s {{
            margin-top: .2rem;
            font-size: .68rem;
            line-height: 1.25;
            opacity: .86;
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
            border-radius: var(--panel-shell-radius);
            box-shadow: var(--shadow-sm);
            background: rgba(255,255,255,0.9);
            border: 1px solid var(--border-soft);
        }}
        
        .hotels-table {{
            width: 100%;
            border-collapse: separate;
            border-spacing: 0;
            margin: 0;
            font-size: 0.875rem;
        }}
        
        .hotels-table th {{
            background: linear-gradient(135deg, rgba(79,70,229,.93) 0%, rgba(14,165,233,.90) 100%);
            color: #fff;
            font-weight: 600;
            font-size: 0.875rem;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            padding: 1rem 1.25rem;
            text-align: left;
            cursor: pointer;
            user-select: none;
            position: sticky;
            top: 0;
            z-index: 10;
            transition: var(--transition-fast);
            border-bottom: 2px solid rgba(255,255,255,.30);
            border-top: 1px solid rgba(255,255,255,.18);
        }}
        
        .hotels-table th:hover {{
            background: linear-gradient(135deg, rgba(79,70,229,1) 0%, rgba(14,165,233,1) 100%);
            transform: translateY(-1px);
            box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1);
        }}
        
        .hotels-table th.sortable::after {{
            content: ' ↕';
            opacity: 0.7;
            margin-left: 0.5rem;
            font-size: 0.75rem;
        }}
        
        .hotels-table th.sort-asc::after {{
            content: ' ↑';
            opacity: 1;
            color: #fbbf24;
        }}
        
        .hotels-table th.sort-desc::after {{
            content: ' ↓';
            opacity: 1;
            color: #fbbf24;
        }}
        
        .hotels-table td {{
            padding: 1rem 1.25rem;
            border-bottom: 1px solid #f1f5f9;
            transition: var(--transition-fast);
        }}
        
        .hotels-table tbody tr:nth-child(even) {{
            background: #f8fafc;
        }}
        
        .hotels-table tbody tr:nth-child(odd) {{
            background: white;
        }}
        
        .hotels-table tbody tr:hover {{
            background: linear-gradient(90deg, rgba(79,70,229,.08) 0%, rgba(14,165,233,.08) 100%);
            transform: scale(1.01);
            box-shadow: var(--shadow-sm);
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
        
        .hotel-name {{
            color: var(--primary-color);
            font-weight: 700;
            font-size: 0.95rem;
        }}
        
        .hotel-name a {{
            color: inherit;
            text-decoration: none;
            transition: var(--transition-fast);
        }}
        
        .hotel-name a:hover {{
            color: var(--primary-dark);
            text-decoration: underline;
        }}
        
        .price {{
            font-weight: 800;
            font-size: 1.1rem;
            color: var(--success-color);
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
            background: var(--gradient-danger);
            color: white;
        }}
        
        .delta.down {{
            background: var(--gradient-success);
            color: white;
        }}
        
        .delta.flat {{
            background: #f1f5f9;
            color: #64748b;
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
            transition: var(--transition-normal);
        }}
        
        .sidebar-overlay.open {{
            opacity: 1;
            visibility: visible;
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
        
    </style>
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
        <div class="hero">
            <div class="hero-content">
                <h2 style="margin:0; font-size:clamp(1.2rem,2.4vw,1.65rem); font-weight:800;">🌊 Sea Intelligence</h2>
                <div style="margin-top:.35rem; font-size:.92rem; opacity:.96;">Сканируем рынок туров и показываем точки входа раньше массового спроса.</div>
                <h1 style="margin:.55rem 0 0; font-size:clamp(1.4rem,3vw,2.1rem); font-weight:800; line-height:1.15;">{title.replace('🇬🇷 ', '').replace('🇪🇬 ', '').replace('🇹🇷 ', '')}</h1>
                <div style="margin-top:.3rem; font-size:.9rem; opacity:.92;">Обновлено: {updated_str}</div>
                <div class="hero-kpis">
                    <div class="hero-kpi">
                        <div class="v">{len(entry_candidates)}</div>
                        <div class="l">Кандидатов входа</div>
                        <div class="s">Отели с сильным Deal Score и ценой ниже своего обычного уровня.</div>
                    </div>
                    <div class="hero-kpi">
                        <div class="v">{market_breadth*100:.0f}%</div>
                        <div class="l">Рынок дешевеет</div>
                        <div class="s">Доля отелей, где цена снизилась за последние 48 часов.</div>
                    </div>
                    <div class="hero-kpi">
                        <div class="v">{max((v['score'] for v in deal_score_by_hotel.values()), default=0)}</div>
                        <div class="l">Лучший Deal Score</div>
                        <div class="s">Самая сильная найденная возможность в текущем ране.</div>
                    </div>
                    <div class="hero-kpi">
                        <div class="v">{updated_str}</div>
                        <div class="l">Обновлено</div>
                        <div class="s">Время последнего обновления данных по этому фильтру.</div>
                    </div>
                </div>
            </div>
        </div>

"""

    from price_alerts_v2 import ALERT_THRESHOLD_PERCENT

    current_alerts = []
    current_hotels = set()
    for a in alerts:
        if not _alert_is_current(a, table_prices):
            continue
        hotel_name = str(a.get('hotel_name') or a.get('hotel') or '')
        if hotel_name in current_hotels:
            continue
        current_hotels.add(hotel_name)
        current_alerts.append(a)

    latest_run_ts = None
    if not df_canonical.empty:
        latest_run_ts = df_canonical['scraped_at_display'].max()
    for hotel_name, price in table_prices.items():
        if hotel_name in current_hotels:
            continue
        comeback = _comeback_from_premium(
            price, premium_history_by_hotel.get(hotel_name), ceiling_val
        )
        if not comeback or comeback['drop_from_peak_pct'] < ALERT_THRESHOLD_PERCENT:
            continue
        peak = float(comeback['peak_price'])
        curr = float(price)
        current_alerts.append({
            'hotel_name': hotel_name,
            'old_price': peak,
            'new_price': curr,
            'price_change': curr - peak,
            'price_change_pct': (curr - peak) / peak * 100.0 if peak else 0.0,
            'timestamp': latest_run_ts,
            'alert_type': 'premium_comeback',
            'created_at': latest_run_ts.isoformat() if latest_run_ts is not None else '',
            'threshold_percent': ALERT_THRESHOLD_PERCENT,
            'unique_key': f"{hotel_name}_comeback_{peak:.0f}_to_{curr:.0f}",
        })
        current_hotels.add(hotel_name)

    current_alert_keys = {a.get('unique_key') for a in current_alerts if a.get('unique_key')}
    history_alerts = [
        a for a in alerts
        if not a.get('unique_key') or a.get('unique_key') not in current_alert_keys
    ]

    def _count_alert_kinds(items):
        drops = sum(1 for a in items if float(a.get('price_change') or 0) < 0)
        ups = sum(1 for a in items if float(a.get('price_change') or 0) > 0)
        return drops, ups

    cur_drops, cur_ups = _count_alert_kinds(current_alerts)
    alert_chips_html = ""
    if current_alerts:
        if cur_drops:
            alert_chips_html += f'<span class="alert-chip drop">↓ {cur_drops} подешевело сейчас</span>'
        if cur_ups:
            alert_chips_html += f'<span class="alert-chip up">↑ {cur_ups} подорожало сейчас</span>'
    if history_alerts:
        alert_chips_html += f'<span class="alert-chip missing">🕘 {len(history_alerts)} в истории</span>'

    alerts_html = f"""
        <div class="alerts-section" id="alertsSection">
            <div class="alerts-header" onclick="toggleAlerts()">
                <div class="alerts-header-main">
                    <h3>📊 Заметные изменения цен</h3>
                    <p class="alerts-lead">Отели, у которых <strong>заметно изменилась цена</strong> (от {ALERT_THRESHOLD_PERCENT:.0f}% между проверками или возврат из дорогого сегмента) и <strong>эта цена всё ещё актуальна</strong> — в последнем обновлении она не менялась. Прошлые события — в «Истории».</p>
                    <div class="alerts-summary-chips">{alert_chips_html}</div>
                </div>
                <span class="expand-icon" id="alertsExpandIcon">▼</span>
            </div>
            <div class="alerts-content" id="alertsContent">
"""

    if alerts:
        if current_alerts:
            alerts_html += f"""
                <p class="alerts-section-label">Действует сейчас · {len(current_alerts)}</p>
                <div class="alerts-grid">
"""
            for a in current_alerts:
                hotel_name = str(a.get('hotel_name') or a.get('hotel') or 'Unknown')
                meta = hotel_meta_by_name.get(hotel_name, {})
                alerts_html += _render_alert_card(a, meta, slugify, parse_iso)
            alerts_html += """
                </div>
"""
        else:
            alerts_html += """
                <div class="alerts-empty">Сейчас нет отелей с актуальным изменением цены — зафиксированные сдвиги уже устарели. Смотрите «Историю» ниже.</div>
"""

        if history_alerts:
            alerts_html += f"""
                <details class="alerts-history-fold" onclick="event.stopPropagation()">
                    <summary>🕘 Показать историю ({len(history_alerts)} прошлых изменений)</summary>
                    <div class="alert-history-list">
"""
            for a in history_alerts:
                hotel_name = str(a.get('hotel_name') or a.get('hotel') or 'Unknown')
                meta = hotel_meta_by_name.get(hotel_name, {})
                alerts_html += _render_alert_history_row(a, meta, slugify, parse_iso)
            alerts_html += """
                    </div>
                </details>
"""
    else:
        alerts_html += """
                <div class="alerts-empty">Пока нет изменений от {ALERT_THRESHOLD_PERCENT:.0f}% — они появятся здесь после следующих проверок.</div>
"""
    alerts_html += """
            </div>
        </div>
"""

    # Карточки отелей (визуальный режим)
    cards_html = """
        <div class="cards-section" id="cardsSection">
            <div class="cards-grid">
"""
    for c in hotel_cards:
        img_html = (
            f'<img src="{html_lib.escape(c["image_url"], quote=True)}" alt="hotel image" loading="lazy" '
            f'onerror="this.onerror=null;this.parentElement.innerHTML=\'<div>Фото отеля</div>\';" />'
        ) if c["image_url"] else '<div>Фото отеля</div>'
        offer_btn = f'<a class="card-btn" href="{html_lib.escape(c["offer_url"], quote=True)}" target="_blank">Открыть оффер</a>' if c["offer_url"] else '<span class="card-btn" style="opacity:.6;">Оффер недоступен</span>'
        cards_html += f"""
            <article class="hotel-card">
                <div class="hotel-card-img">{img_html}</div>
                <div class="hotel-card-body">
                    <h4 class="hotel-card-title">{c["hotel_name_html"]}</h4>
                    <div class="hotel-card-meta">
                        <div class="hotel-card-price">{c["price"]:.0f} PLN</div>
                        <span class="deal-pill {c["deal_class"]}">Deal {c["deal_score"]} • {c["deal_label"]}</span>
                    </div>
                    {c["comeback_html"]}
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
    html_template += alerts_html
    html_template += departure_block_html
    html_template += departure_history_html

    # --- Секция «Когда покупать»: статистика снижения цен по времени ---
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

    html_template += f"""

        <div class="avg-top10-section">
            <h3>📉 Средняя цена ТОП‑10 дешёвых предложений</h3>
            <div id="avgTop10" style="height:300px;"></div>
        </div>

        <details class="dashboard-fold" id="trendFold">
            <summary>
                <span>Индекс ценовой динамики</span>
                <span class="fold-title-meta">Доп. аналитика</span>
                <span class="fold-chevron">⌄</span>
            </summary>
            <div class="fold-content">
                <div class="trend-index-section">
                    <h3>📊 Индекс ценовой динамики</h3>
                    <div id="trendIndexChart" style="height:280px;"></div>
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
                <div class="metrics metrics-compact">
                    {stats_metrics_row1}
                </div>

                <div class="metrics metrics-compact">
                    {stats_metrics_row2}
                </div>

                {changes_html}
                {entry_signal_html}
            </div>
        </details>
"""

    # Блок выбора вида списка предложений (всегда виден)
    html_template += f"""
        <div class="table-toolbar" id="modeSwitchRow">
            <div class="table-toolbar-title">Вид</div>
            <div class="mode-switch table-mode-switch" id="modeSwitch" data-mode="cards">
                <button class="mode-btn active" data-mode="cards">Карточки</button>
                <button class="mode-btn" data-mode="table">Таблица</button>
            </div>
        </div>
        {cards_html}
"""

    # Адаптивные диапазоны фильтра по цене на основе фактических цен таблицы
    try:
        _pr = pd.to_numeric(all_hotels['price'], errors='coerce').dropna()
        _pr = _pr[_pr > 0]
    except Exception:
        _pr = pd.Series([], dtype='float64')
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
            </div>
            
            <!-- Table Filters -->
            <div class="table-filters">
                <input type="text" class="filter-input" id="searchInput" placeholder="🔍 Поиск по отелям..." />
                <select class="filter-select" id="priceFilter">
                    {price_filter_options_html}
                </select>
                <select class="filter-select" id="changeFilter">
                    <option value="">Все изменения</option>
                    <option value="decrease">Снижение цен</option>
                    <option value="increase">Рост цен</option>
                    <option value="stable">Стабильные</option>
                </select>
                <button class="filter-button" id="clearFilters" style="padding: 0.75rem 1rem; background: var(--gradient-primary); color: white; border: none; border-radius: var(--radius-md); cursor: pointer; font-weight: 600;">Очистить</button>
            </div>
            
            <div class="table-container">
            <table class="hotels-table" id="hotelsTable">
                <thead>
                    <tr>
                        <th class="sortable" data-sort="hotel">Отель</th>
                        <th class="sortable" data-sort="price">Цена</th>
                        <th class="sortable" data-sort="deal">Deal Score</th>
                        <th class="sortable" data-sort="delta48">Δ 48ч</th>
                        <th class="sortable" data-sort="deltaavg" title="Отклонение от средней, взвешенной по длительности удержания каждой цены">Δ к средней</th>
                        <th class="sortable" data-sort="dates">Даты</th>
                        <th class="sortable" data-sort="duration">Длительность</th>
                        <th>Ссылка</th>
                    </tr>
                </thead>
                <tbody>"""

    # Добавляем строки таблицы
    for i, (_, hotel) in enumerate(all_hotels.iterrows()):
        hotel_name = hotel['hotel_name']
        price = hotel['price']
        dates = hotel['dates'] if pd.notna(hotel['dates']) else '20-09-2025 - 04-10-2025'
        duration = hotel['duration'] if pd.notna(hotel['duration']) else '6-15 дней'
        
        # Δ 48ч
        delta_display = "—"
        delta_class = "delta flat"
        delta_info = deltas_by_hotel.get(hotel_name)
        if delta_info is not None:
            delta_abs, delta_pct = delta_info
            arrow = '↑' if delta_abs > 0 else ('↓' if delta_abs < 0 else '→')
            delta_class = 'delta up' if delta_abs > 0 else ('delta down' if delta_abs < 0 else 'delta flat')
            sign = '+' if delta_abs > 0 else ('' if delta_abs < 0 else '')
            delta_display = f"{arrow} {sign}{delta_pct:.1f}%"

        # Δ к time-weighted средней по полной истории отеля (без потолка показа)
        avg_display = "—"
        avg_info = avg_baseline_delta.get(hotel_name)
        avg_sort_value = 0
        if avg_info is not None:
            avg_abs, avg_pct = avg_info
            arrow2 = '↑' if avg_abs > 0 else ('↓' if avg_abs < 0 else '→')
            sign2 = '+' if avg_abs > 0 else ('' if avg_abs < 0 else '')
            avg_display = f"{arrow2} {sign2}{avg_pct:.1f}%"
            avg_sort_value = avg_pct

        hotel_slug = slugify(hotel_name)
        # Строим ссылку на страницу графика, учитывая поддиректорию
        if charts_subdir:
            chart_href = f"{charts_subdir.rstrip('/')}/{hotel_slug}.html"
        else:
            chart_href = f"hotel-charts/{hotel_slug}.html"
        
        # Откат: не вычисляем аэропорт и альтернативы
        
        # Ссылка на предложение
        offer_url = hotel.get('offer_url', '')
        offer_link_html = ""
        if offer_url and pd.notna(offer_url) and offer_url.strip():
            offer_link_html = f'<a href="{offer_url}" target="_blank" class="offer-link">🔗</a>'
        else:
            offer_link_html = "—"
        
        deal_info = deal_score_by_hotel.get(hotel_name, {'score': 0, 'confidence': 'Low', 'samples': 0})
        deal_score = int(deal_info.get('score', 0))
        confidence_level = deal_info.get('confidence', 'Low')
        d48_tbl = float(delta_info[1]) if delta_info is not None else None
        d_avg_tbl = float(avg_info[1]) if avg_info is not None else None
        comeback_drop_tbl = deal_info.get('comeback_drop_pct')
        _, _, deal_badge = classify_deal_badge(
            deal_score, confidence_level, d48_tbl, d_avg_tbl, comeback_drop_tbl
        )
        confidence_label = (
            "Low confidence" if confidence_level == "Low"
            else ("Medium confidence" if confidence_level == "Medium" else "High confidence")
        )
        comeback = _comeback_from_premium(price, premium_history_by_hotel.get(hotel_name), ceiling_val)
        comeback_cell = (
            f'<br><span class="comeback-badge">{comeback["badge_html"]}</span>'
            if comeback else ''
        )

        html_template += f"""
                    <tr>
                        <td class="hotel-name"><a class=\"open-chart-link\" href=\"{chart_href}\" target=\"_blank\" onmouseover=\"_hoverPreview.show(event,'{hotel_name}')\" onmouseout=\"_hoverPreview.hide()\">{hotel_name}</a></td>
                        <td class="price" data-sort-value="{price}">{price:.0f} PLN{comeback_cell}</td>
                        <td data-sort-value="{deal_score}">{deal_score} <span style="opacity:.85;font-size:.85em;">{deal_badge}</span><br><span style="opacity:.65;font-size:.78em;">{confidence_label}</span></td>
                        <td class=\"{delta_class}\" data-sort-value="{delta_info[1] if delta_info else 0}">{delta_display}</td>
                        <td data-sort-value="{avg_sort_value}">{avg_display}</td>
                        <td data-sort-value="{dates}">{dates}</td>
                        <td data-sort-value="{duration}">{duration}</td>
                        
                        <td class="offer-link-cell">{offer_link_html}</td>
                    </tr>"""

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
        if charts_subdir:
            ev_chart_href = f"{charts_subdir.rstrip('/')}/{ev_slug}.html"
        else:
            ev_chart_href = f"hotel-charts/{ev_slug}.html"
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
        vanished_inner_html = f"""
                <p class="vanished-hint">Все отели, которые хотя бы раз были в зоне отслеживания (≤{ceiling_val:.0f} PLN), но сейчас не в основной таблице. Графики показывают полную историю до {history_val:.0f} PLN; если отель всё ещё в выдаче выше потолка — показываем актуальную цену.</p>
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
        vanished_inner_html = f"""
                <p class="vanished-hint">Пока нет отелей вне основной таблицы: все, кто когда-либо был в зоне ≤{ceiling_val:.0f} PLN, сейчас в ней же.</p>
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
{vanished_section_html}
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
        const detailedData = """ + json.dumps(top10_detailed_data, ensure_ascii=False, default=str) + """;
        
        if (Array.isArray(X) && Array.isArray(Y) && X.length > 0 && Y.length > 0 && window.Plotly) {
          // Подготавливаем данные для hover
          const hoverData = detailedData.map(data => data.hover_data || {});
          
          // Создаем простой текст для hover с правильными переносами строк
          const hoverTexts = detailedData.map((data, index) => {
            const hover = data.hover_data || {};
            let text = hover.title || '';
            
            // Добавляем среднюю цену
            if (hover.avg_price) {
              text += '<br><br><b>Средняя цена:</b><br>';
              text += `${Math.round(hover.avg_price)} PLN`;
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
          
          const trace = { 
            x: X, 
            y: Y, 
            type: 'scatter', 
            mode: 'lines+markers', 
            line: { color: '#A23B72', width: 3 }, 
            marker: { size: 8 },
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
            hovermode: 'closest'
          };
          
          Plotly.newPlot('avgTop10', [trace], layout);
        }
      })();
      
      // График индекса ценовой динамики
      (function(){
        const trendIndexX = """ + json.dumps(trend_index_x_values, ensure_ascii=False) + """;
        const trendIndexY = """ + json.dumps(trend_index_y_values, ensure_ascii=False) + """;
        const trendIndexDetailedData = """ + json.dumps(trend_index_detailed_data, ensure_ascii=False, default=str) + """;
        
        if (Array.isArray(trendIndexX) && Array.isArray(trendIndexY) && trendIndexX.length > 0 && trendIndexY.length > 0 && window.Plotly) {
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
          
          Plotly.newPlot('trendIndexChart', [trendIndexTrace], trendIndexLayout);
        }
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
        function show(e, name){ const url = map[name]; if(!url){ return; } img.src = url; hover.style.display = 'block'; hover.style.left = ((e.pageX||0)+12) + 'px'; hover.style.top = ((e.pageY||0)+12) + 'px'; }
        function move(e){ if(hover.style.display === 'block'){ hover.style.left = ((e.pageX||0)+12) + 'px'; hover.style.top = ((e.pageY||0)+12) + 'px'; } }
        function hide(){ hover.style.display = 'none'; img.src = ''; }
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
        const columnMap = { 'hotel': 0, 'price': 1, 'deal': 2, 'delta48': 3, 'deltaavg': 4, 'dates': 5, 'duration': 6, 'offer': 7 };
        return columnMap[column];
      }

      function normalizeSortValue(row, column) {
        const idx = getColumnIndex(column);
        const cell = row.cells[idx];
        const raw = (cell && (cell.dataset.sortValue || cell.textContent) || '').trim();
        if (column === 'hotel') {
          return (row.cells[0].textContent || '').trim().toLowerCase();
        }
        if (column === 'dates') {
          const m = raw.match(/(\d{2})\.(\d{2})\.(\d{4})/);
          return m ? `${m[3]}-${m[2]}-${m[1]}` : raw.toLowerCase();
        }
        if (column === 'duration') {
          const m = raw.match(/(\d+)/);
          return m ? parseFloat(m[1]) : 0;
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
      
      function sortTable(column) {
        // Определяем направление сортировки
        if (currentSort.column === column) {
          currentSort.direction = currentSort.direction === 'asc' ? 'desc' : 'asc';
        } else {
          currentSort.direction = 'asc';
        }
        currentSort.column = column;

        if (window._hotelTableSortAll) {
          window._hotelTableSortAll();
        } else {
          const table = document.getElementById('hotelsTable');
          const tbody = table.querySelector('tbody');
          const rows = Array.from(tbody.querySelectorAll('tr'));
          rows.sort(compareHotelRows);
          rows.forEach(row => tbody.appendChild(row));
        }

        // Обновляем индикаторы сортировки
        updateSortIndicators();
      }
      
      function updateSortIndicators() {
        const headers = document.querySelectorAll('#hotelsTable th.sortable');
        headers.forEach(header => {
          header.classList.remove('sort-asc', 'sort-desc');
          if (header.dataset.sort === currentSort.column) {
            header.classList.add(currentSort.direction === 'asc' ? 'sort-asc' : 'sort-desc');
          }
        });
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
        el.addEventListener('toggle', () => {
          try { localStorage.setItem(key, el.open ? '1' : '0'); } catch (e) {}
          // Перерисовываем графики Plotly внутри: при закрытом details ширина была 0
          if (el.open && window.Plotly) {
            el.querySelectorAll('.js-plotly-plot').forEach(p => {
              try { window.Plotly.Plots.resize(p); } catch (e) {}
            });
          }
        });
      }

      // Добавляем обработчики кликов на заголовки
      document.addEventListener('DOMContentLoaded', function() {
        const headers = document.querySelectorAll('#hotelsTable th.sortable');
        headers.forEach(header => {
          header.addEventListener('click', () => sortTable(header.dataset.sort));
        });
        
        // Sidebar functionality
        const sidebar = document.getElementById('sidebar');
        const sidebarToggle = document.getElementById('sidebarToggle');
        const sidebarOverlay = document.getElementById('sidebarOverlay');
        const mainContent = document.getElementById('mainContent');
        
        function toggleSidebar() {
          sidebar.classList.toggle('open');
          sidebarOverlay.classList.toggle('open');
          mainContent.classList.toggle('sidebar-open');
        }
        
        sidebarToggle.addEventListener('click', toggleSidebar);
        sidebarOverlay.addEventListener('click', toggleSidebar);

        // Cards/Table view mode
        const cardsSection = document.getElementById('cardsSection');
        const tableSection = document.getElementById('tableSection');
        const alertsSection = document.getElementById('alertsSection');
        const modeSwitch = document.getElementById('modeSwitch');
        const modeButtons = modeSwitch ? Array.from(modeSwitch.querySelectorAll('.mode-btn')) : [];
        function setMode(mode) {
          const cardsMode = mode !== 'table';
          if (cardsSection) cardsSection.style.display = cardsMode ? '' : 'none';
          if (tableSection) tableSection.style.display = cardsMode ? 'none' : '';
          if (alertsSection) alertsSection.style.display = '';
          modeButtons.forEach(btn => btn.classList.toggle('active', btn.dataset.mode === mode));
          if (modeSwitch) modeSwitch.dataset.mode = mode;
          try { localStorage.setItem('dashboard_mode', mode); } catch(e) {}
        }
        modeButtons.forEach(btn => btn.addEventListener('click', () => setMode(btn.dataset.mode)));
        let initialMode = 'cards';
        try { initialMode = localStorage.getItem('dashboard_mode') || 'cards'; } catch(e) {}
        setMode(initialMode);
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
          themeToggle.textContent = '☀️';
        }
        
        themeToggle.addEventListener('click', function() {
          body.classList.toggle('dark-theme');
          const isDark = body.classList.contains('dark-theme');
          themeToggle.textContent = isDark ? '☀️' : '🌙';
          localStorage.setItem('theme', isDark ? 'dark' : 'light');
        });
        
        // Table filtering and pagination
        const searchInput = document.getElementById('searchInput');
        const priceFilter = document.getElementById('priceFilter');
        const changeFilter = document.getElementById('changeFilter');
        const clearFilters = document.getElementById('clearFilters');
        const table = document.getElementById('hotelsTable');
        const tbody = table.querySelector('tbody');
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
        const cardsPrevPage = document.getElementById('cardsPrevPage');
        const cardsNextPage = document.getElementById('cardsNextPage');
        const cardsShowingFrom = document.getElementById('cardsShowingFrom');
        const cardsShowingTo = document.getElementById('cardsShowingTo');
        const cardsTotalItems = document.getElementById('cardsTotalItems');
        let cardsPage = 1;
        const cardsPerPage = 24;

        function updateCardsPagination() {
          const total = cardItems.length;
          const totalPages = Math.max(1, Math.ceil(total / cardsPerPage));
          if (cardsPage > totalPages) cardsPage = totalPages;
          const start = (cardsPage - 1) * cardsPerPage;
          const end = start + cardsPerPage;

          cardItems.forEach((card, idx) => {
            card.style.display = idx >= start && idx < end ? '' : 'none';
          });

          if (cardsTotalItems) cardsTotalItems.textContent = String(total);
          if (cardsShowingFrom) cardsShowingFrom.textContent = total ? String(start + 1) : '0';
          if (cardsShowingTo) cardsShowingTo.textContent = total ? String(Math.min(end, total)) : '0';
          if (cardsPrevPage) cardsPrevPage.disabled = cardsPage <= 1;
          if (cardsNextPage) cardsNextPage.disabled = cardsPage >= totalPages;
        }

        function cardsNextPageFunc() {
          const totalPages = Math.max(1, Math.ceil(cardItems.length / cardsPerPage));
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
          
          filteredRows = rows.filter(row => {
            const hotelName = row.cells[0].textContent.toLowerCase();
            const price = parseFloat(row.cells[1].textContent.replace(/[^0-9.-]/g, ''));
            const delta48 = row.cells[3].textContent.trim();
            
            // Search filter
            if (searchTerm && !hotelName.includes(searchTerm)) {
              return false;
            }
            
            // Price filter (диапазоны генерируются динамически под текущий фильтр)
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
            
            return true;
          });

          if (currentSort.column) {
            filteredRows.sort(compareHotelRows);
          }
          
          currentPage = 1;
          updateTable();
        }
        
        function updateTable() {
          const startIndex = (currentPage - 1) * itemsPerPage;
          const endIndex = startIndex + itemsPerPage;
          const pageRows = filteredRows.slice(startIndex, endIndex);
          
          // Clear current rows
          tbody.innerHTML = '';
          
          // Add filtered rows
          pageRows.forEach(row => tbody.appendChild(row));
          
          // Update pagination info
          showingFrom.textContent = filteredRows.length > 0 ? startIndex + 1 : 0;
          showingTo.textContent = Math.min(endIndex, filteredRows.length);
          totalItems.textContent = filteredRows.length;
          
          // Update pagination buttons
          prevPage.disabled = currentPage === 1;
          nextPage.disabled = endIndex >= filteredRows.length;
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
        
        // Event listeners
        searchInput.addEventListener('input', filterRows);
        priceFilter.addEventListener('change', filterRows);
        changeFilter.addEventListener('change', filterRows);
        clearFilters.addEventListener('click', function() {
          searchInput.value = '';
          priceFilter.value = '';
          changeFilter.value = '';
          filterRows();
        });
        nextPage.addEventListener('click', nextPageFunc);
        prevPage.addEventListener('click', prevPageFunc);
        if (cardsNextPage) cardsNextPage.addEventListener('click', cardsNextPageFunc);
        if (cardsPrevPage) cardsPrevPage.addEventListener('click', cardsPrevPageFunc);
        
        // Initialize
        updateTable();
        updateCardsPagination();
      });
    </script>
"""
    html_template += """
    <script>
      (function(){
        const departureOffers = """ + departure_offers_json + """;
        const departurePriceCurves = """ + departure_price_curves_json + """;

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
          chartEl.style.display = 'block';
          const x = curve.days.map(function(d) { return 'D-' + d; });
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
          const chartHeight = 300;
          const layout = {
            height: chartHeight,
            autosize: false,
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
          Plotly.newPlot(chartEl, traces, layout, { responsive: false, displayModeBar: false });
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
            bodyEl.innerHTML = '<table class="departure-offers-table"><thead><tr><th>Отель</th><th>Цена</th><th>Deal</th><th title="Отклонение от типичной цены отеля по истории вылетов">Δ типич.</th><th>Ссылки</th></tr></thead><tbody>' + rows + '</tbody></table>';
          }

          modal.classList.add('open');
          modal.setAttribute('aria-hidden', 'false');
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

        document.addEventListener('DOMContentLoaded', bindDepartureOfferClicks);
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
    args = parser.parse_args()
    generate_inline_charts_dashboard(data_file=args.data_file, output_file=args.output, title=args.title, charts_subdir=args.charts_dir, tz=args.tz, alerts_file=args.alerts_file, all_airports_data_file=args.all_airports_data_file, disappeared_after_runs=args.disappeared_after_runs, display_price_ceiling=args.display_price_ceiling, history_price_ceiling=args.history_price_ceiling)
