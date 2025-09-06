#!/bin/bash

# Скрипт установки для Travel Price Monitor

echo "🚀 Установка Travel Price Monitor..."

# Проверяем наличие Python
if ! command -v python3 &> /dev/null; then
    echo "❌ Python3 не найден. Установите Python 3.8+"
    exit 1
fi

# Проверяем наличие pip
if ! command -v pip3 &> /dev/null; then
    echo "❌ pip3 не найден. Установите pip"
    exit 1
fi

# Создаем виртуальное окружение
if [ ! -d "venv" ]; then
    echo "📦 Создаем виртуальное окружение..."
    python3 -m venv venv
fi

echo "🔧 Активируем виртуальное окружение..."
source venv/bin/activate

echo "📚 Устанавливаем зависимости..."
pip install -r requirements.txt

echo "🌐 Устанавливаем браузеры для Playwright..."
playwright install chromium

echo "📁 Создаем директории..."
mkdir -p data/charts
mkdir -p data/advanced_charts

echo "✅ Установка завершена!"
echo ""
echo "🎯 Для запуска используйте:"
echo "  python travel_monitor.py          # Разовый мониторинг"
echo "  python analyze_data.py --charts   # Анализ данных"
echo "  python scheduler.py               # Автоматический мониторинг"
echo ""
echo "📖 Подробная документация в README.md"
