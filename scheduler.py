#!/usr/bin/env python3
"""
Планировщик для автоматического запуска мониторинга по расписанию
"""

import schedule
import time
import subprocess
import logging
import csv
from datetime import datetime
import os
import json

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('scheduler.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

class ScheduledMonitor:
    def __init__(self, config_file="scheduler_config.json"):
        self.config_file = config_file
        self.config = self.load_config()
        
    def load_config(self):
        """Загружает конфигурацию расписания"""
        default_config = {
            "enabled": True,
            "intervals": {
                "daily": "09:00",
                "hourly": False,
                "custom_hours": [9, 15, 21]
            },
            "notifications": {
                "enabled": False,
                "telegram_bot_token": "",
                "telegram_chat_id": ""
            },
            "data_retention_days": 30,
            "min_price_change_threshold": 100
        }
        
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                logger.info(f"Конфигурация расписания загружена из {self.config_file}")
                return {**default_config, **config}
            except Exception as e:
                logger.warning(f"Ошибка загрузки конфигурации: {e}. Используется дефолтная конфигурация.")
        
        # Создаем дефолтную конфигурацию
        with open(self.config_file, 'w', encoding='utf-8') as f:
            json.dump(default_config, f, indent=2, ensure_ascii=False)
        logger.info(f"Создана дефолтная конфигурация в {self.config_file}")
        return default_config
    
    def run_monitoring(self):
        """Запускает мониторинг"""
        try:
            logger.info("🚀 Запуск мониторинга по расписанию...")
            
            # Запускаем основной скрипт
            result = subprocess.run(['python', 'travel_monitor.py'], 
                                  capture_output=True, text=True, timeout=300)
            
            if result.returncode == 0:
                logger.info("✅ Мониторинг выполнен успешно")
                
                # Проверяем изменения цен
                self.check_price_changes()
                
                # Отправляем уведомления если настроено
                if self.config['notifications']['enabled']:
                    self.send_notification("Мониторинг цен завершен успешно")
                    
            else:
                logger.error(f"❌ Ошибка выполнения мониторинга: {result.stderr}")
                if self.config['notifications']['enabled']:
                    self.send_notification(f"Ошибка мониторинга: {result.stderr}")
                    
        except subprocess.TimeoutExpired:
            logger.error("❌ Таймаут выполнения мониторинга")
        except Exception as e:
            logger.error(f"❌ Неожиданная ошибка: {e}")
    
    def check_price_changes(self):
        """Проверяет значительные изменения цен"""
        try:
            import pandas as pd
            
            data_file = "data/travel_prices.csv"
            if not os.path.exists(data_file):
                return
            
            df = pd.read_csv(data_file, quoting=csv.QUOTE_ALL, on_bad_lines='skip')
            df['scraped_at'] = pd.to_datetime(df['scraped_at'])
            
            # Получаем последние данные
            latest_data = df[df['scraped_at'] == df['scraped_at'].max()]
            previous_data = df[df['scraped_at'] < df['scraped_at'].max()]
            
            if previous_data.empty:
                return
            
            # Находим значительные изменения цен
            significant_changes = []
            threshold = self.config['min_price_change_threshold']
            
            for hotel in latest_data['hotel_name'].unique():
                latest_price = latest_data[latest_data['hotel_name'] == hotel]['price'].iloc[0]
                previous_prices = previous_data[previous_data['hotel_name'] == hotel]['price']
                
                if not previous_prices.empty:
                    previous_price = previous_prices.iloc[-1]
                    change = latest_price - previous_price
                    
                    if abs(change) >= threshold:
                        significant_changes.append({
                            'hotel': hotel,
                            'previous_price': previous_price,
                            'current_price': latest_price,
                            'change': change,
                            'change_pct': (change / previous_price) * 100
                        })
            
            if significant_changes:
                self.report_price_changes(significant_changes)
                
        except Exception as e:
            logger.warning(f"Ошибка проверки изменений цен: {e}")
    
    def report_price_changes(self, changes):
        """Отправляет отчет об изменениях цен"""
        message = "📊 ЗНАЧИТЕЛЬНЫЕ ИЗМЕНЕНИЯ ЦЕН:\n\n"
        
        for change in changes[:10]:  # Топ-10 изменений
            direction = "📈" if change['change'] > 0 else "📉"
            message += f"{direction} {change['hotel'][:40]}\n"
            message += f"   {change['previous_price']:.0f} PLN → {change['current_price']:.0f} PLN "
            message += f"({change['change']:+.0f} PLN, {change['change_pct']:+.1f}%)\n\n"
        
        logger.info(f"Найдено {len(changes)} значительных изменений цен")
        
        if self.config['notifications']['enabled']:
            self.send_notification(message)
    
    def send_notification(self, message):
        """Отправляет уведомление"""
        try:
            # Telegram уведомления
            if self.config['notifications']['telegram_bot_token']:
                self.send_telegram_message(message)
                
        except Exception as e:
            logger.warning(f"Ошибка отправки уведомления: {e}")
    
    def send_telegram_message(self, message):
        """Отправляет сообщение в Telegram"""
        try:
            import requests
            
            bot_token = self.config['notifications']['telegram_bot_token']
            chat_id = self.config['notifications']['telegram_chat_id']
            
            url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            data = {
                'chat_id': chat_id,
                'text': message,
                'parse_mode': 'HTML'
            }
            
            response = requests.post(url, data=data, timeout=10)
            if response.status_code == 200:
                logger.info("✅ Telegram уведомление отправлено")
            else:
                logger.warning(f"Ошибка отправки Telegram: {response.text}")
                
        except Exception as e:
            logger.warning(f"Ошибка Telegram уведомления: {e}")
    
    def setup_schedule(self):
        """Настраивает расписание"""
        if not self.config['enabled']:
            logger.info("Мониторинг по расписанию отключен")
            return
        
        # Ежедневный запуск
        if self.config['intervals']['daily']:
            schedule.every().day.at(self.config['intervals']['daily']).do(self.run_monitoring)
            logger.info(f"Настроен ежедневный запуск в {self.config['intervals']['daily']}")
        
        # Почасовой запуск
        if self.config['intervals']['hourly']:
            schedule.every().hour.do(self.run_monitoring)
            logger.info("Настроен почасовой запуск")
        
        # Пользовательские часы
        if self.config['intervals']['custom_hours']:
            for hour in self.config['intervals']['custom_hours']:
                schedule.every().day.at(f"{hour:02d}:00").do(self.run_monitoring)
            logger.info(f"Настроен запуск в часы: {self.config['intervals']['custom_hours']}")
    
    def run_scheduler(self):
        """Запускает планировщик"""
        logger.info("🕐 Запуск планировщика мониторинга...")
        logger.info("Нажмите Ctrl+C для остановки")
        
        self.setup_schedule()
        
        try:
            while True:
                schedule.run_pending()
                time.sleep(60)  # Проверяем каждую минуту
        except KeyboardInterrupt:
            logger.info("Планировщик остановлен пользователем")

def main():
    monitor = ScheduledMonitor()
    monitor.run_scheduler()

if __name__ == "__main__":
    main()

