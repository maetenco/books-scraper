"""
Tests de indicadores.py: parseo de la tabla de UF del SII y caché en la
tabla `uf`, sin red (descarga mockeada).
"""
import os
import sqlite3
import sys
from datetime import date

import pytest
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import indicadores
from indicadores import actualizar_uf, cargar_uf, parsear_tabla_uf_sii
from scrap import crear_tablas

MESES = ["Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]


def _html_sii():
    # Misma estructura que https://www.sii.cl/valores_y_fechas/uf/uf2022.htm (table_export).
    enc = "".join(f"<th>{m}</th>" for m in MESES)
    fila1 = "".join("<td>30.996,73</td>" for _ in MESES)
    fila30 = "<td>31.100,00</td><td></td>" + "".join("<td>1,00</td>" for _ in range(10))
    return ("<html><body><table><tr><td>otra tabla</td></tr></table>"
            f"<table id='table_export'><tr><th>Día</th>{enc}</tr>"
            f"<tr><th>1</th>{fila1}</tr><tr><th>30</th>{fila30}</tr></table></body></html>")


def test_parsea_formato_chileno_y_salta_dias_inexistentes():
    v = parsear_tabla_uf_sii(_html_sii(), 2022)
    assert v["2022-01-01"] == 30996.73
    assert v["2022-01-30"] == 31100.0
    assert "2022-02-30" not in v
    assert len(v) == 12 + 11


def test_html_sin_tabla_lanza_value_error():
    with pytest.raises(ValueError):
        parsear_tabla_uf_sii("<html></html>", 2022)


def _conn():
    conn = sqlite3.connect(":memory:")
    crear_tablas(conn)
    conn.execute("INSERT INTO libros (id, titulo, autor) VALUES (1, 'dune', 'herbert')")
    conn.execute("INSERT INTO precios (libro_id, precio_actual, fecha) VALUES (1, 100, '2025-03-01 10:00:00')")
    return conn


def test_actualizar_uf_descarga_años_faltantes_y_cachea(monkeypatch):
    pedidos = []

    def fake(anio):
        pedidos.append(anio)
        return {f"{anio}-01-01": 1.0, f"{anio}-10-12": 2.0}

    monkeypatch.setattr(indicadores, "descargar_uf_anio", fake)
    conn = _conn()
    assert actualizar_uf(conn, hoy=date(2026, 10, 12)) == 4
    assert pedidos == [2025, 2026]
    assert cargar_uf(conn)["2026-10-12"] == 2.0

    pedidos.clear()
    actualizar_uf(conn, hoy=date(2026, 10, 12))
    assert pedidos == [2025]  # 2025 incompleto (<365 días) se reintenta; 2026 ya tiene el día de hoy


def test_actualizar_uf_no_lanza_si_el_sii_falla(monkeypatch, caplog):
    def falla(anio):
        raise requests.ConnectionError("sin red")

    monkeypatch.setattr(indicadores, "descargar_uf_anio", falla)
    assert actualizar_uf(_conn(), hoy=date(2026, 10, 12)) == 0
    assert "No se pudo obtener la UF" in caplog.text


def test_cargar_uf_sin_tabla_devuelve_vacio():
    assert cargar_uf(sqlite3.connect(":memory:")) == {}
