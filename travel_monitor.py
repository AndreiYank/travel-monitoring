#!/usr/bin/env python3
"""
Основной скрипт для мониторинга цен на путешествия с сайта fly.pl
- Исправлены проблемы с таймаутами
- Добавляет данные к существующим (не перезаписывает)
- Более надежный парсинг
"""

import asyncio
import json
import csv
import os
import sys
from datetime import datetime
from typing import List, Dict, Any
import pandas as pd
import matplotlib.pyplot as plt
from playwright.async_api import async_playwright
import logging

# Настройка логирования
logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('monitor.log', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

class TravelPriceMonitor:
    def __init__(self, config_file: str = "config.json"):
        self.config_file = config_file
        self.data_file = "travel_prices.csv"
        self.config = self.load_config()
        
    def load_config(self) -> Dict[str, Any]:
        """Загружает конфигурацию из файла"""
        try:
            with open(self.config_file, 'r', encoding='utf-8') as f:
                config = json.load(f)
            logger.info(f"Конфигурация загружена из {self.config_file}")
            return config
        except Exception as e:
            logger.error(f"Ошибка загрузки конфигурации: {e}")
            sys.exit(1)

    async def scrape_offers_with_retry(self) -> List[Dict[str, Any]]:
        """Парсит предложения с повторными попытками"""
        for attempt in range(self.config['max_retries']):
            try:
                logger.info(f"Попытка {attempt + 1}/{self.config['max_retries']}")
                offers = await self.scrape_offers()
                if offers:
                    return offers
                else:
                    logger.warning(f"Попытка {attempt + 1} не дала результатов")
            except Exception as e:
                logger.error(f"Ошибка в попытке {attempt + 1}: {e}")
                if attempt < self.config['max_retries'] - 1:
                    logger.info(f"Ждем {self.config['retry_delay']} секунд...")
                    await asyncio.sleep(self.config['retry_delay'])
        
        logger.error("Все попытки исчерпаны")
        return []

    async def scrape_offers(self) -> List[Dict[str, Any]]:
        """Парсит предложения с сайта fly.pl"""
        offers = []
        
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    '--no-sandbox',
                    '--disable-dev-shm-usage',
                    '--disable-gpu',
                    '--disable-web-security'
                ]
            )
            
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                viewport={'width': 1920, 'height': 1080}
            )
            
            page = await context.new_page()
            
            try:
                logger.info(f"Переходим на страницу: {self.config['url']}")
                
                # Устанавливаем таймауты
                page.set_default_timeout(self.config['wait_timeout'])
                
                # Переходим на страницу
                response = await page.goto(
                    self.config['url'], 
                    wait_until='domcontentloaded',
                    timeout=self.config['wait_timeout']
                )
                
                if not response or response.status >= 400:
                    raise Exception(f"Ошибка загрузки: {response.status if response else 'No response'}")
                
                logger.info("Страница загружена, ждем контент...")
                await page.wait_for_timeout(5000)
                
                # Ищем предложения
                offers_data = await self.find_offers(page)
                
                if not offers_data:
                    logger.warning("Предложения не найдены, пробуем альтернативный подход...")
                    offers_data = await self.find_offers_alternative(page)
                
                # Парсим предложения
                max_offers = min(self.config['max_offers'], len(offers_data)) if offers_data else 0
                logger.info(f"Парсим {max_offers} предложений...")
                
                for i in range(max_offers):
                    try:
                        element = offers_data[i]
                        offer_data = await self.extract_offer_data(element, i)
                        if offer_data and offer_data.get('price', 0) > 0:
                            offers.append(offer_data)
                    except Exception as e:
                        logger.warning(f"Ошибка парсинга предложения {i}: {e}")
                        continue
                
                logger.info(f"Успешно собрано {len(offers)} предложений")
                
            except Exception as e:
                logger.error(f"Ошибка при парсинге: {e}")
            finally:
                try:
                    await browser.close()
                except:
                    pass
        
        return offers

    async def find_offers(self, page) -> List:
        """Ищет предложения на странице"""
        selectors_to_try = [
            '.offer-item',
            '.trip-item', 
            '.hotel-item',
            '.search-result-item',
            '[data-testid*="offer"]',
            '.result-item',
            '.offer',
            '.trip',
            '.hotel',
            '[class*="offer"]',
            '[class*="trip"]',
            '[class*="hotel"]'
        ]
        
        for selector in selectors_to_try:
            try:
                await page.wait_for_selector(selector, timeout=10000)
                elements = await page.query_selector_all(selector)
                if elements and len(elements) > 0:
                    logger.info(f"Найдено {len(elements)} предложений с селектором: {selector}")
                    return elements
            except:
                continue
        
        return []

    async def find_offers_alternative(self, page) -> List:
        """Альтернативный поиск предложений"""
        try:
            # Ищем элементы с ценами
            price_elements = await page.query_selector_all('[class*="price"], [class*="cost"], [class*="amount"]')
            if price_elements:
                logger.info(f"Найдено {len(price_elements)} элементов с ценами")
                return price_elements[:50]
            
            # Ищем любые div элементы
            all_divs = await page.query_selector_all('div')
            if all_divs:
                logger.info(f"Найдено {len(all_divs)} div элементов")
                return all_divs[:100]
                
        except Exception as e:
            logger.warning(f"Ошибка альтернативного поиска: {e}")
        
        return []

    async def extract_offer_data(self, element, index: int) -> Dict[str, Any]:
        """Извлекает данные из элемента предложения"""
        try:
            # Получаем весь текст элемента
            full_text = await element.inner_text()
            if not full_text or len(full_text.strip()) < 10:
                return None
            
            # Ищем название отеля/тура
            hotel_name = await self.extract_text_by_selectors(element, [
                'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
                '.title', '.name', '.hotel-name', '.offer-title',
                '[class*="title"]', '[class*="name"]', '[class*="hotel"]'
            ])
            
            # Ищем цену
            price = await self.extract_text_by_selectors(element, [
                '.price', '.cost', '.amount', '.value',
                '[class*="price"]', '[class*="cost"]', '[class*="amount"]'
            ])
            
            # Ищем даты
            dates = await self.extract_text_by_selectors(element, [
                '.date', '.dates', '.departure', '.arrival',
                '[class*="date"]', '[class*="time"]'
            ])
            
            # Ищем длительность
            duration = await self.extract_text_by_selectors(element, [
                '.duration', '.nights', '.days',
                '[class*="duration"]', '[class*="nights"]'
            ])
            
            # Ищем рейтинг
            rating = await self.extract_text_by_selectors(element, [
                '.rating', '.stars', '.score',
                '[class*="rating"]', '[class*="stars"]'
            ])
            
            # Очищаем и форматируем данные
            hotel_name = self.clean_text(hotel_name) if hotel_name else f"Предложение {index + 1}"
            price_value = self.extract_price(price) if price else 0
            dates = self.clean_text(dates) if dates else ""
            duration = self.clean_text(duration) if duration else ""
            rating = self.clean_text(rating) if rating else ""
            
            # Если не нашли название, используем первые слова из текста
            if not hotel_name or hotel_name == f"Предложение {index + 1}":
                words = full_text.split()[:5]
                hotel_name = " ".join(words) if words else f"Предложение {index + 1}"
            
            return {
                'hotel_name': hotel_name[:100],
                'price': price_value,
                'dates': dates[:50],
                'duration': duration[:30],
                'rating': rating[:20],
                'scraped_at': datetime.now().isoformat(),
                'url': self.config['url']
            }
            
        except Exception as e:
            logger.warning(f"Ошибка извлечения данных из элемента {index}: {e}")
            return None

    async def extract_text_by_selectors(self, element, selectors: List[str]) -> str:
        """Извлекает текст используя различные селекторы"""
        for selector in selectors:
            try:
                sub_element = await element.query_selector(selector)
                if sub_element:
                    text = await sub_element.inner_text()
                    if text and text.strip():
                        return text.strip()
            except:
                continue
        return ""

    def clean_text(self, text: str) -> str:
        """Очищает текст от лишних символов"""
        if not text:
            return ""
        return ' '.join(text.split())

    def extract_price(self, price_text: str) -> float:
        """Извлекает числовое значение цены из текста"""
        if not price_text:
            return 0
        
        import re
        # Ищем числа в тексте
        numbers = re.findall(r'[\d\s,]+', price_text.replace('.', '').replace(',', '.'))
        if numbers:
            try:
                price_str = numbers[0].replace(' ', '')
                return float(price_str)
            except:
                pass
        return 0

    def save_data_append(self, offers: List[Dict[str, Any]]):
        """Сохраняет данные, добавляя к существующим"""
        if not offers:
            logger.warning("Нет данных для сохранения")
            return
        
        # Создаем директорию
        os.makedirs(self.config['data_dir'], exist_ok=True)
        
        filepath = os.path.join(self.config['data_dir'], self.data_file)
        
        # Загружаем существующие данные для проверки дубликатов
        existing_data = []
        if os.path.exists(filepath):
            try:
                existing_df = pd.read_csv(filepath)
                existing_data = existing_df.to_dict('records')
                logger.info(f"Загружено {len(existing_data)} существующих записей")
            except Exception as e:
                logger.warning(f"Ошибка загрузки существующих данных: {e}")
        
        # Фильтруем дубликаты
        new_offers = []
        for offer in offers:
            is_duplicate = False
            for existing in existing_data:
                if (offer['hotel_name'] == existing['hotel_name'] and 
                    offer['price'] == existing['price'] and
                    offer['dates'] == existing['dates']):
                    is_duplicate = True
                    break
            
            if not is_duplicate:
                new_offers.append(offer)
        
        if not new_offers:
            logger.info("Все предложения уже существуют в базе данных")
            return
        
        # Добавляем новые данные
        with open(filepath, 'a', newline='', encoding='utf-8') as csvfile:
            fieldnames = ['hotel_name', 'price', 'dates', 'duration', 'rating', 'scraped_at', 'url']
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            
            if not os.path.exists(filepath):
                writer.writeheader()
            
            for offer in new_offers:
                writer.writerow(offer)
        
        logger.info(f"Добавлено {len(new_offers)} новых предложений в {filepath}")

    def create_charts(self):
        """Создает графики"""
        try:
            df = self.load_data()
            
            if df.empty:
                logger.warning("Нет данных для создания графиков")
                return
            
            # Создаем директорию для графиков
            charts_dir = os.path.join(self.config['data_dir'], 'charts')
            os.makedirs(charts_dir, exist_ok=True)
            
            # График 1: Изменение цен по времени
            plt.figure(figsize=(15, 8))
            
            df['scraped_at'] = pd.to_datetime(df['scraped_at'])
            daily_prices = df.groupby(df['scraped_at'].dt.date)['price'].agg(['mean', 'min', 'max'])
            
            plt.plot(daily_prices.index, daily_prices['mean'], marker='o', linewidth=2, label='Средняя цена')
            plt.fill_between(daily_prices.index, daily_prices['min'], daily_prices['max'], alpha=0.3, label='Диапазон цен')
            
            plt.title('Динамика цен на путешествия', fontsize=16)
            plt.xlabel('Дата', fontsize=12)
            plt.ylabel('Цена (PLN)', fontsize=12)
            plt.legend()
            plt.grid(True, alpha=0.3)
            plt.xticks(rotation=45)
            plt.tight_layout()
            
            chart_path = os.path.join(charts_dir, 'price_timeline.png')
            plt.savefig(chart_path, dpi=300, bbox_inches='tight')
            plt.close()
            
            # График 2: Топ-10 самых дешевых предложений
            plt.figure(figsize=(15, 8))
            top_cheap = df.nsmallest(10, 'price')
            
            bars = plt.barh(range(len(top_cheap)), top_cheap['price'])
            plt.yticks(range(len(top_cheap)), 
                      [name[:40] + '...' if len(name) > 40 else name for name in top_cheap['hotel_name']])
            
            plt.title('Топ-10 самых дешевых предложений', fontsize=16)
            plt.xlabel('Цена (PLN)', fontsize=12)
            plt.grid(True, alpha=0.3)
            
            # Добавляем значения на столбцы
            for i, (bar, price) in enumerate(zip(bars, top_cheap['price'])):
                plt.text(bar.get_width() + 50, bar.get_y() + bar.get_height()/2, 
                        f'{price:.0f} PLN', ha='left', va='center')
            
            plt.tight_layout()
            chart_path = os.path.join(charts_dir, 'top_cheap_offers.png')
            plt.savefig(chart_path, dpi=300, bbox_inches='tight')
            plt.close()
            
            logger.info(f"Графики сохранены в {charts_dir}")
            
        except Exception as e:
            logger.error(f"Ошибка создания графиков: {e}")

    def load_data(self) -> pd.DataFrame:
        """Загружает данные из CSV"""
        filepath = os.path.join(self.config['data_dir'], self.data_file)
        
        if not os.path.exists(filepath):
            return pd.DataFrame()
        
        try:
            df = pd.read_csv(filepath)
            return df
        except Exception as e:
            logger.error(f"Ошибка загрузки данных: {e}")
            return pd.DataFrame()

    def generate_report(self):
        """Генерирует отчет"""
        try:
            df = self.load_data()
            
            if df.empty:
                logger.warning("Нет данных для генерации отчета")
                return
            
            report_path = os.path.join(self.config['data_dir'], 'price_report.txt')
            
            with open(report_path, 'w', encoding='utf-8') as f:
                f.write("=== ОТЧЕТ ПО МОНИТОРИНГУ ЦЕН НА ПУТЕШЕСТВИЯ ===\n\n")
                f.write(f"Дата генерации: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"URL: {self.config['url']}\n\n")
                
                f.write("=== СТАТИСТИКА ===\n")
                f.write(f"Общее количество предложений: {len(df)}\n")
                f.write(f"Уникальных отелей: {df['hotel_name'].nunique()}\n")
                f.write(f"Средняя цена: {df['price'].mean():.2f} PLN\n")
                f.write(f"Минимальная цена: {df['price'].min():.2f} PLN\n")
                f.write(f"Максимальная цена: {df['price'].max():.2f} PLN\n\n")
                
                f.write("=== ТОП-5 САМЫХ ДЕШЕВЫХ ПРЕДЛОЖЕНИЙ ===\n")
                top_cheap = df.nsmallest(5, 'price')
                for i, (_, row) in enumerate(top_cheap.iterrows(), 1):
                    f.write(f"{i}. {row['hotel_name']} - {row['price']:.2f} PLN\n")
                    if row['dates']:
                        f.write(f"   Даты: {row['dates']}\n")
                    f.write(f"   Собрано: {row['scraped_at']}\n\n")
            
            logger.info(f"Отчет сохранен: {report_path}")
            
        except Exception as e:
            logger.error(f"Ошибка генерации отчета: {e}")

    async def run_monitoring(self):
        """Запускает полный цикл мониторинга"""
        logger.info("🚀 Начинаем мониторинг цен на путешествия...")
        
        try:
            # Собираем данные с повторными попытками
            offers = await self.scrape_offers_with_retry()
            
            if not offers:
                logger.error("❌ Не удалось собрать данные после всех попыток")
                return False
            
            # Сохраняем данные (добавляем к существующим)
            self.save_data_append(offers)
            
            # Создаем графики
            self.create_charts()
            
            # Генерируем отчет
            self.generate_report()
            
            logger.info("✅ Мониторинг завершен успешно!")
            return True
            
        except Exception as e:
            logger.error(f"❌ Критическая ошибка: {e}")
            return False

def main():
    """Главная функция"""
    monitor = TravelPriceMonitor()
    
    try:
        success = asyncio.run(monitor.run_monitoring())
        if success:
            print("✅ Мониторинг завершен успешно!")
            sys.exit(0)
        else:
            print("❌ Мониторинг завершен с ошибками")
            sys.exit(1)
    except KeyboardInterrupt:
        logger.info("Мониторинг прерван пользователем")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Неожиданная ошибка: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
