"""
Tests de telegram.py: envío (escape HTML, 429, rechazo) y alerta de
fallo, sin red (requests.post mockeado).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from utils import formatear_precio


def test_formatear_precio_normal():
    assert formatear_precio(12990) == "$12.990"


def test_formatear_precio_none():
    assert formatear_precio(None) == "—"


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


def test_esc_escapa_html_y_comillas():
    assert telegram.esc('tom & jerry <ed> "x"') == "tom &amp; jerry &lt;ed&gt; &quot;x&quot;"


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
