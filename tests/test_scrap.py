from scrap import limpiar_precio
import sys
import os
import sqlite3

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
# Test de scrap.py


def test_limpiar_precio_normal():
    assert limpiar_precio("$12.990") == 12990


def test_limpiar_precio_sin_signo():
    assert limpiar_precio("12990") == 12990


def test_limpiar_precio_none():
    assert limpiar_precio(None) is None


def test_limpiar_precio_invalido():
    assert limpiar_precio("abc") is None


def test_crear_tablas():
    from scrap import crear_tablas
    conn = sqlite3.connect(":memory:")
    crear_tablas(conn)
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tablas = {row[0] for row in cursor.fetchall()}
    assert "libros" in tablas
    assert "listas" in tablas
    assert "libros_listas" in tablas
    assert "precios" in tablas
    conn.close()
