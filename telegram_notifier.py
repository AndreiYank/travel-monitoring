#!/usr/bin/env python3
"""
telegram_notifier.py — Flexible Telegram notification engine for Travel Price Monitor.

Features:
- Reads subscriptions and filter criteria from telegram_subscriptions.json.
- Identifies Hot Deals (Deal Score >= threshold), Significant Price Drops, and Historic Lows.
- Supports Daily Market Digests (summary of market movements and top deals per destination).
- Respects quiet hours per subscriber timezone (e.g., Europe/Warsaw).
- Deduplicates notifications via a local cache to avoid repeating alerts for unchanged prices.
- Supports rate limits (max messages per run/hour).
- CLI modes: --dry-run, --force-digest, --force-instant.
"""

from __future__ import annotations

import argparse
import datetime
import html
import json
import logging
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import zoneinfo
from typing import Any, Dict, List, Optional, Set, Tuple

import pandas as pd

from filter_registry import active_filter_groups, is_filter_active
from hotel_deal_score import (
    blend_tripadvisor_into_deal_score,
    compute_hotel_deal_metrics,
    time_weighted_price_baseline,
    time_weighted_price_quantile,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("telegram_notifier")

DEFAULT_CONFIG_PATH = "telegram_subscriptions.json"
DEFAULT_CACHE_PATH = "data/telegram_sent_cache.json"
CACHE_RETENTION_DAYS = 7


# ============================================================================
# Telegram API Client
# ============================================================================

def send_telegram_message(
    bot_token: str,
    chat_id: str | int,
    text: str,
    *,
    parse_mode: str = "HTML",
    disable_web_page_preview: bool = False,
    timeout: float = 15.0,
) -> bool:
    """Send an HTML message via the Telegram Bot API."""
    if not bot_token or not chat_id:
        logger.warning("Telegram bot token or chat ID is missing. Message skipped.")
        return False

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": disable_web_page_preview,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )

    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                if response.status == 200:
                    return True
                logger.warning(f"Telegram API response code: {response.status}")
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="ignore")
            logger.error(f"Telegram HTTP error ({e.code}): {err_body}")
            if e.code == 400 and "can't parse entities" in err_body:
                # Fallback: send as plain text if HTML parsing failed
                try:
                    payload["parse_mode"] = ""
                    fallback_data = json.dumps(payload).encode("utf-8")
                    fallback_req = urllib.request.Request(
                        url,
                        data=fallback_data,
                        headers={"Content-Type": "application/json; charset=utf-8"},
                        method="POST",
                    )
                    with urllib.request.urlopen(fallback_req, timeout=timeout) as fb_resp:
                        return fb_resp.status == 200
                except Exception as fb_err:
                    logger.error(f"Fallback plain text send failed: {fb_err}")
            if e.code in (403, 404):
                return False
        except Exception as err:
            logger.warning(f"Telegram request attempt {attempt} failed: {err}")
            time.sleep(1.5)

    return False


# ============================================================================
# Cache & Deduplication
# ============================================================================

def load_sent_cache(cache_path: str = DEFAULT_CACHE_PATH) -> Dict[str, Any]:
    if not os.path.isfile(cache_path):
        return {"version": 2, "history": {}, "digests": {}}
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, dict):
                return {"version": 2, "history": {}, "digests": {}}
            if "history" in data or "digests" in data:
                return {
                    "version": 2,
                    "history": dict(data.get("history") or {}),
                    "digests": dict(data.get("digests") or {}),
                }
            # Migration from version 1
            sent_legacy = data.get("sent", {})
            return {"version": 2, "history": {}, "digests": sent_legacy}
    except Exception as e:
        logger.warning(f"Could not load cache {cache_path}: {e}")
        return {"version": 2, "history": {}, "digests": {}}


def save_sent_cache(cache: Dict[str, Any], cache_path: str = DEFAULT_CACHE_PATH) -> None:
    try:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        cutoff = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=CACHE_RETENTION_DAYS)).isoformat()
        
        history = cache.get("history", {})
        cleaned_history = {
            k: v for k, v in history.items()
            if isinstance(v, dict) and v.get("last_sent_at", "") >= cutoff
        }
        
        digests = cache.get("digests", {})
        cleaned_digests = {
            k: v for k, v in digests.items()
            if str(v) >= cutoff
        }
        
        doc = {
            "version": 2,
            "history": cleaned_history,
            "digests": cleaned_digests,
        }
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.warning(f"Could not save cache {cache_path}: {e}")


# ============================================================================
# Configuration Loader
# ============================================================================

def load_config(config_path: str = DEFAULT_CONFIG_PATH) -> Dict[str, Any]:
    if not os.path.isfile(config_path):
        logger.warning(f"Config file '{config_path}' not found. Using default template.")
        return {"version": 1, "subscribers": []}

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            raw_text = f.read()

        # Substitute environment variables (e.g. ${TELEGRAM_CHAT_ID})
        def repl(match):
            var_name = match.group(1)
            return os.environ.get(var_name, f"${{{var_name}}}")

        processed_text = re.sub(r"\$\{([A-Za-z0-9_]+)\}", repl, raw_text)
        return json.loads(processed_text)
    except Exception as e:
        logger.error(f"Failed to parse config '{config_path}': {e}")
        return {"version": 1, "subscribers": []}


def slugify(text: str) -> str:
    text = str(text).lower().strip()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text or "hotel"


# ============================================================================
# Filter & Data Processing
# ============================================================================

class FilterDataSummary:
    def __init__(
        self,
        filter_id: str,
        data_id: str,
        filter_title: str,
        filter_href: str,
        data_dir: str,
        csv_path: str,
        df: pd.DataFrame,
        display_price_ceiling: int = 10000,
        config_url: str = "",
    ):
        self.filter_id = filter_id
        self.data_id = data_id
        self.filter_title = filter_title
        self.filter_href = filter_href
        self.data_dir = data_dir
        self.csv_path = csv_path
        self.df = df
        self.display_price_ceiling = display_price_ceiling
        self.config_url = config_url
        self.latest_run_time: Optional[datetime.datetime] = None
        self.hotel_metrics: List[Dict[str, Any]] = []
        self.market_breadth: float = 0.0
        self.hotels_down_count: int = 0
        self.hotels_up_count: int = 0
        self.total_active_hotels: int = 0
        self.median_market_price: float = 0.0
        self.best_deal: Optional[Dict[str, Any]] = None


def analyze_filter_data(flt: Dict[str, Any], group: Optional[Dict[str, Any]] = None) -> Optional[FilterDataSummary]:
    filter_id = flt.get("id", "")
    flt_title = flt.get("title", filter_id)
    filter_href = flt.get("href", "index.html")
    charts = flt.get("charts_subdir") or ""
    data_id = charts.rsplit("/", 1)[-1] if charts else filter_id
    data_dir = os.path.join("data/filters", data_id)
    csv_path = os.path.join(data_dir, "travel_prices.csv")

    group_label = (group.get("label") or "") if group else ""
    group_icon = (group.get("icon") or "") if group else ""
    
    if group_label and group_label.lower() not in flt_title.lower() and group_label != "Поездки":
        filter_title = f"{group_icon} {group_label} • {flt_title}".strip()
    elif group_icon and not flt_title.startswith(group_icon):
        filter_title = f"{group_icon} {flt_title}".strip()
    else:
        filter_title = flt_title

    # Read display price ceiling and url from filter config
    config_file = flt.get("config")
    display_price_ceiling = 10000
    config_url = ""
    if config_file and os.path.isfile(config_file):
        try:
            with open(config_file, "r", encoding="utf-8") as f:
                cfg_json = json.load(f)
                display_price_ceiling = int(cfg_json.get("display_price_ceiling") or 10000)
                config_url = str(cfg_json.get("url") or "").strip()
        except Exception:
            display_price_ceiling = 10000

    if not os.path.isfile(csv_path):
        return None

    try:
        frames = []
        archive_dir = os.path.join(data_dir, "archive")
        if os.path.isdir(archive_dir):
            for af in sorted(os.listdir(archive_dir)):
                if af.startswith("travel_prices_") and af.endswith(".csv"):
                    try:
                        frames.append(pd.read_csv(os.path.join(archive_dir, af), quoting=1, on_bad_lines="skip"))
                    except Exception:
                        pass
        frames.append(pd.read_csv(csv_path, quoting=1, on_bad_lines="skip"))
        df = pd.concat(frames, ignore_index=True, sort=False) if len(frames) > 1 else frames[0]
    except Exception as e:
        logger.warning(f"Could not read {csv_path}: {e}")
        return None

    if df.empty or "hotel_name" not in df.columns or "price" not in df.columns or "scraped_at" not in df.columns:
        return None

    df["price_num"] = pd.to_numeric(df["price"], errors="coerce")
    df = df[df["price_num"].notna() & (df["price_num"] > 0)].copy()
    if df.empty:
        return None

    df["scraped_at_dt"] = pd.to_datetime(df["scraped_at"], errors="coerce", utc=True)
    df = df.dropna(subset=["scraped_at_dt"]).sort_values("scraped_at_dt")
    if df.empty:
        return None

    latest_run_time = df["scraped_at_dt"].max()
    summary = FilterDataSummary(
        filter_id=filter_id,
        data_id=data_id,
        filter_title=filter_title,
        filter_href=filter_href,
        data_dir=data_dir,
        csv_path=csv_path,
        df=df,
        display_price_ceiling=display_price_ceiling,
        config_url=config_url,
    )
    summary.latest_run_time = latest_run_time

    # Group by hotel_name
    hotel_metrics_list = []
    breadth_total = 0
    breadth_down = 0
    breadth_up = 0
    current_prices = []

    cutoff_48h = latest_run_time - datetime.timedelta(hours=48)
    cutoff_run = latest_run_time - datetime.timedelta(minutes=45)

    for hotel_name, grp in df.groupby("hotel_name", sort=False):
        grp_sorted = grp.sort_values("scraped_at_dt")
        if grp_sorted.empty:
            continue

        latest_row = grp_sorted.iloc[-1]
        latest_time = latest_row["scraped_at_dt"]
        is_in_latest_run = latest_time >= cutoff_run

        # If hotel was not scraped in latest run, skip active notifications
        if not is_in_latest_run:
            continue

        current_price = float(latest_row["price_num"])
        current_prices.append(current_price)

        # Filter history to same trip dates for accurate price comparisons
        current_dates = str(latest_row.get("dates") or "")
        if current_dates:
            same_dates_grp = grp_sorted[grp_sorted["dates"].astype(str) == current_dates]
        else:
            same_dates_grp = grp_sorted

        # Baseline 48 hours ago (same trip dates only)
        same_dates_48h = same_dates_grp[same_dates_grp["scraped_at_dt"] >= cutoff_48h]
        if len(same_dates_48h) >= 2:
            baseline_row = same_dates_48h.iloc[0]
        elif len(same_dates_grp) >= 2:
            baseline_row = same_dates_grp.iloc[-2]
        else:
            baseline_row = latest_row

        baseline_price = float(baseline_row["price_num"])
        delta_48h_pct = ((current_price - baseline_price) / baseline_price * 100.0) if baseline_price > 0 else 0.0

        if baseline_price > 0 and len(same_dates_grp) >= 2:
            breadth_total += 1
            if current_price < baseline_price:
                breadth_down += 1
            elif current_price > baseline_price:
                breadth_up += 1

        # Previous run baseline (same trip dates only, within last 4 hours)
        if len(same_dates_grp) >= 2:
            prev_row = same_dates_grp.iloc[-2]
            prev_time = prev_row["scraped_at_dt"]
            if (latest_time - prev_time) <= datetime.timedelta(hours=4):
                prev_price = float(prev_row["price_num"])
                delta_run_pct = ((current_price - prev_price) / prev_price * 100.0) if prev_price > 0 else 0.0
            else:
                prev_price = current_price
                delta_run_pct = 0.0
        else:
            prev_price = current_price
            delta_run_pct = 0.0

        # Historical minimum across full season history (all dates for this hotel)
        all_prices_prior = grp_sorted.iloc[:-1]["price_num"].astype(float).tolist()
        hist_min = min(grp_sorted["price_num"].astype(float).tolist())
        is_historic_low = (current_price <= min(all_prices_prior)) if all_prices_prior else False

        # TripAdvisor info
        ta_rating_raw = latest_row.get("ta_rating")
        ta_reviews_raw = latest_row.get("ta_review_count")
        try:
            ta_rating = float(ta_rating_raw) if pd.notna(ta_rating_raw) else None
        except Exception:
            ta_rating = None
        try:
            ta_reviews = int(float(ta_reviews_raw)) if pd.notna(ta_reviews_raw) else 0
        except Exception:
            ta_reviews = 0

        # Deal metrics
        deal_info = compute_hotel_deal_metrics(
            grp_sorted,
            current_price,
            time_col="scraped_at_dt",
            price_col="price_num",
            ta_rating=ta_rating,
            ta_review_count=ta_reviews,
        )
        deal_score = int(deal_info.get("deal_score") or 0)
        confidence = str(deal_info.get("confidence") or "Low")

        # Blend TA
        deal_score, _ = blend_tripadvisor_into_deal_score(deal_score, ta_rating, ta_reviews)

        # Offer details
        dates = str(latest_row.get("dates") or "")
        duration = str(latest_row.get("duration") or "")
        airport = str(latest_row.get("departure_airport") or "")
        offer_url = str(latest_row.get("offer_url") or latest_row.get("url") or "")
        image_url = str(latest_row.get("image_url") or "")

        hotel_metrics_list.append({
            "hotel_name": str(hotel_name),
            "current_price": current_price,
            "prev_price": prev_price,
            "baseline_48h_price": baseline_price,
            "delta_48h_pct": delta_48h_pct,
            "delta_run_pct": delta_run_pct,
            "hist_min": hist_min,
            "is_historic_low": is_historic_low,
            "deal_score": deal_score,
            "confidence": confidence,
            "ta_rating": ta_rating,
            "ta_reviews": ta_reviews,
            "dates": dates,
            "duration": duration,
            "airport": airport,
            "offer_url": offer_url,
            "image_url": image_url,
            "scraped_at": latest_time.isoformat(),
        })

    summary.hotel_metrics = hotel_metrics_list
    summary.total_active_hotels = len(hotel_metrics_list)
    summary.hotels_down_count = breadth_down
    summary.hotels_up_count = breadth_up
    summary.market_breadth = (breadth_down / breadth_total * 100.0) if breadth_total > 0 else 0.0
    summary.median_market_price = float(pd.Series(current_prices).median()) if current_prices else 0.0

    max_ceiling = (display_price_ceiling or 10000) + 1000
    valid_deals = [h for h in hotel_metrics_list if h["current_price"] <= max_ceiling]
    if valid_deals:
        summary.best_deal = max(valid_deals, key=lambda x: (x["deal_score"], -x["delta_48h_pct"]))
    elif hotel_metrics_list:
        summary.best_deal = max(hotel_metrics_list, key=lambda x: (x["deal_score"], -x["delta_48h_pct"]))

    return summary


# ============================================================================
# Notification Builders
# ============================================================================

def normalize_fly_offer_url(offer_url: str, config_url: str = "") -> str:
    """
    Normalizes a Fly.pl offer URL:
    1. Ensures all participant parameters (filter[person], filter[child], filter[childAge])
       are preserved, and restores them from the filter's search config if missing.
    2. Reconstructs the query string with decoded parameter keys (filter[person] instead of filter%5Bperson%5D)
       so that Fly.pl accurately recognizes family group composition (e.g. 2 adults + 1 child).
    """
    if not offer_url or "fly.pl" not in offer_url:
        return str(offer_url).strip()

    pax_params: Dict[str, str] = {}
    if config_url:
        try:
            cfg_parsed = urllib.parse.urlparse(config_url)
            cfg_qs = urllib.parse.parse_qs(cfg_parsed.query, keep_blank_values=True)
            for k, v in cfg_qs.items():
                norm_k = urllib.parse.unquote(k)
                if any(p in norm_k for p in ["person", "child", "childAge"]):
                    pax_params[norm_k] = v[0] if v else ""
        except Exception:
            pass

    try:
        parsed = urllib.parse.urlparse(offer_url)
        qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        norm_qs: Dict[str, List[str]] = {}
        for k, v_list in qs.items():
            norm_k = urllib.parse.unquote(k)
            norm_qs[norm_k] = [urllib.parse.unquote(str(v)) for v in v_list]

        has_person = any("person" in k for k in norm_qs.keys())
        has_child = any("child" in k or "childAge" in k for k in norm_qs.keys())

        if (not has_person or not has_child) and pax_params:
            for k, v in pax_params.items():
                if k not in norm_qs:
                    norm_qs[k] = [v]

        query_parts = []
        for k, v_list in norm_qs.items():
            for v in v_list:
                query_parts.append(f"{k}={v}")

        new_query = "&".join(query_parts)
        return urllib.parse.urlunparse((
            parsed.scheme or "https",
            parsed.netloc,
            parsed.path,
            parsed.params,
            new_query,
            parsed.fragment,
        ))
    except Exception:
        return str(offer_url).strip()


def _clean_telegram_href(url: str, config_url: str = "") -> str:
    if not url:
        return ""
    normalized = normalize_fly_offer_url(url, config_url)
    # In Telegram HTML parse mode, attributes in <a href="..."> MUST be HTML-escaped (& -> &amp;, " -> &quot;).
    # Telegram Bot API converts &amp; back to & when creating the message text_link entity.
    # If & is left unescaped, Telegram's parser treats &filter as an unknown entity and drops subsequent query parameters,
    # stripping filter[person]=2 and filter[child]=1, causing Fly.pl to fall back to default 2+0.
    return html.escape(str(normalized).strip(), quote=True)


def format_hotel_alert_message(
    flt_summary: FilterDataSummary,
    item: Dict[str, Any],
    alert_type: str,  # "hot_deal" | "price_drop" | "historic_low"
) -> str:
    hotel_name = html.escape(item["hotel_name"])
    title = html.escape(flt_summary.filter_title)
    current_p = f"{item['current_price']:,.0f}".replace(",", " ")
    base_p = f"{item['baseline_48h_price']:,.0f}".replace(",", " ")
    delta_pct = item["delta_48h_pct"]

    # Header emoji & title
    if alert_type == "flash_drop":
        header = f"⚡ <b>РЕЗКИЙ ОБВАЛ ЦЕНЫ {delta_pct:+.1f}% • {title}</b>"
    elif alert_type == "hot_deal":
        header = f"🔥 <b>SUPER HOT DEAL • {title}</b>"
    elif alert_type == "historic_low":
        header = f"📉 <b>ИСТОРИЧЕСКИЙ МИНИМУМ • {title}</b>"
    else:
        header = f"📉 <b>СНИЖЕНИЕ ЦЕНЫ {delta_pct:+.1f}% • {title}</b>"

    # Rating badge
    rating_str = ""
    if item["ta_rating"] is not None:
        rating_str = f" ⭐ <b>{item['ta_rating']:.1f}</b>"
        if item["ta_reviews"] > 0:
            rating_str += f" <i>({item['ta_reviews']} отзывов)</i>"

    # Deal pill
    score = item["deal_score"]
    score_pill = f"🎯 Deal Score: <b>{score} / 100</b> ({item['confidence']})"

    # Details
    dates_str = html.escape(item["dates"]) if item["dates"] else "По запросу"
    dur_str = html.escape(item["duration"]) if item["duration"] else ""
    dur_part = f" • {dur_str}" if dur_str else ""
    airport_str = html.escape(item["airport"]) if item["airport"] else "Любой аэропорт"

    # Links
    links = []
    if item["offer_url"]:
        clean_url = _clean_telegram_href(item["offer_url"], flt_summary.config_url)
        links.append(f'<a href="{clean_url}">🔗 Открыть на Fly.pl</a>')
    
    # Hotel specific chart link
    hotel_slug = slugify(item["hotel_name"])
    hotel_chart_url = f"https://jancker2a.github.io/travel-monitoring/hotel-chart.html?filter={flt_summary.data_id}&hotel={hotel_slug}"
    links.append(f'<a href="{hotel_chart_url}">📈 График отеля</a>')

    # Dashboard link
    dashboard_url = f"https://jancker2a.github.io/travel-monitoring/{flt_summary.filter_href}"
    links.append(f'<a href="{dashboard_url}">📊 Дашборд</a>')

    price_info = f"💰 <b>{current_p} PLN</b>"
    if float(item.get("baseline_48h_price") or 0) > 0 and base_p != current_p and delta_pct < 0:
        price_info += f" (было {base_p} PLN → <b>{delta_pct:+.1f}%</b>)"

    lines = [
        header,
        "",
        f"🏨 <b>{hotel_name}</b>{rating_str}",
        price_info,
        f"{score_pill}",
        f"📅 {dates_str}{dur_part}",
        f"✈️ {airport_str}",
    ]

    if item["is_historic_low"]:
        lines.append("📉 <i>Это новый исторический минимум цены за всё время наблюдений!</i>")

    lines.append("")
    lines.append(" • ".join(links))

    return "\n".join(lines)


def format_daily_digest_message(summaries: List[FilterDataSummary], subscriber_name: str = "") -> str:
    now_str = datetime.datetime.now(zoneinfo.ZoneInfo("Europe/Warsaw")).strftime("%d.%m.%Y %H:%M")
    greeting = f"Привет, {html.escape(subscriber_name)}!" if subscriber_name else "Доброе утро!"
    
    lines = [
        f"📊 <b>Утренний дайджест цен • {now_str}</b>",
        f"<i>{greeting} Вот сводка ситуации на рынке туров:</i>",
        "━━━━━━━━━━━━━━━━━━",
    ]

    for s in summaries:
        if not s.hotel_metrics:
            continue
        title = html.escape(s.filter_title)
        lines.append(f"🎯 <b>{title}</b> (активно {s.total_active_hotels} отелей):")
        
        # Stats
        if s.hotels_down_count > 0 or s.hotels_up_count > 0:
            lines.append(f"  📉 Подешевели: <b>{s.hotels_down_count}</b> • 📈 Подорожали: <b>{s.hotels_up_count}</b> (Breadth: {s.market_breadth:.0f}%)")
        
        if s.best_deal:
            bd = s.best_deal
            bd_name = html.escape(bd["hotel_name"])
            bd_price = f"{bd['current_price']:,.0f}".replace(",", " ")
            lines.append(f"  🔥 Топ-дил: <b>{bd_name}</b> — <b>{bd_price} PLN</b> (Deal Score {bd['deal_score']}, {bd['delta_48h_pct']:+.1f}%)")
        lines.append("")

    lines.append("━━━━━━━━━━━━━━━━━━")
    lines.append('🌐 <a href="https://jancker2a.github.io/travel-monitoring/">Открыть все дашборды и графики</a>')

    return "\n".join(lines)


# ============================================================================
# Main Dispatcher
# ============================================================================

def process_notifications(
    config_path: str = DEFAULT_CONFIG_PATH,
    cache_path: str = DEFAULT_CACHE_PATH,
    dry_run: bool = False,
    force_digest: bool = False,
    force_instant: bool = False,
    target_filter_id: Optional[str] = None,
) -> int:
    config = load_config(config_path)
    subscribers = config.get("subscribers", [])
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()

    if not subscribers:
        logger.info("No subscribers configured in telegram_subscriptions.json.")
        return 0

    if not bot_token and not dry_run:
        logger.warning("TELEGRAM_BOT_TOKEN is not set. Running in dry-run simulation mode.")
        dry_run = True

    # 1. Discover all active filters and process summaries
    logger.info("Scanning active filters data...")
    filter_summaries: Dict[str, FilterDataSummary] = {}
    
    for group in active_filter_groups():
        for flt in group["filters"]:
            flt_id = flt.get("id")
            if target_filter_id and flt_id != target_filter_id:
                continue
            summary = analyze_filter_data(flt, group)
            if summary:
                filter_summaries[flt_id] = summary
                logger.info(f"  ✓ {flt_id} ({summary.filter_title}): {summary.total_active_hotels} hotels, ceiling: {summary.display_price_ceiling} PLN")

    sent_cache = load_sent_cache(cache_path)
    total_messages_sent = 0
    now_utc_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

    # 2. Iterate subscribers and evaluate rules
    for sub in subscribers:
        if not sub.get("enabled", True):
            continue

        chat_id = str(sub.get("chat_id") or "").strip()
        sub_name = sub.get("name", "Subscriber")
        if not chat_id or chat_id.startswith("${"):
            logger.warning(f"Subscriber '{sub_name}' has invalid chat_id ({chat_id}). Skipping.")
            continue

        rules = sub.get("rules", {})
        schedule = sub.get("schedule", {})
        sub_filters = sub.get("filters", [])  # Empty = all filters

        tz_name = schedule.get("timezone", "Europe/Warsaw")
        try:
            sub_tz = zoneinfo.ZoneInfo(tz_name)
        except Exception:
            sub_tz = zoneinfo.ZoneInfo("Europe/Warsaw")

        now_sub_time = datetime.datetime.now(sub_tz)
        current_hour = now_sub_time.hour
        quiet_hours = schedule.get("quiet_hours", [23, 0, 1, 2, 3, 4, 5, 6])
        daily_digest_hour = int(schedule.get("daily_digest_hour", 9))
        mode = schedule.get("mode", "instant")
        max_msgs = int(schedule.get("max_messages_per_hour", 5))

        is_quiet = (current_hour in quiet_hours) and not force_instant
        is_digest_time = (current_hour == daily_digest_hour) or force_digest

        logger.info(f"Processing subscriber '{sub_name}' (Chat: {chat_id}, Local time: {now_sub_time.strftime('%H:%M %Z')}, Quiet: {is_quiet}, Digest: {is_digest_time})")

        # A. Send Daily Digest if scheduled
        if is_digest_time and rules.get("notify_market_summary", True):
            digest_cache_key = f"{chat_id}:daily_digest:{now_sub_time.strftime('%Y-%m-%d')}"
            digests_cache = sent_cache.setdefault("digests", {})
            if digest_cache_key not in digests_cache or force_digest:
                applicable_summaries = [
                    s for fid, s in filter_summaries.items()
                    if not sub_filters or fid in sub_filters
                ]
                if applicable_summaries:
                    digest_text = format_daily_digest_message(applicable_summaries, sub_name)
                    logger.info(f"  📨 Sending daily digest to {sub_name}...")
                    if dry_run:
                        print("\n" + "=" * 50 + " [SIMULATED TELEGRAM DIGEST] " + "=" * 50)
                        print(digest_text)
                        print("=" * 125 + "\n")
                        digests_cache[digest_cache_key] = now_utc_iso
                        total_messages_sent += 1
                    else:
                        ok = send_telegram_message(bot_token, chat_id, digest_text)
                        if ok:
                            digests_cache[digest_cache_key] = now_utc_iso
                            total_messages_sent += 1
                            time.sleep(1.0)

        # B. If quiet hours or digest-only mode, skip instant alerts
        if is_quiet or mode == "digest":
            logger.info(f"  ⏭ Instant alerts skipped for {sub_name} (quiet hours or digest mode).")
            continue

        # C. Instant Alerts (Hot Deals, Big Drops, Historic Lows)
        candidate_alerts: List[Tuple[int, FilterDataSummary, Dict[str, Any], str]] = []
        
        deal_score_min = int(rules.get("deal_score_min", 90))
        price_drop_pct_min = float(rules.get("price_drop_pct_min", 15.0))
        single_run_drop_pct_min = float(rules.get("single_run_drop_pct_min", 12.0))
        max_price_pln = float(rules.get("max_price_pln")) if rules.get("max_price_pln") is not None else None
        min_ta_rating = float(rules.get("min_ta_rating", 3.8))
        notify_hot_deals = rules.get("notify_hot_deals", True)
        notify_price_drops = rules.get("notify_price_drops", True)
        notify_historic_low = rules.get("notify_new_historic_low", True)

        for flt_id, summary in filter_summaries.items():
            if sub_filters and flt_id not in sub_filters:
                continue

            max_ceiling_price = (summary.display_price_ceiling or 10000) + 1000

            for item in summary.hotel_metrics:
                cur_price = item["current_price"]
                prev_price = item.get("prev_price")
                deal_score = item["deal_score"]
                delta_48h = item["delta_48h_pct"]
                delta_run = item.get("delta_run_pct", 0.0)
                ta_rating = item["ta_rating"]

                # Price ceiling filter (must be within filter's ceiling + 1000 PLN)
                if cur_price > max_ceiling_price:
                    continue

                # Global filters
                if max_price_pln and cur_price > max_price_pln:
                    continue
                if ta_rating is not None and ta_rating < min_ta_rating:
                    continue

                # Instant alert requirement: MUST be a fresh price drop in the latest scrape run
                # (dropped by >= 2% in the last 4h, or brand-new offer with high deal score)
                is_fresh_drop = (delta_run <= -2.0)
                is_brand_new_offer = (prev_price is None or prev_price <= 0)
                if not is_fresh_drop and not is_brand_new_offer:
                    continue

                # Check 1: Flash Drop (Sudden drop >= 12% in this run AND delta_48h < 0, OR >= 15% drop in 48h with fresh drop)
                if (
                    ((delta_run <= -single_run_drop_pct_min and delta_48h < 0) or (delta_48h <= -price_drop_pct_min and is_fresh_drop))
                    and cur_price > 0
                ):
                    candidate_alerts.append((150 + int(abs(delta_48h)), summary, item, "flash_drop"))
                    continue

                # Check 2: Super Hot Deal (Deal Score >= 90, e.g. 90-100, and price dropped >= 3% in 48h)
                if notify_hot_deals and deal_score >= deal_score_min and delta_48h <= -3.0 and item["confidence"] != "Low":
                    candidate_alerts.append((deal_score, summary, item, "hot_deal"))
                    continue

                # Check 3: True Historic Low (All-time low + drop >= 5% in 48h + Deal Score >= 80 + high confidence)
                if (
                    notify_historic_low
                    and item["is_historic_low"]
                    and delta_48h <= -5.0
                    and deal_score >= 80
                    and item["confidence"] != "Low"
                ):
                    candidate_alerts.append((deal_score, summary, item, "historic_low"))
                    continue

        # Sort candidate alerts by highest priority
        candidate_alerts.sort(key=lambda x: x[0], reverse=True)

        sent_count_sub = 0
        history_cache = sent_cache.setdefault("history", {})

        for _, flt_summary, item, alert_type in candidate_alerts:
            if sent_count_sub >= max_msgs:
                logger.info(f"  Reached max message limit ({max_msgs}) for {sub_name}.")
                break

            hotel_name = item["hotel_name"]
            cur_price = item["current_price"]
            hotel_key = f"{chat_id}:{flt_summary.filter_id}:{hotel_name}"

            # Check if this hotel was already sent to this subscriber
            prev_info = history_cache.get(hotel_key)
            if prev_info:
                last_sent_price = float(prev_info.get("last_sent_price") or 0.0)
                # If already sent, only re-alert if price dropped by another >= 5% below last sent price
                if last_sent_price > 0 and cur_price >= last_sent_price * 0.95:
                    continue

            msg_text = format_hotel_alert_message(flt_summary, item, alert_type)
            logger.info(f"  📨 Sending {alert_type} alert for '{hotel_name}' ({cur_price:.0f} PLN) to {sub_name}...")

            if dry_run:
                print("\n" + "-" * 40 + f" [SIMULATED ALERT: {alert_type.upper()}] " + "-" * 40)
                print(msg_text)
                print("-" * 105 + "\n")
                history_cache[hotel_key] = {
                    "last_sent_price": cur_price,
                    "last_sent_deal_score": item["deal_score"],
                    "last_sent_at": now_utc_iso,
                    "alert_type": alert_type,
                }
                sent_count_sub += 1
                total_messages_sent += 1
            else:
                ok = send_telegram_message(bot_token, chat_id, msg_text)
                if ok:
                    history_cache[hotel_key] = {
                        "last_sent_price": cur_price,
                        "last_sent_deal_score": item["deal_score"],
                        "last_sent_at": now_utc_iso,
                        "alert_type": alert_type,
                    }
                    sent_count_sub += 1
                    total_messages_sent += 1
                    time.sleep(1.0)
                else:
                    # If failed (e.g. 403 Forbidden or invalid chat_id), abort loop for this subscriber
                    logger.warning(f"  Aborting further notifications for {sub_name} due to delivery failure.")
                    break

    save_sent_cache(sent_cache, cache_path)
    logger.info(f"Done! Total notifications dispatched: {total_messages_sent}")
    return total_messages_sent


# ============================================================================
# CLI Entrypoint
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description="Travel Price Monitor — Telegram Notifier")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH, help="Path to telegram_subscriptions.json")
    parser.add_argument("--cache", default=DEFAULT_CACHE_PATH, help="Path to telegram_sent_cache.json")
    parser.add_argument("--dry-run", action="store_true", help="Simulate and print to stdout without calling Telegram API")
    parser.add_argument("--force-digest", action="store_true", help="Force send daily digest regardless of current hour")
    parser.add_argument("--force-instant", action="store_true", help="Ignore quiet hours and force check instant alerts")
    parser.add_argument("--filter", dest="filter_id", default=None, help="Process only a specific filter ID")

    args = parser.parse_args()
    process_notifications(
        config_path=args.config,
        cache_path=args.cache,
        dry_run=args.dry_run,
        force_digest=args.force_digest,
        force_instant=args.force_instant,
        target_filter_id=args.filter_id,
    )


if __name__ == "__main__":
    main()
