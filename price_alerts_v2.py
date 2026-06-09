#!/usr/bin/env python3
"""
Модуль для отслеживания изменений цен и отправки алертов (версия 2)
Согласно новым требованиям:
- Документировать все одномоментные изменения цен больше 8%
- Каждый ран обрабатывать весь файл с данными
- На дашборд добавлять только новые алерты
"""

import pandas as pd
import json
import os
import csv
from datetime import datetime, timezone
from typing import List, Dict, Any, Set, Optional
import logging

from generate_inline_charts_dashboard import (
    collapse_canonical_per_run,
    iter_scrape_runs,
    _parse_price_ceiling,
    _resolve_history_ceiling,
)
from hotel_deal_score import comeback_from_premium

logger = logging.getLogger(__name__)

ALERT_THRESHOLD_PERCENT = 8.0

class PriceAlertManagerV2:
    def __init__(
        self,
        data_file="data/travel_prices.csv",
        alerts_file="data/price_alerts_history.json",
        display_price_ceiling=None,
        history_price_ceiling=None,
    ):
        self.data_file = data_file
        self.alerts_file = alerts_file
        self.display_price_ceiling = display_price_ceiling
        self.history_price_ceiling = history_price_ceiling
        self.df_raw = self.load_data()
        self.df = self._build_canonical_df()
        self.df_history = self._build_history_df()
        self._scan_caches_ready = False
        self._run_times: List[datetime] = []
        self._zone_prices_by_run: Dict[datetime, Dict[str, float]] = {}
        self._history_prices_by_run: Dict[datetime, Dict[str, float]] = {}
        self._premium_by_run: Dict[datetime, Dict[str, Dict[str, Any]]] = {}
        self._last_all_changes: Optional[List[Dict[str, Any]]] = None

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

    def _build_history_df(self) -> pd.DataFrame:
        """Мин. цена на отель за ран в полной зоне сбора (до history ceiling)."""
        if self.df_raw.empty:
            return self.df_raw
        work = self.df_raw.copy()
        work['scraped_at_display'] = work['scraped_at']
        display = _parse_price_ceiling(self.display_price_ceiling)
        history = _resolve_history_ceiling(display, self.history_price_ceiling)
        return collapse_canonical_per_run(work, history)
    
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
    
    def _build_prices_by_run(self, frame: pd.DataFrame) -> Dict[datetime, Dict[str, float]]:
        prices_by_run: Dict[datetime, Dict[str, float]] = {}
        if frame.empty:
            return prices_by_run
        for _, _, run_slice in iter_scrape_runs(frame, time_col='scraped_at'):
            if len(run_slice) == 0:
                continue
            run_time = run_slice['scraped_at'].iloc[0]
            prices_by_run[run_time] = {
                str(name): float(price)
                for name, price in zip(run_slice['hotel_name'], run_slice['price'])
            }
        return prices_by_run

    def _ensure_scan_caches(self) -> None:
        if self._scan_caches_ready:
            return
        self._zone_prices_by_run = self._build_prices_by_run(self.df)
        self._history_prices_by_run = self._build_prices_by_run(self.df_history)
        frame = self.df_history if not self.df_history.empty else self.df
        if frame.empty:
            self._run_times = []
            self._premium_by_run = {}
        else:
            self._run_times = sorted(self._history_prices_by_run.keys())
            self._premium_by_run = self._build_premium_snapshots_by_run()
        self._scan_caches_ready = True

    def _build_premium_snapshots_by_run(self) -> Dict[datetime, Dict[str, Dict[str, Any]]]:
        """Premium peaks after each run — incremental, без пересборки CSV на каждую пару."""
        display = _parse_price_ceiling(self.display_price_ceiling)
        if display is None:
            return {}

        snapshots: Dict[datetime, Dict[str, Dict[str, Any]]] = {}
        running: Dict[str, Dict[str, Any]] = {}
        disp = float(display)

        for run_time in self._run_times:
            for hotel_name, price in self._history_prices_by_run.get(run_time, {}).items():
                info = running.get(hotel_name)
                if info is None:
                    info = {'history_max': price, 'premium_peak': None}
                    running[hotel_name] = info
                else:
                    info['history_max'] = max(float(info['history_max']), price)
                if price > disp:
                    peak = info.get('premium_peak')
                    info['premium_peak'] = price if peak is None else max(float(peak), price)
            snapshots[run_time] = {
                name: {'history_max': data['history_max'], 'premium_peak': data['premium_peak']}
                for name, data in running.items()
            }
        return snapshots

    def get_run_times(self) -> List[datetime]:
        """Времена всех ранов — по расширенному ряду (включая раны без офферов ≤ ceiling)."""
        self._ensure_scan_caches()
        return list(self._run_times)

    def get_hotel_prices_for_run(self, run_time: datetime) -> Dict[str, float]:
        """Получает цены всех отелей для конкретного рана (канонический ряд)."""
        self._ensure_scan_caches()
        return dict(self._zone_prices_by_run.get(run_time, {}))

    def get_hotel_history_prices_for_run(self, run_time: datetime) -> Dict[str, float]:
        """Цены отелей за ран в расширенной зоне сбора (≤ history ceiling)."""
        self._ensure_scan_caches()
        return dict(self._history_prices_by_run.get(run_time, {}))

    def _append_alert(
        self,
        changes: List[Dict[str, Any]],
        *,
        hotel_name: str,
        old_price,
        new_price,
        alert_type: str,
        curr_run: datetime,
        threshold_percent: float,
        unique_suffix: str,
    ) -> None:
        try:
            old_val = float(old_price) if old_price is not None else None
        except (TypeError, ValueError):
            old_val = None
        try:
            new_val = float(new_price) if new_price is not None else None
        except (TypeError, ValueError):
            new_val = None
        if new_val is None:
            return
        if old_val is not None:
            price_change = new_val - old_val
            price_change_pct = (price_change / old_val * 100.0) if old_val else 0.0
        else:
            price_change = 0.0
            price_change_pct = 0.0
        changes.append({
            'hotel_name': hotel_name,
            'old_price': old_val if old_val is not None else new_val,
            'new_price': new_val,
            'price_change': price_change,
            'price_change_pct': price_change_pct,
            'timestamp': curr_run,
            'alert_type': alert_type,
            'created_at': datetime.now(timezone.utc).isoformat(),
            'threshold_percent': threshold_percent,
            'unique_key': f"{hotel_name}_{curr_run.strftime('%Y-%m-%d_%H-%M')}_{unique_suffix}",
        })

    def find_zone_transitions_between_runs(
        self,
        prev_run: datetime,
        curr_run: datetime,
        threshold_percent: float = ALERT_THRESHOLD_PERCENT,
    ) -> List[Dict[str, Any]]:
        """Вход / выход / изменение в зоне отслеживания (≤ display ceiling)."""
        prev_zone = self.get_hotel_prices_for_run(prev_run)
        curr_zone = self.get_hotel_prices_for_run(curr_run)
        curr_history = self.get_hotel_history_prices_for_run(curr_run)

        changes: List[Dict[str, Any]] = []

        for hotel_name in set(prev_zone.keys()) & set(curr_zone.keys()):
            prev_price = prev_zone[hotel_name]
            curr_price = curr_zone[hotel_name]
            price_change_pct = (
                (curr_price - prev_price) / prev_price * 100.0 if prev_price > 0 else 0.0
            )
            if abs(price_change_pct) < threshold_percent:
                continue
            alert_type = 'price_drop' if curr_price < prev_price else 'price_increase'
            self._append_alert(
                changes,
                hotel_name=hotel_name,
                old_price=prev_price,
                new_price=curr_price,
                alert_type=alert_type,
                curr_run=curr_run,
                threshold_percent=threshold_percent,
                unique_suffix=f"{price_change_pct:+.1f}",
            )

        for hotel_name in sorted(set(prev_zone.keys()) - set(curr_zone.keys())):
            last_zone_price = prev_zone[hotel_name]
            raw_price = curr_history.get(hotel_name)
            suffix = 'zone_out_gone'
            if raw_price is not None and last_zone_price > 0:
                pct = (raw_price - last_zone_price) / last_zone_price * 100.0
                suffix = f"zone_out_{pct:+.1f}"
            self._append_alert(
                changes,
                hotel_name=hotel_name,
                old_price=last_zone_price,
                new_price=raw_price if raw_price is not None else last_zone_price,
                alert_type='zone_exit',
                curr_run=curr_run,
                threshold_percent=threshold_percent,
                unique_suffix=suffix,
            )

        return changes
    
    def find_premium_comeback_between_runs(
        self,
        prev_run: datetime,
        curr_run: datetime,
        threshold_percent: float = ALERT_THRESHOLD_PERCENT,
    ) -> List[Dict[str, Any]]:
        """Отель снова в диапазоне показа после истории выше потолка."""
        display = _parse_price_ceiling(self.display_price_ceiling)
        if display is None or self.df_raw.empty:
            return []

        self._ensure_scan_caches()
        prev_prices = self._zone_prices_by_run.get(prev_run, {})
        curr_prices = self._zone_prices_by_run.get(curr_run, {})
        premium = self._premium_by_run.get(prev_run, {})

        changes = []
        for hotel_name, curr_price in curr_prices.items():
            if hotel_name in prev_prices:
                continue
            comeback = comeback_from_premium(
                curr_price,
                premium.get(str(hotel_name)),
                display,
                min_drop_pct=threshold_percent,
            )
            if not comeback:
                continue
            peak = float(comeback['peak_price'])
            drop_pct = float(comeback['drop_from_peak_pct'])
            changes.append({
                'hotel_name': hotel_name,
                'old_price': peak,
                'new_price': curr_price,
                'price_change': curr_price - peak,
                'price_change_pct': (curr_price - peak) / peak * 100.0 if peak else 0.0,
                'timestamp': curr_run,
                'alert_type': 'premium_comeback',
                'created_at': datetime.now(timezone.utc).isoformat(),
                'threshold_percent': threshold_percent,
                'unique_key': (
                    f"{hotel_name}_{curr_run.strftime('%Y-%m-%d_%H-%M')}"
                    f"_comeback_{drop_pct:.1f}"
                ),
            })
        return changes
    
    def scan_all_runs_for_changes(self, threshold_percent: float = ALERT_THRESHOLD_PERCENT) -> List[Dict[str, Any]]:
        """Сканирует все раны и находит все изменения цен >= порога"""
        self._ensure_scan_caches()
        run_times = self._run_times
        if len(run_times) < 2:
            return []
        
        all_changes = []
        
        logger.info(f"🔍 Сканируем {len(run_times)} ранов на изменения >= {threshold_percent}%...")
        
        # Сравниваем каждый ран с предыдущим
        for i in range(1, len(run_times)):
            prev_run = run_times[i-1]
            curr_run = run_times[i]
            
            changes = self.find_zone_transitions_between_runs(
                prev_run, curr_run, threshold_percent
            )
            comebacks = self.find_premium_comeback_between_runs(
                prev_run, curr_run, threshold_percent
            )
            all_changes.extend(changes)
            all_changes.extend(comebacks)

            if changes or comebacks:
                logger.info(
                    f"  📊 Ран {curr_run}: найдено {len(changes)} событий зоны"
                    f"{f', {len(comebacks)} comeback' if comebacks else ''}"
                )
        
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
    
    def process_all_changes(self, threshold_percent: float = ALERT_THRESHOLD_PERCENT) -> List[Dict[str, Any]]:
        """Пересобирает алерты из канонического ряда и возвращает только новые."""
        if self.df.empty:
            logger.warning("Нет данных для обработки")
            return []
        
        all_changes = self.scan_all_runs_for_changes(threshold_percent)
        self._last_all_changes = all_changes
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
    
    def create_alert_report(
        self,
        threshold_percent: float = ALERT_THRESHOLD_PERCENT,
        all_changes: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        """Создает отчет об изменениях цен"""
        if self.df.empty:
            return "❌ Нет данных для анализа"

        if all_changes is None:
            all_changes = self._last_all_changes
        if all_changes is None:
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
    new_alerts = alert_manager.process_all_changes()
    
    print(f"\\n🆕 Новых алертов: {len(new_alerts)}")
    
    if new_alerts:
        print("\\n📋 Новые алерты:")
        for alert in new_alerts[-5:]:
            arrow = '↑' if alert['price_change'] > 0 else '↓'
            print(f"  {arrow} {alert['hotel_name']}: {alert['old_price']} → {alert['new_price']} PLN ({alert['price_change_pct']:+.1f}%) - {alert['timestamp']}")
    
    # Создаем отчет
    report = alert_manager.create_alert_report()
    print(f"\\n📊 Отчет:")
    print(report)

if __name__ == "__main__":
    main()
