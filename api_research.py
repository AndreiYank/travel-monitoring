#!/usr/bin/env python3
"""
Исследование API fly.pl для ускорения сбора данных
"""

import asyncio
import aiohttp
import json
import time
from datetime import datetime
from typing import Dict, List, Any
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class FlyPlAPIResearch:
    def __init__(self):
        self.base_url = "https://fly.pl"
        self.session = None
        
    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        return self
        
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()
    
    async def research_api_endpoints(self):
        """Исследует возможные API endpoints fly.pl"""
        
        # Возможные API endpoints для исследования
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
            "/api/offers/search",
            "/api/travel/offers",
            "/api/search/travel",
            "/api/booking/search",
            "/api/tours/search"
        ]
        
        logger.info("🔍 Исследуем возможные API endpoints...")
        
        results = {}
        
        for endpoint in potential_endpoints:
            url = f"{self.base_url}{endpoint}"
            try:
                # Пробуем GET запрос
                async with self.session.get(url, timeout=10) as response:
                    if response.status == 200:
                        content_type = response.headers.get('content-type', '')
                        if 'json' in content_type:
                            data = await response.json()
                            results[endpoint] = {
                                'status': response.status,
                                'content_type': content_type,
                                'data_preview': str(data)[:200] + "..." if len(str(data)) > 200 else str(data)
                            }
                            logger.info(f"✅ Найден API endpoint: {endpoint}")
                        else:
                            results[endpoint] = {
                                'status': response.status,
                                'content_type': content_type,
                                'note': 'Not JSON response'
                            }
                    else:
                        results[endpoint] = {
                            'status': response.status,
                            'note': 'Not found or error'
                        }
                        
            except Exception as e:
                results[endpoint] = {
                    'error': str(e)
                }
        
        return results
    
    async def test_search_with_params(self):
        """Тестирует поиск с параметрами из текущего URL"""
        
        # Параметры из текущего конфига
        search_params = {
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
        }
        
        # Возможные API endpoints для POST запросов
        post_endpoints = [
            "/api/search",
            "/api/search/offers", 
            "/api/travel/search",
            "/api/offers/search",
            "/search/api",
            "/api/v1/search"
        ]
        
        logger.info("🔍 Тестируем POST запросы с параметрами поиска...")
        
        results = {}
        
        for endpoint in post_endpoints:
            url = f"{self.base_url}{endpoint}"
            try:
                # Пробуем POST с JSON
                async with self.session.post(
                    url, 
                    json=search_params,
                    headers={'Content-Type': 'application/json'},
                    timeout=15
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
                            logger.info(f"✅ POST JSON успешен: {endpoint}")
                        except:
                            text = await response.text()
                            results[endpoint]['text_preview'] = text[:300] + "..." if len(text) > 300 else text
                            
            except Exception as e:
                results[endpoint] = {
                    'method': 'POST JSON',
                    'error': str(e)
                }
        
        return results
    
    async def analyze_network_requests(self):
        """Анализирует возможные сетевые запросы"""
        
        # Имитируем запрос к главной странице поиска
        search_url = "https://fly.pl/kierunek/turcja/"
        
        logger.info("🔍 Анализируем сетевые запросы...")
        
        try:
            async with self.session.get(search_url, timeout=10) as response:
                if response.status == 200:
                    html = await response.text()
                    
                    # Ищем упоминания API в HTML
                    api_mentions = []
                    lines = html.split('\n')
                    for i, line in enumerate(lines):
                        if 'api' in line.lower() and ('search' in line.lower() or 'offer' in line.lower()):
                            api_mentions.append({
                                'line': i + 1,
                                'content': line.strip()[:200]
                            })
                    
                    return {
                        'status': response.status,
                        'api_mentions': api_mentions[:10],  # Первые 10 упоминаний
                        'total_lines': len(lines)
                    }
                    
        except Exception as e:
            return {'error': str(e)}
    
    async def test_ajax_requests(self):
        """Тестирует возможные AJAX запросы"""
        
        # Заголовки для имитации браузера
        headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'pl-PL,pl;q=0.9,en;q=0.8',
            'Referer': 'https://fly.pl/kierunek/turcja/',
            'X-Requested-With': 'XMLHttpRequest'
        }
        
        # Возможные AJAX endpoints
        ajax_endpoints = [
            "/ajax/search",
            "/ajax/offers",
            "/ajax/travel/search",
            "/search/ajax",
            "/offers/ajax",
            "/api/ajax/search",
            "/api/ajax/offers"
        ]
        
        logger.info("🔍 Тестируем AJAX endpoints...")
        
        results = {}
        
        for endpoint in ajax_endpoints:
            url = f"{self.base_url}{endpoint}"
            try:
                async with self.session.get(url, headers=headers, timeout=10) as response:
                    results[endpoint] = {
                        'status': response.status,
                        'content_type': response.headers.get('content-type', ''),
                        'headers': dict(response.headers)
                    }
                    
                    if response.status == 200:
                        try:
                            data = await response.json()
                            results[endpoint]['data_preview'] = str(data)[:200] + "..." if len(str(data)) > 200 else str(data)
                            logger.info(f"✅ AJAX endpoint найден: {endpoint}")
                        except:
                            text = await response.text()
                            results[endpoint]['text_preview'] = text[:200] + "..." if len(text) > 200 else text
                            
            except Exception as e:
                results[endpoint] = {'error': str(e)}
        
        return results

async def main():
    """Основная функция исследования"""
    
    logger.info("🚀 Начинаем исследование API fly.pl...")
    start_time = time.time()
    
    async with FlyPlAPIResearch() as researcher:
        # 1. Исследуем API endpoints
        logger.info("\n=== 1. Исследование API endpoints ===")
        api_results = await researcher.research_api_endpoints()
        
        # 2. Тестируем поиск с параметрами
        logger.info("\n=== 2. Тестирование поиска с параметрами ===")
        search_results = await researcher.test_search_with_params()
        
        # 3. Анализируем сетевые запросы
        logger.info("\n=== 3. Анализ сетевых запросов ===")
        network_results = await researcher.analyze_network_requests()
        
        # 4. Тестируем AJAX запросы
        logger.info("\n=== 4. Тестирование AJAX endpoints ===")
        ajax_results = await researcher.test_ajax_requests()
        
        # Сохраняем результаты
        all_results = {
            'timestamp': datetime.now().isoformat(),
            'api_endpoints': api_results,
            'search_tests': search_results,
            'network_analysis': network_results,
            'ajax_tests': ajax_results
        }
        
        with open('api_research_results.json', 'w', encoding='utf-8') as f:
            json.dump(all_results, f, indent=2, ensure_ascii=False)
        
        # Выводим краткий отчет
        logger.info("\n" + "="*50)
        logger.info("📊 ОТЧЕТ ОБ ИССЛЕДОВАНИИ API")
        logger.info("="*50)
        
        successful_apis = [k for k, v in api_results.items() if v.get('status') == 200]
        successful_searches = [k for k, v in search_results.items() if v.get('status') == 200]
        successful_ajax = [k for k, v in ajax_results.items() if v.get('status') == 200]
        
        logger.info(f"✅ Найдено API endpoints: {len(successful_apis)}")
        logger.info(f"✅ Успешных поисков: {len(successful_searches)}")
        logger.info(f"✅ AJAX endpoints: {len(successful_ajax)}")
        logger.info(f"⏱️ Время исследования: {time.time() - start_time:.2f} сек")
        logger.info(f"📄 Результаты сохранены: api_research_results.json")
        
        if successful_apis:
            logger.info(f"\n🎯 Найденные API endpoints:")
            for endpoint in successful_apis:
                logger.info(f"   - {endpoint}")
        
        if successful_searches:
            logger.info(f"\n🎯 Рабочие поисковые endpoints:")
            for endpoint in successful_searches:
                logger.info(f"   - {endpoint}")
        
        if successful_ajax:
            logger.info(f"\n🎯 AJAX endpoints:")
            for endpoint in successful_ajax:
                logger.info(f"   - {endpoint}")

if __name__ == "__main__":
    asyncio.run(main())
