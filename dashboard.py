import re
import csv
import io
import sqlite3
from datetime import datetime
from flask import Flask, render_template, jsonify, request, Response

# ═══════════════════════════════════════════════════════════════
# Conceptos de dashboard.py:
#  [1] app / DB_PATH          → Instancia Flask y ruta a SQLite.
#  [2] get_db()               → Conexión con row_factory para
#       acceder a columnas por nombre (dict-like).
#  [3] formatear_precio /     → Filtros Jinja2 para mostrar
#      formatear_fecha          precios ($12.990) y fechas (DD/MM/AA).
#  [4] sparkline()            → Genera SVG inline de minigráfico
#       de tendencia de precios (verde si baja, rojo si sube).
#  [5] index()                → Ruta "/": resumen con cards de
#       totales y grid de listas de deseos.
#  [6] ver_lista()            → Ruta "/lista/<id>": libros de una
#       wishlist específica con precios y sparklines.
#  [7] ver_todos()            → Ruta "/todos": todos los libros
#       con búsqueda, orden por columna y toggle de columnas.
#  [8] export_csv()           → Ruta "/todos/export": descarga
#       CSV de todos los libros con su último precio.
#  [9] stats()                → Ruta "/stats": tablas de libros
#       con más cambios, mayor ahorro, mayores descuentos.
# [10] ver_libro()            → Ruta "/libro/<id>": detalle con
#       gráfico Chart.js y recomendación de compra.
# [11] eliminar_libro()       → Ruta POST "/libro/<id>/eliminar":
#       borra libro y su historial de precios.
# [12] api_precios()          → Ruta "/api/precios/<id>": JSON
#       con historial para el gráfico Chart.js.
# ═══════════════════════════════════════════════════════════════

# ── [1] app Flask ──
app = Flask(__name__)
DB_PATH = "libros.db"


# ── [2] get_db: conexión SQLite con row_factory ──
# type hint: retorno -> sqlite3.Connection
# Mejora: mypy sabe que retorna una conexión real, activa autocompletado
#         de .cursor(), .commit(), .close() en el IDE.
def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ── [3] Filtros de template ──
def formatear_precio(valor: int | None) -> str:
    if valor is None:
        return "—"
    return f"${valor:,.0f}".replace(",", ".")


# type hint: fecha: str | None, retorno -> str
# Mejora: str | None cubre fechas nulas de la DB;
#         str siempre devuelve algo renderizable, evita errores en templates.
def formatear_fecha(fecha: str | None) -> str:
    if not fecha:
        return "—"
    return fecha[8:10] + "/" + fecha[5:7] + "/" + fecha[2:4]


app.jinja_env.filters["fecha"] = formatear_fecha


# type hint: precios_str: str | None, ancho: int = 80, alto: int = 24,
#           retorno -> str
# Mejora: str | None cubre libros sin historial;
#         ancho/alto tipados como int evitan pasar strings.
def sparkline(precios_str: str | None, ancho: int = 80, alto: int = 24) -> str:
    """Genera SVG inline de un mini gráfico de precios."""
    if not precios_str:
        return ""
    try:
        nums = [int(x) for x in precios_str.split(",") if x.strip()]
    except ValueError:
        return ""
    if len(nums) < 2:
        return ""
    nums = nums[::-1]  # cronológico
    minimo = min(nums)
    maximo = max(nums)
    rango = maximo - minimo if maximo != minimo else 1
    padding = 2
    w = ancho - padding * 2
    h = alto - padding * 2
    puntos = []
    for i, n in enumerate(nums):
        x = padding + (w * i / (len(nums) - 1))
        y = padding + h - (h * (n - minimo) / rango)
        puntos.append(f"{x:.1f},{y:.1f}")
    color = "#198754" if nums[-1] <= nums[0] else "#dc3545"
    return f"""<svg width="{ancho}" height="{alto}" viewBox="0 0 {ancho} {alto}" style="vertical-align:middle">
  <polyline fill="none" stroke="{color}" stroke-width="1.5" points="{" ".join(puntos)}"/>
</svg>"""


app.jinja_env.filters["sparkline"] = sparkline


# ── [5] index: página principal con resumen ──
# type hint: retorno -> str
# Mejora: render_template() retorna str; tiparlo evita confusiones
#         con otros tipos de respuesta HTTP.
@app.route("/")
def index() -> str:
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) FROM libros")
    total_libros = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM precios")
    total_precios = cursor.fetchone()[0]

    cursor.execute("SELECT MAX(fecha) FROM precios")
    ultima_actualizacion = cursor.fetchone()[0]

    cursor.execute("""
        SELECT COUNT(*) FROM precios
        WHERE id IN (SELECT MAX(id) FROM precios GROUP BY libro_id)
        AND estado = 'sin_stock'
    """)
    sin_stock_count = cursor.fetchone()[0]

    cursor.execute("""
        SELECT l.*, COUNT(ll.libro_id) as num_libros
        FROM listas l
        LEFT JOIN libros_listas ll ON l.id = ll.lista_id
        GROUP BY l.id
        ORDER BY l.nombre
    """)
    listas = cursor.fetchall()

    conn.close()
    return render_template("index.html",
                           listas=listas,
                           total_libros=total_libros,
                           total_precios=total_precios,
                           ultima_actualizacion=ultima_actualizacion,
                           sin_stock_count=sin_stock_count)


# ── [6] ver_lista: libros de una wishlist ──
# type hint: lista_id: int, retorno -> str | tuple
# Mejora: int lo recibe de la URL (Flask lo convierte automáticamente);
#         str | tuple cubre tanto el render exitoso como el 404.
@app.route("/lista/<int:lista_id>")
def ver_lista(lista_id: int) -> str | tuple:
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM listas WHERE id = ?", (lista_id,))
    lista = cursor.fetchone()
    if lista is None:
        conn.close()
        return "Lista no encontrada", 404

    cursor.execute("""
        SELECT lib.id, lib.titulo, lib.autor, lib.url, lib.imagen_url,
               p.precio_actual, p.precio_antes, p.descuento, p.estado, p.fecha,
               (SELECT MIN(precio_actual) FROM precios WHERE libro_id = lib.id AND estado = 'disponible') as precio_min,
               (SELECT fecha FROM precios WHERE libro_id = lib.id AND estado = 'disponible'
                ORDER BY precio_actual ASC, fecha ASC LIMIT 1) as fecha_min,
               (SELECT GROUP_CONCAT(precio_actual, ',') FROM (SELECT precio_actual FROM precios WHERE libro_id = lib.id AND estado = 'disponible' ORDER BY fecha DESC LIMIT 10)) as precios_hist
        FROM libros lib
        JOIN libros_listas ll ON lib.id = ll.libro_id
        LEFT JOIN precios p ON p.id = (
            SELECT id FROM precios
            WHERE libro_id = lib.id
            ORDER BY fecha DESC LIMIT 1
        )
        WHERE ll.lista_id = ?
        ORDER BY lib.titulo
    """, (lista_id,))
    libros = cursor.fetchall()

    conn.close()
    return render_template("lista.html", lista=lista, libros=libros)


# ── [7] ver_todos: todos los libros con búsqueda y orden ──
# type hint: retorno -> str
# Mejora: str indica que siempre renderiza HTML (nunca retorna error).
@app.route("/todos")
def ver_todos() -> str:
    conn = get_db()
    cursor = conn.cursor()

    sort = request.args.get("sort", "titulo")
    order = request.args.get("order", "asc")
    q = request.args.get("q", "").strip()

    SORT_COLUMNS = {"titulo", "autor", "precio", "descuento"}
    ALLOWED_ORDER = {"asc", "desc"}

    if sort not in SORT_COLUMNS:
        sort = "titulo"
    if order not in ALLOWED_ORDER:
        order = "asc"

    sort_sql = {
        "titulo": "lib.titulo",
        "autor": "lib.autor",
        "precio": "p.precio_actual",
        "descuento": "lib.titulo",
    }

    where = ""
    params = []
    if q:
        where = "WHERE (lib.titulo LIKE ? OR lib.autor LIKE ?)"
        params = [f"%{q}%", f"%{q}%"]

    cursor.execute(f"""
        SELECT lib.id, lib.titulo, lib.autor, lib.url, lib.imagen_url,
               p.precio_actual, p.precio_antes, p.descuento, p.estado, p.fecha,
               (SELECT MIN(precio_actual) FROM precios WHERE libro_id = lib.id AND estado = 'disponible') as precio_min,
               (SELECT fecha FROM precios WHERE libro_id = lib.id AND estado = 'disponible'
                ORDER BY precio_actual ASC, fecha ASC LIMIT 1) as fecha_min,
               (SELECT GROUP_CONCAT(l.nombre, ' | ') FROM listas l
                JOIN libros_listas ll2 ON ll2.lista_id = l.id
                WHERE ll2.libro_id = lib.id) as listas,
               (SELECT GROUP_CONCAT(precio_actual, ',') FROM (SELECT precio_actual FROM precios WHERE libro_id = lib.id AND estado = 'disponible' ORDER BY fecha DESC LIMIT 10)) as precios_hist
        FROM libros lib
        LEFT JOIN precios p ON p.id = (
            SELECT id FROM precios
            WHERE libro_id = lib.id
            ORDER BY fecha DESC LIMIT 1
        )
        {where}
        ORDER BY {sort_sql[sort]} {order.upper()}
    """, params)

    libros = list(cursor.fetchall())

    if sort == "descuento":
        def descuento_valor(libro):
            d = libro["descuento"]
            if d and d != "Sin descuento":
                m = re.search(r"(\d+)", d)
                if m:
                    return int(m.group(1))
            return -1
        libros.sort(key=descuento_valor, reverse=(order == "desc"))

    conn.close()
    return render_template("todos.html", libros=libros, sort=sort, order=order, q=q)


# ── [8] export_csv: descarga CSV ──
# type hint: retorno -> flask.Response
# Mejora: Response es el tipo exacto que retorna Flask para descargas;
#         el IDE autocompleta mimetype, headers y set_cookie().
@app.route("/todos/export")
def export_csv() -> Response:
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT lib.id, lib.titulo, lib.autor,
               p.precio_actual, p.precio_antes, p.descuento, p.fecha,
               (SELECT MIN(precio_actual) FROM precios WHERE libro_id = lib.id) as precio_min,
               (SELECT GROUP_CONCAT(l.nombre, ' | ') FROM listas l
                JOIN libros_listas ll2 ON ll2.lista_id = l.id
                WHERE ll2.libro_id = lib.id) as listas
        FROM libros lib
        LEFT JOIN precios p ON p.id = (
            SELECT id FROM precios
            WHERE libro_id = lib.id
            ORDER BY fecha DESC LIMIT 1
        )

        ORDER BY lib.titulo
    """)
    rows = cursor.fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Título", "Autor", "Listas", "Precio Actual", "Precio Antes", "Descuento", "Precio Más Bajo", "Última Actualización"])
    for r in rows:
        writer.writerow([
            r["titulo"],
            r["autor"],
            r["listas"] or "",
            r["precio_actual"] or "",
            r["precio_antes"] or "",
            r["descuento"] if r["descuento"] != "Sin descuento" else "",
            r["precio_min"] or "",
            r["fecha"][:10] if r["fecha"] else "",
        ])

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment;filename=libros.csv"},
    )


# ── [9] stats: estadísticas y rankings ──
# type hint: retorno -> str
# Mejora: str documenta que renderiza HTML (nunca retorna datos crudos).
@app.route("/stats")
def stats() -> str:
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) FROM libros")
    total_libros = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM precios")
    total_precios = cursor.fetchone()[0]

    cursor.execute("SELECT MAX(fecha) FROM precios")
    ultima_actualizacion = cursor.fetchone()[0]

    cursor.execute("""
        SELECT MIN(fecha) FROM precios
    """)
    primera_fecha = cursor.fetchone()[0]

    cursor.execute("""
        SELECT lib.titulo, lib.autor, COUNT(p.id) as cambios,
               (SELECT GROUP_CONCAT(l.nombre, ' | ') FROM listas l
                JOIN libros_listas ll2 ON ll2.lista_id = l.id
                WHERE ll2.libro_id = lib.id) as listas
        FROM precios p
        JOIN libros lib ON lib.id = p.libro_id
        GROUP BY p.libro_id
        ORDER BY cambios DESC LIMIT 5
    """)
    mas_cambios = cursor.fetchall()

    cursor.execute("""
        SELECT lib.titulo, lib.autor, p.precio_actual, p.precio_antes, p.descuento,
               (p.precio_antes - p.precio_actual) as ahorro,
               (SELECT GROUP_CONCAT(l.nombre, ' | ') FROM listas l
                JOIN libros_listas ll2 ON ll2.lista_id = l.id
                WHERE ll2.libro_id = lib.id) as listas
        FROM precios p
        JOIN libros lib ON lib.id = p.libro_id
        WHERE p.id IN (SELECT id FROM precios WHERE estado = 'disponible' GROUP BY libro_id HAVING MAX(fecha))
        AND p.precio_antes IS NOT NULL AND p.precio_actual IS NOT NULL
        ORDER BY ahorro DESC LIMIT 5
    """)
    mayor_ahorro = cursor.fetchall()

    cursor.execute("""
        SELECT lib.titulo, lib.autor, p.descuento, p.precio_actual, p.precio_antes,
               (SELECT GROUP_CONCAT(l.nombre, ' | ') FROM listas l
                JOIN libros_listas ll2 ON ll2.lista_id = l.id
                WHERE ll2.libro_id = lib.id) as listas
        FROM precios p
        JOIN libros lib ON lib.id = p.libro_id
        WHERE p.estado = 'disponible' AND p.descuento != 'Sin descuento'
        ORDER BY CAST(SUBSTR(p.descuento, 1, INSTR(p.descuento, '%') - 1) AS INTEGER) DESC
        LIMIT 5
    """)
    mayor_descuento = cursor.fetchall()

    cursor.execute("""
        SELECT SUM(p.precio_antes - p.precio_actual) as total_ahorro
        FROM precios p
        WHERE p.id IN (SELECT id FROM precios WHERE estado = 'disponible' GROUP BY libro_id HAVING MAX(fecha))
        AND p.precio_antes IS NOT NULL AND p.precio_actual IS NOT NULL
    """)
    total_ahorro = cursor.fetchone()[0]

    cursor.execute("""
        SELECT lib.titulo, lib.autor, MIN(p2.fecha) as desde,
               (SELECT GROUP_CONCAT(l.nombre, ' | ') FROM listas l
                JOIN libros_listas ll2 ON ll2.lista_id = l.id
                WHERE ll2.libro_id = lib.id) as listas
        FROM libros lib
        JOIN precios p2 ON p2.libro_id = lib.id
        GROUP BY lib.id
        ORDER BY desde ASC LIMIT 5
    """)
    mas_antiguos = cursor.fetchall()

    cursor.execute("""
        SELECT COUNT(*) FROM precios
        WHERE DATE(fecha) = DATE('now')
    """)
    precios_hoy = cursor.fetchone()[0]

    conn.close()

    return render_template("stats.html",
        total_libros=total_libros,
        total_precios=total_precios,
        ultima_actualizacion=ultima_actualizacion,
        primera_fecha=primera_fecha,
        mas_cambios=mas_cambios,
        mayor_ahorro=mayor_ahorro,
        mayor_descuento=mayor_descuento,
        total_ahorro=total_ahorro,
        mas_antiguos=mas_antiguos,
        precios_hoy=precios_hoy,
    )


# ── [10] ver_libro: detalle individual con gráfico ──
# type hint: libro_id: int, retorno -> str | tuple
# Mejora: str | tuple cubre el caso 404 cuando el libro no existe.
@app.route("/libro/<int:libro_id>")
def ver_libro(libro_id: int) -> str | tuple:
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM libros WHERE id = ?", (libro_id,))
    libro = cursor.fetchone()
    if libro is None:
        conn.close()
        return "Libro no encontrado", 404

    cursor.execute("""
        SELECT l.id, l.nombre
        FROM listas l
        JOIN libros_listas ll ON l.id = ll.lista_id
        WHERE ll.libro_id = ?
    """, (libro_id,))
    listas = cursor.fetchall()

    cursor.execute("""
        SELECT precio_actual, precio_antes, descuento, estado, fecha
        FROM precios
        WHERE libro_id = ?
        ORDER BY fecha DESC
    """, (libro_id,))
    precios = list(cursor.fetchall())

    conn.close()

    recomendacion = None
    if len(precios) >= 1:
        valores = [p["precio_actual"] for p in precios if p["precio_actual"] is not None]
        if valores:
            min_price = min(valores)
            current_price = valores[0]

            ratio = current_price / min_price if min_price > 0 else 1

            recent_window = precios[:5]
            recent_window.reverse()
            recent_vals = [p["precio_actual"] for p in recent_window if p["precio_actual"] is not None]

            if len(recent_vals) >= 2:
                first_p = recent_vals[0]
                last_p = recent_vals[-1]
                if last_p < first_p:
                    trend = "down"
                elif last_p > first_p:
                    trend = "up"
                else:
                    trend = "stable"
            else:
                trend = "stable"

            if ratio <= 1.03:
                recomendacion = {
                    "label": "Mejor momento ✅",
                    "class": "success",
                    "detail": f"A solo {((ratio - 1) * 100):.0f}% del mínimo histórico"
                }
            elif trend == "down":
                recomendacion = {
                    "label": "Espera 📉",
                    "class": "info",
                    "detail": "El precio viene bajando, conviene esperar"
                }
            elif trend == "up":
                recomendacion = {
                    "label": "Subiendo 📈",
                    "class": "warning",
                    "detail": "El precio está subiendo en los últimos registros"
                }
            else:
                recomendacion = {
                    "label": "Sin tendencia clara",
                    "class": "secondary",
                    "detail": ""
                }

    return render_template("libro.html",
                           libro=libro,
                           listas=listas,
                           precios=precios,
                           recomendacion=recomendacion)


# ── [11] eliminar_libro: borra libro vía POST ──
# type hint: libro_id: int, retorno -> flask.Response
# Mejora: jsonify() retorna Response; mypy verifica que siempre
#         retornes una respuesta HTTP válida.
@app.route("/libro/<int:libro_id>/eliminar", methods=["POST"])
def eliminar_libro(libro_id: int) -> Response:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM libros WHERE id = ?", (libro_id,))
    if cursor.fetchone() is None:
        conn.close()
        return jsonify({"ok": False, "error": "Libro no encontrado"}), 404
    cursor.execute("DELETE FROM precios WHERE libro_id = ?", (libro_id,))
    cursor.execute("DELETE FROM libros_listas WHERE libro_id = ?", (libro_id,))
    cursor.execute("DELETE FROM libros WHERE id = ?", (libro_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ── [12] api_precios: JSON para Chart.js ──
# type hint: libro_id: int, retorno -> flask.Response
# Mejora: Response documenta que retorna JSON (no HTML);
#         mypy valida que el endpoint siempre responda correctamente.
@app.route("/api/precios/<int:libro_id>")
def api_precios(libro_id: int) -> Response:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT precio_actual, precio_antes, fecha, descuento, estado
        FROM precios
        WHERE libro_id = ?
        ORDER BY fecha ASC
    """, (libro_id,))
    rows = cursor.fetchall()
    conn.close()

    fechas = []
    actual = []
    antes = []
    descuentos = []
    for r in rows:
        fechas.append(formatear_fecha(r["fecha"]))
        actual.append(r["precio_actual"])
        antes.append(r["precio_antes"])
        d = r["descuento"]
        if d and d != "Sin descuento":
            m = re.search(r"(\d+)", d)
            descuentos.append(int(m.group(1)) if m else None)
        else:
            descuentos.append(None)
    return jsonify({"fechas": fechas, "actual": actual, "antes": antes, "descuentos": descuentos})


# ── Entry point ──
if __name__ == "__main__":
    app.run(debug=True, port=5000)
