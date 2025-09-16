#!/usr/bin/env python3
"""
Дашборд с встроенными графиками и фильтрацией видимости предложений
Использует оригинальную логику генерации, но фильтрует неактуальные предложения
"""

import pandas as pd
import json
import csv
from datetime import datetime, timedelta, timezone
import os
import re
from urllib.parse import urlparse, parse_qs
from offer_visibility_manager import OfferVisibilityManager

def generate_inline_charts_dashboard_with_visibility_filter(data_file: str = 'data/travel_prices.csv', 
                                                          output_file: str = 'index.html', 
                                                          title: str = 'Travel Price Monitor • Расширенный дашборд', 
                                                          charts_subdir: str = 'hotel-charts', 
                                                          tz: str = 'Europe/Warsaw', 
                                                          alerts_file: str = None, 
                                                          all_airports_data_file: str = None, 
                                                          airport_comparison_file: str = None):
    """Генерирует дашборд с встроенными графиками и фильтрацией видимости"""
    
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
    
    # Используем отфильтрованные данные для генерации дашборда
    df = df_visible
    
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
    
    # Теперь импортируем и используем оригинальную функцию генерации дашборда
    # но с отфильтрованными данными
    from generate_inline_charts_dashboard_with_airport_comparison_final import generate_inline_charts_dashboard
    
    # Вызываем оригинальную функцию с отфильтрованными данными
    generate_inline_charts_dashboard(
        data_file=data_file,  # Передаем оригинальный файл, но df уже отфильтрован
        output_file=output_file,
        title=title,
        charts_subdir=charts_subdir,
        tz=tz,
        alerts_file=alerts_file,
        all_airports_data_file=all_airports_data_file,
        airport_comparison_file=airport_comparison_file
    )
    
    # Добавляем информацию о фильтрации видимости в заголовок
    add_visibility_info_to_dashboard(output_file, visibility_stats)

def add_visibility_info_to_dashboard(dashboard_file: str, visibility_stats: dict):
    """Добавляет информацию о фильтрации видимости в дашборд"""
    try:
        with open(dashboard_file, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Находим заголовок и добавляем информацию о фильтрации
        visibility_info = f"""
        <div style="background: #e8f4fd; border: 1px solid #b3d9ff; border-radius: 8px; padding: 15px; margin: 20px 0; font-size: 0.9em;">
            <strong>🔍 Динамическая видимость предложений:</strong> 
            Показаны только актуальные предложения ({visibility_stats['visible_count']} видимых, {visibility_stats['hidden_count']} скрытых). 
            Предложения, которых нет в последнем запуске, автоматически скрыты.
        </div>
        """
        
        # Вставляем информацию после заголовка
        if '<h1' in content:
            content = content.replace('<h1', visibility_info + '<h1', 1)
        else:
            # Если нет h1, добавляем в начало body
            content = content.replace('<body>', '<body>' + visibility_info, 1)
        
        with open(dashboard_file, 'w', encoding='utf-8') as f:
            f.write(content)
            
        print("✅ Добавлена информация о фильтрации видимости в дашборд")
        
    except Exception as e:
        print(f"⚠️ Ошибка добавления информации о видимости: {e}")

def main():
    """Основная функция для тестирования"""
    import sys
    
    data_file = sys.argv[1] if len(sys.argv) > 1 else 'data/travel_prices.csv'
    output_file = sys.argv[2] if len(sys.argv) > 2 else 'index_with_visibility_filter.html'
    
    print(f"Генерация дашборда с фильтрацией видимости...")
    print(f"Данные: {data_file}")
    print(f"Выходной файл: {output_file}")
    
    generate_inline_charts_dashboard_with_visibility_filter(
        data_file=data_file,
        output_file=output_file,
        title="Travel Price Monitor • Расширенный дашборд"
    )

if __name__ == "__main__":
    main()
