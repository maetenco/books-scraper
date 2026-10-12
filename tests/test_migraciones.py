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
from migraciones import (
    aplicar_migraciones, fusionar_libros, reportar_duplicados_url, version_actual, VerificacionFallida,
)
from scrap import crear_tablas


def _db_con_duplicados() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    crear_tablas(conn)
    conn.execute("INSERT INTO libros (id, titulo, autor) VALUES (1, 'dune', 'frank herbert')")
    conn.execute("INSERT INTO libros (id, titulo, autor) VALUES (2, 'emma', 'jane austen')")
    filas = [
        # libro 1, día 1: 2 filas idénticas (corrida repetida) → queda 1;
        # 18000 cambió dentro del día → se conserva (no se pierde ningún precio)
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


def test_deduplica_solo_filas_identicas_del_mismo_dia():
    conn = _db_con_duplicados()
    resultados = aplicar_migraciones(conn)

    assert resultados[0]["filas_antes"] == 6
    assert resultados[0]["filas_borradas"] == 1
    assert resultados[0]["filas_despues"] == 5
    filas_libro_1 = conn.execute(
        "SELECT precio_actual, fecha FROM precios WHERE libro_id = 1 ORDER BY fecha").fetchall()
    assert filas_libro_1 == [
        (20000, "2026-10-01 10:00:00"),  # se conserva la primera de las idénticas
        (18000, "2026-10-01 14:00:00"),  # cambio intradía: se conserva
        (17000, "2026-10-02 10:00:00"),
    ]
    assert version_actual(conn) == 1


def test_no_borra_filas_iguales_de_dias_distintos():
    conn = _db_con_duplicados()
    conn.execute("INSERT INTO precios (libro_id, precio_actual, estado, fecha) VALUES (2, 9500, 'disponible', '2026-10-03 10:00:00')")
    aplicar_migraciones(conn)
    assert conn.execute("SELECT COUNT(*) FROM precios WHERE libro_id = 2").fetchone()[0] == 3


def test_dry_run_no_modifica_nada():
    conn = _db_con_duplicados()
    resultados = aplicar_migraciones(conn, dry_run=True)

    assert resultados[0]["filas_borradas"] == 1  # informa lo que haría
    assert conn.execute("SELECT COUNT(*) FROM precios").fetchone()[0] == 6
    assert version_actual(conn) == 0


def test_es_idempotente():
    conn = _db_con_duplicados()
    aplicar_migraciones(conn)
    assert aplicar_migraciones(conn) == []  # ya en versión 1: no hay pendientes
    assert conn.execute("SELECT COUNT(*) FROM precios").fetchone()[0] == 5


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
    assert {fila[1] for fila in libros} == {"dune", "dune (ed. bolsillo)"}


# ── fusionar_libros(): une dos registros del mismo libro ──

def _db_libro_partido() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    crear_tablas(conn)
    conn.execute("INSERT INTO listas (id, lista_id, nombre) VALUES (1, 'L1', 'A'), (2, 'L2', 'B')")
    # 294: formato de autor viejo, con imagen; 27455: formato nuevo, el que se sigue escaneando.
    conn.execute("INSERT INTO libros (id, titulo, autor, url, imagen_url) VALUES (294, 'attached', 'levine, amir', 'u', 'img')")
    conn.execute("INSERT INTO libros (id, titulo, autor, url, imagen_url) VALUES (27455, 'attached', 'amir levine', 'u', '')")
    conn.execute("INSERT INTO libros_listas (libro_id, lista_id) VALUES (294, 1), (27455, 1), (294, 2)")
    conn.executemany(
        "INSERT INTO precios (libro_id, precio_actual, estado, fecha) VALUES (?, ?, 'disponible', ?)",
        [(294, 15000, "2025-03-01 10:00:00"), (294, 16000, "2026-06-01 10:00:00"),
         (27455, 21000, "2026-10-12 10:00:00")],
    )
    return conn


def test_fusionar_mueve_todo_al_libro_con_precio_mas_reciente():
    conn = _db_libro_partido()
    r = fusionar_libros(conn, 27455, 294)  # el orden de los ids no importa

    assert (r["origen"], r["destino"], r["precios_movidos"]) == (294, 27455, 2)
    assert conn.execute("SELECT COUNT(*) FROM libros WHERE id = 294").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM precios WHERE libro_id = 27455").fetchone()[0] == 3
    listas = {r[0] for r in conn.execute("SELECT lista_id FROM libros_listas WHERE libro_id = 27455")}
    assert listas == {1, 2}
    assert conn.execute("SELECT imagen_url FROM libros WHERE id = 27455").fetchone()[0] == "img"


def test_fusionar_rechaza_ids_inexistentes_o_iguales():
    conn = _db_libro_partido()
    with pytest.raises(ValueError):
        fusionar_libros(conn, 294, 999)
    with pytest.raises(ValueError):
        fusionar_libros(conn, 294, 294)
