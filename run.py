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


async def run():
    parser = argparse.ArgumentParser(description="Books scraper runner")
    parser.add_argument("--no-telegram", action="store_true", help="Skip Telegram notifications")
    parser.add_argument("--login", action="store_true", help="Run with browser (save session)")
    args = parser.parse_args()

    cambios = await scrap_main(headless=not args.login)

    conn = sqlite3.connect("libros.db")

    bajas = detectar_bajas(conn)
    ofertas_flash = detectar_ofertas_flash(conn)
    stats = {
        "libros": len(set(c["titulo"] for c in cambios)),
        "actualizados": len(cambios),
        "bajas": len(bajas),
        "ofertas_flash": len(ofertas_flash),
    }

    print(f"\nResumen: {stats['libros']} libros, {stats['actualizados']} actualizados, {stats['bajas']} bajas, {stats['ofertas_flash']} ofertas flash")

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


if __name__ == "__main__":
    asyncio.run(run())
