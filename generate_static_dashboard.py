#!/usr/bin/env python3
"""
Генератор статического дашборда для GitHub Pages
"""

import pandas as pd
import json
from datetime import datetime
import os

def generate_static_dashboard():
    """Генерирует статический HTML дашборд"""
    
    # Загружаем данные
    try:
        df = pd.read_csv('data/travel_prices.csv')
        df['scraped_at'] = pd.to_datetime(df['scraped_at'])
    except Exception as e:
        print(f"Ошибка загрузки данных: {e}")
        return
    
    # Вычисляем статистику
    total_offers = len(df)
    unique_hotels = df['hotel_name'].nunique()
    avg_price = df['price'].mean()
    min_price = df['price'].min()
    max_price = df['price'].max()
    
    # Топ-10 самых дешевых отелей
    top_cheap = df.nsmallest(10, 'price')
    
    # Статистика по отелям
    hotel_stats = df.groupby('hotel_name')['price'].agg(['count', 'mean', 'min', 'max']).round(2)
    hotel_stats = hotel_stats.sort_values('mean').head(20)
    
    # Динамика по дням
    daily_stats = df.groupby(df['scraped_at'].dt.date)['price'].agg(['mean', 'min', 'max', 'count']).round(2)
    
    # Создаем HTML
    html_content = f"""
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
            max-width: 1200px;
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
        
        .metric {{
            background: white;
            padding: 25px;
            border-radius: 10px;
            text-align: center;
            box-shadow: 0 5px 15px rgba(0,0,0,0.1);
            transition: transform 0.3s ease;
        }}
        
        .metric:hover {{
            transform: translateY(-5px);
        }}
        
        .metric h3 {{
            margin: 0 0 10px 0;
            color: #2E86AB;
            font-size: 0.9em;
            text-transform: uppercase;
            letter-spacing: 1px;
        }}
        
        .metric .value {{
            font-size: 2.5em;
            font-weight: bold;
            color: #333;
            margin: 0;
        }}
        
        .content {{
            padding: 30px;
        }}
        
        .section {{
            margin-bottom: 40px;
        }}
        
        .section h2 {{
            color: #2E86AB;
            border-bottom: 3px solid #A23B72;
            padding-bottom: 10px;
            margin-bottom: 20px;
        }}
        
        .chart-container {{
            background: white;
            padding: 20px;
            border-radius: 10px;
            box-shadow: 0 5px 15px rgba(0,0,0,0.1);
            margin-bottom: 20px;
        }}
        
        .table-container {{
            overflow-x: auto;
            background: white;
            border-radius: 10px;
            box-shadow: 0 5px 15px rgba(0,0,0,0.1);
        }}
        
        table {{
            width: 100%;
            border-collapse: collapse;
        }}
        
        th, td {{
            padding: 12px;
            text-align: left;
            border-bottom: 1px solid #eee;
        }}
        
        th {{
            background: #f8f9fa;
            font-weight: 600;
            color: #2E86AB;
        }}
        
        tr:hover {{
            background: #f8f9fa;
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
            background: #333;
            color: white;
            text-align: center;
            padding: 20px;
            margin-top: 40px;
        }}
        
        @media (max-width: 768px) {{
            .metrics {{
                grid-template-columns: 1fr;
            }}
            
            .header h1 {{
                font-size: 2em;
            }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>✈️ Travel Price Monitor</h1>
            <p>Интерактивный дашборд для мониторинга цен на путешествия</p>
        </div>
        
        <div class="metrics">
            <div class="metric">
                <h3>Всего предложений</h3>
                <p class="value">{total_offers}</p>
            </div>
            <div class="metric">
                <h3>Уникальных отелей</h3>
                <p class="value">{unique_hotels}</p>
            </div>
            <div class="metric">
                <h3>Средняя цена</h3>
                <p class="value">{avg_price:.0f} PLN</p>
            </div>
            <div class="metric">
                <h3>Диапазон цен</h3>
                <p class="value">{min_price:.0f} - {max_price:.0f} PLN</p>
            </div>
        </div>
        
        <div class="content">
            <div class="section">
                <h2>📊 Топ-10 самых дешевых отелей</h2>
                <div class="table-container">
                    <table>
                        <thead>
                            <tr>
                                <th>#</th>
                                <th>Отель</th>
                                <th>Цена (PLN)</th>
                                <th>Дата сбора</th>
                            </tr>
                        </thead>
                        <tbody>
"""
    
    # Добавляем топ-10 самых дешевых отелей
    for i, (_, row) in enumerate(top_cheap.iterrows(), 1):
        html_content += f"""
                            <tr>
                                <td>{i}</td>
                                <td>{row['hotel_name']}</td>
                                <td class="price-low">{row['price']:.0f} PLN</td>
                                <td>{row['scraped_at'].strftime('%Y-%m-%d')}</td>
                            </tr>
"""
    
    html_content += """
                        </tbody>
                    </table>
                </div>
            </div>
            
            <div class="section">
                <h2>📋 Статистика по отелям</h2>
                <div class="table-container">
                    <table>
                        <thead>
                            <tr>
                                <th>Отель</th>
                                <th>Предложений</th>
                                <th>Средняя цена</th>
                                <th>Мин цена</th>
                                <th>Макс цена</th>
                            </tr>
                        </thead>
                        <tbody>
"""
    
    # Добавляем статистику по отелям
    for hotel, stats in hotel_stats.iterrows():
        html_content += f"""
                            <tr>
                                <td>{hotel}</td>
                                <td>{stats['count']}</td>
                                <td>{stats['mean']:.0f} PLN</td>
                                <td class="price-low">{stats['min']:.0f} PLN</td>
                                <td class="price-high">{stats['max']:.0f} PLN</td>
                            </tr>
"""
    
    html_content += f"""
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
        
        <div class="footer">
            <p>Последнее обновление: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
        </div>
    </div>
</body>
</html>
"""
    
    # Сохраняем HTML файл
    with open('index.html', 'w', encoding='utf-8') as f:
        f.write(html_content)
    
    print("✅ Статический дашборд сгенерирован: index.html")
    print(f"📊 Статистика: {total_offers} предложений, {unique_hotels} отелей")
    print(f"💰 Цены: {min_price:.0f} - {max_price:.0f} PLN (средняя: {avg_price:.0f} PLN)")

if __name__ == "__main__":
    generate_static_dashboard()
