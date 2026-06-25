import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Test de rutas flask


def test_index(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"Books Dashboard" in resp.data or b"Libros" in resp.data


def test_stats(client):
    resp = client.get("/stats")
    assert resp.status_code == 200


def test_todos(client):
    resp = client.get("/todos")
    assert resp.status_code == 200
    assert b"Todos" in resp.data


def test_libro_no_encontrado(client):
    resp = client.get("/libro/999")
    assert resp.status_code == 404


def test_export_csv(client):
    resp = client.get("/todos/export")
    assert resp.status_code == 200
    assert "text/csv" in resp.content_type
