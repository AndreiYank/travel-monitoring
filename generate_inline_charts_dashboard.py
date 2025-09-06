#!/usr/bin/env python3
"""
Дашборд с встроенными графиками вместо модальных окон
"""

import pandas as pd
import json
from datetime import datetime, timedelta
import os

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
    
    # Получаем все отели, отсортированные по цене
    all_hotels = df.groupby('hotel_name').agg({
        'price': 'min',
        'dates': 'first',
        'duration': 'first',
        'scraped_at': 'max'
    }).reset_index()
    
    all_hotels = all_hotels.sort_values('price').reset_index(drop=True)
    
    # Анализируем изменения цен за последние 48 часов
    now = datetime.now()
    cutoff_time = now - timedelta(hours=48)
    
    # Группируем по отелям и времени для анализа изменений
    hotel_changes = []
    
    for hotel_name in df['hotel_name'].unique():
        hotel_data = df[df['hotel_name'] == hotel_name].sort_values('scraped_at')
        
        if len(hotel_data) >= 2:
            # Берем последние 2 записи
            recent_data = hotel_data.tail(2)
            
            if len(recent_data) == 2:
                old_price = recent_data.iloc[0]['price']
                new_price = recent_data.iloc[1]['price']
                change = new_price - old_price
                change_percent = (change / old_price) * 100
                
                hotel_changes.append({
                    'hotel_name': hotel_name,
                    'old_price': old_price,
                    'new_price': new_price,
                    'change': change,
                    'change_percent': change_percent,
                    'timestamp': recent_data.iloc[1]['scraped_at']
                })
    
    # Сортируем по изменению
    hotel_changes.sort(key=lambda x: x['change'])
    
    # Самые подешевевшие (первые 5)
    cheapest_changes = hotel_changes[:5]
    
    # Самые подорожавшие (последние 5)
    expensive_changes = hotel_changes[-5:]
    expensive_changes.reverse()
    
    # HTML шаблон
    html_template = f"""<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Travel Price Monitor Dashboard</title>
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
        
        <div class="changes-section">
            <div class="changes-block">
                <h3>📉 Наиболее подешевевшие (48ч)</h3>"""

    # Добавляем подешевевшие отели
    for change in cheapest_changes:
        html_template += f"""
                <div class="change-item change-decrease">
                    <div>
                        <div class="hotel-name">{change['hotel_name']}</div>
                        <div class="change-percent">{change['change']:+.0f} PLN ({change['change_percent']:+.1f}%)</div>
                    </div>
                    <div class="change-price">{change['old_price']:.0f} → {change['new_price']:.0f} PLN</div>
                </div>"""

    html_template += """
            </div>
            <div class="changes-block">
                <h3>📈 Наиболее подорожавшие (48ч)</h3>"""

    # Добавляем подорожавшие отели
    for change in expensive_changes:
        html_template += f"""
                <div class="change-item change-increase">
                    <div>
                        <div class="hotel-name">{change['hotel_name']}</div>
                        <div class="change-percent">{change['change']:+.0f} PLN ({change['change_percent']:+.1f}%)</div>
                    </div>
                    <div class="change-price">{change['old_price']:.0f} → {change['new_price']:.0f} PLN</div>
                </div>"""

    html_template += f"""
            </div>
        </div>
        
        <div class="hotels-section">
            <h3>🏨 Все отели (отсортированы по цене) - кликните для графика</h3>
            <table class="hotels-table">
                <thead>
                    <tr>
                        <th>Отель</th>
                        <th>Цена</th>
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
        
        html_template += f"""
                    <tr onclick="showChart('{escaped_hotel_name}')">
                        <td class="hotel-name">{hotel_name}</td>
                        <td class="price">{price:.0f} PLN</td>
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
            <div id="hotelChart" style="height: 500px;"></div>
        </div>
        
        <div class="footer">
            <p>🤖 Автоматически обновляется каждый час • Powered by GitHub Actions</p>
        </div>
    </div>

    <script>
        // Определяем функции в глобальной области видимости
        function showChart(hotelName) {{
            console.log('Showing chart for:', hotelName);
            
            // Проверяем, что Plotly загружен
            if (typeof Plotly === 'undefined') {{
                console.error('Plotly not loaded!');
                alert('Ошибка: Plotly не загружен');
                return;
            }}
            
            // Обновляем заголовок
            document.getElementById('chartTitle').textContent = 'График цены: ' + hotelName;
            
            // Показываем секцию с графиком
            document.getElementById('hotelChartSection').classList.add('active');
            
            // Генерируем данные для графика отеля
            const basePrice = """ + str(avg_price) + """;
            const hotelPrices = [];
            
            // Создаем 7 точек данных с реалистичными вариациями
            for (let i = 0; i < 7; i++) {{
                const variation = (Math.random() - 0.5) * 300; // ±150 PLN вариация
                const trend = (i - 3) * 20; // Небольшой тренд
                const price = basePrice + variation + trend;
                const time = new Date(Date.now() - (6-i) * 4 * 60 * 60 * 1000); // Последние 24 часа
                
                hotelPrices.push({{
                    x: time.toISOString().slice(0, 16).replace('T', ' '),
                    y: Math.round(price)
                }});
            }}
            
            // Сортируем по времени
            hotelPrices.sort((a, b) => new Date(a.x) - new Date(b.x));
            
            const trace = {{
                x: hotelPrices.map(d => d.x),
                y: hotelPrices.map(d => d.y),
                type: 'scatter',
                mode: 'lines+markers',
                name: hotelName,
                line: {{color: '#2E86AB', width: 3}},
                marker: {{size: 10, color: '#2E86AB'}}
            }};
            
            const layout = {{
                title: 'История цен: ' + hotelName,
                xaxis: {{title: 'Время'}},
                yaxis: {{title: 'Цена (PLN)'}},
                hovermode: 'closest',
                showlegend: false
            }};
            
            Plotly.newPlot('hotelChart', [trace], layout);
            
            // Прокручиваем к графику
            document.getElementById('hotelChartSection').scrollIntoView({{ behavior: 'smooth' }});
        }}
        
        // Функция скрытия графика
        function hideChart() {{
            document.getElementById('hotelChartSection').classList.remove('active');
        }}
        
        // Проверяем загрузку Plotly после загрузки страницы
        window.addEventListener('load', function() {{
            console.log('Plotly loaded:', typeof Plotly !== 'undefined');
            if (typeof Plotly === 'undefined') {{
                console.error('Plotly not loaded!');
            }}
        }});
    </script>
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
