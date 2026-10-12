"""
Tests de run.py: la marca .last_run solo se escribe si la corrida
escaneó libros, y los fallos no la escriben. Sin red ni Telegram:
scrap_main y las notificaciones se reemplazan por dobles.
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import run


@pytest.fixture
def entorno(monkeypatch, tmp_path):
    marcas = []
    monkeypatch.setattr(run, "ya_corrio_hoy", lambda: False)
    monkeypatch.setattr(run, "marcar_ejecucion_hoy", lambda: marcas.append(True))
    monkeypatch.setattr(run, "DB_PATH", str(tmp_path / "test.db"))
    for nombre in ("notificar_bajas", "notificar_resumen", "notificar_ofertas_flash", "notificar_fallo"):
        monkeypatch.setattr(run, nombre, lambda *a, **k: True)
    monkeypatch.setattr(run, "detectar_ofertas_flash", lambda conn: [])
    monkeypatch.setattr(run, "actualizar_uf", lambda conn: 0)  # sin red (SII)
    return marcas


def _scrap_que_devuelve(resultado):
    async def fake(headless=True):
        return resultado
    return fake


def test_corrida_con_libros_marca_ejecucion(entorno, monkeypatch):
    monkeypatch.setattr(run, "scrap_main", _scrap_que_devuelve(([], 586)))
    asyncio.run(run.run(run.parsear_args(["--no-telegram"])))
    assert entorno == [True]


def test_corrida_sin_libros_falla_y_no_marca(entorno, monkeypatch):
    monkeypatch.setattr(run, "scrap_main", _scrap_que_devuelve(([], 0)))
    with pytest.raises(RuntimeError):
        asyncio.run(run.run(run.parsear_args(["--no-telegram"])))
    assert entorno == []


def test_sesion_expirada_no_marca(entorno, monkeypatch):
    async def fake(headless=True):
        raise run.SesionExpirada("sin sesión")
    monkeypatch.setattr(run, "scrap_main", fake)
    with pytest.raises(run.SesionExpirada):
        asyncio.run(run.run(run.parsear_args([])))
    assert entorno == []
