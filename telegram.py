import html
import logging
import os
import time
import requests
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo
from utils import formatear_precio

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# Conceptos de telegram.py:
#  [1] TOKEN / CHAT_ID      → Credenciales del bot Telegram
#       (variables de entorno). Se obtienen de @BotFather.
#  [2] enviar_mensaje()     → POST a API de Telegram con
#       parse_mode=HTML. Retorna bool según éxito.
#  [3] formatear_precio()   → int 12990 → string "$12.990"
#       (importada de utils.py, compartida con dashboard.py).
#  [4] notificar_bajas()    → Envía notificación por cada baja
#       de precio > $5.000 CLP con detalle de ahorro. Recibe las
#       bajas ya calculadas por guardar_libro() (scrap.py) — no
#       las vuelve a calcular con una query propia (ver Fase 4,
#       punto 16: antes había dos caminos calculando lo mismo).
#  [5] notificar_resumen()  → Envía resumen del escaneo:
#       libros procesados, bajas, ofertas flash.
#  [5b] notificar_fallo()   → Avisa si la corrida falló o si la sesión
#       de BuscaLibre expiró (hay que correr run.py --login).
#  [6] _extraer_porcentaje()→ Parsea "30% OFF" → int 30.
#       Helper usado por detectar_ofertas_flash().
#  [7] detectar_ofertas_flash() → Detecta productos cuyo % de
#       descuento saltó significativamente vs su máximo histórico.
#  [8] notificar_ofertas_flash() → Envía alerta de oferta flash
#       con detalle del salto porcentual.
# ═══════════════════════════════════════════════════════════════

# ── [1] TOKEN / CHAT_ID: credenciales del bot ──
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

API_URL = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

# Umbral de ahorro (CLP) a partir del cual una baja de precio se
# considera relevante y se notifica por Telegram.
UMBRAL_BAJA_RELEVANTE_CLP = 5000


# Telegram limita a ~1 mensaje por segundo por chat; ráfagas más rápidas
# reciben HTTP 429 y el mensaje se pierde.
PAUSA_ENTRE_MENSAJES_SEG = 1.0
# Tope de espera ante un 429 (Telegram indica retry_after en segundos).
MAX_ESPERA_RETRY_SEG = 60
_ultimo_envio = 0.0


# type hint: valor: object, retorno -> str
# Mejora: con parse_mode=HTML, un título con "&" o "<" hacía que Telegram
#         rechazara el mensaje entero ("can't parse entities"). Todo texto
#         que viene de BuscaLibre/DB debe pasar por aquí antes de interpolarse.
def esc(valor: object) -> str:
    return html.escape(str(valor), quote=True)


# ── [2] enviar_mensaje: envía texto HTML al chat ──
# type hint: texto: str, retorno -> bool
# Mejora: str evita pasar números u objetos por error;
#         bool documenta que retorna éxito/fracaso de la API.
def enviar_mensaje(texto: str) -> bool:
    global _ultimo_envio
    if not TOKEN or not CHAT_ID:
        logger.warning("TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID no configurados")
        return False

    for intento in range(2):  # 1 envío + 1 reintento si Telegram pide esperar (429)
        espera = PAUSA_ENTRE_MENSAJES_SEG - (time.monotonic() - _ultimo_envio)
        if espera > 0:
            time.sleep(espera)
        try:
            r = requests.post(API_URL, json={
                "chat_id": CHAT_ID,
                "text": texto,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }, timeout=10)
            _ultimo_envio = time.monotonic()
        except requests.RequestException as e:
            logger.error("Error de red enviando mensaje Telegram: %s", e)
            return False

        if r.status_code == 429 and intento == 0:
            try:
                retry_after = int(r.json().get("parameters", {}).get("retry_after", 1))
            except ValueError:
                retry_after = 1
            logger.warning("Telegram pidió esperar %ss (429), reintentando", retry_after)
            time.sleep(min(retry_after, MAX_ESPERA_RETRY_SEG))
            continue
        if not r.ok:
            # La descripción de Telegram dice el motivo real (p. ej. HTML inválido).
            try:
                detalle = r.json().get("description", r.text)
            except ValueError:
                detalle = r.text
            logger.error("Telegram rechazó el mensaje (HTTP %s): %s", r.status_code, detalle)
            return False
        return True
    return False



# ── [4] notificar_bajas: alerta de bajas > $5.000 ──
# type hint: bajas: list[dict], retorno -> bool
# Mejora: list[dict] documenta que espera una lista de resultados;
#         bool indica que la función siempre retorna True.
def notificar_bajas(bajas: list[dict]) -> bool:
    if not bajas:
        logger.info("No hay bajas de precio relevantes para notificar")
        return True

    for baja in bajas:
        ahorro = baja["precio_viejo"] - baja["precio_nuevo"]
        if ahorro <= UMBRAL_BAJA_RELEVANTE_CLP:
            continue
        pct = (ahorro / baja["precio_viejo"]) * 100

        mensaje = (
            "📉 <b>BAJÓ DE PRECIO</b>\n\n"
            f"📖 <b>{esc(baja['titulo'].title())}</b>\n"
            f"✍️ {esc(baja['autor'].title())}\n\n"
            f"<s>{formatear_precio(baja['precio_viejo'])}</s>\n"
            f"<b>{formatear_precio(baja['precio_nuevo'])}</b>\n"
            f"💰 Ahorro: <b>{formatear_precio(ahorro)}</b> ({pct:.0f}% menos)\n"
        )
        if baja["descuento"] and baja["descuento"] != "Sin descuento":
            mensaje += f"🏷️ Descuento: {esc(baja['descuento'])}\n"

        if baja.get("url"):
            mensaje += f"\n🔗 <a href=\"{esc(baja['url'])}\">Ver en BuscaLibre</a>"
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
        f"📚 Libros escaneados: {stats.get('total_escaneados', '—')}\n"
        f"🔻 Libros con baja de precio: {stats.get('libros_con_baja', stats.get('libros', 0))}\n"
        f"📉 Bajas detectadas: {stats['bajas']}\n"
        f"⚡ Ofertas flash: {stats.get('ofertas_flash', 0)}\n"
    )
    enviar_mensaje(texto)


# ── [5b] notificar_fallo: aviso de salud del scraper ──
# type hint: error: BaseException, sesion_expirada: bool, retorno -> bool
# Mejora: antes un fallo (sesión vencida, sin red) solo quedaba en el log
#         y nadie se enteraba hasta abrir el dashboard días después.
def notificar_fallo(error: BaseException, sesion_expirada: bool = False) -> bool:
    if sesion_expirada:
        texto = (
            "🔑 <b>Sesión de BuscaLibre expirada</b>\n\n"
            "No se pudieron abrir tus listas de deseos.\n"
            "Ejecuta <code>python run.py --login</code> para volver a iniciar sesión.\n\n"
            f"<i>{esc(str(error)[:300])}</i>"
        )
    else:
        texto = (
            "❌ <b>El scraper falló</b>\n\n"
            f"<code>{esc(type(error).__name__)}</code>: {esc(str(error)[:300])}\n\n"
            "Detalle completo en scraper.log"
        )
    return enviar_mensaje(texto)


# ── [6] _extraer_porcentaje: "30% OFF" → 30 ──
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


# ── [7] detectar_ofertas_flash: saltos de descuento vs histórico ──
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


# ── [8] notificar_ofertas_flash: alerta de oferta flash ──
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
            f"📖 <b>{esc(oferta['titulo'].title())}</b>\n"
            f"✍️ {esc(oferta['autor'].title())}\n\n"
            f"🏷️ <b>{esc(oferta['descuento'])}</b>\n"
            f"💰 {formatear_precio(oferta['precio_actual'])}"
        )
        if oferta["precio_antes"]:
            mensaje += f" <s>{formatear_precio(oferta['precio_antes'])}</s>"
        mensaje += f"\n📈 Salto de {salto}% vs su mejor oferta anterior ({oferta['dcto_max_anterior']}%)"
        if oferta["url"]:
            mensaje += f"\n\n🔗 <a href=\"{esc(oferta['url'])}\">Ver en BuscaLibre</a>"
        enviar_mensaje(mensaje)
    return True
