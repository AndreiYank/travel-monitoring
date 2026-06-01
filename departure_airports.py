#!/usr/bin/env python3
"""Arrival airport hubs: several resorts share one charter arrival airport."""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Set

import pandas as pd

# Turkey: fly.pl regions -> arrival airport used by charter packages.
# Sources: AYT serves Antalya coast (Side, Kemer, Alanya, Belek);
# DLM -> Marmaris/Fethiye area; BJV -> Bodrum; ADB -> Izmir/Kusadasi/Cesme.
TURKEY_ARRIVAL_HUBS: Dict[str, Dict[str, Any]] = {
    "antalya": {
        "label": "Анталия (AYT)",
        "iata": "AYT",
        "regions": {"antalya", "side", "kemer", "alanya", "belek"},
    },
    "dalaman": {
        "label": "Даламан (DLM)",
        "iata": "DLM",
        "regions": {"marmaris", "fethiye", "dalaman", "oludeniz", "gocek"},
    },
    "bodrum": {
        "label": "Бодрум (BJV)",
        "iata": "BJV",
        "regions": {"bodrum"},
    },
    "izmir": {
        "label": "Измир (ADB)",
        "iata": "ADB",
        "regions": {"izmir", "kusadasi", "cesme"},
    },
    "istanbul": {
        "label": "Стамбул (IST/SAW)",
        "iata": "IST",
        "regions": {"istambul", "istanbul"},
    },
}

_REGION_TO_TURKEY_HUB: Dict[str, str] = {}
for _hub_id, _hub in TURKEY_ARRIVAL_HUBS.items():
    for _region in _hub["regions"]:
        _REGION_TO_TURKEY_HUB[_region] = _hub_id


def _norm(value: Any) -> str:
    return str(value or "").strip().lower().replace("_", "-")


def turkey_hub_id_for_region(region: Any) -> Optional[str]:
    return _REGION_TO_TURKEY_HUB.get(_norm(region))


def turkey_hub_label(hub_id: str) -> str:
    hub = TURKEY_ARRIVAL_HUBS.get(_norm(hub_id), {})
    return str(hub.get("label") or hub_id.replace("-", " ").title())


def turkey_hub_regions(hub_id: str) -> Set[str]:
    hub = TURKEY_ARRIVAL_HUBS.get(_norm(hub_id), {})
    return set(hub.get("regions") or [])


def should_group_by_arrival_airport(country: Any, filter_id: str = "") -> bool:
    country_norm = _norm(country)
    if country_norm in {"turcja", "turkey", "turkiye"}:
        return True
    return filter_id.startswith("turkey_")


def hub_id_for_offer(country: Any, region: Any) -> Optional[str]:
    if not should_group_by_arrival_airport(country):
        return None
    return turkey_hub_id_for_region(region)


def build_hub_departure_key(
    country: str,
    hub_id: str,
    departure_date: str,
    return_date: str,
    nights: Any,
    origin_scope: str,
    pax_profile: str,
) -> str:
    return "|".join(
        [
            _norm(country) or "country",
            f"hub:{_norm(hub_id)}",
            str(departure_date or ""),
            str(return_date or ""),
            str(nights or ""),
            str(origin_scope or ""),
            str(pax_profile or ""),
        ]
    )


def parse_hub_departure_key(key: str) -> Optional[Dict[str, str]]:
    parts = str(key or "").split("|")
    if len(parts) < 7 or not parts[1].startswith("hub:"):
        return None
    return {
        "country": parts[0],
        "hub_id": parts[1].split(":", 1)[1],
        "departure_date": parts[2],
        "return_date": parts[3],
        "nights": parts[4],
        "origin_scope": parts[5],
        "pax_profile": parts[6],
    }


def hub_regions_subtitle(regions: Iterable[str]) -> str:
    labels = sorted({str(r or "").replace("-", " ").title() for r in regions if str(r or "").strip()})
    if not labels:
        return ""
    if len(labels) <= 3:
        return " · ".join(labels)
    return " · ".join(labels[:3]) + f" +{len(labels) - 3}"


def aggregate_cohorts_by_arrival_airport(cohorts: pd.DataFrame) -> pd.DataFrame:
    """Collapse regional Turkey cohort snapshots into airport-hub rows."""
    if cohorts.empty:
        return cohorts

    work = cohorts.copy()
    work["_country_norm"] = work["country"].map(_norm)
    work["_hub_id"] = work.apply(
        lambda row: hub_id_for_offer(row.get("country"), row.get("region")),
        axis=1,
    )
    if work["_hub_id"].fillna("").astype(str).eq("").all():
        return cohorts

    grouped_rows: List[Dict[str, Any]] = []
    group_cols = [
        "run_started_at",
        "_hub_id",
        "departure_date",
        "return_date",
        "nights",
        "origin_scope",
        "pax_profile",
        "filter_id",
        "_country_norm",
    ]
    for keys, grp in work.dropna(subset=["_hub_id"]).groupby(group_cols, sort=False):
        if grp.empty:
            continue
        hub_id = keys[1]
        country = keys[8] or str(grp.iloc[0].get("country") or "")
        regions = sorted({str(r) for r in grp["region"].fillna("").astype(str) if r})
        hotel_count = int(grp["hotel_count"].fillna(0).sum())
        if hotel_count <= 0:
            continue

        weights = grp["hotel_count"].fillna(0).astype(float)
        total_weight = float(weights.sum()) or 1.0

        def _wavg(col: str, default: float = 0.0) -> float:
            if col not in grp.columns:
                return default
            series = pd.to_numeric(grp[col], errors="coerce").fillna(default)
            return float((series * weights).sum() / total_weight)

        row = {
            "run_started_at": keys[0],
            "departure_key": build_hub_departure_key(
                country,
                hub_id,
                keys[2],
                keys[3],
                keys[4],
                keys[5],
                keys[6],
            ),
            "filter_id": keys[7],
            "country": country,
            "region": hub_id,
            "hub_id": hub_id,
            "hub_label": turkey_hub_label(hub_id),
            "hub_regions": ",".join(regions),
            "departure_date": keys[2],
            "return_date": keys[3],
            "nights": keys[4],
            "origin_scope": keys[5],
            "pax_profile": keys[6],
            "days_to_departure": grp["days_to_departure"].dropna().iloc[0]
            if grp["days_to_departure"].notna().any()
            else "",
            "offer_count": int(grp["offer_count"].fillna(0).sum()) if "offer_count" in grp else hotel_count,
            "hotel_count": hotel_count,
            "min_price": float(pd.to_numeric(grp["min_price"], errors="coerce").min()),
            "p10_price": float(pd.to_numeric(grp["p10_price"], errors="coerce").min()),
            "p25_price": float(pd.to_numeric(grp["p25_price"], errors="coerce").min()),
            "median_price": round(_wavg("median_price"), 2),
            "max_price": float(pd.to_numeric(grp["max_price"], errors="coerce").max()),
            "below_10000_count": int(grp["below_10000_count"].fillna(0).sum())
            if "below_10000_count" in grp
            else 0,
            "below_8000_count": int(grp["below_8000_count"].fillna(0).sum())
            if "below_8000_count" in grp
            else 0,
            "hot_score": int(pd.to_numeric(grp["hot_score"], errors="coerce").fillna(0).max()),
            "min_change_pct": float(pd.to_numeric(grp["min_change_pct"], errors="coerce").min())
            if "min_change_pct" in grp
            else 0.0,
            "p10_change_pct": float(pd.to_numeric(grp["p10_change_pct"], errors="coerce").min())
            if "p10_change_pct" in grp
            else 0.0,
            "median_change_pct": float(pd.to_numeric(grp["median_change_pct"], errors="coerce").min())
            if "median_change_pct" in grp
            else 0.0,
            "prev_min_price": float(pd.to_numeric(grp["prev_min_price"], errors="coerce").min())
            if "prev_min_price" in grp
            else 0.0,
            "prev_p10_price": float(pd.to_numeric(grp["prev_p10_price"], errors="coerce").min())
            if "prev_p10_price" in grp
            else 0.0,
            "prev_median_price": round(_wavg("prev_median_price"), 2),
            "hotel_count_delta": int(grp["hotel_count_delta"].fillna(0).sum())
            if "hotel_count_delta" in grp
            else 0,
        }
        grouped_rows.append(row)

    if not grouped_rows:
        return cohorts
    return pd.DataFrame(grouped_rows)
