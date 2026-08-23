#!/usr/bin/env python3
"""
Скрипт для мониторинга цен на путешествия с сравнением аэропортов
Запускает поиск из Варшавы и из всех аэропортов, затем сравнивает результаты
"""

import asyncio
import json
import os
import sys
from datetime import datetime
from typing import List, Dict, Any, Optional
import logging
from travel_monitor import TravelPriceMonitor

# Настройка логирования
logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('monitor_with_airport_comparison.log', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

class TravelMonitorWithAirportComparison:
    def __init__(self, base_config_file: str, any_airports_config_file: str):
        self.base_config_file = base_config_file
        self.any_airports_config_file = any_airports_config_file
        
        # Загружаем конфигурации
        self.base_config = self.load_config(base_config_file)
        self.any_airports_config = self.load_config(any_airports_config_file)
        
    def load_config(self, config_file: str) -> Dict[str, Any]:
        """Загружает конфигурацию из файла"""
        try:
            with open(config_file, 'r', encoding='utf-8') as f:
                config = json.load(f)
            logger.info(f"Конфигурация загружена из {config_file}")
            return config
        except Exception as e:
            logger.error(f"Ошибка загрузки конфигурации {config_file}: {e}")
            sys.exit(1)
    
    async def run_monitoring_with_comparison(self):
        """Запускает мониторинг с сравнением аэропортов"""
        logger.info("🚀 Начинаем мониторинг с сравнением аэропортов...")
        
        try:
            # 1. Запускаем мониторинг из Варшавы
            logger.info("📍 Этап 1: Мониторинг из Варшавы...")
            warsaw_monitor = TravelPriceMonitor(self.base_config_file)
            warsaw_success = await warsaw_monitor.run_monitoring()
            
            if not warsaw_success:
                logger.error("❌ Ошибка при мониторинге из Варшавы")
                return False
            
            # 2. Запускаем мониторинг из всех аэропортов
            logger.info("🌍 Этап 2: Мониторинг из всех аэропортов...")
            any_airports_monitor = TravelPriceMonitor(self.any_airports_config_file)
            any_airports_success = await any_airports_monitor.run_monitoring()
            
            if not any_airports_success:
                logger.error("❌ Ошибка при мониторинге из всех аэропортов")
                return False
            
            # 3. Сравниваем результаты
            logger.info("🔄 Этап 3: Сравнение результатов...")
            warsaw_monitor.compare_airports(self.any_airports_config_file)
            
            logger.info("✅ Мониторинг с сравнением аэропортов завершен успешно!")
            return True
            
        except Exception as e:
            logger.error(f"❌ Критическая ошибка: {e}")
            return False

def main():
    """Главная функция"""
    import argparse
    parser = argparse.ArgumentParser(description="Travel price monitor with airport comparison")
    parser.add_argument("--base-config", default="config.json", help="Путь к базовой конфигурации (Варшава)")
    parser.add_argument("--any-airports-config", default="config_any_airports.json", help="Путь к конфигурации всех аэропортов")
    args = parser.parse_args()

    # Проверяем существование файлов конфигурации
    if not os.path.exists(args.base_config):
        logger.error(f"❌ Файл конфигурации не найден: {args.base_config}")
        sys.exit(1)
    
    if not os.path.exists(args.any_airports_config):
        logger.error(f"❌ Файл конфигурации не найден: {args.any_airports_config}")
        sys.exit(1)
    
    monitor = TravelMonitorWithAirportComparison(args.base_config, args.any_airports_config)
    
    try:
        success = asyncio.run(monitor.run_monitoring_with_comparison())
        if success:
            print("✅ Мониторинг с сравнением аэропортов завершен успешно!")
            sys.exit(0)
        else:
            print("❌ Мониторинг завершен с ошибками")
            sys.exit(1)
    except KeyboardInterrupt:
        logger.info("Мониторинг прерван пользователем")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Неожиданная ошибка: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
