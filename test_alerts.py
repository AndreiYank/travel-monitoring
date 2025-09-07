#!/usr/bin/env python3
"""Тест новой системы алертов"""

from price_alerts import PriceAlertManager

def test_alerts():
    print("🔍 Тестируем новую систему алертов...")
    
    am = PriceAlertManager()
    alerts = am.scan_all_price_changes(4.0)
    
    print(f"Найдено {len(alerts)} алертов >= 4%")
    
    if alerts:
        print("\nТоп-5 алертов:")
        for i, a in enumerate(alerts[:5], 1):
            hotel_name = a['hotel_name'][:30]
            change_pct = a['price_change_pct']
            old_price = a['old_price']
            new_price = a['new_price']
            print(f"{i}. {hotel_name} {change_pct:+.1f}% {old_price}→{new_price}")
    else:
        print("Алертов не найдено")

if __name__ == "__main__":
    test_alerts()
