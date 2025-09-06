#!/usr/bin/env python3
"""
Генератор интерактивного дашборда с графиками для GitHub Pages
"""

import pandas as pd
import json
from datetime import datetime
import os

def generate_interactive_dashboard():
    """Генерирует интерактивный HTML дашборд с графиками"""
    
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
    
    # Топ-15 самых дешевых отелей
    top_cheap = df.nsmallest(15, 'price')
    
    # Данные для графиков
    price_data = df.groupby('scraped_at')['price'].agg(['mean', 'min', 'max', 'count']).reset_index()
    price_data['scraped_at_str'] = price_data['scraped_at'].dt.strftime('%Y-%m-%d %H:%M')
    
    # Топ отелей по количеству предложений
    hotel_counts = df['hotel_name'].value_counts().head(10)
    
    # Распределение цен
    price_bins = pd.cut(df['price'], bins=10)
    price_distribution = price_bins.value_counts().sort_index()
    # Конвертируем интервалы в строки для JSON
    price_distribution_str = {str(interval): count for interval, count in price_distribution.items()}
    
    # HTML шаблон с интерактивными графиками
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
            max-width: 1400px;
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
        
        .table-container {{
            margin: 30px 0;
            background: white;
            border-radius: 10px;
            padding: 20px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
        }}
        
        table {{
            width: 100%;
            border-collapse: collapse;
            margin-top: 20px;
        }}
        
        th, td {{
            padding: 12px;
            text-align: left;
            border-bottom: 1px solid #ddd;
        }}
        
        th {{
            background-color: #f8f9fa;
            font-weight: 600;
        }}
        
        .price-low {{
            color: #28a745;
            font-weight: bold;
        }}
        
        .price-high {{
            color: #dc3545;
            font-weight: bold;
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
        
        <div class="table-container">
            <h2>🏆 Топ-15 самых дешевых отелей</h2>
            <table>
                <thead>
                    <tr>
                        <th>Отель</th>
                        <th>Рейтинг</th>
                        <th>Цена</th>
                        <th>Даты</th>
                        <th>Длительность</th>
                    </tr>
                </thead>
                <tbody>
"""

    # Добавляем строки таблицы
    for _, row in top_cheap.iterrows():
        html_template += f"""
                    <tr>
                        <td>{row['hotel_name']}</td>
                        <td>{row['rating'] if pd.notna(row['rating']) else 'N/A'}</td>
                        <td class="price-low">{row['price']:.0f} PLN</td>
                        <td>{row['dates'] if pd.notna(row['dates']) else 'N/A'}</td>
                        <td>{row['duration'] if pd.notna(row['duration']) else 'N/A'}</td>
                    </tr>
"""

    # Завершаем HTML
    html_template += """
                </tbody>
            </table>
        </div>
        
        <div class="footer">
            <p>🤖 Автоматически обновляется каждый час • Powered by GitHub Actions</p>
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
        const timelineTrace = {
            x: priceData.timeline.map(d => d.scraped_at_str),
            y: priceData.timeline.map(d => d.mean),
            type: 'scatter',
            mode: 'lines+markers',
            name: 'Средняя цена',
            line: {color: '#2E86AB', width: 3},
            marker: {size: 8}
        };

        const minTrace = {
            x: priceData.timeline.map(d => d.scraped_at_str),
            y: priceData.timeline.map(d => d.min),
            type: 'scatter',
            mode: 'lines+markers',
            name: 'Минимальная цена',
            line: {color: '#28a745', width: 2},
            marker: {size: 6}
        };

        const maxTrace = {
            x: priceData.timeline.map(d => d.scraped_at_str),
            y: priceData.timeline.map(d => d.max),
            type: 'scatter',
            mode: 'lines+markers',
            name: 'Максимальная цена',
            line: {color: '#dc3545', width: 2},
            marker: {size: 6}
        };

        Plotly.newPlot('price-timeline', [timelineTrace, minTrace, maxTrace], {
            title: 'Динамика цен на путешествия',
            xaxis: {title: 'Время'},
            yaxis: {title: 'Цена (PLN)'},
            hovermode: 'closest'
        });

        // График топ отелей
        const hotelData = {
            x: Object.values(priceData.hotel_counts),
            y: Object.keys(priceData.hotel_counts),
            type: 'bar',
            orientation: 'h',
            marker: {color: '#A23B72'}
        };

        Plotly.newPlot('hotel-counts', [hotelData], {
            title: 'Количество предложений по отелям',
            xaxis: {title: 'Количество предложений'},
            yaxis: {title: 'Отель'}
        });

        // График распределения цен
        const distributionData = {
            x: Object.keys(priceData.price_distribution),
            y: Object.values(priceData.price_distribution),
            type: 'bar',
            marker: {color: '#667eea'}
        };

        Plotly.newPlot('price-distribution', [distributionData], {
            title: 'Распределение цен',
            xaxis: {title: 'Диапазон цен (PLN)'},
            yaxis: {title: 'Количество предложений'}
        });
    </script>
</body>
</html>
"""

    # Сохраняем файл
    with open('index.html', 'w', encoding='utf-8') as f:
        f.write(html_template)
    
    print(f"✅ Интерактивный дашборд сгенерирован: index.html")
    print(f"📊 Статистика: {total_offers} предложений, {unique_hotels} отелей")
    print(f"💰 Цены: {min_price:.0f} - {max_price:.0f} PLN (средняя: {avg_price:.0f} PLN)")

if __name__ == "__main__":
    generate_interactive_dashboard()
