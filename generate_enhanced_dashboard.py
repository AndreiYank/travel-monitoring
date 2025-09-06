#!/usr/bin/env python3
"""
Генератор улучшенного интерактивного дашборда с алертами и графиками
"""

import pandas as pd
import json
from datetime import datetime
import os
from enhanced_alerts import EnhancedAlertManager

def generate_enhanced_dashboard():
    """Генерирует улучшенный интерактивный HTML дашборд"""
    
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
    recent_alerts = alert_manager.get_recent_alerts(20)
    current_alerts = alert_manager.get_price_changes()
    
    # Вычисляем статистику
    total_offers = len(df)
    unique_hotels = df['hotel_name'].nunique()
    avg_price = df['price'].mean()
    min_price = df['price'].min()
    max_price = df['price'].max()
    
    # Получаем все отели, отсортированные по цене
    all_hotels = df.groupby('hotel_name').agg({
        'price': 'min',  # Минимальная цена для отеля
        'rating': 'first',
        'dates': 'first',
        'duration': 'first',
        'scraped_at': 'max'  # Последнее обновление
    }).reset_index()
    
    # Сортируем по цене (от наименьшей к наибольшей)
    all_hotels = all_hotels.sort_values('price').reset_index(drop=True)
    
    # Создаем словарь алертов для быстрого поиска
    alerts_dict = {}
    for alert in current_alerts:
        alerts_dict[alert['hotel_name']] = alert
    
    # Данные для графиков
    price_data = df.groupby('scraped_at')['price'].agg(['mean', 'min', 'max', 'count']).reset_index()
    price_data['scraped_at_str'] = price_data['scraped_at'].dt.strftime('%Y-%m-%d %H:%M')
    
    # Топ отелей по количеству предложений
    hotel_counts = df['hotel_name'].value_counts().head(10)
    
    # Распределение цен
    price_bins = pd.cut(df['price'], bins=10)
    price_distribution = price_bins.value_counts().sort_index()
    price_distribution_str = {str(interval): count for interval, count in price_distribution.items()}
    
    # HTML шаблон с улучшенным интерфейсом
    html_template = f"""
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Travel Price Monitor Dashboard</title>
    <script src="https://cdn.plot.ly/plotly-latest.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
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
        
        .rating {{
            color: #ffc107;
            font-weight: bold;
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
        
        @media (max-width: 768px) {{
            .container {{
                margin: 10px;
                border-radius: 10px;
            }}
            
            .header h1 {{
                font-size: 2em;
            }}
            
            .metrics {{
                grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
                padding: 20px;
            }}
            
            .search-box {{
                width: 100%;
                margin-top: 10px;
            }}
            
            .hotels-header {{
                flex-direction: column;
                align-items: flex-start;
            }}
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
                <div class="metric-value">{len(current_alerts)}</div>
                <div class="metric-label">Активных алертов</div>
            </div>
        </div>
        
        <div class="charts-section">
            <div class="chart-container">
                <div class="chart-title">📈 Динамика цен по времени</div>
                <div id="price-timeline" style="height: 400px;"></div>
            </div>
            
            <div class="chart-container">
                <div class="chart-title">🏨 Топ-10 отелей по количеству предложений</div>
                <div id="hotel-counts" style="height: 400px;"></div>
            </div>
            
            <div class="chart-container">
                <div class="chart-title">📊 Распределение цен</div>
                <div id="price-distribution" style="height: 400px;"></div>
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
                        <th>Рейтинг</th>
                        <th>Даты</th>
                        <th>Длительность</th>
                    </tr>
                </thead>
                <tbody>
"""

    # Добавляем строки таблицы с алертами
    for i, (_, hotel) in enumerate(all_hotels.iterrows()):
        hotel_name = hotel['hotel_name']
        price = hotel['price']
        rating = hotel['rating'] if pd.notna(hotel['rating']) else 'N/A'
        dates = hotel['dates'] if pd.notna(hotel['dates']) else '20-09-2025 - 04-10-2025'
        duration = hotel['duration'] if pd.notna(hotel['duration']) else '6-15 дней'
        
        # Проверяем есть ли алерт для этого отеля
        alert = alerts_dict.get(hotel_name)
        if alert:
            change_class = "price-decrease" if alert['change'] < 0 else "price-increase"
            change_text = f"{alert['change']:+.0f} PLN ({alert['change_percent']:+.1f}%)"
            change_icon = "📉" if alert['change'] < 0 else "📈"
        else:
            change_class = "price-stable"
            change_text = "Без изменений"
            change_icon = "➡️"
        
        html_template += f"""
                    <tr onclick="showHotelChart('{hotel_name}')">
                        <td class="hotel-name">{hotel_name}</td>
                        <td class="price">{price:.0f} PLN</td>
                        <td><span class="price-change {change_class}">{change_icon} {change_text}</span></td>
                        <td class="rating">{rating}</td>
                        <td class="dates">{dates}</td>
                        <td class="dates">{duration}</td>
                    </tr>
"""

    # Завершаем таблицу и добавляем алерты
    html_template += """
                </tbody>
            </table>
        </div>
        
        <div class="alerts-section">
            <h2>🚨 Последние алерты</h2>
            <div id="alertsList">
"""

    # Добавляем алерты
    for alert in recent_alerts[-10:]:  # Последние 10 алертов
        alert_class = "alert-decrease" if alert['alert_type'] == 'decrease' else "alert-increase"
        html_template += f"""
            <div class="alert-item {alert_class}">
                <strong>{alert['icon']} {alert['hotel_name']}</strong><br>
                Цена: {alert['previous_price']:.0f} → {alert['current_price']:.0f} PLN 
                ({alert['change']:+.0f} PLN, {alert['change_percent']:+.1f}%)<br>
                <small>Время: {alert['timestamp'][:19]}</small>
            </div>
"""

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
        // Данные для графиков
        const priceData = """ + json.dumps({
            'timeline': price_data[['scraped_at_str', 'mean', 'min', 'max']].to_dict('records'),
            'hotel_counts': hotel_counts.to_dict(),
            'price_distribution': price_distribution_str
        }) + """;

        // График динамики цен
        const timelineTrace = {{
            x: priceData.timeline.map(d => d.scraped_at_str),
            y: priceData.timeline.map(d => d.mean),
            type: 'scatter',
            mode: 'lines+markers',
            name: 'Средняя цена',
            line: {{color: '#2E86AB', width: 3}},
            marker: {{size: 8}}
        }};

        const minTrace = {{
            x: priceData.timeline.map(d => d.scraped_at_str),
            y: priceData.timeline.map(d => d.min),
            type: 'scatter',
            mode: 'lines+markers',
            name: 'Минимальная цена',
            line: {{color: '#28a745', width: 2}},
            marker: {{size: 6}}
        }};

        const maxTrace = {{
            x: priceData.timeline.map(d => d.scraped_at_str),
            y: priceData.timeline.map(d => d.max),
            type: 'scatter',
            mode: 'lines+markers',
            name: 'Максимальная цена',
            line: {{color: '#dc3545', width: 2}},
            marker: {{size: 6}}
        }};

        Plotly.newPlot('price-timeline', [timelineTrace, minTrace, maxTrace], {{
            title: 'Динамика цен на путешествия',
            xaxis: {{title: 'Время'}},
            yaxis: {{title: 'Цена (PLN)'}},
            hovermode: 'closest'
        }});

        // График топ отелей
        const hotelData = {{
            x: Object.values(priceData.hotel_counts),
            y: Object.keys(priceData.hotel_counts),
            type: 'bar',
            orientation: 'h',
            marker: {{color: '#A23B72'}}
        }};

        Plotly.newPlot('hotel-counts', [hotelData], {{
            title: 'Количество предложений по отелям',
            xaxis: {{title: 'Количество предложений'}},
            yaxis: {{title: 'Отель'}}
        }});

        // График распределения цен
        const distributionData = {{
            x: Object.keys(priceData.price_distribution),
            y: Object.values(priceData.price_distribution),
            type: 'bar',
            marker: {{color: '#667eea'}}
        }};

        Plotly.newPlot('price-distribution', [distributionData], {{
            title: 'Распределение цен',
            xaxis: {{title: 'Диапазон цен (PLN)'}},
            yaxis: {{title: 'Количество предложений'}}
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

        // Модальное окно
        const modal = document.getElementById('hotelModal');
        const closeBtn = document.getElementsByClassName('close')[0];

        function showHotelChart(hotelName) {{
            document.getElementById('modalTitle').textContent = `График цены: ${{hotelName}}`;
            
            // Здесь можно загрузить данные для конкретного отеля
            // Пока что показываем заглушку
            const placeholderData = {{
                x: ['2025-09-06 10:00', '2025-09-06 12:00', '2025-09-06 14:00'],
                y: [6500, 6400, 6300],
                type: 'scatter',
                mode: 'lines+markers',
                name: hotelName,
                line: {{color: '#2E86AB', width: 3}}
            }};
            
            Plotly.newPlot('hotelChart', [placeholderData], {{
                title: `История цен: ${{hotelName}}`,
                xaxis: {{title: 'Время'}},
                yaxis: {{title: 'Цена (PLN)'}}
            }});
            
            modal.style.display = 'block';
        }}

        closeBtn.onclick = function() {{
            modal.style.display = 'none';
        }}

        window.onclick = function(event) {{
            if (event.target == modal) {{
                modal.style.display = 'none';
            }}
        }}
    </script>
</body>
</html>
"""

    # Сохраняем файл
    with open('index.html', 'w', encoding='utf-8') as f:
        f.write(html_template)
    
    print(f"✅ Улучшенный дашборд сгенерирован: index.html")
    print(f"📊 Статистика: {total_offers} предложений, {unique_hotels} отелей")
    print(f"💰 Цены: {min_price:.0f} - {max_price:.0f} PLN (средняя: {avg_price:.0f} PLN)")
    print(f"🚨 Активных алертов: {len(current_alerts)}")

if __name__ == "__main__":
    generate_enhanced_dashboard()
