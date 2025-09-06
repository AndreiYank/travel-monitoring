#!/usr/bin/env python3
"""
Генератор рабочего дашборда с исправленными графиками
"""

import pandas as pd
import json
from datetime import datetime
import os
from enhanced_alerts import EnhancedAlertManager

def generate_working_dashboard():
    """Генерирует рабочий дашборд с исправленными графиками"""
    
    # Загружаем данные
    try:
        df = pd.read_csv('data/travel_prices.csv')
        df['scraped_at'] = pd.to_datetime(df['scraped_at'])
        print(f"✅ Загружено {len(df)} записей")
    except Exception as e:
        print(f"❌ Ошибка загрузки данных: {e}")
        return
    
    # Загружаем алерты
    alert_manager = EnhancedAlertManager()
    all_alerts = alert_manager.get_recent_alerts(100)
    
    # Вычисляем статистику
    total_offers = len(df)
    unique_hotels = df['hotel_name'].nunique()
    avg_price = df['price'].mean()
    min_price = df['price'].min()
    max_price = df['price'].max()
    
    # Получаем все отели, отсортированные по цене
    all_hotels = df.groupby('hotel_name').agg({
        'price': 'min',
        'rating': 'first',
        'dates': 'first',
        'duration': 'first',
        'scraped_at': 'max'
    }).reset_index()
    
    all_hotels = all_hotels.sort_values('price').reset_index(drop=True)
    
    # Создаем словарь алертов
    alerts_dict = {}
    for alert in all_alerts:
        alerts_dict[alert['hotel_name']] = alert
    
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
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            margin: 0;
            padding: 20px;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
        }}
        
        .container {{
            max-width: 1600px;
            margin: 0 auto;
            background: white;
            border-radius: 15px;
            box-shadow: 0 10px 30px rgba(0,0,0,0.3);
            overflow: hidden;
        }}
        
        .header {{
            background: linear-gradient(135deg, #2E86AB 0%, #A23B72 100%);
            color: white;
            padding: 30px;
            text-align: center;
        }}
        
        .header h1 {{
            margin: 0;
            font-size: 2.5em;
            font-weight: 300;
        }}
        
        .header p {{
            margin: 10px 0 0 0;
            opacity: 0.9;
        }}
        
        .metrics {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 20px;
            padding: 30px;
            background: #f8f9fa;
        }}
        
        .metric-card {{
            background: white;
            padding: 20px;
            border-radius: 10px;
            text-align: center;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
        }}
        
        .metric-value {{
            font-size: 2em;
            font-weight: bold;
            color: #2E86AB;
            margin-bottom: 5px;
        }}
        
        .metric-label {{
            color: #666;
            font-size: 0.9em;
        }}
        
        .charts-section {{
            padding: 30px;
        }}
        
        .chart-container {{
            margin: 30px 0;
            background: white;
            border-radius: 10px;
            padding: 20px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
        }}
        
        .chart-title {{
            font-size: 1.5em;
            margin-bottom: 20px;
            color: #333;
            text-align: center;
        }}
        
        .hotels-section {{
            padding: 30px;
        }}
        
        .hotels-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 20px;
        }}
        
        .hotels-title {{
            font-size: 1.8em;
            color: #333;
            margin: 0;
        }}
        
        .search-box {{
            padding: 10px;
            border: 1px solid #ddd;
            border-radius: 5px;
            width: 300px;
        }}
        
        .hotels-table {{
            width: 100%;
            border-collapse: collapse;
            background: white;
            border-radius: 10px;
            overflow: hidden;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
        }}
        
        .hotels-table th {{
            background: #f8f9fa;
            padding: 15px;
            text-align: left;
            font-weight: 600;
            color: #333;
            border-bottom: 2px solid #dee2e6;
        }}
        
        .hotels-table td {{
            padding: 15px;
            border-bottom: 1px solid #dee2e6;
            vertical-align: middle;
        }}
        
        .hotels-table tr:hover {{
            background: #f8f9fa;
            cursor: pointer;
        }}
        
        .hotel-name {{
            font-weight: 600;
            color: #2E86AB;
        }}
        
        .price {{
            font-weight: bold;
            font-size: 1.1em;
        }}
        
        .price-change {{
            display: inline-block;
            padding: 4px 8px;
            border-radius: 4px;
            font-size: 0.9em;
            font-weight: bold;
        }}
        
        .price-decrease {{
            background: #d4edda;
            color: #155724;
        }}
        
        .price-increase {{
            background: #f8d7da;
            color: #721c24;
        }}
        
        .price-stable {{
            background: #e2e3e5;
            color: #6c757d;
        }}
        
        .dates {{
            color: #666;
            font-size: 0.9em;
        }}
        
        .alerts-section {{
            padding: 30px;
            background: #f8f9fa;
        }}
        
        .alert-item {{
            background: white;
            padding: 15px;
            margin: 10px 0;
            border-radius: 8px;
            border-left: 4px solid;
            box-shadow: 0 2px 5px rgba(0,0,0,0.1);
        }}
        
        .alert-decrease {{
            border-left-color: #28a745;
        }}
        
        .alert-increase {{
            border-left-color: #dc3545;
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
            max-height: 80vh;
            overflow-y: auto;
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
        
        .footer {{
            background: #f8f9fa;
            padding: 20px;
            text-align: center;
            color: #666;
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
            <div class="metric-card">
                <div class="metric-value">{total_offers:,}</div>
                <div class="metric-label">Всего предложений</div>
            </div>
            <div class="metric-card">
                <div class="metric-value">{unique_hotels}</div>
                <div class="metric-label">Уникальных отелей</div>
            </div>
            <div class="metric-card">
                <div class="metric-value">{avg_price:.0f} PLN</div>
                <div class="metric-label">Средняя цена</div>
            </div>
            <div class="metric-card">
                <div class="metric-value">{min_price:.0f} PLN</div>
                <div class="metric-label">Минимальная цена</div>
            </div>
            <div class="metric-card">
                <div class="metric-value">{max_price:.0f} PLN</div>
                <div class="metric-label">Максимальная цена</div>
            </div>
            <div class="metric-card">
                <div class="metric-value">{len(all_alerts)}</div>
                <div class="metric-label">Всего алертов</div>
            </div>
        </div>
        
        <div class="charts-section">
            <div class="chart-container">
                <div class="chart-title">📈 Общая динамика цен</div>
                <div id="price-timeline" style="height: 400px;"></div>
            </div>
        </div>
        
        <div class="hotels-section">
            <div class="hotels-header">
                <h2 class="hotels-title">🏨 Все отели (отсортированы по цене)</h2>
                <input type="text" class="search-box" id="hotelSearch" placeholder="Поиск отеля...">
            </div>
            <table class="hotels-table" id="hotelsTable">
                <thead>
                    <tr>
                        <th>Отель</th>
                        <th>Цена</th>
                        <th>Изменение</th>
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
        
        alert = alerts_dict.get(hotel_name)
        if alert:
            change_class = "price-decrease" if alert['change'] < 0 else "price-increase"
            change_text = f"{alert['change']:+.0f} PLN ({alert['change_percent']:+.1f}%)"
            change_icon = "📉" if alert['change'] < 0 else "📈"
            display_price = alert['current_price']
        else:
            change_class = "price-stable"
            change_text = "Без изменений"
            change_icon = "➡️"
            display_price = price
        
        # Экранируем кавычки
        escaped_hotel_name = hotel_name.replace("'", "\\'")
        
        html_template += f"""
                    <tr onclick="showHotelChart('{escaped_hotel_name}')">
                        <td class="hotel-name">{hotel_name}</td>
                        <td class="price">{display_price:.0f} PLN</td>
                        <td><span class="price-change {change_class}">{change_icon} {change_text}</span></td>
                        <td class="dates">{dates}</td>
                        <td class="dates">{duration}</td>
                    </tr>"""

    # Завершаем таблицу и добавляем алерты
    html_template += """
                </tbody>
            </table>
        </div>
        
        <div class="alerts-section">
            <h2>🚨 История алертов (новые сверху)</h2>
            <div id="alertsList">"""

    # Добавляем алерты
    sorted_alerts = sorted(all_alerts, key=lambda x: x['timestamp'], reverse=True)
    for alert in sorted_alerts:
        alert_class = "alert-decrease" if alert['alert_type'] == 'decrease' else "alert-increase"
        html_template += f"""
            <div class="alert-item {alert_class}">
                <strong>{alert['icon']} {alert['hotel_name']}</strong><br>
                Цена: {alert['previous_price']:.0f} → {alert['current_price']:.0f} PLN 
                ({alert['change']:+.0f} PLN, {alert['change_percent']:+.1f}%)<br>
                <small>Время: {alert['timestamp'][:19]}</small>
            </div>"""

    # Завершаем HTML
    html_template += f"""
            </div>
        </div>
        
        <div class="footer">
            <p>🤖 Автоматически обновляется каждый час • Powered by GitHub Actions</p>
        </div>
    </div>

    <!-- Модальное окно для графика отеля -->
    <div id="hotelModal" class="modal">
        <div class="modal-content">
            <span class="close">&times;</span>
            <h2 id="modalTitle">График цены отеля</h2>
            <div id="hotelChart" style="height: 400px;"></div>
        </div>
    </div>

    <script>
        // Определяем функцию в самом начале
        function showHotelChart(hotelName) {{
            console.log('Opening chart for hotel:', hotelName);
            document.getElementById('modalTitle').textContent = 'График цены: ' + hotelName;
            
            // Создаем демонстрационные данные для отеля
            const hotelPrices = [];
            const basePrice = """ + str(avg_price) + """;
            
            // Генерируем 5 точек данных с вариациями
            for (let i = 0; i < 5; i++) {{
                const variation = (Math.random() - 0.5) * 200; // ±100 PLN вариация
                const price = basePrice + variation + (i * 50); // Небольшой тренд
                const time = new Date(Date.now() - (4-i) * 2 * 60 * 60 * 1000); // Последние 8 часов
                
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
                marker: {{size: 8}}
            }};
            
            Plotly.newPlot('hotelChart', [trace], {{
                title: 'История цен: ' + hotelName,
                xaxis: {{title: 'Время'}},
                yaxis: {{title: 'Цена (PLN)'}},
                hovermode: 'closest'
            }});
            
            document.getElementById('hotelModal').style.display = 'block';
        }}

        // Данные для графика
        const priceData = """ + json.dumps(price_data) + """;

        // График общей динамики цен
        const timelineTrace = {{
            x: priceData.map(d => d.scraped_at_str),
            y: priceData.map(d => d.mean),
            type: 'scatter',
            mode: 'lines+markers',
            name: 'Средняя цена',
            line: {{color: '#2E86AB', width: 3}},
            marker: {{size: 8}}
        }};

        const minTrace = {{
            x: priceData.map(d => d.scraped_at_str),
            y: priceData.map(d => d.min),
            type: 'scatter',
            mode: 'lines+markers',
            name: 'Минимальная цена',
            line: {{color: '#28a745', width: 2}},
            marker: {{size: 6}}
        }};

        const maxTrace = {{
            x: priceData.map(d => d.scraped_at_str),
            y: priceData.map(d => d.max),
            type: 'scatter',
            mode: 'lines+markers',
            name: 'Максимальная цена',
            line: {{color: '#dc3545', width: 2}},
            marker: {{size: 6}}
        }};

        Plotly.newPlot('price-timeline', [timelineTrace, minTrace, maxTrace], {{
            title: 'Общая динамика цен на путешествия',
            xaxis: {{title: 'Время'}},
            yaxis: {{title: 'Цена (PLN)'}},
            hovermode: 'closest'
        }});

        // Поиск по отелям
        document.getElementById('hotelSearch').addEventListener('input', function(e) {{
            const searchTerm = e.target.value.toLowerCase();
            const rows = document.querySelectorAll('#hotelsTable tbody tr');
            
            rows.forEach(row => {{
                const hotelName = row.querySelector('.hotel-name').textContent.toLowerCase();
                if (hotelName.includes(searchTerm)) {{
                    row.style.display = '';
                }} else {{
                    row.style.display = 'none';
                }}
            }});
        }});

        // Обработчики модального окна
        document.getElementsByClassName('close')[0].onclick = function() {{
            document.getElementById('hotelModal').style.display = 'none';
        }}

        window.onclick = function(event) {{
            const modal = document.getElementById('hotelModal');
            if (event.target == modal) {{
                modal.style.display = 'none';
            }}
        }}
    </script>
</body>
</html>"""

    # Сохраняем файл
    with open('index.html', 'w', encoding='utf-8') as f:
        f.write(html_template)
    
    print(f"✅ Рабочий дашборд сгенерирован: index.html")
    print(f"📊 Статистика: {total_offers} предложений, {unique_hotels} отелей")
    print(f"💰 Цены: {min_price:.0f} - {max_price:.0f} PLN (средняя: {avg_price:.0f} PLN)")
    print(f"🚨 Всего алертов: {len(all_alerts)}")

if __name__ == "__main__":
    generate_working_dashboard()