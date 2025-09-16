#!/usr/bin/env python3
"""
Запуск мониторинга с фильтрацией видимости предложений
Использует оригинальные дашборды, но фильтрует неактуальные предложения
"""

import os
import sys
import subprocess
import json
from datetime import datetime
import logging

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def run_monitoring_for_country(country: str, config_file: str):
    """Запускает мониторинг для конкретной страны"""
    logger.info(f"🔄 Запуск мониторинга для {country}...")
    
    try:
        # Запускаем travel_monitor.py с конфигурацией
        result = subprocess.run([
            sys.executable, 'travel_monitor.py', 
            '--config', config_file,
            '--data-file', f'{country}_travel_prices.csv'
        ], capture_output=True, text=True, timeout=300)
        
        if result.returncode == 0:
            logger.info(f"✅ Мониторинг {country} завершен успешно")
            return True
        else:
            logger.error(f"❌ Ошибка мониторинга {country}: {result.stderr}")
            return False
            
    except subprocess.TimeoutExpired:
        logger.error(f"⏰ Таймаут мониторинга {country}")
        return False
    except Exception as e:
        logger.error(f"❌ Исключение при мониторинге {country}: {e}")
        return False

def generate_dashboards_with_visibility_filter():
    """Генерирует дашборды с фильтрацией видимости для всех стран"""
    countries = ['greece', 'egypt', 'turkey']
    
    for country in countries:
        data_file = f'data/{country}_travel_prices.csv'
        if os.path.exists(data_file):
            logger.info(f"📊 Генерация дашборда для {country} с фильтрацией видимости...")
            
            try:
                result = subprocess.run([
                    sys.executable, 'generate_inline_charts_dashboard_with_visibility_filter.py',
                    data_file,
                    f'index_{country}_filtered.html'
                ], capture_output=True, text=True, timeout=60)
                
                if result.returncode == 0:
                    logger.info(f"✅ Дашборд {country} с фильтрацией сгенерирован")
                else:
                    logger.error(f"❌ Ошибка генерации дашборда {country}: {result.stderr}")
                    
            except Exception as e:
                logger.error(f"❌ Исключение при генерации дашборда {country}: {e}")
        else:
            logger.warning(f"⚠️ Файл данных {data_file} не найден")

def generate_main_dashboard_with_visibility_filter():
    """Генерирует основной дашборд с фильтрацией видимости"""
    data_file = 'data/travel_prices.csv'
    if os.path.exists(data_file):
        logger.info("📊 Генерация основного дашборда с фильтрацией видимости...")
        
        try:
            result = subprocess.run([
                sys.executable, 'generate_inline_charts_dashboard_with_visibility_filter.py',
                data_file,
                'index_filtered.html'
            ], capture_output=True, text=True, timeout=60)
            
            if result.returncode == 0:
                logger.info("✅ Основной дашборд с фильтрацией сгенерирован")
            else:
                logger.error(f"❌ Ошибка генерации основного дашборда: {result.stderr}")
                
        except Exception as e:
            logger.error(f"❌ Исключение при генерации основного дашборда: {e}")
    else:
        logger.warning(f"⚠️ Файл данных {data_file} не найден")

def main():
    """Основная функция"""
    logger.info("🚀 Запуск мониторинга с фильтрацией видимости предложений")
    
    # Конфигурации для мониторинга
    configs = {
        'greece': 'config.json',
        'egypt': 'config_egypt.json', 
        'turkey': 'config_turkey.json'
    }
    
    # Запускаем мониторинг для каждой страны
    successful_runs = 0
    for country, config_file in configs.items():
        if os.path.exists(config_file):
            if run_monitoring_for_country(country, config_file):
                successful_runs += 1
        else:
            logger.warning(f"⚠️ Конфигурационный файл {config_file} не найден")
    
    logger.info(f"📈 Успешно завершено {successful_runs} из {len(configs)} мониторингов")
    
    # Генерируем дашборды с фильтрацией видимости
    generate_dashboards_with_visibility_filter()
    
    # Генерируем основной дашборд
    generate_main_dashboard_with_visibility_filter()
    
    logger.info("🎉 Мониторинг с фильтрацией видимости завершен")

if __name__ == "__main__":
    main()
