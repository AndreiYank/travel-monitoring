#!/usr/bin/env python3
import datetime

def generate_landing(tiles, output_file='index.html'):
    now = datetime.datetime.now().strftime('%d.%m.%Y %H:%M')
    cards_html = "".join([
        f"""
        <a class=\"card\" href=\"{t['href']}\">\n  <div class=\"card-body\">\n    <div class=\"card-title\">{t['title']}</div>\n    <div class=\"card-sub\">{t.get('subtitle','')}</div>\n  </div>\n</a>
        """ for t in tiles
    ])
    html = f"""
<!DOCTYPE html>
<html lang=\"ru\">
<head>
  <meta charset=\"UTF-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\" />
  <title>Travel Price Monitor • Выбор направления</title>
  <style>
    body {{ font-family: Arial, sans-serif; background: #f5f5f5; margin: 0; padding: 0; }}
    .container {{ max-width: 1100px; margin: 0 auto; padding: 24px; }}
    .header {{ background: linear-gradient(135deg, #2E86AB, #A23B72); color: #fff; padding: 24px; border-radius: 10px; }}
    .subtitle {{ opacity: .9; margin-top: 6px; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 16px; margin-top: 24px; }}
    .card {{ background: #fff; border-radius: 10px; text-decoration: none; color: inherit; box-shadow: 0 2px 10px rgba(0,0,0,.08); transition: transform .12s ease, box-shadow .12s ease; }}
    .card:hover {{ transform: translateY(-2px); box-shadow: 0 6px 18px rgba(0,0,0,.12); }}
    .card-body {{ padding: 18px; }}
    .card-title {{ font-size: 18px; font-weight: 700; color: #2E86AB; }}
    .card-sub {{ margin-top: 6px; color: #555; font-size: 14px; }}
    .footer {{ text-align: center; color: #666; margin-top: 28px; }}
  </style>
</head>
<body>
  <div class=\"container\">
    <div class=\"header\">
      <h1>🏨 Travel Price Monitor</h1>
      <div class=\"subtitle\">Выберите направление • Обновлено: {now}</div>
    </div>
    <div class=\"grid\">
      {cards_html}
    </div>
    <div class=\"footer\">🤖 Обновляется каждый час • GitHub Pages</div>
  </div>
</body>
</html>
"""
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(html)

if __name__ == '__main__':
    tiles = [
        { 'title': 'Фильтр 7–10 дней', 'subtitle': '4766–9000 PLN • WAW/WMI/RDO', 'href': 'index_filter_7_10_days.html' },
        { 'title': 'Фильтр 13–16 дней', 'subtitle': '4766–9000 PLN • WAW/WMI/RDO', 'href': 'index_filter_13_16_days.html' }
    ]
    generate_landing(tiles, 'index.html')
