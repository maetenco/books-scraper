"""
Tests de alertas.py (Fase 4) y de run.enviar_alertas(): nuevos mínimos con
umbral exacto, no repetir avisos, evento solo el primer día, resumen una
vez por semana y escape HTML. Sin red: Telegram mockeado.
"""
import os
import sqlite3
import sys
from datetime import date, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import alertas
import run
from alertas import calcular_alertas, redactar_mensajes, registrar_notificaciones
from analisis import MIN_LIBROS_DIA
from scrap import crear_tablas

HOY = date(2026, 10, 13)  # martes


def _conn():
    conn = sqlite3.connect(":memory:")
    crear_tablas(conn)
    return conn


def _libro(conn, titulo, previos, hoy, dcto_previo="42%", dcto_hoy="42%", dia_hoy=HOY):
    """Un precio por día durante len(previos) días antes de dia_hoy, más el de dia_hoy."""
    cur = conn.cursor()
    cur.execute("INSERT INTO libros (titulo, autor, url) VALUES (?, 'autor', 'https://x/1')", (titulo,))
    lid = cur.lastrowid
    n = len(previos)
    for i, p in enumerate(previos):
        dia = dia_hoy - timedelta(days=n - i)
        cur.execute("INSERT INTO precios (libro_id, precio_actual, descuento, estado, fecha) "
                    "VALUES (?, ?, ?, 'disponible', ?)", (lid, p, dcto_previo, f"{dia} 10:00:00"))
    cur.execute("INSERT INTO precios (libro_id, precio_actual, descuento, estado, fecha) "
                "VALUES (?, ?, ?, 'disponible', ?)", (lid, hoy, dcto_hoy, f"{dia_hoy} 10:00:00"))
    conn.commit()
    return lid


def test_nuevo_minimo_respeta_umbral_exacto_del_3_por_ciento():
    conn = _conn()
    _libro(conn, "justo 3%", [10000] * 40, 9700)
    _libro(conn, "solo 2,9%", [10000] * 40, 9710)
    a = calcular_alertas(conn, hoy=HOY)
    assert [n["titulo"] for n in a["nuevos_minimos"]] == ["justo 3%"]


def test_nuevo_minimo_no_se_repite_tras_registrarlo():
    conn = _conn()
    _libro(conn, "dune", [10000] * 40, 9000)
    mensajes = redactar_mensajes(calcular_alertas(conn, hoy=HOY))
    registrar_notificaciones(conn, mensajes[0][1])
    assert calcular_alertas(conn, hoy=HOY)["nuevos_minimos"] == []


def test_historial_corto_no_alerta():
    conn = _conn()
    _libro(conn, "nuevo", [10000] * 10, 5000)
    assert calcular_alertas(conn, hoy=HOY)["nuevos_minimos"] == []


def _catalogo_con_evento(conn, dias_evento):
    """MIN_LIBROS_DIA libros, 12 días al 42% salvo `dias_evento` (índices 0–11) al 50%."""
    cur = conn.cursor()
    for n in range(MIN_LIBROS_DIA):
        cur.execute("INSERT INTO libros (titulo, autor) VALUES (?, 'a')", (f"libro {n}",))
        lid = cur.lastrowid
        for i in range(12):
            dia = HOY - timedelta(days=11 - i)
            dcto = "50%" if i in dias_evento else "42%"
            cur.execute("INSERT INTO precios (libro_id, precio_actual, descuento, estado, fecha) "
                        "VALUES (?, 10000, ?, 'disponible', ?)", (lid, dcto, f"{dia} 10:00:00"))
    conn.commit()


def test_evento_se_avisa_el_primer_dia():
    conn = _conn()
    _catalogo_con_evento(conn, dias_evento={11})
    a = calcular_alertas(conn, hoy=HOY)
    assert a["evento"]["dia"] == HOY.isoformat()
    texto = redactar_mensajes(a)[0][0]
    assert "HOY HAY REBAJA EN LA TIENDA" in texto


def test_evento_no_se_repite_el_segundo_dia():
    conn = _conn()
    _catalogo_con_evento(conn, dias_evento={10, 11})
    assert calcular_alertas(conn, hoy=HOY)["evento"] is None


def test_resumen_semanal_una_vez_por_semana(monkeypatch):
    conn = _conn()
    _libro(conn, "dune", [10000] * 40, 10000)
    a = calcular_alertas(conn, hoy=HOY)
    assert a["resumen"] is not None
    registrar_notificaciones(conn, [a["claves_resumen"]])
    assert calcular_alertas(conn, hoy=HOY + timedelta(days=1))["resumen"] is None   # misma semana
    assert calcular_alertas(conn, hoy=HOY + timedelta(days=6))["resumen"] is not None  # lunes siguiente

    monkeypatch.setattr(alertas, "DIA_RESUMEN_SEMANAL", 4)  # viernes
    conn2 = _conn()
    _libro(conn2, "dune", [10000] * 40, 10000)
    assert calcular_alertas(conn2, hoy=HOY)["resumen"] is None  # martes < viernes


def test_mensaje_agrupado_escapa_html_y_limita_cantidad():
    conn = _conn()
    for i in range(alertas.MAX_LIBROS_MENSAJE + 3):
        _libro(conn, f"tom & jerry <{i}>", [10000] * 40, 9000)
    texto = redactar_mensajes(calcular_alertas(conn, hoy=HOY))[0][0]
    assert "Tom &amp; jerry &lt;0&gt;" in texto
    assert "<0>" not in texto
    assert "…y 3 más" in texto
    assert f"{alertas.MAX_LIBROS_MENSAJE + 3} libros en nuevo mínimo del año" in texto


# ── run.enviar_alertas(): registra solo si Telegram aceptó ──

def test_enviar_alertas_registra_solo_si_se_envio(monkeypatch):
    conn = _conn()
    _libro(conn, "dune", [10000] * 40, 9000)

    monkeypatch.setattr(run, "enviar_mensaje", lambda texto: False)
    run.enviar_alertas(conn)
    assert conn.execute("SELECT COUNT(*) FROM notificaciones").fetchone()[0] == 0  # se reintentará

    enviados = []
    monkeypatch.setattr(run, "enviar_mensaje", lambda texto: enviados.append(texto) or True)
    run.enviar_alertas(conn)
    assert len(enviados) == 2  # nuevos mínimos + resumen semanal
    assert conn.execute("SELECT COUNT(*) FROM notificaciones").fetchone()[0] == 2


def test_preview_no_envia_ni_registra(monkeypatch):
    conn = _conn()
    _libro(conn, "dune", [10000] * 40, 9000)
    monkeypatch.setattr(run, "enviar_mensaje", lambda texto: pytest.fail("preview no debe enviar"))
    textos = run.enviar_alertas(conn, enviar=False)
    assert len(textos) == 2
    assert conn.execute("SELECT COUNT(*) FROM notificaciones").fetchone()[0] == 0
