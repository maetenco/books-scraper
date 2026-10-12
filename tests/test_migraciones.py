"""
Tests de migraciones.py: deduplicación de precios por libro y día,
dry-run sin efectos, idempotencia, rollback ante verificación fallida
y reporte de URLs duplicadas.
"""
import os
import sys
import sqlite3

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import migraciones
from migraciones import aplicar_migraciones, reportar_duplicados_url, version_actual, VerificacionFallida
from scrap import crear_tablas


def _db_con_duplicados() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    crear_tablas(conn)
    conn.execute("INSERT INTO libros (id, titulo, autor) VALUES (1, 'dune', 'frank herbert')")
    conn.execute("INSERT INTO libros (id, titulo, autor) VALUES (2, 'emma', 'jane austen')")
    filas = [
        # libro 1: 3 filas el mismo día (2 listas + corrida repetida) → queda la última (18000)
        (1, 20000, "2026-10-01 10:00:00"),
        (1, 20000, "2026-10-01 10:05:00"),
        (1, 18000, "2026-10-01 14:00:00"),
        (1, 17000, "2026-10-02 10:00:00"),
        # libro 2: sin duplicados
        (2, 9000, "2026-10-01 10:00:00"),
        (2, 9500, "2026-10-02 10:00:00"),
    ]
    conn.executemany(
        "INSERT INTO precios (libro_id, precio_actual, estado, fecha) VALUES (?, ?, 'disponible', ?)", filas
    )
    return conn


def test_deduplica_dejando_la_ultima_fila_del_dia():
    conn = _db_con_duplicados()
    resultados = aplicar_migraciones(conn)

    assert resultados[0]["filas_antes"] == 6
    assert resultados[0]["filas_borradas"] == 2
    assert resultados[0]["filas_despues"] == 4
    precios_libro_1 = [r[0] for r in conn.execute(
        "SELECT precio_actual FROM precios WHERE libro_id = 1 ORDER BY fecha")]
    assert precios_libro_1 == [18000, 17000]
    assert version_actual(conn) == 1


def test_dry_run_no_modifica_nada():
    conn = _db_con_duplicados()
    resultados = aplicar_migraciones(conn, dry_run=True)

    assert resultados[0]["filas_borradas"] == 2  # informa lo que haría
    assert conn.execute("SELECT COUNT(*) FROM precios").fetchone()[0] == 6
    assert version_actual(conn) == 0


def test_es_idempotente():
    conn = _db_con_duplicados()
    aplicar_migraciones(conn)
    assert aplicar_migraciones(conn) == []  # ya en versión 1: no hay pendientes
    assert conn.execute("SELECT COUNT(*) FROM precios").fetchone()[0] == 4


def test_verificacion_fallida_revierte(monkeypatch):
    conn = _db_con_duplicados()

    def migracion_que_falla(c):
        c.execute("DELETE FROM precios")
        raise VerificacionFallida("simulada")

    monkeypatch.setattr(migraciones, "MIGRACIONES", [(1, "falla", migracion_que_falla)])
    with pytest.raises(VerificacionFallida):
        aplicar_migraciones(conn)

    assert conn.execute("SELECT COUNT(*) FROM precios").fetchone()[0] == 6
    assert version_actual(conn) == 0


def test_reporta_libros_distintos_con_misma_url():
    conn = sqlite3.connect(":memory:")
    crear_tablas(conn)
    conn.execute("INSERT INTO libros (titulo, autor, url) VALUES ('dune', 'herbert', 'https://x/1')")
    conn.execute("INSERT INTO libros (titulo, autor, url) VALUES ('dune (ed. bolsillo)', 'herbert', 'https://x/1')")
    conn.execute("INSERT INTO libros (titulo, autor, url) VALUES ('emma', 'austen', 'https://x/2')")

    reporte = reportar_duplicados_url(conn)

    assert len(reporte) == 1
    url, libros = reporte[0]
    assert url == "https://x/1"
    assert {titulo for _, titulo, _ in libros} == {"dune", "dune (ed. bolsillo)"}
