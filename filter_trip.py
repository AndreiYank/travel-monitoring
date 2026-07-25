"""Fixed-date vacation filters: scrape window vs target departure anchor."""

from __future__ import annotations

import json
import os
import re
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from departure_identity import parse_dates_text, parse_duration


def is_fixed_trip_config(config: Optional[Dict[str, Any]]) -> bool:
    return str((config or {}).get("filter_mode") or "") == "fixed_trip"


def set_fly_duration_in_url(url: str, lo: int, hi: int) -> str:
    """Replace fly.pl filter[duration]=X:Y in a search URL."""
    text = str(url or "").strip()
    if not text:
        return text
    return re.sub(
        r"(filter(?:\[|%5B)duration(?:\]|%5D)=)(\d+):(\d+)",
        rf"\g<1>{int(lo)}:{int(hi)}",
        text,
        count=1,
    )


def trip_scrape_passes(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Build one scrape URL per duration bucket (fixed-trip multi-filter)."""
    base_url = str((config or {}).get("url") or "").strip()
    if not is_fixed_trip_config(config) or not base_url:
        return [{"url": base_url, "bucket_id": "", "label": ""}]
    buckets = parse_trip_duration_buckets(config)
    if not buckets:
        return [{"url": base_url, "bucket_id": "", "label": ""}]
    return [
        {
            "url": set_fly_duration_in_url(base_url, bucket["min"], bucket["max"]),
            "bucket_id": str(bucket["id"]),
            "label": str(bucket.get("label") or bucket["id"]),
        }
        for bucket in buckets
    ]


def has_multiple_trip_duration_buckets(config: Optional[Dict[str, Any]]) -> bool:
    return is_fixed_trip_config(config) and len(parse_trip_duration_buckets(config or {})) > 1


def parse_trip_duration_buckets(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw = config.get("trip_duration_buckets") or []
    buckets: List[Dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            lo = int(item.get("min"))
            hi = int(item.get("max"))
        except (TypeError, ValueError):
            continue
        bucket_id = str(item.get("id") or f"{lo}-{hi}").strip()
        label = str(item.get("label") or f"{lo}–{hi} дней").strip()
        buckets.append({"id": bucket_id, "min": lo, "max": hi, "label": label})
    return buckets


def offer_duration_days(offer: Dict[str, Any]) -> Optional[int]:
    days, _ = parse_duration(str(offer.get("duration") or ""))
    if days is not None:
        return int(days)
    text = str(offer.get("duration") or "")
    m = re.search(r"(\d+)\s*[-–]\s*(\d+)", text)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)\s*dni", text, re.I)
    if m:
        return int(m.group(1))
    return None


def trip_duration_bucket_id(
    days: Optional[int],
    buckets: List[Dict[str, Any]],
) -> str:
    if days is None or not buckets:
        return ""
    for bucket in buckets:
        if bucket["min"] <= days <= bucket["max"]:
            return str(bucket["id"])
    return ""


def _duration_bucket_is_missing(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return not text or text in {"nan", "none", "null"}


def infer_offer_duration_bucket(
    offer: Dict[str, Any],
    config: Dict[str, Any],
) -> str:
    existing = str(offer.get("duration_bucket") or "").strip()
    if not _duration_bucket_is_missing(existing):
        return existing
    buckets = parse_trip_duration_buckets(config)
    if not buckets:
        return ""
    return trip_duration_bucket_id(offer_duration_days(offer), buckets)


def trip_row_key(hotel_name: str, duration_bucket: str = "") -> str:
    hotel = str(hotel_name or "").strip()
    bucket = str(duration_bucket or "").strip()
    return f"{hotel}|{bucket}" if bucket else hotel


def _parse_iso_date(value: Any) -> Optional[date]:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def parse_trip_anchor_date(config: Dict[str, Any]) -> Optional[date]:
    return _parse_iso_date(config.get("trip_anchor_date"))


def is_filter_retired(config: Dict[str, Any], *, today: Optional[date] = None) -> bool:
    retire = _parse_iso_date(config.get("retire_after"))
    if retire is None:
        return False
    ref = today or date.today()
    return ref >= retire


def trip_window_end_date(config: Dict[str, Any]) -> Optional[date]:
    """Last calendar day when a departure can still match trip_anchor ± slip."""
    if not is_fixed_trip_config(config):
        return None
    anchor = parse_trip_anchor_date(config)
    if anchor is None:
        return None
    slip_days = int(config.get("trip_departure_slip_days", 7))
    from datetime import timedelta

    return anchor + timedelta(days=max(0, slip_days))


def is_trip_window_expired(config: Dict[str, Any], *, today: Optional[date] = None) -> bool:
    """True when the fixed-trip departure window is no longer bookable/useful."""
    end = trip_window_end_date(config)
    if end is None:
        return False
    ref = today or date.today()
    # On the last day of the window fly.pl usually has nothing left near the anchor.
    return ref >= end


def skip_monitor_reason(config: Dict[str, Any], *, today: Optional[date] = None) -> str:
    """Human-readable reason to skip scraping, or empty string."""
    if is_filter_retired(config, today=today):
        return "retire_after прошёл"
    if is_trip_window_expired(config, today=today):
        end = trip_window_end_date(config)
        return (
            f"окно вылета закрыто "
            f"(anchor {config.get('trip_anchor_date')} ± "
            f"{config.get('trip_departure_slip_days', 7)} дн., до {end})"
        )
    return ""


def offer_departure_iso(offer: Dict[str, Any]) -> str:
    qs = parse_qs(urlparse(str(offer.get("offer_url") or "")).query)
    dep = (qs.get("departureDate") or [""])[0]
    if dep:
        parsed = _parse_iso_date(dep)
        return parsed.isoformat() if parsed else str(dep).strip()
    dep_text, _ = parse_dates_text(str(offer.get("dates") or ""))
    return dep_text


def select_trip_offers(
    offers: List[Dict[str, Any]],
    config: Dict[str, Any],
    *,
    force_bucket_id: str = "",
) -> List[Dict[str, Any]]:
    """Keep best offer per hotel (and per duration bucket when configured).

    When force_bucket_id is set (one fly.pl scrape pass = one virtual filter),
    all kept rows are tagged with that bucket — independent of other passes.
    """
    anchor = parse_trip_anchor_date(config)
    buckets = parse_trip_duration_buckets(config)
    if anchor is None:
        return offers
    slip_days = int(config.get("trip_departure_slip_days", 7))
    forced_bucket = str(force_bucket_id or "").strip()

    candidates: List[tuple[int, float, str, Dict[str, Any]]] = []
    for offer in offers:
        if not offer:
            continue
        dep_iso = offer_departure_iso(offer)
        dep_dt = _parse_iso_date(dep_iso)
        if dep_dt is None:
            continue
        delta_days = abs((dep_dt - anchor).days)
        if delta_days > slip_days:
            continue
        try:
            price = float(offer.get("price") or 0)
        except (TypeError, ValueError):
            continue
        name = str(offer.get("hotel_name") or "").strip()
        if not name:
            continue
        if forced_bucket:
            bucket_id = forced_bucket
        else:
            bucket_id = trip_duration_bucket_id(offer_duration_days(offer), buckets)
            if buckets and not bucket_id:
                continue
        candidates.append((delta_days, price, bucket_id, dict(offer)))

    best: Dict[Tuple[str, str], tuple[int, float, Dict[str, Any]]] = {}
    for delta_days, price, bucket_id, offer in candidates:
        name = str(offer.get("hotel_name") or "").strip()
        key = (name, bucket_id)
        prev = best.get(key)
        if prev is None or (delta_days, price) < (prev[0], prev[1]):
            if bucket_id:
                offer["duration_bucket"] = bucket_id
            best[key] = (delta_days, price, offer)

    return [row[2] for row in best.values()]


def load_config_json(path: str) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def should_skip_monitor_config(config_path: str, *, today: Optional[date] = None) -> bool:
    if not os.path.isfile(config_path):
        return False
    try:
        cfg = load_config_json(config_path)
    except (OSError, json.JSONDecodeError):
        return False
    return bool(skip_monitor_reason(cfg, today=today))
