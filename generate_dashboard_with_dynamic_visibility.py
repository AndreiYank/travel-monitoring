#!/usr/bin/env python3
"""
Дашборд с динамической видимостью предложений - скрывает предложения, которых нет в последнем актуальном ране
"""

import pandas as pd
import json
import csv
from datetime import datetime, timedelta, timezone
import os
import re
from urllib.parse import urlparse, parse_qs
from offer_visibility_manager import OfferVisibilityManager

def generate_dashboard_with_dynamic_visibility(data_file: str = 'data/travel_prices.csv', 
                                             output_file: str = 'index.html', 
                                             title: str = 'Travel Price Monitor • Расширенный дашборд с динамической видимостью', 
                                             charts_subdir: str = 'hotel-charts', 
                                             tz: str = 'Europe/Warsaw', 
                                             alerts_file: str = None, 
                                             all_airports_data_file: str = None, 
                                             airport_comparison_file: str = None):
    """Генерирует дашборд с динамической видимостью предложений"""
    
    # Инициализируем менеджер видимости
    visibility_manager = OfferVisibilityManager(data_file)
    
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
    
    # Обновляем видимость предложений на основе последнего рана
    print("🔄 Обновление видимости предложений...")
    visibility_manager.update_visibility(df)
    
    # Фильтруем данные, оставляя только видимые предложения
    print("🔍 Фильтрация видимых предложений...")
    df_visible = visibility_manager.filter_visible_offers(df)
    
    # Получаем статистику видимости
    visibility_stats = visibility_manager.get_visibility_stats()
    print(f"📊 Статистика видимости: {visibility_stats['visible_count']} видимых, {visibility_stats['hidden_count']} скрытых предложений")
    
    # Откат фичи сравнения аэропортов: не используем общий датасет
    df_all_airports = None
    
    # Загружаем данные сравнения аэропортов
    airport_comparison_data = None
    if airport_comparison_file and os.path.exists(airport_comparison_file):
        try:
            with open(airport_comparison_file, 'r', encoding='utf-8') as f:
                airport_comparison_data = json.load(f)
            print(f"✅ Загружены данные сравнения аэропортов")
        except Exception as e:
            print(f"⚠️ Ошибка загрузки данных сравнения аэропортов: {e}")
    
    # Загружаем данные всех аэропортов (если есть)
    if all_airports_data_file and os.path.exists(all_airports_data_file):
        try:
            df_all_airports = pd.read_csv(all_airports_data_file, quoting=csv.QUOTE_ALL, on_bad_lines='skip')
            # Применяем ту же обработку времени
            raw_all = df_all_airports['scraped_at'].astype(str)
            mask_tz_all = raw_all.str.contains(r"Z$|[+-]\d{2}:\d{2}$", regex=True)
            tz_series_all = pd.to_datetime(raw_all.where(mask_tz_all), errors='coerce', utc=True)
            tz_series_all = tz_series_all.dt.tz_convert(tz)
            naive_series_all = pd.to_datetime(raw_all.where(~mask_tz_all), errors='coerce')
            try:
                naive_series_all = naive_series_all.dt.tz_localize(tz)
            except Exception:
                pass
            df_all_airports['scraped_at_local'] = tz_series_all.combine_first(naive_series_all)
            df_all_airports = df_all_airports.dropna(subset=['scraped_at_local'])
            df_all_airports['scraped_at_display'] = df_all_airports['scraped_at_local']
            
            # Применяем фильтрацию видимости и к данным всех аэропортов
            df_all_airports_visible = visibility_manager.filter_visible_offers(df_all_airports)
            print(f"✅ Загружены данные всех аэропортов: {len(df_all_airports_visible)} видимых записей")
        except Exception as e:
            print(f"⚠️ Ошибка загрузки данных всех аэропортов: {e}")
            df_all_airports = None
    
    # Используем отфильтрованные данные для генерации дашборда
    df = df_visible
    
    # Остальная логика генерации дашборда остается такой же, как в оригинальном файле
    # но с использованием отфильтрованных данных
    
    # ... (здесь будет скопирована основная логика из оригинального файла)
    
    # Для начала создадим упрощенную версию дашборда с информацией о видимости
    html_content = generate_simplified_dashboard_html(df, visibility_stats, title)
    
    # Сохраняем дашборд
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(html_content)
    
    print(f"✅ Дашборд с динамической видимостью сохранен: {output_file}")

def generate_simplified_dashboard_html(df: pd.DataFrame, visibility_stats: dict, title: str) -> str:
    """Генерирует упрощенный HTML дашборд с информацией о видимости"""
    
    # Время последнего обновления
    try:
        updated_str = df['scraped_at_display'].max().strftime('%d.%m.%Y %H:%M')
    except Exception:
        updated_str = datetime.now().strftime('%d.%m.%Y %H:%M')
    
    # Статистика по отелям
    hotel_stats = df.groupby('hotel_name').agg({
        'price': ['min', 'max', 'mean', 'count'],
        'scraped_at_display': 'max'
    }).round(2)
    
    hotel_stats.columns = ['min_price', 'max_price', 'avg_price', 'offers_count', 'last_seen']
    hotel_stats = hotel_stats.sort_values('min_price')
    
    html = f"""
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            margin: 0;
            padding: 20px;
            background-color: #f5f5f7;
            color: #1d1d1f;
        }}
        .container {{
            max-width: 1200px;
            margin: 0 auto;
        }}
        .header {{
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 30px;
            border-radius: 12px;
            margin-bottom: 30px;
            box-shadow: 0 8px 32px rgba(0,0,0,0.1);
        }}
        .header h1 {{
            margin: 0 0 10px 0;
            font-size: 2.5em;
            font-weight: 700;
        }}
        .header p {{
            margin: 0;
            opacity: 0.9;
            font-size: 1.1em;
        }}
        .stats-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
        }}
        .stat-card {{
            background: white;
            padding: 25px;
            border-radius: 12px;
            box-shadow: 0 4px 20px rgba(0,0,0,0.08);
            border-left: 4px solid #667eea;
        }}
        .stat-card h3 {{
            margin: 0 0 10px 0;
            color: #667eea;
            font-size: 1.1em;
            font-weight: 600;
        }}
        .stat-value {{
            font-size: 2em;
            font-weight: 700;
            color: #1d1d1f;
            margin: 0;
        }}
        .visibility-info {{
            background: #e8f4fd;
            border: 1px solid #b3d9ff;
            border-radius: 8px;
            padding: 20px;
            margin-bottom: 30px;
        }}
        .visibility-info h3 {{
            margin: 0 0 15px 0;
            color: #0066cc;
        }}
        .visibility-stats {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 15px;
        }}
        .visibility-stat {{
            text-align: center;
        }}
        .visibility-stat .label {{
            font-size: 0.9em;
            color: #666;
            margin-bottom: 5px;
        }}
        .visibility-stat .value {{
            font-size: 1.5em;
            font-weight: 600;
            color: #0066cc;
        }}
        .hotels-table {{
            background: white;
            border-radius: 12px;
            overflow: hidden;
            box-shadow: 0 4px 20px rgba(0,0,0,0.08);
        }}
        .hotels-table h3 {{
            margin: 0;
            padding: 20px;
            background: #f8f9fa;
            border-bottom: 1px solid #e9ecef;
            color: #495057;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
        }}
        th, td {{
            padding: 12px 20px;
            text-align: left;
            border-bottom: 1px solid #e9ecef;
        }}
        th {{
            background: #f8f9fa;
            font-weight: 600;
            color: #495057;
        }}
        tr:hover {{
            background: #f8f9fa;
        }}
        .price {{
            font-weight: 600;
            color: #28a745;
        }}
        .last-seen {{
            font-size: 0.9em;
            color: #6c757d;
        }}
        .footer {{
            text-align: center;
            margin-top: 40px;
            padding: 20px;
            color: #6c757d;
            font-size: 0.9em;
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>{title}</h1>
            <p>Последнее обновление: {updated_str}</p>
        </div>
        
        <div class="stats-grid">
            <div class="stat-card">
                <h3>Всего отелей</h3>
                <p class="stat-value">{len(hotel_stats)}</p>
            </div>
            <div class="stat-card">
                <h3>Всего предложений</h3>
                <p class="stat-value">{len(df)}</p>
            </div>
            <div class="stat-card">
                <h3>Средняя цена</h3>
                <p class="stat-value">{df['price'].mean():.0f} zł</p>
            </div>
            <div class="stat-card">
                <h3>Минимальная цена</h3>
                <p class="stat-value">{df['price'].min():.0f} zł</p>
            </div>
        </div>
        
        <div class="visibility-info">
            <h3>🔍 Динамическая видимость предложений</h3>
            <p>Показаны только предложения, которые есть в последнем актуальном ране. Предложения, которых нет в последнем запуске, скрыты и будут показаны снова, когда появятся в новом ране.</p>
            <div class="visibility-stats">
                <div class="visibility-stat">
                    <div class="label">Видимых предложений</div>
                    <div class="value">{visibility_stats['visible_count']}</div>
                </div>
                <div class="visibility-stat">
                    <div class="label">Скрытых предложений</div>
                    <div class="value">{visibility_stats['hidden_count']}</div>
                </div>
                <div class="visibility-stat">
                    <div class="label">Отслеживаемых предложений</div>
                    <div class="value">{visibility_stats['total_tracked_offers']}</div>
                </div>
                <div class="visibility-stat">
                    <div class="label">Последний ран</div>
                    <div class="value">{visibility_stats['last_run_timestamp'][:16] if visibility_stats['last_run_timestamp'] else 'N/A'}</div>
                </div>
            </div>
        </div>
        
        <div class="hotels-table">
            <h3>🏨 Отели (только видимые предложения)</h3>
            <table>
                <thead>
                    <tr>
                        <th>Отель</th>
                        <th>Мин. цена</th>
                        <th>Макс. цена</th>
                        <th>Средняя цена</th>
                        <th>Предложений</th>
                        <th>Последний раз</th>
                    </tr>
                </thead>
                <tbody>
"""
    
    for hotel_name, stats in hotel_stats.head(20).iterrows():
        last_seen = stats['last_seen'].strftime('%d.%m %H:%M') if pd.notna(stats['last_seen']) else 'N/A'
        html += f"""
                    <tr>
                        <td><strong>{hotel_name}</strong></td>
                        <td class="price">{stats['min_price']:.0f} zł</td>
                        <td class="price">{stats['max_price']:.0f} zł</td>
                        <td class="price">{stats['avg_price']:.0f} zł</td>
                        <td>{int(stats['offers_count'])}</td>
                        <td class="last-seen">{last_seen}</td>
                    </tr>
"""
    
    html += """
                </tbody>
            </table>
        </div>
        
        <div class="footer">
            <p>Дашборд с динамической видимостью предложений • Обновляется автоматически</p>
        </div>
    </div>
</body>
</html>
"""
    
    return html

def main():
    """Основная функция для тестирования"""
    import sys
    
    data_file = sys.argv[1] if len(sys.argv) > 1 else 'data/travel_prices.csv'
    output_file = sys.argv[2] if len(sys.argv) > 2 else 'index_dynamic_visibility.html'
    
    print(f"Генерация дашборда с динамической видимостью...")
    print(f"Данные: {data_file}")
    print(f"Выходной файл: {output_file}")
    
    generate_dashboard_with_dynamic_visibility(
        data_file=data_file,
        output_file=output_file,
        title="Travel Price Monitor • Динамическая видимость"
    )

if __name__ == "__main__":
    main()
