#!/usr/bin/env python3
"""
Модуль для сравнения цен на путешествия из разных аэропортов
"""

import pandas as pd
import json
import os
import csv
from datetime import datetime
from typing import List, Dict, Any, Optional
import logging

logger = logging.getLogger(__name__)

class AirportComparison:
    def __init__(self, data_dir: str = "data"):
        self.data_dir = data_dir
        
    def load_data(self, data_file: str) -> pd.DataFrame:
        """Загружает данные из CSV файла"""
        filepath = os.path.join(self.data_dir, data_file)
        logger.info(f"Пытаемся загрузить данные из: {filepath}")
        logger.info(f"Файл существует: {os.path.exists(filepath)}")
        
        if not os.path.exists(filepath):
            logger.warning(f"Файл не найден: {filepath}")
            return pd.DataFrame()
        
        try:
            df = pd.read_csv(filepath, quoting=csv.QUOTE_ALL, on_bad_lines='skip')
            logger.info(f"Успешно загружено {len(df)} записей из {filepath}")
            return df
        except Exception as e:
            logger.error(f"Ошибка загрузки данных из {filepath}: {e}")
            return pd.DataFrame()
    
    def extract_airport_from_url(self, url: str) -> str:
        """Извлекает аэропорт вылета из URL"""
        try:
            if 'filter[from]=' in url:
                # Ищем параметр filter[from] в URL
                import re
                match = re.search(r'filter\[from\]=([^&]*)', url)
                if match:
                    airports = match.group(1)
                    if airports:
                        # Если несколько аэропортов через запятую, берем первый
                        return airports.split(',')[0]
            return "Все аэропорты"
        except Exception as e:
            logger.warning(f"Ошибка извлечения аэропорта из URL: {e}")
            return "Неизвестно"
    
    def compare_airports(self, warsaw_data_file: str, any_airports_data_file: str) -> Dict[str, Any]:
        """Сравнивает данные из Варшавы и всех аэропортов"""
        logger.info("🔄 Начинаем сравнение аэропортов...")
        
        # Загружаем данные
        warsaw_df = self.load_data(warsaw_data_file)
        any_airports_df = self.load_data(any_airports_data_file)
        
        logger.info(f"Загружено данных из Варшавы: {len(warsaw_df)} записей")
        logger.info(f"Загружено данных из всех аэропортов: {len(any_airports_df)} записей")
        
        if warsaw_df.empty or any_airports_df.empty:
            logger.warning("❌ Нет данных для сравнения")
            return {}
        
        # Добавляем информацию об аэропорте
        warsaw_df['departure_airport'] = 'Warszawa'
        any_airports_df['departure_airport'] = any_airports_df['url'].apply(self.extract_airport_from_url)
        
        # Находим отели до 8000 PLN, которые есть в любых аэропортах, но нет в Варшаве
        warsaw_hotels = set(warsaw_df['hotel_name'].unique())
        any_airports_hotels = set(any_airports_df['hotel_name'].unique())
        
        # Отели, которые есть в любых аэропортах, но нет в Варшаве
        missing_in_warsaw = any_airports_hotels - warsaw_hotels
        
        # Фильтруем по цене до 8000 PLN
        missing_under_8000 = any_airports_df[
            (any_airports_df['hotel_name'].isin(missing_in_warsaw)) & 
            (any_airports_df['price'] <= 8000)
        ].copy()
        
        # Группируем по отелям и берем минимальную цену
        missing_under_8000 = missing_under_8000.loc[missing_under_8000.groupby('hotel_name')['price'].idxmin()]
        
        # Сортируем по цене
        missing_under_8000 = missing_under_8000.sort_values('price')
        
        # Находим отели, которые есть в обеих выборках, и сравниваем цены
        common_hotels = warsaw_hotels & any_airports_hotels
        
        comparison_results = []
        
        for hotel in common_hotels:
            warsaw_hotel = warsaw_df[warsaw_df['hotel_name'] == hotel]
            any_airports_hotel = any_airports_df[any_airports_df['hotel_name'] == hotel]
            
            if warsaw_hotel.empty or any_airports_hotel.empty:
                continue
            
            # Берем минимальные цены
            warsaw_min_price = warsaw_hotel['price'].min()
            any_airports_min_price = any_airports_hotel['price'].min()
            
            # Находим лучшее предложение из любых аэропортов
            best_any_airports = any_airports_hotel.loc[any_airports_hotel['price'].idxmin()]
            
            if any_airports_min_price < warsaw_min_price:
                comparison_results.append({
                    'hotel_name': hotel,
                    'warsaw_price': warsaw_min_price,
                    'best_other_price': any_airports_min_price,
                    'savings': warsaw_min_price - any_airports_min_price,
                    'savings_percent': ((warsaw_min_price - any_airports_min_price) / warsaw_min_price) * 100,
                    'best_departure_airport': best_any_airports['departure_airport'],
                    'best_offer_url': best_any_airports.get('offer_url', ''),
                    'best_dates': best_any_airports.get('dates', ''),
                    'best_duration': best_any_airports.get('duration', '')
                })
        
        # Сортируем по экономии
        comparison_results.sort(key=lambda x: x['savings'], reverse=True)
        
        result = {
            'missing_in_warsaw_under_8000': missing_under_8000.to_dict('records'),
            'cheaper_alternatives': comparison_results,
            'comparison_date': datetime.now().isoformat(),
            'warsaw_hotels_count': len(warsaw_hotels),
            'any_airports_hotels_count': len(any_airports_hotels),
            'missing_in_warsaw_count': len(missing_in_warsaw),
            'cheaper_alternatives_count': len(comparison_results)
        }
        
        logger.info(f"✅ Сравнение завершено:")
        logger.info(f"   - Отелей в Варшаве: {result['warsaw_hotels_count']}")
        logger.info(f"   - Отелей в любых аэропортах: {result['any_airports_hotels_count']}")
        logger.info(f"   - Отсутствует в Варшаве (до 8000 PLN): {result['missing_in_warsaw_count']}")
        logger.info(f"   - Дешевле из других аэропортов: {result['cheaper_alternatives_count']}")
        
        return result
    
    def save_comparison_results(self, results: Dict[str, Any], output_file: str):
        """Сохраняет результаты сравнения в JSON файл"""
        filepath = os.path.join(self.data_dir, output_file)
        
        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(results, f, ensure_ascii=False, indent=2, default=str)
            logger.info(f"✅ Результаты сравнения сохранены в {filepath}")
        except Exception as e:
            logger.error(f"❌ Ошибка сохранения результатов сравнения: {e}")
    
    def create_comparison_report(self, results: Dict[str, Any]) -> str:
        """Создает текстовый отчет о сравнении аэропортов"""
        if not results:
            return "❌ Нет данных для создания отчета"
        
        report = []
        report.append("🛫 ОТЧЕТ О СРАВНЕНИИ АЭРОПОРТОВ")
        report.append("=" * 50)
        report.append(f"Дата сравнения: {results['comparison_date'][:19]}")
        report.append(f"Отелей в Варшаве: {results['warsaw_hotels_count']}")
        report.append(f"Отелей в любых аэропортах: {results['any_airports_hotels_count']}")
        report.append("")
        
        # Отели, отсутствующие в Варшаве
        missing_hotels = results['missing_in_warsaw_under_8000']
        if missing_hotels:
            report.append("🏨 ОТЕЛИ ДО 8000 PLN, ОТСУТСТВУЮЩИЕ В ВАРШАВЕ:")
            report.append("-" * 50)
            for i, hotel in enumerate(missing_hotels[:20], 1):  # Показываем топ-20
                report.append(f"{i:2d}. {hotel['hotel_name'][:50]:<50} | {hotel['price']:>8.0f} PLN")
                if hotel.get('departure_airport'):
                    report.append(f"    Аэропорт: {hotel['departure_airport']}")
                report.append("")
        else:
            report.append("🏨 Отели до 8000 PLN, отсутствующие в Варшаве: не найдены")
            report.append("")
        
        # Дешевле из других аэропортов
        cheaper_alternatives = results['cheaper_alternatives']
        if cheaper_alternatives:
            report.append("💰 ДЕШЕВЛЕ ИЗ ДРУГИХ АЭРОПОРТОВ:")
            report.append("-" * 50)
            for i, alt in enumerate(cheaper_alternatives[:20], 1):  # Показываем топ-20
                report.append(f"{i:2d}. {alt['hotel_name'][:40]:<40}")
                report.append(f"    Варшава: {alt['warsaw_price']:>8.0f} PLN | {alt['best_departure_airport']}: {alt['best_other_price']:>8.0f} PLN")
                report.append(f"    Экономия: {alt['savings']:>8.0f} PLN ({alt['savings_percent']:>5.1f}%)")
                if alt.get('best_dates'):
                    report.append(f"    Даты: {alt['best_dates']}")
                report.append("")
        else:
            report.append("💰 Дешевле из других аэропортов: не найдено")
            report.append("")
        
        return "\n".join(report)
    
    def save_comparison_report(self, results: Dict[str, Any], output_file: str):
        """Сохраняет текстовый отчет о сравнении"""
        report = self.create_comparison_report(results)
        filepath = os.path.join(self.data_dir, output_file)
        
        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(report)
            logger.info(f"✅ Отчет о сравнении сохранен в {filepath}")
        except Exception as e:
            logger.error(f"❌ Ошибка сохранения отчета: {e}")

def main():
    """Главная функция для тестирования"""
    comparison = AirportComparison()
    
    # Пример использования
    results = comparison.compare_airports(
        "travel_prices.csv",
        "travel_prices_any_airports.csv"
    )
    
    if results:
        comparison.save_comparison_results(results, "airport_comparison_results.json")
        comparison.save_comparison_report(results, "airport_comparison_report.txt")
        print("✅ Сравнение аэропортов завершено!")

if __name__ == "__main__":
    main()
