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


def _seed_precios(db_path, titulo, filas):
    """filas: lista de (precio_actual, precio_antes, descuento, fecha)."""
    import sqlite3
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO libros (titulo, autor) VALUES (?, 'autor')", (titulo,))
    libro_id = cursor.lastrowid
    cursor.executemany(
        "INSERT INTO precios (libro_id, precio_actual, precio_antes, descuento, estado, fecha) "
        "VALUES (?, ?, ?, ?, 'disponible', ?)",
        [(libro_id, *f) for f in filas],
    )
    conn.commit()
    conn.close()


def test_stats_cuenta_cambios_reales_y_usa_ultimo_precio(client):
    import re
    import dashboard
    # "estable": 4 registros con el mismo precio → 0 cambios.
    _seed_precios(dashboard.DB_PATH, "estable", [
        (10000, 20000, "50%", f"2026-10-0{d} 10:00:00") for d in range(1, 5)
    ])
    # "variable": 3 registros 10000 → 9000 → 10000 → 2 cambios; su último
    # ahorro es 20000-10000, no el de la fila intermedia (21000).
    _seed_precios(dashboard.DB_PATH, "variable", [
        (10000, 20000, "50%", "2026-10-01 10:00:00"),
        (9000, 30000, "70%", "2026-10-02 10:00:00"),
        (10000, 20000, "50%", "2026-10-03 10:00:00"),
    ])

    html = client.get("/stats").data.decode("utf-8")

    filas_cambios = re.findall(r"<td[^>]*>(Estable|Variable)</td>.*?<td class=\"text-end fw-bold\">(\d+)</td>", html, re.S)
    assert ("Variable", "2") in filas_cambios
    assert "$21.000" not in html  # ahorro de una fila vieja
    assert "70%" not in html      # descuento de una fila vieja
