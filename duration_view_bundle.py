"""Per-duration dashboard view bundles for fixed-trip filters."""

from __future__ import annotations

import html as html_lib
import json
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pandas as pd


def _reference_time(series: pd.Series) -> datetime:
    """Latest timestamp from a series, or now when history is empty."""
    if series is None or series.empty:
        return datetime.now()
    value = series.max()
    if value is None or pd.isna(value):
        return datetime.now()
    if hasattr(value, "to_pydatetime"):
        return value.to_pydatetime()
    return value

from departure_airports import arrival_hub_label
from departure_identity import parse_offer_path
from hotel_deal_score import (
    blend_tripadvisor_into_deal_score,
    build_premium_history_by_hotel,
    comeback_from_premium,
)


def _generate_top10_hover_data(detailed_data: dict) -> dict:
    hover_data = {
        'title': f"📊 ТОП-10 ({detailed_data['run_time']})",
        'avg_price': detailed_data.get('avg_price', 0),
        'avg_change': None,
        'price_changes': [],
        'new_hotels': [],
        'removed_hotels': [],
        'no_changes': False,
    }
    if detailed_data.get('avg_price_change', 0) != 0:
        change = detailed_data['avg_price_change']
        change_percent = detailed_data.get('avg_price_change_percent', 0)
        arrow = "↗️" if change > 0 else "↘️"
        sign = "+" if change > 0 else ""
        hover_data['avg_change'] = {
            'arrow': arrow,
            'change': change,
            'change_percent': change_percent,
            'sign': sign,
        }
    if detailed_data.get('price_changes'):
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
                'sign': sign,
            })
    if detailed_data.get('new_hotels'):
        for hotel in detailed_data['new_hotels']:
            hover_data['new_hotels'].append({
                'name': hotel['name'],
                'price': hotel['price'],
                'position': hotel['position'],
            })
    if detailed_data.get('removed_hotels'):
        for hotel in detailed_data['removed_hotels']:
            hover_data['removed_hotels'].append({
                'name': hotel['name'],
                'price': hotel['price'],
                'position': hotel['position'],
            })
    if (
        not detailed_data.get('price_changes')
        and not detailed_data.get('new_hotels')
        and not detailed_data.get('removed_hotels')
        and detailed_data.get('avg_price_change', 0) == 0
    ):
        hover_data['no_changes'] = True
    return hover_data


def _build_changes_html(
    decreases_24h,
    increases_24h,
    decreases_7d,
    increases_7d,
) -> str:
    changes_html = ""
    if decreases_24h or increases_24h:
        changes_html += '<div class="changes-section">'
        if decreases_24h:
            changes_html += '<div class="changes-block"><h3>📉 Наиболее подешевевшие (24ч)</h3>'
            for change in decreases_24h:
                changes_html += (
                    f'<div class="change-item change-decrease">'
                    f'<div><div class="hotel-name">{html_lib.escape(change["hotel_name"])}</div>'
                    f'<div class="change-percent">{change["change"]:+.0f} PLN ({change["change_percent"]:+.1f}%)</div></div>'
                    f'<div class="change-price">{change["old_price"]:.0f} → {change["new_price"]:.0f} PLN</div></div>'
                )
            changes_html += '</div>'
        if increases_24h:
            changes_html += '<div class="changes-block"><h3>📈 Наиболее подорожавшие (24ч)</h3>'
            for change in increases_24h:
                changes_html += (
                    f'<div class="change-item change-increase">'
                    f'<div><div class="hotel-name">{html_lib.escape(change["hotel_name"])}</div>'
                    f'<div class="change-percent">{change["change"]:+.0f} PLN ({change["change_percent"]:+.1f}%)</div></div>'
                    f'<div class="change-price">{change["old_price"]:.0f} → {change["new_price"]:.0f} PLN</div></div>'
                )
            changes_html += '</div>'
        changes_html += '</div>'
    if decreases_7d or increases_7d:
        changes_html += '<div class="changes-section">'
        if decreases_7d:
            changes_html += '<div class="changes-block"><h3>📉 Наиболее подешевевшие (7д)</h3>'
            for change in decreases_7d:
                changes_html += (
                    f'<div class="change-item change-decrease">'
                    f'<div><div class="hotel-name">{html_lib.escape(change["hotel_name"])}</div>'
                    f'<div class="change-percent">{change["change"]:+.0f} PLN ({change["change_percent"]:+.1f}%)</div></div>'
                    f'<div class="change-price">{change["old_price"]:.0f} → {change["new_price"]:.0f} PLN</div></div>'
                )
            changes_html += '</div>'
        if increases_7d:
            changes_html += '<div class="changes-block"><h3>📈 Наиболее подорожавшие (7д)</h3>'
            for change in increases_7d:
                changes_html += (
                    f'<div class="change-item change-increase">'
                    f'<div><div class="hotel-name">{html_lib.escape(change["hotel_name"])}</div>'
                    f'<div class="change-percent">{change["change"]:+.0f} PLN ({change["change_percent"]:+.1f}%)</div></div>'
                    f'<div class="change-price">{change["old_price"]:.0f} → {change["new_price"]:.0f} PLN</div></div>'
                )
            changes_html += '</div>'
        changes_html += '</div>'
    return changes_html


def _build_entry_signal_html(
    entry_candidates,
    entry_top,
    market_breadth: float,
    entry_signal_level: str,
    entry_signal_title: str,
    entry_signal_note: str,
) -> str:
    signal_class = f"entry-signal entry-{entry_signal_level}"
    if entry_top:
        items = []
        for item in entry_top:
            items.append(
                f'<div class="entry-item">'
                f'<div><div class="hotel-name">{html_lib.escape(item["hotel_name"])}</div>'
                f'<div class="change-percent">Deal Score: {item["deal_score"]} ({item.get("confidence", "")}) • '
                f'Δ48ч {item["delta48_pct"]:+.1f}%</div></div>'
                f'<div class="change-price">{item["latest"]:.0f} PLN</div></div>'
            )
        signal_items_html = "".join(items)
    else:
        signal_items_html = "<div class='alerts-empty'>Пока нет кандидатов под строгие критерии раннего входа.</div>"
    return (
        f'<div class="{signal_class}">'
        f'<div class="entry-title">{html_lib.escape(entry_signal_title)}</div>'
        f'<div class="entry-note">{html_lib.escape(entry_signal_note)}</div>'
        f'<div class="entry-stats">Кандидаты: {len(entry_candidates)} • '
        f'Доля отелей со снижением (48ч): {market_breadth * 100:.1f}%</div>'
        f'<div class="entry-list">{signal_items_html}</div></div>'
    )


def pack_duration_view_bundle(
    *,
    entry_candidates_count: int,
    market_breadth_pct: float,
    best_deal_score: int,
    total_offers: int,
    unique_hotels: int,
    current_table_hotels: int,
    avg_price: float,
    history_min_price: float,
    history_max_price: float,
    avg_deal_score: int,
    entry_hotspots: int,
    top10_x: list,
    top10_y: list,
    top10_detailed: list,
    trend_x: list,
    trend_y: list,
    trend_detailed: list,
    offers_count_dates: list,
    offers_count_values: list,
    offers_count_meta: list,
    changes_html: str,
    entry_signal_html: str,
    cards_html: str,
    table_rows_html: str,
    price_filter_options_html: str,
    region_filter_options_html: str,
    hotel_names: list,
    label: str = "",
    alerts_chips_html: str = "",
    alerts_content_html: str = "",
) -> dict:
    return {
        'label': label,
        'hero': {
            'entry_candidates': entry_candidates_count,
            'market_breadth_pct': round(market_breadth_pct, 1),
            'best_deal_score': best_deal_score,
        },
        'stats': {
            'total_offers': total_offers,
            'unique_hotels': unique_hotels,
            'current_table_hotels': current_table_hotels,
            'avg_price': round(avg_price),
            'history_min_price': round(history_min_price),
            'history_max_price': round(history_max_price),
            'avg_deal_score': avg_deal_score,
            'best_deal_score': best_deal_score,
            'entry_hotspots': entry_hotspots,
            'market_breadth_pct': round(market_breadth_pct, 1),
        },
        'charts': {
            'top10_x': top10_x,
            'top10_y': top10_y,
            'top10_detailed': top10_detailed,
            'trend_x': trend_x,
            'trend_y': trend_y,
            'trend_detailed': trend_detailed,
            'offers_count_dates': offers_count_dates,
            'offers_count_values': offers_count_values,
            'offers_count_meta': offers_count_meta,
        },
        'html': {
            'changes': changes_html,
            'entry_signal': entry_signal_html,
            'cards': cards_html,
            'table_rows': table_rows_html,
            'price_filter_options': price_filter_options_html,
            'region_filter_options': region_filter_options_html,
            'alerts_chips': alerts_chips_html,
            'alerts_content': alerts_content_html,
        },
        'hotel_names': hotel_names,
    }


def build_duration_view_bundle(
    df: pd.DataFrame,
    *,
    group_cols: List[str],
    use_trip_buckets: bool,
    ceiling_val: Optional[float],
    history_val: Optional[float],
    data_file: str,
    config_file: Optional[str],
    filter_data_id: str,
    price_scope_tip: str,
    skip_ta_backfill: bool = True,
    alerts: Optional[list] = None,
    hotel_meta_by_name: Optional[dict] = None,
    alert_threshold_percent: float = 8.0,
    parse_iso_fn=None,
    duration_bucket: str = "",
) -> dict:
    """Recompute dashboard sections for one duration scope."""
    import generate_inline_charts_dashboard as g

    df_canonical = g.collapse_canonical_per_run(df, ceiling_val, group_cols=group_cols)
    df_history = g.collapse_canonical_per_run(df, history_val, group_cols=group_cols)
    df_full = g.collapse_canonical_per_run(df, None, group_cols=group_cols)

    run_slices = list(g.iter_scrape_runs(df_canonical))
    top10_x_values: List[str] = []
    top10_y_values: List[float] = []
    top10_detailed_data: List[dict] = []
    run_data = []
    chart_group_cols = group_cols

    for _, _, run_data_slice in run_slices:
        if run_data_slice.empty:
            continue
        run_time = run_data_slice['scraped_at_display'].iloc[0]
        latest_prices = []
        row_prices = {}
        for group_key, hotel_grp in run_data_slice.groupby(chart_group_cols):
            hotel_name, _, row_id = g._unpack_table_group_key(group_key, use_trip_buckets)
            if hotel_grp.empty:
                continue
            latest_price = float(hotel_grp['price'].astype(float).min())
            latest_prices.append(latest_price)
            row_prices[row_id] = (hotel_name, latest_price)
        if not latest_prices:
            continue
        sorted_prices = sorted(latest_prices)
        top_n = sorted_prices[: min(10, len(sorted_prices))]
        avg_price = sum(top_n) / len(top_n)
        top_hotels = []
        for row_id, (hotel_name, price) in row_prices.items():
            if price in top_n:
                top_hotels.append({
                    'name': hotel_name,
                    'price': price,
                    'position': sorted_prices.index(price) + 1,
                })
        top_hotels.sort(key=lambda x: x['position'])
        run_data.append((run_time, avg_price))
        top10_detailed_data.append({
            'run_time': run_time,
            'avg_price': avg_price,
            'top10_hotels': top_hotels,
        })

    if run_data:
        top10_x_values = [pd.Timestamp(ts).isoformat() for ts, _ in run_data]
        top10_y_values = [float(price) for _, price in run_data]
        for i, detailed in enumerate(top10_detailed_data):
            if i == 0:
                detailed.update({
                    'price_changes': [],
                    'new_hotels': [],
                    'removed_hotels': [],
                    'avg_price_change': 0,
                    'avg_price_change_percent': 0,
                })
            else:
                prev = top10_detailed_data[i - 1]
                current_hotels = {h['name']: h for h in detailed['top10_hotels']}
                prev_hotels = {h['name']: h for h in prev['top10_hotels']}
                price_changes = []
                for name, cur in current_hotels.items():
                    if name in prev_hotels:
                        prev_price = prev_hotels[name]['price']
                        cur_price = cur['price']
                        if prev_price != cur_price:
                            price_changes.append({
                                'name': name,
                                'old_price': prev_price,
                                'new_price': cur_price,
                                'change': cur_price - prev_price,
                                'change_percent': ((cur_price - prev_price) / prev_price) * 100,
                                'position': cur['position'],
                            })
                new_hotels = [
                    {'name': n, 'price': current_hotels[n]['price'], 'position': current_hotels[n]['position']}
                    for n in current_hotels if n not in prev_hotels
                ]
                removed_hotels = [
                    {'name': n, 'price': prev_hotels[n]['price'], 'position': prev_hotels[n]['position']}
                    for n in prev_hotels if n not in current_hotels
                ]
                detailed['price_changes'] = price_changes
                detailed['new_hotels'] = new_hotels
                detailed['removed_hotels'] = removed_hotels
                prev_avg = prev['avg_price']
                detailed['avg_price_change'] = detailed['avg_price'] - prev_avg
                detailed['avg_price_change_percent'] = (
                    (detailed['avg_price'] - prev_avg) / prev_avg * 100 if prev_avg else 0
                )
            detailed['hover_data'] = _generate_top10_hover_data(detailed)

    trend_index_x_values: List[str] = []
    trend_index_y_values: List[float] = []
    trend_index_detailed_data: List[dict] = []
    prev_hotel_prices = {}
    for _, _, run_data_slice in run_slices:
        if run_data_slice.empty:
            continue
        run_time = run_data_slice['scraped_at_display'].iloc[0]
        current_hotel_prices = {}
        for group_key, hotel_grp in run_data_slice.groupby(chart_group_cols):
            _, _, row_id = g._unpack_table_group_key(group_key, use_trip_buckets)
            if not hotel_grp.empty:
                current_hotel_prices[row_id] = float(hotel_grp['price'].astype(float).min())
        total_price_change = 0.0
        hotels_with_changes = 0
        price_changes = []
        for row_id, current_price in current_hotel_prices.items():
            if row_id in prev_hotel_prices:
                prev_price = prev_hotel_prices[row_id]
                if prev_price > 0:
                    pct = (current_price - prev_price) / prev_price * 100
                    total_price_change += pct
                    hotels_with_changes += 1
                    price_changes.append({
                        'hotel': row_id.split('|', 1)[0],
                        'prev_price': prev_price,
                        'current_price': current_price,
                        'change_pct': pct,
                    })
        if hotels_with_changes > 0:
            avg_change = total_price_change / hotels_with_changes
            trend_index_x_values.append(pd.Timestamp(run_time).isoformat())
            trend_index_y_values.append(avg_change)
            trend_index_detailed_data.append({
                'run_time': run_time.strftime('%Y-%m-%d %H:%M'),
                'avg_change_pct': avg_change,
                'hotels_with_changes': hotels_with_changes,
                'total_hotels': len(current_hotel_prices),
                'price_changes': price_changes,
            })
        prev_hotel_prices = current_hotel_prices.copy()

    offers_count_timeline = g.build_daily_offers_count_timeline(
        df,
        ceiling_val=ceiling_val,
        group_cols=group_cols,
        pick='last',
    )

    latest_run_slice = g._last_run_slice(df_canonical)
    df_sorted_all = latest_run_slice.sort_values(group_cols + ['scraped_at_display'])
    latest_rows = []
    for group_key, grp in df_sorted_all.groupby(group_cols):
        hotel_name, bucket, row_id = g._unpack_table_group_key(group_key, use_trip_buckets)
        last = grp.sort_values('scraped_at_display').iloc[-1]
        latest_rows.append({
            'hotel_name': hotel_name,
            'duration_bucket': bucket,
            'row_id': row_id,
            'price': float(last['price']),
            'dates': last.get('dates', None),
            'duration': last.get('duration', None),
            'offer_url': last.get('offer_url', None),
            'image_url': last.get('image_url', None),
            'ta_rating': last.get('ta_rating', ''),
            'ta_review_count': last.get('ta_review_count', ''),
        })
    if not skip_ta_backfill:
        g._backfill_ta_for_latest_rows(latest_rows, df, config_file, data_file)

    all_hotels = pd.DataFrame(latest_rows).sort_values('price').reset_index(drop=True) if latest_rows else pd.DataFrame()
    table_prices = {row['row_id']: float(row['price']) for row in latest_rows}

    total_offers = len(df_canonical)
    unique_hotels = int(df_canonical['hotel_name'].nunique()) if not df_canonical.empty else 0
    avg_price = float(df_canonical['price'].mean()) if not df_canonical.empty else 0.0
    history_min_price = float(df_canonical['price'].min()) if not df_canonical.empty else 0.0
    history_max_price = float(df_canonical['price'].max()) if not df_canonical.empty else 0.0
    current_table_hotels = len(all_hotels)

    df_sorted = df_canonical.sort_values(group_cols + ['scraped_at_display'])
    df_sorted_full = df_full.sort_values(group_cols + ['scraped_at_display'])
    ref_time_series = df_canonical['scraped_at_display'] if not df_canonical.empty else df['scraped_at_display']

    def compute_changes(window_hours: int):
        cutoff = _reference_time(ref_time_series) - timedelta(hours=window_hours)
        changes = []
        deltas_map = {}
        for group_key, grp in df_sorted.groupby(group_cols):
            hotel_name, _, row_id = g._unpack_table_group_key(group_key, use_trip_buckets)
            if row_id not in table_prices:
                continue
            grp = grp.sort_values('scraped_at_display')
            latest_price = table_prices[row_id]
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
            })
            deltas_map[row_id] = (change, change_percent)
        decreases = sorted([h for h in changes if h['change'] < 0], key=lambda x: x['change'])[:5]
        increases = sorted([h for h in changes if h['change'] > 0], key=lambda x: x['change'], reverse=True)[:5]
        return decreases, increases, deltas_map

    decreases_48h, increases_48h, deltas_by_hotel = compute_changes(48)
    decreases_24h, increases_24h, _ = compute_changes(24)
    decreases_7d, increases_7d, _ = compute_changes(24 * 7)

    avg_baseline_delta = {}
    for row_id, last_price in table_prices.items():
        if use_trip_buckets:
            hotel_name, bucket, _ = g._unpack_table_group_key(
                tuple(row_id.split('|', 1)) if '|' in row_id else (row_id, ''),
                True,
            )
            mask = df_sorted_full['hotel_name'] == hotel_name
            if bucket:
                mask &= df_sorted_full['duration_bucket'].astype(str) == bucket
            grp = df_sorted_full[mask]
        else:
            grp = df_sorted_full[df_sorted_full['hotel_name'] == row_id]
        if grp.empty:
            avg_baseline_delta[row_id] = None
            continue
        baseline = g._time_weighted_price_baseline(grp.sort_values('scraped_at_display'))
        if baseline is None or baseline == 0:
            avg_baseline_delta[row_id] = None
            continue
        change_abs = float(last_price) - baseline
        avg_baseline_delta[row_id] = (change_abs, (change_abs / baseline) * 100.0)

    if use_trip_buckets:
        premium_history_by_hotel = g._build_premium_history_index(df_history, ceiling_val, group_cols)
    else:
        premium_history_by_hotel = build_premium_history_by_hotel(
            df_history, ceiling_val, time_col='scraped_at_display', price_col='price'
        )

    def _clamp(v, lo, hi):
        return max(lo, min(hi, v))

    deal_score_by_hotel = {}
    entry_candidates = []
    for group_key, grp in df_sorted.groupby(group_cols):
        hotel_name, bucket, row_id = g._unpack_table_group_key(group_key, use_trip_buckets)
        grp = grp.sort_values('scraped_at_display')
        if use_trip_buckets:
            mask = df_sorted_full['hotel_name'] == hotel_name
            if bucket:
                mask &= df_sorted_full['duration_bucket'].astype(str) == bucket
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
        typical = g._time_weighted_price_baseline(hist_grp) or latest
        median = float(typical)
        p25 = g._time_weighted_price_quantile(hist_grp, 0.25) or latest
        rel_discount = (median - latest) / median if median > 0 else 0.0
        score_discount = _clamp(50 + rel_discount * 200, 0, 100)
        if latest <= (g._time_weighted_price_quantile(hist_grp, 0.10) or latest):
            score_rarity = 100
        elif latest <= p25:
            score_rarity = 80
        elif latest <= median:
            score_rarity = 50
        else:
            score_rarity = 35
        recent = prices[-3:] if len(prices) >= 3 else prices
        score_momentum = 50
        if len(recent) >= 3 and (recent[-1] <= recent[-2] <= recent[-3]):
            score_momentum = 85
        elif len(recent) >= 2 and recent[-1] < recent[-2]:
            score_momentum = 70
        elif len(recent) >= 2 and recent[-1] > recent[-2]:
            score_momentum = 35
        cv = g._time_weighted_price_volatility(hist_grp)
        score_stability = 50
        if cv is not None:
            score_stability = 50 if cv < 0.01 else _clamp(70 - cv * 120, 20, 85)
        raw_deal_score = float(_clamp(
            score_discount * 0.40 + score_rarity * 0.30 + score_momentum * 0.20 + score_stability * 0.10,
            0, 100,
        ))
        confidence_weight = _clamp(samples / 20.0, 0.15, 1.0)
        deal_score = int(round(_clamp(50.0 + (raw_deal_score - 50.0) * confidence_weight, 0, 100)))
        confidence_level = "Low" if samples < 8 else ("Medium" if samples < 20 else "High")
        delta48_info = deltas_by_hotel.get(row_id)
        avg_info = avg_baseline_delta.get(row_id)
        d48_pct = float(delta48_info[1]) if delta48_info else None
        d_avg_pct = float(avg_info[1]) if avg_info else None
        comeback = comeback_from_premium(latest, premium_history_by_hotel.get(row_id), ceiling_val)
        comeback_drop_pct = float(comeback['drop_from_peak_pct']) if comeback else None
        if comeback_drop_pct is not None:
            deal_score = int(max(deal_score, round(_clamp(55 + comeback_drop_pct * 1.1, 55, 92))))
        is_bad = d48_pct is not None and d48_pct > 0 and d_avg_pct is not None and d_avg_pct > 0
        if is_bad and comeback_drop_pct is None:
            deal_score = int(_clamp(50 - ((d48_pct + d_avg_pct) / 2.0) * 1.2, 5, 42))
        ta_row = None
        if not all_hotels.empty:
            matches = all_hotels[all_hotels['row_id'].astype(str) == str(row_id)]
            if not matches.empty:
                ta_row = matches.iloc[0]
        ta_rating = ta_row.get('ta_rating') if ta_row is not None else ''
        ta_reviews = ta_row.get('ta_review_count') if ta_row is not None else ''
        deal_score, _ = blend_tripadvisor_into_deal_score(deal_score, ta_rating, ta_reviews)
        deal_score_by_hotel[row_id] = {
            'score': deal_score,
            'confidence': confidence_level,
            'comeback_drop_pct': comeback_drop_pct,
        }
        ta_rating_val = g._parse_ta_rating_value(ta_rating)
        ta_reviews_val = g._parse_ta_review_count(ta_reviews)
        ta_ok = ta_rating_val is None or ta_reviews_val < 15 or ta_rating_val >= 3.8
        if (
            delta48_info is not None and delta48_info[1] <= -2.0 and latest <= p25
            and deal_score >= 72 and confidence_level != "Low" and ta_ok
        ):
            entry_candidates.append({
                'hotel_name': hotel_name,
                'deal_score': deal_score,
                'latest': latest,
                'delta48_pct': float(delta48_info[1]),
                'confidence': confidence_level,
            })

    entry_candidates = sorted(
        entry_candidates,
        key=lambda x: (x['deal_score'], -x['latest']),
        reverse=True,
    )
    entry_top = entry_candidates[:5]
    current_hotels_for_breadth = set(all_hotels['row_id'].astype(str).tolist()) if not all_hotels.empty else set()
    breadth_total = breadth_down = 0
    for group_key, grp in df_sorted.groupby(group_cols):
        _, _, row_id = g._unpack_table_group_key(group_key, use_trip_buckets)
        if current_hotels_for_breadth and row_id not in current_hotels_for_breadth:
            continue
        grp = grp.sort_values('scraped_at_display')
        if len(grp) < 2:
            continue
        cutoff = _reference_time(df['scraped_at_display']) - timedelta(hours=48)
        win = grp[grp['scraped_at_display'] >= cutoff]
        baseline_row = win.iloc[0] if len(win) >= 2 else grp.iloc[-2]
        latest_price = float(grp.iloc[-1]['price'])
        baseline_price = float(baseline_row['price'])
        if baseline_price <= 0:
            continue
        breadth_total += 1
        if latest_price < baseline_price:
            breadth_down += 1
    market_breadth = (breadth_down / breadth_total) if breadth_total > 0 else 0.0

    if len(entry_candidates) >= 5 and market_breadth >= 0.45:
        entry_signal_level, entry_signal_title, entry_signal_note = (
            "high", "🔥 Сильный сигнал раннего входа",
            "Много отелей одновременно дешевеют и уже торгуются в нижнем квартиле своих цен.",
        )
    elif len(entry_candidates) >= 2 and market_breadth >= 0.30:
        entry_signal_level, entry_signal_title, entry_signal_note = (
            "medium", "⚡ Умеренный сигнал раннего входа",
            "Есть несколько сильных кандидатов; рынок начинает смещаться в сторону более выгодных цен.",
        )
    else:
        entry_signal_level, entry_signal_title, entry_signal_note = (
            "low", "🟢 Нейтральный сигнал",
            "Явного массового снижения пока нет, но отдельные выгодные точки могут появляться.",
        )

    changes_html = _build_changes_html(decreases_24h, increases_24h, decreases_7d, increases_7d)
    entry_signal_html = _build_entry_signal_html(
        entry_candidates, entry_top, market_breadth,
        entry_signal_level, entry_signal_title, entry_signal_note,
    )

    arrival_hub_by_hotel = {}
    arrival_hub_labels = set()
    for _, hotel_row in all_hotels.iterrows():
        path = parse_offer_path(str(hotel_row.get('offer_url') or ''))
        hub = arrival_hub_label(path.get('country'), path.get('region'))
        row_id = str(hotel_row.get('row_id') or hotel_row.get('hotel_name') or '')
        arrival_hub_by_hotel[row_id] = hub
        if hub and hub != '—':
            arrival_hub_labels.add(hub)

    cards_parts = []
    for _, hotel in all_hotels.head(200).iterrows():
        hotel_name = hotel['hotel_name']
        row_id = str(hotel.get('row_id') or hotel_name)
        bucket = str(hotel.get('duration_bucket') or '')
        price = float(hotel['price'])
        delta_info = deltas_by_hotel.get(row_id)
        avg_info = avg_baseline_delta.get(row_id)
        deal_info = deal_score_by_hotel.get(row_id, {'score': 0, 'confidence': 'Low'})
        deal_score = int(deal_info.get('score', 0))
        confidence = deal_info.get('confidence', 'Low')
        d48 = float(delta_info[1]) if delta_info else None
        d_avg = float(avg_info[1]) if avg_info else None
        _, deal_class, deal_label = g.classify_deal_badge(
            deal_score, confidence, d48, d_avg, deal_info.get('comeback_drop_pct'),
        )
        comeback = comeback_from_premium(price, premium_history_by_hotel.get(row_id), ceiling_val)
        comeback_html = (
            f'<span class="comeback-badge">{comeback["badge_html"]}</span>' if comeback else ''
        )
        img = hotel.get('image_url', '')
        img_html = (
            f'<img src="{html_lib.escape(str(img), quote=True)}" alt="hotel image" loading="lazy" '
            f'onerror="this.onerror=null;this.parentElement.innerHTML=\'<div>Фото отеля</div>\';" />'
        ) if img and pd.notna(img) and str(img).strip() else '<div>Фото отеля</div>'
        offer_url = str(hotel.get('offer_url') or '')
        offer_btn = (
            f'<a class="card-btn" href="{html_lib.escape(offer_url, quote=True)}" target="_blank">Открыть оффер</a>'
            if offer_url.strip() else '<span class="card-btn" style="opacity:.6;">Оффер недоступен</span>'
        )
        forecast = g.determine_price_forecast(
            deal_score, confidence, d_avg, d48, deal_info.get('comeback_drop_pct'),
        )
        dep_date_esc = html_lib.escape(str(hotel.get('departure_date') or ''), quote=True)
        dep_key_esc = html_lib.escape(str(hotel.get('departure_key') or ''), quote=True)

        chart_href = g._hotel_chart_viewer_href(filter_data_id, g.slugify(hotel_name))
        cards_parts.append(
            f'<article class="hotel-card" data-duration-bucket="{html_lib.escape(bucket, quote=True)}" data-departure-date="{dep_date_esc}" data-departure-key="{dep_key_esc}">'
            f'<div class="hotel-card-img">{img_html}<button class="watchlist-star-btn card-star" data-hotel-name="{html_lib.escape(str(hotel_name), quote=True)}" title="Добавить в избранное">☆</button></div>'
            f'<div class="hotel-card-body">'
            f'<h4 class="hotel-card-title">{html_lib.escape(str(hotel_name))}</h4>'
            f'<div class="hotel-card-meta"><div class="hotel-card-price">{price:.0f} PLN</div>'
            f'<span class="deal-pill {deal_class}">Deal {deal_score} • {deal_label}</span>'
            f'<span class="forecast-pill {forecast["class"]}">{forecast["icon"]} {forecast["text"]}</span></div>'
            f'{comeback_html}'
            f'<div class="hotel-card-stats">'
            f'<div>Δ48ч: <strong>{f"{d48:+.1f}%" if d48 is not None else "—"}</strong></div>'
            f'<div>Δср: <strong>{f"{d_avg:+.1f}%" if d_avg is not None else "—"}</strong></div>'
            f'<div>{html_lib.escape(str(hotel.get("duration") or "—"))}</div>'
            f'<div>{html_lib.escape(confidence)} confidence</div></div>'
            f'<div class="hotel-card-actions">'
            f'<a class="card-btn primary" href="{html_lib.escape(chart_href, quote=True)}" target="_blank">График</a>'
            f'{offer_btn}</div></div></article>'
        )
    cards_html = "".join(cards_parts)

    table_rows_parts = []
    for i, (_, hotel) in enumerate(all_hotels.iterrows()):
        hotel_name = hotel['hotel_name']
        row_id = str(hotel.get('row_id') or hotel_name)
        bucket = str(hotel.get('duration_bucket') or '')
        price = float(hotel['price'])
        dates = hotel.get('dates', '—')
        duration = hotel.get('duration', '—')
        delta_info = deltas_by_hotel.get(row_id)
        avg_info = avg_baseline_delta.get(row_id)
        delta_display = "—"
        delta_class = "delta flat"
        if delta_info is not None:
            delta_abs, delta_pct = delta_info
            arrow = '↑' if delta_abs > 0 else ('↓' if delta_abs < 0 else '→')
            delta_class = 'delta up' if delta_abs > 0 else ('delta down' if delta_abs < 0 else 'delta flat')
            sign = '+' if delta_abs > 0 else ('' if delta_abs < 0 else '')
            delta_display = f"{arrow} {sign}{delta_pct:.1f}%"
        avg_display = "—"
        avg_sort_value = 0
        if avg_info is not None:
            avg_abs, avg_pct = avg_info
            arrow2 = '↑' if avg_abs > 0 else ('↓' if avg_abs < 0 else '→')
            sign2 = '+' if avg_abs > 0 else ('' if avg_abs < 0 else '')
            avg_display = f"{arrow2} {sign2}{avg_pct:.1f}%"
            avg_sort_value = avg_pct
        deal_info = deal_score_by_hotel.get(row_id, {'score': 0, 'confidence': 'Low'})
        deal_score = int(deal_info.get('score', 0))
        confidence_level = deal_info.get('confidence', 'Low')
        d48_tbl = float(delta_info[1]) if delta_info else None
        d_avg_tbl = float(avg_info[1]) if avg_info else None
        _, _, deal_badge = g.classify_deal_badge(
            deal_score, confidence_level, d48_tbl, d_avg_tbl, deal_info.get('comeback_drop_pct'),
        )
        confidence_short = "Low" if confidence_level == "Low" else ("Med" if confidence_level == "Medium" else "High")
        deal_title = html_lib.escape(f"{deal_score} {deal_badge} · {confidence_level}")
        duration_display = g._table_duration_compact(duration)
        duration_title = html_lib.escape(str(duration))
        comeback = comeback_from_premium(price, premium_history_by_hotel.get(row_id), ceiling_val)
        comeback_cell = ""
        if comeback:
            peak = float(comeback['peak_price'])
            drop = float(comeback['drop_from_peak_pct'])
            comeback_cell = (
                f'<span class="comeback-badge" title="{html_lib.escape(f"Было до {peak:.0f} PLN (−{drop:.0f}%)", quote=True)}">'
                f'↩ −{drop:.0f}%</span>'
            )
        arrival_hub = arrival_hub_by_hotel.get(row_id, '—')
        chart_href = g._hotel_chart_viewer_href(filter_data_id, g.slugify(hotel_name))
        offer_url = str(hotel.get('offer_url') or '')
        offer_link_html = (
            f'<a href="{html_lib.escape(offer_url, quote=True)}" target="_blank" class="col-link-btn" title="Открыть предложение"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg></a>'
            if offer_url and offer_url.strip() else '—'
        )
        ta_html = g._render_ta_rating_html(hotel.get('ta_rating', ''), hotel.get('ta_review_count', ''))
        ta_sort_val = g._parse_ta_rating_value(hotel.get('ta_rating', ''))
        ta_data_attr = f"{ta_sort_val:.2f}" if ta_sort_val is not None else "-1"
        forecast = g.determine_price_forecast(
            deal_score, confidence_level, d_avg_tbl, d48_tbl,
            deal_info.get('comeback_drop_pct'),
        )
        dep_date_esc = html_lib.escape(str(hotel.get('departure_date') or ''), quote=True)
        dep_key_esc = html_lib.escape(str(hotel.get('departure_key') or ''), quote=True)
        
        table_rows_parts.append(
            f'<tr class="hotel-row {"row-odd" if i % 2 == 0 else "row-even"}" data-region="{html_lib.escape(str(arrival_hub))}" data-ta-rating="{ta_data_attr}" '
            f'data-duration-bucket="{html_lib.escape(bucket, quote=True)}" data-departure-date="{dep_date_esc}" data-departure-key="{dep_key_esc}">'
            f'<td class="hotel-name col-hotel" data-label="Отель"><button class="watchlist-star-btn" data-hotel-name="{html_lib.escape(str(hotel_name), quote=True)}" title="Добавить в избранное">☆</button><a class="open-chart-link hotel-hover-link" href="{chart_href}" '
            f'target="_blank" data-hotel-name="{html_lib.escape(str(hotel_name), quote=True)}">'
            f'{html_lib.escape(str(hotel_name))}</a></td>'
            f'<td class="price col-tight" data-label="Цена" data-sort-value="{price}"><span class="price-main">{price:.0f} PLN</span>{comeback_cell}</td>'
            f'<td class="col-tight col-w-deal-td" data-label="Deal" data-sort-value="{deal_score}" title="{deal_title}">'
            f'<span class="deal-cell-inline">{deal_score} <span style="opacity:.85;">{deal_badge}</span> '
            f'<span class="deal-conf-short">{confidence_short}</span></span></td>'
            f'<td class="col-tight col-w-forecast-td" data-label="Прогноз" data-sort-value="{forecast["text"]}">'
            f'<span class="forecast-pill {forecast["class"]}">{forecast["icon"]} {forecast["text"]}</span></td>'
            f'<td class="col-tight col-w-ta-td col-hide-sm" data-label="TripAdvisor">{ta_html}</td>'
            f'<td class="col-tight col-w-d48-td" data-label="Δ 48ч" data-sort-value="{d48_tbl or 0}"><span class="{delta_class}">{delta_display}</span></td>'
            f'<td class="col-tight col-w-davg-td col-hide-sm" data-label="Δ к средней" data-sort-value="{avg_sort_value}">{avg_display}</td>'
            f'<td class="arrival-hub col-tight" data-label="Регион" data-sort-value="{html_lib.escape(str(arrival_hub))}">{html_lib.escape(str(arrival_hub))}</td>'
            f'<td class="col-tight col-dates" data-label="Даты" data-sort-value="{dates}">{dates}</td>'
            f'<td class="col-tight col-w-dur-td col-duration" data-label="Длительность" data-sort-value="{duration}" title="{duration_title}">{duration_display}</td>'
            f'<td class="offer-link-cell col-tight" data-label="Ссылка">{offer_link_html}</td></tr>'
        )
    table_rows_html = "".join(table_rows_parts)

    try:
        _pr = pd.to_numeric(all_hotels['price'], errors='coerce').dropna()
        _pr = _pr[_pr > 0]
    except Exception:
        _pr = pd.Series([], dtype='float64')
    if len(_pr) >= 2:
        _lo = float(_pr.quantile(0.02))
        _hi = float(_pr.quantile(0.98))
        if _hi - _lo < 500:
            _lo, _hi = float(_pr.min()), float(_pr.max())
        _step_base = 500
        _lo_r = int(_lo // _step_base) * _step_base
        _hi_r = int(math.ceil(_hi / _step_base)) * _step_base
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

    region_filter_options_html = "".join(
        f'<option value="{html_lib.escape(label)}">{html_lib.escape(label)}</option>'
        for label in sorted(arrival_hub_labels)
    )

    avg_deal_score = (
        int(round(sum(v['score'] for v in deal_score_by_hotel.values()) / len(deal_score_by_hotel)))
        if deal_score_by_hotel else 0
    )
    best_deal_score = max((v['score'] for v in deal_score_by_hotel.values()), default=0)

    stats_row1 = "".join([
        g._metric_card(f"{total_offers:,}", "Проверок цен", "Сколько раз мы замерили цены."),
        g._metric_card(str(unique_hotels), "Отелей отслеживали", "Сколько разных отелей попадали в фильтр."),
        g._metric_card(str(current_table_hotels), "Сейчас в списке", "Сколько отелей в последнем замере."),
        g._metric_card(f"{avg_price:.0f} PLN", "Средняя цена", price_scope_tip),
        g._metric_card(f"{history_min_price:.0f} PLN", "Самая низкая", price_scope_tip),
        g._metric_card(f"{history_max_price:.0f} PLN", "Самая высокая", price_scope_tip),
    ])
    stats_row2 = "".join([
        g._metric_card(str(avg_deal_score), "Средняя выгодность", "Deal Score по текущему срезу."),
        g._metric_card(str(best_deal_score), "Лучший шанс", "Максимальный Deal Score."),
        g._metric_card(str(len(entry_candidates)), "Горячие точки", "Кандидаты раннего входа."),
        g._metric_card(f"{market_breadth * 100:.0f}%", "Подешевело за 2 дня", "Доля отелей со снижением за 48ч."),
    ])

    hotel_names = sorted({str(h) for h in all_hotels['hotel_name'].astype(str).tolist()}) if not all_hotels.empty else []

    scope_hotel_names = (
        set(df_canonical['hotel_name'].astype(str).tolist()) if not df_canonical.empty else set()
    )
    latest_run_ts = _reference_time(df_canonical['scraped_at_display']) if not df_canonical.empty else None
    alerts_chips_html = ""
    alerts_content_html = ""
    if alerts is not None:
        parse_fn = parse_iso_fn or (lambda ts: datetime.min.replace(tzinfo=timezone.utc))
        alerts_chips_html, alerts_content_html = g._build_alerts_panel_html(
            alerts=alerts,
            table_prices=table_prices,
            premium_history_by_hotel=premium_history_by_hotel,
            scope_hotel_names=scope_hotel_names,
            hotel_meta_by_name=hotel_meta_by_name or {},
            latest_run_ts=latest_run_ts,
            ceiling_val=ceiling_val,
            alert_threshold_percent=alert_threshold_percent,
            parse_iso_fn=parse_fn,
            scope_duration_bucket=duration_bucket,
        )

    return pack_duration_view_bundle(
        entry_candidates_count=len(entry_candidates),
        market_breadth_pct=market_breadth * 100,
        best_deal_score=best_deal_score,
        total_offers=total_offers,
        unique_hotels=unique_hotels,
        current_table_hotels=current_table_hotels,
        avg_price=avg_price,
        history_min_price=history_min_price,
        history_max_price=history_max_price,
        avg_deal_score=avg_deal_score,
        entry_hotspots=len(entry_candidates),
        top10_x=top10_x_values,
        top10_y=top10_y_values,
        top10_detailed=top10_detailed_data,
        trend_x=trend_index_x_values,
        trend_y=trend_index_y_values,
        trend_detailed=trend_index_detailed_data,
        offers_count_dates=offers_count_timeline['dates'],
        offers_count_values=offers_count_timeline['counts'],
        offers_count_meta=offers_count_timeline['meta'],
        changes_html=changes_html,
        entry_signal_html=entry_signal_html,
        cards_html=cards_html,
        table_rows_html=table_rows_html,
        price_filter_options_html=price_filter_options_html,
        region_filter_options_html=region_filter_options_html,
        hotel_names=hotel_names,
        alerts_chips_html=alerts_chips_html,
        alerts_content_html=alerts_content_html,
    ) | {
        'stats_row1_html': stats_row1,
        'stats_row2_html': stats_row2,
    }


def duration_views_json(views: Dict[str, dict]) -> str:
    return json.dumps(views, ensure_ascii=False, default=str)
