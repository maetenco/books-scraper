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


# ── Fase 3: evaluación por libro y eventos de tienda ──

def _seed_libro_con_historial(db_path, titulo, precios_previos, precio_hoy, lista=True):
    """Un precio por día hasta 2026-10-12 (hoy = último). Devuelve libro_id."""
    import sqlite3
    from datetime import date, timedelta
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("INSERT INTO libros (titulo, autor) VALUES (?, 'autor')", (titulo,))
    libro_id = cur.lastrowid
    if lista:
        cur.execute("INSERT OR IGNORE INTO listas (lista_id, nombre) VALUES ('L1', 'Mi lista')")
        cur.execute("INSERT INTO libros_listas (libro_id, lista_id) SELECT ?, id FROM listas WHERE lista_id = 'L1'", (libro_id,))
    hoy = date(2026, 10, 12)
    todos = precios_previos + [precio_hoy]
    for i, precio in enumerate(todos):
        dia = hoy - timedelta(days=len(todos) - 1 - i)
        cur.execute(
            "INSERT INTO precios (libro_id, precio_actual, precio_antes, descuento, estado, fecha) "
            "VALUES (?, ?, 40000, '50%', 'disponible', ?)", (libro_id, precio, f"{dia} 10:00:00"))
    conn.commit()
    conn.close()
    return libro_id


def test_libro_muestra_evaluacion_minimo_del_año(client):
    import dashboard
    libro_id = _seed_libro_con_historial(dashboard.DB_PATH, "dune", [20000] * 40, 18000)
    html = client.get(f"/libro/{libro_id}").data.decode("utf-8")
    assert "Mínimo histórico 🏆" in html or "Mínimo del año ✅" in html
    assert "precio más bajo del año ($18.000)" in html


def test_todos_filtra_solo_buenos_precios(client):
    import dashboard
    _seed_libro_con_historial(dashboard.DB_PATH, "barato", [20000] * 40, 18000)
    _seed_libro_con_historial(dashboard.DB_PATH, "caro", list(range(10000, 50000, 1000)), 48000)

    todos = client.get("/todos").data.decode("utf-8").lower()
    assert "barato" in todos and "caro" in todos
    buenos = client.get("/todos?buenos=1").data.decode("utf-8").lower()
    assert "barato" in buenos
    assert ">caro<" not in buenos and "caro\n" not in buenos.split("<tbody>")[1]


def test_stats_muestra_eventos_de_tienda(client):
    import sqlite3
    import dashboard
    from analisis import MIN_LIBROS_DIA
    conn = sqlite3.connect(dashboard.DB_PATH)
    cur = conn.cursor()
    # MIN_LIBROS_DIA libros escaneados 12 días: 42% habitual, el día 11 sube a 50%.
    for n in range(MIN_LIBROS_DIA):
        cur.execute("INSERT INTO libros (titulo, autor) VALUES (?, 'a')", (f"libro {n}",))
        lid = cur.lastrowid
        for d in range(1, 13):
            dcto = "50%" if d == 11 else "42%"
            cur.execute("INSERT INTO precios (libro_id, precio_actual, descuento, estado, fecha) "
                        "VALUES (?, 10000, ?, 'disponible', ?)", (lid, dcto, f"2026-09-{d:02d} 10:00:00"))
    conn.commit()
    conn.close()

    html = client.get("/stats").data.decode("utf-8")
    assert "Rebajas de la tienda" in html
    assert "El 11/09/26 fue una buena rebaja de la tienda" in html
