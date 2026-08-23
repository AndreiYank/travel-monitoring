#!/usr/bin/env python3
"""
Скрипт для генерации всех дашбордов с поддержкой сравнения аэропортов
"""

import os
import json
from generate_dashboard_with_airport_comparison import generate_dashboard_with_airport_comparison

def generate_all_dashboards():
    """Генерирует дашборды для всех стран с поддержкой сравнения аэропортов"""
    
    countries = [
        {
            'name': 'Греция',
            'data_file': 'data/travel_prices.csv',
            'airport_comparison_file': 'data/travel_prices_airport_comparison.json',
            'output_file': 'index_greece_airports.html',
            'title': 'Греция • Сравнение аэропортов'
        },
        {
            'name': 'Египет',
            'data_file': 'data/egypt_travel_prices.csv',
            'airport_comparison_file': 'data/egypt_travel_prices_airport_comparison.json',
            'output_file': 'index_egypt_airports.html',
            'title': 'Египет • Сравнение аэропортов'
        },
        {
            'name': 'Турция',
            'data_file': 'data/turkey_travel_prices.csv',
            'airport_comparison_file': 'data/turkey_travel_prices_airport_comparison.json',
            'output_file': 'index_turkey_airports.html',
            'title': 'Турция • Сравнение аэропортов'
        }
    ]
    
    print("🌍 Генерируем дашборды с поддержкой сравнения аэропортов...")
    
    for country in countries:
        print(f"\n📊 Обрабатываем: {country['name']}")
        
        # Проверяем существование файлов
        if not os.path.exists(country['data_file']):
            print(f"❌ Файл данных не найден: {country['data_file']}")
            continue
        
        if not os.path.exists(country['airport_comparison_file']):
            print(f"⚠️ Файл сравнения аэропортов не найден: {country['airport_comparison_file']}")
            # Генерируем дашборд без сравнения аэропортов
            airport_comparison_file = None
        else:
            airport_comparison_file = country['airport_comparison_file']
        
        try:
            generate_dashboard_with_airport_comparison(
                data_file=country['data_file'],
                output_file=country['output_file'],
                title=country['title'],
                airport_comparison_file=airport_comparison_file
            )
            print(f"✅ Дашборд создан: {country['output_file']}")
        except Exception as e:
            print(f"❌ Ошибка создания дашборда для {country['name']}: {e}")
    
    # Создаем главную страницу с ссылками на все дашборды
    create_main_landing_page()

def create_main_landing_page():
    """Создает главную страницу с ссылками на все дашборды"""
    
    tiles = [
        {
            'href': 'index_greece_airports.html',
            'title': 'Греция',
            'subtitle': 'Сравнение аэропортов'
        },
        {
            'href': 'index_egypt_airports.html',
            'title': 'Египет',
            'subtitle': 'Сравнение аэропортов'
        },
        {
            'href': 'index_turkey_airports.html',
            'title': 'Турция',
            'subtitle': 'Сравнение аэропортов'
        }
    ]
    
    html_content = f"""
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Travel Price Monitor • Сравнение аэропортов</title>
    <style>
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}
        
        body {{
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
        }}
        
        .container {{
            max-width: 1200px;
            padding: 40px;
            text-align: center;
        }}
        
        .header {{
            margin-bottom: 50px;
        }}
        
        .header h1 {{
            font-size: 3em;
            color: white;
            margin-bottom: 20px;
            text-shadow: 0 4px 8px rgba(0, 0, 0, 0.3);
        }}
        
        .header p {{
            font-size: 1.2em;
            color: rgba(255, 255, 255, 0.9);
            margin-bottom: 10px;
        }}
        
        .tiles {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
            gap: 30px;
            margin-top: 40px;
        }}
        
        .tile {{
            background: rgba(255, 255, 255, 0.95);
            backdrop-filter: blur(10px);
            border-radius: 20px;
            padding: 40px;
            text-decoration: none;
            color: #333;
            transition: all 0.3s ease;
            box-shadow: 0 20px 40px rgba(0, 0, 0, 0.1);
        }}
        
        .tile:hover {{
            transform: translateY(-10px);
            box-shadow: 0 30px 60px rgba(0, 0, 0, 0.2);
        }}
        
        .tile h2 {{
            font-size: 2em;
            margin-bottom: 15px;
            background: linear-gradient(135deg, #667eea, #764ba2);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
        }}
        
        .tile p {{
            font-size: 1.1em;
            color: #666;
            margin-bottom: 20px;
        }}
        
        .tile-icon {{
            font-size: 3em;
            margin-bottom: 20px;
        }}
        
        .features {{
            background: rgba(255, 255, 255, 0.1);
            backdrop-filter: blur(10px);
            border-radius: 20px;
            padding: 30px;
            margin-top: 50px;
            color: white;
        }}
        
        .features h3 {{
            font-size: 1.8em;
            margin-bottom: 20px;
        }}
        
        .features ul {{
            list-style: none;
            text-align: left;
            max-width: 600px;
            margin: 0 auto;
        }}
        
        .features li {{
            padding: 10px 0;
            font-size: 1.1em;
        }}
        
        .features li:before {{
            content: "✅ ";
            margin-right: 10px;
        }}
        
        @media (max-width: 768px) {{
            .container {{
                padding: 20px;
            }}
            
            .header h1 {{
                font-size: 2.5em;
            }}
            
            .tiles {{
                grid-template-columns: 1fr;
            }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🛫 Travel Price Monitor</h1>
            <p>Сравнение аэропортов вылета</p>
            <p>Последнее обновление: {datetime.now().strftime('%d.%m.%Y %H:%M')}</p>
        </div>
        
        <div class="tiles">
"""
    
    for tile in tiles:
        html_content += f"""
            <a href="{tile['href']}" class="tile">
                <div class="tile-icon">🏖️</div>
                <h2>{tile['title']}</h2>
                <p>{tile['subtitle']}</p>
            </a>
"""
    
    html_content += """
        </div>
        
        <div class="features">
            <h3>🎯 Возможности системы</h3>
            <ul>
                <li>Сравнение цен из разных аэропортов вылета</li>
                <li>Поиск отелей, отсутствующих в Варшаве</li>
                <li>Анализ экономии при вылете из других городов</li>
                <li>Интерактивные графики и таблицы</li>
                <li>Фильтрация и поиск по отелям</li>
                <li>Автоматическое обновление данных</li>
            </ul>
        </div>
    </div>
</body>
</html>
"""
    
    try:
        with open('index_airports.html', 'w', encoding='utf-8') as f:
            f.write(html_content)
        print(f"✅ Главная страница создана: index_airports.html")
    except Exception as e:
        print(f"❌ Ошибка создания главной страницы: {e}")

if __name__ == "__main__":
    from datetime import datetime
    generate_all_dashboards()


