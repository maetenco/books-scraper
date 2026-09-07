"""
Tests de integración de guardar_libro() (scrap.py) contra SQLite en
memoria, y de eliminar_libro() (dashboard.py) contra el SQLite de
archivo temporal que ya provee la fixture `app` de conftest.py.

Cubre: libro nuevo, libro repetido (UPSERT), detección de baja,
transición a sin_stock, y cascada de borrado en precios/libros_listas.
"""
import os
import sys
import sqlite3

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import dashboard
from scrap import crear_tablas, guardar_libro


def _nueva_conexion() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    crear_tablas(conn)
    return conn


def _crear_lista(conn: sqlite3.Connection, lista_id: str = "L1", nombre: str = "Mi lista") -> int:
    cursor = conn.cursor()
    cursor.execute("INSERT INTO listas (lista_id, nombre) VALUES (?, ?)", (lista_id, nombre))
    conn.commit()
    cursor.execute("SELECT id FROM listas WHERE lista_id = ?", (lista_id,))
    return cursor.fetchone()[0]


def _libro(titulo="dune", autor="frank herbert", precio_actual=20000, precio_antes=None,
           descuento="Sin descuento", estado="disponible", url="", imagen_url="") -> dict:
    return {
        "titulo": titulo,
        "autor": autor,
        "precio_actual": precio_actual,
        "precio_antes": precio_antes,
        "descuento": descuento,
        "estado": estado,
        "url": url,
        "imagen_url": imagen_url,
    }


# ── guardar_libro(): integración contra SQLite en memoria ──

def test_guardar_libro_nuevo_no_detecta_baja():
    conn = _nueva_conexion()
    id_lista = _crear_lista(conn)
    cambio = guardar_libro(conn, _libro(precio_actual=20000), id_lista)
    assert cambio is None  # primer precio registrado, no hay "antes" con qué comparar

    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM libros")
    assert cursor.fetchone()[0] == 1
    cursor.execute("SELECT COUNT(*) FROM libros_listas")
    assert cursor.fetchone()[0] == 1
    cursor.execute("SELECT COUNT(*) FROM precios")
    assert cursor.fetchone()[0] == 1
    conn.close()


def test_guardar_libro_repetido_hace_upsert_no_duplica_libro():
    conn = _nueva_conexion()
    id_lista = _crear_lista(conn)
    guardar_libro(conn, _libro(precio_actual=20000), id_lista)
    guardar_libro(conn, _libro(precio_actual=18000), id_lista)  # mismo título+autor

    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM libros")
    assert cursor.fetchone()[0] == 1  # no duplica el libro (UNIQUE titulo+autor)
    cursor.execute("SELECT COUNT(*) FROM libros_listas")
    assert cursor.fetchone()[0] == 1  # UNIQUE(libro_id, lista_id) evita duplicar la relación
    cursor.execute("SELECT COUNT(*) FROM precios")
    assert cursor.fetchone()[0] == 2  # histórico append-only: 2 registros de precio
    conn.close()


def test_guardar_libro_detecta_baja_de_precio():
    conn = _nueva_conexion()
    id_lista = _crear_lista(conn)
    guardar_libro(conn, _libro(precio_actual=20000), id_lista)
    cambio = guardar_libro(conn, _libro(precio_actual=15000), id_lista)

    assert cambio is not None
    assert cambio["precio_viejo"] == 20000
    assert cambio["precio_nuevo"] == 15000
    conn.close()


def test_guardar_libro_no_reporta_cambio_si_precio_sube():
    conn = _nueva_conexion()
    id_lista = _crear_lista(conn)
    guardar_libro(conn, _libro(precio_actual=15000), id_lista)
    cambio = guardar_libro(conn, _libro(precio_actual=20000), id_lista)
    assert cambio is None
    conn.close()


def test_guardar_libro_transicion_a_sin_stock_no_reporta_cambio():
    conn = _nueva_conexion()
    id_lista = _crear_lista(conn)
    guardar_libro(conn, _libro(precio_actual=20000), id_lista)
    cambio = guardar_libro(
        conn,
        _libro(precio_actual=None, precio_antes=None, descuento="Sin stock", estado="sin_stock"),
        id_lista,
    )
    assert cambio is None

    cursor = conn.cursor()
    cursor.execute("SELECT estado FROM precios ORDER BY id DESC LIMIT 1")
    assert cursor.fetchone()[0] == "sin_stock"
    conn.close()


def test_guardar_libro_detecta_baja_ignorando_registros_sin_stock():
    # El "último precio" para detectar bajas debe ignorar los registros
    # con estado 'sin_stock' (sin precio real) y comparar contra el
    # último precio con estado 'disponible'.
    conn = _nueva_conexion()
    id_lista = _crear_lista(conn)
    guardar_libro(conn, _libro(precio_actual=20000), id_lista)
    guardar_libro(conn, _libro(precio_actual=None, estado="sin_stock", descuento="Sin stock"), id_lista)
    cambio = guardar_libro(conn, _libro(precio_actual=15000), id_lista)

    assert cambio is not None
    assert cambio["precio_viejo"] == 20000
    assert cambio["precio_nuevo"] == 15000
    conn.close()


# ── eliminar_libro(): cascada de borrado (ruta Flask) ──

def _seed_libro_completo(db_path: str) -> int:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    id_lista = _crear_lista(conn)
    guardar_libro(conn, _libro(precio_actual=20000), id_lista)
    guardar_libro(conn, _libro(precio_actual=15000), id_lista)  # 2 registros de precio
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM libros LIMIT 1")
    libro_id = cursor.fetchone()[0]
    conn.close()
    return libro_id


def test_eliminar_libro_borra_libro_y_su_historial_en_cascada(client):
    libro_id = _seed_libro_completo(dashboard.DB_PATH)

    resp = client.post(
        f"/libro/{libro_id}/eliminar",
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True

    conn = sqlite3.connect(dashboard.DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM libros WHERE id = ?", (libro_id,))
    assert cursor.fetchone()[0] == 0
    cursor.execute("SELECT COUNT(*) FROM precios WHERE libro_id = ?", (libro_id,))
    assert cursor.fetchone()[0] == 0
    cursor.execute("SELECT COUNT(*) FROM libros_listas WHERE libro_id = ?", (libro_id,))
    assert cursor.fetchone()[0] == 0
    conn.close()


def test_eliminar_libro_sin_header_csrf_es_rechazado(client):
    libro_id = _seed_libro_completo(dashboard.DB_PATH)

    resp = client.post(f"/libro/{libro_id}/eliminar")  # sin X-Requested-With
    assert resp.status_code == 403

    conn = sqlite3.connect(dashboard.DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM libros WHERE id = ?", (libro_id,))
    assert cursor.fetchone()[0] == 1  # no se borró nada
    conn.close()


def test_eliminar_libro_inexistente_da_404(client):
    resp = client.post(
        "/libro/999999/eliminar",
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert resp.status_code == 404


class _ConexionQueFalla(sqlite3.Connection):
    """Doble de prueba: sqlite3.Connection es un tipo inmutable de C
    (no se puede monkeypatchear su método commit directamente), así
    que se usa una subclase con factory= para simular
    "database is locked" en el commit."""

    def commit(self):
        raise sqlite3.OperationalError("database is locked")


def test_eliminar_libro_database_locked_devuelve_503(client, monkeypatch):
    # Fuerza sqlite3.OperationalError("database is locked") en el commit
    # (Fase 5, punto 21) y confirma que se responde 503 en vez de un
    # error 500 genérico, y que la transacción se revierte.
    libro_id = _seed_libro_completo(dashboard.DB_PATH)

    def get_db_que_falla():
        conn = sqlite3.connect(dashboard.DB_PATH, factory=_ConexionQueFalla)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(dashboard, "get_db", get_db_que_falla)

    resp = client.post(
        f"/libro/{libro_id}/eliminar",
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert resp.status_code == 503
    assert resp.get_json()["ok"] is False

    conn = sqlite3.connect(dashboard.DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM libros WHERE id = ?", (libro_id,))
    assert cursor.fetchone()[0] == 1  # no se borró nada, el rollback funcionó
    conn.close()
