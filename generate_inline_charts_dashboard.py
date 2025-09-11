#!/usr/bin/env python3
"""
Дашборд с встроенными графиками вместо модальных окон
"""

import pandas as pd
import json
import csv
from datetime import datetime, timedelta, timezone
import os
import re

def generate_inline_charts_dashboard(data_file: str = 'data/travel_prices.csv', output_file: str = 'index.html', title: str = 'Travel Price Monitor • Расширенный дашборд', charts_subdir: str = 'hotel-charts', tz: str = 'Europe/Warsaw', alerts_file: str = None):
    """Генерирует дашборд с встроенными графиками"""
    
    # Загружаем данные
    try:
        df = pd.read_csv(data_file, quoting=csv.QUOTE_ALL, on_bad_lines='skip')
        # Нормализуем время: аккуратно обрабатываем смешанные строки (с/без таймзоны)
        raw = df['scraped_at'].astype(str)
        mask_tz = raw.str.contains(r"Z$|[+-]\d{2}:\d{2}$", regex=True)
        tz_series = pd.to_datetime(raw.where(mask_tz), errors='coerce', utc=True)
        tz_series = tz_series.dt.tz_convert(tz)
        naive_series = pd.to_datetime(raw.where(~mask_tz), errors='coerce')
        try:
            naive_series = naive_series.dt.tz_localize(tz)
        except Exception:
            # Если часть уже осознанно tz-aware/NaT — оставим как есть
            pass
        df['scraped_at_local'] = tz_series.combine_first(naive_series)
        # Убираем строки с некорректной датой
        df = df.dropna(subset=['scraped_at_local'])
        # Используем локализованное время без дополнительных сдвигов
        df['scraped_at_display'] = df['scraped_at_local']
        print(f"✅ Загружено {len(df)} записей")
    except Exception as e:
        print(f"❌ Ошибка загрузки данных: {e}")
        return
    
    # Вычисляем статистику
    total_offers = len(df)
    unique_hotels = df['hotel_name'].nunique()
    avg_price = df['price'].mean()
    min_price = df['price'].min()
    max_price = df['price'].max()

    # Функция для генерации hover-данных с использованием встроенных возможностей Plotly
    def generate_hover_data(detailed_data):
        """Генерирует данные для hover с детальной информацией о ране"""
        hover_data = {
            'title': f"📊 ТОП-10 ({detailed_data['run_time']})",
            'avg_price': detailed_data.get('avg_price', 0),
            'avg_change': None,
            'price_changes': [],
            'new_hotels': [],
            'removed_hotels': [],
            'no_changes': False
        }
        
        # Изменение средней цены
        if detailed_data.get('avg_price_change', 0) != 0:
            change = detailed_data['avg_price_change']
            change_percent = detailed_data.get('avg_price_change_percent', 0)
            arrow = "↗️" if change > 0 else "↘️"
            sign = "+" if change > 0 else ""
            
            hover_data['avg_change'] = {
                'arrow': arrow,
                'change': change,
                'change_percent': change_percent,
                'sign': sign
            }
        
        # Изменения цен отелей
        if detailed_data.get('price_changes') and len(detailed_data['price_changes']) > 0:
            for change in detailed_data['price_changes']:
                arrow = "↗️" if change['change'] > 0 else "↘️"
                sign = "+" if change['change'] > 0 else ""
                
                hover_data['price_changes'].append({
                    'name': change['name'],
                    'old_price': change['old_price'],
                    'new_price': change['new_price'],
                    'change': change['change'],
                    'change_percent': change['change_percent'],
                    'arrow': arrow,
                    'sign': sign
                })
        
        # Новые отели в ТОП-10
        if detailed_data.get('new_hotels') and len(detailed_data['new_hotels']) > 0:
            for hotel in detailed_data['new_hotels']:
                hover_data['new_hotels'].append({
                    'name': hotel['name'],
                    'price': hotel['price'],
                    'position': hotel['position']
                })
        
        # Отели, покинувшие ТОП-10
        if detailed_data.get('removed_hotels') and len(detailed_data['removed_hotels']) > 0:
            for hotel in detailed_data['removed_hotels']:
                hover_data['removed_hotels'].append({
                    'name': hotel['name'],
                    'price': hotel['price'],
                    'position': hotel['position']
                })
        
        # Если нет изменений
        if (not detailed_data.get('price_changes') or len(detailed_data['price_changes']) == 0) and \
           (not detailed_data.get('new_hotels') or len(detailed_data['new_hotels']) == 0) and \
           (not detailed_data.get('removed_hotels') or len(detailed_data['removed_hotels']) == 0) and \
           detailed_data.get('avg_price_change', 0) == 0:
            hover_data['no_changes'] = True
        
        return hover_data

    # Средняя цена ТОП-10 дешёвых предложений по ранам с детальной информацией
    try:
        # Определяем раны по большим интервалам между записями
        df_sorted = df.sort_values('scraped_at_display')
        run_data = []
        top10_detailed_data = []  # Детальная информация для hover
        
        # Находим границы ранов (интервалы > 5 минут)
        df_sorted['time_diff'] = df_sorted['scraped_at_display'].diff()
        run_boundaries = df_sorted[df_sorted['time_diff'] > pd.Timedelta(minutes=5)].index.tolist()
        
        # Добавляем начало и конец данных
        run_starts = [0] + run_boundaries
        run_ends = run_boundaries + [len(df_sorted)]
        
        print(f"🔍 Найдено {len(run_starts)} ранов")
        
        # Обрабатываем каждый ран
        for i, (start_idx, end_idx) in enumerate(zip(run_starts, run_ends)):
            run_data_slice = df_sorted.iloc[start_idx:end_idx]
            if len(run_data_slice) == 0:
                continue
                
            run_time = run_data_slice['scraped_at_display'].iloc[0]  # Время начала рана
            
            # Для каждого рана берем последние данные по каждому отелю в этом ране
            latest_prices = []
            hotel_prices = {}  # Словарь отель -> цена для этого рана
            
            # Берем последние данные по каждому отелю в этом ране
            for hotel_name, hotel_grp in run_data_slice.groupby('hotel_name'):
                if not hotel_grp.empty:
                    latest_price = hotel_grp.iloc[-1]['price']
                    latest_prices.append(latest_price)
                    hotel_prices[hotel_name] = latest_price
            
            if len(latest_prices) >= 10:
                # Берем ТОП-10 дешевых из всех отелей на этот ран
                sorted_prices = sorted(latest_prices)
                top10_prices = sorted_prices[:10]
                avg_price = sum(top10_prices) / len(top10_prices)
                
                # Находим отели, которые попали в ТОП-10
                top10_hotels = []
                for hotel_name, price in hotel_prices.items():
                    if price in top10_prices:
                        top10_hotels.append({
                            'name': hotel_name,
                            'price': price,
                            'position': sorted_prices.index(price) + 1
                        })
                
                # Сортируем по позиции в ТОП-10
                top10_hotels.sort(key=lambda x: x['position'])
                
                # Добавляем точку для каждого рана (убираем фильтрацию по одинаковым ценам)
                run_data.append((run_time, avg_price))
                top10_detailed_data.append({
                    'run_time': run_time,
                    'avg_price': avg_price,
                    'top10_hotels': top10_hotels
                })
            elif len(latest_prices) > 0:
                # Если отелей меньше 10, берем все
                avg_price = sum(latest_prices) / len(latest_prices)
                
                # Все отели попадают в "ТОП"
                sorted_prices = sorted(latest_prices)
                top_hotels = []
                for hotel_name, price in hotel_prices.items():
                    top_hotels.append({
                        'name': hotel_name,
                        'price': price,
                        'position': sorted_prices.index(price) + 1
                    })
                
                # Добавляем точку для каждого рана (убираем фильтрацию по одинаковым ценам)
                run_data.append((run_time, avg_price))
                top10_detailed_data.append({
                    'run_time': run_time,
                    'avg_price': avg_price,
                    'top10_hotels': top_hotels
                })
        
        if run_data:
            top10_x_values = [ts.strftime('%Y-%m-%d %H:%M') for ts, _ in run_data]
            top10_y_values = [float(price) for _, price in run_data]
            
            # Добавляем информацию об изменениях цен для каждого рана
            for i, detailed in enumerate(top10_detailed_data):
                if i == 0:
                    # Первый ран - нет изменений
                    detailed['price_changes'] = []
                    detailed['new_hotels'] = []
                    detailed['removed_hotels'] = []
                else:
                    # Сравниваем с предыдущим раном
                    prev_detailed = top10_detailed_data[i-1]
                    current_hotels = {h['name']: h for h in detailed['top10_hotels']}
                    prev_hotels = {h['name']: h for h in prev_detailed['top10_hotels']}
                    
                    # Находим изменения цен
                    price_changes = []
                    for hotel_name, current_hotel in current_hotels.items():
                        if hotel_name in prev_hotels:
                            prev_price = prev_hotels[hotel_name]['price']
                            current_price = current_hotel['price']
                            if prev_price != current_price:
                                price_changes.append({
                                    'name': hotel_name,
                                    'old_price': prev_price,
                                    'new_price': current_price,
                                    'change': current_price - prev_price,
                                    'change_percent': ((current_price - prev_price) / prev_price) * 100,
                                    'position': current_hotel['position']
                                })
                    
                    # Находим новые и удаленные отели
                    new_hotels = []
                    removed_hotels = []
                    
                    for hotel_name in current_hotels:
                        if hotel_name not in prev_hotels:
                            new_hotels.append({
                                'name': hotel_name,
                                'price': current_hotels[hotel_name]['price'],
                                'position': current_hotels[hotel_name]['position']
                            })
                    
                    for hotel_name in prev_hotels:
                        if hotel_name not in current_hotels:
                            removed_hotels.append({
                                'name': hotel_name,
                                'price': prev_hotels[hotel_name]['price'],
                                'position': prev_hotels[hotel_name]['position']
                            })
                    
                    detailed['price_changes'] = price_changes
                    detailed['new_hotels'] = new_hotels
                    detailed['removed_hotels'] = removed_hotels
                
                # Добавляем информацию об изменении средней цены
                if i > 0:
                    prev_avg = top10_detailed_data[i-1]['avg_price']
                    current_avg = detailed['avg_price']
                    detailed['avg_price_change'] = current_avg - prev_avg
                    detailed['avg_price_change_percent'] = ((current_avg - prev_avg) / prev_avg) * 100
                else:
                    detailed['avg_price_change'] = 0
                    detailed['avg_price_change_percent'] = 0
                
                # Создаем данные для hover с использованием встроенных возможностей Plotly
                detailed['hover_data'] = generate_hover_data(detailed)
            
            print(f"🔍 Отладка ТОП-10: {len(run_data)} точек данных")
            if run_data:
                print(f"   Последняя точка: {run_data[-1][1]:.2f} PLN")
        else:
            top10_x_values, top10_y_values = [], []
            top10_detailed_data = []
            print("❌ Нет данных для ТОП-10 графика")
            
    except Exception as e:
        print(f"Ошибка расчета ТОП-10: {e}")
        top10_x_values, top10_y_values = [], []
        top10_detailed_data = []
    
    # Индекс ценовой динамики (Price Trend Index)
    try:
        print("📊 Расчет индекса ценовой динамики...")
        trend_index_x_values, trend_index_y_values = [], []
        trend_index_detailed_data = []
        
        # Словарь для хранения предыдущих цен каждого отеля
        prev_hotel_prices = {}
        
        # Обрабатываем каждый ран
        for i, (start_idx, end_idx) in enumerate(zip(run_starts, run_ends)):
            run_data_slice = df_sorted.iloc[start_idx:end_idx]
            if len(run_data_slice) == 0:
                continue
                
            run_time = run_data_slice['scraped_at_display'].iloc[0]  # Время начала рана
            
            # Собираем текущие цены отелей в этом ране
            current_hotel_prices = {}
            for hotel_name, hotel_grp in run_data_slice.groupby('hotel_name'):
                if not hotel_grp.empty:
                    latest_price = hotel_grp.iloc[-1]['price']
                    current_hotel_prices[hotel_name] = latest_price
            
            # Рассчитываем индекс ценовой динамики
            total_price_change = 0
            hotels_with_changes = 0
            price_changes = []
            
            for hotel_name, current_price in current_hotel_prices.items():
                if hotel_name in prev_hotel_prices:
                    prev_price = prev_hotel_prices[hotel_name]
                    if prev_price > 0:  # Избегаем деления на ноль
                        price_change_pct = ((current_price - prev_price) / prev_price) * 100
                        total_price_change += price_change_pct
                        hotels_with_changes += 1
                        price_changes.append({
                            'hotel': hotel_name,
                            'prev_price': prev_price,
                            'current_price': current_price,
                            'change_pct': price_change_pct
                        })
            
            # Рассчитываем средний индекс (если есть изменения)
            if hotels_with_changes > 0:
                avg_price_change = total_price_change / hotels_with_changes
                
                # Добавляем точку для каждого рана
                trend_index_x_values.append(run_time.strftime('%Y-%m-%d %H:%M'))
                trend_index_y_values.append(avg_price_change)
                trend_index_detailed_data.append({
                    'run_time': run_time.strftime('%Y-%m-%d %H:%M'),
                    'avg_change_pct': avg_price_change,
                    'hotels_with_changes': hotels_with_changes,
                    'total_hotels': len(current_hotel_prices),
                    'price_changes': price_changes
                })
            
            # Обновляем предыдущие цены для следующего рана
            prev_hotel_prices = current_hotel_prices.copy()
        
        print(f"🔍 Отладка индекса тренда: {len(trend_index_x_values)} точек данных")
        if trend_index_x_values:
            print(f"   Последняя точка: {trend_index_y_values[-1]:.2f}%")
    except Exception as e:
        print(f"Ошибка расчета индекса тренда: {e}")
        trend_index_x_values, trend_index_y_values = [], []
        trend_index_detailed_data = []
    
    
    # Получаем актуальные цены по каждому отелю (последнее наблюдение)
    df_sorted_all = df.sort_values(['hotel_name', 'scraped_at_display'])
    latest_rows = []
    for hotel_name, grp in df_sorted_all.groupby('hotel_name'):
        last = grp.iloc[-1]
        latest_rows.append({
            'hotel_name': hotel_name,
            'price': float(last['price']),
            'dates': last.get('dates', None),
            'duration': last.get('duration', None),
            'scraped_at_local': last['scraped_at_local'],
            'offer_url': last.get('offer_url', None),
            'image_url': last.get('image_url', None)
        })
    all_hotels = pd.DataFrame(latest_rows).sort_values('price').reset_index(drop=True)
    
    # Анализ изменений за разные окна времени
    df_sorted = df.sort_values(['hotel_name', 'scraped_at_display'])

    def compute_changes(window_hours: int):
        cutoff = (df['scraped_at_display'].max() or datetime.now()) - timedelta(hours=window_hours)
        changes = []
        deltas_map = {}
        for hotel_name, grp in df_sorted.groupby('hotel_name'):
            grp = grp.sort_values('scraped_at_display')
            latest_row = grp.iloc[-1]
            latest_time = latest_row['scraped_at_display']
            win = grp[grp['scraped_at_display'] >= cutoff]
            if len(win) >= 2:
                baseline_row = win.iloc[0]
            elif len(grp) >= 2:
                baseline_row = grp.iloc[-2]
            else:
                deltas_map[hotel_name] = None
                continue
            latest_price = float(latest_row['price'])
            baseline_price = float(baseline_row['price'])
            if baseline_price == 0:
                deltas_map[hotel_name] = None
                continue
            change = latest_price - baseline_price
            if change == 0:
                deltas_map[hotel_name] = None
                continue
            change_percent = (change / baseline_price) * 100.0
            changes.append({
                'hotel_name': hotel_name,
                'old_price': baseline_price,
                'new_price': latest_price,
                'change': change,
                'change_percent': change_percent,
                'timestamp': str(latest_time)
            })
            deltas_map[hotel_name] = (change, change_percent)
        decreases = sorted([h for h in changes if h['change'] < 0], key=lambda x: x['change'])[:5]
        increases = sorted([h for h in changes if h['change'] > 0], key=lambda x: x['change'], reverse=True)[:5]
        return decreases, increases, deltas_map

    # Для таблицы оставляем 48ч, для блоков добавим 24ч и 7д
    decreases_48h, increases_48h, deltas_by_hotel = compute_changes(48)
    decreases_24h, increases_24h, _ = compute_changes(24)
    decreases_7d, increases_7d, _ = compute_changes(24 * 7)

    # Метки нового минимума/максимума за 7д и 30д
    ref_time = df['scraped_at_display'].max() or datetime.now()
    minmax_labels_by_hotel = {}
    for hotel_name, grp in df_sorted_all.groupby('hotel_name'):
        grp = grp.sort_values('scraped_at_display')
        latest_price = float(grp.iloc[-1]['price'])
        labels = []
        for days in (7, 30):
            cutoff_d = ref_time - timedelta(days=days)
            window = grp[grp['scraped_at_local'] >= cutoff_d]
            if len(window) == 0:
                continue
            win_min = float(window['price'].min())
            win_max = float(window['price'].max())
            if latest_price <= win_min:
                labels.append(f"Новый минимум {days}д")
            if latest_price >= win_max:
                labels.append(f"Новый максимум {days}д")
        minmax_labels_by_hotel[hotel_name] = labels

    # Изменение с начала наблюдений (первое значение -> последнее)
    since_start_delta = {}
    for hotel_name, grp in df_sorted.groupby('hotel_name'):
        grp = grp.sort_values('scraped_at_display')
        first_price = float(grp.iloc[0]['price'])
        last_price = float(grp.iloc[-1]['price'])
        if first_price == 0:
            since_start_delta[hotel_name] = None
            continue
        change_abs = last_price - first_price
        change_pct = (change_abs / first_price) * 100.0
        since_start_delta[hotel_name] = (change_abs, change_pct)
    
    # Загружаем историю алертов (если есть)
    alerts = []
    # Автоматически определяем файл алертов на основе файла данных
    if alerts_file is None:
        if 'egypt' in data_file:
            alerts_file = 'data/egypt_travel_prices_alerts.json'
        elif 'turkey' in data_file:
            alerts_file = 'data/turkey_travel_prices_alerts.json'
        else:
            alerts_file = 'data/travel_prices_alerts.json'
    
    alerts_path = alerts_file
    if os.path.exists(alerts_path):
        try:
            with open(alerts_path, 'r', encoding='utf-8') as f:
                alerts_data = json.load(f)
                # Поддерживаем как старый формат {"alerts": [...]}, так и новый формат [...]
                if isinstance(alerts_data, dict) and 'alerts' in alerts_data:
                    alerts = alerts_data.get('alerts', [])
                elif isinstance(alerts_data, list):
                    alerts = alerts_data
                else:
                    alerts = []
        except Exception:
            alerts = []

    # Сортируем алерты по времени (новые сверху)
    def parse_iso(ts):
        try:
            dt = datetime.fromisoformat(ts)
            # Если datetime naive, делаем его UTC
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            return datetime.min.replace(tzinfo=timezone.utc)

    # Сортируем по времени создания (created_at) если есть, иначе по timestamp
    alerts.sort(key=lambda a: parse_iso(a.get('created_at') or a.get('timestamp') or a.get('time') or ''), reverse=True)

    # Загружаем карту изображений (если есть)
    images_map = {}
    images_path = os.path.join('data', 'hotel_images.json')
    if os.path.exists(images_path):
        try:
            with open(images_path, 'r', encoding='utf-8') as f:
                images_map = json.load(f) or {}
        except Exception:
            images_map = {}

    # Функция для слуг-имени файла по названию отеля
    def slugify(text: str) -> str:
        text = text.lower().strip()
        text = re.sub(r"[^a-z0-9]+", "-", text)
        text = re.sub(r"-+", "-", text).strip('-')
        return text or "hotel"

    # Создаём директорию для страниц графиков
    charts_dir = os.path.join(charts_subdir)
    os.makedirs(charts_dir, exist_ok=True)

    # Генерируем страницу с графиком для каждого отеля
    for hotel_name in sorted(df['hotel_name'].unique()):
        hotel_ts = df[df['hotel_name'] == hotel_name].dropna(subset=['scraped_at_display']).sort_values('scraped_at_display')
        x_values = [pd.to_datetime(t).strftime('%Y-%m-%d %H:%M') for t in hotel_ts['scraped_at_display'].tolist()]
        y_values = [float(p) for p in hotel_ts['price'].tolist()]

        hotel_slug = slugify(hotel_name)
        hotel_html_path = os.path.join(charts_dir, f"{hotel_slug}.html")

        # Определяем корректную ссылку "Назад к дашборду" в зависимости от поддиректории
        if charts_subdir and charts_subdir.rstrip('/').endswith('greece'):
            back_target = 'index_greece.html'
        elif charts_subdir and charts_subdir.rstrip('/').endswith('egypt'):
            back_target = 'index_egypt.html'
        else:
            back_target = 'index.html'
        back_href = os.path.relpath(back_target, start=os.path.dirname(hotel_html_path))

        chart_html = f"""<!DOCTYPE html>
<html lang=\"ru\">
<head>
    <meta charset=\"UTF-8\">
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">
    <title>График цен — {hotel_name}</title>
    <script src=\"https://cdn.plot.ly/plotly-latest.min.js\"></script>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 20px; }}
        .back {{ margin-bottom: 10px; }}
        #chart {{ height: 520px; }}
    </style>
<head>
<body>
    <div class=\"back\"><a href=\"{back_href}\">← Назад к дашборду</a></div>
    <h2>График цен: {hotel_name}</h2>
    <div id=\"chart\"></div>
    <script>
      const x = {json.dumps(x_values, ensure_ascii=False)};
      const y = {json.dumps(y_values, ensure_ascii=False)};
      const trace = {{
        x: x,
        y: y,
        type: 'scatter',
        mode: 'lines+markers',
        line: {{ color: '#2E86AB', width: 3 }},
        marker: {{ size: 8 }}
      }};
      const layout = {{
        title: 'История цен',
        xaxis: {{ title: 'Время' }},
        yaxis: {{ title: 'Цена (PLN)' }}
      }};
      Plotly.newPlot('chart', [trace], layout);
    </script>
  </body>
</html>"""

        with open(hotel_html_path, 'w', encoding='utf-8') as f:
            f.write(chart_html)

    # HTML шаблон
    # Готовим HTML блок изменений, выводим только если есть хотя бы один список
    changes_html = ""
    if decreases_24h or increases_24h:
        changes_html += """
        <div class=\"changes-section\">"""
        if decreases_24h:
            changes_html += """
            <div class=\"changes-block\">
                <h3>📉 Наиболее подешевевшие (24ч)</h3>"""
            for change in decreases_24h:
                changes_html += f"""
                <div class=\"change-item change-decrease\">
                    <div>
                        <div class=\"hotel-name\">{change['hotel_name']}</div>
                        <div class=\"change-percent\">{change['change']:+.0f} PLN ({change['change_percent']:+.1f}%)</div>
                    </div>
                    <div class=\"change-price\">{change['old_price']:.0f} → {change['new_price']:.0f} PLN</div>
                </div>"""
            changes_html += """
            </div>"""
        if increases_24h:
            changes_html += """
            <div class=\"changes-block\">
                <h3>📈 Наиболее подорожавшие (24ч)</h3>"""
            for change in increases_24h:
                changes_html += f"""
                <div class=\"change-item change-increase\">
                    <div>
                        <div class=\"hotel-name\">{change['hotel_name']}</div>
                        <div class=\"change-percent\">{change['change']:+.0f} PLN ({change['change_percent']:+.1f}%)</div>
                    </div>
                    <div class=\"change-price\">{change['old_price']:.0f} → {change['new_price']:.0f} PLN</div>
                </div>"""
            changes_html += """
            </div>"""
        changes_html += """
        </div>"""

    if decreases_7d or increases_7d:
        changes_html += """
        <div class=\"changes-section\">"""
        if decreases_7d:
            changes_html += """
            <div class=\"changes-block\">
                <h3>📉 Наиболее подешевевшие (7д)</h3>"""
            for change in decreases_7d:
                changes_html += f"""
                <div class=\"change-item change-decrease\">
                    <div>
                        <div class=\"hotel-name\">{change['hotel_name']}</div>
                        <div class=\"change-percent\">{change['change']:+.0f} PLN ({change['change_percent']:+.1f}%)</div>
                    </div>
                    <div class=\"change-price\">{change['old_price']:.0f} → {change['new_price']:.0f} PLN</div>
                </div>"""
            changes_html += """
            </div>"""
        if increases_7d:
            changes_html += """
            <div class=\"changes-block\">
                <h3>📈 Наиболее подорожавшие (7д)</h3>"""
            for change in increases_7d:
                changes_html += f"""
                <div class=\"change-item change-increase\">
                    <div>
                        <div class=\"hotel-name\">{change['hotel_name']}</div>
                        <div class=\"change-percent\">{change['change']:+.0f} PLN ({change['change_percent']:+.1f}%)</div>
                    </div>
                    <div class=\"change-price\">{change['old_price']:.0f} → {change['new_price']:.0f} PLN</div>
                </div>"""
            changes_html += """
            </div>"""
        changes_html += """
        </div>"""

    # Время последнего обновления для шапки
    try:
        updated_str = df['scraped_at_display'].max().strftime('%d.%m.%Y %H:%M')
    except Exception:
        updated_str = datetime.now().strftime('%d.%m.%Y %H:%M')

    html_template = f"""<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <script src="https://cdn.plot.ly/plotly-latest.min.js"></script>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800;900&display=swap" rel="stylesheet">
    <title>{title}</title>
    <style>
        :root {{
            /* Цветовая палитра */
            --primary-color: #2563eb;
            --primary-dark: #1d4ed8;
            --secondary-color: #7c3aed;
            --accent-color: #f59e0b;
            --success-color: #10b981;
            --danger-color: #ef4444;
            --warning-color: #f59e0b;
            --info-color: #3b82f6;
            
            /* Градиенты */
            --gradient-primary: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            --gradient-success: linear-gradient(135deg, #4facfe 0%, #00f2fe 100%);
            --gradient-danger: linear-gradient(135deg, #fa709a 0%, #fee140 100%);
            --gradient-card: linear-gradient(145deg, #ffffff 0%, #f8fafc 100%);
            
            /* Тени */
            --shadow-sm: 0 1px 2px 0 rgba(0, 0, 0, 0.05);
            --shadow-md: 0 4px 6px -1px rgba(0, 0, 0, 0.1), 0 2px 4px -1px rgba(0, 0, 0, 0.06);
            --shadow-lg: 0 10px 15px -3px rgba(0, 0, 0, 0.1), 0 4px 6px -2px rgba(0, 0, 0, 0.05);
            --shadow-xl: 0 20px 25px -5px rgba(0, 0, 0, 0.1), 0 10px 10px -5px rgba(0, 0, 0, 0.04);
            
            /* Радиусы */
            --radius-sm: 0.375rem;
            --radius-md: 0.5rem;
            --radius-lg: 0.75rem;
            --radius-xl: 1rem;
            
            /* Переходы */
            --transition-fast: 0.15s ease-in-out;
            --transition-normal: 0.3s ease-in-out;
            --transition-slow: 0.5s ease-in-out;
        }}
        
        * {{
            box-sizing: border-box;
        }}
        
        body {{
            font-family: 'Inter', 'Segoe UI', 'Roboto', 'Helvetica Neue', Arial, sans-serif;
            margin: 0;
            padding: 0;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            line-height: 1.6;
            color: #1f2937;
        }}
        
        .container {{
            max-width: 1400px;
            margin: 0 auto;
            background: rgba(255, 255, 255, 0.95);
            backdrop-filter: blur(10px);
            padding: 2rem;
            border-radius: var(--radius-xl);
            box-shadow: var(--shadow-xl);
            margin-top: 2rem;
            margin-bottom: 2rem;
        }}
        
        .header {{
            text-align: center;
            margin-bottom: 3rem;
            padding: 3rem 2rem;
            background: var(--gradient-primary);
            color: white;
            border-radius: var(--radius-xl);
            position: relative;
            overflow: hidden;
        }}
        
        .header::before {{
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background: url('data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><defs><pattern id="grain" width="100" height="100" patternUnits="userSpaceOnUse"><circle cx="25" cy="25" r="1" fill="white" opacity="0.1"/><circle cx="75" cy="75" r="1" fill="white" opacity="0.1"/><circle cx="50" cy="10" r="0.5" fill="white" opacity="0.1"/><circle cx="10" cy="60" r="0.5" fill="white" opacity="0.1"/><circle cx="90" cy="40" r="0.5" fill="white" opacity="0.1"/></pattern></defs><rect width="100" height="100" fill="url(%23grain)"/></svg>');
            opacity: 0.3;
        }}
        
        .header h1 {{
            font-size: 3rem;
            font-weight: 800;
            margin: 0 0 1rem 0;
            text-shadow: 0 2px 4px rgba(0, 0, 0, 0.1);
            position: relative;
            z-index: 1;
        }}
        
        .header p {{
            font-size: 1.125rem;
            opacity: 0.9;
            margin: 0;
            position: relative;
            z-index: 1;
        }}
        
        /* Темная тема */
        .dark-theme {{
            --bg-primary: #0f172a;
            --bg-secondary: #1e293b;
            --text-primary: #f1f5f9;
            --text-secondary: #cbd5e1;
            --border-color: #334155;
        }}
        
        .dark-theme body {{
            background: linear-gradient(135deg, #1e293b 0%, #0f172a 100%) !important;
            color: #f1f5f9 !important;
        }}
        
        .dark-theme .main-content {{
            background: linear-gradient(135deg, #1e293b 0%, #0f172a 100%) !important;
        }}
        
        .dark-theme .container {{
            background: rgba(30, 41, 59, 0.95);
            color: #f1f5f9;
        }}
        
        .dark-theme .metric {{
            background: linear-gradient(145deg, #1e293b 0%, #334155 100%);
            border: 1px solid #475569;
        }}
        
        .dark-theme .hotels-section {{
            background: linear-gradient(145deg, #1e293b 0%, #334155 100%);
            border: 1px solid #475569;
        }}
        
        .dark-theme .hotels-table th {{
            background: linear-gradient(135deg, #334155 0%, #475569 100%);
            color: #f1f5f9;
            border-bottom: 2px solid #475569;
            border-top: 1px solid #475569;
        }}
        
        .dark-theme .hotels-table th:hover {{
            background: linear-gradient(135deg, #475569 0%, #64748b 100%);
        }}
        
        .dark-theme .hotels-table tbody tr:nth-child(even) {{
            background: #1e293b;
        }}
        
        .dark-theme .hotels-table tbody tr:nth-child(odd) {{
            background: #0f172a;
        }}
        
        .dark-theme .hotels-table tbody tr:hover {{
            background: linear-gradient(90deg, #1e40af 0%, #3b82f6 100%);
        }}
        
        .dark-theme .filter-input,
        .dark-theme .filter-select {{
            background: #1e293b;
            border-color: #475569;
            color: #f1f5f9;
        }}
        
        .dark-theme .filter-input:focus,
        .dark-theme .filter-select:focus {{
            border-color: #3b82f6;
            box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.1);
        }}
        
        .dark-theme .sidebar {{
            background: linear-gradient(145deg, #1e293b 0%, #334155 100%);
        }}
        
        .dark-theme .nav-item {{
            color: #cbd5e1;
        }}
        
        .dark-theme .nav-item:hover {{
            background: #334155;
            color: #3b82f6;
        }}
        
        .dark-theme .nav-item.active {{
            background: linear-gradient(90deg, #1e40af 0%, #3b82f6 100%);
            color: white;
        }}
        
        .dark-theme .avg-top10-section {{
            background: linear-gradient(145deg, #1e293b 0%, #334155 100%);
            border: 1px solid #475569;
        }}
        
        .dark-theme .avg-top10-section h3 {{
            color: #f1f5f9;
        }}
        
        .dark-theme .trend-section {{
            background: linear-gradient(145deg, #1e293b 0%, #334155 100%);
            border: 1px solid #475569;
        }}
        
        .dark-theme .trend-section h3 {{
            color: #f1f5f9;
        }}
        
        .dark-theme .footer {{
            background: #1e293b;
            color: #cbd5e1;
        }}
        
        .dark-theme .pagination button {{
            background: #1e293b;
            border-color: #475569;
            color: #cbd5e1;
        }}
        
        .dark-theme .pagination button:hover:not(:disabled) {{
            background: var(--gradient-primary);
            color: white;
        }}
        
        .dark-theme .pagination button.active {{
            background: var(--gradient-primary);
            color: white;
        }}
        
        .dark-theme .pagination-info {{
            color: #cbd5e1;
        }}
        
        .theme-toggle {{
            position: fixed;
            top: 2rem;
            right: 2rem;
            z-index: 1000;
            background: var(--gradient-primary);
            border: none;
            border-radius: 50%;
            width: 3rem;
            height: 3rem;
            color: white;
            cursor: pointer;
            box-shadow: var(--shadow-lg);
            transition: var(--transition-normal);
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 1.25rem;
        }}
        
        .theme-toggle:hover {{
            transform: scale(1.1);
            box-shadow: var(--shadow-xl);
        }}
        .metrics {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
            gap: 1.5rem;
            margin-bottom: 3rem;
        }}
        
        .metric {{
            background: var(--gradient-card);
            padding: 2rem;
            border-radius: var(--radius-xl);
            text-align: center;
            box-shadow: var(--shadow-md);
            transition: var(--transition-normal);
            border: 1px solid rgba(255, 255, 255, 0.2);
            position: relative;
            overflow: hidden;
        }}
        
        .metric::before {{
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            right: 0;
            height: 4px;
            background: var(--gradient-primary);
        }}
        
        .metric:hover {{
            transform: translateY(-4px);
            box-shadow: var(--shadow-lg);
        }}
        
        .metric-value {{
            font-size: 2.5rem;
            font-weight: 800;
            background: var(--gradient-primary);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
            margin: 0.5rem 0;
        }}
        
        .metric-label {{
            font-size: 0.875rem;
            font-weight: 600;
            color: #6b7280;
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }}
        
        .avg-top10-section {{
            background: var(--gradient-card);
            padding: 2rem;
            border-radius: var(--radius-xl);
            margin-bottom: 3rem;
            box-shadow: var(--shadow-md);
            border: 1px solid rgba(255, 255, 255, 0.2);
        }}
        
        .avg-top10-section h3 {{
            font-size: 1.5rem;
            font-weight: 700;
            margin: 0 0 1.5rem 0;
            color: #1f2937;
            display: flex;
            align-items: center;
            gap: 0.5rem;
        }}
        
        .trend-section {{
            background: var(--gradient-card);
            padding: 2rem;
            border-radius: var(--radius-xl);
            margin-bottom: 3rem;
            box-shadow: var(--shadow-md);
            border: 1px solid rgba(255, 255, 255, 0.2);
        }}
        
        .trend-index-section {{
            background: var(--gradient-card);
            padding: 2rem;
            border-radius: var(--radius-xl);
            margin-bottom: 3rem;
            box-shadow: var(--shadow-md);
            border: 1px solid rgba(255, 255, 255, 0.2);
            border-top: 3px solid #7C3AED;
        }}
        
        .trend-section h3 {{
            font-size: 1.5rem;
            font-weight: 700;
            margin: 0 0 1.5rem 0;
            color: #1f2937;
            display: flex;
            align-items: center;
            gap: 0.5rem;
        }}
        
        .changes-section {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 20px;
            margin-bottom: 30px;
        }}
        .changes-block {{
            background: #f8f9fa;
            padding: 20px;
            border-radius: 8px;
        }}
        .changes-block h3 {{
            margin-top: 0;
            text-align: center;
        }}
        .change-item {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 10px;
            margin: 5px 0;
            background: white;
            border-radius: 5px;
            border-left: 4px solid;
        }}
        .change-decrease {{
            border-left-color: #28a745;
        }}
        .change-increase {{
            border-left-color: #dc3545;
        }}
        .change-price {{
            font-weight: bold;
        }}
        .change-percent {{
            font-size: 0.9em;
            opacity: 0.8;
        }}
        .alerts-section {{
            margin-top: 30px;
        }}
        .alert-item {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 10px;
            margin: 5px 0;
            background: white;
            border-radius: 5px;
            border-left: 4px solid;
        }}
        .alert-decrease {{
            border-left-color: #28a745;
        }}
        .alert-increase {{
            border-left-color: #dc3545;
        }}
        .alert-missing {{
            border-left-color: #6c757d;
        }}
        .alerts-empty {{
            color: #6c757d;
            font-style: italic;
        }}
        .alerts-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            cursor: pointer;
            user-select: none;
        }}
        .alerts-header:hover {{
            background: #f8f9fa;
        }}
        .alerts-content {{
            max-height: 400px;
            overflow-y: auto;
            transition: max-height 0.3s ease;
        }}
        .alerts-content.collapsed {{
            max-height: 0;
            overflow: hidden;
        }}
        .expand-icon {{
            transition: transform 0.3s ease;
        }}
        .expand-icon.collapsed {{
            transform: rotate(-90deg);
        }}
        .delta {{ font-weight: bold; }}
        .delta.up {{ color: #dc3545; }}
        .delta.down {{ color: #28a745; }}
        .delta.flat {{ color: #6c757d; }}
        .hotels-section {{
            margin-top: 3rem;
            background: var(--gradient-card);
            border-radius: var(--radius-xl);
            padding: 2rem;
            box-shadow: var(--shadow-md);
            border: 1px solid rgba(255, 255, 255, 0.2);
        }}
        
        .hotels-section h3 {{
            font-size: 1.5rem;
            font-weight: 700;
            margin: 0 0 1.5rem 0;
            color: #1f2937;
            display: flex;
            align-items: center;
            gap: 0.5rem;
        }}
        
        .table-container {{
            overflow-x: auto;
            border-radius: var(--radius-lg);
            box-shadow: var(--shadow-sm);
            background: white;
        }}
        
        .hotels-table {{
            width: 100%;
            border-collapse: separate;
            border-spacing: 0;
            margin: 0;
            font-size: 0.875rem;
        }}
        
        .hotels-table th {{
            background: linear-gradient(135deg, #f8fafc 0%, #e2e8f0 100%);
            color: #374151;
            font-weight: 600;
            font-size: 0.875rem;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            padding: 1rem 1.25rem;
            text-align: left;
            cursor: pointer;
            user-select: none;
            position: sticky;
            top: 0;
            z-index: 10;
            transition: var(--transition-fast);
            border-bottom: 2px solid #d1d5db;
            border-top: 1px solid #e5e7eb;
        }}
        
        .hotels-table th:hover {{
            background: linear-gradient(135deg, #e2e8f0 0%, #cbd5e1 100%);
            transform: translateY(-1px);
            box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1);
        }}
        
        .hotels-table th.sortable::after {{
            content: ' ↕';
            opacity: 0.7;
            margin-left: 0.5rem;
            font-size: 0.75rem;
        }}
        
        .hotels-table th.sort-asc::after {{
            content: ' ↑';
            opacity: 1;
            color: #fbbf24;
        }}
        
        .hotels-table th.sort-desc::after {{
            content: ' ↓';
            opacity: 1;
            color: #fbbf24;
        }}
        
        .hotels-table td {{
            padding: 1rem 1.25rem;
            border-bottom: 1px solid #f1f5f9;
            transition: var(--transition-fast);
        }}
        
        .hotels-table tbody tr:nth-child(even) {{
            background: #f8fafc;
        }}
        
        .hotels-table tbody tr:nth-child(odd) {{
            background: white;
        }}
        
        .hotels-table tbody tr:hover {{
            background: linear-gradient(90deg, #f0f9ff 0%, #e0f2fe 100%);
            transform: scale(1.01);
            box-shadow: var(--shadow-sm);
        }}
        
        .hotel-name {{
            color: var(--primary-color);
            font-weight: 700;
            font-size: 0.95rem;
        }}
        
        .hotel-name a {{
            color: inherit;
            text-decoration: none;
            transition: var(--transition-fast);
        }}
        
        .hotel-name a:hover {{
            color: var(--primary-dark);
            text-decoration: underline;
        }}
        
        .price {{
            font-weight: 800;
            font-size: 1.1rem;
            color: var(--success-color);
        }}
        
        .delta {{
            font-weight: 700;
            font-size: 0.9rem;
            padding: 0.25rem 0.5rem;
            border-radius: var(--radius-sm);
            display: inline-block;
            min-width: 3rem;
            text-align: center;
        }}
        
        .delta.up {{
            background: var(--gradient-danger);
            color: white;
        }}
        
        .delta.down {{
            background: var(--gradient-success);
            color: white;
        }}
        
        .delta.flat {{
            background: #f1f5f9;
            color: #64748b;
        }}
        
        .offer-link {{
            color: var(--primary-color);
            text-decoration: none;
            font-size: 1.2rem;
            padding: 0.5rem 0.75rem;
            background: var(--gradient-card);
            border-radius: var(--radius-md);
            border: 1px solid #e2e8f0;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            transition: var(--transition-normal);
            box-shadow: var(--shadow-sm);
        }}
        
        .offer-link:hover {{
            background: var(--gradient-primary);
            color: white;
            transform: scale(1.1);
            box-shadow: var(--shadow-md);
            text-decoration: none;
        }}
        
        .offer-link-cell {{
            text-align: center;
            width: 80px;
        }}
        
        /* Пагинация */
        .pagination {{
            display: flex;
            justify-content: center;
            align-items: center;
            gap: 0.5rem;
            margin-top: 2rem;
            padding: 1rem;
        }}
        
        .pagination button {{
            padding: 0.5rem 1rem;
            border: 1px solid #e2e8f0;
            background: white;
            color: #64748b;
            border-radius: var(--radius-md);
            cursor: pointer;
            transition: var(--transition-fast);
            font-weight: 600;
        }}
        
        .pagination button:hover:not(:disabled) {{
            background: var(--gradient-primary);
            color: white;
            border-color: transparent;
        }}
        
        .pagination button:disabled {{
            opacity: 0.5;
            cursor: not-allowed;
        }}
        
        .pagination button.active {{
            background: var(--gradient-primary);
            color: white;
            border-color: transparent;
        }}
        
        .pagination-info {{
            color: #64748b;
            font-size: 0.875rem;
            margin: 0 1rem;
        }}
        
        /* Фильтры */
        .table-filters {{
            display: flex;
            gap: 1rem;
            margin-bottom: 1.5rem;
            flex-wrap: wrap;
            align-items: center;
        }}
        
        .filter-input {{
            padding: 0.75rem 1rem;
            border: 1px solid #e2e8f0;
            border-radius: var(--radius-md);
            background: white;
            font-size: 0.875rem;
            transition: var(--transition-fast);
            min-width: 200px;
        }}
        
        .filter-input:focus {{
            outline: none;
            border-color: var(--primary-color);
            box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.1);
        }}
        
        .filter-select {{
            padding: 0.75rem 1rem;
            border: 1px solid #e2e8f0;
            border-radius: var(--radius-md);
            background: white;
            font-size: 0.875rem;
            cursor: pointer;
            transition: var(--transition-fast);
        }}
        
        .filter-select:focus {{
            outline: none;
            border-color: var(--primary-color);
            box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.1);
        }}
        
        /* Sidebar Navigation */
        .sidebar {{
            position: fixed;
            top: 0;
            left: 0;
            width: 280px;
            height: 100vh;
            background: var(--gradient-card);
            backdrop-filter: blur(10px);
            box-shadow: var(--shadow-xl);
            z-index: 1000;
            transform: translateX(-100%);
            transition: var(--transition-normal);
            overflow-y: auto;
        }}
        
        .sidebar.open {{
            transform: translateX(0);
        }}
        
        .sidebar-header {{
            padding: 4rem 1.5rem 1rem;
            border-bottom: 1px solid #e2e8f0;
            background: var(--gradient-primary);
            color: white;
            margin-top: 2rem;
        }}
        
        .sidebar-header h2 {{
            margin: 0;
            font-size: 1.25rem;
            font-weight: 800;
        }}
        
        .sidebar-nav {{
            padding: 1rem 0;
        }}
        
        .nav-item {{
            display: block;
            padding: 1rem 1.5rem;
            color: #64748b;
            text-decoration: none;
            transition: var(--transition-fast);
            border-left: 3px solid transparent;
            position: relative;
        }}
        
        .nav-item:hover {{
            background: #f8fafc;
            color: var(--primary-color);
            border-left-color: var(--primary-color);
        }}
        
        .nav-item.active {{
            background: linear-gradient(90deg, #f0f9ff 0%, #e0f2fe 100%);
            color: var(--primary-color);
            border-left-color: var(--primary-color);
            font-weight: 700;
        }}
        
        .nav-item .flag {{
            font-size: 1.5rem;
            margin-right: 0.75rem;
            display: inline-block;
            width: 2rem;
            text-align: center;
        }}
        
        .nav-item .country-name {{
            font-weight: 600;
            font-size: 0.95rem;
        }}
        
        .nav-item .country-desc {{
            font-size: 0.75rem;
            opacity: 0.7;
            margin-top: 0.25rem;
        }}
        
        .sidebar-toggle {{
            position: fixed;
            top: 2rem;
            left: 2rem;
            z-index: 1001;
            background: var(--gradient-primary);
            border: none;
            border-radius: var(--radius-md);
            width: 3rem;
            height: 3rem;
            color: white;
            cursor: pointer;
            box-shadow: var(--shadow-lg);
            transition: var(--transition-normal);
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 1.25rem;
        }}
        
        .sidebar-toggle:hover {{
            transform: scale(1.05);
            box-shadow: var(--shadow-xl);
        }}
        
        .sidebar-overlay {{
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background: rgba(0, 0, 0, 0.5);
            z-index: 999;
            opacity: 0;
            visibility: hidden;
            transition: var(--transition-normal);
        }}
        
        .sidebar-overlay.open {{
            opacity: 1;
            visibility: visible;
        }}
        
        .main-content {{
            transition: var(--transition-normal);
            margin-left: 0;
        }}
        
        .main-content.sidebar-open {{
            margin-left: 280px;
        }}
        
        /* Responsive */
        @media (max-width: 768px) {{
            .sidebar {{
                width: 100%;
            }}
            
            .main-content.sidebar-open {{
                margin-left: 0;
            }}
            
            .container {{
                margin: 1rem;
                padding: 1rem;
            }}
            
            .header h1 {{
                font-size: 2rem;
            }}
            
            .metrics {{
                grid-template-columns: 1fr;
            }}
            
            .table-filters {{
                flex-direction: column;
                align-items: stretch;
            }}
            
            .filter-input, .filter-select {{
                min-width: auto;
            }}
        }}
        
        /* Country Flags */
        .country-flag {{
            font-size: 2rem;
            margin-right: 0.5rem;
            display: inline-block;
            vertical-align: middle;
        }}
        
        .header .country-flag {{
            font-size: 3rem;
            margin-right: 1rem;
        }}
        .footer {{
            text-align: center;
            margin-top: 30px;
            padding: 20px;
            background: #f8f9fa;
            border-radius: 8px;
        }}
        /* Hover preview */
        .hover-thumb {{ position: absolute; display: none; width: 240px; height: 160px; background: #fff; border: 1px solid #ddd; box-shadow: 0 2px 8px rgba(0,0,0,.15); border-radius: 6px; padding: 4px; z-index: 9999; }}
        .hover-thumb img {{ width: 100%; height: 100%; object-fit: cover; border-radius: 4px; }}
        
    </style>
</head>
<body>
    <!-- Sidebar Navigation -->
    <div class="sidebar" id="sidebar">
        <div class="sidebar-header">
            <h2>🌍 Travel Monitor</h2>
        </div>
        <nav class="sidebar-nav">
            <a href="index_greece.html" class="nav-item {'active' if 'Греция' in title else ''}">
                <span class="flag">🇬🇷</span>
                <div>
                    <div class="country-name">Греция</div>
                    <div class="country-desc">Солнечные острова</div>
                </div>
            </a>
            <a href="index_egypt.html" class="nav-item {'active' if 'Египет' in title else ''}">
                <span class="flag">🇪🇬</span>
                <div>
                    <div class="country-name">Египет</div>
                    <div class="country-desc">Древние пирамиды</div>
                </div>
            </a>
            <a href="index_turkey.html" class="nav-item {'active' if 'Турция' in title else ''}">
                <span class="flag">🇹🇷</span>
                <div>
                    <div class="country-name">Турция</div>
                    <div class="country-desc">Восточная экзотика</div>
                </div>
            </a>
        </nav>
    </div>
    
    <!-- Sidebar Overlay -->
    <div class="sidebar-overlay" id="sidebarOverlay"></div>
    
    <!-- Main Content -->
    <div class="main-content" id="mainContent">
        <!-- Sidebar Toggle -->
        <button class="sidebar-toggle" id="sidebarToggle">☰</button>
        
        <!-- Theme Toggle -->
        <button class="theme-toggle" id="themeToggle">🌙</button>
        
    <div class="container">
        <div class="header">
                <h1>
                    <span class="country-flag">{'🇬🇷' if 'Греция' in title else '🇪🇬' if 'Египет' in title else '🇹🇷' if 'Турция' in title else '🌍'}</span>
                    {title.replace('🇬🇷 ', '').replace('🇪🇬 ', '').replace('🇹🇷 ', '')}
                </h1>
            <p>Обновлено: {updated_str}</p>
        </div>

        <div class="avg-top10-section">
            <h3>📉 Средняя цена ТОП‑10 дешёвых предложений</h3>
            <div id="avgTop10" style="height:360px;"></div>
        </div>
        
        <div class="trend-index-section">
            <h3>📊 Индекс ценовой динамики</h3>
            <div id="trendIndexChart" style="height:360px;"></div>
        </div>
        
        <div class="metrics">
            <div class="metric">
                <div class="metric-value">{total_offers:,}</div>
                <div>Всего предложений</div>
            </div>
            <div class="metric">
                <div class="metric-value">{unique_hotels}</div>
                <div>Уникальных отелей</div>
            </div>
            <div class="metric">
                <div class="metric-value">{avg_price:.0f} PLN</div>
                <div>Средняя цена</div>
            </div>
            <div class="metric">
                <div class="metric-value">{min_price:.0f} PLN</div>
                <div>Минимальная цена</div>
            </div>
            <div class="metric">
                <div class="metric-value">{max_price:.0f} PLN</div>
                <div>Максимальная цена</div>
            </div>
        </div>
        
        {changes_html}
        
        <div class="alerts-section">
            <div class="alerts-header" onclick="toggleAlerts()">
                <h3>🚨 История алертов</h3>
                <span class="expand-icon" id="alertsExpandIcon">▼</span>
            </div>
            <div class="alerts-content" id="alertsContent">
"""

    if alerts:
        for a in alerts:
            hotel_name = a.get('hotel_name') or a.get('hotel') or 'Unknown'
            alert_type = a.get('alert_type') or a.get('type') or ''
            old_price = a.get('old_price') or a.get('from') or a.get('previous_price')
            new_price = a.get('new_price') if 'new_price' in a else (a.get('to') or a.get('current_price'))
            ts = a.get('timestamp') or a.get('time') or ''

            if alert_type == 'missing' or new_price in (None, '', 'null'):
                direction_class = 'alert-missing'
                arrow = '—'
                change_text = a.get('message') or a.get('note') or 'Отель пропал из выдачи'
                price_text = f"{old_price if old_price is not None else '—'} → —"
                html_template += f"""
                <div class="alert-item {direction_class}">
                    <div>
                        <div class="hotel-name">{hotel_name}</div>
                        <div class="change-percent">{arrow} {change_text} • {ts}</div>
                    </div>
                    <div class="change-price">{price_text}</div>
                </div>
"""
            else:
                # Обычный ценовой алерт (новая структура)
                change_pct = a.get('price_change_pct', 0.0)
                price_change = a.get('price_change', 0.0)
                direction_class = 'alert-increase' if price_change > 0 else ('alert-decrease' if price_change < 0 else '')
                arrow = '↑' if price_change > 0 else ('↓' if price_change < 0 else '→')
                html_template += f"""
                <div class="alert-item {direction_class}">
                    <div>
                        <div class="hotel-name">{hotel_name}</div>
                        <div class="change-percent">{arrow} {change_pct:+.1f}% • {ts}</div>
                    </div>
                    <div class="change-price">{old_price} → {new_price} PLN</div>
                </div>
"""
    else:
        html_template += """
                <div class="alerts-empty">Нет алертов</div>
"""

    html_template += f"""
            </div>
        </div>

        <div class="hotels-section">
            <h3>🏨 Все отели • клик по отелю откроет график на отдельной странице</h3>
            
            <!-- Table Filters -->
            <div class="table-filters">
                <input type="text" class="filter-input" id="searchInput" placeholder="🔍 Поиск по отелям..." />
                <select class="filter-select" id="priceFilter">
                    <option value="">Все цены</option>
                    <option value="0-2000">До 2000 PLN</option>
                    <option value="2000-3000">2000-3000 PLN</option>
                    <option value="3000-4000">3000-4000 PLN</option>
                    <option value="4000+">От 4000 PLN</option>
                </select>
                <select class="filter-select" id="changeFilter">
                    <option value="">Все изменения</option>
                    <option value="decrease">Снижение цен</option>
                    <option value="increase">Рост цен</option>
                    <option value="stable">Стабильные</option>
                </select>
                <button class="filter-button" id="clearFilters" style="padding: 0.75rem 1rem; background: var(--gradient-primary); color: white; border: none; border-radius: var(--radius-md); cursor: pointer; font-weight: 600;">Очистить</button>
            </div>
            
            <div class="table-container">
            <table class="hotels-table" id="hotelsTable">
                <thead>
                    <tr>
                        <th class="sortable" data-sort="hotel">Отель</th>
                        <th class="sortable" data-sort="price">Цена</th>
                        <th class="sortable" data-sort="delta48">Δ 48ч</th>
                        <th class="sortable" data-sort="deltastart">Δ с начала</th>
                        <th class="sortable" data-sort="dates">Даты</th>
                        <th class="sortable" data-sort="duration">Длительность</th>
                        <th>Ссылка</th>
                    </tr>
                </thead>
                <tbody>"""

    # Добавляем строки таблицы
    for i, (_, hotel) in enumerate(all_hotels.iterrows()):
        hotel_name = hotel['hotel_name']
        price = hotel['price']
        dates = hotel['dates'] if pd.notna(hotel['dates']) else '20-09-2025 - 04-10-2025'
        duration = hotel['duration'] if pd.notna(hotel['duration']) else '6-15 дней'
        
        # Δ 48ч
        delta_display = "—"
        delta_class = "delta flat"
        delta_info = deltas_by_hotel.get(hotel_name)
        if delta_info is not None:
            delta_abs, delta_pct = delta_info
            arrow = '↑' if delta_abs > 0 else ('↓' if delta_abs < 0 else '→')
            delta_class = 'delta up' if delta_abs > 0 else ('delta down' if delta_abs < 0 else 'delta flat')
            sign = '+' if delta_abs > 0 else ('' if delta_abs < 0 else '')
            delta_display = f"{arrow} {sign}{delta_pct:.1f}%"

        # Δ с начала наблюдений
        since_display = "—"
        since_info = since_start_delta.get(hotel_name)
        if since_info is not None:
            since_abs, since_pct = since_info
            arrow2 = '↑' if since_abs > 0 else ('↓' if since_abs < 0 else '→')
            sign2 = '+' if since_abs > 0 else ('' if since_abs < 0 else '')
            since_display = f"{arrow2} {sign2}{since_pct:.1f}%"

        hotel_slug = slugify(hotel_name)
        # Строим ссылку на страницу графика, учитывая поддиректорию
        if charts_subdir:
            chart_href = f"{charts_subdir.rstrip('/')}/{hotel_slug}.html"
        else:
            chart_href = f"hotel-charts/{hotel_slug}.html"
        
        # Ссылка на предложение
        offer_url = hotel.get('offer_url', '')
        offer_link_html = ""
        if offer_url and pd.notna(offer_url) and offer_url.strip():
            offer_link_html = f'<a href="{offer_url}" target="_blank" class="offer-link">🔗</a>'
        else:
            offer_link_html = "—"
        
        html_template += f"""
                    <tr>
                        <td class="hotel-name"><a class=\"open-chart-link\" href=\"{chart_href}\" target=\"_blank\" onmouseover=\"_hoverPreview.show(event,'{hotel_name}')\" onmouseout=\"_hoverPreview.hide()\">{hotel_name}</a></td>
                        <td class="price" data-sort-value="{price}">{price:.0f} PLN</td>
                        <td class=\"{delta_class}\" data-sort-value="{delta_info[1] if delta_info else 0}">{delta_display}</td>
                        <td data-sort-value="{since_info[1] if since_info else 0}">{since_display}</td>
                        <td data-sort-value="{dates}">{dates}</td>
                        <td data-sort-value="{duration}">{duration}</td>
                        <td class="offer-link-cell">{offer_link_html}</td>
                    </tr>"""

    # Завершаем таблицу и добавляем секцию для графика
    html_template += f"""
                </tbody>
            </table>
            </div>
            
            <!-- Pagination -->
            <div class="pagination" id="pagination">
                <button id="prevPage" disabled>← Предыдущая</button>
                <div class="pagination-info">
                    Показано <span id="showingFrom">1</span>-<span id="showingTo">50</span> из <span id="totalItems">{len(all_hotels)}</span> отелей
                </div>
                <button id="nextPage">Следующая →</button>
            </div>
        </div>
        
        <div class="footer">
            <p>🤖 Автоматически обновляется каждый час • Powered by GitHub Actions</p>
        </div>
    </div>
    <div id="hoverThumb" class="hover-thumb"><img id="hoverImg" src="" alt="preview"/></div>
"""

    # Вставляем скрипт превью слиянием JSON вне f-строки, чтобы избежать конфликтов с фигурными скобками
    html_template += """
    <script>
      (function(){
        const X = """ + json.dumps(top10_x_values, ensure_ascii=False) + """;
        const Y = """ + json.dumps(top10_y_values, ensure_ascii=False) + """;
        const detailedData = """ + json.dumps(top10_detailed_data, ensure_ascii=False, default=str) + """;
        
        if (Array.isArray(X) && Array.isArray(Y) && X.length > 0 && Y.length > 0 && window.Plotly) {
          // Подготавливаем данные для hover
          const hoverData = detailedData.map(data => data.hover_data || {});
          
          // Создаем простой текст для hover с правильными переносами строк
          const hoverTexts = detailedData.map((data, index) => {
            const hover = data.hover_data || {};
            let text = hover.title || '';
            
            // Добавляем среднюю цену
            if (hover.avg_price) {
              text += '<br><br><b>Средняя цена:</b><br>';
              text += `${Math.round(hover.avg_price)} PLN`;
            }
            
            if (hover.avg_change) {
              text += '<br><br><b>Изменение средней цены:</b><br>';
              text += `${hover.avg_change.arrow} ${hover.avg_change.sign}${Math.round(hover.avg_change.change)} PLN (${hover.avg_change.sign}${hover.avg_change.change_percent.toFixed(1)}%)`;
            }
            
            if (hover.price_changes && hover.price_changes.length > 0) {
              text += '<br><br><b>🏨 Изменения цен:</b><br>';
              hover.price_changes.forEach(change => {
                text += `• ${change.name}<br>  ${Math.round(change.old_price)} → ${Math.round(change.new_price)} PLN<br>  ${change.arrow} ${change.sign}${Math.round(change.change)} PLN (${change.sign}${change.change_percent.toFixed(1)}%)<br>`;
              });
            }
            
            if (hover.new_hotels && hover.new_hotels.length > 0) {
              text += '<br><b>🆕 Новые в ТОП-10:</b><br>';
              hover.new_hotels.forEach(hotel => {
                text += `• ${hotel.name}<br>  Цена: ${Math.round(hotel.price)} PLN (позиция ${hotel.position})<br>`;
              });
            }
            
            if (hover.removed_hotels && hover.removed_hotels.length > 0) {
              text += '<br><b>❌ Покинули ТОП-10:</b><br>';
              hover.removed_hotels.forEach(hotel => {
                text += `• ${hotel.name}<br>  Цена: ${Math.round(hotel.price)} PLN (была позиция ${hotel.position})<br>`;
              });
            }
            
            if (hover.no_changes) {
              text += '<br><br><i>Нет изменений в этом ране</i>';
            }
            
            return text;
          });
          
          const trace = { 
            x: X, 
            y: Y, 
            type: 'scatter', 
            mode: 'lines+markers', 
            line: { color: '#A23B72', width: 3 }, 
            marker: { size: 8 },
            text: hoverTexts,
            hovertemplate: '%{text}<extra></extra>',
            hoverinfo: 'text',
            hoverlabel: {
              bgcolor: 'rgba(248, 249, 250, 0.98)',
              bordercolor: '#A23B72',
              font: {
                family: 'Inter, sans-serif',
                size: 12,
                color: '#333'
              },
              align: 'left',
              namelength: -1
            }
          };
          
          const layout = { 
            margin: { t: 10, r: 10, b: 40, l: 50 }, 
            xaxis: { title: 'Время' }, 
            yaxis: { title: 'Цена (PLN)' },
            hovermode: 'closest'
          };
          
          Plotly.newPlot('avgTop10', [trace], layout);
        }
      })();
      
      // График индекса ценовой динамики
      (function(){
        const trendIndexX = """ + json.dumps(trend_index_x_values, ensure_ascii=False) + """;
        const trendIndexY = """ + json.dumps(trend_index_y_values, ensure_ascii=False) + """;
        const trendIndexDetailedData = """ + json.dumps(trend_index_detailed_data, ensure_ascii=False, default=str) + """;
        
        if (Array.isArray(trendIndexX) && Array.isArray(trendIndexY) && trendIndexX.length > 0 && trendIndexY.length > 0 && window.Plotly) {
          // Создаем hover текст для каждой точки
          const trendIndexHoverTexts = trendIndexDetailedData.map((data, index) => {
            let text = `<b>📊 Индекс ценовой динамики</b><br>`;
            text += `<b>Время:</b> ${data.run_time}<br>`;
            text += `<b>Среднее изменение:</b> ${data.avg_change_pct.toFixed(2)}%<br>`;
            text += `<b>Отелей с изменениями:</b> ${data.hotels_with_changes}/${data.total_hotels}<br><br>`;
            
            if (data.price_changes && data.price_changes.length > 0) {
              text += `<b>Изменения по отелям:</b><br>`;
              data.price_changes.slice(0, 10).forEach(change => {
                const arrow = change.change_pct > 0 ? '↗️' : change.change_pct < 0 ? '↘️' : '➡️';
                const color = change.change_pct > 0 ? '#ef4444' : change.change_pct < 0 ? '#22c55e' : '#6b7280';
                text += `${arrow} <span style="color: ${color}">${change.hotel}: ${change.change_pct.toFixed(1)}%</span><br>`;
              });
              if (data.price_changes.length > 10) {
                text += `... и еще ${data.price_changes.length - 10} отелей`;
              }
            }
            
            return text;
          });
          
          const trendIndexTrace = {
            x: trendIndexX,
            y: trendIndexY,
            type: 'scatter',
            mode: 'lines+markers',
            name: 'Индекс ценовой динамики',
            line: { color: '#7C3AED', width: 3 },
            marker: { size: 6, color: '#7C3AED' },
            text: trendIndexHoverTexts,
            hovertemplate: '%{text}<extra></extra>',
            hoverinfo: 'text',
            hoverlabel: {
              bgcolor: 'rgba(248, 249, 250, 0.98)',
              bordercolor: '#7C3AED',
              font: { size: 12, color: '#333' },
              align: 'left',
              namelength: -1
            }
          };
          
          const trendIndexLayout = {
            title: {
              text: 'Индекс ценовой динамики (%)',
              font: { size: 16, color: '#374151' }
            },
            xaxis: {
              title: 'Время',
              gridcolor: '#e5e7eb',
              showgrid: true
            },
            yaxis: {
              title: 'Изменение цен (%)',
              gridcolor: '#e5e7eb',
              showgrid: true,
              zeroline: true,
              zerolinecolor: '#6b7280',
              zerolinewidth: 2
            },
            plot_bgcolor: 'rgba(0,0,0,0)',
            paper_bgcolor: 'rgba(0,0,0,0)',
            font: { family: 'Inter, sans-serif' },
            margin: { t: 50, b: 50, l: 60, r: 30 }
          };
          
          Plotly.newPlot('trendIndexChart', [trendIndexTrace], trendIndexLayout);
        }
      })();
      (function(){
        const map = """ + json.dumps(images_map, ensure_ascii=False) + """;
        try { Object.assign(map, JSON.parse(localStorage.getItem('hotel_images')||'{}')); } catch(e) {}
        const hover = document.getElementById('hoverThumb');
        const img = document.getElementById('hoverImg');
        function show(e, name){ const url = map[name]; if(!url){ return; } img.src = url; hover.style.display = 'block'; hover.style.left = ((e.pageX||0)+12) + 'px'; hover.style.top = ((e.pageY||0)+12) + 'px'; }
        function move(e){ if(hover.style.display === 'block'){ hover.style.left = ((e.pageX||0)+12) + 'px'; hover.style.top = ((e.pageY||0)+12) + 'px'; } }
        function hide(){ hover.style.display = 'none'; img.src = ''; }
        document.addEventListener('mousemove', move);
        window._hoverPreview = { show, hide };
      })();
      
      function toggleAlerts() {
        const content = document.getElementById('alertsContent');
        const icon = document.getElementById('alertsExpandIcon');
        if (content.classList.contains('collapsed')) {
          content.classList.remove('collapsed');
          icon.classList.remove('collapsed');
        } else {
          content.classList.add('collapsed');
          icon.classList.add('collapsed');
        }
      }
      
      // Таблица сортировки
      let currentSort = { column: null, direction: 'asc' };
      
      function sortTable(column) {
        const table = document.getElementById('hotelsTable');
        const tbody = table.querySelector('tbody');
        const rows = Array.from(tbody.querySelectorAll('tr'));
        
        // Определяем направление сортировки
        if (currentSort.column === column) {
          currentSort.direction = currentSort.direction === 'asc' ? 'desc' : 'asc';
        } else {
          currentSort.direction = 'asc';
        }
        currentSort.column = column;
        
        // Сортируем строки
        rows.sort((a, b) => {
          let aVal, bVal;
          
          if (column === 'hotel') {
            aVal = a.cells[0].textContent.trim();
            bVal = b.cells[0].textContent.trim();
            return currentSort.direction === 'asc' ? aVal.localeCompare(bVal) : bVal.localeCompare(aVal);
          } else {
            aVal = parseFloat(a.cells[getColumnIndex(column)].dataset.sortValue) || 0;
            bVal = parseFloat(b.cells[getColumnIndex(column)].dataset.sortValue) || 0;
            return currentSort.direction === 'asc' ? aVal - bVal : bVal - aVal;
          }
        });
        
        // Обновляем таблицу
        rows.forEach(row => tbody.appendChild(row));
        
        // Обновляем индикаторы сортировки
        updateSortIndicators();
      }
      
      function getColumnIndex(column) {
        const columnMap = { 'hotel': 0, 'price': 1, 'delta48': 2, 'deltastart': 3, 'dates': 4, 'duration': 5, 'offer': 6 };
        return columnMap[column];
      }
      
      function updateSortIndicators() {
        const headers = document.querySelectorAll('#hotelsTable th.sortable');
        headers.forEach(header => {
          header.classList.remove('sort-asc', 'sort-desc');
          if (header.dataset.sort === currentSort.column) {
            header.classList.add(currentSort.direction === 'asc' ? 'sort-asc' : 'sort-desc');
          }
        });
      }
      
      // Добавляем обработчики кликов на заголовки
      document.addEventListener('DOMContentLoaded', function() {
        const headers = document.querySelectorAll('#hotelsTable th.sortable');
        headers.forEach(header => {
          header.addEventListener('click', () => sortTable(header.dataset.sort));
        });
        
        // Sidebar functionality
        const sidebar = document.getElementById('sidebar');
        const sidebarToggle = document.getElementById('sidebarToggle');
        const sidebarOverlay = document.getElementById('sidebarOverlay');
        const mainContent = document.getElementById('mainContent');
        
        function toggleSidebar() {
          sidebar.classList.toggle('open');
          sidebarOverlay.classList.toggle('open');
          mainContent.classList.toggle('sidebar-open');
        }
        
        sidebarToggle.addEventListener('click', toggleSidebar);
        sidebarOverlay.addEventListener('click', toggleSidebar);
        
        // Theme toggle functionality
        const themeToggle = document.getElementById('themeToggle');
        const body = document.body;
        
        // Load saved theme
        const savedTheme = localStorage.getItem('theme') || 'light';
        if (savedTheme === 'dark') {
          body.classList.add('dark-theme');
          themeToggle.textContent = '☀️';
        }
        
        themeToggle.addEventListener('click', function() {
          body.classList.toggle('dark-theme');
          const isDark = body.classList.contains('dark-theme');
          themeToggle.textContent = isDark ? '☀️' : '🌙';
          localStorage.setItem('theme', isDark ? 'dark' : 'light');
        });
        
        // Table filtering and pagination
        const searchInput = document.getElementById('searchInput');
        const priceFilter = document.getElementById('priceFilter');
        const changeFilter = document.getElementById('changeFilter');
        const clearFilters = document.getElementById('clearFilters');
        const table = document.getElementById('hotelsTable');
        const tbody = table.querySelector('tbody');
        const rows = Array.from(tbody.querySelectorAll('tr'));
        const prevPage = document.getElementById('prevPage');
        const nextPage = document.getElementById('nextPage');
        const showingFrom = document.getElementById('showingFrom');
        const showingTo = document.getElementById('showingTo');
        const totalItems = document.getElementById('totalItems');
        
        let currentPage = 1;
        const itemsPerPage = 50;
        let filteredRows = [...rows];
        
        function filterRows() {
          const searchTerm = searchInput.value.toLowerCase();
          const priceRange = priceFilter.value;
          const changeType = changeFilter.value;
          
          filteredRows = rows.filter(row => {
            const hotelName = row.cells[0].textContent.toLowerCase();
            const price = parseFloat(row.cells[1].textContent.replace(/[^0-9.-]/g, ''));
            const delta48 = row.cells[2].textContent.trim();
            
            // Search filter
            if (searchTerm && !hotelName.includes(searchTerm)) {
              return false;
            }
            
            // Price filter
            if (priceRange) {
              if (priceRange === '0-2000' && price > 2000) return false;
              if (priceRange === '2000-3000' && (price < 2000 || price > 3000)) return false;
              if (priceRange === '3000-4000' && (price < 3000 || price > 4000)) return false;
              if (priceRange === '4000+' && price < 4000) return false;
            }
            
            // Change filter
            if (changeType) {
              if (changeType === 'decrease' && !delta48.includes('-')) return false;
              if (changeType === 'increase' && !delta48.includes('+')) return false;
              if (changeType === 'stable' && delta48 !== '—') return false;
            }
            
            return true;
          });
          
          currentPage = 1;
          updateTable();
        }
        
        function updateTable() {
          const startIndex = (currentPage - 1) * itemsPerPage;
          const endIndex = startIndex + itemsPerPage;
          const pageRows = filteredRows.slice(startIndex, endIndex);
          
          // Clear current rows
          tbody.innerHTML = '';
          
          // Add filtered rows
          pageRows.forEach(row => tbody.appendChild(row));
          
          // Update pagination info
          showingFrom.textContent = filteredRows.length > 0 ? startIndex + 1 : 0;
          showingTo.textContent = Math.min(endIndex, filteredRows.length);
          totalItems.textContent = filteredRows.length;
          
          // Update pagination buttons
          prevPage.disabled = currentPage === 1;
          nextPage.disabled = endIndex >= filteredRows.length;
        }
        
        function nextPageFunc() {
          const maxPage = Math.ceil(filteredRows.length / itemsPerPage);
          if (currentPage < maxPage) {
            currentPage++;
            updateTable();
          }
        }
        
        function prevPageFunc() {
          if (currentPage > 1) {
            currentPage--;
            updateTable();
          }
        }
        
        // Event listeners
        searchInput.addEventListener('input', filterRows);
        priceFilter.addEventListener('change', filterRows);
        changeFilter.addEventListener('change', filterRows);
        clearFilters.addEventListener('click', function() {
          searchInput.value = '';
          priceFilter.value = '';
          changeFilter.value = '';
          filterRows();
        });
        nextPage.addEventListener('click', nextPageFunc);
        prevPage.addEventListener('click', prevPageFunc);
        
        // Initialize
        updateTable();
      });
    </script>
  </body>
</html>
"""

    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(html_template)
    
    print(f"✅ Дашборд с встроенными графиками сгенерирован: index.html")
    print(f"📊 Статистика: {total_offers} предложений, {unique_hotels} отелей")
    print(f"💰 Цены: {min_price:.0f} - {max_price:.0f} PLN (средняя: {avg_price:.0f} PLN)")
    print(f"📈 Изменения цен: {len(decreases_48h) + len(increases_48h)} отелей за 48ч")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description='Generate inline charts dashboard')
    parser.add_argument('--data-file', default='data/travel_prices.csv')
    parser.add_argument('--output', default='index.html')
    parser.add_argument('--title', default='Travel Price Monitor • Расширенный дашборд')
    parser.add_argument('--charts-dir', default='hotel-charts')
    parser.add_argument('--tz', default='Europe/Warsaw')
    parser.add_argument('--alerts-file', default=None)
    args = parser.parse_args()
    generate_inline_charts_dashboard(data_file=args.data_file, output_file=args.output, title=args.title, charts_subdir=args.charts_dir, tz=args.tz, alerts_file=args.alerts_file)
