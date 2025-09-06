# 🚀 План перехода на API подход для ускорения сбора данных

## 📊 Текущее состояние (UI Scraping)

### Проблемы текущего подхода:
- ⏱️ **Медленно**: 2-3 минуты на сбор данных
- 🎭 **Playwright overhead**: Загрузка браузера, рендеринг страниц
- 🔄 **Pagination**: Переключение между страницами
- 🎯 **Fragile selectors**: Зависимость от CSS селекторов
- 📱 **UI changes**: Ломается при изменении интерфейса

### Текущие селекторы:
```javascript
// Основные селекторы для поиска предложений
[class*="offer"] - основной селектор
.price-view-2 - цена за всех
[class*="title"] - название отеля
[class*="date"] - даты
[class*="duration"] - длительность
```

## 🎯 API подход - Концепция

### 1. Анализ сетевых запросов fly.pl

**Цель**: Найти API endpoints, которые использует сайт для загрузки данных

**Методы исследования**:
```bash
# 1. Анализ Network tab в DevTools
# 2. Поиск XHR/Fetch запросов
# 3. Анализ JSON ответов
# 4. Поиск API endpoints
```

**Ожидаемые endpoints**:
```
GET /api/search/offers
GET /api/offers/list
GET /api/travel/search
POST /api/search
```

### 2. Структура API запроса

**Базовый URL**: `https://fly.pl/api/`

**Параметры запроса** (из текущего URL):
```json
{
  "destination": "14:",  // Турция
  "cityIds": [0],
  "dateFrom": "20-09-2025",
  "dateTo": "04-10-2025", 
  "duration": "6:15",
  "departure": ["Warszawa", "Warszawa-Radom"],
  "adults": 2,
  "children": 1,
  "priceFrom": 0,
  "priceTo": 8100,
  "category": 40,
  "catering": 1,
  "transport": false,
  "orderBy": "price",
  "page": 1,
  "limit": 50
}
```

### 3. Ожидаемая структура ответа

```json
{
  "success": true,
  "data": {
    "offers": [
      {
        "id": "12345",
        "hotelName": "Aqua Sun Village",
        "price": 6354.0,
        "pricePerPerson": false,
        "dates": {
          "from": "24-09-2025",
          "to": "01-10-2025"
        },
        "duration": "8 dni",
        "rating": 4.2,
        "url": "https://fly.pl/offer/12345",
        "images": ["url1", "url2"],
        "description": "...",
        "amenities": ["WiFi", "Pool", "Beach"]
      }
    ],
    "pagination": {
      "currentPage": 1,
      "totalPages": 3,
      "totalOffers": 150,
      "hasNext": true
    },
    "filters": {
      "priceRange": {"min": 6100, "max": 8100},
      "hotels": 47,
      "destinations": 1
    }
  }
}
```

## 🔧 Реализация API подхода

### 1. Новый класс APITravelMonitor

```python
class APITravelMonitor:
    def __init__(self, config_file: str = "api_config.json"):
        self.base_url = "https://fly.pl/api"
        self.session = requests.Session()
        self.config = self.load_config()
        
    async def search_offers(self, filters: Dict) -> List[Dict]:
        """Поиск предложений через API"""
        endpoint = f"{self.base_url}/search/offers"
        
        response = await self.session.post(endpoint, json=filters)
        if response.status_code == 200:
            data = response.json()
            return data['data']['offers']
        else:
            raise Exception(f"API Error: {response.status_code}")
    
    async def get_offer_details(self, offer_id: str) -> Dict:
        """Получение деталей предложения"""
        endpoint = f"{self.base_url}/offers/{offer_id}"
        response = await self.session.get(endpoint)
        return response.json()
```

### 2. Конфигурация API

```json
{
  "api_base_url": "https://fly.pl/api",
  "endpoints": {
    "search": "/search/offers",
    "offer_details": "/offers/{id}",
    "hotels": "/hotels",
    "destinations": "/destinations"
  },
  "headers": {
    "User-Agent": "TravelMonitor/1.0",
    "Accept": "application/json",
    "Content-Type": "application/json"
  },
  "rate_limits": {
    "requests_per_minute": 60,
    "requests_per_hour": 1000
  },
  "retry_config": {
    "max_retries": 3,
    "backoff_factor": 2
  }
}
```

### 3. Преимущества API подхода

**Скорость**:
- ⚡ **10-30 секунд** вместо 2-3 минут
- 🚀 **Прямые HTTP запросы** без браузера
- 📦 **Структурированные данные** JSON

**Надежность**:
- 🎯 **Стабильные endpoints** 
- 📊 **Консистентные данные**
- 🔄 **Лучшая обработка ошибок**

**Масштабируемость**:
- 📈 **Параллельные запросы**
- 🔄 **Кэширование**
- 📊 **Batch операции**

## 🛠️ План внедрения

### Этап 1: Исследование API (1-2 дня)
1. **Анализ Network tab** в DevTools
2. **Поиск API endpoints** fly.pl
3. **Документирование** структуры запросов/ответов
4. **Тестирование** доступности endpoints

### Этап 2: Создание API клиента (2-3 дня)
1. **APITravelMonitor класс**
2. **Обработка аутентификации** (если нужно)
3. **Rate limiting** и retry логика
4. **Обработка ошибок**

### Этап 3: Интеграция (1 день)
1. **Замена UI scraping** на API
2. **Обновление конфигурации**
3. **Тестирование** сбора данных
4. **Сравнение** качества данных

### Этап 4: Оптимизация (1 день)
1. **Параллельные запросы**
2. **Кэширование** данных
3. **Мониторинг** производительности
4. **Документация**

## 📊 Ожидаемые результаты

### Производительность:
- ⚡ **Скорость**: 10-30 сек (vs 2-3 мин)
- 🚀 **Надежность**: 99%+ успешных запросов
- 📈 **Масштабируемость**: 10x больше данных

### Качество данных:
- 🎯 **Точность**: Структурированные данные
- 📊 **Полнота**: Больше полей данных
- 🔄 **Консистентность**: Стабильный формат

### Техническая простота:
- 🧹 **Меньше кода**: Убрать Playwright
- 🔧 **Проще поддержка**: Стабильные API
- 📦 **Меньше зависимостей**: Только requests

## 🚨 Риски и ограничения

### Возможные проблемы:
1. **API недоступен** - fly.pl не предоставляет публичный API
2. **Аутентификация** - может потребоваться регистрация
3. **Rate limits** - ограничения на количество запросов
4. **Изменения API** - endpoints могут измениться

### План B:
Если API недоступен, можно рассмотреть:
1. **Headless browser optimization** - ускорить Playwright
2. **Caching** - кэшировать результаты
3. **Parallel processing** - параллельный парсинг
4. **Alternative sources** - другие источники данных

## 🎯 Следующие шаги

1. **Исследовать Network tab** fly.pl
2. **Найти API endpoints** 
3. **Создать прототип** API клиента
4. **Протестировать** с реальными данными
5. **Сравнить** с текущим подходом

---

*Этот план поможет ускорить сбор данных в 5-10 раз и сделать систему более надежной!* 🚀
