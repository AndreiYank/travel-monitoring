#!/usr/bin/env python3
"""Per-hotel Deal Score and Δ vs typical price (shared with departure analytics)."""

from __future__ import annotations

from typing import Any, Dict, Optional

import pandas as pd


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _typical_observation_gap_hours(times: list) -> float:
    if len(times) < 2:
        return 1.0
    gaps = []
    for i in range(1, len(times)):
        gaps.append((times[i] - times[i - 1]).total_seconds() / 3600.0)
    gaps.sort()
    return max(0.5, float(gaps[len(gaps) // 2]))


def _data_gap_threshold_hours(typical_gap: float) -> float:
    return max(6.0, typical_gap * 4.0)


def _iter_price_plateaus(times, prices):
    if not times or not prices or len(times) != len(prices):
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
                hours = typical_gap if end == idx else (
                    (times[end] - times[idx]).total_seconds() / 3600.0 + typical_gap
                )
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


def _price_plateau_segments(grp: pd.DataFrame, time_col: str, price_col: str):
    if grp is None or grp.empty:
        return []
    work = grp.sort_values(time_col).dropna(subset=[time_col, price_col])
    if work.empty:
        return []
    times = pd.to_datetime(work[time_col], utc=True).tolist()
    prices = work[price_col].astype(float).tolist()
    return list(_iter_price_plateaus(times, prices))


def time_weighted_price_baseline(grp: pd.DataFrame, time_col: str = "scraped_at", price_col: str = "price"):
    segments = _price_plateau_segments(grp, time_col, price_col)
    if not segments:
        return None
    total_hours = sum(h for _, h in segments)
    if total_hours <= 0:
        return float(segments[-1][0])
    return sum(p * h for p, h in segments) / total_hours


def time_weighted_price_quantile(
    grp: pd.DataFrame, quantile: float, time_col: str = "scraped_at", price_col: str = "price"
):
    segments = _price_plateau_segments(grp, time_col, price_col)
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


def time_weighted_price_volatility(grp: pd.DataFrame, time_col: str = "scraped_at", price_col: str = "price"):
    segments = _price_plateau_segments(grp, time_col, price_col)
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


def compute_hotel_deal_metrics(
    hist_df: pd.DataFrame,
    latest_price: float,
    time_col: str = "scraped_at",
    price_col: str = "price",
) -> Dict[str, Any]:
    """Deal Score + Δ к типичной цене (как в дашборде, по истории офферов отеля)."""
    empty = {"deal_score": 0, "avg_delta_pct": 0.0, "confidence": "Low"}
    if hist_df is None or hist_df.empty:
        return empty
    try:
        latest = float(latest_price)
    except (TypeError, ValueError):
        return empty

    work = hist_df.dropna(subset=[time_col, price_col]).copy()
    if work.empty:
        return empty
    work[price_col] = pd.to_numeric(work[price_col], errors="coerce")
    work = work.dropna(subset=[price_col])
    if work.empty:
        return empty

    work = work.sort_values(time_col)
    prices = work[price_col].astype(float).tolist()
    samples = len(prices)
    typical = time_weighted_price_baseline(work, time_col, price_col) or latest
    median = float(typical)
    p25 = time_weighted_price_quantile(work, 0.25, time_col, price_col) or latest
    p10 = time_weighted_price_quantile(work, 0.10, time_col, price_col) or latest

    rel_discount = (median - latest) / median if median > 0 else 0.0
    score_discount = _clamp(50 + rel_discount * 200, 0, 100)

    if latest <= p10:
        score_rarity = 100
    elif latest <= p25:
        score_rarity = 80
    elif latest <= median:
        score_rarity = 50
    else:
        score_rarity = 35

    recent = prices[-3:] if len(prices) >= 3 else prices
    if len(recent) >= 3 and recent[-1] <= recent[-2] <= recent[-3]:
        score_momentum = 85
    elif len(recent) >= 2 and recent[-1] < recent[-2]:
        score_momentum = 70
    elif len(recent) >= 2 and recent[-1] > recent[-2]:
        score_momentum = 35
    else:
        score_momentum = 50

    score_stability = 50
    cv = time_weighted_price_volatility(work, time_col, price_col)
    if cv is not None:
        score_stability = _clamp(70 - cv * 120, 20, 85) if cv >= 0.01 else 50

    raw_deal_score = (
        score_discount * 0.40
        + score_rarity * 0.30
        + score_momentum * 0.20
        + score_stability * 0.10
    )
    confidence_weight = _clamp(samples / 20.0, 0.15, 1.0)
    deal_score = int(round(_clamp(50 + (raw_deal_score - 50) * confidence_weight, 0, 100)))

    if samples < 8:
        confidence = "Low"
    elif samples < 20:
        confidence = "Medium"
    else:
        confidence = "High"

    avg_delta_pct = round((latest - median) / median * 100.0, 2) if median > 0 else 0.0

    return {
        "deal_score": deal_score,
        "avg_delta_pct": avg_delta_pct,
        "confidence": confidence,
    }
