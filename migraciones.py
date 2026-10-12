"""
Migraciones de datos versionadas para libros.db.

Uso:
    python migraciones.py --dry-run               # muestra qué cambiaría, no escribe
    python migraciones.py                         # backup + aplica migraciones pendientes
    python migraciones.py --reportar-duplicados   # libros distintos con la misma URL
    python migraciones.py --fusionar 294 27455    # une dos registros del mismo libro (admite --dry-run)
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


# Clave de "fila idéntica": mismo libro, mismo día y mismos valores. Los
# COALESCE hacen que dos NULL cuenten como iguales al agrupar.
_CLAVE_FILA_IDENTICA = """
    libro_id, DATE(fecha),
    COALESCE(precio_actual, -1), COALESCE(precio_antes, -1),
    COALESCE(descuento, ''), COALESCE(estado, '')
"""


# ── Migración 1: borrar filas de precio idénticas del mismo día ──
# type hint: conexion: sqlite3.Connection, retorno -> dict
# Mejora: dict con conteos para mostrarlos en --dry-run y en el log.
def _m001_deduplicar_precios(conexion: sqlite3.Connection) -> dict:
    """Borra las filas de precios que repiten exactamente los mismos
    valores (precio actual, precio antes, descuento, estado) para el
    mismo libro el mismo día, conservando la primera (menor id). Venían
    de corridas repetidas el mismo día y de registrar el precio una vez
    por cada lista. Si el precio cambió dentro del día, ambas filas se
    conservan: no se pierde ningún precio observado.
    """
    cursor = conexion.cursor()
    cursor.execute("SELECT COUNT(*) FROM precios")
    filas_antes = cursor.fetchone()[0]
    cursor.execute(f"SELECT COUNT(*) FROM (SELECT 1 FROM precios GROUP BY {_CLAVE_FILA_IDENTICA})")
    grupos = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM (SELECT 1 FROM precios GROUP BY libro_id, DATE(fecha))")
    libro_dias_antes = cursor.fetchone()[0]

    cursor.execute(f"""
        DELETE FROM precios
        WHERE id NOT IN (SELECT MIN(id) FROM precios GROUP BY {_CLAVE_FILA_IDENTICA})
    """)
    borradas = cursor.rowcount

    cursor.execute("SELECT COUNT(*) FROM precios")
    filas_despues = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM (SELECT 1 FROM precios GROUP BY libro_id, DATE(fecha))")
    libro_dias_despues = cursor.fetchone()[0]

    # Invariantes: quedó exactamente una fila por grupo idéntico y ningún
    # libro perdió un día de historial.
    if filas_despues != grupos or libro_dias_despues != libro_dias_antes:
        raise VerificacionFallida(
            f"esperadas {grupos} filas y {libro_dias_antes} libro-días; "
            f"quedaron {filas_despues} filas y {libro_dias_despues} libro-días"
        )
    return {"filas_antes": filas_antes, "filas_borradas": borradas, "filas_despues": filas_despues}


# Lista ordenada (versión, descripción, función). Para agregar una
# migración: siguiente número, nunca modificar una ya publicada.
MIGRACIONES: list[tuple[int, str, Callable[[sqlite3.Connection], dict]]] = [
    (1, "Borrar filas de precio idénticas del mismo día", _m001_deduplicar_precios),
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
def reportar_duplicados_url(conexion: sqlite3.Connection) -> list[tuple[str, list[tuple]]]:
    """Libros distintos (titulo+autor) que comparten URL de producto:
    probablemente el mismo libro con título o formato de autor cambiado
    por BuscaLibre. Solo informa: (id, titulo, autor, n_precios, ultima_fecha)."""
    cursor = conexion.cursor()
    cursor.execute("""
        SELECT url FROM libros
        WHERE url IS NOT NULL AND url != ''
        GROUP BY url HAVING COUNT(*) > 1
        ORDER BY url
    """)
    reporte = []
    for (url,) in cursor.fetchall():
        cursor.execute("""
            SELECT lib.id, lib.titulo, lib.autor, COUNT(p.id), MAX(p.fecha)
            FROM libros lib LEFT JOIN precios p ON p.libro_id = lib.id
            WHERE lib.url = ? GROUP BY lib.id ORDER BY lib.id
        """, (url,))
        reporte.append((url, cursor.fetchall()))
    return reporte


# ── Fusionar dos registros del mismo libro ──
# type hint: conexion, id_a, id_b: int, retorno -> dict
# Mejora: dict con destino/origen y filas movidas para el log.
def fusionar_libros(conexion: sqlite3.Connection, id_a: int, id_b: int) -> dict:
    """Une dos registros del mismo libro. El DESTINO es el que tiene el
    precio más reciente: es el que el scraper sigue actualizando (si se
    fusionara al revés, el próximo escaneo recrearía el duplicado). Mueve
    precios y vínculos a listas del origen al destino, completa url/imagen
    si faltan y borra el origen. Debe llamarse dentro de una transacción.
    """
    if id_a == id_b:
        raise ValueError("Los dos ids son el mismo libro")
    cursor = conexion.cursor()
    cursor.execute("""
        SELECT lib.id, lib.url, lib.imagen_url, MAX(p.fecha) as ultima
        FROM libros lib LEFT JOIN precios p ON p.libro_id = lib.id
        WHERE lib.id IN (?, ?) GROUP BY lib.id
    """, (id_a, id_b))
    filas = cursor.fetchall()
    if len(filas) != 2:
        raise ValueError(f"No existen ambos libros (encontrados: {[f[0] for f in filas]})")

    # Orden por última fecha (None = sin precios, va primero) → el último es el destino.
    origen, destino = sorted(filas, key=lambda f: f[3] or "")
    id_origen, id_destino = origen[0], destino[0]

    cursor.execute("UPDATE precios SET libro_id = ? WHERE libro_id = ?", (id_destino, id_origen))
    precios_movidos = cursor.rowcount
    cursor.execute(
        "INSERT OR IGNORE INTO libros_listas (libro_id, lista_id) "
        "SELECT ?, lista_id FROM libros_listas WHERE libro_id = ?",
        (id_destino, id_origen)
    )
    cursor.execute("DELETE FROM libros_listas WHERE libro_id = ?", (id_origen,))
    cursor.execute("""
        UPDATE libros SET
            url = COALESCE(NULLIF(url, ''), ?),
            imagen_url = COALESCE(NULLIF(imagen_url, ''), ?)
        WHERE id = ?
    """, (origen[1], origen[2], id_destino))
    cursor.execute("DELETE FROM libros WHERE id = ?", (id_origen,))
    return {"origen": id_origen, "destino": id_destino, "precios_movidos": precios_movidos}


# Backups junto a la DB que se modifica (no en el backups/ del proyecto si
# se trabaja sobre una copia).
def _dir_backups(db_path: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), "backups")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")
    parser = argparse.ArgumentParser(description="Migraciones de datos de libros.db")
    parser.add_argument("--dry-run", action="store_true", help="Simula sin modificar datos")
    parser.add_argument("--reportar-duplicados", action="store_true",
                        help="Lista libros distintos con la misma URL (no modifica nada)")
    parser.add_argument("--fusionar", nargs=2, type=int, metavar=("ID", "ID"),
                        help="Une dos registros del mismo libro en el que tiene el precio más reciente")
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
                for libro_id, titulo, autor, n_precios, ultima in libros:
                    print(f"  id={libro_id}  {titulo} — {autor}  ({n_precios} precios, último: {ultima})")
                ids = " ".join(str(lb[0]) for lb in libros)
                print(f"  → para unirlos: python migraciones.py --fusionar {ids}")
            return 0

        if args.fusionar:
            if not args.dry_run and hacer_backup_db(conexion, _dir_backups(db_path)) is None:
                logger.error("No se pudo crear el backup; no se fusiona")
                return 1
            conexion.execute("BEGIN")
            try:
                r = fusionar_libros(conexion, *args.fusionar)
                conexion.execute("ROLLBACK" if args.dry_run else "COMMIT")
            except Exception:
                conexion.execute("ROLLBACK")
                raise
            prefijo = "[DRY-RUN] " if args.dry_run else ""
            logger.info("%sLibro %s fusionado en %s (%s precios movidos)",
                        prefijo, r["origen"], r["destino"], r["precios_movidos"])
            return 0

        crear_tablas(conexion)
        version = version_actual(conexion)
        pendientes = [m for m in MIGRACIONES if m[0] > version]
        logger.info("DB: %s | versión actual: %s | pendientes: %s", db_path, version, len(pendientes))
        if not pendientes:
            return 0

        if not args.dry_run:
            backup = hacer_backup_db(conexion, _dir_backups(db_path))
            if backup is None:
                logger.error("No se pudo crear el backup; no se aplica ninguna migración")
                return 1

        for r in aplicar_migraciones(conexion, dry_run=args.dry_run):
            detalle = ", ".join(f"{k}={v}" for k, v in r.items() if k not in ("version", "descripcion"))
            prefijo = "[DRY-RUN] " if args.dry_run else ""
            logger.info("%sMigración %s (%s): %s", prefijo, r["version"], r["descripcion"], detalle)
        return 0
    except (VerificacionFallida, ValueError, sqlite3.Error) as e:
        logger.error("Operación revertida, la DB no cambió: %s", e)
        return 1
    finally:
        conexion.close()


if __name__ == "__main__":
    sys.exit(main())
