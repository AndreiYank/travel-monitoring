#!/usr/bin/env python3
"""
Модуль для отслеживания изменений цен и отправки алертов (версия 2)
Согласно новым требованиям:
- Документировать все одномоментные изменения цен больше 4%
- Каждый ран обрабатывать весь файл с данными
- На дашборд добавлять только новые алерты
"""

import pandas as pd
import json
import os
import csv
from datetime import datetime, timezone
from typing import List, Dict, Any, Set
import logging

from generate_inline_charts_dashboard import (
    collapse_canonical_per_run,
    iter_scrape_runs,
    _parse_price_ceiling,
)

logger = logging.getLogger(__name__)

class PriceAlertManagerV2:
    def __init__(
        self,
        data_file="data/travel_prices.csv",
        alerts_file="data/price_alerts_history.json",
        display_price_ceiling=None,
    ):
        self.data_file = data_file
        self.alerts_file = alerts_file
        self.display_price_ceiling = display_price_ceiling
        self.df_raw = self.load_data()
        self.df = self._build_canonical_df()

    def load_data(self) -> pd.DataFrame:
        """Загружает данные из CSV файла"""
        if not os.path.exists(self.data_file):
            return pd.DataFrame()
        
        try:
            df = pd.read_csv(self.data_file, quoting=csv.QUOTE_ALL, on_bad_lines='skip')
            df['scraped_at'] = pd.to_datetime(df['scraped_at'], errors='coerce', utc=True, format='ISO8601')
            df = df.dropna(subset=['scraped_at'])
            return df
        except Exception as e:
            logger.error(f"Ошибка загрузки данных: {e}")
            return pd.DataFrame()

    def _build_canonical_df(self) -> pd.DataFrame:
        """Мин. цена на отель за ран; при потолке — только офферы ≤ ceiling."""
        if self.df_raw.empty:
            return self.df_raw
        work = self.df_raw.copy()
        work['scraped_at_display'] = work['scraped_at']
        ceiling = _parse_price_ceiling(self.display_price_ceiling)
        return collapse_canonical_per_run(work, ceiling)
    
    def load_alerts(self) -> List[Dict[str, Any]]:
        """Загружает историю алертов"""
        if not os.path.exists(self.alerts_file):
            return []
        
        try:
            with open(self.alerts_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, dict) and 'alerts' in data:
                    return data['alerts']
                elif isinstance(data, list):
                    return data
                else:
                    return []
        except Exception as e:
            logger.error(f"Ошибка загрузки алертов: {e}")
            return []
    
    def save_alerts(self, alerts: List[Dict[str, Any]]):
        """Сохраняет алерты в файл"""
        try:
            with open(self.alerts_file, 'w', encoding='utf-8') as f:
                json.dump(alerts, f, indent=2, ensure_ascii=False, default=str)
        except Exception as e:
            logger.error(f"Ошибка сохранения алертов: {e}")
    
    def get_run_times(self) -> List[datetime]:
        """Получает все времена ранов (по интервалам > 5 минут)"""
        if self.df.empty:
            return []
        
        run_times = []
        for _, _, run_slice in iter_scrape_runs(self.df, time_col='scraped_at'):
            if len(run_slice) > 0:
                run_times.append(run_slice['scraped_at'].iloc[0])
        
        return sorted(run_times)
    
    def get_hotel_prices_for_run(self, run_time: datetime) -> Dict[str, float]:
        """Получает цены всех отелей для конкретного рана (канонический ряд)."""
        for _, _, run_slice in iter_scrape_runs(self.df, time_col='scraped_at'):
            if len(run_slice) > 0 and run_slice['scraped_at'].iloc[0] == run_time:
                return {
                    str(name): float(price)
                    for name, price in zip(run_slice['hotel_name'], run_slice['price'])
                }
        return {}
    
    def find_price_changes_between_runs(self, prev_run: datetime, curr_run: datetime, threshold_percent: float = 4.0) -> List[Dict[str, Any]]:
        """Находит изменения цен между двумя ранами"""
        prev_prices = self.get_hotel_prices_for_run(prev_run)
        curr_prices = self.get_hotel_prices_for_run(curr_run)
        
        changes = []
        
        # Проверяем изменения для отелей, которые есть в обоих ранах
        for hotel_name in set(prev_prices.keys()) & set(curr_prices.keys()):
            prev_price = prev_prices[hotel_name]
            curr_price = curr_prices[hotel_name]
            
            price_change = curr_price - prev_price
            price_change_pct = (price_change / prev_price) * 100 if prev_price > 0 else 0
            
            if abs(price_change_pct) >= threshold_percent:
                changes.append({
                    'hotel_name': hotel_name,
                    'old_price': prev_price,
                    'new_price': curr_price,
                    'price_change': price_change,
                    'price_change_pct': price_change_pct,
                    'timestamp': curr_run,
                    'alert_type': 'price_drop' if price_change < 0 else 'price_increase',
                    'created_at': datetime.now(timezone.utc).isoformat(),
                    'threshold_percent': threshold_percent,
                    'unique_key': f"{hotel_name}_{curr_run.strftime('%Y-%m-%d_%H-%M')}_{price_change_pct:+.1f}"
                })
        
        return changes
    
    def scan_all_runs_for_changes(self, threshold_percent: float = 4.0) -> List[Dict[str, Any]]:
        """Сканирует все раны и находит все изменения цен >= порога"""
        if self.df.empty:
            return []
        
        run_times = self.get_run_times()
        if len(run_times) < 2:
            return []
        
        all_changes = []
        
        logger.info(f"🔍 Сканируем {len(run_times)} ранов на изменения >= {threshold_percent}%...")
        
        # Сравниваем каждый ран с предыдущим
        for i in range(1, len(run_times)):
            prev_run = run_times[i-1]
            curr_run = run_times[i]
            
            changes = self.find_price_changes_between_runs(prev_run, curr_run, threshold_percent)
            all_changes.extend(changes)
            
            if changes:
                logger.info(f"  📊 Ран {curr_run}: найдено {len(changes)} изменений")
        
        logger.info(f"✅ Всего найдено изменений: {len(all_changes)}")
        return all_changes
    
    def get_existing_alert_keys(self) -> Set[str]:
        """Получает ключи существующих алертов"""
        existing_alerts = self.load_alerts()
        return {alert.get('unique_key') for alert in existing_alerts if alert.get('unique_key')}
    
    def get_new_alerts(self, all_changes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Возвращает только новые алерты (которых нет в существующих)"""
        existing_keys = self.get_existing_alert_keys()
        new_alerts = [alert for alert in all_changes if alert.get('unique_key') not in existing_keys]
        
        logger.info(f"📋 Существующих алертов: {len(existing_keys)}")
        logger.info(f"🆕 Новых алертов: {len(new_alerts)}")
        
        return new_alerts
    
    def process_all_changes(self, threshold_percent: float = 4.0) -> List[Dict[str, Any]]:
        """Пересобирает алерты из канонического ряда и возвращает только новые."""
        if self.df.empty:
            logger.warning("Нет данных для обработки")
            return []
        
        all_changes = self.scan_all_runs_for_changes(threshold_percent)
        old_keys = self.get_existing_alert_keys()
        new_alerts = [a for a in all_changes if a.get('unique_key') not in old_keys]

        # Полная пересборка истории — убирает ложные алерты от смешанных офферов.
        self.save_alerts(all_changes)
        if all_changes:
            logger.info(
                f"💾 Алертов пересобрано: {len(all_changes)} "
                f"(новых с прошлого запуска: {len(new_alerts)})"
            )
        
        return new_alerts
    
    def create_alert_report(self, threshold_percent: float = 4.0) -> str:
        """Создает отчет об изменениях цен"""
        if self.df.empty:
            return "❌ Нет данных для анализа"
        
        all_changes = self.scan_all_runs_for_changes(threshold_percent)
        
        if not all_changes:
            return "✅ Изменений цен не найдено"
        
        price_drops = [change for change in all_changes if change['price_change'] < 0]
        price_increases = [change for change in all_changes if change['price_change'] > 0]
        
        report = []
        report.append("🚨 ОТЧЕТ ОБ ИЗМЕНЕНИЯХ ЦЕН")
        report.append("=" * 50)
        report.append(f"Порог изменения: {threshold_percent}%")
        report.append(f"Всего изменений: {len(all_changes)}")
        report.append(f"Снижения цен: {len(price_drops)}")
        report.append(f"Повышения цен: {len(price_increases)}")
        report.append("")
        
        if price_drops:
            report.append("📉 СНИЖЕНИЯ ЦЕН:")
            for change in sorted(price_drops, key=lambda x: x['price_change'])[:10]:
                report.append(f"  {change['hotel_name']}: {change['old_price']} → {change['new_price']} PLN ({change['price_change_pct']:+.1f}%)")
        
        if price_increases:
            report.append("\\n📈 ПОВЫШЕНИЯ ЦЕН:")
            for change in sorted(price_increases, key=lambda x: x['price_change'], reverse=True)[:10]:
                report.append(f"  {change['hotel_name']}: {change['old_price']} → {change['new_price']} PLN ({change['price_change_pct']:+.1f}%)")
        
        return "\\n".join(report)

def main():
    """Тестирование новой логики алертов"""
    import sys
    
    if len(sys.argv) < 2:
        print("Использование: python price_alerts_v2.py <data_file> [alerts_file]")
        sys.exit(1)
    
    data_file = sys.argv[1]
    alerts_file = sys.argv[2] if len(sys.argv) > 2 else data_file.replace('.csv', '_alerts.json')
    
    print(f"🧪 Тестируем новую логику алертов:")
    print(f"📁 Данные: {data_file}")
    print(f"📁 Алерты: {alerts_file}")
    print("=" * 60)
    
    alert_manager = PriceAlertManagerV2(data_file, alerts_file, display_price_ceiling=10000)
    
    if alert_manager.df.empty:
        print("❌ Нет данных для анализа")
        sys.exit(1)
    
    print(f"📊 Загружено записей: {len(alert_manager.df)}")
    print(f"🏨 Уникальных отелей: {alert_manager.df['hotel_name'].nunique()}")
    print(f"📅 Период данных: {alert_manager.df['scraped_at'].min()} - {alert_manager.df['scraped_at'].max()}")
    
    # Обрабатываем все изменения
    new_alerts = alert_manager.process_all_changes(threshold_percent=4.0)
    
    print(f"\\n🆕 Новых алертов: {len(new_alerts)}")
    
    if new_alerts:
        print("\\n📋 Новые алерты:")
        for alert in new_alerts[-5:]:
            arrow = '↑' if alert['price_change'] > 0 else '↓'
            print(f"  {arrow} {alert['hotel_name']}: {alert['old_price']} → {alert['new_price']} PLN ({alert['price_change_pct']:+.1f}%) - {alert['timestamp']}")
    
    # Создаем отчет
    report = alert_manager.create_alert_report(threshold_percent=4.0)
    print(f"\\n📊 Отчет:")
    print(report)

if __name__ == "__main__":
    main()
