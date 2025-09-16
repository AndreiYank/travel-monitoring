#!/usr/bin/env python3
"""
Менеджер видимости предложений - скрывает предложения, которых нет в последнем актуальном ране
и показывает их снова, когда они появляются в новом ране.
"""

import pandas as pd
import json
import os
from datetime import datetime, timedelta
from typing import Dict, List, Set, Optional
import logging

logger = logging.getLogger(__name__)

class OfferVisibilityManager:
    def __init__(self, data_file: str = 'data/travel_prices.csv', 
                 visibility_state_file: str = 'data/offer_visibility_state.json'):
        self.data_file = data_file
        self.visibility_state_file = visibility_state_file
        self.visibility_state = self.load_visibility_state()
        
    def load_visibility_state(self) -> Dict:
        """Загружает состояние видимости предложений"""
        if os.path.exists(self.visibility_state_file):
            try:
                with open(self.visibility_state_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Ошибка загрузки состояния видимости: {e}")
        
        return {
            'last_run_timestamp': None,
            'visible_offers': set(),
            'hidden_offers': set(),
            'offer_history': {}  # История появления/исчезновения предложений
        }
    
    def save_visibility_state(self):
        """Сохраняет состояние видимости предложений"""
        try:
            # Конвертируем sets в lists для JSON сериализации
            state_to_save = {
                'last_run_timestamp': self.visibility_state['last_run_timestamp'],
                'visible_offers': list(self.visibility_state['visible_offers']),
                'hidden_offers': list(self.visibility_state['hidden_offers']),
                'offer_history': self.visibility_state['offer_history']
            }
            
            with open(self.visibility_state_file, 'w', encoding='utf-8') as f:
                json.dump(state_to_save, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Ошибка сохранения состояния видимости: {e}")
    
    def get_offer_key(self, row: pd.Series) -> str:
        """Создает уникальный ключ для предложения"""
        # Используем комбинацию отеля, цены, дат и аэропорта для уникальной идентификации
        hotel = str(row.get('hotel_name', ''))
        price = str(row.get('price', ''))
        dates = str(row.get('dates', ''))
        airport = str(row.get('from_airport', ''))
        duration = str(row.get('duration', ''))
        
        return f"{hotel}|{price}|{dates}|{airport}|{duration}"
    
    def identify_runs(self, df: pd.DataFrame) -> List[Dict]:
        """Определяет раны в данных (интервалы > 5 минут между записями)"""
        if df.empty:
            return []
            
        df_sorted = df.sort_values('scraped_at_display')
        df_sorted['time_diff'] = df_sorted['scraped_at_display'].diff()
        
        # Находим границы ранов (интервалы > 5 минут)
        run_boundaries = df_sorted[df_sorted['time_diff'] > pd.Timedelta(minutes=5)].index.tolist()
        
        # Добавляем начало и конец данных
        run_starts = [0] + run_boundaries
        run_ends = run_boundaries + [len(df_sorted)]
        
        runs = []
        for i, (start_idx, end_idx) in enumerate(zip(run_starts, run_ends)):
            run_data = df_sorted.iloc[start_idx:end_idx]
            if len(run_data) > 0:
                runs.append({
                    'index': i,
                    'start_idx': start_idx,
                    'end_idx': end_idx,
                    'data': run_data,
                    'timestamp': run_data['scraped_at_display'].iloc[0],
                    'is_latest': i == len(run_starts) - 1
                })
        
        return runs
    
    def get_offers_from_run(self, run_data: pd.DataFrame) -> Set[str]:
        """Извлекает уникальные предложения из рана"""
        offers = set()
        for _, row in run_data.iterrows():
            offer_key = self.get_offer_key(row)
            offers.add(offer_key)
        return offers
    
    def update_visibility(self, df: pd.DataFrame):
        """Обновляет видимость предложений на основе последнего рана"""
        if df.empty:
            logger.warning("Нет данных для обновления видимости")
            return
        
        runs = self.identify_runs(df)
        if not runs:
            logger.warning("Не найдено ранов в данных")
            return
        
        # Получаем предложения из последнего рана
        latest_run = runs[-1]
        current_offers = self.get_offers_from_run(latest_run['data'])
        
        # Получаем предложения из предыдущего рана (если есть)
        previous_offers = set()
        if len(runs) > 1:
            previous_run = runs[-2]
            previous_offers = self.get_offers_from_run(previous_run['data'])
        
        # Обновляем состояние видимости
        current_timestamp = latest_run['timestamp'].isoformat()
        
        # Предложения, которые появились в текущем ране
        new_offers = current_offers - previous_offers
        
        # Предложения, которые исчезли в текущем ране
        disappeared_offers = previous_offers - current_offers
        
        # Обновляем видимые предложения
        self.visibility_state['visible_offers'] = current_offers
        
        # Обновляем скрытые предложения (те, что были видимы, но исчезли)
        self.visibility_state['hidden_offers'] = disappeared_offers
        
        # Обновляем историю
        for offer in new_offers:
            if offer not in self.visibility_state['offer_history']:
                self.visibility_state['offer_history'][offer] = {
                    'first_seen': current_timestamp,
                    'last_seen': current_timestamp,
                    'disappeared_count': 0,
                    'reappeared_count': 0
                }
            else:
                self.visibility_state['offer_history'][offer]['last_seen'] = current_timestamp
                self.visibility_state['offer_history'][offer]['reappeared_count'] += 1
        
        for offer in disappeared_offers:
            if offer in self.visibility_state['offer_history']:
                self.visibility_state['offer_history'][offer]['disappeared_count'] += 1
        
        # Обновляем timestamp последнего рана
        self.visibility_state['last_run_timestamp'] = current_timestamp
        
        logger.info(f"Обновлена видимость: {len(current_offers)} видимых, {len(disappeared_offers)} скрытых предложений")
        
        # Сохраняем состояние
        self.save_visibility_state()
    
    def filter_visible_offers(self, df: pd.DataFrame) -> pd.DataFrame:
        """Фильтрует DataFrame, оставляя только видимые предложения"""
        if df.empty:
            return df
        
        visible_offers = self.visibility_state.get('visible_offers', set())
        if not visible_offers:
            # Если нет информации о видимых предложениях, показываем все
            return df
        
        # Создаем маску для видимых предложений
        mask = df.apply(lambda row: self.get_offer_key(row) in visible_offers, axis=1)
        
        filtered_df = df[mask].copy()
        logger.info(f"Отфильтровано: {len(df)} -> {len(filtered_df)} предложений")
        
        return filtered_df
    
    def get_visibility_stats(self) -> Dict:
        """Возвращает статистику видимости предложений"""
        return {
            'visible_count': len(self.visibility_state.get('visible_offers', set())),
            'hidden_count': len(self.visibility_state.get('hidden_offers', set())),
            'last_run_timestamp': self.visibility_state.get('last_run_timestamp'),
            'total_tracked_offers': len(self.visibility_state.get('offer_history', {}))
        }
    
    def reset_visibility(self):
        """Сбрасывает состояние видимости (показывает все предложения)"""
        self.visibility_state = {
            'last_run_timestamp': None,
            'visible_offers': set(),
            'hidden_offers': set(),
            'offer_history': {}
        }
        self.save_visibility_state()
        logger.info("Состояние видимости сброшено")

def main():
    """Тестирование функциональности"""
    import sys
    
    if len(sys.argv) > 1:
        data_file = sys.argv[1]
    else:
        data_file = 'data/travel_prices.csv'
    
    manager = OfferVisibilityManager(data_file)
    
    # Загружаем данные
    try:
        df = pd.read_csv(data_file, quoting=1, on_bad_lines='skip')
        df['scraped_at_display'] = pd.to_datetime(df['scraped_at'], errors='coerce')
        df = df.dropna(subset=['scraped_at_display'])
        
        print(f"Загружено {len(df)} записей")
        
        # Обновляем видимость
        manager.update_visibility(df)
        
        # Показываем статистику
        stats = manager.get_visibility_stats()
        print(f"Статистика видимости: {stats}")
        
        # Фильтруем данные
        filtered_df = manager.filter_visible_offers(df)
        print(f"После фильтрации: {len(filtered_df)} записей")
        
    except Exception as e:
        print(f"Ошибка: {e}")

if __name__ == "__main__":
    main()
