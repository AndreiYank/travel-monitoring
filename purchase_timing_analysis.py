#!/usr/bin/env python3
"""Анализ «когда покупать»: статистика снижения цен по часу дня, дню недели,
части месяца и месяцу.

Методология (см. план Purchase Timing Stats):
- Единица анализа — конкретный оффер (hotel_name, dates, duration, departure_airport),
  отслеживаемый во времени.
- Цены дедуплицируются до одного значения на (оффер, час) через медиану.
- Между соседними замерами одного оффера считается лог-доходность r = ln(p_i / p_{i-1}).
  Лог-доходности аддитивны и нормируют разный уровень цен. Изменение приписывается
  времени позднего замера t_i (в локальном часовом поясе покупателя).
- По каждому временному бакету считается P(снижение) с доверительным интервалом
  Уилсона, средняя лог-доходность, медианный размер снижения и «интенсивность».
- Достоверность бакета определяется числом РАЗЛИЧНЫХ календарных единиц покрытия
  (дней/недель/месяцев), а не сырым числом интервалов — иначе один день данных
  заполнит все 24 часовых бакета коррелированным шумом.
"""

from __future__ import annotations

import math
import numpy as np
import pandas as pd

OFFER_KEYS = ["hotel_name", "dates", "duration", "departure_airport"]

DOW_LABELS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
MONTH_LABELS = {
    1: "Январь", 2: "Февраль", 3: "Март", 4: "Апрель", 5: "Май", 6: "Июнь",
    7: "Июль", 8: "Август", 9: "Сентябрь", 10: "Октябрь", 11: "Ноябрь", 12: "Декабрь",
}
PART_ORDER = ["early", "mid", "late"]
PART_LABELS = {"early": "1–10", "mid": "11–20", "late": "21–31"}

# Порог |r|, ниже которого изменение считаем шумом округления (0.5%).
DEFAULT_EPS = 0.005
# Максимальный разрыв между соседними замерами, который ещё считаем непрерывным (часы).
DEFAULT_MAX_GAP_HOURS = 6

# Пороги достоверности (число различных единиц покрытия) для каждого измерения.
CONFIDENCE_THRESHOLDS = {
    "hour": {"reliable": 14, "preliminary": 7, "unit": "дней"},
    "dow": {"reliable": 4, "preliminary": 2, "unit": "недель"},
    "part": {"reliable": 3, "preliminary": 2, "unit": "месяцев"},
    "month": {"reliable": 2, "preliminary": 1, "unit": "сезонов"},
}


def wilson_interval(k: int, n: int, z: float = 1.96):
    """Доверительный интервал Уилсона для доли (корректен при малых n)."""
    if n <= 0:
        return 0.0, 0.0
    phat = k / n
    denom = 1.0 + z * z / n
    center = (phat + z * z / (2 * n)) / denom
    margin = (z * math.sqrt((phat * (1 - phat) + z * z / (4 * n)) / n)) / denom
    return max(0.0, center - margin), min(1.0, center + margin)


def coverage_confidence(distinct: int, thresholds: dict):
    """Возвращает (уровень, прогресс_к_надёжному) по числу единиц покрытия."""
    r = thresholds["reliable"]
    p = thresholds["preliminary"]
    progress = min(1.0, distinct / r) if r else 0.0
    if distinct >= r:
        return "reliable", progress
    if distinct >= p:
        return "preliminary", progress
    return "collecting", progress


def _ensure_local(df: pd.DataFrame, tz: str) -> pd.Series:
    """Возвращает локализованный (tz покупателя) timestamp."""
    if "scraped_at_local" in df.columns:
        s = pd.to_datetime(df["scraped_at_local"], errors="coerce")
        # Если уже tz-aware — конвертируем, иначе локализуем.
        try:
            if s.dt.tz is None:
                s = s.dt.tz_localize(tz)
            else:
                s = s.dt.tz_convert(tz)
        except (TypeError, AttributeError):
            pass
        return s
    raw = df["scraped_at"].astype(str)
    s = pd.to_datetime(raw, errors="coerce", utc=True)
    try:
        s = s.dt.tz_convert(tz)
    except (TypeError, AttributeError):
        pass
    return s


def build_offer_panel(df: pd.DataFrame, tz: str = "Europe/Warsaw") -> pd.DataFrame:
    """Дедуплицирует цены до одного значения на (оффер, час) через медиану."""
    work = df.copy()
    work["_ts"] = _ensure_local(work, tz)
    work = work.dropna(subset=["_ts"])
    work["price"] = pd.to_numeric(work["price"], errors="coerce")
    work = work.dropna(subset=["price"])
    work = work[work["price"] > 0]
    for k in OFFER_KEYS:
        if k not in work.columns:
            work[k] = ""
        work[k] = work[k].fillna("").astype(str)
    if work.empty:
        return work.assign(_hour=pd.Series(dtype="datetime64[ns]"))
    work["_hour"] = work["_ts"].dt.floor("h")
    panel = (
        work.groupby(OFFER_KEYS + ["_hour"], as_index=False)
        .agg(price=("price", "median"))
    )
    return panel


def compute_change_events(
    panel: pd.DataFrame,
    eps: float = DEFAULT_EPS,
    max_gap_hours: float = DEFAULT_MAX_GAP_HOURS,
) -> pd.DataFrame:
    """Считает события изменения цены между соседними замерами одного оффера."""
    if panel.empty:
        return panel.assign(r=pd.Series(dtype="float64"))
    panel = panel.sort_values(OFFER_KEYS + ["_hour"]).copy()
    grp = panel.groupby(OFFER_KEYS, sort=False)
    panel["_prev_price"] = grp["price"].shift(1)
    panel["_prev_hour"] = grp["_hour"].shift(1)
    ev = panel.dropna(subset=["_prev_price", "_prev_hour"]).copy()
    if ev.empty:
        return ev.assign(r=pd.Series(dtype="float64"))
    gap = (ev["_hour"] - ev["_prev_hour"]).dt.total_seconds() / 3600.0
    ev["_gap"] = gap
    ev = ev[(ev["_gap"] > 0) & (ev["_gap"] <= max_gap_hours)].copy()
    if ev.empty:
        return ev.assign(r=pd.Series(dtype="float64"))
    ev["r"] = np.log(ev["price"] / ev["_prev_price"])
    ev["abs_change"] = ev["price"] - ev["_prev_price"]
    ev["is_drop"] = ev["r"] < -eps
    ev["is_rise"] = ev["r"] > eps
    t = ev["_hour"]
    ev["hour"] = t.dt.hour.astype(int)
    ev["dow"] = t.dt.dayofweek.astype(int)
    ev["dom"] = t.dt.day.astype(int)
    ev["month"] = t.dt.month.astype(int)
    ev["year"] = t.dt.year.astype(int)
    ev["date"] = t.dt.strftime("%Y-%m-%d")
    ev["isoweek"] = t.dt.strftime("%G-W%V")
    ev["yearmonth"] = t.dt.strftime("%Y-%m")
    ev["part"] = np.where(ev["dom"] <= 10, "early", np.where(ev["dom"] <= 20, "mid", "late"))
    return ev


def _bucket_stats(sub: pd.DataFrame, baseline_p: float, distinct: int, thresholds: dict) -> dict:
    n = int(len(sub))
    d = int(sub["is_drop"].sum()) if n else 0
    p_drop = (d / n) if n else 0.0
    lo, hi = wilson_interval(d, n)
    mean_r = float(sub["r"].mean()) if n else 0.0
    drops = sub[sub["is_drop"]]
    drop_pct = float((-drops["r"]).median() * 100) if len(drops) else 0.0
    drop_pln = float((-drops["abs_change"]).median()) if len(drops) else 0.0
    intensity = p_drop * (drop_pct / 100.0)
    conf, progress = coverage_confidence(distinct, thresholds)
    # Значимо лучше среднего: нижняя граница CI выше базовой вероятности снижения.
    significant = bool(n > 0 and lo > baseline_p and conf in ("reliable", "preliminary"))
    return {
        "n": n,
        "drops": d,
        "p_drop": round(p_drop, 4),
        "wilson_lo": round(lo, 4),
        "wilson_hi": round(hi, 4),
        "mean_r": round(mean_r, 5),
        "drop_pct": round(drop_pct, 2),
        "drop_pln": round(drop_pln, 1),
        "intensity": round(intensity, 5),
        "distinct": int(distinct),
        "confidence": conf,
        "progress": round(progress, 3),
        "significant": significant,
    }


def aggregate_dimension(
    events: pd.DataFrame,
    key_col: str,
    order: list,
    labels: dict,
    conf_unit_col: str,
    thresholds: dict,
    baseline_p: float,
    informational: bool = False,
) -> dict:
    """Агрегирует события по измерению и возвращает бакеты + мета измерения."""
    buckets = []
    best = None
    max_progress = 0.0
    reached = "collecting"
    rank = {"collecting": 0, "preliminary": 1, "reliable": 2}
    for key in order:
        sub = events[events[key_col] == key] if not events.empty else events
        distinct = int(sub[conf_unit_col].nunique()) if len(sub) else 0
        stats = _bucket_stats(sub, baseline_p, distinct, thresholds)
        stats["key"] = key if not isinstance(key, (np.integer,)) else int(key)
        stats["label"] = labels.get(key, str(key))
        buckets.append(stats)
        max_progress = max(max_progress, stats["progress"])
        if rank[stats["confidence"]] > rank[reached]:
            reached = stats["confidence"]
        if (
            not informational
            and stats["significant"]
            and stats["confidence"] == "reliable"
            and (best is None or stats["intensity"] > best["intensity"])
        ):
            best = stats
    return {
        "buckets": buckets,
        "best": best,
        "reached_confidence": reached,
        "progress": round(max_progress, 3),
        "informational": informational,
        "unit": thresholds["unit"],
        "reliable_threshold": thresholds["reliable"],
    }


def analyze_purchase_timing(df: pd.DataFrame, tz: str = "Europe/Warsaw") -> dict:
    """Главная точка входа: возвращает JSON-сериализуемый словарь со всеми измерениями."""
    panel = build_offer_panel(df, tz=tz)
    events = compute_change_events(panel)

    n_events = int(len(events))
    if n_events == 0:
        return {
            "available": False,
            "status": "collecting",
            "message": "Недостаточно парных наблюдений одного и того же тура для анализа динамики.",
            "n_events": 0,
            "history": {"days": 0, "weeks": 0, "first": None, "last": None},
            "baseline_p_drop": 0.0,
            "dimensions": {},
            "recommendation": "Накапливаем данные — оценка появится по мере роста истории наблюдений.",
        }

    baseline_p = float(events["is_drop"].mean())
    distinct_days = int(events["date"].nunique())
    distinct_weeks = int(events["isoweek"].nunique())
    distinct_months = int(events["yearmonth"].nunique())
    distinct_years = int(events["year"].nunique())

    months_present = sorted(events["month"].unique().tolist())

    dimensions = {
        "hour": aggregate_dimension(
            events, "hour", list(range(24)),
            {h: f"{h:02d}:00" for h in range(24)},
            "date", CONFIDENCE_THRESHOLDS["hour"], baseline_p,
        ),
        "dow": aggregate_dimension(
            events, "dow", list(range(7)),
            {i: DOW_LABELS[i] for i in range(7)},
            "date", CONFIDENCE_THRESHOLDS["dow"], baseline_p,
        ),
        "part": aggregate_dimension(
            events, "part", PART_ORDER, PART_LABELS,
            "yearmonth", CONFIDENCE_THRESHOLDS["part"], baseline_p,
        ),
        "month": aggregate_dimension(
            events, "month", months_present,
            {m: MONTH_LABELS.get(m, str(m)) for m in months_present},
            "year", CONFIDENCE_THRESHOLDS["month"], baseline_p,
            informational=True,
        ),
    }

    # Кросс-таблица «день недели × час» для теплокарты интенсивности снижения.
    heatmap_intensity = [[None] * 24 for _ in range(7)]
    heatmap_n = [[0] * 24 for _ in range(7)]
    for (dw, hr), sub in events.groupby(["dow", "hour"]):
        n = len(sub)
        if n == 0:
            continue
        d = int(sub["is_drop"].sum())
        p = d / n
        drops = sub[sub["is_drop"]]
        dp = float((-drops["r"]).median()) if len(drops) else 0.0
        heatmap_intensity[int(dw)][int(hr)] = round(p * dp, 5)
        heatmap_n[int(dw)][int(hr)] = int(n)

    recommendation = _build_recommendation(dimensions, distinct_days, baseline_p)

    return {
        "heatmap": {
            "intensity": heatmap_intensity,
            "n": heatmap_n,
            "dow_labels": DOW_LABELS,
            "hours": list(range(24)),
        },
        "available": True,
        "status": "ok",
        "n_events": n_events,
        "history": {
            "days": distinct_days,
            "weeks": distinct_weeks,
            "months": distinct_months,
            "first": events["date"].min(),
            "last": events["date"].max(),
        },
        "baseline_p_drop": round(baseline_p, 4),
        "dimensions": dimensions,
        "recommendation": recommendation,
        "tz": tz,
        "eps": DEFAULT_EPS,
    }


def _build_recommendation(dimensions: dict, distinct_days: int, baseline_p: float) -> str:
    """Строит итоговую рекомендацию только из достоверных измерений."""
    parts = []
    hour = dimensions["hour"]["best"]
    if hour:
        parts.append(f"около {hour['label']} (снижение {hour['p_drop']*100:.0f}% против {baseline_p*100:.0f}% в среднем)")
    dow = dimensions["dow"]["best"]
    if dow:
        parts.append(f"в {dow['label']}")
    part = dimensions["part"]["best"]
    if part:
        parts.append(f"в дни {part['label']} месяца")

    if parts:
        return "Цены чаще снижаются " + ", ".join(parts) + " — статистически значимый сигнал."

    # Достоверных сигналов ещё нет — оцениваем, сколько данных не хватает.
    need_days = CONFIDENCE_THRESHOLDS["hour"]["reliable"]
    remaining = max(0, need_days - distinct_days)
    return (
        f"Накапливаем данные: устойчивых паттернов пока нет. "
        f"Для надёжной оценки по времени суток нужно ещё ~{remaining} дн. наблюдений "
        f"({distinct_days}/{need_days})."
    )


if __name__ == "__main__":
    import argparse
    import csv
    import json

    ap = argparse.ArgumentParser(description="Purchase timing analysis")
    ap.add_argument("--data-file", required=True)
    ap.add_argument("--tz", default="Europe/Warsaw")
    args = ap.parse_args()

    df_in = pd.read_csv(args.data_file, quoting=csv.QUOTE_ALL, on_bad_lines="skip")
    result = analyze_purchase_timing(df_in, tz=args.tz)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
