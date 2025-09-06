# 🎯 Travel Price Monitor

Автоматический мониторинг цен на путешествия с сайта fly.pl для поиска выгодных предложений.

## 🚀 Возможности

- **Автоматический парсинг** предложений с fly.pl
- **Накопительное хранение** данных (каждый запуск добавляет новые данные)
- **Визуализация** изменений цен с помощью графиков
- **Автоматические отчеты** с анализом трендов
- **Планировщик** для регулярного мониторинга
- **Уведомления** о значительных изменениях цен

## 📋 Требования

- Python 3.8+
- Playwright
- pandas
- matplotlib

## 🛠 Быстрая установка

```bash
# Клонируйте репозиторий
git clone <repository-url>
cd travel-price-monitor

# Установите зависимости
pip install -r requirements.txt

# Установите браузеры
playwright install chromium

# Запустите мониторинг
python travel_monitor.py
```

## 🎯 Использование

### Основной скрипт

```bash
python travel_monitor.py
```

### Анализ данных

```bash
python analyze_data.py --charts
```

### Автоматический мониторинг

```bash
python scheduler.py
```

## 📊 Результаты

- `data/travel_prices.csv` - собранные данные
- `data/charts/` - графики цен
- `data/price_report.txt` - текстовый отчет

## ⚙️ Настройка

Отредактируйте `config.json` для изменения параметров поиска и мониторинга.

## 📈 Автоматизация

Настройте cron или Task Scheduler для регулярного запуска:

```bash
# Ежедневно в 9:00
0 9 * * * cd /path/to/travel-price-monitor && python travel_monitor.py
```

---

**Удачного мониторинга цен! 🎯✈️**

