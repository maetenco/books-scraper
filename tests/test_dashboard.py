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


def test_lista_no_encontrada(client):
    resp = client.get("/lista/999")
    assert resp.status_code == 404


def test_ver_lista_muestra_libros_de_la_lista(client):
    import dashboard
    from scrap import guardar_libro
    import sqlite3

    conn = sqlite3.connect(dashboard.DB_PATH)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO listas (lista_id, nombre) VALUES ('L1', 'Mi lista')")
    conn.commit()
    cursor.execute("SELECT id FROM listas WHERE lista_id = 'L1'")
    id_lista = cursor.fetchone()[0]
    guardar_libro(conn, {
        "titulo": "dune", "autor": "frank herbert", "precio_actual": 20000,
        "precio_antes": None, "descuento": "Sin descuento", "estado": "disponible",
        "url": "", "imagen_url": "",
    }, id_lista)
    conn.close()

    resp = client.get(f"/lista/{id_lista}")
    assert resp.status_code == 200
    assert b"dune" in resp.data.lower()


def test_export_csv(client):
    resp = client.get("/todos/export")
    assert resp.status_code == 200
    assert "text/csv" in resp.content_type
