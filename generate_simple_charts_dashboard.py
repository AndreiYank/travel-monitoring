#!/usr/bin/env python3
"""
Максимально простой дашборд с графиками
"""

import pandas as pd
import json
from datetime import datetime, timedelta
import os

def generate_simple_charts_dashboard():
    """Генерирует максимально простой дашборд с графиками"""
    
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
    
    # Рассчитываем изменение цены за 48 часов на уровне отеля
    df_sorted = df.sort_values(['hotel_name', 'scraped_at'])
    forty_eight_hours = pd.Timedelta(hours=48)

    latest_rows = []
    deltas_by_hotel = {}

    for hotel_name, grp in df_sorted.groupby('hotel_name'):
        grp = grp.sort_values('scraped_at')
        latest_row = grp.iloc[-1]
        latest_time = latest_row['scraped_at']
        cutoff_time = latest_time - forty_eight_hours

        # Ищем цену на момент не позже cutoff_time
        baseline_candidates = grp[grp['scraped_at'] <= cutoff_time]
        baseline_row = baseline_candidates.iloc[-1] if len(baseline_candidates) > 0 else (grp.iloc[0] if len(grp) > 1 else None)

        if baseline_row is not None and baseline_row['scraped_at'] != latest_row['scraped_at']:
            latest_price = float(latest_row['price'])
            baseline_price = float(baseline_row['price'])
            if baseline_price > 0:
                delta_abs = latest_price - baseline_price
                delta_pct = (delta_abs / baseline_price) * 100.0
                deltas_by_hotel[hotel_name] = (delta_abs, delta_pct)
            else:
                deltas_by_hotel[hotel_name] = None
        else:
            deltas_by_hotel[hotel_name] = None

        latest_rows.append({
            'hotel_name': hotel_name,
            'price': latest_row['price'],
            'dates': latest_row.get('dates', None),
            'duration': latest_row.get('duration', None),
            'scraped_at': latest_row['scraped_at'],
        })

    all_hotels = pd.DataFrame(latest_rows)
    all_hotels = all_hotels.sort_values('price').reset_index(drop=True)
    
    # HTML шаблон - максимально простой
    html_template = f"""<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Travel Price Monitor • Дашборд с графиками</title>
    <script src="https://cdn.plot.ly/plotly-latest.min.js"></script>
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
        .chart-section {{
            margin: 30px 0;
            padding: 20px;
            background: #f8f9fa;
            border-radius: 8px;
            display: none;
        }}
        .chart-section.active {{
            display: block;
        }}
        .chart-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 20px;
        }}
        .close-chart {{
            background: #dc3545;
            color: white;
            border: none;
            padding: 8px 16px;
            border-radius: 4px;
            cursor: pointer;
        }}
        .close-chart:hover {{
            background: #c82333;
        }}
        .delta {{
            font-weight: bold;
        }}
        .delta.up {{
            color: #dc3545; /* подорожание - красный */
        }}
        .delta.down {{
            color: #28a745; /* подешевело - зеленый */
        }}
        .delta.flat {{
            color: #6c757d; /* без изменений - серый */
        }}
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
        
        <div>
            <h3>🏨 Все отели (отсортированы по цене) • кликните для графика</h3>
            <table class="hotels-table">
                <thead>
                    <tr>
                        <th>Отель</th>
                        <th>Цена</th>
                        <th>Δ 48ч</th>
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
        
        # Экранируем кавычки
        escaped_hotel_name = hotel_name.replace("'", "\\'")
        
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

        html_template += f"""
                    <tr onclick=\"showChart('{escaped_hotel_name}')\">
                        <td class=\"hotel-name\">{hotel_name}</td>
                        <td class=\"price\">{price:.0f} PLN</td>
                        <td class=\"{delta_class}\">{delta_display}</td>
                        <td>{dates}</td>
                        <td>{duration}</td>
                    </tr>"""

    # Завершаем таблицу и добавляем секцию для графика
    html_template += f"""
                </tbody>
            </table>
        </div>
        
        <!-- Секция для графика отеля -->
        <div id="hotelChartSection" class="chart-section">
            <div class="chart-header">
                <h3 id="chartTitle">График цены отеля</h3>
                <button class="close-chart" onclick="hideChart()">Закрыть график</button>
            </div>
            <div id="hotelChart" style="height: 400px;"></div>
        </div>
        
        <div class="footer">
            <p>🤖 Автоматически обновляется каждый час • Powered by GitHub Actions</p>
        </div>
    </div>

    <script>
        // Простые функции
        function showChart(hotelName) {{
            console.log('Showing chart for:', hotelName);
            
            document.getElementById('chartTitle').textContent = 'График цены: ' + hotelName;
            document.getElementById('hotelChartSection').classList.add('active');
            
            // Простые тестовые данные
            const data = [{{
                x: ['2025-09-20', '2025-09-21', '2025-09-22', '2025-09-23', '2025-09-24'],
                y: [""" + str(avg_price) + """, """ + str(avg_price + 100) + """, """ + str(avg_price - 50) + """, """ + str(avg_price + 200) + """, """ + str(avg_price + 150) + """],
                type: 'scatter',
                mode: 'lines+markers',
                name: hotelName,
                line: {{color: '#2E86AB', width: 3}},
                marker: {{size: 8}}
            }}];
            
            const layout = {{
                title: 'История цен: ' + hotelName,
                xaxis: {{title: 'Дата'}},
                yaxis: {{title: 'Цена (PLN)'}}
            }};
            
            Plotly.newPlot('hotelChart', data, layout);
            
            // Прокрутка к графику
            document.getElementById('hotelChartSection').scrollIntoView({{ behavior: 'smooth' }});
        }}
        
        function hideChart() {{
            document.getElementById('hotelChartSection').classList.remove('active');
        }}
    </script>
</body>
</html>"""

    # Сохраняем файл
    with open('index.html', 'w', encoding='utf-8') as f:
        f.write(html_template)
    
    print(f"✅ Простой дашборд с графиками сгенерирован: index.html")
    print(f"📊 Статистика: {total_offers} предложений, {unique_hotels} отелей")
    print(f"💰 Цены: {min_price:.0f} - {max_price:.0f} PLN (средняя: {avg_price:.0f} PLN)")

if __name__ == "__main__":
    generate_simple_charts_dashboard()
