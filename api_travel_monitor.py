#!/usr/bin/env python3
"""
API-based Travel Price Monitor - Прототип для ускорения сбора данных
"""

import asyncio
import aiohttp
import json
import csv
import os
import sys
from datetime import datetime
from typing import List, Dict, Any, Optional
import pandas as pd
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class APITravelMonitor:
    """API-based мониторинг цен на путешествия"""
    
    def __init__(self, config_file: str = "api_config.json"):
        self.config_file = config_file
        self.data_file = "data/travel_prices.csv"
        self.config = self.load_config()
        self.session = None
        
    def load_config(self) -> Dict[str, Any]:
        """Загружает конфигурацию API"""
        default_config = {
            "api_base_url": "https://fly.pl",
            "endpoints": {
                "search": "/api/search/offers",
                "offers": "/api/offers",
                "search_ajax": "/ajax/search"
            },
            "headers": {
                "User-Agent": "TravelMonitor/1.0 (API Research)",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.8",
                "Referer": "https://fly.pl/kierunek/turcja/",
                "X-Requested-With": "XMLHttpRequest"
            },
            "search_params": {
                "filter[dest]": "14:",
                "filter[cityids]": "0",
                "filter[whenFrom]": "20-09-2025",
                "filter[whenTo]": "04-10-2025",
                "filter[duration]": "6:15",
                "filter[from]": "Warszawa,Warszawa-Radom",
                "filter[person]": "2",
                "filter[child]": "1",
                "filter[price]": "0",
                "filter[PriceFrom]": "0",
                "filter[PriceTo]": "8100",
                "filter[addCategory]": "40",
                "filter[addCatering]": "1",
                "filter[addMisc]": "0",
                "filter[addTransport]": "F",
                "filter[fp]": "1",
                "order_by": "ofr_price",
                "filter[tourOperator]": "0",
                "filter[childAge][1]": "20201210"
            },
            "rate_limits": {
                "requests_per_minute": 60,
                "delay_between_requests": 1.0
            },
            "retry_config": {
                "max_retries": 3,
                "backoff_factor": 2
            }
        }
        
        try:
            if os.path.exists(self.config_file):
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                logger.info(f"Конфигурация API загружена из {self.config_file}")
                return {**default_config, **config}
            else:
                logger.info("Используется конфигурация по умолчанию")
                return default_config
        except Exception as e:
            logger.error(f"Ошибка загрузки конфигурации: {e}")
            return default_config
    
    async def __aenter__(self):
        """Асинхронный контекстный менеджер"""
        self.session = aiohttp.ClientSession(
            headers=self.config['headers'],
            timeout=aiohttp.ClientTimeout(total=30)
        )
        return self
        
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Закрытие сессии"""
        if self.session:
            await self.session.close()
    
    async def discover_api_endpoints(self) -> Dict[str, Any]:
        """Обнаруживает доступные API endpoints"""
        
        logger.info("🔍 Обнаружение API endpoints...")
        
        # Список потенциальных endpoints для тестирования
        potential_endpoints = [
            "/api/search/offers",
            "/api/offers",
            "/api/travel/search",
            "/api/search",
            "/api/v1/search",
            "/api/v2/search",
            "/search/api",
            "/offers/api",
            "/travel/api",
            "/ajax/search",
            "/ajax/offers",
            "/search/ajax",
            "/offers/ajax"
        ]
        
        results = {}
        
        for endpoint in potential_endpoints:
            url = f"{self.config['api_base_url']}{endpoint}"
            
            try:
                # Тестируем GET запрос
                async with self.session.get(url) as response:
                    if response.status == 200:
                        content_type = response.headers.get('content-type', '')
                        if 'json' in content_type:
                            data = await response.json()
                            results[endpoint] = {
                                'method': 'GET',
                                'status': response.status,
                                'content_type': content_type,
                                'data_preview': str(data)[:200] + "..." if len(str(data)) > 200 else str(data),
                                'success': True
                            }
                            logger.info(f"✅ Найден API endpoint: {endpoint}")
                        else:
                            text = await response.text()
                            results[endpoint] = {
                                'method': 'GET',
                                'status': response.status,
                                'content_type': content_type,
                                'text_preview': text[:200] + "..." if len(text) > 200 else text,
                                'success': False
                            }
                    else:
                        results[endpoint] = {
                            'method': 'GET',
                            'status': response.status,
                            'success': False
                        }
                        
            except Exception as e:
                results[endpoint] = {
                    'method': 'GET',
                    'error': str(e),
                    'success': False
                }
        
        return results
    
    async def test_search_endpoints(self) -> Dict[str, Any]:
        """Тестирует поисковые endpoints с параметрами"""
        
        logger.info("🔍 Тестирование поисковых endpoints...")
        
        # Endpoints для POST запросов
        post_endpoints = [
            "/api/search/offers",
            "/api/search",
            "/api/travel/search",
            "/ajax/search",
            "/search/api"
        ]
        
        results = {}
        
        for endpoint in post_endpoints:
            url = f"{self.config['api_base_url']}{endpoint}"
            
            try:
                # Тестируем POST с JSON
                async with self.session.post(
                    url,
                    json=self.config['search_params'],
                    headers={'Content-Type': 'application/json'}
                ) as response:
                    
                    results[endpoint] = {
                        'method': 'POST JSON',
                        'status': response.status,
                        'content_type': response.headers.get('content-type', ''),
                        'content_length': response.headers.get('content-length', 'unknown')
                    }
                    
                    if response.status == 200:
                        try:
                            data = await response.json()
                            results[endpoint]['data_preview'] = str(data)[:300] + "..." if len(str(data)) > 300 else str(data)
                            results[endpoint]['success'] = True
                            logger.info(f"✅ POST JSON успешен: {endpoint}")
                        except:
                            text = await response.text()
                            results[endpoint]['text_preview'] = text[:300] + "..." if len(text) > 300 else text
                            results[endpoint]['success'] = True
                    else:
                        results[endpoint]['success'] = False
                        
            except Exception as e:
                results[endpoint] = {
                    'method': 'POST JSON',
                    'error': str(e),
                    'success': False
                }
        
        return results
    
    async def search_offers_api(self, endpoint: str = None) -> List[Dict[str, Any]]:
        """Поиск предложений через API"""
        
        if not endpoint:
            # Пробуем разные endpoints
            endpoints_to_try = [
                "/api/search/offers",
                "/api/search",
                "/ajax/search",
                "/search/api"
            ]
        else:
            endpoints_to_try = [endpoint]
        
        for endpoint in endpoints_to_try:
            url = f"{self.config['api_base_url']}{endpoint}"
            
            try:
                logger.info(f"🔍 Попытка поиска через {endpoint}...")
                
                # Пробуем POST с JSON
                async with self.session.post(
                    url,
                    json=self.config['search_params'],
                    headers={'Content-Type': 'application/json'}
                ) as response:
                    
                    if response.status == 200:
                        data = await response.json()
                        offers = self.parse_api_response(data)
                        if offers:
                            logger.info(f"✅ Найдено {len(offers)} предложений через {endpoint}")
                            return offers
                    else:
                        logger.warning(f"❌ {endpoint} вернул статус {response.status}")
                        
            except Exception as e:
                logger.warning(f"❌ Ошибка с {endpoint}: {e}")
                continue
        
        logger.error("❌ Не удалось найти рабочий API endpoint")
        return []
    
    def parse_api_response(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Парсит ответ API и извлекает предложения"""
        
        offers = []
        
        try:
            # Пробуем разные структуры ответа
            if isinstance(data, dict):
                # Ищем массив предложений в разных местах
                offers_data = None
                
                if 'data' in data and 'offers' in data['data']:
                    offers_data = data['data']['offers']
                elif 'offers' in data:
                    offers_data = data['offers']
                elif 'results' in data:
                    offers_data = data['results']
                elif 'items' in data:
                    offers_data = data['items']
                elif isinstance(data.get('data'), list):
                    offers_data = data['data']
                elif isinstance(data, list):
                    offers_data = data
                
                if offers_data and isinstance(offers_data, list):
                    for item in offers_data:
                        if isinstance(item, dict):
                            offer = self.parse_offer_item(item)
                            if offer:
                                offers.append(offer)
            
            elif isinstance(data, list):
                # Если ответ - это массив предложений
                for item in data:
                    if isinstance(item, dict):
                        offer = self.parse_offer_item(item)
                        if offer:
                            offers.append(offer)
                            
        except Exception as e:
            logger.error(f"Ошибка парсинга ответа API: {e}")
        
        return offers
    
    def parse_offer_item(self, item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Парсит отдельное предложение из API ответа"""
        
        try:
            # Извлекаем данные из разных возможных полей
            hotel_name = (
                item.get('hotelName') or 
                item.get('hotel_name') or 
                item.get('name') or 
                item.get('title') or 
                item.get('hotel') or
                ""
            )
            
            price = self.extract_price_from_item(item)
            
            dates = self.extract_dates_from_item(item)
            duration = self.extract_duration_from_item(item)
            
            # URL предложения
            url = (
                item.get('url') or 
                item.get('link') or 
                item.get('offerUrl') or
                ""
            )
            
            if hotel_name and price:
                return {
                    'hotel_name': hotel_name,
                    'price': price,
                    'dates': dates,
                    'duration': duration,
                    'rating': item.get('rating', ''),
                    'scraped_at': datetime.now().isoformat(),
                    'url': url
                }
                
        except Exception as e:
            logger.warning(f"Ошибка парсинга предложения: {e}")
        
        return None
    
    def extract_price_from_item(self, item: Dict[str, Any]) -> float:
        """Извлекает цену из элемента предложения"""
        
        # Пробуем разные поля для цены
        price_fields = ['price', 'cost', 'amount', 'totalPrice', 'total_price', 'value']
        
        for field in price_fields:
            if field in item:
                price_value = item[field]
                if isinstance(price_value, (int, float)):
                    return float(price_value)
                elif isinstance(price_value, str):
                    # Извлекаем число из строки
                    import re
                    numbers = re.findall(r'[\d,]+', price_value.replace('.', '').replace(',', '.'))
                    if numbers:
                        return float(numbers[0])
        
        return 0.0
    
    def extract_dates_from_item(self, item: Dict[str, Any]) -> str:
        """Извлекает даты из элемента предложения"""
        
        # Пробуем разные поля для дат
        date_fields = ['dates', 'dateRange', 'date_range', 'period', 'travelDates']
        
        for field in date_fields:
            if field in item:
                date_value = item[field]
                if isinstance(date_value, str) and date_value.strip():
                    return date_value.strip()
        
        # Если есть отдельные поля дат
        if 'dateFrom' in item and 'dateTo' in item:
            return f"{item['dateFrom']} - {item['dateTo']}"
        
        return "20-09-2025 - 04-10-2025"  # Значение по умолчанию
    
    def extract_duration_from_item(self, item: Dict[str, Any]) -> str:
        """Извлекает длительность из элемента предложения"""
        
        # Пробуем разные поля для длительности
        duration_fields = ['duration', 'nights', 'days', 'length', 'period']
        
        for field in duration_fields:
            if field in item:
                duration_value = item[field]
                if isinstance(duration_value, str) and duration_value.strip():
                    return duration_value.strip()
                elif isinstance(duration_value, (int, float)):
                    return f"{int(duration_value)} dni"
        
        return "6-15 дней"  # Значение по умолчанию
    
    async def save_offers(self, offers: List[Dict[str, Any]]):
        """Сохраняет предложения в CSV файл"""
        
        if not offers:
            logger.warning("Нет предложений для сохранения")
            return
        
        # Создаем директорию если не существует
        os.makedirs(os.path.dirname(self.data_file), exist_ok=True)
        
        # Проверяем существует ли файл
        file_exists = os.path.exists(self.data_file)
        
        # Сохраняем данные
        with open(self.data_file, 'a', newline='', encoding='utf-8') as f:
            fieldnames = ['hotel_name', 'price', 'dates', 'duration', 'rating', 'scraped_at', 'url']
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            
            # Записываем заголовки только если файл новый
            if not file_exists:
                writer.writeheader()
            
            # Записываем данные
            for offer in offers:
                writer.writerow(offer)
        
        logger.info(f"✅ Сохранено {len(offers)} предложений в {self.data_file}")

async def main():
    """Основная функция для тестирования API подхода"""
    
    logger.info("🚀 Запуск API Travel Monitor...")
    
    async with APITravelMonitor() as monitor:
        # 1. Обнаружение API endpoints
        logger.info("\n=== 1. Обнаружение API endpoints ===")
        api_endpoints = await monitor.discover_api_endpoints()
        
        # Сохраняем результаты обнаружения
        with open('api_endpoints_discovery.json', 'w', encoding='utf-8') as f:
            json.dump(api_endpoints, f, indent=2, ensure_ascii=False)
        
        # 2. Тестирование поисковых endpoints
        logger.info("\n=== 2. Тестирование поисковых endpoints ===")
        search_results = await monitor.test_search_endpoints()
        
        # Сохраняем результаты поиска
        with open('api_search_tests.json', 'w', encoding='utf-8') as f:
            json.dump(search_results, f, indent=2, ensure_ascii=False)
        
        # 3. Попытка поиска предложений
        logger.info("\n=== 3. Поиск предложений через API ===")
        offers = await monitor.search_offers_api()
        
        if offers:
            logger.info(f"✅ Найдено {len(offers)} предложений через API")
            
            # Сохраняем предложения
            await monitor.save_offers(offers)
            
            # Показываем примеры
            logger.info("\n📊 Примеры найденных предложений:")
            for i, offer in enumerate(offers[:3]):
                logger.info(f"  {i+1}. {offer['hotel_name']} - {offer['price']} PLN")
        else:
            logger.warning("❌ Не удалось найти предложения через API")
        
        # 4. Сводка результатов
        logger.info("\n" + "="*50)
        logger.info("📊 СВОДКА РЕЗУЛЬТАТОВ")
        logger.info("="*50)
        
        successful_apis = [k for k, v in api_endpoints.items() if v.get('success')]
        successful_searches = [k for k, v in search_results.items() if v.get('success')]
        
        logger.info(f"✅ Найдено API endpoints: {len(successful_apis)}")
        logger.info(f"✅ Рабочих поисковых endpoints: {len(successful_searches)}")
        logger.info(f"✅ Найдено предложений: {len(offers)}")
        
        if successful_apis:
            logger.info(f"\n🎯 Рабочие API endpoints:")
            for endpoint in successful_apis:
                logger.info(f"   - {endpoint}")
        
        if successful_searches:
            logger.info(f"\n🎯 Рабочие поисковые endpoints:")
            for endpoint in successful_searches:
                logger.info(f"   - {endpoint}")
        
        logger.info(f"\n📄 Результаты сохранены:")
        logger.info(f"   - api_endpoints_discovery.json")
        logger.info(f"   - api_search_tests.json")
        if offers:
            logger.info(f"   - {monitor.data_file}")

if __name__ == "__main__":
    asyncio.run(main())
