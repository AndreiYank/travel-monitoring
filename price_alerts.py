#!/usr/bin/env python3
"""
Модуль для отслеживания изменений цен и отправки алертов
"""

import pandas as pd
import json
import os
from datetime import datetime
from typing import List, Dict, Any
import logging

logger = logging.getLogger(__name__)

class PriceAlertManager:
    def __init__(self, data_file="data/travel_prices.csv", alerts_file="data/price_alerts_history.json"):
        self.data_file = data_file
        self.alerts_file = alerts_file
        self.df = self.load_data()
        
    def load_data(self) -> pd.DataFrame:
        """Загружает данные из CSV файла"""
        if not os.path.exists(self.data_file):
            return pd.DataFrame()
        
        try:
            df = pd.read_csv(self.data_file)
            # Используем robust парсинг дат как в других файлах
            df['scraped_at'] = pd.to_datetime(df['scraped_at'], errors='coerce', utc=True)
            df = df.dropna(subset=['scraped_at'])
            return df
        except Exception as e:
            logger.error(f"Ошибка загрузки данных: {e}")
            return pd.DataFrame()
    
    def load_alerts(self) -> List[Dict[str, Any]]:
        """Загружает историю алертов"""
        if not os.path.exists(self.alerts_file):
            return []
        
        try:
            with open(self.alerts_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                # Если это старая структура с "alerts" ключом
                if isinstance(data, dict) and 'alerts' in data:
                    return data['alerts']
                # Если это уже список
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
    
    def check_price_changes(self, threshold_percent: float = 4.0) -> List[Dict[str, Any]]:
        """Проверяет изменения цен между всеми соседними записями и возвращает алерты"""
        if self.df.empty:
            return []
        
        alerts = []
        
        # Группируем по отелям
        for hotel_name in self.df['hotel_name'].unique():
            hotel_data = self.df[self.df['hotel_name'] == hotel_name].sort_values('scraped_at')
            
            if len(hotel_data) < 2:
                continue
            
            # Проверяем изменения между всеми соседними записями
            for i in range(1, len(hotel_data)):
                prev_price = hotel_data.iloc[i-1]['price']
                curr_price = hotel_data.iloc[i]['price']
                prev_date = hotel_data.iloc[i-1]['scraped_at']
                curr_date = hotel_data.iloc[i]['scraped_at']
                
                # Вычисляем изменение
                price_change = curr_price - prev_price
                price_change_pct = (price_change / prev_price) * 100 if prev_price > 0 else 0
                
                # Проверяем, превышает ли изменение порог
                if abs(price_change_pct) >= threshold_percent:
                    alert = {
                        'hotel_name': hotel_name,
                        'old_price': prev_price,
                        'new_price': curr_price,
                        'price_change': price_change,
                        'price_change_pct': price_change_pct,
                        'timestamp': curr_date.isoformat(),
                        'alert_type': 'price_drop' if price_change < 0 else 'price_increase',
                        'created_at': datetime.now().isoformat(),
                        'threshold_percent': threshold_percent,
                        # Уникальный ключ для дедупликации
                        'unique_key': f"{hotel_name}_{curr_date.strftime('%Y-%m-%d_%H-%M')}_{price_change_pct:.1f}"
                    }
                    alerts.append(alert)
        
        return alerts
    
    def deduplicate_alerts(self, new_alerts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Удаляет дубликаты алертов на основе unique_key"""
        existing_alerts = self.load_alerts()
        existing_keys = {alert.get('unique_key') for alert in existing_alerts if 'unique_key' in alert}
        
        # Фильтруем новые алерты, оставляя только уникальные
        unique_new_alerts = []
        for alert in new_alerts:
            if alert.get('unique_key') not in existing_keys:
                unique_new_alerts.append(alert)
                existing_keys.add(alert.get('unique_key'))
        
        return unique_new_alerts
    
    def save_new_alerts(self, new_alerts: List[Dict[str, Any]]):
        """Сохраняет только новые (не дублирующиеся) алерты"""
        if not new_alerts:
            return
        
        # Загружаем существующие алерты
        existing_alerts = self.load_alerts()
        
        # Дедуплицируем новые алерты
        unique_new_alerts = self.deduplicate_alerts(new_alerts)
        
        if unique_new_alerts:
            # Добавляем новые алерты к существующим
            all_alerts = existing_alerts + unique_new_alerts
            
            # Сохраняем обновленный список
            try:
                with open(self.alerts_file, 'w', encoding='utf-8') as f:
                    json.dump(all_alerts, f, indent=2, ensure_ascii=False, default=str)
                logger.info(f"Сохранено {len(unique_new_alerts)} новых алертов (пропущено {len(new_alerts) - len(unique_new_alerts)} дубликатов)")
            except Exception as e:
                logger.error(f"Ошибка сохранения новых алертов: {e}")
        else:
            logger.info("Нет новых алертов для сохранения")
    
    def get_price_drops(self, threshold_percent: float = 4.0) -> List[Dict[str, Any]]:
        """Возвращает только снижения цен"""
        all_alerts = self.check_price_changes(threshold_percent)
        return [alert for alert in all_alerts if alert['price_change'] < 0]
    
    def get_price_increases(self, threshold_percent: float = 4.0) -> List[Dict[str, Any]]:
        """Возвращает только повышения цен"""
        all_alerts = self.check_price_changes(threshold_percent)
        return [alert for alert in all_alerts if alert['price_change'] > 0]
    
    def scan_all_price_changes(self, threshold_percent: float = 4.0) -> List[Dict[str, Any]]:
        """Сканирует всю базу данных и находит все изменения цен >= порога"""
        if self.df.empty:
            return []
        
        logger.info(f"🔍 Сканируем всю базу данных на изменения >= {threshold_percent}%...")
        all_alerts = self.check_price_changes(threshold_percent)
        
        # Сохраняем только новые алерты (с дедупликацией)
        self.save_new_alerts(all_alerts)
        
        return all_alerts
    
    def create_alert_report(self, threshold_percent: float = 4.0) -> str:
        """Создает отчет об изменениях цен"""
        if self.df.empty:
            return "❌ Нет данных для анализа"
        
        price_drops = self.get_price_drops(threshold_percent)
        price_increases = self.get_price_increases(threshold_percent)
        
        report = []
        report.append("🚨 ОТЧЕТ ОБ ИЗМЕНЕНИЯХ ЦЕН")
        report.append("=" * 50)
        report.append(f"Порог изменения: {threshold_percent}%")
        report.append(f"Дата анализа: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        report.append("")
        
        if price_drops:
            report.append("📉 СНИЖЕНИЯ ЦЕН:")
            report.append("-" * 30)
            for i, alert in enumerate(price_drops, 1):
                report.append(f"{i}. {alert['hotel_name'][:50]}")
                report.append(f"   Было: {alert['old_price']:.0f} PLN → Стало: {alert['new_price']:.0f} PLN")
                report.append(f"   Изменение: {alert['price_change']:+.0f} PLN ({alert['price_change_pct']:+.1f}%)")
                report.append(f"   Время: {alert['timestamp'][:19]}")
                report.append("")
        else:
            report.append("📉 Снижений цен не обнаружено")
            report.append("")
        
        if price_increases:
            report.append("📈 ПОВЫШЕНИЯ ЦЕН:")
            report.append("-" * 30)
            for i, alert in enumerate(price_increases, 1):
                report.append(f"{i}. {alert['hotel_name'][:50]}")
                report.append(f"   Было: {alert['old_price']:.0f} PLN → Стало: {alert['new_price']:.0f} PLN")
                report.append(f"   Изменение: {alert['price_change']:+.0f} PLN ({alert['price_change_pct']:+.1f}%)")
                report.append(f"   Время: {alert['timestamp'][:19]}")
                report.append("")
        else:
            report.append("📈 Повышений цен не обнаружено")
            report.append("")
        
        return "\n".join(report)
    
    def save_alert_report(self, threshold_percent: float = 5.0):
        """Сохраняет отчет об алертах в файл"""
        report = self.create_alert_report(threshold_percent)
        
        report_path = "data/price_alerts_report.txt"
        try:
            with open(report_path, 'w', encoding='utf-8') as f:
                f.write(report)
            logger.info(f"Отчет об алертах сохранен: {report_path}")
        except Exception as e:
            logger.error(f"Ошибка сохранения отчета об алертах: {e}")
    
    def get_top_cheap_hotels_with_alerts(self, n: int = 15) -> List[Dict[str, Any]]:
        """Возвращает топ самых дешевых отелей с информацией об изменениях цен"""
        if self.df.empty:
            return []
        
        # Получаем топ самых дешевых отелей
        top_hotels = self.df.nsmallest(n, 'price')['hotel_name'].unique()
        
        result = []
        for hotel_name in top_hotels:
            hotel_data = self.df[self.df['hotel_name'] == hotel_name].sort_values('scraped_at')
            
            if len(hotel_data) < 2:
                continue
            
            # Получаем статистику по отелю
            first_price = hotel_data.iloc[0]['price']
            last_price = hotel_data.iloc[-1]['price']
            min_price = hotel_data['price'].min()
            max_price = hotel_data['price'].max()
            avg_price = hotel_data['price'].mean()
            
            price_change = last_price - first_price
            price_change_pct = (price_change / first_price) * 100 if first_price > 0 else 0
            
            # Определяем статус
            if price_change < 0:
                status = "📉 СНИЖЕНИЕ"
                status_color = "green"
            elif price_change > 0:
                status = "📈 ПОВЫШЕНИЕ"
                status_color = "red"
            else:
                status = "➡️ БЕЗ ИЗМЕНЕНИЙ"
                status_color = "gray"
            
            hotel_info = {
                'hotel_name': hotel_name,
                'current_price': last_price,
                'min_price': min_price,
                'max_price': max_price,
                'avg_price': avg_price,
                'price_change': price_change,
                'price_change_pct': price_change_pct,
                'status': status,
                'status_color': status_color,
                'records_count': len(hotel_data),
                'first_date': hotel_data.iloc[0]['scraped_at'].isoformat(),
                'last_date': hotel_data.iloc[-1]['scraped_at'].isoformat()
            }
            
            result.append(hotel_info)
        
        return result

def main():
    """Главная функция для тестирования"""
    alert_manager = PriceAlertManager()
    
    if alert_manager.df.empty:
        print("❌ Нет данных для анализа")
        return
    
    print("🔍 Анализируем изменения цен...")
    
    # Создаем отчет
    alert_manager.save_alert_report(threshold_percent=5.0)
    
    # Получаем топ отелей с алертами
    top_hotels = alert_manager.get_top_cheap_hotels_with_alerts(15)
    
    print(f"\n📊 ТОП-15 САМЫХ ДЕШЕВЫХ ОТЕЛЕЙ С ИЗМЕНЕНИЯМИ ЦЕН:")
    print("=" * 80)
    
    for i, hotel in enumerate(top_hotels, 1):
        print(f"{i:2d}. {hotel['hotel_name'][:50]:<50} | {hotel['current_price']:>8.0f} PLN | {hotel['status']}")
        print(f"    Изменение: {hotel['price_change']:+.0f} PLN ({hotel['price_change_pct']:+.1f}%) | Записей: {hotel['records_count']}")
        print()

if __name__ == "__main__":
    main()
