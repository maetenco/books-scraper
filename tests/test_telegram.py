from telegram import formatear_precio, _extraer_porcentaje
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
# Test de telegram.py


def test_formatear_precio_normal():
    assert formatear_precio(12990) == "$12.990"


def test_formatear_precio_none():
    assert formatear_precio(None) == "—"


def test_extraer_porcentaje_normal():
    assert _extraer_porcentaje("30% OFF") == 30


def test_extraer_porcentaje_sin_descuento():
    assert _extraer_porcentaje("Sin descuento") == 0
