#!/usr/bin/env python3
"""
Дашборд с встроенными графиками вместо модальных окон
"""

import pandas as pd
import json
from datetime import datetime, timedelta, timezone
import os
import re

def generate_inline_charts_dashboard(data_file: str = 'data/travel_prices.csv', output_file: str = 'index.html', title: str = 'Travel Price Monitor • Расширенный дашборд', charts_subdir: str = 'hotel-charts', tz: str = 'Europe/Warsaw'):
    """Генерирует дашборд с встроенными графиками"""
    
    # Загружаем данные
    try:
        df = pd.read_csv(data_file)
        # Нормализуем время: аккуратно обрабатываем смешанные строки (с/без таймзоны)
        raw = df['scraped_at'].astype(str)
        mask_tz = raw.str.contains(r"Z$|[+-]\d{2}:\d{2}$", regex=True)
        tz_series = pd.to_datetime(raw.where(mask_tz), errors='coerce', utc=True)
        tz_series = tz_series.dt.tz_convert(tz)
        naive_series = pd.to_datetime(raw.where(~mask_tz), errors='coerce')
        try:
            naive_series = naive_series.dt.tz_localize(tz)
        except Exception:
            # Если часть уже осознанно tz-aware/NaT — оставим как есть
            pass
        df['scraped_at_local'] = tz_series.combine_first(naive_series)
        # Убираем строки с некорректной датой
        df = df.dropna(subset=['scraped_at_local'])
        # Используем локализованное время без дополнительных сдвигов
        df['scraped_at_display'] = df['scraped_at_local']
        print(f"✅ Загружено {len(df)} записей")
    except Exception as e:
        print(f"❌ Ошибка загрузки данных: {e}")
        return
    
    # Вычисляем статистику
    total_offers = len(df)
    unique_hotels = df['hotel_name'].nunique()
    avg_price = df['price'].mean()
    min_price = df['price'].min()
    max_price = df['price'].max()

    # Средняя цена ТОП-10 дешёвых предложений по часам
    try:
        hourly = df.set_index('scraped_at_display').sort_index()
        top10_avg = hourly['price'].groupby(pd.Grouper(freq='H')).apply(
            lambda s: float(s.nsmallest(10).mean()) if len(s) else None
        ).dropna()
        top10_x_values = [ts.strftime('%Y-%m-%d %H:%M') for ts in top10_avg.index.to_pydatetime().tolist()]
        top10_y_values = [float(v) for v in top10_avg.values.tolist()]
    except Exception:
        top10_x_values, top10_y_values = [], []
    
    # Получаем актуальные цены по каждому отелю (последнее наблюдение)
    df_sorted_all = df.sort_values(['hotel_name', 'scraped_at_display'])
    latest_rows = []
    for hotel_name, grp in df_sorted_all.groupby('hotel_name'):
        last = grp.iloc[-1]
        latest_rows.append({
            'hotel_name': hotel_name,
            'price': float(last['price']),
            'dates': last.get('dates', None),
            'duration': last.get('duration', None),
            'scraped_at_local': last['scraped_at_local']
        })
    all_hotels = pd.DataFrame(latest_rows).sort_values('price').reset_index(drop=True)
    
    # Анализ изменений за разные окна времени
    df_sorted = df.sort_values(['hotel_name', 'scraped_at_display'])

    def compute_changes(window_hours: int):
        cutoff = (df['scraped_at_display'].max() or datetime.now()) - timedelta(hours=window_hours)
        changes = []
        deltas_map = {}
        for hotel_name, grp in df_sorted.groupby('hotel_name'):
            grp = grp.sort_values('scraped_at_display')
            latest_row = grp.iloc[-1]
            latest_time = latest_row['scraped_at_display']
            win = grp[grp['scraped_at_display'] >= cutoff]
            if len(win) >= 2:
                baseline_row = win.iloc[0]
            elif len(grp) >= 2:
                baseline_row = grp.iloc[-2]
            else:
                deltas_map[hotel_name] = None
                continue
            latest_price = float(latest_row['price'])
            baseline_price = float(baseline_row['price'])
            if baseline_price == 0:
                deltas_map[hotel_name] = None
                continue
            change = latest_price - baseline_price
            if change == 0:
                deltas_map[hotel_name] = None
                continue
            change_percent = (change / baseline_price) * 100.0
            changes.append({
                'hotel_name': hotel_name,
                'old_price': baseline_price,
                'new_price': latest_price,
                'change': change,
                'change_percent': change_percent,
                'timestamp': str(latest_time)
            })
            deltas_map[hotel_name] = (change, change_percent)
        decreases = sorted([h for h in changes if h['change'] < 0], key=lambda x: x['change'])[:5]
        increases = sorted([h for h in changes if h['change'] > 0], key=lambda x: x['change'], reverse=True)[:5]
        return decreases, increases, deltas_map

    # Для таблицы оставляем 48ч, для блоков добавим 24ч и 7д
    decreases_48h, increases_48h, deltas_by_hotel = compute_changes(48)
    decreases_24h, increases_24h, _ = compute_changes(24)
    decreases_7d, increases_7d, _ = compute_changes(24 * 7)

    # Метки нового минимума/максимума за 7д и 30д
    ref_time = df['scraped_at_display'].max() or datetime.now()
    minmax_labels_by_hotel = {}
    for hotel_name, grp in df_sorted_all.groupby('hotel_name'):
        grp = grp.sort_values('scraped_at_display')
        latest_price = float(grp.iloc[-1]['price'])
        labels = []
        for days in (7, 30):
            cutoff_d = ref_time - timedelta(days=days)
            window = grp[grp['scraped_at_local'] >= cutoff_d]
            if len(window) == 0:
                continue
            win_min = float(window['price'].min())
            win_max = float(window['price'].max())
            if latest_price <= win_min:
                labels.append(f"Новый минимум {days}д")
            if latest_price >= win_max:
                labels.append(f"Новый максимум {days}д")
        minmax_labels_by_hotel[hotel_name] = labels

    # Изменение с начала наблюдений (первое значение -> последнее)
    since_start_delta = {}
    for hotel_name, grp in df_sorted.groupby('hotel_name'):
        grp = grp.sort_values('scraped_at_display')
        first_price = float(grp.iloc[0]['price'])
        last_price = float(grp.iloc[-1]['price'])
        if first_price == 0:
            since_start_delta[hotel_name] = None
            continue
        change_abs = last_price - first_price
        change_pct = (change_abs / first_price) * 100.0
        since_start_delta[hotel_name] = (change_abs, change_pct)
    
    # Загружаем историю алертов (если есть)
    alerts = []
    alerts_path = os.path.join('data', 'price_alerts_history.json')
    if os.path.exists(alerts_path):
        try:
            with open(alerts_path, 'r', encoding='utf-8') as f:
                alerts_data = json.load(f)
                # Поддерживаем как старый формат {"alerts": [...]}, так и новый формат [...]
                if isinstance(alerts_data, dict) and 'alerts' in alerts_data:
                    alerts = alerts_data.get('alerts', [])
                elif isinstance(alerts_data, list):
                    alerts = alerts_data
                else:
                    alerts = []
        except Exception:
            alerts = []

    # Сортируем алерты по времени (новые сверху)
    def parse_iso(ts):
        try:
            dt = datetime.fromisoformat(ts)
            # Если datetime naive, делаем его UTC
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            return datetime.min.replace(tzinfo=timezone.utc)

    # Сортируем по времени создания (created_at) если есть, иначе по timestamp
    alerts.sort(key=lambda a: parse_iso(a.get('created_at') or a.get('timestamp') or a.get('time') or ''), reverse=True)

    # Загружаем карту изображений (если есть)
    images_map = {}
    images_path = os.path.join('data', 'hotel_images.json')
    if os.path.exists(images_path):
        try:
            with open(images_path, 'r', encoding='utf-8') as f:
                images_map = json.load(f) or {}
        except Exception:
            images_map = {}

    # Функция для слуг-имени файла по названию отеля
    def slugify(text: str) -> str:
        text = text.lower().strip()
        text = re.sub(r"[^a-z0-9]+", "-", text)
        text = re.sub(r"-+", "-", text).strip('-')
        return text or "hotel"

    # Создаём директорию для страниц графиков
    charts_dir = os.path.join(charts_subdir)
    os.makedirs(charts_dir, exist_ok=True)

    # Генерируем страницу с графиком для каждого отеля
    for hotel_name in sorted(df['hotel_name'].unique()):
        hotel_ts = df[df['hotel_name'] == hotel_name].dropna(subset=['scraped_at_display']).sort_values('scraped_at_display')
        x_values = [pd.to_datetime(t).strftime('%Y-%m-%d %H:%M') for t in hotel_ts['scraped_at_display'].tolist()]
        y_values = [float(p) for p in hotel_ts['price'].tolist()]

        hotel_slug = slugify(hotel_name)
        hotel_html_path = os.path.join(charts_dir, f"{hotel_slug}.html")

        # Определяем корректную ссылку "Назад к дашборду" в зависимости от поддиректории
        if charts_subdir and charts_subdir.rstrip('/').endswith('greece'):
            back_target = 'index_greece.html'
        elif charts_subdir and charts_subdir.rstrip('/').endswith('egypt'):
            back_target = 'index_egypt.html'
        else:
            back_target = 'index.html'
        back_href = os.path.relpath(back_target, start=os.path.dirname(hotel_html_path))

        chart_html = f"""<!DOCTYPE html>
<html lang=\"ru\">
<head>
    <meta charset=\"UTF-8\">
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">
    <title>График цен — {hotel_name}</title>
    <script src=\"https://cdn.plot.ly/plotly-latest.min.js\"></script>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 20px; }}
        .back {{ margin-bottom: 10px; }}
        #chart {{ height: 520px; }}
    </style>
<head>
<body>
    <div class=\"back\"><a href=\"{back_href}\">← Назад к дашборду</a></div>
    <h2>График цен: {hotel_name}</h2>
    <div id=\"chart\"></div>
    <script>
      const x = {json.dumps(x_values, ensure_ascii=False)};
      const y = {json.dumps(y_values, ensure_ascii=False)};
      const trace = {{
        x: x,
        y: y,
        type: 'scatter',
        mode: 'lines+markers',
        line: {{ color: '#2E86AB', width: 3 }},
        marker: {{ size: 8 }}
      }};
      const layout = {{
        title: 'История цен',
        xaxis: {{ title: 'Время' }},
        yaxis: {{ title: 'Цена (PLN)' }}
      }};
      Plotly.newPlot('chart', [trace], layout);
    </script>
  </body>
</html>"""

        with open(hotel_html_path, 'w', encoding='utf-8') as f:
            f.write(chart_html)

    # HTML шаблон
    # Готовим HTML блок изменений, выводим только если есть хотя бы один список
    changes_html = ""
    if decreases_24h or increases_24h:
        changes_html += """
        <div class=\"changes-section\">"""
        if decreases_24h:
            changes_html += """
            <div class=\"changes-block\">
                <h3>📉 Наиболее подешевевшие (24ч)</h3>"""
            for change in decreases_24h:
                changes_html += f"""
                <div class=\"change-item change-decrease\">
                    <div>
                        <div class=\"hotel-name\">{change['hotel_name']}</div>
                        <div class=\"change-percent\">{change['change']:+.0f} PLN ({change['change_percent']:+.1f}%)</div>
                    </div>
                    <div class=\"change-price\">{change['old_price']:.0f} → {change['new_price']:.0f} PLN</div>
                </div>"""
            changes_html += """
            </div>"""
        if increases_24h:
            changes_html += """
            <div class=\"changes-block\">
                <h3>📈 Наиболее подорожавшие (24ч)</h3>"""
            for change in increases_24h:
                changes_html += f"""
                <div class=\"change-item change-increase\">
                    <div>
                        <div class=\"hotel-name\">{change['hotel_name']}</div>
                        <div class=\"change-percent\">{change['change']:+.0f} PLN ({change['change_percent']:+.1f}%)</div>
                    </div>
                    <div class=\"change-price\">{change['old_price']:.0f} → {change['new_price']:.0f} PLN</div>
                </div>"""
            changes_html += """
            </div>"""
        changes_html += """
        </div>"""

    if decreases_7d or increases_7d:
        changes_html += """
        <div class=\"changes-section\">"""
        if decreases_7d:
            changes_html += """
            <div class=\"changes-block\">
                <h3>📉 Наиболее подешевевшие (7д)</h3>"""
            for change in decreases_7d:
                changes_html += f"""
                <div class=\"change-item change-decrease\">
                    <div>
                        <div class=\"hotel-name\">{change['hotel_name']}</div>
                        <div class=\"change-percent\">{change['change']:+.0f} PLN ({change['change_percent']:+.1f}%)</div>
                    </div>
                    <div class=\"change-price\">{change['old_price']:.0f} → {change['new_price']:.0f} PLN</div>
                </div>"""
            changes_html += """
            </div>"""
        if increases_7d:
            changes_html += """
            <div class=\"changes-block\">
                <h3>📈 Наиболее подорожавшие (7д)</h3>"""
            for change in increases_7d:
                changes_html += f"""
                <div class=\"change-item change-increase\">
                    <div>
                        <div class=\"hotel-name\">{change['hotel_name']}</div>
                        <div class=\"change-percent\">{change['change']:+.0f} PLN ({change['change_percent']:+.1f}%)</div>
                    </div>
                    <div class=\"change-price\">{change['old_price']:.0f} → {change['new_price']:.0f} PLN</div>
                </div>"""
            changes_html += """
            </div>"""
        changes_html += """
        </div>"""

    # Время последнего обновления для шапки
    try:
        updated_str = df['scraped_at_display'].max().strftime('%d.%m.%Y %H:%M')
    except Exception:
        updated_str = datetime.now().strftime('%d.%m.%Y %H:%M')

    html_template = f"""<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <script src="https://cdn.plot.ly/plotly-latest.min.js"></script>
    <title>{title}</title>
    <style>
        body {{
            font-family: Arial, sans-serif;
            margin: 20px;
            background: #f5f5f5;
        }}
        .container {{
            max-width: 1200px;
            margin: 0 auto;
            background: white;
            padding: 20px;
            border-radius: 10px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
        }}
        .header {{
            text-align: center;
            margin-bottom: 30px;
            padding: 20px;
            background: linear-gradient(135deg, #2E86AB, #A23B72);
            color: white;
            border-radius: 10px;
        }}
        .metrics {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
        }}
        .avg-top10-section {{
            background: #f8f9fa;
            padding: 16px;
            border-radius: 8px;
            margin-bottom: 24px;
        }}
        .metric {{
            background: #f8f9fa;
            padding: 20px;
            border-radius: 8px;
            text-align: center;
        }}
        .metric-value {{
            font-size: 2em;
            font-weight: bold;
            color: #2E86AB;
        }}
        .changes-section {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 20px;
            margin-bottom: 30px;
        }}
        .changes-block {{
            background: #f8f9fa;
            padding: 20px;
            border-radius: 8px;
        }}
        .changes-block h3 {{
            margin-top: 0;
            text-align: center;
        }}
        .change-item {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 10px;
            margin: 5px 0;
            background: white;
            border-radius: 5px;
            border-left: 4px solid;
        }}
        .change-decrease {{
            border-left-color: #28a745;
        }}
        .change-increase {{
            border-left-color: #dc3545;
        }}
        .change-price {{
            font-weight: bold;
        }}
        .change-percent {{
            font-size: 0.9em;
            opacity: 0.8;
        }}
        .alerts-section {{
            margin-top: 30px;
        }}
        .alert-item {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 10px;
            margin: 5px 0;
            background: white;
            border-radius: 5px;
            border-left: 4px solid;
        }}
        .alert-decrease {{
            border-left-color: #28a745;
        }}
        .alert-increase {{
            border-left-color: #dc3545;
        }}
        .alert-missing {{
            border-left-color: #6c757d;
        }}
        .alerts-empty {{
            color: #6c757d;
            font-style: italic;
        }}
        .alerts-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            cursor: pointer;
            user-select: none;
        }}
        .alerts-header:hover {{
            background: #f8f9fa;
        }}
        .alerts-content {{
            max-height: 400px;
            overflow-y: auto;
            transition: max-height 0.3s ease;
        }}
        .alerts-content.collapsed {{
            max-height: 0;
            overflow: hidden;
        }}
        .expand-icon {{
            transition: transform 0.3s ease;
        }}
        .expand-icon.collapsed {{
            transform: rotate(-90deg);
        }}
        .delta {{ font-weight: bold; }}
        .delta.up {{ color: #dc3545; }}
        .delta.down {{ color: #28a745; }}
        .delta.flat {{ color: #6c757d; }}
        .hotels-section {{
            margin-top: 30px;
        }}
        .hotels-table {{
            width: 100%;
            border-collapse: collapse;
            margin-top: 20px;
        }}
        .hotels-table th, .hotels-table td {{
            padding: 12px;
            text-align: left;
            border-bottom: 1px solid #ddd;
        }}
        .hotels-table th {{
            background: #f8f9fa;
            font-weight: bold;
            cursor: pointer;
            user-select: none;
            position: relative;
        }}
        .hotels-table th:hover {{
            background: #e9ecef;
        }}
        .hotels-table th.sortable::after {{
            content: ' ↕';
            opacity: 0.5;
        }}
        .hotels-table th.sort-asc::after {{
            content: ' ↑';
            opacity: 1;
        }}
        .hotels-table th.sort-desc::after {{
            content: ' ↓';
            opacity: 1;
        }}
        .hotels-table tr:hover {{
            background: #f5f5f5;
            cursor: pointer;
        }}
        .hotel-name {{
            color: #2E86AB;
            font-weight: bold;
        }}
        .price {{
            font-weight: bold;
            color: #28a745;
        }}
        .open-chart-link {{ color: #2E86AB; text-decoration: underline; }}
        .footer {{
            text-align: center;
            margin-top: 30px;
            padding: 20px;
            background: #f8f9fa;
            border-radius: 8px;
        }}
        /* Hover preview */
        .hover-thumb {{ position: absolute; display: none; width: 240px; height: 160px; background: #fff; border: 1px solid #ddd; box-shadow: 0 2px 8px rgba(0,0,0,.15); border-radius: 6px; padding: 4px; z-index: 9999; }}
        .hover-thumb img {{ width: 100%; height: 100%; object-fit: cover; border-radius: 4px; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🏨 {title}</h1>
            <p>Обновлено: {updated_str}</p>
        </div>

        <div class="avg-top10-section">
            <h3>📉 Средняя цена ТОП‑10 дешёвых предложений</h3>
            <div id="avgTop10" style="height:360px;"></div>
        </div>
        
        <div class="metrics">
            <div class="metric">
                <div class="metric-value">{total_offers:,}</div>
                <div>Всего предложений</div>
            </div>
            <div class="metric">
                <div class="metric-value">{unique_hotels}</div>
                <div>Уникальных отелей</div>
            </div>
            <div class="metric">
                <div class="metric-value">{avg_price:.0f} PLN</div>
                <div>Средняя цена</div>
            </div>
            <div class="metric">
                <div class="metric-value">{min_price:.0f} PLN</div>
                <div>Минимальная цена</div>
            </div>
            <div class="metric">
                <div class="metric-value">{max_price:.0f} PLN</div>
                <div>Максимальная цена</div>
            </div>
        </div>
        
        {changes_html}
        
        <div class="alerts-section">
            <div class="alerts-header" onclick="toggleAlerts()">
                <h3>🚨 История алертов</h3>
                <span class="expand-icon" id="alertsExpandIcon">▼</span>
            </div>
            <div class="alerts-content" id="alertsContent">
"""

    if alerts:
        for a in alerts:
            hotel_name = a.get('hotel_name') or a.get('hotel') or 'Unknown'
            alert_type = a.get('alert_type') or a.get('type') or ''
            old_price = a.get('old_price') or a.get('from') or a.get('previous_price')
            new_price = a.get('new_price') if 'new_price' in a else (a.get('to') or a.get('current_price'))
            ts = a.get('timestamp') or a.get('time') or ''

            if alert_type == 'missing' or new_price in (None, '', 'null'):
                direction_class = 'alert-missing'
                arrow = '—'
                change_text = a.get('message') or a.get('note') or 'Отель пропал из выдачи'
                price_text = f"{old_price if old_price is not None else '—'} → —"
                html_template += f"""
                <div class="alert-item {direction_class}">
                    <div>
                        <div class="hotel-name">{hotel_name}</div>
                        <div class="change-percent">{arrow} {change_text} • {ts}</div>
                    </div>
                    <div class="change-price">{price_text}</div>
                </div>
"""
            else:
                # Обычный ценовой алерт (новая структура)
                change_pct = a.get('price_change_pct', 0.0)
                price_change = a.get('price_change', 0.0)
                direction_class = 'alert-increase' if price_change > 0 else ('alert-decrease' if price_change < 0 else '')
                arrow = '↑' if price_change > 0 else ('↓' if price_change < 0 else '→')
                html_template += f"""
                <div class="alert-item {direction_class}">
                    <div>
                        <div class="hotel-name">{hotel_name}</div>
                        <div class="change-percent">{arrow} {change_pct:+.1f}% • {ts}</div>
                    </div>
                    <div class="change-price">{old_price} → {new_price} PLN</div>
                </div>
"""
    else:
        html_template += """
                <div class="alerts-empty">Нет алертов</div>
"""

    html_template += f"""
            </div>
        </div>

        <div class="hotels-section">
            <h3>🏨 Все отели • клик по отелю откроет график на отдельной странице</h3>
            <table class="hotels-table" id="hotelsTable">
                <thead>
                    <tr>
                        <th class="sortable" data-sort="hotel">Отель</th>
                        <th class="sortable" data-sort="price">Цена</th>
                        <th class="sortable" data-sort="delta48">Δ 48ч</th>
                        <th class="sortable" data-sort="deltastart">Δ с начала</th>
                        <th class="sortable" data-sort="dates">Даты</th>
                        <th class="sortable" data-sort="duration">Длительность</th>
                    </tr>
                </thead>
                <tbody>"""

    # Добавляем строки таблицы
    for i, (_, hotel) in enumerate(all_hotels.iterrows()):
        hotel_name = hotel['hotel_name']
        price = hotel['price']
        dates = hotel['dates'] if pd.notna(hotel['dates']) else '20-09-2025 - 04-10-2025'
        duration = hotel['duration'] if pd.notna(hotel['duration']) else '6-15 дней'
        
        # Δ 48ч
        delta_display = "—"
        delta_class = "delta flat"
        delta_info = deltas_by_hotel.get(hotel_name)
        if delta_info is not None:
            delta_abs, delta_pct = delta_info
            arrow = '↑' if delta_abs > 0 else ('↓' if delta_abs < 0 else '→')
            delta_class = 'delta up' if delta_abs > 0 else ('delta down' if delta_abs < 0 else 'delta flat')
            sign = '+' if delta_abs > 0 else ('' if delta_abs < 0 else '')
            delta_display = f"{arrow} {sign}{delta_pct:.1f}%"

        # Δ с начала наблюдений
        since_display = "—"
        since_info = since_start_delta.get(hotel_name)
        if since_info is not None:
            since_abs, since_pct = since_info
            arrow2 = '↑' if since_abs > 0 else ('↓' if since_abs < 0 else '→')
            sign2 = '+' if since_abs > 0 else ('' if since_abs < 0 else '')
            since_display = f"{arrow2} {sign2}{since_pct:.1f}%"

        hotel_slug = slugify(hotel_name)
        # Строим ссылку на страницу графика, учитывая поддиректорию
        if charts_subdir:
            chart_href = f"{charts_subdir.rstrip('/')}/{hotel_slug}.html"
        else:
            chart_href = f"hotel-charts/{hotel_slug}.html"
        html_template += f"""
                    <tr>
                        <td class="hotel-name"><a class=\"open-chart-link\" href=\"{chart_href}\" target=\"_blank\" onmouseover=\"_hoverPreview.show(event,'{hotel_name}')\" onmouseout=\"_hoverPreview.hide()\">{hotel_name}</a></td>
                        <td class="price" data-sort-value="{price}">{price:.0f} PLN</td>
                        <td class=\"{delta_class}\" data-sort-value="{delta_info[1] if delta_info else 0}">{delta_display}</td>
                        <td data-sort-value="{since_info[1] if since_info else 0}">{since_display}</td>
                        <td data-sort-value="{dates}">{dates}</td>
                        <td data-sort-value="{duration}">{duration}</td>
                    </tr>"""

    # Завершаем таблицу и добавляем секцию для графика
    html_template += f"""
                </tbody>
            </table>
        </div>
        
        <div class="footer">
            <p>🤖 Автоматически обновляется каждый час • Powered by GitHub Actions</p>
        </div>
    </div>
    <div id="hoverThumb" class="hover-thumb"><img id="hoverImg" src="" alt="preview"/></div>
"""

    # Вставляем скрипт превью слиянием JSON вне f-строки, чтобы избежать конфликтов с фигурными скобками
    html_template += """
    <script>
      (function(){
        const X = """ + json.dumps(top10_x_values, ensure_ascii=False) + """;
        const Y = """ + json.dumps(top10_y_values, ensure_ascii=False) + """;
        if (Array.isArray(X) && Array.isArray(Y) && X.length > 0 && Y.length > 0 && window.Plotly) {
          const trace = { x: X, y: Y, type: 'scatter', mode: 'lines+markers', line: { color: '#A23B72', width: 3 }, marker: { size: 6 } };
          const layout = { margin: { t: 10, r: 10, b: 40, l: 50 }, xaxis: { title: 'Время' }, yaxis: { title: 'Цена (PLN)' } };
          Plotly.newPlot('avgTop10', [trace], layout);
        }
      })();
      (function(){
        const map = """ + json.dumps(images_map, ensure_ascii=False) + """;
        try { Object.assign(map, JSON.parse(localStorage.getItem('hotel_images')||'{}')); } catch(e) {}
        const hover = document.getElementById('hoverThumb');
        const img = document.getElementById('hoverImg');
        function show(e, name){ const url = map[name]; if(!url){ return; } img.src = url; hover.style.display = 'block'; hover.style.left = ((e.pageX||0)+12) + 'px'; hover.style.top = ((e.pageY||0)+12) + 'px'; }
        function move(e){ if(hover.style.display === 'block'){ hover.style.left = ((e.pageX||0)+12) + 'px'; hover.style.top = ((e.pageY||0)+12) + 'px'; } }
        function hide(){ hover.style.display = 'none'; img.src = ''; }
        document.addEventListener('mousemove', move);
        window._hoverPreview = { show, hide };
      })();
      function toggleAlerts() {
        const content = document.getElementById('alertsContent');
        const icon = document.getElementById('alertsExpandIcon');
        if (content.classList.contains('collapsed')) {
          content.classList.remove('collapsed');
          icon.classList.remove('collapsed');
        } else {
          content.classList.add('collapsed');
          icon.classList.add('collapsed');
        }
      }
      
      // Таблица сортировки
      let currentSort = { column: null, direction: 'asc' };
      
      function sortTable(column) {
        const table = document.getElementById('hotelsTable');
        const tbody = table.querySelector('tbody');
        const rows = Array.from(tbody.querySelectorAll('tr'));
        
        // Определяем направление сортировки
        if (currentSort.column === column) {
          currentSort.direction = currentSort.direction === 'asc' ? 'desc' : 'asc';
        } else {
          currentSort.direction = 'asc';
        }
        currentSort.column = column;
        
        // Сортируем строки
        rows.sort((a, b) => {
          let aVal, bVal;
          
          if (column === 'hotel') {
            aVal = a.cells[0].textContent.trim();
            bVal = b.cells[0].textContent.trim();
            return currentSort.direction === 'asc' ? aVal.localeCompare(bVal) : bVal.localeCompare(aVal);
          } else {
            aVal = parseFloat(a.cells[getColumnIndex(column)].dataset.sortValue) || 0;
            bVal = parseFloat(b.cells[getColumnIndex(column)].dataset.sortValue) || 0;
            return currentSort.direction === 'asc' ? aVal - bVal : bVal - aVal;
          }
        });
        
        // Обновляем таблицу
        rows.forEach(row => tbody.appendChild(row));
        
        // Обновляем индикаторы сортировки
        updateSortIndicators();
      }
      
      function getColumnIndex(column) {
        const columnMap = { 'hotel': 0, 'price': 1, 'delta48': 2, 'deltastart': 3, 'dates': 4, 'duration': 5 };
        return columnMap[column];
      }
      
      function updateSortIndicators() {
        const headers = document.querySelectorAll('#hotelsTable th.sortable');
        headers.forEach(header => {
          header.classList.remove('sort-asc', 'sort-desc');
          if (header.dataset.sort === currentSort.column) {
            header.classList.add(currentSort.direction === 'asc' ? 'sort-asc' : 'sort-desc');
          }
        });
      }
      
      // Добавляем обработчики кликов на заголовки
      document.addEventListener('DOMContentLoaded', function() {
        const headers = document.querySelectorAll('#hotelsTable th.sortable');
        headers.forEach(header => {
          header.addEventListener('click', () => sortTable(header.dataset.sort));
        });
      });
    </script>
  </body>
</html>
"""

    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(html_template)
    
    print(f"✅ Дашборд с встроенными графиками сгенерирован: index.html")
    print(f"📊 Статистика: {total_offers} предложений, {unique_hotels} отелей")
    print(f"💰 Цены: {min_price:.0f} - {max_price:.0f} PLN (средняя: {avg_price:.0f} PLN)")
    print(f"📈 Изменения цен: {len(decreases_48h) + len(increases_48h)} отелей за 48ч")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description='Generate inline charts dashboard')
    parser.add_argument('--data-file', default='data/travel_prices.csv')
    parser.add_argument('--output', default='index.html')
    parser.add_argument('--title', default='Travel Price Monitor • Расширенный дашборд')
    parser.add_argument('--charts-dir', default='hotel-charts')
    parser.add_argument('--tz', default='Europe/Warsaw')
    args = parser.parse_args()
    generate_inline_charts_dashboard(data_file=args.data_file, output_file=args.output, title=args.title, charts_subdir=args.charts_dir, tz=args.tz)
