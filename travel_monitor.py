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
import time
import html as ihtml
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
import pandas as pd
import requests
import matplotlib.pyplot as plt
from playwright.async_api import async_playwright
import logging
from price_alerts import PriceAlertManager
from price_alerts_v2 import PriceAlertManagerV2, ALERT_THRESHOLD_PERCENT
from airport_comparison import AirportComparison
from departure_analytics import BASE_OFFER_FIELDS, write_departure_analytics
from departure_identity import DEPARTURE_FIELDS, enrich_offers
from filter_params import fly_query_param, resolve_config_url, should_use_dynamic_search_dates
from filter_trip import (
    has_multiple_trip_duration_buckets,
    is_fixed_trip_config,
    parse_trip_duration_buckets,
    select_trip_offers,
    trip_scrape_passes,
)
from hotel_deal_score import extract_tripadvisor_from_card_html

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

TRAVEL_PRICE_CSV_FIELDS = [
    'hotel_name', 'price', 'dates', 'duration', 'duration_bucket', 'rating',
    'ta_rating', 'ta_review_count', 'ta_source',
    'departure_airport', 'scraped_at', 'url', 'image_url', 'offer_url',
]


def _log_timing(label: str, started: float, extra: str = "") -> float:
    """Логирует длительность шага; возвращает elapsed в секундах."""
    elapsed = time.monotonic() - started
    suffix = f" | {extra}" if extra else ""
    logger.info(f"⏱ {label}: {elapsed:.2f}s{suffix}")
    return elapsed


class TravelPriceMonitor:
    def __init__(self, config_file: str = "config.json", data_file: Optional[str] = None):
        self.config_file = config_file
        self.config = self.load_config()
        # data_file из аргументов имеет приоритет над output_data_file из конфигурации
        self.data_file = data_file or self.config.get('output_data_file', 'travel_prices.csv')
        self._timing_summary: Dict[str, float] = {}
        
    def load_config(self) -> Dict[str, Any]:
        """Загружает конфигурацию из файла"""
        try:
            with open(self.config_file, 'r', encoding='utf-8') as f:
                config = json.load(f)
            logger.info(f"Конфигурация загружена из {self.config_file}")
            
            if 'url' in config and config['url']:
                config['url'] = resolve_config_url(config)
                if should_use_dynamic_search_dates(config):
                    logger.info(
                        "🔄 URL обновлен динамически: с %s по %s",
                        fly_query_param(config['url'], 'whenFrom'),
                        fly_query_param(config['url'], 'whenTo'),
                    )
                elif is_fixed_trip_config(config):
                    logger.info(
                        "📌 Фиксированное окно поиска: %s — %s (вылет ≈ %s)",
                        fly_query_param(config['url'], 'whenFrom'),
                        fly_query_param(config['url'], 'whenTo'),
                        config.get('trip_anchor_date', '—'),
                    )
                
            return config
        except Exception as e:
            logger.error(f"Ошибка загрузки конфигурации: {e}")
            sys.exit(1)

    def _scrape_timestamp(self) -> str:
        """One UTC timestamp for the whole scrape run (all offers in a batch)."""
        ts = getattr(self, '_current_scrape_at', None)
        if ts:
            return ts
        return datetime.now(timezone.utc).isoformat()

    def _resolve_http_max_retries(self) -> int:
        env_val = os.environ.get("HTTP_MAX_RETRIES", "").strip()
        if env_val.isdigit():
            return max(1, int(env_val))
        return max(1, int(self.config.get("http_max_retries", 6)))

    def _http_retry_pause_seconds(self, attempt_index: int) -> float:
        base = float(self.config.get("http_retry_delay", self.config.get("retry_delay", 5)))
        cap = float(self.config.get("http_retry_delay_max", 45))
        return min(base * (2 ** attempt_index), cap)

    async def scrape_offers_with_retry(self) -> List[Dict[str, Any]]:
        """Парсит предложения.

        Сначала пробуем быстрый путь: прямой HTTP-запрос + разбор server-side HTML
        (на порядок быстрее, без запуска браузера). Если он не дал результата
        (например, изменилась разметка или включился анти-бот) — автоматически
        откатываемся на надёжный браузерный парсинг через Playwright.
        """
        self._current_scrape_at = datetime.now(timezone.utc).isoformat()
        scrape_t0 = time.monotonic()
        http_total_pause = 0.0
        if self.config.get('disable_http_fast_path') is not True:
            http_max_retries = self._resolve_http_max_retries()
            logger.info(
                f"HTTP fast path: max_retries={http_max_retries}, "
                f"page_retries={self.config.get('http_page_retries', 3)}"
            )
            last_http_note = "неизвестно"
            http_phase_t0 = time.monotonic()
            for http_attempt in range(http_max_retries):
                attempt_t0 = time.monotonic()
                try:
                    if http_attempt > 0:
                        pause = self._http_retry_pause_seconds(http_attempt - 1)
                        logger.info(
                            f"Повтор HTTP {http_attempt + 1}/{http_max_retries} "
                            f"(пауза {pause:.0f} с)..."
                        )
                        await asyncio.sleep(pause)
                        http_total_pause += pause
                    offers, last_http_note = self.scrape_offers_http()
                    _log_timing(
                        f"HTTP попытка {http_attempt + 1}/{http_max_retries}",
                        attempt_t0,
                        f"offers={len(offers)}, note={last_http_note or 'ok'}",
                    )
                    if offers:
                        self._timing_summary["scrape_http"] = time.monotonic() - http_phase_t0
                        self._timing_summary["scrape_http_pause"] = http_total_pause
                        self._timing_summary["scrape_total"] = time.monotonic() - scrape_t0
                        logger.info(f"⚡ Быстрый HTTP-парсинг успешен: {len(offers)} предложений")
                        return offers
                    logger.warning(
                        f"HTTP-попытка {http_attempt + 1}/{http_max_retries}: "
                        f"результатов нет ({last_http_note})"
                    )
                except Exception as e:
                    last_http_note = str(e)
                    _log_timing(
                        f"HTTP попытка {http_attempt + 1}/{http_max_retries} (ошибка)",
                        attempt_t0,
                        str(e),
                    )
                    logger.warning(
                        f"HTTP-попытка {http_attempt + 1}/{http_max_retries} не удалась ({e})"
                    )
            self._timing_summary["scrape_http_failed"] = time.monotonic() - http_phase_t0
            self._timing_summary["scrape_http_pause"] = http_total_pause
            logger.warning(
                f"HTTP-парсинг не дал результатов после {http_max_retries} попыток "
                f"({last_http_note}, паузы между попытками {http_total_pause:.0f}s) — "
                f"переключаемся на Playwright"
            )

        pw_phase_t0 = time.monotonic()
        for attempt in range(self.config['max_retries']):
            attempt_t0 = time.monotonic()
            try:
                logger.info(f"Попытка {attempt + 1}/{self.config['max_retries']} (Playwright)")
                offers = await self.scrape_offers()
                _log_timing(
                    f"Playwright попытка {attempt + 1}/{self.config['max_retries']}",
                    attempt_t0,
                    f"offers={len(offers)}",
                )
                if offers:
                    self._timing_summary["scrape_playwright"] = time.monotonic() - pw_phase_t0
                    self._timing_summary["scrape_total"] = time.monotonic() - scrape_t0
                    return offers
                logger.warning(f"Попытка {attempt + 1} не дала результатов")
            except Exception as e:
                _log_timing(
                    f"Playwright попытка {attempt + 1}/{self.config['max_retries']} (ошибка)",
                    attempt_t0,
                    str(e),
                )
                logger.error(f"Ошибка в попытке {attempt + 1}: {e}")
                if attempt < self.config['max_retries'] - 1:
                    logger.info(f"Ждем {self.config['retry_delay']} секунд...")
                    await asyncio.sleep(self.config['retry_delay'])
        
        self._timing_summary["scrape_playwright_failed"] = time.monotonic() - pw_phase_t0
        self._timing_summary["scrape_total"] = time.monotonic() - scrape_t0
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

    def _ta_fields_from_card(self, card_html: str) -> Dict[str, Any]:
        ta = extract_tripadvisor_from_card_html(card_html)
        rating_val = ta.get("ta_rating")
        review_val = ta.get("ta_review_count")
        rating_str = ""
        if rating_val is not None:
            try:
                rating_str = f"{float(rating_val):.1f}"
            except (TypeError, ValueError):
                rating_str = ""
        return {
            "rating": rating_str,
            "ta_rating": rating_str,
            "ta_review_count": review_val if review_val is not None else "",
            "ta_source": ta.get("ta_source") or "",
        }

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
            ta_fields = self._ta_fields_from_card(card)

            offers.append({
                'hotel_name': (name or 'Предложение')[:100],
                'price': price_value,
                'dates': dates[:50],
                'duration': duration[:30],
                'rating': ta_fields['rating'],
                'ta_rating': ta_fields['ta_rating'],
                'ta_review_count': ta_fields['ta_review_count'],
                'ta_source': ta_fields['ta_source'],
                'departure_airport': departure_airport,
                'scraped_at': self._scrape_timestamp(),
                'url': self.config['url'],
                'image_url': image_url or '',
                'offer_url': offer_url or '',
            })

        return offers

    def _fetch_search_page_html(
        self,
        session: requests.Session,
        url: str,
        page_number: int,
        timeout_s: float,
    ) -> tuple[Optional[str], str]:
        """Загружает HTML страницы выдачи с короткими ретраями на уровне страницы."""
        page_retries = max(1, int(self.config.get("http_page_retries", 3)))
        page_retry_delay = float(self.config.get("http_page_retry_delay", 3))
        last_note = "неизвестно"

        for page_attempt in range(page_retries):
            req_t0 = time.monotonic()
            if page_attempt > 0:
                pause = page_retry_delay * page_attempt
                logger.info(
                    f"Страница {page_number}: повтор {page_attempt + 1}/{page_retries} "
                    f"(пауза {pause:.0f} с)..."
                )
                time.sleep(pause)
            try:
                resp = session.get(url, timeout=timeout_s)
            except requests.RequestException as exc:
                last_note = f"сеть: {exc}"
                _log_timing(
                    f"HTTP GET стр.{page_number} попытка {page_attempt + 1}",
                    req_t0,
                    f"ошибка: {exc}",
                )
                continue

            if resp.status_code != 200:
                last_note = f"HTTP {resp.status_code}"
                _log_timing(
                    f"HTTP GET стр.{page_number} попытка {page_attempt + 1}",
                    req_t0,
                    f"status={resp.status_code}, bytes={len(resp.content)}",
                )
                continue

            html = resp.text or ""
            if "card-offer-search" not in html:
                if len(html) < 5000:
                    last_note = "короткий HTML (возможно анти-бот/ошибка fly.pl)"
                else:
                    last_note = "в HTML нет card-offer-search (разметка или пустая выдача)"
                _log_timing(
                    f"HTTP GET стр.{page_number} попытка {page_attempt + 1}",
                    req_t0,
                    f"bytes={len(html)}, {last_note}",
                )
                continue

            _log_timing(
                f"HTTP GET стр.{page_number} попытка {page_attempt + 1}",
                req_t0,
                f"ok, bytes={len(html)}, cards≈{html.count('card-offer-search')}",
            )
            return html, ""

        logger.warning(
            f"Страница {page_number}: не удалось загрузить после {page_retries} попыток ({last_note})"
        )
        return None, last_note

    def scrape_offers_http(self) -> tuple[List[Dict[str, Any]], str]:
        """Быстрый парсинг через прямые HTTP-запросы (без браузера).

        Постранично запрашивает выдачу (пагинация p:N), разбирает HTML и применяет
        те же фильтры (мин/макс цена, целевой scope), что и браузерный путь.
        Возвращает (офферы, причина_пустого_результата).
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
        empty_note = "нет офферов"

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
        http_scan_t0 = time.monotonic()
        while page_number <= max_pages:
            page_t0 = time.monotonic()
            url = self._build_page_url(page_number)
            page_html, fetch_note = self._fetch_search_page_html(
                session, url, page_number, timeout_s
            )
            if not page_html:
                empty_note = fetch_note or empty_note
                _log_timing(f"HTTP страница {page_number} (fetch fail)", page_t0, fetch_note)
                break

            parse_t0 = time.monotonic()
            raw_offers = self._parse_offers_from_html(page_html)
            parse_elapsed = time.monotonic() - parse_t0
            if not raw_offers:
                empty_note = "парсер не извлёк карточки из HTML"
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
                empty_note = (
                    f"после фильтра 0 офферов (сырых {len(raw_offers)}, отфильтровано {filtered_out})"
                )
                logger.info(f"Страница {page_number}: после фильтра не осталось предложений, завершаем")
                break

            _log_timing(
                f"HTTP страница {page_number}",
                page_t0,
                f"raw={len(raw_offers)}, kept={len(page_offers)}, parse={parse_elapsed:.2f}s",
            )
            page_number += 1

        _log_timing(
            "HTTP scan total",
            http_scan_t0,
            f"offers={len(all_offers)}, pages={page_number}",
        )
        logger.info(f"⚡ HTTP-парсинг завершён: {len(all_offers)} предложений с {page_number} страниц")
        if not all_offers:
            return [], empty_note
        return all_offers, ""

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
        
        pw_t0 = time.monotonic()
        async with async_playwright() as p:
            launch_t0 = time.monotonic()
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    '--no-sandbox',
                    '--disable-dev-shm-usage',
                    '--disable-gpu',
                    '--disable-web-security'
                ]
            )
            _log_timing("Playwright browser launch", launch_t0)
            
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                viewport={'width': 1920, 'height': 1080}
            )
            
            page = await context.new_page()
            
            try:
                logger.info(f"Переходим на страницу: {self.config['url']}")
                
                # Устанавливаем таймауты
                page.set_default_timeout(self.config['wait_timeout'])
                
                goto_t0 = time.monotonic()
                response = await page.goto(
                    self.config['url'], 
                    wait_until='domcontentloaded',
                    timeout=self.config['wait_timeout']
                )
                _log_timing(
                    "Playwright goto page 1",
                    goto_t0,
                    f"status={response.status if response else 'none'}",
                )
                
                if not response or response.status >= 400:
                    raise Exception(f"Ошибка загрузки: {response.status if response else 'No response'}")
                
                wait_t0 = time.monotonic()
                logger.info("Страница загружена, ждем контент...")
                await page.wait_for_timeout(5000)
                _log_timing("Playwright post-goto wait", wait_t0, "5000ms fixed")
                
                # Парсим страницы пока не достигнем максимальной цены
                while page_number <= max_pages:
                    page_t0 = time.monotonic()
                    logger.info(f"Парсим страницу {page_number}...")
                    
                    find_t0 = time.monotonic()
                    offers_data = await self.find_offers(page)
                    find_elapsed = time.monotonic() - find_t0
                    
                    if not offers_data:
                        logger.warning(
                            f"Предложения не найдены за {find_elapsed:.2f}s, "
                            f"пробуем альтернативный подход..."
                        )
                        alt_t0 = time.monotonic()
                        offers_data = await self.find_offers_alternative(page)
                        _log_timing("Playwright find_offers_alternative", alt_t0, f"found={len(offers_data)}")
                    else:
                        _log_timing("Playwright find_offers", find_t0, f"found={len(offers_data)}")
                    
                        if not offers_data:
                            logger.info("Предложения не найдены, завершаем парсинг")
                            break
                    
                    page_offers = []
                    max_price_on_page = 0
                    filtered_out_on_scope = 0
                    extract_t0 = time.monotonic()
                    
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
                    extract_elapsed = time.monotonic() - extract_t0
                    
                    if page_offers:
                        all_offers.extend(page_offers)
                        logger.info(
                            f"Страница {page_number}: собрано {len(page_offers)} предложений, "
                            f"макс. цена {max_price_on_page:.0f} PLN, extract {extract_elapsed:.2f}s"
                        )
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
                    
                    next_t0 = time.monotonic()
                    next_page_url = await self.find_next_page_url(page)
                    _log_timing(
                        "Playwright find_next_page",
                        next_t0,
                        "found" if next_page_url else "not found",
                    )
                    if not next_page_url:
                        logger.info("Кнопка 'Следующая страница' не найдена, завершаем парсинг")
                        _log_timing(f"Playwright страница {page_number} total", page_t0)
                        break
                    
                    logger.info(f"Переходим на страницу {page_number + 1}...")
                    nav_t0 = time.monotonic()
                    try:
                        await page.goto(next_page_url, wait_until='domcontentloaded', timeout=self.config['wait_timeout'])
                        await page.wait_for_timeout(3000)
                        _log_timing(f"Playwright goto page {page_number + 1}", nav_t0, "incl. 3000ms wait")
                        _log_timing(f"Playwright страница {page_number} total", page_t0)
                        page_number += 1
                    except Exception as e:
                        _log_timing(f"Playwright goto page {page_number + 1} (ошибка)", nav_t0, str(e))
                        logger.warning(f"Ошибка перехода на страницу {page_number + 1}: {e}")
                        break
                
                _log_timing(
                    "Playwright scan total",
                    pw_t0,
                    f"offers={len(all_offers)}, pages={page_number}",
                )
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

    @staticmethod
    def _dedupe_lowest_per_hotel_and_bucket(offers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Один оффер на пару (отель, duration_bucket) — для multi-duration trip-фильтров."""
        best: Dict[tuple, Dict[str, Any]] = {}
        for offer in offers:
            if not offer:
                continue
            name = (offer.get('hotel_name') or '').strip()
            if not name:
                continue
            bucket = str(offer.get('duration_bucket') or '').strip()
            try:
                price = float(offer.get('price') or 0)
            except (TypeError, ValueError):
                continue
            key = (name, bucket)
            prev = best.get(key)
            if prev is None or price < float(prev.get('price') or 0):
                best[key] = offer
        return list(best.values())

    async def _collect_virtual_filter_offers(self) -> List[Dict[str, Any]]:
        """Collect offers: one independent virtual filter per duration bucket."""
        if not has_multiple_trip_duration_buckets(self.config):
            return await self.scrape_offers_with_retry()

        virtual_datasets: List[Dict[str, Any]] = []
        saved_url = self.config.get('url')
        try:
            passes = trip_scrape_passes(self.config)
            for index, spec in enumerate(passes, start=1):
                scrape_url = str(spec.get('url') or '').strip()
                bucket_id = str(spec.get('bucket_id') or '').strip()
                if not scrape_url or not bucket_id:
                    continue
                self.config['url'] = scrape_url
                label = str(spec.get('label') or bucket_id)
                duration_q = fly_query_param(scrape_url, 'duration') or '—'
                logger.info(
                    "📏 Virtual filter %s/%s: %s (duration=%s)",
                    index,
                    len(passes),
                    label,
                    duration_q,
                )
                raw = await self.scrape_offers_with_retry()
                filtered = select_trip_offers(
                    raw,
                    self.config,
                    force_bucket_id=bucket_id,
                )
                dataset = self._dedupe_lowest_per_hotel_and_bucket(filtered)
                logger.info(
                    "   → %s scraped, %s after trip-filter, %s in virtual dataset",
                    len(raw),
                    len(filtered),
                    len(dataset),
                )
                virtual_datasets.extend(dataset)
            return virtual_datasets
        finally:
            if saved_url is not None:
                self.config['url'] = saved_url

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
            '.card-offer-search',
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
            sel_t0 = time.monotonic()
            try:
                await page.wait_for_selector(selector, timeout=10000)
                elements = await page.query_selector_all(selector)
                if elements and len(elements) > 0:
                    _log_timing(
                        "Playwright selector hit",
                        sel_t0,
                        f"{selector} → {len(elements)}",
                    )
                    return elements
                _log_timing("Playwright selector miss", sel_t0, f"{selector} → 0")
            except Exception as exc:
                _log_timing("Playwright selector timeout", sel_t0, f"{selector}: {exc}")
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
            
            rating = ""
            ta_rating = ""
            ta_review_count = ""
            ta_source = ""
            try:
                card_html = await element.inner_html()
                ta_fields = self._ta_fields_from_card(card_html)
                rating = ta_fields['rating']
                ta_rating = ta_fields['ta_rating']
                ta_review_count = ta_fields['ta_review_count']
                ta_source = ta_fields['ta_source']
            except Exception:
                pass

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
                'ta_rating': str(ta_rating)[:10],
                'ta_review_count': ta_review_count if ta_review_count != "" else "",
                'ta_source': str(ta_source)[:20],
                'departure_airport': departure_airport,
                'scraped_at': self._scrape_timestamp(),
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

    @staticmethod
    def _archive_if_month_rolled(filepath: str) -> None:
        """Если текущий месяц не совпадает с первой записью в файле — архивируем.

        Архивный файл: <dir>/archive/<name>_YYYY-MM.csv
        Если файл отсутствует или пуст — ничего не делаем.
        """
        if not os.path.exists(filepath):
            return
        try:
            # Читаем только первую строку данных (после заголовка) для определения месяца файла
            with open(filepath, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                first_row = next(reader, None)
            if first_row is None:
                return  # файл пустой
            file_ts = str(first_row.get('scraped_at') or '')
            if not file_ts:
                return
            file_month = file_ts[:7]  # 'YYYY-MM'
            current_month = datetime.now(timezone.utc).strftime('%Y-%m')
            if file_month == current_month:
                return  # тот же месяц — архивировать не нужно
            # Архивируем
            parent = os.path.dirname(filepath)
            archive_dir = os.path.join(parent, 'archive')
            os.makedirs(archive_dir, exist_ok=True)
            base = os.path.splitext(os.path.basename(filepath))[0]
            archive_path = os.path.join(archive_dir, f'{base}_{file_month}.csv')
            if not os.path.exists(archive_path):
                import shutil
                shutil.copy2(filepath, archive_path)
                logger.info(f"📦 Архивирован {os.path.basename(filepath)} → archive/{os.path.basename(archive_path)}")
            # Очищаем основной файл (оставляем только заголовок)
            with open(filepath, 'r', encoding='utf-8') as f:
                header = f.readline()
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(header)
            logger.info(f"🗂️ Начат новый месяц ({current_month}) в {os.path.basename(filepath)}")
        except Exception as e:
            logger.warning(f"Ошибка при архивировании {filepath}: {e}")

    def save_data_append(self, offers: List[Dict[str, Any]]):
        """Сохраняет данные, добавляя к существующим"""
        if not offers:
            logger.warning("Нет данных для сохранения")
            return
        
        # Создаем директорию
        os.makedirs(self.config['data_dir'], exist_ok=True)
        
        filepath = os.path.join(self.config['data_dir'], self.data_file)

        # Архивируем при смене календарного месяца (хранит полную историю, но каждый файл < 50 MB)
        self._archive_if_month_rolled(filepath)
        
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
                        normalized_row = {k: row.get(k, '') for k in TRAVEL_PRICE_CSV_FIELDS}
                        existing_data.append(normalized_row)
            except Exception as e:
                logger.warning(f"Ошибка чтения существующих данных: {e}, создаем новый файл")
                existing_data = []
        
        # Перезаписываем файл с правильными заголовками
        with open(filepath, 'w', newline='', encoding='utf-8') as csvfile:
            fieldnames = TRAVEL_PRICE_CSV_FIELDS
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

    def save_departure_offers_append(self, offers: List[Dict[str, Any]]):
        """Сохраняет все raw-офферы до дедупликации для аналитики вылетов."""
        if not offers:
            logger.warning("Нет raw-офферов для аналитики вылетов")
            return

        os.makedirs(self.config['data_dir'], exist_ok=True)
        filepath = os.path.join(self.config['data_dir'], 'departure_offers.csv')

        # Архивируем при смене календарного месяца (полная история сохраняется в archive/)
        self._archive_if_month_rolled(filepath)

        fieldnames = BASE_OFFER_FIELDS + DEPARTURE_FIELDS
        enriched = enrich_offers(offers, self.config, self.config_file)

        existing_data = []
        if os.path.exists(filepath):
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        existing_data.append({k: row.get(k, '') for k in fieldnames})
            except Exception as e:
                logger.warning(f"Ошибка чтения departure_offers.csv: {e}, создаем новый файл")
                existing_data = []


        with open(filepath, 'w', newline='', encoding='utf-8') as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames, quoting=csv.QUOTE_ALL)
            writer.writeheader()
            for row in existing_data:
                writer.writerow({k: row.get(k, '') for k in fieldnames})
            for offer in enriched:
                writer.writerow({k: offer.get(k, '') for k in fieldnames})

        logger.info(f"Добавлено {len(enriched)} raw-офферов для аналитики вылетов в {filepath}")

        try:
            result = write_departure_analytics(filepath, self.config['data_dir'])
            logger.info(
                "Обновлена аналитика вылетов: "
                f"{result['cohorts']} снимков, {result['events']} событий"
            )
        except Exception as e:
            logger.warning(f"Не удалось обновить аналитику вылетов: {e}")

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
            required_columns = TRAVEL_PRICE_CSV_FIELDS
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
            alerts_t0 = time.monotonic()
            alerts_file = self.data_file.replace('.csv', '_alerts.json')
            csv_path = os.path.join(self.config['data_dir'], self.data_file)
            if os.path.exists(csv_path):
                csv_size = os.path.getsize(csv_path)
                logger.info(f"Алерты: CSV {csv_path} ({csv_size / 1024 / 1024:.1f} MB)")
            alert_manager = PriceAlertManagerV2(
                data_file=os.path.join(self.config['data_dir'], self.data_file), 
                alerts_file=os.path.join(self.config['data_dir'], alerts_file),
                display_price_ceiling=self.config.get('display_price_ceiling', 10000),
                history_price_ceiling=self.config.get('history_price_ceiling'),
                filter_config=self.config,
            )
            
            if alert_manager.df.empty:
                logger.warning("Нет данных для проверки алертов")
                return
            
            proc_t0 = time.monotonic()
            new_alerts = alert_manager.process_all_changes()
            _log_timing("alerts process_all_changes", proc_t0, f"new={len(new_alerts)}")

            report_t0 = time.monotonic()
            report = alert_manager.create_alert_report(
                all_changes=alert_manager._last_all_changes,
            )
            _log_timing("alerts create_alert_report", report_t0)
            
            # Логируем новые алерты
            if new_alerts:
                price_drops = [a for a in new_alerts if a['price_change'] < 0]
                price_increases = [a for a in new_alerts if a['price_change'] > 0]
                
                if price_drops:
                    logger.info(f"🚨 Обнаружено {len(price_drops)} новых снижений цен >= {ALERT_THRESHOLD_PERCENT:.0f}%!")
                    for alert in price_drops[:5]:  # Показываем первые 5
                        logger.info(f"  📉 {alert['hotel_name']}: {alert['old_price']} → {alert['new_price']} PLN ({alert['price_change_pct']:+.1f}%)")
                
                if price_increases:
                    logger.info(f"🚨 Обнаружено {len(price_increases)} новых повышений цен >= {ALERT_THRESHOLD_PERCENT:.0f}%!")
                    for alert in price_increases[:5]:  # Показываем первые 5
                        logger.info(f"  📈 {alert['hotel_name']}: {alert['old_price']} → {alert['new_price']} PLN ({alert['price_change_pct']:+.1f}%)")
            else:
                logger.info("✅ Новых значительных изменений цен не обнаружено")
            
            # Сохраняем отчет
            report_path = os.path.join(self.config['data_dir'], 'price_alerts_report.txt')
            with open(report_path, 'w', encoding='utf-8') as f:
                f.write(report)
            logger.info(f"📊 Отчет об алертах сохранен: {report_path}")
            _log_timing("check_price_alerts total", alerts_t0)
                
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
        run_t0 = time.monotonic()
        logger.info(f"🚀 Начинаем мониторинг ({self.config_file}, data_dir={self.config.get('data_dir')})...")
        
        try:
            multi_bucket_trip = has_multiple_trip_duration_buckets(self.config)
            offers = await self._collect_virtual_filter_offers()

            if not offers:
                logger.error("❌ Не удалось собрать данные после всех попыток")
                return False

            raw_count = len(offers)
            if is_fixed_trip_config(self.config) and not multi_bucket_trip:
                t0 = time.monotonic()
                offers = select_trip_offers(offers, self.config)
                _log_timing("select_trip_offers", t0, f"{raw_count} → {len(offers)}")
                logger.info(
                    "📌 Trip-фильтр: %s офферов → %s (вылет в пределах ±%s дн. от %s)",
                    raw_count,
                    len(offers),
                    self.config.get("trip_departure_slip_days", 7),
                    self.config.get("trip_anchor_date", "—"),
                )
                if not offers:
                    logger.warning("⚠️ После trip-фильтра не осталось офферов с подходящим вылетом")
                    return False
                raw_count = len(offers)
            elif multi_bucket_trip:
                per_bucket: Dict[str, int] = {}
                for offer in offers:
                    bucket = str(offer.get("duration_bucket") or "").strip()
                    per_bucket[bucket] = per_bucket.get(bucket, 0) + 1
                logger.info(
                    "📌 Virtual filters merged: %s offers (%s)",
                    raw_count,
                    ", ".join(f"{k}={v}" for k, v in sorted(per_bucket.items())),
                )

            t0 = time.monotonic()
            self.save_departure_offers_append(offers)
            _log_timing("save_departure_offers + analytics", t0)

            t0 = time.monotonic()
            if multi_bucket_trip:
                dedupe_label = "virtual_filter_datasets_ready"
            elif is_fixed_trip_config(self.config) and parse_trip_duration_buckets(self.config):
                offers = self._dedupe_lowest_per_hotel_and_bucket(offers)
                dedupe_label = "dedupe_lowest_per_hotel_and_bucket"
            else:
                offers = self._dedupe_lowest_per_hotel(offers)
                dedupe_label = "dedupe_lowest_per_hotel"
            _log_timing(dedupe_label, t0, f"{raw_count} → {len(offers)}")
            if len(offers) < raw_count:
                logger.info(
                    f"Дедупликация: {raw_count} офферов → {len(offers)} отелей (мин. цена на отель)"
                )
            
            t0 = time.monotonic()
            self.detect_missing_hotels_and_alert(offers)
            _log_timing("detect_missing_hotels_and_alert", t0)
            
            t0 = time.monotonic()
            self.save_data_append(offers)
            _log_timing("save_data_append", t0)
            
            t0 = time.monotonic()
            self.create_charts()
            _log_timing("create_charts", t0)
            
            t0 = time.monotonic()
            self.generate_report()
            _log_timing("generate_report", t0)
            
            t0 = time.monotonic()
            self.check_price_alerts()
            _log_timing("check_price_alerts", t0)

            total = time.monotonic() - run_t0
            scrape_total = self._timing_summary.get("scrape_total", 0.0)
            post_total = total - scrape_total
            logger.info(
                f"📊 TIMING SUMMARY config={self.config_file}: "
                f"total={total:.1f}s scrape={scrape_total:.1f}s post={post_total:.1f}s "
                f"detail={self._timing_summary}"
            )
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

