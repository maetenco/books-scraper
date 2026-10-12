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


# ── enviar_mensaje(): escape, 429 y rechazo (sin red: requests.post mockeado) ──

import pytest
import telegram


class _Resp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = str(self._payload)

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        return self._payload


@pytest.fixture
def envios(monkeypatch):
    """Captura los POST a Telegram y evita esperas reales."""
    enviados = []
    respuestas = []

    def fake_post(url, json=None, timeout=None):
        enviados.append(json["text"])
        return respuestas.pop(0) if respuestas else _Resp(200, {"ok": True})

    monkeypatch.setattr(telegram, "TOKEN", "t")
    monkeypatch.setattr(telegram, "CHAT_ID", "c")
    monkeypatch.setattr(telegram.requests, "post", fake_post)
    monkeypatch.setattr(telegram.time, "sleep", lambda s: None)
    return enviados, respuestas


def test_notificar_bajas_escapa_html_de_titulos(envios):
    enviados, _ = envios
    telegram.notificar_bajas([{
        "titulo": "tom & jerry <edición>", "autor": "a & b", "precio_viejo": 30000,
        "precio_nuevo": 20000, "descuento": "50%", "url": "https://x/?a=1&b=2",
    }])
    assert len(enviados) == 1
    assert "Tom &amp; Jerry &lt;Edición&gt;" in enviados[0]
    assert "<edición>" not in enviados[0].lower()
    assert 'href="https://x/?a=1&amp;b=2"' in enviados[0]


def test_enviar_mensaje_reintenta_tras_429(envios):
    enviados, respuestas = envios
    respuestas.append(_Resp(429, {"ok": False, "parameters": {"retry_after": 3}}))
    assert telegram.enviar_mensaje("hola") is True
    assert len(enviados) == 2


def test_enviar_mensaje_rechazado_retorna_false(envios, caplog):
    enviados, respuestas = envios
    respuestas.append(_Resp(400, {"ok": False, "description": "Bad Request: can't parse entities"}))
    assert telegram.enviar_mensaje("<b>roto") is False
    assert "can't parse entities" in caplog.text


def test_notificar_fallo_sesion_expirada_indica_login(envios):
    enviados, _ = envios
    telegram.notificar_fallo(Exception("sin listas <url>"), sesion_expirada=True)
    assert "run.py --login" in enviados[0]
    assert "&lt;url&gt;" in enviados[0]
