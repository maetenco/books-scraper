"""
Entry point para ejecución programada (headless + notificaciones).

Uso:
    python run.py                    # headless + Telegram
    python run.py --no-telegram      # solo headless, sin notificar
    python run.py --login            # mode manual (guardar sesión)
    python run.py --preview-alertas  # muestra las alertas de la DB actual, sin scrapear ni enviar

Para programar (Windows Task Scheduler):
    Action: python C:\ruta\run.py
    Trigger 1: diario a las 10:00
    Trigger 2: al iniciar sesión (con retraso ~5 min), por si el equipo
               estaba apagado/suspendido a la hora fija.
    (run.py evita correr dos veces el mismo día vía .last_run, así que
     es seguro combinar ambos triggers en la misma tarea.)

Logging:
    Task Scheduler no captura stdout, así que todo lo que antes solo
    se imprimía por consola se perdía cuando corría desatendido. Acá
    se agrega un log rotativo (scraper.log) con lo mismo que se ve en
    consola, más el traceback completo si algo revienta, para poder
    diagnosticar corridas fallidas después del hecho.
"""

import asyncio
import argparse
import logging
import sqlite3
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv

from utils import ya_corrio_hoy, marcar_ejecucion_hoy

# ── Logging: consola (igual que antes) + archivo rotativo ──
_LOG_FILE = Path(__file__).parent / "scraper.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        RotatingFileHandler(_LOG_FILE, maxBytes=2_000_000, backupCount=5, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("run")

# ═══════════════════════════════════════════════════════════════
# Conceptos de run.py:
#  [1] load_dotenv()         → Carga .env con TELEGRAM_BOT_TOKEN
#       y TELEGRAM_CHAT_ID desde el archivo .env.
#  [2] scrap_main()          → Llama a main() de scrap.py con
#       headless=True/False según flag --login.
#  [3] calcular_alertas()   → (alertas.py) nuevos mínimos del año,
#       evento de rebaja de la tienda y resumen semanal.
#  [4] enviar_alertas()     → Envía cada mensaje por Telegram y lo
#       registra en `notificaciones` solo si Telegram lo aceptó.
#  [5] Argumentos CLI        → --no-telegram omite notificaciones;
#       --login fuerza navegador visible para autenticación.
#  [6] asyncio.run(run())    → Entry point asíncrono requerido
#       por Playwright (toda la lógica es async).
# ═══════════════════════════════════════════════════════════════

# ── [1] load_dotenv: carga credenciales Telegram ──
load_dotenv()

# ── [2] scrap_main: scraper principal ──
from scrap import main as scrap_main, DB_PATH, SesionExpirada
from indicadores import actualizar_uf
from alertas import calcular_alertas, redactar_mensajes, registrar_notificaciones
from telegram import enviar_mensaje, notificar_fallo


# ── [5] Argumentos CLI ──
# type hint: argv: list[str] | None, retorno -> argparse.Namespace
# Mejora: separado de run() para que el bloque __main__ sepa si debe
#         avisar fallos por Telegram (--no-telegram) aunque run() reviente.
def parsear_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Books scraper runner")
    parser.add_argument("--no-telegram", action="store_true", help="Skip Telegram notifications")
    parser.add_argument("--login", action="store_true", help="Run with browser (save session)")
    parser.add_argument("--force", action="store_true", help="Run even if it already ran today")
    parser.add_argument("--preview-alertas", action="store_true",
                        help="Show the alerts for the current DB without scraping or sending")
    return parser.parse_args(argv)


# ── [4] enviar_alertas: calcula, envía (o muestra) y registra ──
# type hint: conn: sqlite3.Connection, enviar: bool, retorno -> list[str]
# Mejora: list[str] = los textos generados, para --preview-alertas y tests.
def enviar_alertas(conn: sqlite3.Connection, enviar: bool = True) -> list[str]:
    mensajes = redactar_mensajes(calcular_alertas(conn))
    if not mensajes:
        logger.info("Sin alertas para enviar")
    for texto, claves in mensajes:
        if not enviar:
            continue
        if enviar_mensaje(texto):
            registrar_notificaciones(conn, claves)
        else:
            logger.warning("No se pudo enviar una alerta; se reintentará en la próxima corrida")
    return [texto for texto, _ in mensajes]


# type hint: args: argparse.Namespace, retorno -> None
# Mejora: None indica que run() ejecuta efectos secundarios
#         (scraping, notificaciones) sin retornar valor útil.
async def run(args: argparse.Namespace) -> None:
    if args.preview_alertas:
        # Solo lectura de lo que se enviaría: no scrapea, no envía, no registra.
        conn = sqlite3.connect(DB_PATH)
        textos = enviar_alertas(conn, enviar=False)
        conn.close()
        for texto in textos:
            print("\n" + "─" * 60 + "\n" + texto)
        print(f"\n{len(textos)} mensaje(s) se enviarían.")
        return

    # ── Evita correr dos veces el mismo día ──
    # El Task Scheduler dispara run.py por horario fijo (10:00) y también
    # al iniciar sesión (por si el equipo estaba apagado/suspendido a esa
    # hora). Si ambos disparan el mismo día, el segundo se corta acá.
    logger.info("=== Inicio de corrida (args: %s) ===", vars(args))

    if args.force:
        logger.info("--force: se ignora el chequeo de ejecución diaria")
    elif ya_corrio_hoy():
        logger.info("Ya se ejecutó hoy, se omite este disparo (usar --force para forzar)")
        return

    cambios, total_escaneados = await scrap_main(headless=not args.login)
    # Una corrida que no escaneó ningún libro no cuenta como "ya corrió hoy":
    # así el siguiente disparo del Task Scheduler lo vuelve a intentar.
    if total_escaneados == 0:
        raise RuntimeError("La corrida no escaneó ningún libro (¿listas vacías o página cambió?)")
    marcar_ejecucion_hoy()

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")

    # UF del SII para el "mínimo histórico ajustado" (nunca lanza: si el
    # SII no responde, el dashboard solo omite ese nivel).
    nuevas_uf = actualizar_uf(conn)
    if nuevas_uf:
        logger.info("UF: %s valores nuevos desde el SII", nuevas_uf)

    logger.info("Resumen: %s libros escaneados, %s bajaron vs su registro anterior",
                total_escaneados, len(cambios))

    # ── [3]/[4] Alertas: nuevos mínimos, evento de tienda, resumen semanal ──
    if args.no_telegram:
        logger.info("--no-telegram: no se envían alertas")
    else:
        enviar_alertas(conn)

    conn.close()
    logger.info("=== Corrida finalizada OK ===")


# ── [6] Entry point asíncrono ──
if __name__ == "__main__":
    _args = parsear_args()
    try:
        asyncio.run(run(_args))
    except SesionExpirada as e:
        logger.error("Sesión expirada: %s", e)
        if not _args.no_telegram:
            notificar_fallo(e, sesion_expirada=True)
        sys.exit(1)
    except Exception as e:
        logger.exception("Corrida abortada por excepción no controlada")
        if not _args.no_telegram:
            notificar_fallo(e)
        sys.exit(1)
