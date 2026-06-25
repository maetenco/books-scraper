"""
Entry point para ejecución programada (headless + notificaciones).

Uso:
    python run.py                    # headless + Telegram
    python run.py --no-telegram      # solo headless, sin notificar
    python run.py --login            # mode manual (guardar sesión)

Para programar (Windows Task Scheduler):
    Action: python C:\ruta\run.py
    Trigger: diario a las 10:00
"""

import asyncio
import argparse
import sqlite3
import os
from dotenv import load_dotenv

load_dotenv()

from scrap import main as scrap_main
from telegram import notificar_bajas, notificar_resumen, notificar_ofertas_flash, detectar_bajas, detectar_ofertas_flash

# ═══════════════════════════════════════════════════════════════
# Conceptos de run.py:
#  [1] load_dotenv()         → Carga .env con TELEGRAM_BOT_TOKEN
#       y TELEGRAM_CHAT_ID desde el archivo .env.
#  [2] scrap_main()          → Llama a main() de scrap.py con
#       headless=True/False según flag --login.
#  [3] detectar_bajas /      → Consultan la DB en busca de
#      detectar_ofertas_flash   cambios de precio significativos.
#  [4] notificar_bajas /     → Envían alertas al chat de Telegram
#      notificar_resumen /      con formato HTML.
#      notificar_ofertas_flash
#  [5] Argumentos CLI        → --no-telegram omite notificaciones;
#       --login fuerza navegador visible para autenticación.
#  [6] asyncio.run(run())    → Entry point asíncrono requerido
#       por Playwright (toda la lógica es async).
# ═══════════════════════════════════════════════════════════════

# ── [1] load_dotenv: carga credenciales Telegram ──
load_dotenv()

# ── [2] scrap_main: scraper principal ──
from scrap import main as scrap_main
from telegram import notificar_bajas, notificar_resumen, notificar_ofertas_flash, detectar_bajas, detectar_ofertas_flash


# type hint: retorno -> None
# Mejora: None indica que run() ejecuta efectos secundarios
#         (scraping, notificaciones) sin retornar valor útil.
async def run() -> None:
    # ── [5] Argumentos CLI ──
    parser = argparse.ArgumentParser(description="Books scraper runner")
    parser.add_argument("--no-telegram", action="store_true", help="Skip Telegram notifications")
    parser.add_argument("--login", action="store_true", help="Run with browser (save session)")
    args = parser.parse_args()

    cambios = await scrap_main(headless=not args.login)

    conn = sqlite3.connect("libros.db")

    # ── [3] Detección de bajas y ofertas flash ──
    bajas = detectar_bajas(conn)
    ofertas_flash = detectar_ofertas_flash(conn)
    stats = {
        "libros": len(set(c["titulo"] for c in cambios)),
        "actualizados": len(cambios),
        "bajas": len(bajas),
        "ofertas_flash": len(ofertas_flash),
    }

    print(f"\nResumen: {stats['libros']} libros, {stats['actualizados']} actualizados, {stats['bajas']} bajas, {stats['ofertas_flash']} ofertas flash")

    # ── [4] Notificaciones Telegram ──
    if not args.no_telegram:
        if bajas:
            notificar_bajas(bajas)
        else:
            print("Sin bajas de precio para notificar")
        if ofertas_flash:
            notificar_ofertas_flash(ofertas_flash)
        else:
            print("Sin ofertas flash para notificar")
        notificar_resumen(stats)

    conn.close()


# ── [6] Entry point asíncrono ──
if __name__ == "__main__":
    asyncio.run(run())
