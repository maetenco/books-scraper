"""
Tests del algoritmo de recomendación de compra (calcular_recomendacion,
extraído de ver_libro() en dashboard.py). Cubre las 4 ramas posibles:
"Mejor momento", "Espera" (bajando), "Subiendo", "Sin tendencia clara".

Cada caso se construye como una lista de dicts con clave
"precio_actual", ordenada por fecha DESCENDENTE (el más reciente
primero, índice 0) — el mismo orden que entrega la query real de
ver_libro().
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dashboard import calcular_recomendacion


def test_sin_precios_no_recomienda():
    assert calcular_recomendacion([]) is None


def test_todos_los_precios_nulos_no_recomienda():
    assert calcular_recomendacion([{"precio_actual": None}, {"precio_actual": None}]) is None


def test_mejor_momento_precio_actual_igual_al_minimo_historico():
    # Más reciente primero: 15000 (actual = mínimo) <- 15000 <- 20000 (más antiguo)
    precios = [{"precio_actual": 15000}, {"precio_actual": 15000}, {"precio_actual": 20000}]
    recomendacion = calcular_recomendacion(precios)
    assert recomendacion["label"] == "Mejor momento ✅"
    assert recomendacion["class"] == "success"


def test_mejor_momento_dentro_del_3_por_ciento_del_minimo():
    # Mínimo histórico 10000, actual 10250 -> ratio 1.025 (<=1.03)
    precios = [{"precio_actual": 10250}, {"precio_actual": 10000}, {"precio_actual": 15000}]
    recomendacion = calcular_recomendacion(precios)
    assert recomendacion["label"] == "Mejor momento ✅"


def test_espera_precio_bajando_pero_lejos_del_minimo_historico():
    # Ventana reciente (los últimos 5) muestra una baja clara: 20000 -> 16000.
    # El mínimo histórico (10000) quedó fuera de la ventana, más antiguo,
    # así que no participa del cálculo de tendencia, solo del ratio.
    precios = [
        {"precio_actual": 16000},  # más reciente
        {"precio_actual": 18000},
        {"precio_actual": 20000},
        {"precio_actual": 20000},
        {"precio_actual": 20000},  # límite de la ventana (5to)
        {"precio_actual": 10000},  # mínimo histórico, fuera de la ventana
    ]
    recomendacion = calcular_recomendacion(precios)
    assert recomendacion["label"] == "Espera 📉"
    assert recomendacion["class"] == "info"


def test_subiendo_precio_al_alza():
    # Mínimo histórico 10000 (ya pasado); ventana reciente sube: 14000 -> 18000
    precios = [
        {"precio_actual": 18000},  # más reciente
        {"precio_actual": 14000},
        {"precio_actual": 10000},  # mínimo histórico, más antiguo
    ]
    recomendacion = calcular_recomendacion(precios)
    assert recomendacion["label"] == "Subiendo 📈"
    assert recomendacion["class"] == "warning"


def test_sin_tendencia_clara_precio_estable_lejos_del_minimo():
    # Ventana reciente (los últimos 5) empieza y termina en el mismo
    # valor (18000) -> trend "stable". El mínimo histórico (10000)
    # queda fuera de la ventana, más antiguo, lejos del precio actual.
    precios = [
        {"precio_actual": 18000},  # más reciente
        {"precio_actual": 18000},
        {"precio_actual": 18000},
        {"precio_actual": 19000},
        {"precio_actual": 18000},  # límite de la ventana (5to)
        {"precio_actual": 10000},  # mínimo histórico, fuera de la ventana
    ]
    recomendacion = calcular_recomendacion(precios)
    assert recomendacion["label"] == "Sin tendencia clara"
    assert recomendacion["class"] == "secondary"


def test_un_solo_precio_valido_en_ventana_reciente_da_stable():
    # Solo un precio no-nulo dentro de la ventana reciente (los otros 4
    # son None) -> no hay suficientes puntos para calcular tendencia,
    # cae a "stable" por defecto. El mínimo histórico queda fuera de
    # la ventana (6to registro), lejos del precio actual.
    precios = [
        {"precio_actual": 18000},  # más reciente (único válido en la ventana)
        {"precio_actual": None},
        {"precio_actual": None},
        {"precio_actual": None},
        {"precio_actual": None},  # límite de la ventana (5to)
        {"precio_actual": 10000},  # mínimo histórico, fuera de la ventana
    ]
    recomendacion = calcular_recomendacion(precios)
    assert recomendacion["label"] == "Sin tendencia clara"


def test_ventana_de_tendencia_limitada_a_5_precios_recientes():
    # 6 precios: el 6to (el más antiguo, fuera de la ventana de 5) no
    # debe influir en el cálculo de tendencia.
    precios = [
        {"precio_actual": 20000},  # más reciente
        {"precio_actual": 18000},
        {"precio_actual": 18000},
        {"precio_actual": 18000},
        {"precio_actual": 18000},  # límite de la ventana (5to)
        {"precio_actual": 5000},   # 6to, fuera de la ventana, no debe importar
    ]
    recomendacion = calcular_recomendacion(precios)
    assert recomendacion["label"] == "Subiendo 📈"
