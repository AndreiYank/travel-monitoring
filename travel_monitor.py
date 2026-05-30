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
import re
import html as ihtml
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
import pandas as pd
import requests
import matplotlib.pyplot as plt
from playwright.async_api import async_playwright
import logging
from price_alerts import PriceAlertManager
from price_alerts_v2 import PriceAlertManagerV2
from airport_comparison import AirportComparison

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
    def __init__(self, config_file: str = "config.json", data_file: Optional[str] = None):
        self.config_file = config_file
        self.config = self.load_config()
        # data_file из аргументов имеет приоритет над output_data_file из конфигурации
        self.data_file = data_file or self.config.get('output_data_file', 'travel_prices.csv')
        
    def load_config(self) -> Dict[str, Any]:
        """Загружает конфигурацию из файла"""
        try:
            with open(self.config_file, 'r', encoding='utf-8') as f:
                config = json.load(f)
            logger.info(f"Конфигурация загружена из {self.config_file}")
            
            if 'url' in config and config['url']:
                config['url'] = self._make_url_dynamic(config['url'])
                
            return config
        except Exception as e:
            logger.error(f"Ошибка загрузки конфигурации: {e}")
            sys.exit(1)

    def _make_url_dynamic(self, url: str) -> str:
        """Динамически заменяет даты в URL на период: сегодня + 30 дней"""
        import re
        from datetime import datetime, timedelta
        
        now = datetime.now()
        from_date_str = now.strftime('%d-%m-%Y')
        to_date_str = (now + timedelta(days=30)).strftime('%d-%m-%Y')
        
        # Регулярные выражения для замены whenFrom и whenTo (с поддержкой [ ] и %5B %5D)
        url = re.sub(
            r'(filter(?:\[|%5B)whenFrom(?:\]|%5D)=)([^&]*)',
            rf'\g<1>{from_date_str}',
            url
        )
        url = re.sub(
            r'(filter(?:\[|%5B)whenTo(?:\]|%5D)=)([^&]*)',
            rf'\g<1>{to_date_str}',
            url
        )
        
        logger.info(f"🔄 URL обновлен динамически: с {from_date_str} по {to_date_str}")
        return url


    async def scrape_offers_with_retry(self) -> List[Dict[str, Any]]:
        """Парсит предложения.

        Сначала пробуем быстрый путь: прямой HTTP-запрос + разбор server-side HTML
        (на порядок быстрее, без запуска браузера). Если он не дал результата
        (например, изменилась разметка или включился анти-бот) — автоматически
        откатываемся на надёжный браузерный парсинг через Playwright.
        """
        if self.config.get('disable_http_fast_path') is not True:
            try:
                offers = self.scrape_offers_http()
                if offers:
                    logger.info(f"⚡ Быстрый HTTP-парсинг успешен: {len(offers)} предложений")
                    return offers
                logger.warning("HTTP-парсинг не дал результатов — переключаемся на Playwright")
            except Exception as e:
                logger.warning(f"HTTP-парсинг не удался ({e}) — переключаемся на Playwright")

        for attempt in range(self.config['max_retries']):
            try:
                logger.info(f"Попытка {attempt + 1}/{self.config['max_retries']} (Playwright)")
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

    def _build_page_url(self, page_number: int) -> str:
        """Строит URL страницы выдачи. Пагинация fly.pl — через сегмент пути p:N.

        Важно: параметр filter[fp]=1 принудительно возвращает первую страницу,
        поэтому для N>1 его нужно убрать (так же делают «родные» ссылки сайта).
        """
        base = self.config['url']
        if page_number <= 1:
            return base
        if '?' in base:
            path, query = base.split('?', 1)
        else:
            path, query = base, ''
        # Удаляем filter[fp]=... (в обеих формах: [ ] и %5B %5D), иначе вернётся страница 1
        query = re.sub(r'filter(?:\[|%5B)fp(?:\]|%5D)=[^&]*&?', '', query)
        query = query.strip('&')
        if not path.endswith('/'):
            path += '/'
        query_part = f"?{query}" if query else ''
        return f"{path}p:{page_number}/{query_part}"

    def _parse_offers_from_html(self, page_html: str) -> List[Dict[str, Any]]:
        """Извлекает все офферы со страницы за один проход по HTML.

        fly.pl рендерит карточки на сервере со schema.org-разметкой (RDFa),
        поэтому все нужные поля доступны в чистых атрибутах/мета-тегах:
          - data-phref / meta[property=schema:url] → ссылка на оффер
          - h2[property=schema:name] (текст)       → название отеля
          - div[rel=schema:image] resource         → фото (надёжнее, чем <img>)
          - data-priceperall                        → цена «за всех»
          - текст карточки DD.MM.YYYY - DD.MM.YYYY (N dni/M nocy) → даты/длительность
        """
        offers: List[Dict[str, Any]] = []
        cards = re.split(r'<div class="card-offer-search', page_html)[1:]
        departure_airport = self.extract_departure_airport_from_url(self.config['url'])

        for card in cards:
            def find(pattern: str, flags: int = 0) -> str:
                m = re.search(pattern, card, flags)
                return ihtml.unescape(m.group(1)).strip() if m else ''

            # Ссылка на оффер: предпочитаем декодированный schema:url, иначе data-phref
            offer_url = find(r'property="schema:url"[^>]*content="([^"]+)"')
            if not offer_url:
                offer_url = find(r'data-phref="([^"]+)"')

            # Название отеля — текст внутри <h2 property="schema:name"> (короткая форма,
            # совместимая с историей; content-атрибут содержит лишнюю геолокацию)
            name = ''
            mh = re.search(r'<h2[^>]*property="schema:name"[^>]*>(.*?)</h2>', card, re.S)
            if mh:
                name = self.clean_text(re.sub(r'<[^>]+>', '', mh.group(1)))
            if not name:
                name = find(r'property="schema:name"[^>]*content="([^"]+)"')

            # Фото из resource-атрибута schema:image
            image_url = find(r'rel="schema:image"[^>]*resource="([^"]+)"')
            if image_url:
                image_url = self.make_absolute_url(image_url)

            # Цена «за всех»; запасной вариант — цена за человека из schema:price
            total = find(r'data-priceperall="([^"]+)"')
            price_value = self.extract_price(total) if total else 0
            if not price_value:
                pp = find(r'property="schema:price"[^>]*content="([^"]+)"')
                price_value = self.extract_price(pp) if pp else 0

            # Даты и длительность из текста карточки
            md = re.search(
                r'(\d{2}\.\d{2}\.\d{4}\s*-\s*\d{2}\.\d{2}\.\d{4})\s*\(([^)]*?dni[^)]*)\)',
                card,
            )
            dates = md.group(1).strip() if md else ''
            duration = md.group(2).strip() if md else ''

            offers.append({
                'hotel_name': (name or 'Предложение')[:100],
                'price': price_value,
                'dates': dates[:50],
                'duration': duration[:30],
                'rating': '',
                'departure_airport': departure_airport,
                'scraped_at': datetime.now(timezone.utc).isoformat(),
                'url': self.config['url'],
                'image_url': image_url or '',
                'offer_url': offer_url or '',
            })

        return offers

    def scrape_offers_http(self) -> List[Dict[str, Any]]:
        """Быстрый парсинг через прямые HTTP-запросы (без браузера).

        Постранично запрашивает выдачу (пагинация p:N), разбирает HTML и применяет
        те же фильтры (мин/макс цена, целевой scope), что и браузерный путь.
        """
        max_price_threshold = float(
            self.config.get('max_price_threshold')
            or self._extract_price_limit()
            or 8100
        )
        min_price_threshold = float(self.config.get('min_price_threshold', 0))
        max_pages = int(self.config.get('max_pages', 10))
        max_offers = int(self.config.get('max_offers', 0))
        timeout_s = float(self.config.get('wait_timeout', 30000)) / 1000.0

        session = requests.Session()
        session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                          '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'pl-PL,pl;q=0.9,en;q=0.8',
        })

        logger.info(
            f"⚡ HTTP-режим: лимит цены {max_price_threshold:.0f} PLN, "
            f"мин. цена {min_price_threshold:.0f} PLN, макс. страниц {max_pages}"
        )

        all_offers: List[Dict[str, Any]] = []
        page_number = 1
        while page_number <= max_pages:
            url = self._build_page_url(page_number)
            resp = session.get(url, timeout=timeout_s)
            if resp.status_code != 200:
                logger.warning(f"HTTP {resp.status_code} на странице {page_number}")
                break

            raw_offers = self._parse_offers_from_html(resp.text)
            if not raw_offers:
                logger.info(f"Страница {page_number}: карточки не найдены, завершаем")
                break

            page_offers = []
            max_price_on_page = 0
            filtered_out = 0
            for off in raw_offers:
                if off.get('price', 0) <= 0:
                    continue
                if min_price_threshold > 0 and off['price'] < min_price_threshold:
                    continue
                if not self._is_offer_in_expected_scope(off):
                    filtered_out += 1
                    continue
                page_offers.append(off)
                max_price_on_page = max(max_price_on_page, off['price'])

            if page_offers:
                all_offers.extend(page_offers)
                logger.info(
                    f"Страница {page_number}: собрано {len(page_offers)} "
                    f"(отфильтровано {filtered_out}), макс. цена {max_price_on_page:.0f} PLN"
                )
                if max_offers > 0 and len(all_offers) >= max_offers:
                    all_offers = all_offers[:max_offers]
                    logger.info(f"Достигнут лимит предложений ({max_offers}), завершаем")
                    break
                if max_price_on_page >= max_price_threshold:
                    logger.info(f"Достигнута максимальная цена {max_price_threshold:.0f} PLN, завершаем")
                    break
            else:
                logger.info(f"Страница {page_number}: после фильтра не осталось предложений, завершаем")
                break

            page_number += 1

        logger.info(f"⚡ HTTP-парсинг завершён: {len(all_offers)} предложений с {page_number} страниц")
        return all_offers

    async def scrape_offers(self) -> List[Dict[str, Any]]:
        """Парсит предложения с сайта fly.pl с пагинацией"""
        all_offers = []
        page_number = 1
        max_price_threshold = float(
            self.config.get('max_price_threshold')
            or self._extract_price_limit()
            or 8100
        )  # Максимальная цена для остановки
        min_price_threshold = float(
            self.config.get('min_price_threshold', 0)
        )  # Минимальная цена (фильтр)
        max_pages = int(self.config.get('max_pages', 10))
        max_offers = int(self.config.get('max_offers', 0))  # 0 = без лимита
        logger.info(f"Лимит цены для остановки: {max_price_threshold:.0f} PLN")
        if min_price_threshold > 0:
            logger.info(f"Минимальная цена (фильтр): {min_price_threshold:.0f} PLN")
        logger.info(f"Лимит страниц: {max_pages}")
        if max_offers > 0:
            logger.info(f"Лимит предложений: {max_offers}")
        
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
                
                # Парсим страницы пока не достигнем максимальной цены
                while page_number <= max_pages:
                    logger.info(f"Парсим страницу {page_number}...")
                    
                    # Ищем предложения на текущей странице
                    offers_data = await self.find_offers(page)
                    
                    if not offers_data:
                        logger.warning("Предложения не найдены, пробуем альтернативный подход...")
                        offers_data = await self.find_offers_alternative(page)
                    
                        if not offers_data:
                            logger.info("Предложения не найдены, завершаем парсинг")
                            break
                    
                    # Парсим предложения с текущей страницы
                    page_offers = []
                    max_price_on_page = 0
                    filtered_out_on_scope = 0
                    
                    for i in range(len(offers_data)):
                        try:
                            element = offers_data[i]
                            offer_data = await self.extract_offer_data(element, i)
                            if offer_data and offer_data.get('price', 0) > 0:
                                # Фильтр по минимальной цене
                                if min_price_threshold > 0 and offer_data['price'] < min_price_threshold:
                                    continue
                                # Защитный фильтр от нерелевантных карточек (другие страны/формат поездки)
                                if not self._is_offer_in_expected_scope(offer_data):
                                    filtered_out_on_scope += 1
                                    continue
                                page_offers.append(offer_data)
                                max_price_on_page = max(max_price_on_page, offer_data['price'])
                        except Exception as e:
                            logger.warning(f"Ошибка парсинга предложения {i}: {e}")
                        continue
                    
                    if page_offers:
                        all_offers.extend(page_offers)
                        logger.info(f"Страница {page_number}: собрано {len(page_offers)} предложений, максимальная цена: {max_price_on_page:.0f} PLN")
                        if filtered_out_on_scope:
                            logger.info(f"Страница {page_number}: отфильтровано нерелевантных предложений: {filtered_out_on_scope}")

                        if max_offers > 0 and len(all_offers) >= max_offers:
                            all_offers = all_offers[:max_offers]
                            logger.info(f"Достигнут лимит предложений ({max_offers}), завершаем парсинг")
                            break
                        
                        # Проверяем, достигли ли максимальной цены
                        if max_price_on_page >= max_price_threshold:
                            logger.info(f"Достигнута максимальная цена {max_price_threshold} PLN, завершаем парсинг")
                            break
                    else:
                        logger.info(f"На странице {page_number} не найдено предложений")
                        break
                    
                    # Ищем кнопку "Следующая страница"
                    next_page_url = await self.find_next_page_url(page)
                    if not next_page_url:
                        logger.info("Кнопка 'Следующая страница' не найдена, завершаем парсинг")
                        break
                    
                    # Переходим на следующую страницу
                    logger.info(f"Переходим на страницу {page_number + 1}...")
                    try:
                        await page.goto(next_page_url, wait_until='domcontentloaded', timeout=self.config['wait_timeout'])
                        await page.wait_for_timeout(3000)  # Ждем загрузки контента
                        page_number += 1
                    except Exception as e:
                        logger.warning(f"Ошибка перехода на страницу {page_number + 1}: {e}")
                        break
                
                logger.info(f"Парсинг завершен. Всего собрано {len(all_offers)} предложений с {page_number} страниц")
                
            except Exception as e:
                logger.error(f"Ошибка при парсинге: {e}")
            finally:
                try:
                    await browser.close()
                except:
                    pass
        
        return all_offers

    def _extract_price_limit(self) -> Optional[float]:
        """Пробует достать лимит цены из URL (filter[PriceTo]=...)."""
        try:
            url = self.config.get('url', '') or ''
            import re
            m = re.search(r'(?:PriceTo]|PriceTo)=(\d+)', url)
            if m:
                return float(m.group(1))
        except Exception:
            pass
        return None

    def _extract_duration_range_from_url(self) -> Optional[tuple]:
        """Извлекает ожидаемый диапазон длительности из URL filter[duration]=X:Y."""
        try:
            import re
            url = self.config.get('url', '') or ''
            m = re.search(r'(?:duration=)(\d+):(\d+)', url)
            if not m:
                return None
            lo = int(m.group(1))
            hi = int(m.group(2))
            return (min(lo, hi), max(lo, hi))
        except Exception:
            return None

    def _extract_duration_days_value(self, duration_text: str) -> Optional[int]:
        """Пытается получить количество дней из текста длительности."""
        if not duration_text:
            return None
        try:
            import re
            m = re.search(r'(\d+)\s*(dni|days|day|d|nocleg|nights?)?', str(duration_text), re.IGNORECASE)
            if not m:
                return None
            return int(m.group(1))
        except Exception:
            return None

    def _is_offer_in_expected_scope(self, offer: Dict[str, Any]) -> bool:
        """Проверяет, что оффер соответствует целевому фильтру (страна/тип/длительность)."""
        offer_url = str(offer.get('offer_url') or '').strip().lower()
        if not offer_url:
            return False
        if '/wycieczka/' not in offer_url:
            return False

        # Явно исключаем "только проживание" (без перелета), это источник дешевого мусора.
        if 'transport=accommodation&' in offer_url and 'transport=accommodation_flight' not in offer_url:
            return False

        required = self.config.get('required_offer_url_contains', [])
        if isinstance(required, str):
            required = [required]
        required = [str(x).lower() for x in required if str(x).strip()]
        if required and not any(token in offer_url for token in required):
            return False

        excluded = self.config.get('excluded_offer_url_contains', [])
        if isinstance(excluded, str):
            excluded = [excluded]
        excluded = [str(x).lower() for x in excluded if str(x).strip()]
        if any(token in offer_url for token in excluded):
            return False

        duration_range = self._extract_duration_range_from_url()
        if duration_range:
            days = self._extract_duration_days_value(str(offer.get('duration') or ''))
            if days is not None:
                lo, hi = duration_range
                if days < lo or days > hi:
                    return False

        return True

    @staticmethod
    def _dedupe_lowest_per_hotel(offers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Оставляет один оффер на отель — с минимальной ценой."""
        best: Dict[str, Dict[str, Any]] = {}
        for offer in offers:
            if not offer:
                continue
            name = (offer.get('hotel_name') or '').strip()
            if not name:
                continue
            try:
                price = float(offer.get('price') or 0)
            except (TypeError, ValueError):
                continue
            prev = best.get(name)
            if prev is None or price < float(prev.get('price') or 0):
                best[name] = offer
        return list(best.values())

    def _load_previous_hotels_latest(self) -> pd.DataFrame:
        """Загружает предыдущие данные и возвращает последние цены по каждому отелю.

        Использует робастный парсинг времени, чтобы корректно выделить последние записи.
        """
        try:
            filepath = os.path.join(self.config['data_dir'], self.data_file)
            if not os.path.exists(filepath):
                return pd.DataFrame()
            df = pd.read_csv(filepath, quoting=csv.QUOTE_ALL, on_bad_lines='skip')
            if df.empty or 'scraped_at' not in df.columns:
                return pd.DataFrame()
            raw = df['scraped_at'].astype(str)
            mask_tz = raw.str.contains(r"Z$|[+-]\d{2}:\d{2}$", regex=True)
            tz_series = pd.to_datetime(raw.where(mask_tz), errors='coerce', utc=True)
            tz_series = tz_series.dt.tz_convert('UTC')
            naive_series = pd.to_datetime(raw.where(~mask_tz), errors='coerce')
            try:
                naive_series = naive_series.dt.tz_localize('UTC')
            except Exception:
                pass
            ts = tz_series.combine_first(naive_series)
            df = df.assign(_ts=ts).dropna(subset=['_ts'])
            max_ts = df['_ts'].max()
            latest_run = df[df['_ts'] >= max_ts - pd.Timedelta(minutes=5)]
            if latest_run.empty:
                latest_run = df[df['_ts'] == max_ts]
            priced = latest_run.assign(_p=pd.to_numeric(latest_run['price'], errors='coerce'))
            idx = priced.groupby('hotel_name')['_p'].idxmin()
            latest = latest_run.loc[idx, ['hotel_name', 'price', '_ts']].copy()
            return latest
        except Exception:
            return pd.DataFrame()

    def _append_missing_alerts(self, missing_hotels: List[str], latest_prev: pd.DataFrame):
        """Записывает алерты для отелей, которые пропали из текущей выборки.

        Формат алерта совместим с рендерером дашборда, но с типом 'missing'.
        """
        if not missing_hotels:
            return
        alerts_path = os.path.join(self.config['data_dir'], 'price_alerts_history.json')
        alerts_doc: Dict[str, Any] = { 'alerts': [] }
        if os.path.exists(alerts_path):
            try:
                with open(alerts_path, 'r', encoding='utf-8') as f:
                    alerts_doc = json.load(f) or { 'alerts': [] }
                    if 'alerts' not in alerts_doc or not isinstance(alerts_doc['alerts'], list):
                        alerts_doc['alerts'] = []
            except Exception:
                alerts_doc = { 'alerts': [] }

        price_limit = self._extract_price_limit()
        now_iso = datetime.now(timezone.utc).isoformat()
        for name in missing_hotels:
            try:
                prev_row = latest_prev[latest_prev['hotel_name'] == name]
                last_price = float(prev_row['price'].iloc[0]) if not prev_row.empty else None
            except Exception:
                last_price = None
            note = 'Отель отсутствует в результатах поиска'
            if price_limit is not None:
                note += f' (вероятно цена > {int(price_limit)} PLN либо предложение снято)'
            alerts_doc['alerts'].append({
                'type': 'missing',
                'hotel_name': name,
                'old_price': last_price,
                'new_price': None,
                'timestamp': now_iso,
                'note': note,
            })

        try:
            with open(alerts_path, 'w', encoding='utf-8') as f:
                json.dump(alerts_doc, f, ensure_ascii=False, indent=2)
        except Exception:
            logger.warning('Не удалось сохранить алерты о пропавших отелях')

    def detect_missing_hotels_and_alert(self, current_offers: List[Dict[str, Any]]):
        """Определяет отели, исчезнувшие из текущей выдачи, и пишет алерты."""
        try:
            latest_prev = self._load_previous_hotels_latest()
            if latest_prev.empty:
                return
            prev_hotels: set = set(latest_prev['hotel_name'].astype(str).tolist())
            current_hotels: set = set([ (o.get('hotel_name') or '').strip() for o in current_offers if o ])
            missing = sorted(list(prev_hotels - current_hotels))
            if missing:
                logger.info(f"⚠️ Обнаружены отели, исчезнувшие из текущей выдачи: {len(missing)}")
                self._append_missing_alerts(missing, latest_prev)
        except Exception as e:
            logger.warning(f"Не удалось определить пропавшие отели: {e}")

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

    async def find_next_page_url(self, page) -> str:
        """Ищет URL следующей страницы"""
        try:
            # Ищем кнопку "Следующая страница" или "Następna"
            next_page_selectors = [
                'a[aria-label*="następna"]',
                'a[aria-label*="next"]',
                'a[title*="następna"]',
                'a[title*="next"]',
                '.pagination a:contains("Następna")',
                '.pagination a:contains("Next")',
                '.pagination a:contains(">")',
                '.pagination a:contains("»")',
                'a[class*="next"]',
                'a[class*="pagination"]',
                'button[class*="next"]',
                'button[class*="pagination"]'
            ]
            
            for selector in next_page_selectors:
                try:
                    element = await page.query_selector(selector)
                    if element:
                        # Проверяем, что элемент активен (не disabled)
                        is_disabled = await element.get_attribute('disabled')
                        if not is_disabled:
                            href = await element.get_attribute('href')
                            if href:
                                # Если href относительный, делаем его абсолютным
                                if href.startswith('/'):
                                    base_url = self.config['url'].split('?')[0]
                                    return base_url + href
                                elif href.startswith('http'):
                                    return href
                                else:
                                    return self.config['url'] + '&' + href
                except:
                    continue
            
            # Альтернативный поиск - ищем элементы с номерами страниц
            page_numbers = await page.query_selector_all('a[href*="page"], a[href*="strona"]')
            current_page = 1
            
            for page_link in page_numbers:
                try:
                    href = await page_link.get_attribute('href')
                    text = await page_link.inner_text()
                    
                    # Ищем номер текущей страницы
                    if 'active' in (await page_link.get_attribute('class') or ''):
                        try:
                            current_page = int(text.strip())
                        except:
                            pass
                    
                    # Ищем следующую страницу
                    try:
                        page_num = int(text.strip())
                        if page_num == current_page + 1:
                            if href:
                                if href.startswith('/'):
                                    base_url = self.config['url'].split('?')[0]
                                    return base_url + href
                                elif href.startswith('http'):
                                    return href
                                else:
                                    return self.config['url'] + '&' + href
                    except:
                        continue
                except:
                    continue
            
            # Последняя попытка - ищем кнопку с текстом "Następna" или "Next"
            all_links = await page.query_selector_all('a, button')
            for link in all_links:
                try:
                    text = await link.inner_text()
                    if text and ('następna' in text.lower() or 'next' in text.lower() or text.strip() == '>' or text.strip() == '»'):
                        href = await link.get_attribute('href')
                        if href:
                            if href.startswith('/'):
                                base_url = self.config['url'].split('?')[0]
                                return base_url + href
                            elif href.startswith('http'):
                                return href
                            else:
                                return self.config['url'] + '&' + href
                except:
                    continue
            
            return ""
            
        except Exception as e:
            logger.warning(f"Ошибка поиска следующей страницы: {e}")
            return ""

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
            
            # Ищем цену - сначала ищем цену за всех, потом за одного
            price = await self.extract_price_for_all(element)
            if not price:
                price = await self.extract_text_by_selectors(element, [
                    '.price', '.cost', '.amount', '.value',
                    '[class*="price"]', '[class*="cost"]', '[class*="amount"]'
                ])
            
            # Ищем даты - более специфичные селекторы для fly.pl
            dates = await self.extract_dates_from_offer(element)
            
            # Ищем длительность - более специфичные селекторы для fly.pl
            duration = await self.extract_duration_from_offer(element)
            
            # Если не нашли, используем значения по умолчанию из конфигурации
            if not dates:
                dates = "20-09-2025 - 04-10-2025"  # Из URL конфигурации
            if not duration:
                duration = "6-15 дней"  # Из URL конфигурации
            
            # Рейтинг не извлекаем - не очень важен
            rating = ""
            
            # Изображение отеля (если доступно на карточке)
            image_url = await self.extract_image_url_from_offer(element)
            
            # Ссылка на детальную страницу предложения
            offer_url = await self.extract_offer_url(element)
            
            # Извлекаем аэропорт вылета
            departure_airport = self.extract_departure_airport_from_url(self.config['url'])
            
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
                'departure_airport': departure_airport,
                # Записываем временную метку в UTC с таймзоной, чтобы унифицировать время между локальными и CI-запусками
                'scraped_at': datetime.now(timezone.utc).isoformat(),
                'url': self.config['url'],
                'image_url': image_url or "",
                'offer_url': offer_url or ""
            }
            
        except Exception as e:
            logger.warning(f"Ошибка извлечения данных из элемента {index}: {e}")
            return None

    async def extract_image_url_from_offer(self, element) -> str:
        """Пытается извлечь URL главного изображения из карточки предложения."""
        try:
            def _pick_from_srcset(srcset: str) -> str:
                # srcset: "url1 320w, url2 640w" -> берем первый url
                if not srcset:
                    return ""
                first = srcset.split(",")[0].strip()
                return first.split(" ")[0].strip() if first else ""

            def _normalize_url(u: str) -> str:
                if not u:
                    return ""
                u = u.strip()
                if u.startswith("//"):
                    u = "https:" + u
                # поддержим относительные URL
                if u.startswith("/") or u.startswith("#") or not u.startswith(("http://", "https://")):
                    return self.make_absolute_url(u)
                return u

            def _is_placeholder_image(u: str) -> bool:
                if not u:
                    return True
                low = u.strip().lower()
                if low.startswith("data:image"):
                    return True
                # Частый 1x1 png placeholder в выдаче fly.pl
                if "ivborw0kggoaaaansuheugaaaaeaaaab" in low and len(low) < 260:
                    return True
                if low.startswith("blob:"):
                    return True
                return False

            # 1) Пробуем <img src> / data-src / srcset
            img_el = await element.query_selector('img')
            if img_el:
                # В выдаче часто src = 1x1 placeholder, а реальное фото лежит в data-src
                for attr in ['data-src', 'data-original', 'data-lazy', 'data-srcset', 'srcset', 'src']:
                    val = await img_el.get_attribute(attr)
                    if not val or not val.strip():
                        continue
                    candidate = _pick_from_srcset(val) if 'srcset' in attr else val.strip()
                    if _is_placeholder_image(candidate):
                        continue
                    url = _normalize_url(candidate)
                    if url.startswith(("http://", "https://")) and not _is_placeholder_image(url):
                        return url
            
            # 2) Пробуем фоновые изображения из inline-style
            bg_el = await element.query_selector('[style*="background"]')
            if bg_el:
                bg = await bg_el.get_attribute('style')
                if bg and 'url(' in bg:
                    import re
                    m = re.search(r'url\(("|")?(?P<u>[^\)"\']+)("|")?\)', bg)
                    if m:
                        url = _normalize_url(m.group('u'))
                        if url.startswith(("http://", "https://")):
                            return url
            
            # 3) Пробуем вычисленный стиль (менее гарантировано)
            try:
                url = await element.evaluate("el => getComputedStyle(el).backgroundImage")
                if url and 'url(' in url:
                    import re
                    m = re.search(r'url\(("|")?(?P<u>[^\)"\']+)("|")?\)', url)
                    if m:
                        u = _normalize_url(m.group('u'))
                        if u.startswith(("http://", "https://")):
                            return u
            except:
                pass
        except Exception as e:
            logger.debug(f"Не удалось извлечь изображение: {e}")
        return ""

    async def extract_offer_url(self, element) -> str:
        """Извлекает URL ссылку на детальную страницу предложения"""
        try:
            logger.info("🔍 Начинаем извлечение ссылки на предложение...")
            
            # 1) Проверяем, является ли сам элемент ссылкой
            tag_name = await element.evaluate("el => el.tagName.toLowerCase()")
            if tag_name == 'a':
                href = await element.get_attribute('href')
                if href and href.strip():
                    logger.info(f"✅ Найдена ссылка в самом элементе: {href[:100]}...")
                    return self.make_absolute_url(href)
            
            # 2) Ищем ссылку с классом image-link (основной селектор для ссылок на предложения)
            image_link = await element.query_selector('a.image-link')
            if image_link:
                href = await image_link.get_attribute('href')
                if href and href.strip():
                    logger.info(f"✅ Найдена ссылка через a.image-link: {href[:100]}...")
                    return self.make_absolute_url(href)
                else:
                    logger.info("❌ a.image-link найден, но href пустой")
            else:
                logger.info("❌ a.image-link не найден")
            
            # 3) Ищем ссылку с классом offer-con (альтернативный селектор)
            offer_link = await element.query_selector('a.offer-con')
            if offer_link:
                href = await offer_link.get_attribute('href')
                if href and href.strip():
                    return self.make_absolute_url(href)
            
            # 4) Ищем ссылки на /wycieczka/ (детальные страницы предложений)
            wycieczka_link = await element.query_selector('a[href*="/wycieczka/"]')
            if wycieczka_link:
                href = await wycieczka_link.get_attribute('href')
                if href and href.strip():
                    logger.info(f"✅ Найдена ссылка через a[href*='/wycieczka/']: {href[:100]}...")
                    return self.make_absolute_url(href)
                else:
                    logger.info("❌ a[href*='/wycieczka/'] найден, но href пустой")
            else:
                logger.info("❌ a[href*='/wycieczka/'] не найден")
            
            # 5) Ищем другие возможные ссылки на предложения
            link_selectors = [
                'a[href*="offer"]',        # Ссылка содержащая "offer"
                'a[href*="hotel"]',        # Ссылка содержащая "hotel"
                'a[href*="trip"]',         # Ссылка содержащая "trip"
                'a[href*="detail"]',       # Ссылка содержащая "detail"
                'a[href*="view"]',         # Ссылка содержащая "view"
                'a[class*="link"]',        # Ссылка с классом содержащим "link"
                'a[href]'                  # Любая ссылка
            ]
            
            for selector in link_selectors:
                try:
                    link_element = await element.query_selector(selector)
                    if link_element:
                        href = await link_element.get_attribute('href')
                        if href and href.strip():
                            # Проверяем, что это ссылка на предложение
                            if '/wycieczka/' in href or 'offer' in href.lower():
                                return self.make_absolute_url(href)
                except:
                    continue
            
            # 6) Проверяем родительские элементы на наличие ссылок
            try:
                parent = await element.evaluate("el => el.parentElement")
                if parent:
                    parent_tag = await parent.evaluate("el => el.tagName.toLowerCase()")
                    if parent_tag == 'a':
                        href = await parent.get_attribute('href')
                        if href and href.strip():
                            return self.make_absolute_url(href)
            except:
                pass
                
        except Exception as e:
            logger.debug(f"Не удалось извлечь ссылку на предложение: {e}")
        
        return ""

    def make_absolute_url(self, url: str) -> str:
        """Преобразует относительный URL в абсолютный"""
        if not url:
            return ""
        
        url = url.strip()
        
        # Если уже абсолютный URL
        if url.startswith(('http://', 'https://')):
            return url

        # Protocol-relative URL
        if url.startswith('//'):
            return f"https:{url}"
        
        # Если относительный URL, добавляем базовый домен
        if url.startswith('/'):
            return f"https://fly.pl{url}"
        
        # Если относительный URL без слеша
        if not url.startswith('#'):
            return f"https://fly.pl/{url}"
        
        return url

    async def extract_price_for_all(self, element) -> str:
        """Извлекает цену за всех (za wszystkich)"""
        try:
            # Ищем элементы с текстом "za wszystkich" или "za wszystkie"
            price_elements = await element.query_selector_all('[class*="price"]')
            
            for price_element in price_elements:
                text = await price_element.inner_text()
                if text and ('za wszystkich' in text.lower() or 'za wszystkie' in text.lower()):
                    # Ищем число в этом элементе
                    import re
                    numbers = re.findall(r'[\d\s,]+', text.replace('.', '').replace(',', '.'))
                    if numbers:
                        return text.strip()
            
            # Альтернативный поиск - ищем элементы с классом price-view-2 (цена за всех)
            price_view_2 = await element.query_selector('.price-view-2, [class*="price-view-2"]')
            if price_view_2:
                text = await price_view_2.inner_text()
                if text and text.strip():
                    return text.strip()
            
            return ""
        except Exception as e:
            logger.warning(f"Ошибка извлечения цены за всех: {e}")
            return ""

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
    
    def extract_dates_from_url(self) -> str:
        """Извлекает даты из URL конфигурации"""
        try:
            url = self.config.get('url', '')
            if 'whenFrom' in url and 'whenTo' in url:
                # Извлекаем даты из URL
                import re
                when_from_match = re.search(r'whenFrom(?:\]|%5D)?=(\d{2}-\d{2}-\d{4})', url)
                when_to_match = re.search(r'whenTo(?:\]|%5D)?=(\d{2}-\d{2}-\d{4})', url)
                
                if when_from_match and when_to_match:
                    from_date = when_from_match.group(1)
                    to_date = when_to_match.group(1)
                    return f"{from_date} - {to_date}"
        except Exception as e:
            logger.warning(f"Ошибка извлечения дат из URL: {e}")
        return ""
    
    def extract_duration_from_url(self) -> str:
        """Извлекает длительность из URL конфигурации"""
        try:
            url = self.config.get('url', '')
            if 'duration=' in url:
                # Извлекаем длительность из URL
                import re
                duration_match = re.search(r'duration=(\d+):(\d+)', url)
                
                if duration_match:
                    min_days = duration_match.group(1)
                    max_days = duration_match.group(2)
                    if min_days == max_days:
                        return f"{min_days} дней"
                    else:
                        return f"{min_days}-{max_days} дней"
        except Exception as e:
            logger.warning(f"Ошибка извлечения длительности из URL: {e}")
        return ""
    
    async def extract_dates_from_offer(self, element) -> str:
        """Извлекает даты вылета-прилета из конкретного предложения"""
        try:
            # Ищем различные селекторы для дат на fly.pl
            date_selectors = [
                # Основные селекторы дат
                '.date', '.dates', '.departure-date', '.arrival-date',
                '.travel-date', '.trip-date', '.journey-date',
                # Селекторы с классами
                '[class*="date"]', '[class*="departure"]', '[class*="arrival"]',
                '[class*="travel"]', '[class*="trip"]', '[class*="journey"]',
                # Селекторы с data-атрибутами
                '[data-date]', '[data-departure]', '[data-arrival]',
                # Селекторы для периодов
                '.period', '.range', '.from-to',
                # Селекторы для времени
                '.time', '.when', '.schedule'
            ]
            
            for selector in date_selectors:
                try:
                    date_elements = await element.query_selector_all(selector)
                    for date_element in date_elements:
                        text = await date_element.inner_text()
                        if text and self.is_date_text(text):
                            return self.clean_text(text)
                except:
                    continue
            
            # Ищем в тексте элемента паттерны дат
            full_text = await element.inner_text()
            if full_text:
                import re
                # Ищем паттерны типа "20.09 - 04.10" или "20.09.2025 - 04.10.2025"
                date_patterns = [
                    r'\d{1,2}\.\d{1,2}\.\d{4}\s*-\s*\d{1,2}\.\d{1,2}\.\d{4}',  # 20.09.2025 - 04.10.2025
                    r'\d{1,2}\.\d{1,2}\s*-\s*\d{1,2}\.\d{1,2}',  # 20.09 - 04.10
                    r'\d{1,2}/\d{1,2}/\d{4}\s*-\s*\d{1,2}/\d{1,2}/\d{4}',  # 20/09/2025 - 04/10/2025
                    r'\d{1,2}-\d{1,2}-\d{4}\s*-\s*\d{1,2}-\d{1,2}-\d{4}',  # 20-09-2025 - 04-10-2025
                ]
                
                for pattern in date_patterns:
                    matches = re.findall(pattern, full_text)
                    if matches:
                        return matches[0]
            
            return ""
        except Exception as e:
            logger.warning(f"Ошибка извлечения дат из предложения: {e}")
            return ""
    
    async def extract_duration_from_offer(self, element) -> str:
        """Извлекает длительность (дни/ночи) из конкретного предложения"""
        try:
            # Ищем различные селекторы для длительности на fly.pl
            duration_selectors = [
                # Основные селекторы длительности
                '.duration', '.nights', '.days', '.length',
                '.trip-duration', '.stay-duration', '.period',
                # Селекторы с классами
                '[class*="duration"]', '[class*="nights"]', '[class*="days"]',
                '[class*="length"]', '[class*="period"]',
                # Селекторы с data-атрибутами
                '[data-duration]', '[data-nights]', '[data-days]'
            ]
            
            for selector in duration_selectors:
                try:
                    duration_elements = await element.query_selector_all(selector)
                    for duration_element in duration_elements:
                        text = await duration_element.inner_text()
                        if text and self.is_duration_text(text):
                            return self.clean_text(text)
                except:
                    continue
            
            # Ищем в тексте элемента паттерны длительности
            full_text = await element.inner_text()
            if full_text:
                import re
                # Ищем паттерны типа "7 dni", "7 noclegów", "7 days", "7 nights"
                duration_patterns = [
                    r'(\d+)\s*(dni|noclegów|days|nights|dni|noclegi)',  # 7 dni, 7 noclegów
                    r'(\d+)\s*(dni|noclegów|days|nights)',  # 7 dni, 7 nights
                    r'(\d+)\s*d',  # 7d
                    r'(\d+)\s*n',  # 7n
                ]
                
                for pattern in duration_patterns:
                    matches = re.findall(pattern, full_text, re.IGNORECASE)
                    if matches:
                        # Возвращаем полный текст с числом и единицей измерения
                        return f"{matches[0][0]} {matches[0][1]}" if len(matches[0]) > 1 else f"{matches[0][0]} dni"
            
            return ""
        except Exception as e:
            logger.warning(f"Ошибка извлечения длительности из предложения: {e}")
            return ""
    
    def is_date_text(self, text: str) -> bool:
        """Проверяет, содержит ли текст дату"""
        if not text or len(text.strip()) < 5:
            return False
        
        import re
        
        # Исключаем рейтинги TripAdvisor
        if any(keyword in text.lower() for keyword in ['tripadvisor', 'ocena', 'opinii', 'rating', 'stars']):
            return False
        
        # Проверяем наличие паттернов дат
        date_patterns = [
            r'\d{1,2}\.\d{1,2}\.\d{4}',  # 20.09.2025
            r'\d{1,2}\.\d{1,2}',         # 20.09
            r'\d{1,2}/\d{1,2}/\d{4}',    # 20/09/2025
            r'\d{1,2}/\d{1,2}',          # 20/09
            r'\d{1,2}-\d{1,2}-\d{4}',    # 20-09-2025
            r'\d{1,2}-\d{1,2}',          # 20-09
        ]
        
        for pattern in date_patterns:
            if re.search(pattern, text):
                return True
        
        return False
    
    def is_duration_text(self, text: str) -> bool:
        """Проверяет, содержит ли текст длительность"""
        if not text or len(text.strip()) < 2:
            return False
        
        import re
        # Проверяем наличие паттернов длительности
        duration_patterns = [
            r'\d+\s*(dni|noclegów|days|nights|dni|noclegi)',
            r'\d+\s*d',
            r'\d+\s*n',
        ]
        
        for pattern in duration_patterns:
            if re.search(pattern, text, re.IGNORECASE):
                return True
        
        return False

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
    
    def extract_departure_airport_from_url(self, url: str) -> str:
        """Извлекает аэропорт вылета из URL"""
        try:
            if 'filter[from]=' in url:
                # Ищем параметр filter[from] в URL
                import re
                match = re.search(r'filter\[from\]=([^&]*)', url)
                if match:
                    airports = match.group(1)
                    if airports:
                        # Если несколько аэропортов через запятую, берем первый
                        return airports.split(',')[0]
            return "Все аэропорты"
        except Exception as e:
            logger.warning(f"Ошибка извлечения аэропорта из URL: {e}")
            return "Неизвестно"

    def save_data_append(self, offers: List[Dict[str, Any]]):
        """Сохраняет данные, добавляя к существующим"""
        if not offers:
            logger.warning("Нет данных для сохранения")
            return
        
        # Создаем директорию
        os.makedirs(self.config['data_dir'], exist_ok=True)
        
        filepath = os.path.join(self.config['data_dir'], self.data_file)
        
        # Начиная с этого момента пишем каждую запись как новую точку истории,
        # чтобы графики и анализ имели полную временную серию даже без изменений цен.
        new_offers = offers
        
        # Всегда перезаписываем файл с правильными заголовками для совместимости
        existing_data = []
        file_exists = os.path.exists(filepath)
        
        if file_exists:
            try:
                # Читаем существующие данные с обработкой ошибок структуры
                with open(filepath, 'r', encoding='utf-8') as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        # Заполняем отсутствующие поля пустыми значениями
                        normalized_row = {
                            'hotel_name': row.get('hotel_name', ''),
                            'price': row.get('price', ''),
                            'dates': row.get('dates', ''),
                            'duration': row.get('duration', ''),
                            'rating': row.get('rating', ''),
                            'departure_airport': row.get('departure_airport', ''),
                            'scraped_at': row.get('scraped_at', ''),
                            'url': row.get('url', ''),
                            'image_url': row.get('image_url', ''),
                            'offer_url': row.get('offer_url', '')
                        }
                        existing_data.append(normalized_row)
            except Exception as e:
                logger.warning(f"Ошибка чтения существующих данных: {e}, создаем новый файл")
                existing_data = []
        
        # Перезаписываем файл с правильными заголовками
        with open(filepath, 'w', newline='', encoding='utf-8') as csvfile:
            fieldnames = ['hotel_name', 'price', 'dates', 'duration', 'rating', 'departure_airport', 'scraped_at', 'url', 'image_url', 'offer_url']
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames, quoting=csv.QUOTE_ALL)
            writer.writeheader()
            
            # Записываем существующие данные
            for row in existing_data:
                writer.writerow({k: row.get(k, '') for k in fieldnames})
            
            # Добавляем новые данные
            for offer in new_offers:
                writer.writerow({k: offer.get(k, '') for k in fieldnames})
        
        logger.info(f"Добавлено {len(new_offers)} записей (включая возможные повторы для истории) в {filepath}")

        # Обновляем карту изображений по отелям в отдельном JSON
        try:
            images_path = os.path.join(self.config['data_dir'], 'hotel_images.json')
            images_map: Dict[str, str] = {}
            if os.path.exists(images_path):
                try:
                    with open(images_path, 'r', encoding='utf-8') as jf:
                        import json as _json
                        data = _json.load(jf)
                        if isinstance(data, dict):
                            images_map = data
                except Exception:
                    images_map = {}

            updated = 0
            for offer in new_offers:
                h = offer.get('hotel_name')
                img = (offer.get('image_url') or '').strip()
                if h and img and img.startswith('http'):
                    if h not in images_map:
                        images_map[h] = img
                        updated += 1

            if updated:
                with open(images_path, 'w', encoding='utf-8') as jf:
                    import json as _json
                    _json.dump(images_map, jf, ensure_ascii=False, indent=2)
                logger.info(f"Обновлена карта изображений для отелей: +{updated}")
        except Exception as e:
            logger.warning(f"Не удалось обновить карту изображений: {e}")

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
            
            # Робастный парсинг меток времени (смешанные ISO8601 с/без таймзоны)
            raw = df['scraped_at'].astype(str)
            mask_tz = raw.str.contains(r"Z$|[+-]\d{2}:\d{2}$", regex=True)
            tz_series = pd.to_datetime(raw.where(mask_tz), errors='coerce', utc=True)
            # Графики рисуем в локальном времени runner'а (UTC)
            tz_series = tz_series.dt.tz_convert('UTC')
            naive_series = pd.to_datetime(raw.where(~mask_tz), errors='coerce')
            try:
                naive_series = naive_series.dt.tz_localize('UTC')
            except Exception:
                pass
            ts = tz_series.combine_first(naive_series)
            df = df.assign(_ts=ts).dropna(subset=['_ts'])

            daily_prices = df.groupby(df['_ts'].dt.date)['price'].agg(['mean', 'min', 'max'])
            
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
            # Пробуем загрузить с обработкой ошибок структуры
            df = pd.read_csv(filepath, quoting=csv.QUOTE_ALL, on_bad_lines='skip')
            
            # Проверяем, что все необходимые колонки присутствуют
            required_columns = ['hotel_name', 'price', 'dates', 'duration', 'rating', 'scraped_at', 'url', 'image_url', 'offer_url']
            missing_columns = [col for col in required_columns if col not in df.columns]
            
            if missing_columns:
                logger.warning(f"Отсутствуют колонки: {missing_columns}, добавляем пустые")
                for col in missing_columns:
                    df[col] = ''
            
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

    def check_price_alerts(self):
        """Проверяет изменения цен и создает алерты (новая логика V2)"""
        try:
            # Создаем региональный файл алертов на основе data_file
            alerts_file = self.data_file.replace('.csv', '_alerts.json')
            alert_manager = PriceAlertManagerV2(
                data_file=os.path.join(self.config['data_dir'], self.data_file), 
                alerts_file=os.path.join(self.config['data_dir'], alerts_file)
            )
            
            if alert_manager.df.empty:
                logger.warning("Нет данных для проверки алертов")
                return
            
            # Обрабатываем все изменения и получаем только новые алерты
            new_alerts = alert_manager.process_all_changes(threshold_percent=4.0)
            
            # Создаем отчет об алертах
            report = alert_manager.create_alert_report(threshold_percent=4.0)
            
            # Логируем новые алерты
            if new_alerts:
                price_drops = [a for a in new_alerts if a['price_change'] < 0]
                price_increases = [a for a in new_alerts if a['price_change'] > 0]
                
                if price_drops:
                    logger.info(f"🚨 Обнаружено {len(price_drops)} новых снижений цен >= 4%!")
                    for alert in price_drops[:5]:  # Показываем первые 5
                        logger.info(f"  📉 {alert['hotel_name']}: {alert['old_price']} → {alert['new_price']} PLN ({alert['price_change_pct']:+.1f}%)")
                
                if price_increases:
                    logger.info(f"🚨 Обнаружено {len(price_increases)} новых повышений цен >= 4%!")
                    for alert in price_increases[:5]:  # Показываем первые 5
                        logger.info(f"  📈 {alert['hotel_name']}: {alert['old_price']} → {alert['new_price']} PLN ({alert['price_change_pct']:+.1f}%)")
            else:
                logger.info("✅ Новых значительных изменений цен не обнаружено")
            
            # Сохраняем отчет
            report_path = os.path.join(self.config['data_dir'], 'price_alerts_report.txt')
            with open(report_path, 'w', encoding='utf-8') as f:
                f.write(report)
            logger.info(f"📊 Отчет об алертах сохранен: {report_path}")
                
        except Exception as e:
            logger.error(f"Ошибка при проверке алертов: {e}")

    def compare_airports(self, any_airports_config_file: str):
        """Сравнивает данные из Варшавы и всех аэропортов"""
        try:
            logger.info("🛫 Начинаем сравнение аэропортов...")
            
            # Загружаем конфигурацию для всех аэропортов
            with open(any_airports_config_file, 'r', encoding='utf-8') as f:
                any_airports_config = json.load(f)
            
            any_airports_data_file = any_airports_config.get('output_data_file', 'travel_prices_any_airports.csv')
            
            # Создаем объект для сравнения
            comparison = AirportComparison(self.config['data_dir'])
            
            # Сравниваем данные
            results = comparison.compare_airports(self.data_file, any_airports_data_file)
            
            if results:
                # Сохраняем результаты
                comparison.save_comparison_results(results, f"{self.data_file.replace('.csv', '_airport_comparison.json')}")
                comparison.save_comparison_report(results, f"{self.data_file.replace('.csv', '_airport_comparison_report.txt')}")
                
                logger.info("✅ Сравнение аэропортов завершено!")
            else:
                logger.warning("❌ Не удалось выполнить сравнение аэропортов")
                
        except Exception as e:
            logger.error(f"❌ Ошибка при сравнении аэропортов: {e}")

    async def run_monitoring(self):
        """Запускает полный цикл мониторинга"""
        logger.info("🚀 Начинаем мониторинг цен на путешествия...")
        
        try:
            # Собираем данные с повторными попытками
            offers = await self.scrape_offers_with_retry()
            
            if not offers:
                logger.error("❌ Не удалось собрать данные после всех попыток")
                return False

            raw_count = len(offers)
            offers = self._dedupe_lowest_per_hotel(offers)
            if len(offers) < raw_count:
                logger.info(
                    f"Дедупликация: {raw_count} офферов → {len(offers)} отелей (мин. цена на отель)"
                )
            
            # Перед сохранением проверяем, кто исчез из выдачи, и создаём алерты
            self.detect_missing_hotels_and_alert(offers)
            
            # Сохраняем данные (добавляем к существующим)
            self.save_data_append(offers)
            
            # Создаем графики
            self.create_charts()
            
            # Генерируем отчет
            self.generate_report()
            
            # Проверяем изменения цен и создаем алерты
            self.check_price_alerts()
            
            logger.info("✅ Мониторинг завершен успешно!")
            return True
            
        except Exception as e:
            logger.error(f"❌ Критическая ошибка: {e}")
            return False

def main():
    """Главная функция"""
    # Параметры командной строки: --config, --data-file
    import argparse
    parser = argparse.ArgumentParser(description="Travel price monitor")
    parser.add_argument("--config", default="config.json", help="Путь к конфигу JSON")
    parser.add_argument("--data-file", default=None, help="Имя CSV файла данных (внутри data_dir)")
    args = parser.parse_args()

    monitor = TravelPriceMonitor(config_file=args.config, data_file=args.data_file)
    
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

