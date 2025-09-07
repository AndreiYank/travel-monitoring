#!/usr/bin/env python3
"""
Дашборд с встроенными графиками вместо модальных окон
"""

import pandas as pd
import json
from datetime import datetime, timedelta
import os
import re

def generate_inline_charts_dashboard():
    """Генерирует дашборд с встроенными графиками"""
    
    # Загружаем данные
    try:
        df = pd.read_csv('data/travel_prices.csv')
        df['scraped_at'] = pd.to_datetime(df['scraped_at'])
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
    
    # Получаем актуальные цены по каждому отелю (последнее наблюдение)
    df_sorted_all = df.sort_values(['hotel_name', 'scraped_at'])
    latest_rows = []
    for hotel_name, grp in df_sorted_all.groupby('hotel_name'):
        last = grp.iloc[-1]
        latest_rows.append({
            'hotel_name': hotel_name,
            'price': float(last['price']),
            'dates': last.get('dates', None),
            'duration': last.get('duration', None),
            'scraped_at': last['scraped_at']
        })
    all_hotels = pd.DataFrame(latest_rows).sort_values('price').reset_index(drop=True)
    
    # Анализируем изменения цен за последние 48 часов
    now = datetime.now()
    cutoff_time = now - timedelta(hours=48)

    hotel_changes: list[dict] = []
    df_sorted = df.sort_values(['hotel_name', 'scraped_at'])
    for hotel_name, grp in df_sorted.groupby('hotel_name'):
        grp = grp.sort_values('scraped_at')
        latest_row = grp.iloc[-1]
        latest_time = latest_row['scraped_at']
        baseline_candidates = grp[grp['scraped_at'] <= cutoff_time]
        if len(baseline_candidates) == 0:
            continue  # недостаточно истории для 48ч сравнения
        baseline_row = baseline_candidates.iloc[-1]
        latest_price = float(latest_row['price'])
        baseline_price = float(baseline_row['price'])
        if baseline_price == 0:
            continue
        change = latest_price - baseline_price
        if change == 0:
            continue  # пропускаем нулевые изменения
        change_percent = (change / baseline_price) * 100.0
        hotel_changes.append({
            'hotel_name': hotel_name,
            'old_price': baseline_price,
            'new_price': latest_price,
            'change': change,
            'change_percent': change_percent,
            'timestamp': str(latest_time)
        })

    # Разделяем на подешевевшие и подорожавшие
    decreases = sorted([h for h in hotel_changes if h['change'] < 0], key=lambda x: x['change'])[:5]
    increases = sorted([h for h in hotel_changes if h['change'] > 0], key=lambda x: x['change'], reverse=True)[:5]
    
    # Загружаем историю алертов (если есть)
    alerts = []
    alerts_path = os.path.join('data', 'price_alerts_history.json')
    if os.path.exists(alerts_path):
        try:
            with open(alerts_path, 'r', encoding='utf-8') as f:
                alerts_data = json.load(f)
                alerts = alerts_data.get('alerts', []) or []
        except Exception:
            alerts = []

    # Сортируем алерты по времени (новые сверху)
    def parse_iso(ts):
        try:
            return datetime.fromisoformat(ts)
        except Exception:
            return datetime.min

    alerts.sort(key=lambda a: parse_iso(a.get('timestamp') or a.get('time') or ''), reverse=True)

    # Функция для слуг-имени файла по названию отеля
    def slugify(text: str) -> str:
        text = text.lower().strip()
        text = re.sub(r"[^a-z0-9]+", "-", text)
        text = re.sub(r"-+", "-", text).strip('-')
        return text or "hotel"

    # Создаём директорию для страниц графиков
    charts_dir = os.path.join('hotel-charts')
    os.makedirs(charts_dir, exist_ok=True)

    # Генерируем страницу с графиком для каждого отеля
    for hotel_name in sorted(df['hotel_name'].unique()):
        hotel_ts = df[df['hotel_name'] == hotel_name].sort_values('scraped_at')
        x_values = [pd.to_datetime(t).strftime('%Y-%m-%d %H:%M') for t in hotel_ts['scraped_at'].tolist()]
        y_values = [float(p) for p in hotel_ts['price'].tolist()]

        hotel_slug = slugify(hotel_name)
        hotel_html_path = os.path.join(charts_dir, f"{hotel_slug}.html")

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
    <div class=\"back\"><a href=\"../inline.html\">← Назад к дашборду</a></div>
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
    if decreases or increases:
        changes_html += """
        <div class=\"changes-section\">"""
        if decreases:
            changes_html += """
            <div class=\"changes-block\">
                <h3>📉 Наиболее подешевевшие (48ч)</h3>"""
            for change in decreases:
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
        if increases:
            changes_html += """
            <div class=\"changes-block\">
                <h3>📈 Наиболее подорожавшие (48ч)</h3>"""
            for change in increases:
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

    html_template = f"""<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Travel Price Monitor • Расширенный дашборд</title>
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
        .alerts-empty {{
            color: #6c757d;
            font-style: italic;
        }}
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
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🏨 Travel Price Monitor</h1>
            <p>Мониторинг цен на путешествия в Грецию • Обновлено: {datetime.now().strftime('%d.%m.%Y %H:%M')}</p>
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
            <h3>🚨 История алертов</h3>
            <div>
"""

    if alerts:
        for a in alerts:
            hotel_name = a.get('hotel_name') or a.get('hotel') or 'Unknown'
            old_price = a.get('old_price') or a.get('from') or a.get('previous_price') or 0
            new_price = a.get('new_price') or a.get('to') or a.get('current_price') or 0
            change_pct = 0.0
            try:
                if float(old_price) != 0:
                    change_pct = (float(new_price) - float(old_price)) / float(old_price) * 100.0
            except Exception:
                change_pct = 0.0
            ts = a.get('timestamp') or a.get('time') or ''
            direction_class = 'alert-increase' if (float(new_price) - float(old_price)) > 0 else ('alert-decrease' if (float(new_price) - float(old_price)) < 0 else '')
            arrow = '↑' if (float(new_price) - float(old_price)) > 0 else ('↓' if (float(new_price) - float(old_price)) < 0 else '→')

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
            <h3>🏨 Все отели (отсортированы по цене) • график откроется на отдельной странице</h3>
            <table class="hotels-table">
                <thead>
                    <tr>
                        <th>Отель</th>
                        <th>Цена</th>
                        <th>График</th>
                        <th>Даты</th>
                        <th>Длительность</th>
                    </tr>
                </thead>
                <tbody>"""

    # Добавляем строки таблицы
    for i, (_, hotel) in enumerate(all_hotels.iterrows()):
        hotel_name = hotel['hotel_name']
        price = hotel['price']
        dates = hotel['dates'] if pd.notna(hotel['dates']) else '20-09-2025 - 04-10-2025'
        duration = hotel['duration'] if pd.notna(hotel['duration']) else '6-15 дней'
        
        hotel_slug = slugify(hotel_name)
        chart_href = f"hotel-charts/{hotel_slug}.html"
        html_template += f"""
                    <tr>
                        <td class="hotel-name"><a class=\"open-chart-link\" href=\"{chart_href}\" target=\"_blank\">{hotel_name}</a></td>
                        <td class="price">{price:.0f} PLN</td>
                        <td><a class=\"open-chart-link\" href=\"{chart_href}\" target=\"_blank\">Открыть</a></td>
                        <td>{dates}</td>
                        <td>{duration}</td>
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
</body>
</html>"""

    # Сохраняем файл
    with open('index.html', 'w', encoding='utf-8') as f:
        f.write(html_template)
    
    print(f"✅ Дашборд с встроенными графиками сгенерирован: index.html")
    print(f"📊 Статистика: {total_offers} предложений, {unique_hotels} отелей")
    print(f"💰 Цены: {min_price:.0f} - {max_price:.0f} PLN (средняя: {avg_price:.0f} PLN)")
    print(f"📈 Изменения цен: {len(hotel_changes)} отелей за 48ч")

if __name__ == "__main__":
    generate_inline_charts_dashboard()
