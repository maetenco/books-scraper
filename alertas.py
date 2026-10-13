"""
Alertas de Telegram útiles (Fase 4): reemplazan a "bajas > $5.000",
"ofertas flash" y el resumen diario, que eran ruido del tipo de cambio.

Tres alertas, decididas con analisis.py y redactadas por plantilla:

1. Nuevos mínimos: libros cuyo precio del último escaneo está al menos
   UMBRAL_NUEVO_MINIMO (3%) bajo su mínimo del año anterior (requiere
   MIN_DIAS_HISTORIAL días). Van en UN mensaje agrupado (máx.
   MAX_LIBROS_MENSAJE), con el evento de tienda al inicio si lo hay.
2. Evento de rebaja de la tienda: solo el primer día del evento.
3. Resumen semanal: en la primera corrida de la semana a partir de
   DIA_RESUMEN_SEMANAL (lunes).

La tabla `notificaciones` (tipo, clave) evita repetir un aviso ya
enviado (p. ej. con `run.py --force`). Una alerta solo se registra si
Telegram la aceptó: si el envío falla, se reintenta en la próxima corrida.
"""

import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from analisis import (
    UMBRAL_NUEVO_MINIMO, detectar_eventos, evaluar_libro, redactar_evento, serie_descuento_diario,
)
from indicadores import cargar_uf
from telegram import esc
from utils import formatear_precio

MAX_LIBROS_MENSAJE = 15
DIA_RESUMEN_SEMANAL = 0    # 0 = lunes … 6 = domingo
TOP_RESUMEN = 5
MAX_LARGO_TITULO = 60


# type hint: conexion: sqlite3.Connection, retorno -> None
def crear_tabla_notificaciones(conexion: sqlite3.Connection) -> None:
    conexion.execute("""
        CREATE TABLE IF NOT EXISTS notificaciones (
            tipo TEXT NOT NULL,
            clave TEXT NOT NULL,
            fecha TEXT NOT NULL,
            PRIMARY KEY (tipo, clave)
        )
    """)


def _ya_notificado(conexion: sqlite3.Connection, tipo: str, clave: str) -> bool:
    return conexion.execute(
        "SELECT 1 FROM notificaciones WHERE tipo = ? AND clave = ?", (tipo, clave)
    ).fetchone() is not None


# type hint: conexion, claves: list[tuple[str, str]], retorno -> None
def registrar_notificaciones(conexion: sqlite3.Connection, claves: list[tuple[str, str]]) -> None:
    ahora = datetime.now(ZoneInfo("America/Santiago")).strftime("%Y-%m-%d %H:%M:%S")
    conexion.executemany(
        "INSERT OR IGNORE INTO notificaciones (tipo, clave, fecha) VALUES (?, ?, ?)",
        [(tipo, clave, ahora) for tipo, clave in claves],
    )
    conexion.commit()


def _titulo(texto: str) -> str:
    texto = (texto or "").strip().capitalize()
    return texto if len(texto) <= MAX_LARGO_TITULO else texto[:MAX_LARGO_TITULO - 1] + "…"


# ── Cálculo ──

# type hint: conexion: sqlite3.Connection, hoy: date | None, retorno -> dict
# Mejora: dict con lo que hay que avisar + las claves a registrar si el
#         envío funciona; run.py decide si envía, solo muestra (preview) o nada.
def calcular_alertas(conexion: sqlite3.Connection, hoy: date | None = None) -> dict:
    hoy = hoy or datetime.now(ZoneInfo("America/Santiago")).date()
    crear_tabla_notificaciones(conexion)
    cur = conexion.cursor()
    cur.row_factory = sqlite3.Row  # solo este cursor: no altera la conexión de quien llama

    filas = cur.execute(
        "SELECT libro_id, fecha, precio_actual, descuento, estado FROM precios"
    ).fetchall()
    if not filas:
        return {"evento": None, "nuevos_minimos": [], "resumen": None, "dia": None}
    dia_scan = max(f["fecha"][:10] for f in filas if f["fecha"])
    libros = {r["id"]: r for r in cur.execute("SELECT id, titulo, autor, url FROM libros").fetchall()}
    uf = cargar_uf(conexion)

    por_libro: dict[int, list] = defaultdict(list)
    for f in filas:
        por_libro[f["libro_id"]].append(f)
    evaluaciones = {lid: ev for lid, fs in por_libro.items() if (ev := evaluar_libro(fs, uf))}

    # 1. Nuevos mínimos del último escaneo.
    nuevos = []
    for lid, ev in evaluaciones.items():
        if (ev.get("dia") == dia_scan and lid in libros
                and ev.get("baja_vs_min_anual", 0) >= UMBRAL_NUEVO_MINIMO - 1e-9
                and not _ya_notificado(conexion, "nuevo_minimo", f"{lid}:{dia_scan}")):
            nuevos.append({**ev, "libro_id": lid, "titulo": libros[lid]["titulo"],
                           "url": libros[lid]["url"]})
    nuevos.sort(key=lambda x: x["baja_vs_min_anual"], reverse=True)

    # 2. Evento de tienda: solo si el último escaneo es evento y el anterior no.
    eventos = detectar_eventos(serie_descuento_diario(filas))
    evento = None
    if eventos and eventos[-1]["dia"] == dia_scan and eventos[-1]["es_evento"]:
        anterior_tambien = len(eventos) > 1 and eventos[-2]["es_evento"]
        if not anterior_tambien and not _ya_notificado(conexion, "evento", dia_scan):
            evento = eventos[-1]

    # 3. Resumen semanal (clave = semana ISO).
    anio, semana, _ = hoy.isocalendar()
    clave_semana = f"{anio}-W{semana:02d}"
    resumen = None
    if hoy.weekday() >= DIA_RESUMEN_SEMANAL and not _ya_notificado(conexion, "resumen_semanal", clave_semana):
        hace_7 = (hoy - timedelta(days=6)).isoformat()
        actuales = [(lid, ev) for lid, ev in evaluaciones.items() if lid in libros and ev.get("dia") == dia_scan]
        con_percentil = sorted(
            (x for x in actuales if "percentil" in x[1]), key=lambda x: (x[1]["percentil"], x[1]["actual"])
        )
        resumen = {
            "semana": clave_semana,
            "eventos": [e for e in eventos if e["es_evento"] and e["dia"] >= hace_7],
            "ultimo": next((e for e in reversed(eventos) if e["habitual"] is not None), None),
            "conteo": {n: sum(1 for _, ev in actuales if ev["nivel"] == n)
                       for n in ("min_historico", "min_anual", "buen_precio", "caro")},
            "top": [{**ev, "titulo": libros[lid]["titulo"]} for lid, ev in con_percentil[:TOP_RESUMEN]],
        }

    return {"evento": evento, "nuevos_minimos": nuevos, "resumen": resumen, "dia": dia_scan,
            "claves_resumen": ("resumen_semanal", clave_semana)}


# ── Redacción (HTML de Telegram, todo texto externo escapado) ──

# type hint: alertas: dict, retorno -> list[tuple[str, list[tuple[str, str]]]]
# Mejora: cada mensaje viaja con las claves que se registran si se envía bien.
def redactar_mensajes(alertas: dict) -> list[tuple[str, list[tuple[str, str]]]]:
    mensajes = []
    dia = alertas["dia"]

    partes, claves = [], []
    if alertas["evento"]:
        e = alertas["evento"]
        partes.append(
            "🛍️ <b>HOY HAY REBAJA EN LA TIENDA</b>\n"
            f"{esc(redactar_evento(e))}"
        )
        claves.append(("evento", dia))
    nuevos = alertas["nuevos_minimos"]
    if nuevos:
        lineas = [f"📉 <b>{len(nuevos)} libro{'s' if len(nuevos) != 1 else ''} en nuevo mínimo del año</b>"]
        for i, n in enumerate(nuevos[:MAX_LIBROS_MENSAJE], 1):
            trofeo = " 🏆" if n["nivel"] == "min_historico" else ""
            lineas.append(
                f"{i}. {esc(_titulo(n['titulo']))} — <b>{formatear_precio(n['actual'])}</b> "
                f"(−{n['baja_vs_min_anual'] * 100:.0f}% vs {formatear_precio(n['min_anual'])}){trofeo}"
            )
        if len(nuevos) > MAX_LIBROS_MENSAJE:
            lineas.append(f"…y {len(nuevos) - MAX_LIBROS_MENSAJE} más: ver \"Solo buenos precios\" en el dashboard.")
        lineas.append("\n🏆 = también mínimo histórico ajustado por inflación")
        partes.append("\n".join(lineas))
        claves.extend(("nuevo_minimo", f"{n['libro_id']}:{dia}") for n in nuevos)
    if partes:
        mensajes.append(("\n\n".join(partes), claves))

    r = alertas["resumen"]
    if r:
        lineas = [f"📊 <b>Resumen semanal</b> ({r['semana']})"]
        if r["eventos"]:
            lineas.append("\n🛍️ Rebajas de la tienda esta semana:")
            lineas.extend(f"• {esc(redactar_evento(e))}" for e in r["eventos"])
        elif r["ultimo"]:
            u = r["ultimo"]
            lineas.append(
                f"\n🛍️ Sin rebajas especiales esta semana (último escaneo: "
                f"{u['descuento_medio']:.1f}% vs {u['habitual']:.1f}% habitual)".replace(".", ",")
            )
        c = r["conteo"]
        lineas.append(
            f"\n🏆 {c['min_historico']} en mínimo histórico · ✅ {c['min_anual']} en mínimo del año · "
            f"👍 {c['buen_precio']} a buen precio · 📈 {c['caro']} caros"
        )
        if r["top"]:
            lineas.append("\n<b>Mejores precios vs su historial:</b>")
            for i, t in enumerate(r["top"], 1):
                lineas.append(f"{i}. {esc(_titulo(t['titulo']))} — {formatear_precio(t['actual'])} ({esc(t['label'])})")
        mensajes.append(("\n".join(lineas), [alertas["claves_resumen"]]))
    return mensajes
