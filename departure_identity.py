#!/usr/bin/env python3
"""Helpers for inferring a regional departure/flight cohort from fly.pl offers."""

from __future__ import annotations

import os
import re
from datetime import datetime
from typing import Any, Dict, Iterable, Optional
from urllib.parse import parse_qs, unquote, urlparse


DEPARTURE_FIELDS = [
    "filter_id",
    "country",
    "region",
    "resort",
    "departure_date",
    "return_date",
    "days",
    "nights",
    "origin_scope",
    "pax_profile",
    "departure_key",
]


def slug_label(value: str) -> str:
    return unquote(str(value or "")).strip().lower()


def parse_dates_text(value: str) -> tuple[str, str]:
    """Parse `DD.MM.YYYY - DD.MM.YYYY` into ISO dates."""
    text = str(value or "")
    match = re.search(
        r"(\d{2})\.(\d{2})\.(\d{4})\s*-\s*(\d{2})\.(\d{2})\.(\d{4})",
        text,
    )
    if not match:
        return "", ""
    d1, m1, y1, d2, m2, y2 = match.groups()
    return f"{y1}-{m1}-{d1}", f"{y2}-{m2}-{d2}"


def parse_duration(value: str) -> tuple[Optional[int], Optional[int]]:
    """Parse strings like `15 dni/14 nocy`."""
    text = str(value or "").lower()
    days = None
    nights = None
    m_days = re.search(r"(\d+)\s*dni", text)
    if m_days:
        days = int(m_days.group(1))
    m_nights = re.search(r"(\d+)\s*noc", text)
    if m_nights:
        nights = int(m_nights.group(1))
    if nights is None and days is not None and days > 0:
        nights = days - 1
    return days, nights


def parse_offer_path(offer_url: str) -> dict:
    """Extract country, region and resort from `/wycieczka/country,region,resort/...`."""
    parsed = urlparse(str(offer_url or ""))
    parts = [unquote(p) for p in parsed.path.split("/") if p]
    try:
        idx = parts.index("wycieczka")
    except ValueError:
        return {"country": "", "region": "", "resort": ""}
    if idx + 1 >= len(parts):
        return {"country": "", "region": "", "resort": ""}
    tokens = [slug_label(t) for t in parts[idx + 1].split(",")]
    return {
        "country": tokens[0] if len(tokens) > 0 else "",
        "region": tokens[1] if len(tokens) > 1 else "",
        "resort": tokens[2] if len(tokens) > 2 else "",
    }


def normalize_origin_scope(value: str) -> str:
    """Keep the search scope, not a fake concrete airport."""
    text = unquote(str(value or "")).replace("%20", " ")
    parts = [re.sub(r"\s+", " ", p).strip() for p in text.split(",")]
    parts = [p for p in parts if p]
    return ",".join(parts)


def pax_profile_from_url(url: str) -> str:
    qs = parse_qs(urlparse(str(url or "")).query)
    person = (qs.get("filter[person]") or qs.get("filter%5Bperson%5D") or [""])[0]
    child = (qs.get("filter[child]") or qs.get("filter%5Bchild%5D") or [""])[0]
    if person or child:
        return f"{person or 0}+{child or 0}"
    return ""


def filter_id_from_config(config: Dict[str, Any], config_file: str = "") -> str:
    data_dir = str(config.get("data_dir") or "")
    if data_dir:
        return os.path.basename(data_dir.rstrip("/"))
    if config_file:
        return os.path.splitext(os.path.basename(config_file))[0]
    return ""


def _query_first(qs: dict, key: str) -> str:
    value = qs.get(key)
    if not value:
        return ""
    return str(value[0] or "")


def build_departure_identity(
    offer: Dict[str, Any],
    config: Optional[Dict[str, Any]] = None,
    config_file: str = "",
) -> Dict[str, Any]:
    """Return regional departure identity fields for an offer.

    The key intentionally uses `region`, not `resort`: hotels in the same region
    typically share the same arrival airport/charter slot.
    """
    config = config or {}
    offer_url = str(offer.get("offer_url") or "")
    qs = parse_qs(urlparse(offer_url).query)
    path_bits = parse_offer_path(offer_url)

    departure_date = _query_first(qs, "departureDate")
    return_date = _query_first(qs, "returnDate")
    if not departure_date or not return_date:
        parsed_departure, parsed_return = parse_dates_text(str(offer.get("dates") or ""))
        departure_date = departure_date or parsed_departure
        return_date = return_date or parsed_return

    days, nights = parse_duration(str(offer.get("duration") or ""))
    origin_scope = normalize_origin_scope(
        _query_first(qs, "departureLocation")
        or str(offer.get("departure_airport") or "")
    )
    pax_profile = pax_profile_from_url(str(config.get("url") or offer.get("url") or ""))
    filter_id = filter_id_from_config(config, config_file)

    key_parts = [
        path_bits["country"],
        path_bits["region"],
        departure_date,
        return_date,
        str(nights or ""),
        origin_scope,
        pax_profile,
    ]
    departure_key = "|".join(key_parts)

    return {
        "filter_id": filter_id,
        "country": path_bits["country"],
        "region": path_bits["region"],
        "resort": path_bits["resort"],
        "departure_date": departure_date,
        "return_date": return_date,
        "days": days or "",
        "nights": nights or "",
        "origin_scope": origin_scope,
        "pax_profile": pax_profile,
        "departure_key": departure_key,
    }


def enrich_offer(
    offer: Dict[str, Any],
    config: Optional[Dict[str, Any]] = None,
    config_file: str = "",
) -> Dict[str, Any]:
    out = dict(offer)
    out.update(build_departure_identity(offer, config, config_file))
    return out


def enrich_offers(
    offers: Iterable[Dict[str, Any]],
    config: Optional[Dict[str, Any]] = None,
    config_file: str = "",
) -> list[Dict[str, Any]]:
    return [enrich_offer(o, config, config_file) for o in offers if o]


def days_to_departure(scraped_at: Any, departure_date: str) -> Optional[float]:
    try:
        ts = datetime.fromisoformat(str(scraped_at).replace("Z", "+00:00"))
        dep = datetime.fromisoformat(str(departure_date))
        return (dep.date() - ts.date()).days
    except Exception:
        return None
