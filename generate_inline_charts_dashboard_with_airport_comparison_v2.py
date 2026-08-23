#!/usr/bin/env python3
"""
Дашборд с встроенными графиками и поддержкой сравнения аэропортов
Основан на оригинальном коде с минимальными изменениями
"""

import pandas as pd
import json
import csv
from datetime import datetime, timedelta, timezone
import os
import re
from urllib.parse import urlparse, parse_qs

def generate_inline_charts_dashboard_with_airport_comparison_v2(data_file: str = 'data/travel_prices.csv', output_file: str = 'index.html', title: str = 'Travel Price Monitor • Расширенный дашборд', charts_subdir: str = 'hotel-charts', tz: str = 'Europe/Warsaw', alerts_file: str = None, airport_comparison_file: str = None):
    """Генерирует дашборд с встроенными графиками и поддержкой сравнения аэропортов"""
    
    # Загружаем данные
    try:
        df = pd.read_csv(data_file, quoting=csv.QUOTE_ALL, on_bad_lines='skip')
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
    
    # Загружаем данные сравнения аэропортов
    airport_comparison_data = None
    if airport_comparison_file and os.path.exists(airport_comparison_file):
        try:
            with open(airport_comparison_file, 'r', encoding='utf-8') as f:
                airport_comparison_data = json.load(f)
            print(f"✅ Загружены данные сравнения аэропортов")
        except Exception as e:
            print(f"⚠️ Ошибка загрузки данных сравнения аэропортов: {e}")
    
    # Вычисляем статистику
    total_offers = len(df)
    unique_hotels = df['hotel_name'].nunique()
    avg_price = df['price'].mean()
    min_price = df['price'].min()
    max_price = df['price'].max()

    # Функция для генерации hover-данных с использованием встроенных возможностей Plotly
    def create_hover_data(row):
        return f"Отель: {row['hotel_name']}<br>Цена: {row['price']:.0f} PLN<br>Даты: {row.get('dates', 'N/A')}<br>Время: {row['scraped_at_display'].strftime('%d.%m.%Y %H:%M')}"

    # Получаем последнее обновление
    last_update = df['scraped_at_display'].max()
    if pd.isna(last_update):
        last_update = datetime.now(timezone.utc).astimezone()

    # Загружаем алерты если есть
    alerts_data = []
    if alerts_file and os.path.exists(alerts_file):
        try:
            with open(alerts_file, 'r', encoding='utf-8') as f:
                alerts_data = json.load(f)
        except Exception as e:
            print(f"⚠️ Ошибка загрузки алертов: {e}")

    # Создаем HTML - используем оригинальный код с минимальными изменениями
    html_content = f"""
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>
    <script src="https://cdn.plot.ly/plotly-latest.min.js"></script>
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
            color: #333;
        }}
        
        .container {{
            max-width: 1400px;
            margin: 0 auto;
            padding: 20px;
        }}
        
        .header {{
            background: rgba(255, 255, 255, 0.95);
            backdrop-filter: blur(10px);
            border-radius: 20px;
            padding: 30px;
            margin-bottom: 30px;
            box-shadow: 0 20px 40px rgba(0, 0, 0, 0.1);
            text-align: center;
        }}
        
        .header h1 {{
            font-size: 2.5em;
            margin-bottom: 10px;
            background: linear-gradient(135deg, #667eea, #764ba2);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
        }}
        
        .stats {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 20px;
            margin: 30px 0;
        }}
        
        .stat-card {{
            background: rgba(255, 255, 255, 0.9);
            padding: 20px;
            border-radius: 15px;
            text-align: center;
            box-shadow: 0 10px 30px rgba(0, 0, 0, 0.1);
            transition: transform 0.3s ease;
        }}
        
        .stat-card:hover {{
            transform: translateY(-5px);
        }}
        
        .stat-value {{
            font-size: 2em;
            font-weight: bold;
            color: #667eea;
            margin-bottom: 5px;
        }}
        
        .stat-label {{
            color: #666;
            font-size: 0.9em;
        }}
        
        .charts-section {{
            background: rgba(255, 255, 255, 0.95);
            backdrop-filter: blur(10px);
            border-radius: 20px;
            padding: 30px;
            margin-bottom: 30px;
            box-shadow: 0 20px 40px rgba(0, 0, 0, 0.1);
        }}
        
        .section-title {{
            font-size: 1.8em;
            margin-bottom: 20px;
            color: #333;
            border-bottom: 3px solid #667eea;
            padding-bottom: 10px;
        }}
        
        .chart-container {{
            margin: 30px 0;
            background: white;
            border-radius: 15px;
            padding: 20px;
            box-shadow: 0 10px 30px rgba(0, 0, 0, 0.1);
        }}
        
        /* Стили для секции сравнения аэропортов */
        .airport-comparison {{
            background: rgba(255, 255, 255, 0.95);
            backdrop-filter: blur(10px);
            border-radius: 20px;
            padding: 30px;
            margin-bottom: 30px;
            box-shadow: 0 20px 40px rgba(0, 0, 0, 0.1);
        }}
        
        .comparison-stats {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 20px;
            margin: 20px 0;
        }}
        
        .comparison-card {{
            background: linear-gradient(135deg, #667eea, #764ba2);
            color: white;
            padding: 20px;
            border-radius: 15px;
            text-align: center;
        }}
        
        .comparison-value {{
            font-size: 2em;
            font-weight: bold;
            margin-bottom: 10px;
        }}
        
        .comparison-label {{
            font-size: 1em;
            opacity: 0.9;
        }}
        
        .hotels-table {{
            background: rgba(255, 255, 255, 0.95);
            backdrop-filter: blur(10px);
            border-radius: 20px;
            padding: 30px;
            margin-bottom: 30px;
            box-shadow: 0 20px 40px rgba(0, 0, 0, 0.1);
        }}
        
        .table-filters {{
            display: flex;
            gap: 15px;
            margin-bottom: 20px;
            flex-wrap: wrap;
            align-items: center;
        }}
        
        .filter-input {{
            padding: 10px 15px;
            border: 2px solid #e0e0e0;
            border-radius: 10px;
            font-size: 14px;
            transition: border-color 0.3s ease;
        }}
        
        .filter-input:focus {{
            outline: none;
            border-color: #667eea;
        }}
        
        .filter-select {{
            padding: 10px 15px;
            border: 2px solid #e0e0e0;
            border-radius: 10px;
            font-size: 14px;
            background: white;
            cursor: pointer;
        }}
        
        .btn {{
            padding: 10px 20px;
            background: linear-gradient(135deg, #667eea, #764ba2);
            color: white;
            border: none;
            border-radius: 10px;
            cursor: pointer;
            font-size: 14px;
            transition: transform 0.3s ease;
        }}
        
        .btn:hover {{
            transform: translateY(-2px);
        }}
        
        table {{
            width: 100%;
            border-collapse: collapse;
            margin-top: 20px;
        }}
        
        th, td {{
            padding: 15px;
            text-align: left;
            border-bottom: 1px solid #e0e0e0;
        }}
        
        th {{
            background: linear-gradient(135deg, #667eea, #764ba2);
            color: white;
            font-weight: bold;
        }}
        
        tr:hover {{
            background: rgba(102, 126, 234, 0.1);
        }}
        
        .price {{
            font-weight: bold;
            color: #667eea;
        }}
        
        .change-positive {{
            color: #e74c3c;
        }}
        
        .change-negative {{
            color: #27ae60;
        }}
        
        .change-neutral {{
            color: #95a5a6;
        }}
        
        .pagination {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-top: 20px;
            padding: 20px;
            background: rgba(102, 126, 234, 0.1);
            border-radius: 10px;
        }}
        
        .pagination button {{
            padding: 10px 20px;
            margin: 0 5px;
            border: none;
            border-radius: 5px;
            cursor: pointer;
            background: #667eea;
            color: white;
        }}
        
        .pagination button:disabled {{
            background: #bdc3c7;
            cursor: not-allowed;
        }}
        
        /* Стили для информации об аэропортах */
        .airport-info {{
            background: #f8f9fa;
            padding: 8px 12px;
            border-radius: 6px;
            margin: 2px 0;
            border-left: 3px solid #667eea;
            font-size: 0.9em;
        }}
        
        .airport-savings {{
            background: #d4edda;
            padding: 8px 12px;
            border-radius: 6px;
            margin: 2px 0;
            border-left: 3px solid #28a745;
            font-size: 0.9em;
        }}
        
        .missing-hotels {{
            background: #fff3cd;
            padding: 8px 12px;
            border-radius: 6px;
            margin: 2px 0;
            border-left: 3px solid #ffc107;
            font-size: 0.9em;
        }}
        
        @media (max-width: 768px) {{
            .container {{
                padding: 10px;
            }}
            
            .header h1 {{
                font-size: 2em;
            }}
            
            .stats {{
                grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
            }}
            
            .table-filters {{
                flex-direction: column;
                align-items: stretch;
            }}
            
            .filter-input, .filter-select, .btn {{
                width: 100%;
                margin-bottom: 10px;
            }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🛫 {title}</h1>
            <p>Последнее обновление: {last_update.strftime('%d.%m.%Y %H:%M')}</p>
        </div>
        
        <div class="stats">
            <div class="stat-card">
                <div class="stat-value">{total_offers:,}</div>
                <div class="stat-label">Всего предложений</div>
            </div>
            <div class="stat-card">
                <div class="stat-value">{unique_hotels}</div>
                <div class="stat-label">Уникальных отелей</div>
            </div>
            <div class="stat-card">
                <div class="stat-value">{avg_price:.0f} PLN</div>
                <div class="stat-label">Средняя цена</div>
            </div>
            <div class="stat-card">
                <div class="stat-value">{min_price:.0f} PLN</div>
                <div class="stat-label">Минимальная цена</div>
            </div>
        </div>
"""
    
    # Добавляем секцию сравнения аэропортов, если есть данные
    if airport_comparison_data:
        html_content += f"""
        <div class="airport-comparison">
            <h2 class="section-title">🛫 Сравнение аэропортов</h2>
            <div class="comparison-stats">
                <div class="comparison-card">
                    <div class="comparison-value">{airport_comparison_data.get('warsaw_hotels_count', 0)}</div>
                    <div class="comparison-label">Отелей в Варшаве</div>
                </div>
                <div class="comparison-card">
                    <div class="comparison-value">{airport_comparison_data.get('any_airports_hotels_count', 0)}</div>
                    <div class="comparison-label">Отелей в любых аэропортах</div>
                </div>
                <div class="comparison-card">
                    <div class="comparison-value">{airport_comparison_data.get('missing_in_warsaw_count', 0)}</div>
                    <div class="comparison-label">Отсутствует в Варшаве</div>
                </div>
                <div class="comparison-card">
                    <div class="comparison-value">{airport_comparison_data.get('cheaper_alternatives_count', 0)}</div>
                    <div class="comparison-label">Дешевле из других аэропортов</div>
                </div>
            </div>
        </div>
"""
    
    # Добавляем графики - используем оригинальный код
    html_content += """
        <div class="charts-section">
            <h2 class="section-title">📊 Анализ цен</h2>
            
            <div class="chart-container">
                <h3>Динамика цен по времени</h3>
                <div id="priceTimeline"></div>
            </div>
            
            <div class="chart-container">
                <h3>Распределение цен</h3>
                <div id="priceDistribution"></div>
            </div>
            
            <div class="chart-container">
                <h3>Топ отелей по количеству предложений</h3>
                <div id="topHotels"></div>
            </div>
        </div>
"""
    
    # Добавляем таблицу отелей - используем оригинальный код с добавлением колонок
    html_content += f"""
        <div class="hotels-table">
            <h2 class="section-title">🏨 Отели и предложения</h2>
            
            <div class="table-filters">
                <input type="text" id="searchInput" class="filter-input" placeholder="Поиск по названию отеля...">
                <select id="priceFilter" class="filter-select">
                    <option value="">Все цены</option>
                    <option value="0-2000">0-2000 PLN</option>
                    <option value="2000-3000">2000-3000 PLN</option>
                    <option value="3000-4000">3000-4000 PLN</option>
                    <option value="4000+">4000+ PLN</option>
                </select>
                <select id="changeFilter" class="filter-select">
                    <option value="">Все изменения</option>
                    <option value="decrease">Снижение</option>
                    <option value="increase">Рост</option>
                    <option value="stable">Стабильно</option>
                </select>
                <button id="clearFilters" class="btn">Очистить фильтры</button>
            </div>
            
            <table id="hotelsTable">
                <thead>
                    <tr>
                        <th>Отель</th>
                        <th>Цена</th>
                        <th>Изменение (48ч)</th>
                        <th>Даты</th>
                        <th>Длительность</th>
                        <th>Аэропорт вылета</th>
                        <th>Лучшее предложение</th>
                    </tr>
                </thead>
                <tbody>
"""
    
    # Генерируем строки таблицы - используем оригинальный код с добавлением колонок
    for _, row in df.iterrows():
        hotel_name = row['hotel_name']
        price = row['price']
        dates = row.get('dates', '')
        duration = row.get('duration', '')
        departure_airport = row.get('departure_airport', 'Warszawa')
        
        # Вычисляем изменение цены (упрощенно)
        change_48h = "—"
        
        # Ищем лучшее предложение из других аэропортов
        best_alternative = ""
        if airport_comparison_data:
            for alt in airport_comparison_data.get('cheaper_alternatives', []):
                if alt['hotel_name'] == hotel_name:
                    best_alternative = f"""
                        <div class="airport-savings">
                            <strong>💰 Экономия {alt['savings']:.0f} PLN ({alt['savings_percent']:.1f}%)</strong><br>
                            <small>Из {alt['best_departure_airport']}: {alt['best_other_price']:.0f} PLN</small>
                        </div>
                    """
                    break
        
        html_content += f"""
                    <tr>
                        <td>{hotel_name}</td>
                        <td class="price">{price:.0f} PLN</td>
                        <td class="change-neutral">{change_48h}</td>
                        <td>{dates}</td>
                        <td>{duration}</td>
                        <td>{departure_airport}</td>
                        <td>{best_alternative}</td>
                    </tr>
"""
    
    html_content += """
                </tbody>
            </table>
            
            <div class="pagination">
                <div>
                    <span>Показано <span id="showingFrom">1</span>-<span id="showingTo">50</span> из <span id="totalItems">0</span> записей</span>
                </div>
                <div>
                    <button id="prevPage" onclick="prevPageFunc()">← Предыдущая</button>
                    <button id="nextPage" onclick="nextPageFunc()">Следующая →</button>
                </div>
            </div>
        </div>
    </div>
    
    <script>
        // Данные для графиков - используем оригинальный код
        const priceData = """ + df.to_json(orient='records') + """;
        
        // График динамики цен - используем оригинальный код
        const timelineData = [{
            x: priceData.map(d => d.scraped_at_display),
            y: priceData.map(d => d.price),
            type: 'scatter',
            mode: 'markers',
            marker: {
                color: priceData.map(d => d.price),
                colorscale: 'Viridis',
                size: 8,
                opacity: 0.7
            },
            text: priceData.map(d => `${d.hotel_name}<br>${d.price} PLN`),
            hovertemplate: '<b>%{text}</b><br>Время: %{x}<br>Цена: %{y} PLN<extra></extra>'
        }];
        
        const timelineLayout = {
            title: 'Динамика цен по времени',
            xaxis: { title: 'Время' },
            yaxis: { title: 'Цена (PLN)' },
            hovermode: 'closest'
        };
        
        Plotly.newPlot('priceTimeline', timelineData, timelineLayout);
        
        // График распределения цен - используем оригинальный код
        const distributionData = [{
            x: priceData.map(d => d.price),
            type: 'histogram',
            nbinsx: 30,
            marker: {
                color: '#667eea',
                opacity: 0.7
            }
        }];
        
        const distributionLayout = {
            title: 'Распределение цен',
            xaxis: { title: 'Цена (PLN)' },
            yaxis: { title: 'Количество предложений' }
        };
        
        Plotly.newPlot('priceDistribution', distributionData, distributionLayout);
        
        // График топ отелей - используем оригинальный код
        const hotelCounts = {};
        priceData.forEach(d => {
            hotelCounts[d.hotel_name] = (hotelCounts[d.hotel_name] || 0) + 1;
        });
        
        const topHotels = Object.entries(hotelCounts)
            .sort((a, b) => b[1] - a[1])
            .slice(0, 15);
        
        const topHotelsData = [{
            x: topHotels.map(h => h[1]),
            y: topHotels.map(h => h[0]),
            type: 'bar',
            orientation: 'h',
            marker: {
                color: '#667eea',
                opacity: 0.7
            }
        }];
        
        const topHotelsLayout = {
            title: 'Топ отелей по количеству предложений',
            xaxis: { title: 'Количество предложений' },
            yaxis: { title: 'Отель' }
        };
        
        Plotly.newPlot('topHotels', topHotelsData, topHotelsLayout);
        
        // Фильтрация таблицы - используем оригинальный код
        const searchInput = document.getElementById('searchInput');
        const priceFilter = document.getElementById('priceFilter');
        const changeFilter = document.getElementById('changeFilter');
        const clearFilters = document.getElementById('clearFilters');
        const table = document.getElementById('hotelsTable');
        const tbody = table.querySelector('tbody');
        const rows = Array.from(tbody.querySelectorAll('tr'));
        const prevPage = document.getElementById('prevPage');
        const nextPage = document.getElementById('nextPage');
        const showingFrom = document.getElementById('showingFrom');
        const showingTo = document.getElementById('showingTo');
        const totalItems = document.getElementById('totalItems');
        
        let currentPage = 1;
        const itemsPerPage = 50;
        let filteredRows = [...rows];
        
        function filterRows() {
            const searchTerm = searchInput.value.toLowerCase();
            const priceRange = priceFilter.value;
            const changeType = changeFilter.value;
            
            filteredRows = rows.filter(row => {
                const hotelName = row.cells[0].textContent.toLowerCase();
                const price = parseFloat(row.cells[1].textContent.replace(/[^0-9.-]/g, ''));
                const delta48 = row.cells[2].textContent.trim();
                
                if (searchTerm && !hotelName.includes(searchTerm)) {
                    return false;
                }
                
                if (priceRange) {
                    if (priceRange === '0-2000' && price > 2000) return false;
                    if (priceRange === '2000-3000' && (price < 2000 || price > 3000)) return false;
                    if (priceRange === '3000-4000' && (price < 3000 || price > 4000)) return false;
                    if (priceRange === '4000+' && price < 4000) return false;
                }
                
                if (changeType) {
                    if (changeType === 'decrease' && !delta48.includes('-')) return false;
                    if (changeType === 'increase' && !delta48.includes('+')) return false;
                    if (changeType === 'stable' && delta48 !== '—') return false;
                }
                
                return true;
            });
            
            currentPage = 1;
            updateTable();
        }
        
        function updateTable() {
            const startIndex = (currentPage - 1) * itemsPerPage;
            const endIndex = startIndex + itemsPerPage;
            const pageRows = filteredRows.slice(startIndex, endIndex);
            
            tbody.innerHTML = '';
            pageRows.forEach(row => tbody.appendChild(row));
            
            showingFrom.textContent = filteredRows.length > 0 ? startIndex + 1 : 0;
            showingTo.textContent = Math.min(endIndex, filteredRows.length);
            totalItems.textContent = filteredRows.length;
            
            prevPage.disabled = currentPage === 1;
            nextPage.disabled = endIndex >= filteredRows.length;
        }
        
        function nextPageFunc() {
            const maxPage = Math.ceil(filteredRows.length / itemsPerPage);
            if (currentPage < maxPage) {
                currentPage++;
                updateTable();
            }
        }
        
        function prevPageFunc() {
            if (currentPage > 1) {
                currentPage--;
                updateTable();
            }
        }
        
        // Обработчики событий
        searchInput.addEventListener('input', filterRows);
        priceFilter.addEventListener('change', filterRows);
        changeFilter.addEventListener('change', filterRows);
        clearFilters.addEventListener('click', () => {
            searchInput.value = '';
            priceFilter.value = '';
            changeFilter.value = '';
            filterRows();
        });
        
        // Инициализация
        updateTable();
    </script>
</body>
</html>
"""
    
    # Сохраняем файл
    try:
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(html_content)
        print(f"✅ Дашборд сохранен: {output_file}")
    except Exception as e:
        print(f"❌ Ошибка сохранения дашборда: {e}")

def main():
    """Главная функция"""
    import argparse
    parser = argparse.ArgumentParser(description='Generate inline charts dashboard with airport comparison v2')
    parser.add_argument('--data-file', default='data/travel_prices.csv')
    parser.add_argument('--output', default='index.html')
    parser.add_argument('--title', default='Travel Price Monitor • Расширенный дашборд')
    parser.add_argument('--charts-dir', default='hotel-charts')
    parser.add_argument('--tz', default='Europe/Warsaw')
    parser.add_argument('--alerts-file', default=None)
    parser.add_argument('--airport-comparison-file', default=None, help='JSON файл с результатами сравнения аэропортов')
    args = parser.parse_args()
    
    generate_inline_charts_dashboard_with_airport_comparison_v2(
        data_file=args.data_file,
        output_file=args.output,
        title=args.title,
        charts_subdir=args.charts_dir,
        tz=args.tz,
        alerts_file=args.alerts_file,
        airport_comparison_file=args.airport_comparison_file
    )

if __name__ == "__main__":
    main()


