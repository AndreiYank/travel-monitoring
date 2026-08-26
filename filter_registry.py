"""Shared filter list for landing page and dashboard sidebar."""

from __future__ import annotations

import copy
from datetime import date
from typing import Any, Dict, List, Optional

from filter_trip import should_skip_monitor_config

FILTER_GROUPS = [
    {
        'id': 'trips',
        'label': 'Поездки',
        'icon': '🎯',
        'filters': [
            {
                'id': 'turkey_vacation_jul18_2026',
                'title': 'Турция • отпуск 18 июля',
                'subtitle': '7–9 / 9–11 дн. • один фильтр, переключатель на странице',
                'href': 'index_filter_turkey_vacation_jul18.html',
                'charts_subdir': 'hotel-charts/filter_turkey_vacation_jul18_2026',
                'config': 'config_ci_filter_turkey_vacation_jul18.json',
            },
            {
                'id': 'egypt_autumn_2026_7_10',
                'title': 'Египет • осень 2026 • 7–10 дн.',
                'subtitle': '10.10–05.11.2026 • WAW/WMI/RDO • 2+1 • показ ≤11k',
                'href': 'index_filter_egypt_autumn_2026_7_10_days.html',
                'charts_subdir': 'hotel-charts/filter_egypt_autumn_2026_7_10_days',
                'config': 'config_ci_filter_egypt_autumn_7_10.json',
            },
            {
                'id': 'egypt_autumn_2026_13_16',
                'title': 'Египет • осень 2026 • 13–16 дн.',
                'subtitle': '10.10–05.11.2026 • WAW/WMI/RDO • 2+1 • показ ≤11k',
                'href': 'index_filter_egypt_autumn_2026_13_16_days.html',
                'charts_subdir': 'hotel-charts/filter_egypt_autumn_2026_13_16_days',
                'config': 'config_ci_filter_egypt_autumn_13_16.json',
            },
            {
                'id': 'egypt_ny_dec24_2026_7_10',
                'title': 'Египет • НГ 24.12–06.01 • 7–10 дн.',
                'subtitle': '24.12.2026–06.01.2027 • WAW/WMI/RDO • 2+1 • показ ≤11k',
                'href': 'index_filter_egypt_ny_dec24_2026_7_10_days.html',
                'charts_subdir': 'hotel-charts/filter_egypt_ny_dec24_2026_7_10_days',
                'config': 'config_ci_filter_egypt_ny_dec24_7_10.json',
            },
            {
                'id': 'egypt_ny_dec28_2026_7_10',
                'title': 'Египет • НГ 28.12–06.01 • 7–10 дн.',
                'subtitle': '28.12.2026–06.01.2027 • WAW/WMI/RDO • 2+1 • показ ≤11k',
                'href': 'index_filter_egypt_ny_dec28_2026_7_10_days.html',
                'charts_subdir': 'hotel-charts/filter_egypt_ny_dec28_2026_7_10_days',
                'config': 'config_ci_filter_egypt_ny_dec28_7_10.json',
            },
        ],
    },
    {
        'id': 'egypt',
        'label': 'Египет',
        'icon': '🇪🇬',
        'filters': [
            {
                'id': 'egypt_7_10',
                'title': '7–10 дней',
                'subtitle': 'сбор 4–20k PLN • показ ≤10k • WAW/WMI/RDO',
                'href': 'index_filter_7_10_days.html',
                'charts_subdir': 'hotel-charts/filter_7_10_days',
                'config': 'config_ci_filter_7_10.json',
            },
            {
                'id': 'egypt_13_16',
                'title': '13–16 дней',
                'subtitle': 'сбор 4–20k PLN • показ ≤10k • WAW/WMI/RDO',
                'href': 'index_filter_13_16_days.html',
                'charts_subdir': 'hotel-charts/filter_13_16_days',
                'config': 'config_ci_filter_13_16.json',
            },
        ],
    },
    {
        'id': 'turkey',
        'label': 'Турция',
        'icon': '🇹🇷',
        'filters': [
            {
                'id': 'turkey_7_10',
                'title': '7–10 дней',
                'subtitle': 'сбор 4–20k PLN • показ ≤10k • WAW/RDO',
                'href': 'index_filter_turkey_7_10_days.html',
                'charts_subdir': 'hotel-charts/filter_turkey_7_10_days',
                'config': 'config_ci_filter_turkey_7_10.json',
            },
            {
                'id': 'turkey_9_11',
                'title': '9–11 дней',
                'subtitle': 'сбор 4.6–20k PLN • показ ≤10k • WAW/WMI/RDO • с питанием',
                'href': 'index_filter_turkey_9_11_days.html',
                'charts_subdir': 'hotel-charts/filter_turkey_9_11_days',
                'config': 'config_ci_filter_turkey_9_11.json',
            },
            {
                'id': 'turkey_13_16',
                'title': '13–16 дней',
                'subtitle': 'сбор 4–20k PLN • показ ≤10k • WAW/RDO',
                'href': 'index_filter_turkey_13_16_days.html',
                'charts_subdir': 'hotel-charts/filter_turkey_13_16_days',
                'config': 'config_ci_filter_turkey_13_16.json',
            },
        ],
    },
    {
        'id': 'greece',
        'label': 'Греция',
        'icon': '🇬🇷',
        'filters': [
            {
                'id': 'greece_7_10',
                'title': '7–10 дней',
                'subtitle': 'сбор 4–20k PLN • показ ≤10k • любой аэропорт',
                'href': 'index_filter_greece_7_10_days.html',
                'charts_subdir': 'hotel-charts/filter_greece_7_10_days',
                'config': 'config_ci_filter_greece_7_10.json',
            },
            {
                'id': 'greece_13_16',
                'title': '13–16 дней',
                'subtitle': 'сбор 4–20k PLN • показ ≤10k • любой аэропорт',
                'href': 'index_filter_greece_13_16_days.html',
                'charts_subdir': 'hotel-charts/filter_greece_13_16_days',
                'config': 'config_ci_filter_greece_13_16.json',
            },
        ],
    },
]


def is_filter_active(flt: Dict[str, Any], *, today: Optional[date] = None) -> bool:
    """Hide filters whose monitor config is retired / trip window expired."""
    config_path = str(flt.get('config') or '').strip()
    if not config_path:
        return True
    return not should_skip_monitor_config(config_path, today=today)


def active_filter_groups(*, today: Optional[date] = None) -> List[Dict[str, Any]]:
    """FILTER_GROUPS without retired / expired trip filters (for landing + sidebar)."""
    groups: List[Dict[str, Any]] = []
    for group in FILTER_GROUPS:
        active = [flt for flt in group['filters'] if is_filter_active(flt, today=today)]
        if not active:
            continue
        item = copy.deepcopy(group)
        item['filters'] = copy.deepcopy(active)
        groups.append(item)
    return groups


def iter_filters(groups: Optional[List[Dict[str, Any]]] = None):
    source = groups if groups is not None else FILTER_GROUPS
    for group in source:
        for flt in group['filters']:
            yield group, flt


def filter_href_by_charts_subdir(charts_subdir: str) -> str:
    sub = (charts_subdir or '').rstrip('/')
    for _, flt in iter_filters():
        if flt['charts_subdir'].rstrip('/') == sub:
            return flt['href']
    return 'index.html'


def active_filter_id(charts_subdir: str) -> str:
    sub = (charts_subdir or '').rstrip('/')
    for _, flt in iter_filters():
        if flt['charts_subdir'].rstrip('/') == sub:
            return flt['id']
    return ''
