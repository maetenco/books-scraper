"""
Migraciones de datos versionadas para libros.db.

Uso:
    python migraciones.py --dry-run               # muestra qué cambiaría, no escribe
    python migraciones.py                         # backup + aplica migraciones pendientes
    python migraciones.py --reportar-duplicados   # libros distintos con la misma URL
    BOOKS_DB_PATH=copia.db python migraciones.py  # trabajar sobre una copia

Por qué así:
    crear_tablas() (scrap.py) ya evoluciona el ESQUEMA con ALTER TABLE
    idempotentes. Lo que no puede hacer de forma segura es transformar
    DATOS (p. ej. borrar duplicados): eso tiene que correr una sola vez,
    con backup previo y verificación. La versión aplicada se guarda en
    PRAGMA user_version (nativo de SQLite, sin tablas ni dependencias).

    Nunca corre sola: ni run.py ni dashboard.py la invocan. Cada
    migración se ejecuta dentro de una transacción; si su verificación
    falla, se hace rollback y la DB queda como estaba. En --dry-run se
    ejecuta igual pero siempre se hace rollback, así el resumen refleja
    exactamente lo que pasaría.
"""

import argparse
import logging
import os
import sqlite3
import sys
from collections.abc import Callable

import scrap
from scrap import crear_tablas, hacer_backup_db

logger = logging.getLogger("migraciones")


class VerificacionFallida(Exception):
    """La migración no dejó la DB en el estado esperado: se revierte."""


# ── Migración 1: un precio por libro y día ──
# type hint: conexion: sqlite3.Connection, retorno -> dict
# Mejora: dict con conteos para mostrarlos en --dry-run y en el log.
def _m001_deduplicar_precios(conexion: sqlite3.Connection) -> dict:
    """Deja una sola fila de precios por (libro_id, día): la de mayor id,
    es decir, la última registrada ese día. Los duplicados venían de
    registrar el precio una vez por cada lista donde está el libro y de
    corridas repetidas el mismo día.
    """
    cursor = conexion.cursor()
    cursor.execute("SELECT COUNT(*) FROM precios")
    filas_antes = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM (SELECT 1 FROM precios GROUP BY libro_id, DATE(fecha))")
    grupos = cursor.fetchone()[0]

    cursor.execute("""
        DELETE FROM precios
        WHERE id NOT IN (SELECT MAX(id) FROM precios GROUP BY libro_id, DATE(fecha))
    """)
    borradas = cursor.rowcount

    cursor.execute("SELECT COUNT(*) FROM precios")
    filas_despues = cursor.fetchone()[0]
    cursor.execute("""
        SELECT COUNT(*) FROM (
            SELECT 1 FROM precios GROUP BY libro_id, DATE(fecha) HAVING COUNT(*) > 1
        )
    """)
    grupos_duplicados = cursor.fetchone()[0]

    if filas_despues != grupos or grupos_duplicados != 0 or filas_antes - borradas != filas_despues:
        raise VerificacionFallida(
            f"esperadas {grupos} filas sin duplicados; quedaron {filas_despues} "
            f"({grupos_duplicados} grupos aún duplicados)"
        )
    return {"filas_antes": filas_antes, "filas_borradas": borradas, "filas_despues": filas_despues}


# Lista ordenada (versión, descripción, función). Para agregar una
# migración: siguiente número, nunca modificar una ya publicada.
MIGRACIONES: list[tuple[int, str, Callable[[sqlite3.Connection], dict]]] = [
    (1, "Deduplicar precios: una fila por libro y día", _m001_deduplicar_precios),
]


# type hint: conexion: sqlite3.Connection, retorno -> int
def version_actual(conexion: sqlite3.Connection) -> int:
    return conexion.execute("PRAGMA user_version").fetchone()[0]


# type hint: conexion: sqlite3.Connection, dry_run: bool, retorno -> list[dict]
# Mejora: list[dict] = un resumen por migración, testeable sin parsear logs.
def aplicar_migraciones(conexion: sqlite3.Connection, dry_run: bool = False) -> list[dict]:
    """Aplica en orden las migraciones con versión > user_version.
    Se detiene en la primera que falle (VerificacionFallida u error de
    SQLite) tras revertirla; las anteriores ya confirmadas se mantienen.
    """
    resultados = []
    for version, descripcion, migracion in MIGRACIONES:
        if version <= version_actual(conexion):
            continue
        # isolation_level=None + BEGIN explícito: control total de la
        # transacción (el modo implícito de sqlite3 no abarca el PRAGMA).
        conexion.execute("BEGIN")
        try:
            resumen = migracion(conexion)
            if dry_run:
                conexion.execute("ROLLBACK")
            else:
                conexion.execute(f"PRAGMA user_version = {int(version)}")
                conexion.execute("COMMIT")
        except Exception:
            conexion.execute("ROLLBACK")
            raise
        resultados.append({"version": version, "descripcion": descripcion, **resumen})
    return resultados


# type hint: conexion: sqlite3.Connection, retorno -> list[tuple]
def reportar_duplicados_url(conexion: sqlite3.Connection) -> list[tuple[str, list[tuple[int, str, str]]]]:
    """Libros distintos (titulo+autor) que comparten URL de producto:
    probablemente el mismo libro con título cambiado. Solo informa."""
    cursor = conexion.cursor()
    cursor.execute("""
        SELECT url FROM libros
        WHERE url IS NOT NULL AND url != ''
        GROUP BY url HAVING COUNT(*) > 1
        ORDER BY url
    """)
    reporte = []
    for (url,) in cursor.fetchall():
        cursor.execute("SELECT id, titulo, autor FROM libros WHERE url = ? ORDER BY id", (url,))
        reporte.append((url, cursor.fetchall()))
    return reporte


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")
    parser = argparse.ArgumentParser(description="Migraciones de datos de libros.db")
    parser.add_argument("--dry-run", action="store_true", help="Simula sin escribir nada")
    parser.add_argument("--reportar-duplicados", action="store_true",
                        help="Lista libros distintos con la misma URL (no modifica nada)")
    args = parser.parse_args()

    db_path = scrap.DB_PATH
    if not os.path.exists(db_path):
        logger.error("No existe la base de datos %s", db_path)
        return 1

    conexion = sqlite3.connect(db_path, isolation_level=None)
    try:
        if args.reportar_duplicados:
            reporte = reportar_duplicados_url(conexion)
            if not reporte:
                print("No hay libros distintos con la misma URL.")
            for url, libros in reporte:
                print(f"\n{url}")
                for libro_id, titulo, autor in libros:
                    print(f"  id={libro_id}  {titulo} — {autor}")
            return 0

        crear_tablas(conexion)
        version = version_actual(conexion)
        pendientes = [m for m in MIGRACIONES if m[0] > version]
        logger.info("DB: %s | versión actual: %s | pendientes: %s", db_path, version, len(pendientes))
        if not pendientes:
            return 0

        if not args.dry_run:
            backup = hacer_backup_db(conexion, os.path.join(os.path.dirname(os.path.abspath(db_path)), "backups"))
            if backup is None:
                logger.error("No se pudo crear el backup; no se aplica ninguna migración")
                return 1

        for r in aplicar_migraciones(conexion, dry_run=args.dry_run):
            detalle = ", ".join(f"{k}={v}" for k, v in r.items() if k not in ("version", "descripcion"))
            prefijo = "[DRY-RUN] " if args.dry_run else ""
            logger.info("%sMigración %s (%s): %s", prefijo, r["version"], r["descripcion"], detalle)
        return 0
    except (VerificacionFallida, sqlite3.Error) as e:
        logger.error("Migración revertida, la DB no cambió: %s", e)
        return 1
    finally:
        conexion.close()


if __name__ == "__main__":
    sys.exit(main())
