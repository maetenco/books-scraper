"""
Tests de la capa de parsing HTML de scrap.py con fixtures reales
(guardadas en tests/fixtures/), sin red.

Cubre:
- extraer_libros(): parsing de la página de wishlist (síncrono, puro).
- extraer_precio_producto(): parsing de la página de producto
  individual (async, depende de Playwright). Para no depender de red
  ni de un navegador real, se usan clases "fake" (FakePage/FakeContext)
  que imitan la interfaz mínima de BrowserContext/Page usada por la
  función (new_page, goto, wait_for_selector, content, close) y
  devuelven el HTML de la fixture directamente.
"""
import asyncio
import os
import sys

from bs4 import BeautifulSoup

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scrap import extraer_libros, extraer_precio_producto

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


def _leer_fixture(nombre: str) -> str:
    with open(os.path.join(FIXTURES_DIR, nombre), encoding="utf-8") as f:
        return f.read()


class FakePage:
    """Doble de prueba de playwright.async_api.Page: sin red, sin navegador."""

    def __init__(self, html: str):
        self._html = html

    async def goto(self, url, wait_until=None, timeout=None):
        return None

    async def wait_for_selector(self, selector, timeout=None):
        return None

    async def content(self) -> str:
        return self._html

    async def close(self):
        return None


class FakeContext:
    """Doble de prueba de playwright.async_api.BrowserContext."""

    def __init__(self, html: str):
        self._html = html

    async def new_page(self) -> FakePage:
        return FakePage(self._html)


def _extraer_precio(nombre_fixture: str) -> dict | None:
    html = _leer_fixture(nombre_fixture)
    contexto = FakeContext(html)
    return asyncio.run(extraer_precio_producto("https://www.buscalibre.cl/fake", contexto))


# ── extraer_libros() ──

def test_extraer_libros_caso_completo():
    soup = BeautifulSoup(_leer_fixture("lista_deseos.html"), "html.parser")
    libros = extraer_libros(soup)
    assert len(libros) == 4

    libro1 = libros[0]
    assert libro1["titulo"] == "el nombre del viento"
    assert libro1["autor"] == "patrick rothfuss"
    assert libro1["precio_antes"] == 19990
    assert libro1["precio_actual"] == 15990
    assert libro1["descuento"] == "20% OFF"
    assert libro1["url"] == "https://www.buscalibre.cl/libro-1"
    assert libro1["imagen_url"] == "https://img.buscalibre.cl/portada1.jpg"


def test_extraer_libros_sin_descuento_url_relativa():
    soup = BeautifulSoup(_leer_fixture("lista_deseos.html"), "html.parser")
    libros = extraer_libros(soup)
    libro2 = libros[1]
    assert libro2["titulo"] == "fundación"
    assert libro2["precio_antes"] is None
    assert libro2["precio_actual"] == 12500
    assert libro2["descuento"] == "Sin descuento"
    assert libro2["url"] == "https://www.buscalibre.cl/libro-2"
    assert libro2["imagen_url"] == "https://www.buscalibre.cl/portada2.jpg"


def test_extraer_libros_sin_precio_dispara_lazy_scraping():
    soup = BeautifulSoup(_leer_fixture("lista_deseos.html"), "html.parser")
    libros = extraer_libros(soup)
    libro3 = libros[2]
    assert libro3["titulo"] == "dune"
    assert libro3["precio_actual"] is None
    assert libro3["url"] == "https://www.buscalibre.cl/libro-3"


def test_extraer_libros_campos_faltantes_usa_na():
    soup = BeautifulSoup(_leer_fixture("lista_deseos.html"), "html.parser")
    libros = extraer_libros(soup)
    libro4 = libros[3]
    assert libro4["titulo"] == "N/A"
    assert libro4["autor"] == "N/A"
    assert libro4["url"] == ""
    assert libro4["imagen_url"] == ""


# ── extraer_precio_producto(): 3 fallbacks + sin stock ──

def test_extraer_precio_producto_fallback_json():
    resultado = _extraer_precio("producto_json.html")
    assert resultado == {"precio_actual": 15990, "precio_antes": 19990, "descuento": "20%"}


def test_extraer_precio_producto_fallback_colprecio():
    resultado = _extraer_precio("producto_colprecio.html")
    assert resultado == {"precio_actual": 12990, "precio_antes": 15990, "descuento": "15% OFF"}


def test_extraer_precio_producto_fallback_colprecios():
    resultado = _extraer_precio("producto_colprecios.html")
    assert resultado == {"precio_actual": 9990, "precio_antes": 11990, "descuento": "Sin descuento"}


def test_extraer_precio_producto_sin_stock():
    resultado = _extraer_precio("producto_sin_stock.html")
    assert resultado == {"sin_stock": True}
