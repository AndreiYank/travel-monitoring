"""Visual filter summary for dashboard hero (icons, figures, animated cards)."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

DATA_DIR_CONFIG_FILES: Dict[str, str] = {
    "filter_7_10_days": "config_ci_filter_7_10.json",
    "filter_13_16_days": "config_ci_filter_13_16.json",
    "filter_turkey_7_10_days": "config_ci_filter_turkey_7_10.json",
    "filter_turkey_9_11_days": "config_ci_filter_turkey_9_11.json",
    "filter_turkey_13_16_days": "config_ci_filter_turkey_13_16.json",
    "filter_turkey_vacation_jul18_2026": "config_ci_filter_turkey_vacation_jul18.json",
    "filter_egypt_autumn_2026_7_10_days": "config_ci_filter_egypt_autumn_7_10.json",
    "filter_egypt_autumn_2026_13_16_days": "config_ci_filter_egypt_autumn_13_16.json",
    "filter_egypt_ny_dec24_2026_7_10_days": "config_ci_filter_egypt_ny_dec24_7_10.json",
    "filter_egypt_ny_dec28_2026_7_10_days": "config_ci_filter_egypt_ny_dec28_7_10.json",
    "filter_greece_7_10_days": "config_ci_filter_greece_7_10.json",
    "filter_greece_13_16_days": "config_ci_filter_greece_13_16.json",
}

COUNTRY_FROM_URL_FRAGMENT = {
    "egipt": "Египет",
    "turcja": "Турция",
    "grecja": "Греция",
    "hiszpania": "Испания",
    "cypr": "Кипр",
}

COUNTRY_FLAGS = {
    "Египет": "🇪🇬",
    "Турция": "🇹🇷",
    "Греция": "🇬🇷",
    "Испания": "🇪🇸",
    "Кипр": "🇨🇾",
}

CATERING_LABELS = {
    "": "Любое",
    "0": "Любое",
    "1": "С питанием",
}

TRANSPORT_LABELS = {
    "F": "Перелёт + отель",
    "H": "Только отель",
    "": "Любой",
}

def _query_keys(name: str) -> List[str]:
    enc = name.replace("[", "%5B").replace("]", "%5D")
    return [f"filter[{name}]", enc, name]


def make_fly_search_url_dynamic(url: str, *, window_days: int = 30) -> str:
    """Подставляет whenFrom=сегодня и whenTo=сегодня+N дней (как в travel_monitor)."""
    if not url:
        return url
    now = datetime.now()
    from_date_str = now.strftime("%d-%m-%Y")
    to_date_str = (now + timedelta(days=window_days)).strftime("%d-%m-%Y")
    url = re.sub(
        r"(filter(?:\[|%5B)whenFrom(?:\]|%5D)=)([^&]*)",
        rf"\g<1>{from_date_str}",
        url,
    )
    url = re.sub(
        r"(filter(?:\[|%5B)whenTo(?:\]|%5D)=)([^&]*)",
        rf"\g<1>{to_date_str}",
        url,
    )
    return url


def should_use_dynamic_search_dates(config: Optional[Dict[str, Any]]) -> bool:
    cfg = config or {}
    if cfg.get("dynamic_search_dates") is False:
        return False
    if str(cfg.get("filter_mode") or "") == "fixed_trip":
        return False
    return True


def resolve_config_url(config: Optional[Dict[str, Any]]) -> str:
    url = str((config or {}).get("url") or "")
    if not url:
        return url
    if should_use_dynamic_search_dates(config):
        return make_fly_search_url_dynamic(url)
    return url


def fly_query_param(url: str, name: str) -> str:
    qs = parse_qs(urlparse(str(url or "")).query, keep_blank_values=True)
    for key in _query_keys(name):
        vals = qs.get(key)
        if vals and str(vals[0]).strip() != "":
            return unquote(str(vals[0]).strip())
    return ""


def child_age_param(url: str) -> str:
    qs = parse_qs(urlparse(str(url or "")).query, keep_blank_values=True)
    for key, vals in qs.items():
        if "childAge" in key and vals:
            return str(vals[0]).strip()
    return ""


def resolve_config_path(data_file: str, config_file: str | None = None) -> Optional[str]:
    if config_file and os.path.isfile(config_file):
        return config_file
    match = re.search(r"data/filters/([^/]+)/", str(data_file or ""))
    if not match:
        return None
    rel = DATA_DIR_CONFIG_FILES.get(match.group(1))
    if rel and os.path.isfile(rel):
        return rel
    return None


def load_filter_config(
    data_file: str = "",
    config_file: str | None = None,
) -> Dict[str, Any]:
    path = resolve_config_path(data_file, config_file)
    if not path:
        return {}
    with open(path, encoding="utf-8") as fh:
        config = json.load(fh)
    if config.get("url"):
        config = dict(config)
        config["url"] = resolve_config_url(config)
    return config


def _country_label(config: Dict[str, Any], url: str) -> str:
    for fragment, label in COUNTRY_FROM_URL_FRAGMENT.items():
        for item in config.get("required_offer_url_contains") or []:
            if fragment in str(item).lower():
                return label
    path = urlparse(url).path.lower()
    for fragment, label in COUNTRY_FROM_URL_FRAGMENT.items():
        if fragment in path:
            return label
    dest = fly_query_param(url, "dest")
    if dest.startswith("11"):
        return "Египет"
    if dest.startswith("14") or dest.startswith("39"):
        if "grecja" in path or dest.startswith("14"):
            return "Греция"
        return "Турция"
    return "—"


def _duration_label(url: str) -> str:
    raw = fly_query_param(url, "duration")
    if not raw or ":" not in raw:
        return raw or "—"
    lo, hi = raw.split(":", 1)
    try:
        lo_i, hi_i = int(lo), int(hi)
        if lo_i == hi_i:
            return f"{lo_i}"
        return f"{lo_i}–{hi_i}"
    except ValueError:
        return raw.replace(":", "–")


def _airports_parts(url: str) -> List[str]:
    raw = fly_query_param(url, "from")
    if not raw.strip():
        return []
    return [p.strip() for p in unquote(raw).split(",") if p.strip()]


def _airports_label(url: str) -> str:
    parts = _airports_parts(url)
    if not parts:
        return "Любой аэропорт"
    if len(parts) <= 2:
        return ", ".join(parts)
    return f"{parts[0]} +{len(parts) - 1}"


def _party_counts(url: str) -> Tuple[int, int, Optional[int]]:
    try:
        adults = int(fly_query_param(url, "person") or 2)
        children = int(fly_query_param(url, "child") or 0)
    except ValueError:
        return 2, 0, None
    child_years = None
    digits = re.sub(r"\D", "", child_age_param(url))
    if len(digits) >= 8:
        try:
            born = datetime(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))
            child_years = max(0, (datetime.now().date() - born.date()).days // 365)
        except ValueError:
            pass
    return adults, children, child_years


def _stars_min(url: str) -> int:
    raw = fly_query_param(url, "addCategory")
    if not raw:
        return 0
    try:
        val = int(raw)
        if val >= 10 and val % 10 == 0:
            return val // 10
    except ValueError:
        pass
    return 0


def _format_pln(amount: float, compact: bool = False) -> str:
    if compact and amount >= 1000:
        val = amount / 1000
        if val == int(val):
            return f"{int(val)}k"
        return f"{val:.1f}k".replace(".0k", "k")
    return f"{amount:,.0f}".replace(",", " ")


def _price_bounds(config: Dict[str, Any], url: str) -> Tuple[Optional[float], Optional[float]]:
    lo = fly_query_param(url, "PriceFrom") or config.get("min_price_threshold")
    hi = fly_query_param(url, "PriceTo") or config.get("max_price_threshold")
    if lo is None and hi is None:
        pr = fly_query_param(url, "price")
        if pr and ":" in pr:
            lo, hi = pr.split(":", 1)
    try:
        lo_f = float(lo) if lo not in (None, "") else None
        hi_f = float(hi) if hi not in (None, "") else None
    except (TypeError, ValueError):
        return None, None
    return lo_f, hi_f


def build_filter_param_rows(
    config: Dict[str, Any] | None = None,
    *,
    display_price_ceiling: float | None = None,
    history_price_ceiling: float | None = None,
    include_search_dates: bool = False,
) -> List[Tuple[str, str]]:
    """Plain (label, value) pairs — for tests and accessibility fallbacks."""
    config = config or {}
    url = str(config.get("url") or "")
    if not url:
        return []

    adults, children, child_years = _party_counts(url)
    party = f"{adults} взр."
    if children:
        party += f" + {children} реб."
        if child_years is not None:
            party += f" (~{child_years} лет)"

    rows: List[Tuple[str, str]] = [
        ("Направление", _country_label(config, url)),
        ("Длительность", f"{_duration_label(url)} дн."),
        ("Вылет", _airports_label(url)),
        ("Состав", party),
        ("Класс", f"от {_stars_min(url)}★" if _stars_min(url) else "—"),
        ("Питание", CATERING_LABELS.get(fly_query_param(url, "addCatering"), "—")),
        ("Пакет", TRANSPORT_LABELS.get(fly_query_param(url, "addTransport").upper(), "—")),
    ]
    lo, hi = _price_bounds(config, url)
    if lo is not None and hi is not None:
        rows.append(("Сбор", f"{_format_pln(lo)}–{_format_pln(hi)} PLN"))
    show = display_price_ceiling if display_price_ceiling is not None else config.get("display_price_ceiling")
    if show not in (None, ""):
        try:
            rows.append(("Показ", f"≤ {_format_pln(float(show))} PLN"))
        except (TypeError, ValueError):
            pass
    if include_search_dates:
        wf, wt = fly_query_param(url, "whenFrom"), fly_query_param(url, "whenTo")
        if wf or wt:
            rows.append(("Окно", f"{wf or '…'} — {wt or '…'}"))
    return rows


def _airport_chip_code(name: str) -> str:
    text = name.lower()
    if "modlin" in text:
        return "WMI"
    if "radom" in text:
        return "RDO"
    if "warszawa" in text or "warsaw" in text:
        return "WAW"
    if "krak" in text:
        return "KRK"
    if "gdansk" in text or "gdańsk" in text:
        return "GDN"
    words = name.split()
    return (words[0][:3] if words else name[:3]).upper()


def _airport_compact_label(parts: List[str]) -> str:
    if not parts:
        return "любой аэропорт"
    codes = [_airport_chip_code(p) for p in parts[:4]]
    tail = f" +{len(parts) - 4}" if len(parts) > 4 else ""
    return " • ".join(codes) + tail


def _party_compact_label(adults: int, children: int, child_years: Optional[int]) -> str:
    text = f"{adults} взр."
    if children:
        text += f" + {children} реб."
        if child_years is not None:
            text += f" ~{child_years}л"
    return text


def _hotel_star_value(stars: int) -> str:
    if stars >= 5:
        return f"{stars}*+"
    if stars > 0:
        return f"от {stars}*"
    return "любой"


def _budget_range_label(
    lo: Optional[float],
    hi: Optional[float],
    show_ceiling: Optional[float],
    history_ceiling: Optional[float],
) -> Tuple[str, str]:
    if lo is not None and hi is not None:
        main = f"{_format_pln(lo, True)}–{_format_pln(hi, True)} PLN"
    else:
        main = "—"
    hints: List[str] = []
    if show_ceiling is not None:
        hints.append(f"показ ≤{_format_pln(show_ceiling, True)}")
    if history_ceiling is not None and history_ceiling != show_ceiling:
        hints.append(f"ист. ≤{_format_pln(history_ceiling, True)}")
    return main, " · ".join(hints)


_SVG = {
    "plane": (
        '<svg class="fb-svg" viewBox="0 0 24 24" aria-hidden="true">'
        '<path d="M17.8 19.2 16 11l3.5-3.5C21 6 21.4 4 20 4c-1.4 0-2.8 1-3.3 2.5L13 10 4.8 7.7c-.9-.3-1.7.5-1.4 1.4L6 14l-2 2 1 1 2-2 4.2 2.7c.9.3 1.7-.5 1.4-1.4L13 12l3.3 5.5c.5 1.5 2.1 2.5 3.6 2.5 1.6 0 2.4-2 .5-3.6L16 11l1.8 8.2z" '
        'fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/>'
        "</svg>"
    ),
    "calendar": (
        '<svg class="fb-svg" viewBox="0 0 24 24" aria-hidden="true">'
        '<path d="M7 2v2M17 2v2M4 8h16M5 4h14a2 2 0 012 2v13a2 2 0 01-2 2H5a2 2 0 01-2-2V6a2 2 0 012-2z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>'
        '<path d="M8 12h.01M12 12h.01M16 12h.01" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"/>'
        "</svg>"
    ),
    "users": (
        '<svg class="fb-svg" viewBox="0 0 24 24" aria-hidden="true">'
        '<path d="M16 21v-2a4 4 0 00-4-4H6a4 4 0 00-4 4v2M9 11a4 4 0 100-8 4 4 0 000 8zM22 21v-2a4 4 0 00-3-3.87M16 3.13a4 4 0 010 7.75" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>'
        "</svg>"
    ),
    "hotel": (
        '<svg class="fb-svg" viewBox="0 0 24 24" aria-hidden="true">'
        '<path d="M3 21V8l9-5 9 5v13M9 21v-6h6v6" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/>'
        "</svg>"
    ),
    "package": (
        '<svg class="fb-svg" viewBox="0 0 24 24" aria-hidden="true">'
        '<path d="M12 3l8 4v10l-8 4-8-4V7l8-4zM12 7v14M4 7l8 4 8-4" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/>'
        "</svg>"
    ),
    "wallet": (
        '<svg class="fb-svg" viewBox="0 0 24 24" aria-hidden="true">'
        '<path d="M20 7H4a2 2 0 00-2 2v10a2 2 0 002 2h16a2 2 0 002-2V9a2 2 0 00-2-2z" fill="none" stroke="currentColor" stroke-width="1.8"/>'
        '<path d="M17 14h.01" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"/>'
        "</svg>"
    ),
}


def _fb_badge(svg_key: str, tone: str, *, emoji: str = "") -> str:
    inner = f'<span class="fb-flag-emoji">{emoji}</span>' if emoji else _SVG.get(svg_key, "")
    return f'<div class="fb-badge fb-badge--{tone}">{inner}</div>'


def _fb_slot(
    esc: Callable[[Any], str],
    kicker: str,
    value: str,
    svg_key: str,
    tone: str,
    *,
    sub: str = "",
    value_class: str = "",
    emoji: str = "",
    title: str = "",
) -> str:
    sub_html = f'<span class="fb-sub">{esc(sub)}</span>' if sub else ""
    val_cls = f"fb-value {value_class}".strip()
    attr = f' title="{esc(title)}"' if title else ""
    return (
        f'<div class="fb-slot"{attr}>'
        f'{_fb_badge(svg_key, tone, emoji=emoji)}'
        '<div class="fb-slot-body">'
        f'<span class="fb-kicker">{esc(kicker)}</span>'
        f'<span class="{val_cls}">{value}</span>'
        f"{sub_html}"
        "</div></div>"
    )


def _fb_vdiv() -> str:
    return '<span class="fb-vdiv" aria-hidden="true"></span>'


def _budget_inline_html(
    esc: Callable[[Any], str],
    lo: Optional[float],
    hi: Optional[float],
    show_ceiling: Optional[float],
    history_ceiling: Optional[float],
) -> str:
    chips: List[str] = []
    if lo is not None and hi is not None:
        chips.append(
            '<span class="fb-budget-chip fb-budget-chip--collect">'
            f'<i class="fb-dot"></i> СБОР {esc(_format_pln(lo, True))}–{esc(_format_pln(hi, True))}'
            "</span>"
        )
    if show_ceiling is not None:
        chips.append(
            '<span class="fb-budget-chip fb-budget-chip--show">'
            f'<i class="fb-dot"></i> ПОКАЗ ≤{esc(_format_pln(show_ceiling, True))}'
            "</span>"
        )
    if history_ceiling is not None and history_ceiling != show_ceiling:
        chips.append(
            '<span class="fb-budget-chip fb-budget-chip--hist">'
            f'<i class="fb-dot"></i> ИСТОРИЯ ≤{esc(_format_pln(history_ceiling, True))}'
            "</span>"
        )
    if not chips:
        return ""
    return f'<span class="fb-budget-group">{"".join(chips)}</span>'


def _filter_bar_segment(inner_html: str, *, css_class: str = "", title: str = "") -> str:
    cls = f"filter-bar__seg {css_class}".strip()
    attr = f' title="{title}"' if title else ""
    return f'<div class="{cls}"{attr}>{inner_html}</div>'


def _filter_bar_sep() -> str:
    return '<span class="filter-bar__sep" aria-hidden="true"></span>'


def _party_schematic_html(adults: int, children: int, child_years: Optional[int]) -> str:
    slots: List[str] = []
    for i in range(max(0, adults)):
        slots.append(
            f'<span class="ts-person ts-person--adult" style="--p-i:{i}" '
            f'title="Взрослый"><span class="ts-person-head"></span>'
            f'<span class="ts-person-body"></span></span>'
        )
    for i in range(max(0, children)):
        slots.append(
            f'<span class="ts-person ts-person--child" style="--p-i:{adults + i}" '
            f'title="Ребёнок"><span class="ts-person-head"></span>'
            f'<span class="ts-person-body"></span></span>'
        )
    age_line = ""
    if children and child_years is not None:
        age_line = f'<div class="ts-party-caption">ребёнок ~{child_years} лет</div>'
    elif children:
        age_line = '<div class="ts-party-caption">с ребёнком</div>'
    count_line = f"{adults} взр." + (f" + {children} реб." if children else "")
    return (
        f'<div class="ts-party">'
        f'<div class="ts-party-figs">{"".join(slots)}</div>'
        f'<div class="ts-party-text"><strong>{count_line}</strong>{age_line}</div>'
        f"</div>"
    )


def _stars_schematic_html(min_stars: int) -> str:
    if min_stars <= 0:
        return (
            '<div class="ts-meta-row">'
            '<span class="ts-meta-ico">🏨</span>'
            '<span class="ts-meta-txt">класс: любой</span></div>'
        )
    stars = "".join(
        f'<span class="ts-star{" ts-star--on" if i <= min_stars else ""}">★</span>'
        for i in range(1, 6)
    )
    return (
        f'<div class="ts-meta-row" title="от {min_stars} звёзд">'
        f'<span class="ts-meta-ico">🏨</span>'
        f'<span class="ts-stars">{stars}</span>'
        f'<span class="ts-meta-txt">от {min_stars}★</span></div>'
    )


def _budget_row(
    esc: Callable[[Any], str],
    label: str,
    value: str,
    width_pct: float,
    css_class: str,
) -> str:
    w = min(100.0, max(8.0, width_pct))
    return (
        f'<div class="ts-brow {css_class}">'
        f'<span class="ts-brow-lbl">{esc(label)}</span>'
        f'<div class="ts-brow-track"><span class="ts-brow-fill" style="width:{w:.0f}%"></span></div>'
        f'<span class="ts-brow-val">{esc(value)}</span></div>'
    )


def _budget_schematic_html(
    esc: Callable[[Any], str],
    lo: Optional[float],
    hi: Optional[float],
    show_ceiling: Optional[float],
    history_ceiling: Optional[float],
) -> str:
    max_v = float(hi or history_ceiling or show_ceiling or 20000)
    if max_v <= 0:
        max_v = 20000

    def pct(v: Optional[float]) -> float:
        if v is None:
            return 0.0
        return min(100.0, max(8.0, 100.0 * float(v) / max_v))

    rows: List[str] = []
    if lo is not None and hi is not None:
        rows.append(
            _budget_row(
                esc,
                "Сбор",
                f"{_format_pln(lo, True)}–{_format_pln(hi, True)}",
                pct(hi),
                "ts-brow--collect",
            )
        )
    if show_ceiling is not None:
        rows.append(
            _budget_row(
                esc,
                "Показ",
                f"≤ {_format_pln(show_ceiling, True)}",
                pct(show_ceiling),
                "ts-brow--show",
            )
        )
    if history_ceiling is not None and history_ceiling != show_ceiling:
        rows.append(
            _budget_row(
                esc,
                "История",
                f"≤ {_format_pln(history_ceiling, True)}",
                pct(history_ceiling),
                "ts-brow--hist",
            )
        )

    return (
        '<div class="ts-budget">'
        '<div class="ts-budget-hd"><span class="ts-budget-ico">💰</span>'
        '<span>Бюджет</span><span class="ts-budget-cur">PLN</span></div>'
        f'<div class="ts-budget-rows">{"".join(rows)}</div>'
        "</div>"
    )


def render_filter_params_html(
    config: Dict[str, Any] | None = None,
    *,
    display_price_ceiling: float | None = None,
    history_price_ceiling: float | None = None,
    include_search_dates: bool = False,
    escape: Any = None,
) -> str:
    import html as html_mod

    esc = escape or html_mod.escape
    config = config or {}
    url = str(config.get("url") or "")
    if not url:
        return ""

    country = _country_label(config, url)
    flag = COUNTRY_FLAGS.get(country, "🌍")
    duration = _duration_label(url)
    airports = _airports_parts(url)
    adults, children, child_years = _party_counts(url)
    stars = _stars_min(url)
    catering = CATERING_LABELS.get(fly_query_param(url, "addCatering"), "Любое")
    package = TRANSPORT_LABELS.get(fly_query_param(url, "addTransport").upper(), "—")
    lo, hi = _price_bounds(config, url)
    show_raw = display_price_ceiling if display_price_ceiling is not None else config.get("display_price_ceiling")
    hist_raw = history_price_ceiling if history_price_ceiling is not None else config.get("max_price_threshold")
    try:
        show_f = float(show_raw) if show_raw not in (None, "") else None
    except (TypeError, ValueError):
        show_f = None
    try:
        hist_f = float(hist_raw) if hist_raw not in (None, "") else None
    except (TypeError, ValueError):
        hist_f = None

    airport_title = ", ".join(airports) if airports else "Любой аэропорт в Польше"
    airports_compact = _airport_compact_label(airports)
    party_value = f"{adults} взр." + (f" + {children} реб." if children else "")
    party_sub = ""
    if children and child_years is not None:
        party_sub = f"ребёнок ~{child_years} лет"
    elif children:
        party_sub = "с ребёнком"
    hotel_sub = ""
    if catering not in ("Любое", "—"):
        hotel_sub = catering.replace("С питанием", "с питанием")
    budget_main, budget_hint = _budget_range_label(lo, hi, show_f, hist_f if hist_f != show_f else None)
    budget_title = budget_main
    if budget_hint:
        budget_title = f"{budget_main} ({budget_hint})"

    duration_kicker = "Длительность"
    duration_value = f"{esc(duration)} дней"
    duration_sub = ""
    duration_title = ""
    if str(config.get("filter_mode") or "") == "fixed_trip":
        duration_kicker = "Отпуск"
        wf = fly_query_param(url, "whenFrom")
        wt = fly_query_param(url, "whenTo")
        anchor_raw = str(config.get("trip_anchor_date") or "")
        anchor_fmt = anchor_raw
        anchor_dt = None
        for fmt in ("%Y-%m-%d", "%d-%m-%Y"):
            try:
                anchor_dt = datetime.strptime(anchor_raw, fmt)
                anchor_fmt = anchor_dt.strftime("%d.%m.%Y")
                break
            except ValueError:
                continue
        duration_value = f"с {esc(anchor_fmt)}" if anchor_fmt else duration_value
        slip = int(config.get("trip_departure_slip_days", 7))
        duration_sub = f"вылет ±{slip} дн. от {anchor_fmt}" if anchor_fmt else ""
        buckets = config.get("trip_duration_buckets") or []
        bucket_labels = [
            str(b.get("label") or "").strip()
            for b in buckets
            if isinstance(b, dict) and str(b.get("label") or "").strip()
        ]
        if bucket_labels:
            duration_sub = (
                f"{duration_sub} • {' / '.join(bucket_labels)} — переключатель на странице"
                if duration_sub
                else f"{' / '.join(bucket_labels)} — два фильтра на одной странице"
            )
        if wf or wt:
            duration_title = f"Поиск на fly.pl: {wf} — {wt}"

    dep_slot = _fb_slot(
        esc, "Вылет", esc(airports_compact), "plane", "blue", title=airport_title
    )
    dest_slot = _fb_slot(
        esc, "Куда", esc(country), "", "flag", emoji=flag
    )
    route_group = (
        '<div class="fb-route-group">'
        f"{dep_slot}"
        '<span class="fb-route-arrow" aria-hidden="true">→</span>'
        f"{dest_slot}"
        "</div>"
    )

    parts = [
        route_group,
        _fb_vdiv(),
        _fb_slot(
            esc,
            duration_kicker,
            duration_value,
            "calendar",
            "gold",
            value_class="fb-value--gold",
            sub=duration_sub,
            title=duration_title,
        ),
        _fb_vdiv(),
        _fb_slot(esc, "Туристы", esc(party_value), "users", "neutral", sub=party_sub),
        _fb_vdiv(),
        _fb_slot(
            esc,
            "Отель",
            esc(_hotel_star_value(stars)),
            "hotel",
            "neutral",
            sub=hotel_sub,
        ),
        _fb_vdiv(),
        _fb_slot(
            esc,
            "Включено",
            esc(package),
            "package",
            "neutral",
        ),
        _fb_vdiv(),
        _fb_slot(
            esc,
            "Бюджет",
            f'{esc(budget_main)}<span class="fb-chev" aria-hidden="true">▾</span>',
            "wallet",
            "gold",
            title=budget_title,
        ),
    ]

    track = "\n".join(parts)
    return (
        '<div class="filter-bar filter-bar--premium" role="region" aria-label="Параметры фильтра">'
        f'<div class="filter-bar__track">{track}</div>'
        "</div>"
    )


def render_global_duration_switch_html(
    buckets: list[dict],
    *,
    default_bucket_id: str = "",
    escape: Any = None,
) -> str:
    """Segmented control: separate virtual filters on one dashboard page."""
    import html as html_mod

    esc = escape or html_mod.escape
    if not buckets:
        return ""
    options = []
    for item in buckets:
        if not isinstance(item, dict):
            continue
        bucket_id = str(item.get("id") or "").strip()
        if not bucket_id:
            continue
        label = str(item.get("label") or bucket_id).strip()
        options.append({'id': bucket_id, 'label': label})
    if not options:
        return ""
    default_id = str(default_bucket_id or options[-1]["id"]).strip()
    buttons = []
    for opt in options:
        active = ' active' if opt['id'] == default_id else ''
        buttons.append(
            f'<button type="button" class="duration-global-btn{active}" '
            f'data-duration-bucket="{esc(opt["id"], quote=True)}">{esc(opt["label"])}</button>'
        )
    return (
        '<div class="duration-global-switch" id="durationGlobalSwitch" role="group" '
        f'aria-label="Вариант фильтра" data-default-bucket="{esc(default_id, quote=True)}">'
        '<span class="duration-global-label">Фильтр</span>'
        f'<div class="duration-global-options">{"".join(buttons)}</div>'
        '</div>'
    )
