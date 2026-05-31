"""Shared filter list for landing page and dashboard sidebar."""

FILTER_GROUPS = [
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
            },
            {
                'id': 'egypt_13_16',
                'title': '13–16 дней',
                'subtitle': 'сбор 4–20k PLN • показ ≤10k • WAW/WMI/RDO',
                'href': 'index_filter_13_16_days.html',
                'charts_subdir': 'hotel-charts/filter_13_16_days',
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
            },
            {
                'id': 'turkey_13_16',
                'title': '13–16 дней',
                'subtitle': 'сбор 4–20k PLN • показ ≤10k • WAW/RDO',
                'href': 'index_filter_turkey_13_16_days.html',
                'charts_subdir': 'hotel-charts/filter_turkey_13_16_days',
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
            },
            {
                'id': 'greece_13_16',
                'title': '13–16 дней',
                'subtitle': 'сбор 4–20k PLN • показ ≤10k • любой аэропорт',
                'href': 'index_filter_greece_13_16_days.html',
                'charts_subdir': 'hotel-charts/filter_greece_13_16_days',
            },
        ],
    },
]


def iter_filters():
    for group in FILTER_GROUPS:
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
