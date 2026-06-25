# Books Scraper — BuscaLibre.cl

![Python](https://img.shields.io/badge/Python-3.13-3776AB)
![Playwright](https://img.shields.io/badge/Playwright-1.52-45ba4b)
![Flask](https://img.shields.io/badge/Flask-3.1-000)
![SQLite](https://img.shields.io/badge/SQLite-3-003B57)

Scraper de precios para listas de deseos de **BuscaLibre.cl** con historial, dashboard web y notificaciones por Telegram.

---

## Features

- **Scraping automático** — Obtiene precios de tus wishlists de BuscaLibre
- **Optimizado** — Usa precios visibles en la lista; solo visita páginas individuales cuando faltan datos
- **Historial de precios** — Cada precio queda registrado con fecha, descuento y estado (disponible / sin stock)
- **Notificaciones Telegram** — Alertas de bajas de precio > $5.000 y detección de ofertas flash
- **Dashboard web** — Flask + Chart.js + sparklines inline SVG
- **Múltiples listas** — Soporte para varias wishlists con organización N:M
- **Export CSV** — Descarga de datos completa
- **Recomendación de compra** — Algoritmo que sugiere el mejor momento según mínimo histórico y tendencia

---

## Stack

| Capa           | Tecnología                                  |
| -------------- | ------------------------------------------- |
| Scraping       | Python · Playwright (async) · BeautifulSoup |
| Backend        | Flask · SQLite                              |
| Frontend       | Bootstrap 5 · Chart.js · Sparklines SVG     |
| Notificaciones | Telegram Bot API                            |
| Automatización | Windows Task Scheduler / GitHub Actions     |

---

## Quick Start

```bash
# 1. Clonar
git clone https://github.com/maetenco/books-scraper.git
cd books-scraper

# 2. Dependencias
pip install -r requirements.txt
playwright install chromium

# 3. Configurar Telegram (opcional)
cp .env.example .env
# Editar .env con tu TOKEN y CHAT_ID de @BotFather

# 4. Login manual (primera vez)
python run.py --login

# 5. Ejecutar scraper
python run.py

# 6. Dashboard
python dashboard.py
# Abrir http://localhost:5000
```

---

## Estructura

```
books-scraper/
├── scrap.py          # Scraper principal (Playwright async)
├── telegram.py       # Notificaciones Telegram
├── dashboard.py      # Dashboard web Flask
├── run.py            # Entry point para ejecución
├── templates/        # Jinja2 templates
│   ├── layout.html
│   ├── index.html
│   ├── lista.html
│   ├── libro.html
│   ├── todos.html
│   └── stats.html
├── static/
│   └── style.css
├── requirements.txt
├── .env.example
└── .gitignore
```

---

## Uso

| Comando                       | Descripción                                 |
| ----------------------------- | ------------------------------------------- |
| `python run.py`               | Scraping headless + notificaciones Telegram |
| `python run.py --no-telegram` | Solo scraping, sin notificar                |
| `python run.py --login`       | Modo manual (guarda sesión para headless)   |
| `python dashboard.py`         | Inicia el dashboard web en :5000            |
| `python scrap.py --headless`  | Solo scraping (sin run.py)                  |

---

## Arquitectura

```
BuscaLibre.cl
     │
     ▼ (Playwright)
  scrap.py
     │
     ▼ (SQLite)
  libros.db
     │
     ├──► telegram.py   →  Telegram API
     │
     └──► dashboard.py  →  Flask web :5000
               │
               ├── index.html   (resumen)
               ├── lista.html   (por wishlist)
               ├── todos.html   (búsqueda global)
               ├── libro.html   (detalle + gráfico)
               └── stats.html   (estadísticas)
```
