# 🎯 Travel Price Monitor

Автоматический мониторинг цен на путешествия с сайта fly.pl для поиска выгодных предложений.

## 🚀 Возможности

- **Автоматический парсинг** предложений с fly.pl
- **Накопительное хранение** данных (каждый запуск добавляет новые данные)
- **Визуализация** изменений цен с помощью графиков
- **Автоматические отчеты** с анализом трендов
- **Планировщик** для регулярного мониторинга
- **Уведомления** о значительных изменениях цен
- **🛫 Сравнение аэропортов** - поиск выгодных предложений из разных городов
- **📊 Расширенные дашборды** с поддержкой сравнения аэропортов

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

### 🛫 Сравнение аэропортов

#### Мониторинг всех стран с сравнением аэропортов:
```bash
python run_all_countries_with_airport_comparison.py
```

#### Мониторинг одной страны:
```bash
python travel_monitor_with_airport_comparison.py --base-config config.json --any-airports-config config_any_airports.json
```

#### Генерация дашбордов с поддержкой сравнения:
```bash
python generate_all_dashboards_with_airport_comparison.py
```

#### Тестирование функциональности:
```bash
python test_airport_comparison.py
```

## 📊 Результаты

- `data/travel_prices.csv` - собранные данные
- `data/charts/` - графики цен
- `data/price_report.txt` - текстовый отчет
- `data/*_airport_comparison.json` - результаты сравнения аэропортов
- `data/*_airport_comparison_report.txt` - отчеты о сравнении аэропортов
- `index_airports.html` - главная страница с дашбордами сравнения аэропортов

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

