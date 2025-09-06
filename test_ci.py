#!/usr/bin/env python3
"""
Скрипт для тестирования CI процессов локально
"""

import subprocess
import sys
import os
from datetime import datetime

def run_command(command, description):
    """Выполняет команду и выводит результат"""
    print(f"\n🔄 {description}")
    print(f"Команда: {command}")
    print("-" * 50)
    
    try:
        result = subprocess.run(command, shell=True, capture_output=True, text=True)
        if result.returncode == 0:
            print(f"✅ {description} - Успешно")
            if result.stdout:
                print("Вывод:", result.stdout)
        else:
            print(f"❌ {description} - Ошибка")
            print("Ошибка:", result.stderr)
            return False
    except Exception as e:
        print(f"❌ {description} - Исключение: {e}")
        return False
    
    return True

def main():
    """Основная функция тестирования"""
    print("🚀 Тестирование CI процессов")
    print("=" * 50)
    print(f"Время: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    # Проверяем, что мы в правильной директории
    if not os.path.exists('travel_monitor.py'):
        print("❌ Ошибка: Запустите скрипт из корневой директории проекта")
        sys.exit(1)
    
    success_count = 0
    total_tests = 0
    
    # Тест 1: Проверка зависимостей
    total_tests += 1
    if run_command("python3 -c 'import pandas, matplotlib, playwright'", "Проверка зависимостей"):
        success_count += 1
    
    # Тест 2: Запуск мониторинга
    total_tests += 1
    if run_command("python3 travel_monitor.py", "Запуск мониторинга цен"):
        success_count += 1
    
    # Тест 3: Анализ данных
    total_tests += 1
    if run_command("python3 analyze_data.py --charts", "Анализ данных и создание графиков"):
        success_count += 1
    
    # Тест 4: Генерация статического дашборда
    total_tests += 1
    if run_command("python3 generate_static_dashboard.py", "Генерация статического дашборда"):
        success_count += 1
    
    # Тест 5: Проверка алертов
    total_tests += 1
    if run_command("python3 price_alerts.py", "Проверка системы алертов"):
        success_count += 1
    
    # Тест 6: Проверка файлов
    total_tests += 1
    files_to_check = [
        'data/travel_prices.csv',
        'data/price_report.txt',
        'data/price_alerts_report.txt',
        'index.html'
    ]
    
    all_files_exist = True
    for file_path in files_to_check:
        if os.path.exists(file_path):
            print(f"✅ Файл существует: {file_path}")
        else:
            print(f"❌ Файл отсутствует: {file_path}")
            all_files_exist = False
    
    if all_files_exist:
        success_count += 1
        print("✅ Проверка файлов - Успешно")
    else:
        print("❌ Проверка файлов - Ошибка")
    
    # Итоговый отчет
    print("\n" + "=" * 50)
    print("📊 ИТОГОВЫЙ ОТЧЕТ")
    print("=" * 50)
    print(f"Успешных тестов: {success_count}/{total_tests}")
    print(f"Процент успеха: {(success_count/total_tests)*100:.1f}%")
    
    if success_count == total_tests:
        print("🎉 Все тесты прошли успешно! CI готов к работе.")
        return 0
    else:
        print("⚠️ Некоторые тесты не прошли. Проверьте ошибки выше.")
        return 1

if __name__ == "__main__":
    exit_code = main()
    sys.exit(exit_code)
