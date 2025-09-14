#!/usr/bin/env python3
"""
Скрипт для запуска мониторинга всех стран с сравнением аэропортов
"""

import asyncio
import json
import os
import sys
from datetime import datetime
from typing import List, Dict, Any
import logging
from travel_monitor_with_airport_comparison import TravelMonitorWithAirportComparison

# Настройка логирования
logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('all_countries_monitor.log', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

class AllCountriesMonitor:
    def __init__(self):
        self.countries = [
            {
                'name': 'Греция',
                'base_config': 'config.json',
                'any_airports_config': 'config_any_airports.json'
            },
            {
                'name': 'Египет',
                'base_config': 'config_egypt.json',
                'any_airports_config': 'config_egypt_any_airports.json'
            },
            {
                'name': 'Турция',
                'base_config': 'config_turkey.json',
                'any_airports_config': 'config_turkey_any_airports.json'
            }
        ]
    
    async def run_all_countries(self):
        """Запускает мониторинг для всех стран"""
        logger.info("🌍 Начинаем мониторинг всех стран с сравнением аэропортов...")
        
        results = {}
        
        for country in self.countries:
            logger.info(f"\n{'='*60}")
            logger.info(f"🏖️  Обрабатываем: {country['name']}")
            logger.info(f"{'='*60}")
            
            try:
                # Проверяем существование файлов конфигурации
                if not os.path.exists(country['base_config']):
                    logger.error(f"❌ Файл конфигурации не найден: {country['base_config']}")
                    results[country['name']] = {'status': 'error', 'message': f'Config file not found: {country["base_config"]}'}
                    continue
                
                if not os.path.exists(country['any_airports_config']):
                    logger.error(f"❌ Файл конфигурации не найден: {country['any_airports_config']}")
                    results[country['name']] = {'status': 'error', 'message': f'Config file not found: {country["any_airports_config"]}'}
                    continue
                
                # Запускаем мониторинг для страны
                monitor = TravelMonitorWithAirportComparison(
                    country['base_config'],
                    country['any_airports_config']
                )
                
                success = await monitor.run_monitoring_with_comparison()
                
                if success:
                    logger.info(f"✅ {country['name']}: Мониторинг завершен успешно!")
                    results[country['name']] = {'status': 'success', 'message': 'Monitoring completed successfully'}
                else:
                    logger.error(f"❌ {country['name']}: Ошибка при мониторинге")
                    results[country['name']] = {'status': 'error', 'message': 'Monitoring failed'}
                    
            except Exception as e:
                logger.error(f"❌ {country['name']}: Критическая ошибка: {e}")
                results[country['name']] = {'status': 'error', 'message': str(e)}
        
        # Создаем сводный отчет
        self.create_summary_report(results)
        
        return results
    
    def create_summary_report(self, results: Dict[str, Any]):
        """Создает сводный отчет по всем странам"""
        logger.info("\n" + "="*60)
        logger.info("📊 СВОДНЫЙ ОТЧЕТ")
        logger.info("="*60)
        
        successful_countries = [name for name, result in results.items() if result['status'] == 'success']
        failed_countries = [name for name, result in results.items() if result['status'] == 'error']
        
        logger.info(f"✅ Успешно обработано: {len(successful_countries)} стран")
        for country in successful_countries:
            logger.info(f"   - {country}")
        
        if failed_countries:
            logger.info(f"❌ Ошибки в {len(failed_countries)} странах:")
            for country in failed_countries:
                logger.info(f"   - {country}: {results[country]['message']}")
        
        # Сохраняем сводный отчет в файл
        summary = {
            'timestamp': datetime.now().isoformat(),
            'total_countries': len(self.countries),
            'successful': len(successful_countries),
            'failed': len(failed_countries),
            'results': results
        }
        
        try:
            with open('data/all_countries_monitoring_summary.json', 'w', encoding='utf-8') as f:
                json.dump(summary, f, ensure_ascii=False, indent=2)
            logger.info(f"📁 Сводный отчет сохранен в data/all_countries_monitoring_summary.json")
        except Exception as e:
            logger.error(f"❌ Ошибка сохранения сводного отчета: {e}")

def main():
    """Главная функция"""
    monitor = AllCountriesMonitor()
    
    try:
        results = asyncio.run(monitor.run_all_countries())
        
        # Подсчитываем результаты
        successful = sum(1 for result in results.values() if result['status'] == 'success')
        total = len(results)
        
        if successful == total:
            print(f"\n✅ Мониторинг всех стран завершен успешно! ({successful}/{total})")
            sys.exit(0)
        else:
            print(f"\n⚠️  Мониторинг завершен с ошибками ({successful}/{total})")
            sys.exit(1)
            
    except KeyboardInterrupt:
        logger.info("Мониторинг прерван пользователем")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Неожиданная ошибка: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
