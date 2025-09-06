#!/usr/bin/env python3
"""
Улучшенная система алертов с детальным хранением истории изменений
"""

import pandas as pd
import json
import os
from datetime import datetime
import logging

logger = logging.getLogger(__name__)

class EnhancedAlertManager:
    def __init__(self):
        self.alerts_file = "data/price_alerts_history.json"
        self.data_file = "data/travel_prices.csv"
        
    def load_data(self):
        """Загружает данные о ценах"""
        try:
            if not os.path.exists(self.data_file):
                return pd.DataFrame()
            
            df = pd.read_csv(self.data_file)
            df['scraped_at'] = pd.to_datetime(df['scraped_at'])
            return df
        except Exception as e:
            logger.error(f"Ошибка загрузки данных: {e}")
            return pd.DataFrame()
    
    def load_alerts_history(self):
        """Загружает историю алертов"""
        try:
            if os.path.exists(self.alerts_file):
                with open(self.alerts_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            return {"alerts": [], "last_update": None}
        except Exception as e:
            logger.error(f"Ошибка загрузки истории алертов: {e}")
            return {"alerts": [], "last_update": None}
    
    def save_alerts_history(self, alerts_data):
        """Сохраняет историю алертов"""
        try:
            os.makedirs(os.path.dirname(self.alerts_file), exist_ok=True)
            with open(self.alerts_file, 'w', encoding='utf-8') as f:
                json.dump(alerts_data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Ошибка сохранения истории алертов: {e}")
    
    def get_price_changes(self):
        """Анализирует изменения цен и создает алерты"""
        df = self.load_data()
        if df.empty:
            return []
        
        alerts = []
        current_time = datetime.now()
        
        # Группируем по отелям и анализируем изменения
        for hotel_name in df['hotel_name'].unique():
            hotel_data = df[df['hotel_name'] == hotel_name].sort_values('scraped_at')
            
            if len(hotel_data) < 2:
                continue
            
            # Берем последние 2 записи
            latest = hotel_data.iloc[-1]
            previous = hotel_data.iloc[-2]
            
            current_price = latest['price']
            previous_price = previous['price']
            
            if current_price != previous_price:
                change = current_price - previous_price
                change_percent = (change / previous_price) * 100
                
                # Создаем алерт только если изменение больше 1%
                if abs(change_percent) >= 1.0:
                    alert_type = "decrease" if change < 0 else "increase"
                    alert_icon = "📉" if change < 0 else "📈"
                    alert_color = "green" if change < 0 else "red"
                else:
                    continue  # Пропускаем изменения меньше 1%
                
                alert = {
                    "id": f"{hotel_name}_{current_time.strftime('%Y%m%d_%H%M%S')}",
                    "hotel_name": hotel_name,
                    "current_price": current_price,
                    "previous_price": previous_price,
                    "change": change,
                    "change_percent": change_percent,
                    "alert_type": alert_type,
                    "icon": alert_icon,
                    "color": alert_color,
                    "timestamp": current_time.isoformat(),
                    "dates": latest.get('dates', ''),
                    "duration": latest.get('duration', ''),
                    "rating": latest.get('rating', ''),
                    "url": latest.get('url', '')
                }
                
                alerts.append(alert)
        
        return alerts
    
    def update_alerts_history(self):
        """Обновляет историю алертов"""
        current_alerts = self.get_price_changes()
        history = self.load_alerts_history()
        
        # Добавляем новые алерты
        for alert in current_alerts:
            # Проверяем, не было ли уже такого алерта
            existing = any(a['id'] == alert['id'] for a in history['alerts'])
            if not existing:
                history['alerts'].append(alert)
        
        # Обновляем время последнего обновления
        history['last_update'] = datetime.now().isoformat()
        
        # Сохраняем только последние 1000 алертов
        if len(history['alerts']) > 1000:
            history['alerts'] = history['alerts'][-1000:]
        
        self.save_alerts_history(history)
        return current_alerts
    
    def get_hotel_price_history(self, hotel_name):
        """Получает историю цен для конкретного отеля"""
        df = self.load_data()
        if df.empty:
            return []
        
        hotel_data = df[df['hotel_name'] == hotel_name].sort_values('scraped_at')
        
        history = []
        for _, row in hotel_data.iterrows():
            history.append({
                "date": row['scraped_at'].strftime('%Y-%m-%d %H:%M'),
                "price": row['price'],
                "dates": row.get('dates', ''),
                "duration": row.get('duration', ''),
                "rating": row.get('rating', '')
            })
        
        return history
    
    def get_recent_alerts(self, limit=50):
        """Получает последние алерты"""
        history = self.load_alerts_history()
        return history['alerts'][-limit:]
    
    def get_alerts_by_type(self, alert_type):
        """Получает алерты по типу (increase/decrease)"""
        history = self.load_alerts_history()
        return [alert for alert in history['alerts'] if alert['alert_type'] == alert_type]
    
    def generate_alerts_report(self):
        """Генерирует отчет об алертах"""
        current_alerts = self.update_alerts_history()
        recent_alerts = self.get_recent_alerts(20)
        
        report = []
        report.append("🚨 ОТЧЕТ ОБ АЛЕРТАХ ЦЕН")
        report.append("=" * 50)
        report.append(f"Время обновления: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}")
        report.append("")
        
        if current_alerts:
            report.append("📊 ТЕКУЩИЕ ИЗМЕНЕНИЯ ЦЕН:")
            report.append("-" * 30)
            for alert in current_alerts:
                report.append(f"{alert['icon']} {alert['hotel_name']}")
                report.append(f"   Цена: {alert['previous_price']:.0f} → {alert['current_price']:.0f} PLN")
                report.append(f"   Изменение: {alert['change']:+.0f} PLN ({alert['change_percent']:+.1f}%)")
                report.append(f"   Время: {alert['timestamp'][:19]}")
                report.append("")
        else:
            report.append("✅ Изменений цен не обнаружено")
            report.append("")
        
        if recent_alerts:
            report.append("📈 ПОСЛЕДНИЕ АЛЕРТЫ:")
            report.append("-" * 20)
            for alert in recent_alerts[-10:]:  # Последние 10
                report.append(f"{alert['icon']} {alert['hotel_name']} - {alert['change']:+.0f} PLN ({alert['change_percent']:+.1f}%)")
        
        # Статистика
        increase_alerts = self.get_alerts_by_type("increase")
        decrease_alerts = self.get_alerts_by_type("decrease")
        
        report.append("")
        report.append("📊 СТАТИСТИКА АЛЕРТОВ:")
        report.append(f"   Повышения цен: {len(increase_alerts)}")
        report.append(f"   Снижения цен: {len(decrease_alerts)}")
        report.append(f"   Всего алертов: {len(increase_alerts) + len(decrease_alerts)}")
        
        return "\n".join(report)
    
    def save_alerts_report(self):
        """Сохраняет отчет об алертах"""
        try:
            report = self.generate_alerts_report()
            os.makedirs("data", exist_ok=True)
            
            with open("data/price_alerts_report.txt", "w", encoding="utf-8") as f:
                f.write(report)
            
            logger.info("Отчет об алертах сохранен")
        except Exception as e:
            logger.error(f"Ошибка сохранения отчета об алертах: {e}")

def main():
    """Основная функция для тестирования"""
    alert_manager = EnhancedAlertManager()
    alert_manager.save_alerts_report()
    
    # Показываем статистику
    recent_alerts = alert_manager.get_recent_alerts(10)
    print(f"📊 Найдено {len(recent_alerts)} последних алертов")
    
    for alert in recent_alerts[-5:]:  # Показываем последние 5
        print(f"{alert['icon']} {alert['hotel_name']}: {alert['change']:+.0f} PLN ({alert['change_percent']:+.1f}%)")

if __name__ == "__main__":
    main()
