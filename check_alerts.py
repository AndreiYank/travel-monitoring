#!/usr/bin/env python3
"""Проверить содержимое файла алертов"""

import json

def check_alerts():
    with open('data/price_alerts_history.json', 'r') as f:
        data = json.load(f)
    
    print(f"Всего алертов: {len(data)}")
    print("\nПоследние 5 алертов:")
    for i, a in enumerate(data[-5:], 1):
        hotel_name = a['hotel_name']
        alert_type = a.get('alert_type', 'unknown')
        timestamp = a.get('timestamp', 'no-time')
        print(f"{i}. {hotel_name} {alert_type} {timestamp}")

if __name__ == "__main__":
    check_alerts()
