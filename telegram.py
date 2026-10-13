import html
import logging
import os
import time
import requests

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# Conceptos de telegram.py:
#  [1] TOKEN / CHAT_ID      → Credenciales del bot Telegram
#       (variables de entorno). Se obtienen de @BotFather.
#  [2] enviar_mensaje()     → POST a API de Telegram con
#       parse_mode=HTML, ≥1s entre mensajes y reintento ante 429.
#  [3] esc()                → Escapa texto externo para el HTML.
#  [4] notificar_fallo()    → Avisa si la corrida falló o si la sesión
#       de BuscaLibre expiró (hay que correr run.py --login).
# Las alertas de precios (nuevos mínimos, evento de tienda, resumen
# semanal) se calculan y redactan en alertas.py; aquí solo se envían.
# Antes vivían aquí "bajas > $5.000" y "ofertas flash", eliminadas en la
# Fase 4: reaccionaban al ruido diario del tipo de cambio.
# ═══════════════════════════════════════════════════════════════

# ── [1] TOKEN / CHAT_ID: credenciales del bot ──
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

API_URL = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

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



# ── [4] notificar_fallo: aviso de salud del scraper ──
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
