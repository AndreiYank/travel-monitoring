import datetime

from filter_registry import active_filter_groups


def generate_help_page(output_file='help.html'):
    """Генерирует страницу справки и глоссария (FAQ)."""
    html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Справка и Глоссарий • Travel Price Monitor</title>
  <link rel="icon" href="favicon.svg" type="image/svg+xml">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
  <style>
    :root {{
      --bg: #f8fafc;
      --ink: #0f172a;
      --muted: #475467;
      --line: #e2e8f0;
      --brand: #4f46e5;
      --radius: 16px;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      font-family: 'Inter', sans-serif;
      background: var(--bg);
      margin: 0;
      color: var(--ink);
      line-height: 1.6;
    }}
    .container {{
      max-width: 900px;
      margin: 0 auto;
      padding: 24px 16px 48px;
    }}
    .topbar {{
      margin-bottom: 20px;
    }}
    .back-link {{
      color: var(--brand);
      text-decoration: none;
      font-weight: 700;
      font-size: .95rem;
    }}
    .back-link:hover {{ text-decoration: underline; }}
    .header {{
      background: linear-gradient(135deg, #4f46e5, #0ea5e9);
      color: #fff;
      padding: 32px 28px;
      border-radius: var(--radius);
      margin-bottom: 28px;
      box-shadow: 0 12px 36px rgba(15,23,42,.1);
    }}
    .header h1 {{ margin: 0 0 8px; font-size: 2rem; font-weight: 800; }}
    .header p {{ margin: 0; opacity: .94; font-size: 1.05rem; }}
    
    .faq-card {{
      background: #fff;
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 24px;
      margin-bottom: 18px;
      box-shadow: 0 4px 16px rgba(15,23,42,.04);
    }}
    .faq-card h2 {{
      margin: 0 0 12px;
      font-size: 1.2rem;
      font-weight: 700;
      color: #1e293b;
      display: flex;
      align-items: center;
      gap: 10px;
    }}
    .badge {{
      display: inline-block;
      padding: 2px 8px;
      border-radius: 999px;
      font-size: .78rem;
      font-weight: 700;
    }}
    .badge-hot {{ background: rgba(245,158,11,.18); color: #92400e; }}
    .badge-good {{ background: rgba(16,185,129,.18); color: #065f46; }}
    .badge-normal {{ background: rgba(148,163,184,.18); color: #334155; }}
    .badge-bad {{ background: rgba(239,68,68,.15); color: #991b1b; }}
    
    ul {{ margin: 8px 0 0; padding-left: 20px; }}
    li {{ margin-bottom: 6px; }}
    .footer {{
      text-align: center;
      margin-top: 36px;
      color: var(--muted);
      font-size: .9rem;
    }}
  </style>
  <!-- Cloudflare Web Analytics --><script defer src='https://static.cloudflareinsights.com/beacon.min.js' data-cf-beacon='{{"token": "1b9c3c0ee6164106a1cb5eda9e61a045"}}'></script><!-- End Cloudflare Web Analytics -->
</head>
<body>
  <div class="container">
    <div class="topbar">
      <a href="index.html" class="back-link">← Назад на главную</a>
    </div>
    
    <div class="header">
      <h1>📚 Справка и Глоссарий</h1>
      <p>Всё о том, как читать показатели, оценивать выгоду и пользоваться мониторингом ценовой динамики.</p>
    </div>

    <div class="faq-card">
      <h2>🎯 Что такое Deal Score и как он рассчитывается?</h2>
      <p><strong>Deal Score (от 0 до 100)</strong> — интегральная оценка выгодности цены отеля в текущий момент:</p>
      <ul>
        <li><span class="badge badge-hot">🔥 Hot (80–100)</span> — супер-выгода! Цена находится на уровне исторического минимума отеля.</li>
        <li><span class="badge badge-good">✅ Good (65–79)</span> — удачный момент для покупки, цена заметно ниже обычной.</li>
        <li><span class="badge badge-normal">↔️ Normal (45–64)</span> — стандартная рыночная стоимость без особых скидок.</li>
        <li><span class="badge badge-bad">📈 Bad (<45)</span> — отель подорожал за последние 48 часов или стоит выше средней цены.</li>
      </ul>
      <p style="margin-top:10px; font-size:.9rem; color:var(--muted);">Метрика автоматически учитывает рейтинг на TripAdvisor и стабильность истории замеров (Low / Medium / High confidence).</p>
    </div>

    <div class="faq-card">
      <h2>📊 Что означают Δ 48ч и Δ к средней?</h2>
      <ul>
        <li><strong>Δ 48ч (Динамика за 48 часов):</strong> показывает, как изменилась стоимость отеля за последние двое суток. Зелёная стрелка `↓ –350 PLN` означает, что отель подешевел; красная `↑ +200 PLN` — подорожал.</li>
        <li><strong>Δ к средней:</strong> сравнение текущей стоимости со средней (медианной) исторической ценой этого отеля за всё время наблюдений. `–12%` означает, что отель на 12% дешевле нормы.</li>
      </ul>
    </div>

    <div class="faq-card">
      <h2>✈️ Коды аэропортов вылета из Польши</h2>
      <ul>
        <li><strong>WAW:</strong> Варшава — Аэропорт им. Фредерика Шопена (Warszawa Okęcie)</li>
        <li><strong>WMI:</strong> Варшава — Модлин (Warszawa Modlin)</li>
        <li><strong>RDO:</strong> Радом (Warszawa Radom)</li>
        <li><strong>POZ:</strong> Познань (Poznań Ławica)</li>
        <li><strong>KTW:</strong> Катовице (Katowice Pyrzowice)</li>
      </ul>
    </div>

    <div class="faq-card">
      <h2>⏰ Как часто обновляются данные?</h2>
      <p>Робот запускается в начале каждого часа 24/7. Он запрашивает актуальную стоимость туров на fly.pl и обновляет графики. Индикатор в шапке покажет 🟢 <code>Обновлено N мин назад</code>.</p>
    </div>

    <div class="footer">
      <a href="index.html" class="back-link">Вернуться на главную страницу</a>
    </div>
  </div>
</body>
</html>
"""
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(html)


def generate_landing(tiles=None, output_file='index.html'):
    now = datetime.datetime.now().strftime('%d.%m.%Y %H:%M')
    if tiles is None:
        tiles = active_filter_groups()

    cards_count = sum(len(g['filters']) for g in tiles)
    sections_html = []
    
    # Готовим вкладки категорий (Все, Египет, Турция, Греция)
    categories = [('all', '🌟 Все направления')]
    for g in tiles:
        cat_id = g.get('id') or g['label'].lower()
        categories.append((cat_id, f"{g.get('icon', '')} {g['label']}"))

    tabs_html = "".join([
        f'<button type="button" class="tab-btn{ " active" if cid=="all" else "" }" data-target="{cid}">{label}</button>'
        for cid, label in categories
    ])

    for group in tiles:
        cat_id = group.get('id') or group['label'].lower()
        cards_html = "".join([
            f"""
        <a class="card" href="{flt['href']}">
          <div class="card-body">
            <div class="card-title-row">
              <span class="card-title">{flt['title']}</span>
              <span class="card-arrow">→</span>
            </div>
            <div class="card-sub">{flt.get('subtitle', '')}</div>
            <div class="card-footer">
              <span class="card-tag">📡 Hourly monitoring</span>
              <span class="card-action">Открыть дашборд</span>
            </div>
          </div>
        </a>
            """ for flt in group['filters']
        ])
        sections_html.append(f"""
    <section class="filter-section" data-category="{cat_id}">
      <div class="section-head">
        <span class="section-icon">{group.get('icon', '')}</span>
        <h2 class="section-title">{group['label']}</h2>
      </div>
      <div class="grid">
        {cards_html}
      </div>
    </section>
        """)

    html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Travel Price Monitor • Мониторинг цен на туры из Польши</title>
  <link rel="icon" href="favicon.svg" type="image/svg+xml">
  <link rel="apple-touch-icon" href="favicon.svg">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
  <style>
    :root {{
      --bg: #f8fafc;
      --ink: #0f172a;
      --muted: #475467;
      --line: #e2e8f0;
      --glass: rgba(255,255,255,.85);
      --shadow: 0 16px 48px rgba(15,23,42,.08);
      --brand: #4f46e5;
      --brand-2: #0ea5e9;
      --radius: 18px;
    }}
    [data-theme="dark"] {{
      --bg: #0b0f19;
      --ink: #f8fafc;
      --muted: #94a3b8;
      --line: #1e293b;
      --glass: rgba(15,23,42,.85);
      --shadow: 0 16px 48px rgba(0,0,0,.4);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      font-family: 'Inter', sans-serif;
      background:
        radial-gradient(1200px 500px at -10% -20%, rgba(79,70,229,.15), transparent 60%),
        radial-gradient(900px 500px at 120% 0%, rgba(14,165,233,.15), transparent 55%),
        var(--bg);
      margin: 0;
      color: var(--ink);
      min-height: 100vh;
      transition: background-color .2s ease, color .2s ease;
    }}
    .container {{
      max-width: 1120px;
      margin: 0 auto;
      padding: 24px;
    }}
    .header {{
      background: linear-gradient(135deg, rgba(79,70,229,.95), rgba(14,165,233,.92));
      color: #fff;
      padding: 36px 32px;
      border-radius: calc(var(--radius) + 6px);
      box-shadow: var(--shadow);
      position: relative;
      overflow: hidden;
    }}
    .header::after {{
      content: "";
      position: absolute;
      inset: 0;
      background: linear-gradient(120deg, rgba(255,255,255,.18), transparent 35%);
      pointer-events: none;
    }}
    .header-top {{
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 1rem;
    }}
    .header h1 {{ margin: 0 0 10px; font-size: clamp(1.6rem, 3vw, 2.2rem); font-weight: 800; }}
    .help-btn {{
      background: rgba(255,255,255,.2);
      border: 1px solid rgba(255,255,255,.35);
      color: #fff;
      padding: 6px 14px;
      border-radius: 999px;
      font-size: .88rem;
      font-weight: 600;
      text-decoration: none;
      transition: background .15s;
      white-space: nowrap;
    }}
    .help-btn:hover {{ background: rgba(255,255,255,.32); }}

    .subtitle {{ font-size: 1.05rem; opacity: .95; line-height: 1.45; max-width: 720px; }}
    
    .onboarding-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 12px;
      margin-top: 20px;
      position: relative;
      z-index: 2;
    }}
    .onboarding-card {{
      background: rgba(255,255,255,.15);
      backdrop-filter: blur(8px);
      border: 1px solid rgba(255,255,255,.25);
      border-radius: 14px;
      padding: 14px 16px;
      font-size: .88rem;
    }}
    .onboarding-card-title {{
      font-weight: 700;
      margin-bottom: 4px;
      display: flex;
      align-items: center;
      gap: 6px;
    }}
    .onboarding-card-desc {{
      opacity: .9;
      font-size: .82rem;
      line-height: 1.35;
    }}

    .meta {{
      margin-top: 20px;
      font-size: .88rem;
      opacity: .95;
      padding: 8px 14px;
      background: rgba(255,255,255,.18);
      border: 1px solid rgba(255,255,255,.28);
      border-radius: 999px;
      display: inline-flex;
      gap: 10px;
      align-items: center;
      flex-wrap: wrap;
    }}

    /* Category Tabs */
    .tabs-bar {{
      display: flex;
      gap: 8px;
      margin-top: 24px;
      overflow-x: auto;
      padding-bottom: 4px;
    }}
    .tab-btn {{
      background: var(--glass);
      border: 1px solid var(--line);
      color: var(--muted);
      padding: 10px 18px;
      border-radius: 999px;
      font-size: .9rem;
      font-weight: 600;
      cursor: pointer;
      white-space: nowrap;
      transition: all .15s ease;
    }}
    .tab-btn:hover {{
      color: var(--ink);
      border-color: #cbd5e1;
    }}
    .tab-btn.active {{
      background: var(--brand);
      color: #fff;
      border-color: var(--brand);
      box-shadow: 0 4px 14px rgba(79,70,229,.25);
    }}

    .filter-section {{
      margin-top: 24px;
    }}
    .section-head {{
      display: flex;
      align-items: center;
      gap: 10px;
      margin-bottom: 14px;
      padding: 0 2px;
    }}
    .section-icon {{
      font-size: 1.35rem;
      line-height: 1;
    }}
    .section-title {{
      margin: 0;
      font-size: 1.2rem;
      font-weight: 800;
      letter-spacing: .01em;
      color: #1e293b;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(12, minmax(0, 1fr));
      gap: 16px;
    }}
    .card {{
      grid-column: span 6;
      background: var(--glass);
      backdrop-filter: blur(14px);
      border: 1px solid rgba(255,255,255,.9);
      border-radius: var(--radius);
      text-decoration: none;
      color: inherit;
      box-shadow: 0 8px 26px rgba(16,24,40,.06);
      transition: transform .18s ease, box-shadow .18s ease, border-color .18s ease;
    }}
    .card:hover {{
      transform: translateY(-3px);
      border-color: rgba(79,70,229,.4);
      box-shadow: 0 14px 34px rgba(16,24,40,.12);
    }}
    .card-body {{ padding: 22px; }}
    .card-title-row {{
      display: flex;
      justify-content: space-between;
      align-items: center;
    }}
    .card-title {{
      font-size: 1.12rem;
      font-weight: 700;
      color: #1d4ed8;
      letter-spacing: .01em;
    }}
    .card-arrow {{
      font-size: 1.1rem;
      color: #4338ca;
      font-weight: 700;
    }}
    .card-sub {{
      margin-top: 8px;
      color: var(--muted);
      font-size: .93rem;
      line-height: 1.4;
    }}
    .card-footer {{
      margin-top: 16px;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }}
    .card-tag {{
      font-size: .78rem;
      color: var(--muted);
      background: #f1f5f9;
      padding: 3px 8px;
      border-radius: 6px;
      font-weight: 500;
    }}
    .card-action {{
      font-size: .88rem;
      font-weight: 700;
      color: #4338ca;
    }}
    .footer {{
      text-align: center;
      color: #64748b;
      margin-top: 36px;
      font-size: .88rem;
    }}
    .footer a {{ color: var(--brand); text-decoration: none; font-weight: 600; }}
    .footer a:hover {{ text-decoration: underline; }}
    @media (max-width: 768px) {{
      .card {{ grid-column: span 12; }}
    }}
    @media (max-width: 640px) {{
      .container {{ padding: 14px; }}
      .header {{ padding: 22px 18px; border-radius: 18px; }}
      .header h1 {{ font-size: 1.45rem; }}
      .subtitle {{ font-size: .9rem; line-height: 1.45; }}
      .meta {{
        width: 100%;
        justify-content: center;
        border-radius: 12px;
        flex-wrap: wrap;
        gap: .35rem .5rem;
        padding: 10px 12px;
      }}
      .card-body {{ padding: 16px; }}
      .card-title {{ font-size: 1.05rem; }}
    }}
  </style>
  <!-- Cloudflare Web Analytics --><script defer src='https://static.cloudflareinsights.com/beacon.min.js' data-cf-beacon='{{"token": "1b9c3c0ee6164106a1cb5eda9e61a045"}}'></script><!-- End Cloudflare Web Analytics -->
</head>
<body>
  <div class="container">
    <div class="header">
      <div class="header-top">
        <h1>🌊 Travel Price Monitor</h1>
        <div style="display:flex;gap:8px;align-items:center;">
          <button type="button" class="help-btn" id="themeToggleBtn" onclick="toggleTheme()" title="Переключить тему">🌙 Тема</button>
          <a href="help.html" class="help-btn">📚 Справка & FAQ</a>
        </div>
      </div>
      <div class="subtitle">Автоматический мониторинг цен на туры из Польши. Сканируем миллионы предложений каждый час и находим лучшие моменты для покупки.</div>
      
      <div class="onboarding-grid">
        <div class="onboarding-card">
          <div class="onboarding-card-title">📡 Почасовой сбор</div>
          <div class="onboarding-card-desc">Отслеживаем цены отелей в реальном времени с fly.pl</div>
        </div>
        <div class="onboarding-card">
          <div class="onboarding-card-title">📊 История & Тренды</div>
          <div class="onboarding-card-desc">Строим графики ценовых колебаний за всё время наблюдений</div>
        </div>
        <div class="onboarding-card">
          <div class="onboarding-card-title">🎯 Deal Score 80+</div>
          <div class="onboarding-card-desc">Алгоритм подсвечивает отели, которые подешевели ниже обычного</div>
        </div>
      </div>

      <div class="meta">
        <span>🎯 Активных фильтров: {cards_count}</span>
        <span>•</span>
        <span>🟢 Обновляется каждый час</span>
        <span>•</span>
        <span>Обновлено: {now}</span>
      </div>
    </div>

    <!-- Category Tabs Filter -->
    <div class="tabs-bar">
      {tabs_html}
    </div>

    {''.join(sections_html)}

    <div class="footer">
      🤖 Автоматический мониторинг • Powered by GitHub Actions • <a href="help.html">Справка и глоссарий</a>
    </div>
  </div>

  <script>
    function applyTheme(theme) {{
      document.documentElement.setAttribute('data-theme', theme);
      const btn = document.getElementById('themeToggleBtn');
      if (btn) btn.textContent = theme === 'dark' ? '☀️ Светлая' : '🌙 Тёмная';
    }}
    function toggleTheme() {{
      const current = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
      try {{ localStorage.setItem('theme', current); }} catch(e) {{}}
      applyTheme(current);
    }}
    (function() {{
      let saved = 'light';
      try {{ saved = localStorage.getItem('theme') || (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'); }} catch(e) {{}}
      applyTheme(saved);
    }})();

    document.querySelectorAll('.tab-btn').forEach(btn => {{
      btn.addEventListener('click', function() {{
        document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
        this.classList.add('active');
        const target = this.getAttribute('data-target');
        document.querySelectorAll('.filter-section').forEach(sec => {{
          if (target === 'all' || sec.getAttribute('data-category') === target) {{
            sec.style.display = 'block';
          }} else {{
            sec.style.display = 'none';
          }}
        }});
      }});
    }});
  </script>
</body>
</html>
"""
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(html)
    generate_help_page('help.html')


if __name__ == '__main__':
    generate_landing(None, 'index.html')

