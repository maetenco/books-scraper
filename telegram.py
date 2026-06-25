import os
import requests
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

# ═══════════════════════════════════════════════════════════════
# Conceptos de telegram.py:
#  [1] TOKEN / CHAT_ID      → Credenciales del bot Telegram
#       (variables de entorno). Se obtienen de @BotFather.
#  [2] enviar_mensaje()     → POST a API de Telegram con
#       parse_mode=HTML. Retorna bool según éxito.
#  [3] formatear_precio()   → int 12990 → string "$12.990"
#       usando punto como separador de miles (formato chileno).
#  [4] notificar_bajas()    → Envía notificación por cada baja
#       de precio > $5.000 CLP con detalle de ahorro.
#  [5] notificar_resumen()  → Envía resumen del escaneo:
#       libros procesados, actualizados, bajas, ofertas flash.
#  [6] detectar_bajas()     → Consulta SQL que compara precio
#       actual vs precio anterior del mismo libro; retorna bajas.
#  [7] _extraer_porcentaje()→ Parsea "30% OFF" → int 30.
#       Helper usado por detectar_ofertas_flash().
#  [8] detectar_ofertas_flash() → Detecta productos cuyo % de
#       descuento saltó significativamente vs su máximo histórico.
#  [9] notificar_ofertas_flash() → Envía alerta de oferta flash
#       con detalle del salto porcentual.
# ═══════════════════════════════════════════════════════════════

# ── [1] TOKEN / CHAT_ID: credenciales del bot ──
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

API_URL = f"https://api.telegram.org/bot{TOKEN}/sendMessage"


# ── [2] enviar_mensaje: envía texto HTML al chat ──
# type hint: texto: str, retorno -> bool
# Mejora: str evita pasar números u objetos por error;
#         bool documenta que retorna éxito/fracaso de la API.
def enviar_mensaje(texto: str) -> bool:
    if not TOKEN or not CHAT_ID:
        print("TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID no configurados")
        return False
    try:
        r = requests.post(API_URL, json={
            "chat_id": CHAT_ID,
            "text": texto,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }, timeout=10)
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"Error enviando mensaje Telegram: {e}")
        return False


# ── [3] formatear_precio: 12990 → "$12.990" ──
# type hint: valor: int | None, retorno -> str
# Mejora: int | None documenta que acepta nulos (precio faltante);
#         str siempre devuelve un string, nunca rompe el template.
def formatear_precio(valor: int | None) -> str:
    if valor is None:
        return "—"
    return f"${valor:,.0f}".replace(",", ".")


# ── [4] notificar_bajas: alerta de bajas > $5.000 ──
# type hint: bajas: list[dict], retorno -> bool
# Mejora: list[dict] documenta que espera una lista de resultados;
#         bool indica que la función siempre retorna True.
def notificar_bajas(bajas: list[dict]) -> bool:
    if not bajas:
        print("No hay bajas de precio relevantes para notificar")
        return True

    for baja in bajas:
        ahorro = baja["precio_viejo"] - baja["precio_nuevo"]
        if ahorro <= 5000:
            continue
        pct = (ahorro / baja["precio_viejo"]) * 100

        mensaje = (
            "📉 <b>BAJÓ DE PRECIO</b>\n\n"
            f"📖 <b>{baja['titulo'].title()}</b>\n"
            f"✍️ {baja['autor'].title()}\n\n"
            f"<s>{formatear_precio(baja['precio_viejo'])}</s>\n"
            f"<b>{formatear_precio(baja['precio_nuevo'])}</b>\n"
            f"💰 Ahorro: <b>{formatear_precio(ahorro)}</b> ({pct:.0f}% menos)\n"
        )
        if baja["descuento"] and baja["descuento"] != "Sin descuento":
            mensaje += f"🏷️ Descuento: {baja['descuento']}\n"

        if baja.get("url"):
            mensaje += f"\n🔗 <a href='{baja['url']}'>Ver en BuscaLibre</a>"
        else:
            mensaje += f"\n🔗 <a href='https://www.buscalibre.cl'>BuscaLibre</a>"
        enviar_mensaje(mensaje)

    return True


# ── [5] notificar_resumen: resumen del escaneo ──
# type hint: stats: dict, retorno -> bool
# Mejora: dict evita acceder a stats["libros"] sobre None;
#         bool documenta consistencia con notificar_bajas().
def notificar_resumen(stats: dict) -> bool:
    texto = (
        "📊 <b>Resumen de escaneo</b>\n\n"
        f"📚 Libros procesados: {stats['libros']}\n"
        f"🔄 Precios actualizados: {stats['actualizados']}\n"
        f"📉 Bajas detectadas: {stats['bajas']}\n"
        f"⚡ Ofertas flash: {stats.get('ofertas_flash', 0)}\n"
    )
    enviar_mensaje(texto)


# ── [6] detectar_bajas: consulta SQL de bajas de precio ──
# type hint: conexion: sqlite3.Connection, retorno -> list[dict]
# Mejora: mypy valida que el parámetro sea una conexión SQLite;
#         list[dict] indica que siempre retorna una lista (vacía si no hay bajas).
def detectar_bajas(conexion: sqlite3.Connection) -> list[dict]:
    cursor = conexion.cursor()
    hoy_chile = datetime.now(ZoneInfo("America/Santiago")).strftime("%Y-%m-%d")
    cursor.execute("""
        SELECT l.titulo, l.autor, p1.precio_actual,
               p2.precio_actual as precio_anterior,
               p1.descuento, p1.fecha, p1.libro_id, l.url
        FROM precios p1
        JOIN libros l ON l.id = p1.libro_id
        JOIN precios p2 ON p2.id = (
            SELECT id FROM precios
            WHERE libro_id = p1.libro_id
            AND fecha < p1.fecha
            ORDER BY fecha DESC LIMIT 1
        )
        WHERE DATE(p1.fecha) = ?
        AND p1.precio_actual < p2.precio_actual
        ORDER BY (p2.precio_actual - p1.precio_actual) DESC
    """, (hoy_chile,))
    bajas = []
    for r in cursor.fetchall():
        bajas.append({
            "titulo": r[0],
            "autor": r[1],
            "precio_nuevo": r[2],
            "precio_viejo": r[3],
            "descuento": r[4],
            "fecha": r[5],
            "libro_id": r[6],
            "url": r[7],
        })
    return bajas


# ── [7] _extraer_porcentaje: "30% OFF" → 30 ──
# type hint: descuento_str: str | None, retorno -> int
# Mejora: str | None refleja que puede recibir None desde la DB;
#         int siempre retorna un número (0 si no puede parsear).
def _extraer_porcentaje(descuento_str: str | None) -> int:
    if not descuento_str or descuento_str == "Sin descuento":
        return 0
    try:
        return int(descuento_str.replace("% OFF", "").replace("%", "").strip())
    except (ValueError, AttributeError):
        return 0


# ── [8] detectar_ofertas_flash: saltos de descuento vs histórico ──
# type hint: conexion: sqlite3.Connection, salto_minimo: int = 20,
#           retorno -> list[dict]
# Mejora: tipar salto_minimo como int evita pasar strings al SQL;
#         list[dict] documenta que retorna ofertas o lista vacía.
def detectar_ofertas_flash(conexion: sqlite3.Connection, salto_minimo: int = 20) -> list[dict]:
    cursor = conexion.cursor()
    hoy_chile = datetime.now(ZoneInfo("America/Santiago")).strftime("%Y-%m-%d")
    cursor.execute("""
        SELECT * FROM (
            SELECT p1.libro_id, l.titulo, l.autor, l.url,
                   p1.precio_actual, p1.precio_antes, p1.descuento,
                   (SELECT MAX(
                       CASE
                           WHEN p2.descuento IS NULL OR p2.descuento = 'Sin descuento' THEN 0
                           ELSE CAST(REPLACE(REPLACE(p2.descuento, '% OFF', ''), '%', '') AS INTEGER)
                       END
                   ) FROM precios p2
                   WHERE p2.libro_id = p1.libro_id AND p2.id != p1.id
                  ) as dcto_max_anterior
            FROM precios p1
            JOIN libros l ON l.id = p1.libro_id
            WHERE DATE(p1.fecha) = ?
              AND p1.descuento IS NOT NULL
              AND p1.descuento != 'Sin descuento'
        )
        WHERE CAST(REPLACE(REPLACE(descuento, '% OFF', ''), '%', '') AS INTEGER)
              - dcto_max_anterior >= ?
        ORDER BY CAST(REPLACE(REPLACE(descuento, '% OFF', ''), '%', '') AS INTEGER) DESC
    """, (hoy_chile, salto_minimo,))
    ofertas = []
    for r in cursor.fetchall():
        pct_actual = _extraer_porcentaje(r[6])
        ofertas.append({
            "libro_id": r[0],
            "titulo": r[1],
            "autor": r[2],
            "url": r[3],
            "precio_actual": r[4],
            "precio_antes": r[5],
            "descuento": r[6],
            "dcto_actual": pct_actual,
            "dcto_max_anterior": r[7] or 0,
        })
    return ofertas


# ── [9] notificar_ofertas_flash: alerta de oferta flash ──
# type hint: ofertas: list[dict], retorno -> bool
# Mejora: list[dict] documenta que espera el resultado de detectar_ofertas_flash();
#         bool la hace consistente con las demás notificar_*().
def notificar_ofertas_flash(ofertas: list[dict]) -> bool:
    if not ofertas:
        return True
    for oferta in ofertas:
        salto = oferta["dcto_actual"] - oferta["dcto_max_anterior"]
        mensaje = (
            "⚡ <b>OFERTA FLASH</b>\n\n"
            f"📖 <b>{oferta['titulo'].title()}</b>\n"
            f"✍️ {oferta['autor'].title()}\n\n"
            f"🏷️ <b>{oferta['descuento']}</b>\n"
            f"💰 {formatear_precio(oferta['precio_actual'])}"
        )
        if oferta["precio_antes"]:
            mensaje += f" <s>{formatear_precio(oferta['precio_antes'])}</s>"
        mensaje += f"\n📈 Salto de {salto}% vs su mejor oferta anterior ({oferta['dcto_max_anterior']}%)"
        if oferta["url"]:
            mensaje += f"\n\n🔗 <a href='{oferta['url']}'>Ver en BuscaLibre</a>"
        enviar_mensaje(mensaje)
    return True
