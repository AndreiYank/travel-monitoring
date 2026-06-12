#!/usr/bin/env python3
"""Per-hotel Deal Score and Δ vs typical price (shared with departure analytics)."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

MIN_PREMIUM_PLATEAU_HOURS = 6.0
MIN_PREMIUM_OBSERVATIONS = 2
ISOLATED_SPIKE_NEIGHBOR_RATIO = 0.55
# TripAdvisor: полный вес влияния на Deal Score при достаточном числе отзывов.
TA_DEAL_FULL_WEIGHT_REVIEWS = 50
TA_DEAL_NEUTRAL_RATING = 3.8
TA_DEAL_MAX_ADJUST = 8.0


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


def robust_premium_peak(
    grp: pd.DataFrame,
    display_ceiling: float,
    time_col: str = "scraped_at",
    price_col: str = "price",
) -> Optional[float]:
    """Peak above display ceiling, ignoring short-lived isolated spikes (scraping glitches)."""
    if grp is None or grp.empty or display_ceiling is None:
        return None
    ceiling = float(display_ceiling)
    work = grp.dropna(subset=[time_col, price_col]).copy()
    if work.empty:
        return None
    work[price_col] = pd.to_numeric(work[price_col], errors="coerce")
    work = work.dropna(subset=[price_col]).sort_values(time_col)
    segments = _price_plateau_segments(work, time_col, price_col)
    if not segments:
        return None

    obs_above = work[work[price_col] > ceiling]
    obs_count_by_price: Dict[float, int] = {}
    for price, count in obs_above[price_col].astype(float).value_counts().items():
        obs_count_by_price[float(price)] = int(count)

    candidates: List[float] = []
    for i, (price, hours) in enumerate(segments):
        price = float(price)
        if price <= ceiling:
            continue
        obs_n = obs_count_by_price.get(price, 0)
        sustained = hours >= MIN_PREMIUM_PLATEAU_HOURS or obs_n >= MIN_PREMIUM_OBSERVATIONS
        if not sustained:
            continue
        if 0 < i < len(segments) - 1:
            prev_p = float(segments[i - 1][0])
            next_p = float(segments[i + 1][0])
            threshold = price * ISOLATED_SPIKE_NEIGHBOR_RATIO
            if prev_p < threshold and next_p < threshold:
                continue
        candidates.append(price)
    return max(candidates) if candidates else None


def build_premium_history_by_hotel(
    df_history: pd.DataFrame,
    display_ceiling: Optional[float],
    time_col: str = "scraped_at",
    price_col: str = "price",
) -> Dict[str, Dict[str, Any]]:
    """Peak prices per hotel in the full history band (up to history ceiling)."""
    out: Dict[str, Dict[str, Any]] = {}
    if df_history is None or df_history.empty:
        return out
    for name, grp in df_history.groupby("hotel_name", sort=False):
        prices = grp[price_col].astype(float)
        if prices.empty:
            continue
        premium_peak = robust_premium_peak(grp, display_ceiling, time_col, price_col)
        out[str(name)] = {
            "history_max": float(prices.max()),
            "premium_peak": premium_peak,
        }
    return out


def comeback_from_premium(
    current_price,
    premium_info: Optional[Dict[str, Any]],
    display_ceiling,
    min_drop_pct: float = 8.0,
) -> Optional[Dict[str, Any]]:
    """Hotel re-entered display band after being much more expensive in history."""
    if not premium_info or display_ceiling is None:
        return None
    try:
        current = float(current_price)
    except (TypeError, ValueError):
        return None
    if current > float(display_ceiling):
        return None
    peak = premium_info.get("premium_peak")
    if peak is None or peak <= float(display_ceiling):
        return None
    drop = (float(peak) - current) / float(peak) * 100.0
    if drop < min_drop_pct:
        return None
    return {
        "peak_price": float(peak),
        "drop_from_peak_pct": drop,
        "badge_html": (
            f"↩ Было до {peak:.0f} PLN"
            f' <span style="opacity:.85">(−{drop:.0f}%)</span>'
        ),
    }


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


def _listing_page_url(base_url: str, page_number: int) -> str:
    if page_number <= 1:
        return base_url
    if "?" in base_url:
        path, query = base_url.split("?", 1)
    else:
        path, query = base_url, ""
    query = re.sub(r"filter(?:\[|%5B)fp(?:\]|%5D)=[^&]*&?", "", query).strip("&")
    if not path.endswith("/"):
        path += "/"
    query_part = f"?{query}" if query else ""
    return f"{path}p:{page_number}/{query_part}"


def fetch_tripadvisor_from_listing_url(
    search_url: str,
    *,
    max_pages: int = 5,
    timeout_s: float = 25,
) -> Dict[str, Dict[str, Any]]:
    """Снимок TA с live-выдачи fly.pl (для backfill, пока CSV без ta_* колонок)."""
    import html as ihtml

    try:
        import requests
    except ImportError:
        return {}

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
    }
    by_name: Dict[str, Dict[str, Any]] = {}
    by_offer: Dict[str, Dict[str, Any]] = {}

    for page_number in range(1, max_pages + 1):
        url = _listing_page_url(search_url, page_number)
        try:
            resp = requests.get(url, headers=headers, timeout=timeout_s)
        except Exception:
            break
        if resp.status_code != 200 or "card-offer-search" not in resp.text:
            break
        cards = re.split(r'<div class="card-offer-search', resp.text)[1:]
        if not cards:
            break
        page_hits = 0
        for card in cards:
            ta = extract_tripadvisor_from_card_html(card)
            if not ta.get("ta_rating"):
                continue
            name = ""
            mh = re.search(r'<h2[^>]*property="schema:name"[^>]*>(.*?)</h2>', card, re.S)
            if mh:
                name = re.sub(r"<[^>]+>", "", mh.group(1)).strip()
            if not name:
                nm = re.search(r'property="schema:name"[^>]*content="([^"]+)"', card)
                if nm:
                    name = ihtml.unescape(nm.group(1)).strip()
            offer_url = ""
            for pat in (
                r'property="schema:url"[^>]*content="([^"]+)"',
                r'data-phref="([^"]+)"',
            ):
                om = re.search(pat, card)
                if om:
                    offer_url = ihtml.unescape(om.group(1)).strip()
                    break
            payload = {
                "ta_rating": f"{float(ta['ta_rating']):.1f}",
                "ta_review_count": int(ta["ta_review_count"] or 0),
                "ta_source": "tripadvisor",
            }
            if name:
                by_name[name] = payload
                page_hits += 1
            if offer_url:
                by_offer[offer_url] = payload
        if page_hits == 0:
            break
    return {"by_name": by_name, "by_offer": by_offer}


def extract_tripadvisor_from_card_html(card_html: str) -> Dict[str, Any]:
    """TripAdvisor rating + review count из server-side карточки fly.pl."""
    empty = {"ta_rating": None, "ta_review_count": None, "ta_source": ""}
    if not card_html:
        return empty
    rating_m = re.search(
        r'property="schema:ratingValue"[^>]*content="([^"]+)"',
        card_html,
    )
    count_m = re.search(
        r'property="schema:ratingCount"[^>]*content="([^"]+)"',
        card_html,
    )
    if not rating_m:
        img_m = re.search(
            r'tripadvisor\.com/img/cdsi/img2/ratings/traveler/([0-9.]+)-',
            card_html,
            re.I,
        )
        if img_m:
            try:
                empty["ta_rating"] = float(img_m.group(1))
            except (TypeError, ValueError):
                pass
    else:
        try:
            empty["ta_rating"] = float(rating_m.group(1).replace(",", "."))
        except (TypeError, ValueError):
            pass
    if count_m:
        try:
            empty["ta_review_count"] = int(float(count_m.group(1)))
        except (TypeError, ValueError):
            pass
    else:
        opin_m = re.search(r"(\d+)\s*opinii", card_html, re.I)
        if opin_m:
            try:
                empty["ta_review_count"] = int(opin_m.group(1))
            except (TypeError, ValueError):
                pass
    if empty["ta_rating"] is not None and empty["ta_rating"] > 0:
        empty["ta_source"] = "tripadvisor"
    return empty


def tripadvisor_review_weight(
    review_count: Any,
    *,
    full_weight_reviews: int = TA_DEAL_FULL_WEIGHT_REVIEWS,
) -> float:
    """0 для новых отелей; растёт с числом отзывов (в разумных пределах)."""
    try:
        reviews = int(review_count or 0)
    except (TypeError, ValueError):
        return 0.0
    if reviews <= 0:
        return 0.0
    return _clamp((reviews / max(1, full_weight_reviews)) ** 0.65, 0.0, 1.0)


def blend_tripadvisor_into_deal_score(
    deal_score: int,
    ta_rating: Any = None,
    ta_review_count: Any = None,
    *,
    neutral_rating: float = TA_DEAL_NEUTRAL_RATING,
    max_adjust: float = TA_DEAL_MAX_ADJUST,
    full_weight_reviews: int = TA_DEAL_FULL_WEIGHT_REVIEWS,
) -> Tuple[int, float]:
    """Смещает Deal Score по TA; без отзывов влияние ≈0."""
    weight = tripadvisor_review_weight(ta_review_count, full_weight_reviews=full_weight_reviews)
    if weight < 0.05:
        return int(deal_score), weight
    try:
        rating = float(ta_rating)
    except (TypeError, ValueError):
        return int(deal_score), 0.0
    if rating <= 0:
        return int(deal_score), 0.0
    delta = (rating - neutral_rating) * (max_adjust / 1.2)
    adjusted = int(round(_clamp(deal_score + delta * weight, 0, 100)))
    return adjusted, weight


def compute_hotel_deal_metrics(
    hist_df: pd.DataFrame,
    latest_price: float,
    time_col: str = "scraped_at",
    price_col: str = "price",
    ta_rating: Any = None,
    ta_review_count: Any = None,
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

    price_deal_score = deal_score
    deal_score, ta_weight = blend_tripadvisor_into_deal_score(
        deal_score,
        ta_rating,
        ta_review_count,
    )

    return {
        "deal_score": deal_score,
        "price_deal_score": price_deal_score,
        "ta_weight": round(ta_weight, 3),
        "avg_delta_pct": avg_delta_pct,
        "confidence": confidence,
    }
