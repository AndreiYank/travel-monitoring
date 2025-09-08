#!/usr/bin/env python3
"""
Скрипт для безопасного тестирования мониторинга без влияния на CI данные
"""

import subprocess
import sys
import os

def run_dev_test(country="greece"):
    """Запускает тест для указанной страны с dev конфигурацией"""
    
    dev_configs = {
        "greece": "config_dev_greece.json",
        "egypt": "config_dev_egypt.json", 
        "turkey": "config_dev_turkey.json"
    }
    
    if country not in dev_configs:
        print(f"❌ Неизвестная страна: {country}")
        print(f"Доступные: {', '.join(dev_configs.keys())}")
        return False
    
    config_file = dev_configs[country]
    
    if not os.path.exists(config_file):
        print(f"❌ Конфигурация {config_file} не найдена")
        return False
    
    print(f"🚀 Запуск теста для {country.upper()} с dev конфигурацией...")
    print(f"📁 Конфигурация: {config_file}")
    print(f"📊 Данные будут сохранены в dev_{country}_travel_prices.csv")
    print("⚠️  Эти данные НЕ будут закоммичены в репозиторий")
    print()
    
    try:
        result = subprocess.run([
            "python3", "travel_monitor.py", 
            "--config", config_file
        ], check=True)
        
        print(f"✅ Тест для {country.upper()} завершен успешно!")
        return True
        
    except subprocess.CalledProcessError as e:
        print(f"❌ Ошибка при запуске теста: {e}")
        return False

if __name__ == "__main__":
    if len(sys.argv) > 1:
        country = sys.argv[1].lower()
    else:
        country = "greece"
    
    success = run_dev_test(country)
    sys.exit(0 if success else 1)
