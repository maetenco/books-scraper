"""
Tests de analisis.py (motor de evaluación de precios y eventos de tienda)
con series sintéticas: sin red ni DB.
"""
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import analisis
from analisis import (
    descuento_pct, detectar_eventos, evaluar_libro, precios_diarios, redactar_evento,
    serie_descuento_diario, uf_del_dia,
)

HOY = date(2026, 10, 12)


def _fila(dias_atras, precio, estado="disponible", hora="10:00:00"):
    dia = (HOY - timedelta(days=dias_atras)).isoformat()
    return {"fecha": f"{dia} {hora}", "precio_actual": precio, "estado": estado}


def _historial(precios_previos, hoy):
    """Un precio por día previo (del más antiguo al más reciente) + el de hoy."""
    n = len(precios_previos)
    return [_fila(n - i, p) for i, p in enumerate(precios_previos)] + [_fila(0, hoy)]


def test_descuento_pct():
    assert descuento_pct("50%") == 50
    assert descuento_pct("45% OFF") == 45
    assert descuento_pct("Sin descuento") == 0
    assert descuento_pct("Sin stock") is None
    assert descuento_pct(None) is None


def test_precios_diarios_toma_el_minimo_del_dia_e_ignora_sin_stock():
    filas = [_fila(1, 20000), _fila(1, 18000, hora="20:00:00"), _fila(0, None, estado="sin_stock")]
    assert precios_diarios(filas) == [((HOY - timedelta(days=1)).isoformat(), 18000)]


def test_sin_registros_no_evalua():
    assert evaluar_libro([]) is None


def test_ultimo_sin_stock():
    ev = evaluar_libro([_fila(1, 20000), _fila(0, None, estado="sin_stock")])
    assert ev["nivel"] == "sin_stock"


def test_historial_corto():
    ev = evaluar_libro(_historial([20000] * (analisis.MIN_DIAS_HISTORIAL - 1), 19000))
    assert ev["nivel"] == "pocos_datos"


def test_minimo_del_año_sin_uf():
    ev = evaluar_libro(_historial([20000] * 40 + [18000], 17500))
    assert ev["nivel"] == "min_anual"
    assert ev["min_anual"] == 18000
    assert "min_hist_ajustado" not in ev  # sin UF no se inventa el nivel ajustado


def test_minimo_historico_ajustado_por_uf():
    # Hace 2 años costaba 15000; con UF +20% eso son 18000 de hoy.
    hace_dos_años = HOY - timedelta(days=730)
    viejo = {"fecha": f"{hace_dos_años.isoformat()} 10:00:00", "precio_actual": 15000, "estado": "disponible"}
    uf = {hace_dos_años.isoformat(): 30000.0, HOY.isoformat(): 36000.0}

    ev = evaluar_libro([viejo] + _historial([19000] * 40 + [17800], 17600), uf)
    assert ev["nivel"] == "min_historico"  # 17600 ≤ 18000 ajustado
    assert ev["min_hist_ajustado"] == 18000
    assert ev["min_hist_nominal"] == 15000

    ev = evaluar_libro([viejo] + _historial([19000] * 40 + [18500], 18200), uf)
    assert ev["nivel"] == "min_anual"  # mínimo del año, pero 18200 > 18000 ajustado
    assert "Mínimo histórico ajustado por inflación: $18.000" in ev["detail"]


def test_buen_precio_normal_y_caro_por_percentil():
    base = list(range(10000, 50000, 1000))  # 40 días, precios crecientes
    assert evaluar_libro(_historial(base, 12500))["nivel"] == "buen_precio"
    assert evaluar_libro(_historial(base, 30000))["nivel"] == "normal"
    assert evaluar_libro(_historial(base, 45500))["nivel"] == "caro"


def test_baja_vs_min_anual_para_alertas():
    ev = evaluar_libro(_historial([20000] * 40, 19400))
    assert abs(ev["baja_vs_min_anual"] - 0.03) < 1e-9


def test_uf_del_dia_usa_el_dia_anterior_si_falta():
    uf = {"2026-10-10": 39000.0}
    assert uf_del_dia(uf, "2026-10-12") == 39000.0
    assert uf_del_dia(uf, "2026-12-31") is None


# ── Eventos de tienda ──

def _serie(medias, n=500):
    inicio = date(2026, 8, 1)
    return [((inicio + timedelta(days=i)).isoformat(), m, n) for i, m in enumerate(medias)]


def test_detecta_evento_claro_y_no_el_ruido():
    medias = [42.0, 41.5, 42.5, 41.8, 42.2, 41.9, 42.1, 42.3, 41.7, 45.3, 42.0]
    marcados = [e["dia"] for e in detectar_eventos(_serie(medias)) if e["es_evento"]]
    assert marcados == ["2026-08-10"]  # solo el 45,3%


def test_sin_suficientes_previos_no_hay_evento():
    assert not any(e["es_evento"] for e in detectar_eventos(_serie([42.0, 42.0, 60.0])))


def test_dias_con_pocos_libros_se_ignoran():
    serie = _serie([42.0] * 10) + [("2026-08-20", 70.0, analisis.MIN_LIBROS_DIA - 1)]
    assert all(e["dia"] != "2026-08-20" for e in detectar_eventos(serie))


def test_serie_descuento_cuenta_cada_libro_una_vez_por_dia():
    filas = [
        {"libro_id": 1, "fecha": "2026-08-18 10:00:00", "descuento": "40%", "estado": "disponible"},
        {"libro_id": 1, "fecha": "2026-08-18 20:00:00", "descuento": "60%", "estado": "disponible"},
        {"libro_id": 2, "fecha": "2026-08-18 10:00:00", "descuento": "50%", "estado": "disponible"},
        {"libro_id": 3, "fecha": "2026-08-18 10:00:00", "descuento": "Sin stock", "estado": "sin_stock"},
    ]
    assert serie_descuento_diario(filas) == [("2026-08-18", 55.0, 2)]  # (60 + 50) / 2


def test_redactar_evento():
    texto = redactar_evento({"dia": "2026-10-06", "descuento_medio": 45.33, "habitual": 41.6, "n_libros": 580})
    assert texto == ("El 06/10/26 fue una buena rebaja de la tienda: descuento medio "
                     "45,3% vs 41,6% habitual (580 libros).")
