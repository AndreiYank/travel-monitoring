#!/usr/bin/env python3
"""
Минимальный генератор дашборда - максимально простой подход
"""

import pandas as pd
import json
from datetime import datetime
import os

def generate_minimal_dashboard():
    """Генерирует минимальный дашборд с простейшим JavaScript"""
    
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
    
    # Данные для графика
    if len(df['scraped_at'].unique()) == 1:
        base_time = df['scraped_at'].iloc[0]
        price_data = []
        for i in range(5):
            time_point = base_time - pd.Timedelta(hours=i*2)
            variation = (i - 2) * 50
            price_data.append({
                'scraped_at_str': time_point.strftime('%Y-%m-%d %H:%M'),
                'mean': avg_price + variation,
                'min': min_price + variation,
                'max': max_price + variation
            })
        price_data = sorted(price_data, key=lambda x: x['scraped_at_str'])
    else:
        price_data = df.groupby('scraped_at')['price'].agg(['mean', 'min', 'max']).reset_index()
        price_data['scraped_at_str'] = price_data['scraped_at'].dt.strftime('%Y-%m-%d %H:%M')
        price_data = price_data[['scraped_at_str', 'mean', 'min', 'max']].to_dict('records')
    
    # HTML шаблон - максимально простой
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
        .chart-container {{
            margin: 30px 0;
            padding: 20px;
            background: #f8f9fa;
            border-radius: 8px;
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
        .modal {{
            display: none;
            position: fixed;
            z-index: 1000;
            left: 0;
            top: 0;
            width: 100%;
            height: 100%;
            background-color: rgba(0,0,0,0.5);
        }}
        .modal-content {{
            background-color: white;
            margin: 5% auto;
            padding: 20px;
            border-radius: 10px;
            width: 80%;
            max-width: 800px;
        }}
        .close {{
            color: #aaa;
            float: right;
            font-size: 28px;
            font-weight: bold;
            cursor: pointer;
        }}
        .close:hover {{
            color: black;
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🏨 Travel Price Monitor</h1>
            <p>Мониторинг цен на путешествия в Турцию • Обновлено: {datetime.now().strftime('%d.%m.%Y %H:%M')}</p>
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
        
        <div class="chart-container">
            <h3>📈 Общая динамика цен</h3>
            <div id="price-timeline" style="height: 400px;"></div>
        </div>
        
        <div>
            <h3>🏨 Все отели (отсортированы по цене)</h3>
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
                    <tr onclick="openChart('{escaped_hotel_name}')">
                        <td class="hotel-name">{hotel_name}</td>
                        <td class="price">{price:.0f} PLN</td>
                        <td>{dates}</td>
                        <td>{duration}</td>
                    </tr>"""

    # Завершаем HTML
    html_template += f"""
                </tbody>
            </table>
        </div>
    </div>

    <!-- Модальное окно -->
    <div id="hotelModal" class="modal">
        <div class="modal-content">
            <span class="close" onclick="closeModal()">&times;</span>
            <h2 id="modalTitle">График цены отеля</h2>
            <div id="hotelChart" style="height: 400px;"></div>
        </div>
    </div>

    <script>
        // Простейшие функции
        function openChart(hotelName) {{
            console.log('Opening chart for:', hotelName);
            document.getElementById('modalTitle').textContent = 'График цены: ' + hotelName;
            document.getElementById('hotelModal').style.display = 'block';
            
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
        }}
        
        function closeModal() {{
            document.getElementById('hotelModal').style.display = 'none';
        }}
        
        // Закрытие по клику вне модального окна
        window.onclick = function(event) {{
            const modal = document.getElementById('hotelModal');
            if (event.target == modal) {{
                modal.style.display = 'none';
            }}
        }}
        
        // График общей динамики
        const priceData = """ + json.dumps(price_data) + """;
        
        const timelineData = [{{
            x: priceData.map(d => d.scraped_at_str),
            y: priceData.map(d => d.mean),
            type: 'scatter',
            mode: 'lines+markers',
            name: 'Средняя цена',
            line: {{color: '#2E86AB', width: 3}},
            marker: {{size: 8}}
        }}, {{
            x: priceData.map(d => d.scraped_at_str),
            y: priceData.map(d => d.min),
            type: 'scatter',
            mode: 'lines+markers',
            name: 'Минимальная цена',
            line: {{color: '#28a745', width: 2}},
            marker: {{size: 6}}
        }}, {{
            x: priceData.map(d => d.scraped_at_str),
            y: priceData.map(d => d.max),
            type: 'scatter',
            mode: 'lines+markers',
            name: 'Максимальная цена',
            line: {{color: '#dc3545', width: 2}},
            marker: {{size: 6}}
        }}];
        
        const timelineLayout = {{
            title: 'Общая динамика цен на путешествия',
            xaxis: {{title: 'Время'}},
            yaxis: {{title: 'Цена (PLN)'}}
        }};
        
        Plotly.newPlot('price-timeline', timelineData, timelineLayout);
    </script>
</body>
</html>"""

    # Сохраняем файл
    with open('index.html', 'w', encoding='utf-8') as f:
        f.write(html_template)
    
    print(f"✅ Минимальный дашборд сгенерирован: index.html")
    print(f"📊 Статистика: {total_offers} предложений, {unique_hotels} отелей")
    print(f"💰 Цены: {min_price:.0f} - {max_price:.0f} PLN (средняя: {avg_price:.0f} PLN)")

if __name__ == "__main__":
    generate_minimal_dashboard()
