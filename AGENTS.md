# 🤖 AGENTS.md — Инструкции и правила для AI-агентов

Этот документ содержит обязательные правила, ограничения и рекомендации для всех AI-агентов, работающих в репозитории `travel-price-monitor`.

---

## 🚨 1. Git Safety: Главные правила коммитов

### ❌ Что категорически запрещено:
1. **НИКОГДА не коммитить и не пушить данные (`data/`, `*.csv`, `price_alerts_history.json`) вручную.**
   - Все файлы данных и история цен собираются, обрабатываются и коммитятся **автоматически на GitHub Actions CI**.
   - Ручной коммит локальных CSV файлов может перезатереть или повредить накопленную месяцами историю.
2. **НИКОГДА не коммитить сгенерированные HTML-файлы (`index*.html`, `site/`).**
   - Все HTML-страницы и артефакты генерируются из данных на CI перед деплоем на GitHub Pages.
   - Коммитить нужно **только исходный код** (Python-скрипты, конфиги, шаблоны, стили).
3. **НИКОГДА не делать `git push origin main --force` или `git push --force-with-lease` на `main`.**
   - Прямой force push в `main` уничтожает историю коммитов автоматических скрапов.

### ✅ Правильный рабочий процесс:
1. Работать только в feature-ветке (например, `feature/fix-...` или `feature/debug-ci-monitoring`).
2. Добавлять в коммит только файлы кода:
   ```bash
   git add generate_inline_charts_dashboard.py hotel_deal_score.py ...
   git commit -m "fix(ui): description"
   ```
3. Сбрасывать локальные изменения в `data/` и `.html` перед rebase/push:
   ```bash
   git checkout -- data/ *.html
   git pull --rebase origin <current-branch>
   git push origin <current-branch>
   ```
4. **Автономность запуска команд:** Пользователь разрешил запуск `python3`, `pytest`, `git` и любых верификационных скриптов без дополнительного запроса подтверждения.

---

## 🛠️ 2. Локальное тестирование и верификация

### Запуск локального сервера для проверки:
```bash
python3 -m http.server 8080
```
- Главная страница: `http://localhost:8080/index.html`
- Дашборды фильтров: `http://localhost:8080/index_filter_*.html`

### Проверка интерактивности и консольных ошибок:
При внесении изменений в дашборды (`generate_inline_charts_dashboard.py` или `duration_view_bundle.py`) обязательно проверять страницу через headless Chromium (Playwright):
```bash
source .venv/bin/activate
python -c "
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page()
    page.on('pageerror', lambda err: print('PAGE ERROR:', err))
    page.goto('http://localhost:8080/index_filter_turkey_7_10_days.html', wait_until='networkidle')
    page.click('#sidebarToggle')
    page.click('.mode-btn[data-mode=\"table\"]')
    page.click('#analyticsFold summary')
    browser.close()
"
```
**Критерий успешности:** 0 ошибок `PAGE ERROR` в консоли браузера.

---

## 💻 3. Стандарты веб-разработки и UI дашбордов

1. **Plotly CDN:** Использовать только актуальные версии `https://cdn.plot.ly/plotly-2.35.2.min.js` (устаревший `plotly-latest.min.js` вызывает Stack Overflow при 800+ точках).
2. **Проверка DOM-элементов перед вызовом Plotly:**
   ```javascript
   if (document.getElementById('chartId') && window.Plotly) {
     Plotly.newPlot('chartId', ...);
   }
   ```
   *Невыполнение этой проверки приводит к падению всего JS на странице.*
3. **Обработка событий `<details>` (аккордеоны):**
   При открытии вкладок с графиками (`<details>`) Plotly требует вызова `Plotly.Plots.resize`. Использовать многофазный ресайз с задержками (`40ms`, `150ms`, `350ms`), чтобы компенсировать CSS-анимации.
4. **Обработчики кликов:**
   Использовать прямое назначение свойств `.onclick = function(e) { e.preventDefault(); e.stopPropagation(); ... }` на кнопках тулбаров и сайдбаров во избежание потери контекста событий.

---

## 📋 4. Полезные команды Runbook

| Задача | Команда |
| :--- | :--- |
| **Тестовый запуск монитора (1 фильтр)** | `python travel_monitor.py --config config_ci_filter_turkey_7_10.json` |
| **Параллельный запуск мониторинга** | `python scripts/run_monitors_parallel.py -j 3` |
| **Генерация 1 дашборда** | `python generate_inline_charts_dashboard.py --data-file data/filters/filter_turkey_7_10_days/travel_prices.csv --output index_filter_turkey_7_10_days.html` |
| **Генерация всех дашбордов** | `python scripts/run_dashboards_parallel.py -j 3` |
| **Генерация лэндинга** | `python generate_landing.py` |
| **Сборка сайта для Pages** | `bash scripts/prepare_pages_site.sh` |
