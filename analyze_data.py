#!/usr/bin/env python3
"""
Скрипт для анализа собранных данных о ценах на путешествия
"""

import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns
from datetime import datetime
import argparse
import os
import json

class TravelDataAnalyzer:
    def __init__(self, data_file="data/travel_prices.csv"):
        self.data_file = data_file
        self.df = self.load_data()
        
    def load_data(self):
        """Загружает данные из CSV файла"""
        if not os.path.exists(self.data_file):
            print(f"❌ Файл данных не найден: {self.data_file}")
            return pd.DataFrame()
        
        try:
            df = pd.read_csv(self.data_file)
            df['scraped_at'] = pd.to_datetime(df['scraped_at'])
            print(f"✅ Загружено {len(df)} записей из {self.data_file}")
            return df
        except Exception as e:
            print(f"❌ Ошибка загрузки данных: {e}")
            return pd.DataFrame()
    
    def basic_stats(self):
        """Выводит базовую статистику"""
        if self.df.empty:
            print("❌ Нет данных для анализа")
            return
        
        print("\n📊 БАЗОВАЯ СТАТИСТИКА")
        print("=" * 50)
        print(f"Общее количество предложений: {len(self.df)}")
        print(f"Уникальных отелей: {self.df['hotel_name'].nunique()}")
        print(f"Период сбора данных: {self.df['scraped_at'].min().strftime('%Y-%m-%d')} - {self.df['scraped_at'].max().strftime('%Y-%m-%d')}")
        print(f"Средняя цена: {self.df['price'].mean():.2f} PLN")
        print(f"Медианная цена: {self.df['price'].median():.2f} PLN")
        print(f"Минимальная цена: {self.df['price'].min():.2f} PLN")
        print(f"Максимальная цена: {self.df['price'].max():.2f} PLN")
        print(f"Стандартное отклонение: {self.df['price'].std():.2f} PLN")
    
    def top_offers(self, n=10, cheapest=True):
        """Показывает топ предложений"""
        if self.df.empty:
            return
        
        print(f"\n🏆 ТОП-{n} {'САМЫХ ДЕШЕВЫХ' if cheapest else 'САМЫХ ДОРОГИХ'} ПРЕДЛОЖЕНИЙ")
        print("=" * 70)
        
        if cheapest:
            top_offers = self.df.nsmallest(n, 'price')
        else:
            top_offers = self.df.nlargest(n, 'price')
        
        for i, (_, row) in enumerate(top_offers.iterrows(), 1):
            print(f"{i:2d}. {row['hotel_name'][:50]:<50} | {row['price']:>8.0f} PLN")
            if row['dates']:
                print(f"    📅 {row['dates']}")
            if row['duration']:
                print(f"    ⏱️  {row['duration']}")
            if row['rating']:
                print(f"    ⭐ {row['rating']}")
            print(f"    📊 Собрано: {row['scraped_at'].strftime('%Y-%m-%d %H:%M')}")
            print()
    
    def price_trends(self):
        """Анализирует тренды цен"""
        if self.df.empty:
            return
        
        print("\n📈 АНАЛИЗ ТРЕНДОВ ЦЕН")
        print("=" * 50)
        
        # Группируем по дням
        daily_stats = self.df.groupby(self.df['scraped_at'].dt.date).agg({
            'price': ['mean', 'min', 'max', 'count']
        }).round(2)
        
        daily_stats.columns = ['Средняя цена', 'Мин цена', 'Макс цена', 'Количество']
        print(daily_stats)
        
        # Анализ изменений для каждого отеля
        print("\n🏨 ИЗМЕНЕНИЯ ЦЕН ПО ОТЕЛЯМ (топ-10):")
        print("-" * 60)
        
        hotel_changes = []
        for hotel in self.df['hotel_name'].unique():
            hotel_data = self.df[self.df['hotel_name'] == hotel].sort_values('scraped_at')
            if len(hotel_data) > 1:
                first_price = hotel_data.iloc[0]['price']
                last_price = hotel_data.iloc[-1]['price']
                change = last_price - first_price
                change_pct = (change / first_price) * 100 if first_price > 0 else 0
                
                hotel_changes.append({
                    'hotel': hotel,
                    'first_price': first_price,
                    'last_price': last_price,
                    'change': change,
                    'change_pct': change_pct,
                    'records': len(hotel_data)
                })
        
        # Сортируем по количеству записей
        hotel_changes.sort(key=lambda x: x['records'], reverse=True)
        
        for i, hotel in enumerate(hotel_changes[:10], 1):
            print(f"{i:2d}. {hotel['hotel'][:40]:<40} | {hotel['change']:+.0f} PLN ({hotel['change_pct']:+.1f}%) | {hotel['records']} записей")
    
    def create_advanced_charts(self):
        """Создает расширенные графики"""
        if self.df.empty:
            print("❌ Нет данных для создания графиков")
            return
        
        # Создаем директорию для графиков
        charts_dir = "data/advanced_charts"
        os.makedirs(charts_dir, exist_ok=True)
        
        # Настройка стиля
        plt.style.use('default')
        
        # 1. График цен по времени с трендом
        plt.figure(figsize=(15, 8))
        
        # Группируем по дням
        daily_prices = self.df.groupby(self.df['scraped_at'].dt.date)['price'].agg(['mean', 'min', 'max'])
        
        plt.plot(daily_prices.index, daily_prices['mean'], marker='o', linewidth=2, label='Средняя цена')
        plt.fill_between(daily_prices.index, daily_prices['min'], daily_prices['max'], alpha=0.3, label='Диапазон цен')
        
        plt.title('Динамика цен на путешествия', fontsize=16, fontweight='bold')
        plt.xlabel('Дата', fontsize=12)
        plt.ylabel('Цена (PLN)', fontsize=12)
        plt.legend()
        plt.grid(True, alpha=0.3)
        plt.xticks(rotation=45)
        plt.tight_layout()
        
        plt.savefig(f"{charts_dir}/price_dynamics.png", dpi=300, bbox_inches='tight')
        plt.close()
        
        # 2. Box plot распределения цен
        plt.figure(figsize=(12, 8))
        
        # Группируем по дням для box plot
        daily_data = []
        daily_labels = []
        for date in daily_prices.index:
            day_prices = self.df[self.df['scraped_at'].dt.date == date]['price']
            if len(day_prices) > 0:
                daily_data.append(day_prices)
                daily_labels.append(date.strftime('%m-%d'))
        
        if daily_data:
            plt.boxplot(daily_data, labels=daily_labels)
            plt.title('Распределение цен по дням', fontsize=16, fontweight='bold')
            plt.xlabel('Дата', fontsize=12)
            plt.ylabel('Цена (PLN)', fontsize=12)
            plt.xticks(rotation=45)
            plt.grid(True, alpha=0.3)
            plt.tight_layout()
            
            plt.savefig(f"{charts_dir}/price_distribution_by_day.png", dpi=300, bbox_inches='tight')
            plt.close()
        
        # 3. Топ-20 отелей по количеству предложений
        plt.figure(figsize=(15, 10))
        
        hotel_counts = self.df['hotel_name'].value_counts().head(20)
        
        bars = plt.barh(range(len(hotel_counts)), hotel_counts.values)
        plt.yticks(range(len(hotel_counts)), 
                  [name[:40] + '...' if len(name) > 40 else name for name in hotel_counts.index])
        
        plt.title('Топ-20 отелей по количеству предложений', fontsize=16, fontweight='bold')
        plt.xlabel('Количество предложений', fontsize=12)
        plt.grid(True, alpha=0.3)
        
        # Добавляем значения на столбцы
        for i, (bar, count) in enumerate(zip(bars, hotel_counts.values)):
            plt.text(bar.get_width() + 0.1, bar.get_y() + bar.get_height()/2, 
                    str(count), ha='left', va='center')
        
        plt.tight_layout()
        plt.savefig(f"{charts_dir}/top_hotels_by_offers.png", dpi=300, bbox_inches='tight')
        plt.close()
        
        # 4. Создаем отдельные графики для топ-15 самых дешевых отелей
        self.create_individual_hotel_charts()
        
        print(f"✅ Расширенные графики сохранены в {charts_dir}/")
    
    def create_individual_hotel_charts(self):
        """Создает отдельные графики для топ-15 самых дешевых отелей"""
        if self.df.empty:
            return
        
        # Создаем директорию для индивидуальных графиков
        individual_charts_dir = "data/advanced_charts/individual_hotels"
        os.makedirs(individual_charts_dir, exist_ok=True)
        
        # Получаем топ-15 самых дешевых отелей
        top_15_hotels = self.df.nsmallest(15, 'price')['hotel_name'].unique()
        
        print(f"📊 Создаем индивидуальные графики для {len(top_15_hotels)} отелей...")
        
        for i, hotel_name in enumerate(top_15_hotels, 1):
            try:
                # Фильтруем данные по отелю
                hotel_data = self.df[self.df['hotel_name'] == hotel_name].sort_values('scraped_at')
                
                if len(hotel_data) < 2:
                    continue  # Пропускаем отели с менее чем 2 записями
                
                # Создаем график для отеля
                plt.figure(figsize=(12, 8))
                
                # График изменения цены по времени
                plt.plot(hotel_data['scraped_at'], hotel_data['price'], 
                        marker='o', linewidth=2, markersize=6, color='#2E86AB')
                
                # Добавляем точки с ценами
                for _, row in hotel_data.iterrows():
                    plt.annotate(f'{row["price"]:.0f} PLN', 
                               (row['scraped_at'], row['price']),
                               textcoords="offset points", 
                               xytext=(0,10), ha='center', fontsize=8)
                
                # Настройка графика
                plt.title(f'{i}. {hotel_name[:60]}{"..." if len(hotel_name) > 60 else ""}', 
                         fontsize=14, fontweight='bold', pad=20)
                plt.xlabel('Дата сбора данных', fontsize=12)
                plt.ylabel('Цена (PLN)', fontsize=12)
                plt.grid(True, alpha=0.3)
                plt.xticks(rotation=45)
                
                # Добавляем статистику
                min_price = hotel_data['price'].min()
                max_price = hotel_data['price'].max()
                avg_price = hotel_data['price'].mean()
                price_change = hotel_data['price'].iloc[-1] - hotel_data['price'].iloc[0]
                price_change_pct = (price_change / hotel_data['price'].iloc[0]) * 100 if hotel_data['price'].iloc[0] > 0 else 0
                
                stats_text = f'Мин: {min_price:.0f} PLN | Макс: {max_price:.0f} PLN | Средн: {avg_price:.0f} PLN\n'
                stats_text += f'Изменение: {price_change:+.0f} PLN ({price_change_pct:+.1f}%)'
                
                plt.figtext(0.02, 0.02, stats_text, fontsize=10, 
                           bbox=dict(boxstyle="round,pad=0.3", facecolor="lightgray", alpha=0.8))
                
                plt.tight_layout()
                
                # Сохраняем график
                safe_filename = "".join(c for c in hotel_name if c.isalnum() or c in (' ', '-', '_')).rstrip()
                safe_filename = safe_filename[:50] + f"_{i:02d}"
                plt.savefig(f"{individual_charts_dir}/{safe_filename}.png", 
                           dpi=300, bbox_inches='tight')
                plt.close()
                
                print(f"  ✅ График {i:2d}/15: {hotel_name[:50]}{'...' if len(hotel_name) > 50 else ''}")
                
            except Exception as e:
                print(f"  ❌ Ошибка создания графика для {hotel_name}: {e}")
                continue
        
        print(f"✅ Индивидуальные графики сохранены в {individual_charts_dir}/")
    
    def export_summary(self):
        """Экспортирует сводку в JSON"""
        if self.df.empty:
            return
        
        summary = {
            "analysis_date": datetime.now().isoformat(),
            "total_offers": len(self.df),
            "unique_hotels": self.df['hotel_name'].nunique(),
            "date_range": {
                "start": self.df['scraped_at'].min().isoformat(),
                "end": self.df['scraped_at'].max().isoformat()
            },
            "price_stats": {
                "mean": float(self.df['price'].mean()),
                "median": float(self.df['price'].median()),
                "min": float(self.df['price'].min()),
                "max": float(self.df['price'].max()),
                "std": float(self.df['price'].std())
            },
            "top_cheap_offers": self.df.nsmallest(5, 'price')[['hotel_name', 'price', 'dates']].to_dict('records')
        }
        
        # Сохраняем в файл
        with open("data/analysis_summary.json", 'w', encoding='utf-8') as f:
            json.dump(summary, f, indent=2, ensure_ascii=False)
        
        print("✅ Сводка экспортирована в data/analysis_summary.json")

def main():
    parser = argparse.ArgumentParser(description='Анализ данных о ценах на путешествия')
    parser.add_argument('--data-file', default='data/travel_prices.csv', 
                       help='Путь к файлу с данными')
    parser.add_argument('--charts', action='store_true', 
                       help='Создать расширенные графики')
    parser.add_argument('--export', action='store_true', 
                       help='Экспортировать сводку в JSON')
    
    args = parser.parse_args()
    
    analyzer = TravelDataAnalyzer(args.data_file)
    
    if analyzer.df.empty:
        print("❌ Нет данных для анализа")
        return
    
    # Базовый анализ
    analyzer.basic_stats()
    analyzer.top_offers(10, cheapest=True)
    analyzer.price_trends()
    
    # Дополнительные функции
    if args.charts:
        analyzer.create_advanced_charts()
    
    if args.export:
        analyzer.export_summary()
    
    print("\n✅ Анализ завершен!")

if __name__ == "__main__":
    main()

