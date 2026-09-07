"""
Tests de _corregir_precio_libro() y de la paralelización del lazy
scraping en scrap.main() (Fase 4, punto 17). Reutiliza los dobles de
prueba (FakePage/FakeContext) de test_scrap_html.py.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scrap import _corregir_precio_libro, MAX_CONCURRENCIA_LAZY_SCRAPING
from tests.test_scrap_html import FakeContext, _leer_fixture


def _libro(url: str = "https://www.buscalibre.cl/fake", precio_actual=None) -> dict:
    return {
        "titulo": "dune", "autor": "frank herbert",
        "precio_actual": precio_actual, "precio_antes": None,
        "descuento": "Sin descuento", "estado": "disponible",
        "url": url, "imagen_url": "",
    }


def test_sin_url_no_hace_nada():
    libro = _libro(url="")
    resultado = asyncio.run(
        _corregir_precio_libro(libro, 1, 1, FakeContext(""), asyncio.Semaphore(1))
    )
    assert resultado is False
    assert libro["precio_actual"] is None


def test_corrige_precio_cuando_encuentra_uno():
    libro = _libro()
    contexto = FakeContext(_leer_fixture("producto_colprecio.html"))
    resultado = asyncio.run(
        _corregir_precio_libro(libro, 1, 1, contexto, asyncio.Semaphore(1))
    )
    assert resultado is True
    assert libro["precio_actual"] == 12990
    assert libro["estado"] == "disponible"


def test_marca_sin_stock():
    libro = _libro()
    contexto = FakeContext(_leer_fixture("producto_sin_stock.html"))
    resultado = asyncio.run(
        _corregir_precio_libro(libro, 1, 1, contexto, asyncio.Semaphore(1))
    )
    assert resultado is True
    assert libro["precio_actual"] is None
    assert libro["estado"] == "sin_stock"


def test_sin_cambios_si_no_hay_precio_reconocible():
    libro = _libro()
    contexto = FakeContext(_leer_fixture("producto_desconocido.html"))
    resultado = asyncio.run(
        _corregir_precio_libro(libro, 1, 1, contexto, asyncio.Semaphore(1))
    )
    assert resultado is False
    assert libro["precio_actual"] is None


class _FakePageConContador:
    """Como FakePage, pero simula una carga lenta y registra cuántas
    instancias están "en vuelo" a la vez, para verificar el límite de
    concurrencia del semáforo."""

    contador_activo = 0
    maximo_visto = 0

    def __init__(self, html: str):
        self._html = html

    async def goto(self, url, wait_until=None, timeout=None):
        return None

    async def wait_for_selector(self, selector, timeout=None):
        return None

    async def content(self) -> str:
        type(self).contador_activo += 1
        type(self).maximo_visto = max(type(self).maximo_visto, type(self).contador_activo)
        await asyncio.sleep(0.05)
        type(self).contador_activo -= 1
        return self._html

    async def close(self):
        return None


class _FakeContextConContador:
    def __init__(self, html: str):
        self._html = html

    async def new_page(self):
        return _FakePageConContador(self._html)


def test_semaforo_limita_concurrencia():
    _FakePageConContador.contador_activo = 0
    _FakePageConContador.maximo_visto = 0

    html = _leer_fixture("producto_colprecio.html")
    contexto = _FakeContextConContador(html)
    semaforo = asyncio.Semaphore(MAX_CONCURRENCIA_LAZY_SCRAPING)

    async def _correr():
        libros = [_libro() for _ in range(MAX_CONCURRENCIA_LAZY_SCRAPING * 3)]
        tareas = [
            _corregir_precio_libro(libro, i, len(libros), contexto, semaforo)
            for i, libro in enumerate(libros, 1)
        ]
        return await asyncio.gather(*tareas)

    resultados = asyncio.run(_correr())
    assert all(resultados)  # todos encontraron precio
    assert _FakePageConContador.maximo_visto <= MAX_CONCURRENCIA_LAZY_SCRAPING
