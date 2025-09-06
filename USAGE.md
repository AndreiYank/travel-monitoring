# 📖 Подробная инструкция по использованию

## 🚀 Быстрый старт

### 1. Установка

```bash
# Клонируйте репозиторий
git clone <your-repo-url>
cd travel-price-monitor

# Автоматическая установка
./install.sh
```

### 2. Первый запуск

```bash
# Активируйте виртуальное окружение
source venv/bin/activate

# Запустите мониторинг
python travel_monitor.py
```

## 🎯 Основные команды

### Мониторинг цен

```bash
# Разовый сбор данных
python travel_monitor.py

# Анализ собранных данных
python analyze_data.py

# Анализ с графиками
python analyze_data.py --charts

# Анализ с экспортом в JSON
python analyze_data.py --charts --export
```

### Автоматический мониторинг

```bash
# Запуск планировщика (мониторинг по расписанию)
python scheduler.py
```

## ⚙️ Настройка

### Изменение параметров поиска

1. **Откройте `config.json`**
2. **Измените URL** на нужный с fly.pl
3. **Настройте другие параметры**:
   - `wait_timeout` - таймаут загрузки (мс)
   - `max_offers` - максимальное количество предложений
   - `max_retries` - количество попыток

### Настройка расписания

1. **Откройте `scheduler_config.json`**
2. **Настройте интервалы**:
   - `daily` - ежедневный запуск в определенное время
   - `custom_hours` - запуск в определенные часы
   - `hourly` - почасовой запуск

### Настройка уведомлений

1. **Создайте Telegram бота** через @BotFather
2. **Получите токен бота**
3. **Узнайте свой chat_id** (отправьте сообщение боту @userinfobot)
4. **Обновите `scheduler_config.json`**:

```json
{
  "notifications": {
    "enabled": true,
    "telegram_bot_token": "ВАШ_ТОКЕН",
    "telegram_chat_id": "ВАШ_CHAT_ID"
  }
}
```

## 📊 Результаты работы

### Структура данных

```
data/
├── travel_prices.csv          # Основные данные
├── price_report.txt           # Текстовый отчет
├── analysis_summary.json      # Сводка в JSON
├── charts/                    # Базовые графики
│   ├── price_timeline.png
│   └── top_cheap_offers.png
└── advanced_charts/           # Расширенные графики
    ├── price_dynamics.png
    ├── price_distribution_by_day.png
    └── top_hotels_by_offers.png
```

### Формат данных CSV

| Колонка    | Описание            |
| ---------- | ------------------- |
| hotel_name | Название отеля/тура |
| price      | Цена в PLN          |
| dates      | Даты поездки        |
| duration   | Длительность        |
| rating     | Рейтинг отеля       |
| scraped_at | Время сбора данных  |
| url        | URL страницы поиска |

## 🔄 Автоматизация

### Локальная автоматизация

#### Linux/macOS (cron)

```bash
# Добавьте в crontab
crontab -e

# Ежедневно в 9:00
0 9 * * * cd /path/to/travel-price-monitor && source venv/bin/activate && python travel_monitor.py
```

#### Windows (Task Scheduler)

1. Откройте "Планировщик заданий"
2. Создайте новое задание
3. Укажите путь: `python.exe travel_monitor.py`
4. Настройте расписание

### GitHub Actions (облачная автоматизация)

1. **Создайте репозиторий на GitHub**
2. **Загрузите код**:

```bash
git remote add origin https://github.com/yourusername/travel-price-monitor.git
git push -u origin main
```

3. **Настройте GitHub Actions** (уже настроено в `.github/workflows/monitor.yml`)
4. **Система будет автоматически**:
   - Запускаться каждый день в 9:00 UTC
   - Собирать данные
   - Создавать графики
   - Сохранять результаты
   - Коммитить изменения в репозиторий

## 🐛 Решение проблем

### Проблема: "Предложения не найдены"

- Проверьте URL в `config.json`
- Убедитесь, что сайт доступен
- Увеличьте `wait_timeout`

### Проблема: "Ошибка парсинга"

- Сайт мог изменить структуру
- Проверьте логи в `monitor.log`
- Попробуйте запустить снова

### Проблема: "Нет данных для графиков"

- Убедитесь, что CSV файл создан
- Запустите скрипт несколько раз
- Проверьте права доступа к папке `data/`

### Проблема: "Ошибка установки Playwright"

```bash
# Переустановите браузеры
playwright install chromium

# Или установите все браузеры
playwright install
```

## 📈 Расширенное использование

### Сравнение разных направлений

1. **Создайте копии конфигурации**:

```bash
cp config.json config_turkey.json
cp config.json config_greece.json
```

2. **Настройте разные URL** в каждом файле

3. **Запускайте мониторинг** с разными конфигурациями:

```bash
python travel_monitor.py --config config_turkey.json
python travel_monitor.py --config config_greece.json
```

### Интеграция с внешними сервисами

#### Email уведомления

Добавьте в `scheduler.py`:

```python
import smtplib
from email.mime.text import MIMEText

def send_email_notification(message):
    # Код отправки email
    pass
```

#### Webhook уведомления

```python
import requests

def send_webhook_notification(message):
    webhook_url = "YOUR_WEBHOOK_URL"
    requests.post(webhook_url, json={"text": message})
```

## 🔧 Разработка и расширение

### Добавление новых сайтов

1. **Создайте новый класс** для парсинга
2. **Реализуйте методы** `scrape_offers()` и `extract_offer_data()`
3. **Добавьте конфигурацию** для нового сайта

### Добавление новых метрик

1. **Расширьте структуру данных** в CSV
2. **Обновите методы** извлечения данных
3. **Добавьте новые графики** в `analyze_data.py`

## 📝 Логирование

### Просмотр логов

```bash
# Логи мониторинга
tail -f monitor.log

# Логи планировщика
tail -f scheduler.log
```

### Настройка уровня логирования

Измените в скриптах:

```python
logging.basicConfig(level=logging.DEBUG)  # Подробные логи
logging.basicConfig(level=logging.INFO)   # Обычные логи
logging.basicConfig(level=logging.WARNING) # Только предупреждения
```

## 🎯 Советы по эффективному использованию

1. **Запускайте регулярно** - чем чаще, тем точнее анализ
2. **Настройте уведомления** - не пропустите выгодные предложения
3. **Мониторьте разные направления** - создавайте отдельные конфигурации
4. **Анализируйте исторические данные** - используйте `analyze_data.py`
5. **Настройте автоматическую очистку** - чтобы не накапливались старые данные
6. **Используйте GitHub Actions** - для надежной облачной автоматизации

---

**Удачного мониторинга цен! 🎯✈️**

