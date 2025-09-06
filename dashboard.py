#!/usr/bin/env python3
"""
Интерактивный дашборд для мониторинга цен на путешествия
Использует Streamlit для создания веб-интерфейса
"""

import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import json
from datetime import datetime, timedelta
import os

# Настройка страницы
st.set_page_config(
    page_title="Travel Price Monitor Dashboard",
    page_icon="✈️",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Загрузка данных
@st.cache_data
def load_data():
    """Загружает данные из CSV файла"""
    try:
        df = pd.read_csv('data/travel_prices.csv')
        df['scraped_at'] = pd.to_datetime(df['scraped_at'])
        return df
    except Exception as e:
        st.error(f"Ошибка загрузки данных: {e}")
        return pd.DataFrame()

@st.cache_data
def load_alerts():
    """Загружает данные об алертах"""
    try:
        if os.path.exists('data/price_alerts_report.txt'):
            with open('data/price_alerts_report.txt', 'r', encoding='utf-8') as f:
                return f.read()
        return "Нет данных об алертах"
    except Exception as e:
        return f"Ошибка загрузки алертов: {e}"

def main():
    st.title("✈️ Travel Price Monitor Dashboard")
    st.markdown("---")
    
    # Загружаем данные
    df = load_data()
    
    if df.empty:
        st.error("❌ Нет данных для отображения")
        return
    
    # Боковая панель с фильтрами
    st.sidebar.header("🔍 Фильтры")
    
    # Фильтр по цене
    price_range = st.sidebar.slider(
        "Диапазон цен (PLN)",
        min_value=int(df['price'].min()),
        max_value=int(df['price'].max()),
        value=(int(df['price'].min()), int(df['price'].max()))
    )
    
    # Фильтр по отелям
    unique_hotels = df['hotel_name'].unique()
    selected_hotels = st.sidebar.multiselect(
        "Выберите отели",
        unique_hotels,
        default=unique_hotels[:10] if len(unique_hotels) > 10 else unique_hotels
    )
    
    # Фильтр по дате
    date_range = st.sidebar.date_input(
        "Диапазон дат",
        value=(df['scraped_at'].min().date(), df['scraped_at'].max().date()),
        min_value=df['scraped_at'].min().date(),
        max_value=df['scraped_at'].max().date()
    )
    
    # Применяем фильтры
    filtered_df = df[
        (df['price'] >= price_range[0]) & 
        (df['price'] <= price_range[1]) &
        (df['hotel_name'].isin(selected_hotels)) &
        (df['scraped_at'].dt.date >= date_range[0]) &
        (df['scraped_at'].dt.date <= date_range[1])
    ]
    
    # Основные метрики
    col1, col2, col3, col4 = st.columns(4)
    
    with col1:
        st.metric(
            "Всего предложений",
            len(filtered_df),
            delta=f"+{len(filtered_df) - len(df)}" if len(filtered_df) != len(df) else None
        )
    
    with col2:
        st.metric(
            "Уникальных отелей",
            filtered_df['hotel_name'].nunique()
        )
    
    with col3:
        st.metric(
            "Средняя цена",
            f"{filtered_df['price'].mean():.0f} PLN"
        )
    
    with col4:
        st.metric(
            "Диапазон цен",
            f"{filtered_df['price'].min():.0f} - {filtered_df['price'].max():.0f} PLN"
        )
    
    st.markdown("---")
    
    # Вкладки
    tab1, tab2, tab3, tab4, tab5 = st.tabs([
        "📊 Общая статистика", 
        "📈 Динамика цен", 
        "🏨 Топ отелей", 
        "🔍 Детальный анализ",
        "🚨 Алерты"
    ])
    
    with tab1:
        st.header("📊 Общая статистика")
        
        col1, col2 = st.columns(2)
        
        with col1:
            # Распределение цен
            fig_hist = px.histogram(
                filtered_df, 
                x='price',
                title='Распределение цен',
                labels={'price': 'Цена (PLN)', 'count': 'Количество'},
                color_discrete_sequence=['#2E86AB']
            )
            fig_hist.update_layout(height=400)
            st.plotly_chart(fig_hist, use_container_width=True)
        
        with col2:
            # Box plot цен
            fig_box = px.box(
                filtered_df,
                y='price',
                title='Распределение цен (Box Plot)',
                labels={'price': 'Цена (PLN)'},
                color_discrete_sequence=['#A23B72']
            )
            fig_box.update_layout(height=400)
            st.plotly_chart(fig_box, use_container_width=True)
    
    with tab2:
        st.header("📈 Динамика цен")
        
        # Группируем по дням
        daily_stats = filtered_df.groupby(filtered_df['scraped_at'].dt.date).agg({
            'price': ['mean', 'min', 'max', 'count']
        }).round(2)
        daily_stats.columns = ['Средняя цена', 'Мин цена', 'Макс цена', 'Количество']
        daily_stats = daily_stats.reset_index()
        
        # График динамики цен
        fig_timeline = go.Figure()
        
        fig_timeline.add_trace(go.Scatter(
            x=daily_stats['scraped_at'],
            y=daily_stats['Средняя цена'],
            mode='lines+markers',
            name='Средняя цена',
            line=dict(color='#2E86AB', width=3)
        ))
        
        fig_timeline.add_trace(go.Scatter(
            x=daily_stats['scraped_at'],
            y=daily_stats['Мин цена'],
            mode='lines',
            name='Минимальная цена',
            line=dict(color='#A23B72', width=2, dash='dash')
        ))
        
        fig_timeline.add_trace(go.Scatter(
            x=daily_stats['scraped_at'],
            y=daily_stats['Макс цена'],
            mode='lines',
            name='Максимальная цена',
            line=dict(color='#F18F01', width=2, dash='dash')
        ))
        
        fig_timeline.update_layout(
            title='Динамика цен по дням',
            xaxis_title='Дата',
            yaxis_title='Цена (PLN)',
            height=500,
            hovermode='x unified'
        )
        
        st.plotly_chart(fig_timeline, use_container_width=True)
    
    with tab3:
        st.header("🏨 Топ отелей")
        
        col1, col2 = st.columns(2)
        
        with col1:
            # Топ-10 самых дешевых отелей
            top_cheap = filtered_df.nsmallest(10, 'price')
            
            fig_cheap = px.bar(
                top_cheap,
                x='price',
                y='hotel_name',
                orientation='h',
                title='Топ-10 самых дешевых отелей',
                labels={'price': 'Цена (PLN)', 'hotel_name': 'Отель'},
                color='price',
                color_continuous_scale='Viridis'
            )
            fig_cheap.update_layout(height=500)
            st.plotly_chart(fig_cheap, use_container_width=True)
        
        with col2:
            # Топ-10 отелей по количеству предложений
            hotel_counts = filtered_df['hotel_name'].value_counts().head(10)
            
            fig_counts = px.bar(
                x=hotel_counts.values,
                y=hotel_counts.index,
                orientation='h',
                title='Топ-10 отелей по количеству предложений',
                labels={'x': 'Количество предложений', 'y': 'Отель'},
                color=hotel_counts.values,
                color_continuous_scale='Plasma'
            )
            fig_counts.update_layout(height=500)
            st.plotly_chart(fig_counts, use_container_width=True)
    
    with tab4:
        st.header("🔍 Детальный анализ")
        
        # Таблица с данными
        st.subheader("Таблица данных")
        
        # Добавляем фильтры для таблицы
        col1, col2 = st.columns(2)
        with col1:
            sort_by = st.selectbox("Сортировать по", ['price', 'hotel_name', 'scraped_at'])
        with col2:
            sort_order = st.selectbox("Порядок", ['По возрастанию', 'По убыванию'])
        
        ascending = sort_order == 'По возрастанию'
        sorted_df = filtered_df.sort_values(sort_by, ascending=ascending)
        
        st.dataframe(
            sorted_df[['hotel_name', 'price', 'scraped_at']].head(50),
            use_container_width=True,
            height=400
        )
        
        # Статистика по отелям
        st.subheader("Статистика по отелям")
        hotel_stats = filtered_df.groupby('hotel_name')['price'].agg([
            'count', 'mean', 'min', 'max', 'std'
        ]).round(2)
        hotel_stats.columns = ['Количество', 'Средняя цена', 'Мин цена', 'Макс цена', 'Стд отклонение']
        hotel_stats = hotel_stats.sort_values('Средняя цена')
        
        st.dataframe(hotel_stats.head(20), use_container_width=True)
    
    with tab5:
        st.header("🚨 Алерты и уведомления")
        
        # Загружаем алерты
        alerts_text = load_alerts()
        st.text_area("Отчет об алертах", alerts_text, height=300)
        
        # Анализ изменений цен
        st.subheader("Анализ изменений цен")
        
        if len(filtered_df) > 1:
            # Группируем по отелям и анализируем изменения
            hotel_changes = []
            for hotel in filtered_df['hotel_name'].unique():
                hotel_data = filtered_df[filtered_df['hotel_name'] == hotel].sort_values('scraped_at')
                if len(hotel_data) > 1:
                    first_price = hotel_data.iloc[0]['price']
                    last_price = hotel_data.iloc[-1]['price']
                    change = last_price - first_price
                    change_pct = (change / first_price) * 100 if first_price > 0 else 0
                    
                    hotel_changes.append({
                        'Отель': hotel,
                        'Первая цена': first_price,
                        'Последняя цена': last_price,
                        'Изменение (PLN)': change,
                        'Изменение (%)': change_pct,
                        'Записей': len(hotel_data)
                    })
            
            if hotel_changes:
                changes_df = pd.DataFrame(hotel_changes)
                changes_df = changes_df.sort_values('Изменение (PLN)')
                
                # Цветовое кодирование
                def color_change(val):
                    if val > 0:
                        return 'background-color: #ffcccc'  # Красный для повышения
                    elif val < 0:
                        return 'background-color: #ccffcc'  # Зеленый для снижения
                    else:
                        return ''
                
                styled_df = changes_df.style.applymap(
                    color_change, 
                    subset=['Изменение (PLN)', 'Изменение (%)']
                )
                
                st.dataframe(styled_df, use_container_width=True)
            else:
                st.info("Недостаточно данных для анализа изменений цен")
        else:
            st.info("Недостаточно данных для анализа изменений цен")
    
    # Футер
    st.markdown("---")
    st.markdown(
        f"<div style='text-align: center; color: #666;'>"
        f"Последнее обновление: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | "
        f"Всего записей: {len(df)} | "
        f"Уникальных отелей: {df['hotel_name'].nunique()}"
        f"</div>",
        unsafe_allow_html=True
    )

if __name__ == "__main__":
    main()
