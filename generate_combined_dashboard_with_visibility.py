#!/usr/bin/env python3
"""
Генерация объединенного дашборда всех стран с фильтрацией видимости
"""

import pandas as pd
import os
from offer_visibility_manager import OfferVisibilityManager

def generate_combined_dashboard_with_visibility():
    """Генерирует объединенный дашборд всех стран с фильтрацией видимости"""
    
    countries = ['greece', 'egypt', 'turkey']
    all_data = []
    
    # Загружаем данные всех стран
    for country in countries:
        data_file = f'data/{country}_travel_prices.csv'
        if os.path.exists(data_file):
            print(f"📊 Загрузка данных {country}...")
            try:
                df = pd.read_csv(data_file, quoting=1, on_bad_lines='skip')
                
                # Обрабатываем время для каждого датасета
                raw = df['scraped_at'].astype(str)
                mask_tz = raw.str.contains(r"Z$|[+-]\d{2}:\d{2}$", regex=True)
                tz_series = pd.to_datetime(raw.where(mask_tz), errors='coerce', utc=True)
                tz_series = tz_series.dt.tz_convert('Europe/Warsaw')
                naive_series = pd.to_datetime(raw.where(~mask_tz), errors='coerce')
                try:
                    naive_series = naive_series.dt.tz_localize('Europe/Warsaw')
                except Exception:
                    pass
                df['scraped_at_local'] = tz_series.combine_first(naive_series)
                df = df.dropna(subset=['scraped_at_local'])
                df['scraped_at_display'] = df['scraped_at_local']
                
                df['country'] = country
                all_data.append(df)
                print(f"✅ Загружено {len(df)} записей для {country}")
            except Exception as e:
                print(f"❌ Ошибка загрузки {country}: {e}")
        else:
            print(f"⚠️ Файл {data_file} не найден")
    
    if not all_data:
        print("❌ Нет данных для генерации дашборда")
        return
    
    # Объединяем все данные
    combined_df = pd.concat(all_data, ignore_index=True)
    print(f"📈 Объединено {len(combined_df)} записей из {len(all_data)} стран")
    
    # Сохраняем объединенные данные
    combined_file = 'data/combined_travel_prices.csv'
    combined_df.to_csv(combined_file, index=False, quoting=1)
    print(f"💾 Сохранены объединенные данные: {combined_file}")
    
    # Инициализируем менеджер видимости для объединенных данных
    visibility_manager = OfferVisibilityManager(combined_file)
    
    # Обновляем видимость предложений
    print("🔄 Обновление видимости предложений...")
    visibility_manager.update_visibility(combined_df)
    
    # Фильтруем данные
    print("🔍 Фильтрация видимых предложений...")
    df_visible = visibility_manager.filter_visible_offers(combined_df)
    
    # Получаем статистику видимости
    visibility_stats = visibility_manager.get_visibility_stats()
    print(f"📊 Статистика видимости: {visibility_stats['visible_count']} видимых, {visibility_stats['hidden_count']} скрытых предложений")
    
    # Сохраняем отфильтрованные данные
    filtered_file = 'data/combined_travel_prices_filtered.csv'
    df_visible.to_csv(filtered_file, index=False, quoting=1)
    print(f"💾 Сохранены отфильтрованные данные: {filtered_file}")
    
    # Генерируем дашборд с исправленной фильтрацией
    from generate_dashboard_with_visibility_fix import generate_dashboard_with_visibility_fix
    
    generate_dashboard_with_visibility_fix(
        data_file=filtered_file,
        output_file='index_combined_filtered.html',
        title='Travel Price Monitor • Объединенный дашборд всех стран',
        all_airports_data_file='data/greece_any_airports.csv',  # Используем данные всех аэропортов
        airport_comparison_file='data/greece_airport_comparison.json'  # Используем данные сравнения
    )
    
    print("✅ Объединенный дашборд с фильтрацией видимости сгенерирован: index_combined_filtered.html")

def main():
    """Основная функция"""
    print("🚀 Генерация объединенного дашборда с фильтрацией видимости...")
    generate_combined_dashboard_with_visibility()

if __name__ == "__main__":
    main()
