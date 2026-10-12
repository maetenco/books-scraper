"""
Motor de análisis de precios: ¿es hoy un buen momento para comprar?

Funciones puras (sin Flask, sin red, sin escribir en la DB) usadas por
dashboard.py y, en la Fase 4, por las alertas. Dos preguntas:

1. Por libro — evaluar_libro():
   · "Mínimo del año": el precio actual es ≤ al menor precio de los 365
     días anteriores (pesos nominales).
   · "Mínimo histórico": ≤ al menor precio de TODO el historial, llevando
     cada precio antiguo a pesos de hoy con la UF (inflación). Sin UF,
     este nivel no se calcula.
   · Percentil del precio actual dentro del último año → "Buen precio" /
     "Precio normal" / "Caro".

2. Por tienda — detectar_eventos():
   Un día es "evento de rebaja" si el descuento medio de todo el catálogo
   supera su nivel habitual (mediana de los escaneos previos) por más de
   max(UMBRAL_EVENTO_PP, K_MAD × MAD). Es robusto al ruido diario del
   tipo de cambio porque mira el % de descuento que fija la tienda, no
   el precio en pesos.

Los textos se generan con plantillas (redactar_*): sin LLM, gratis y
deterministas.

Uso de calibración (solo lectura):
    python analisis.py --backtest            # eventos y nuevos mínimos por día
    BOOKS_DB_PATH=copia.db python analisis.py --backtest
"""

import argparse
import re
import sqlite3
import statistics
from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import date, timedelta

# ── Umbrales (calibrar con --backtest) ──
DIAS_VENTANA_ANUAL = 365
MIN_DIAS_HISTORIAL = 30        # días escaneados previos mínimos para opinar (backtest
                               # 2026-10-12: con 10, en julio casi todo era "mínimo del año")
PERCENTIL_BUEN_PRECIO = 20     # ≤ → "Buen precio"
PERCENTIL_CARO = 80            # ≥ → "Caro"
UMBRAL_NUEVO_MINIMO = 0.03     # Fase 4: alerta si baja ≥3% del mínimo anual previo

MIN_LIBROS_DIA = 100           # días con menos libros (era manual) no cuentan para eventos
VENTANA_EVENTOS = 30           # escaneos previos con los que se calcula "lo habitual"
MIN_PUNTOS_PREVIOS = 7
UMBRAL_EVENTO_PP = 2.0         # puntos porcentuales mínimos sobre lo habitual
K_MAD = 3.0


# ── Utilidades de formato (estilo chileno) ──

def _fmt_pesos(valor: float | int | None) -> str:
    if valor is None:
        return "—"
    return f"${round(valor):,}".replace(",", ".")


def _fmt_pct(valor: float) -> str:
    return f"{valor:.1f}".replace(".", ",") + "%"


def _fmt_dia(dia: str) -> str:
    return f"{dia[8:10]}/{dia[5:7]}/{dia[2:4]}"


def descuento_pct(texto: str | None) -> int | None:
    """"50%" / "50% OFF" → 50; "Sin descuento" → 0; vacío/"Sin stock" → None."""
    if not texto or texto == "Sin stock":
        return None
    if texto == "Sin descuento":
        return 0
    m = re.search(r"(\d+)", texto)
    return int(m.group(1)) if m else None


# ── Por libro ──

# type hint: precios: Iterable[Mapping], retorno -> list[tuple[str, int]]
def precios_diarios(precios: Iterable[Mapping]) -> list[tuple[str, int]]:
    """Menor precio disponible de cada día, en orden cronológico. Se usa
    el mínimo del día porque el criterio de "buena rebaja" es el precio
    más bajo (puede haber dos corridas con precios distintos un mismo día)."""
    por_dia: dict[str, int] = {}
    for p in precios:
        if p["estado"] != "disponible" or p["precio_actual"] is None or not p["fecha"]:
            continue
        dia = p["fecha"][:10]
        por_dia[dia] = min(por_dia.get(dia, p["precio_actual"]), p["precio_actual"])
    return sorted(por_dia.items())


# type hint: uf: Mapping[str, float], dia: str, retorno -> float | None
def uf_del_dia(uf: Mapping[str, float], dia: str) -> float | None:
    """UF de ese día o, si falta, la del día anterior más cercano (≤31 días)."""
    d = date.fromisoformat(dia)
    for atras in range(32):
        valor = uf.get((d - timedelta(days=atras)).isoformat())
        if valor:
            return valor
    return None


# type hint: precios: Iterable[Mapping], uf: Mapping | None, retorno -> dict | None
# Mejora: dict con label/class/detail compatible con libro.html, más los
#         datos crudos para tablas, alertas y tests.
def evaluar_libro(precios: Iterable[Mapping], uf: Mapping[str, float] | None = None) -> dict | None:
    """`precios`: filas con fecha, precio_actual y estado (cualquier orden).
    Retorna None si no hay ningún registro."""
    precios = [p for p in precios if p["fecha"]]
    if not precios:
        return None
    ultimo = max(precios, key=lambda p: p["fecha"])
    if ultimo["estado"] == "sin_stock":
        return {"nivel": "sin_stock", "label": "Sin stock", "class": "secondary",
                "detail": "No hay precio vigente para evaluar."}

    serie = precios_diarios(precios)
    if not serie or ultimo["precio_actual"] is None:
        return None
    dia_hoy = serie[-1][0]
    actual = ultimo["precio_actual"]
    inicio_anual = (date.fromisoformat(dia_hoy) - timedelta(days=DIAS_VENTANA_ANUAL)).isoformat()
    previos = [(d, p) for d, p in serie if d < dia_hoy]
    anual = [(d, p) for d, p in previos if d >= inicio_anual]

    r: dict = {"actual": actual, "dia": dia_hoy, "dias_historial": len(anual)}
    if len(anual) < MIN_DIAS_HISTORIAL:
        r.update(nivel="pocos_datos", label="Historial corto", **{"class": "secondary"},
                 detail=f"Solo {len(anual)} días de historial en el último año; aún no hay con qué comparar.")
        return r

    fecha_min_anual, min_anual = min(anual, key=lambda x: (x[1], x[0]))
    percentil = 100 * sum(1 for _, p in anual if p < actual) / len(anual)
    r.update(min_anual=min_anual, fecha_min_anual=fecha_min_anual, percentil=round(percentil),
             baja_vs_min_anual=(min_anual - actual) / min_anual if min_anual else 0.0)

    # Mínimo histórico en pesos de hoy (solo si hay UF para hoy y para algún día previo).
    uf_hoy = uf_del_dia(uf, dia_hoy) if uf else None
    if uf_hoy:
        ajustados = []
        for d, p in previos:
            uf_d = uf_del_dia(uf, d)
            if uf_d:
                ajustados.append((p * uf_hoy / uf_d, d, p))
        if ajustados:
            valor, d, p = min(ajustados)
            r.update(min_hist_ajustado=round(valor), fecha_min_hist=d, min_hist_nominal=p)

    if "min_hist_ajustado" in r and actual <= r["min_hist_ajustado"]:
        r.update(nivel="min_historico", label="Mínimo histórico 🏆", **{"class": "success"})
    elif actual <= min_anual:
        r.update(nivel="min_anual", label="Mínimo del año ✅", **{"class": "success"})
    elif percentil <= PERCENTIL_BUEN_PRECIO:
        r.update(nivel="buen_precio", label="Buen precio 👍", **{"class": "info"})
    elif percentil >= PERCENTIL_CARO:
        r.update(nivel="caro", label="Caro 📈", **{"class": "warning"})
    else:
        r.update(nivel="normal", label="Precio normal", **{"class": "secondary"})
    r["detail"] = redactar_evaluacion(r)
    return r


# type hint: ev: dict, retorno -> str
def redactar_evaluacion(ev: dict) -> str:
    partes = []
    if ev["nivel"] in ("min_historico", "min_anual"):
        partes.append(f"Está a su precio más bajo del año ({_fmt_pesos(ev['actual'])}).")
    else:
        mas_barato = 100 - ev["percentil"]
        partes.append(
            f"Más barato que el {mas_barato}% de los días del último año. "
            f"Mínimo del año: {_fmt_pesos(ev['min_anual'])} ({_fmt_dia(ev['fecha_min_anual'])})."
        )
    if "min_hist_ajustado" in ev:
        if ev["nivel"] == "min_historico":
            partes.append("También es su mínimo histórico, ajustado por inflación.")
        else:
            partes.append(
                f"Mínimo histórico ajustado por inflación: {_fmt_pesos(ev['min_hist_ajustado'])} "
                f"en pesos de hoy ({_fmt_pesos(ev['min_hist_nominal'])} el {_fmt_dia(ev['fecha_min_hist'])})."
            )
    return " ".join(partes)


# ── Por tienda ──

# type hint: filas: Iterable[Mapping], retorno -> list[tuple[str, float, int]]
def serie_descuento_diario(filas: Iterable[Mapping]) -> list[tuple[str, float, int]]:
    """Descuento medio del catálogo por día: (día, media %, nº libros).
    `filas`: libro_id, fecha, descuento, estado. Cada libro cuenta una vez
    por día (su último registro) para que los libros con varias corridas
    ese día no pesen más."""
    por_dia: dict[str, dict[int, tuple[str, int]]] = defaultdict(dict)
    for f in filas:
        if f["estado"] != "disponible" or not f["fecha"]:
            continue
        pct = descuento_pct(f["descuento"])
        if pct is None:
            continue
        dia = f["fecha"][:10]
        previo = por_dia[dia].get(f["libro_id"])
        if previo is None or f["fecha"] >= previo[0]:
            por_dia[dia][f["libro_id"]] = (f["fecha"], pct)
    return [
        (dia, sum(p for _, p in libros.values()) / len(libros), len(libros))
        for dia, libros in sorted(por_dia.items())
    ]


# type hint: serie: list[tuple[str, float, int]], retorno -> list[dict]
def detectar_eventos(serie: list[tuple[str, float, int]]) -> list[dict]:
    """Un dict por día con suficientes libros: descuento medio, nivel
    habitual (mediana de hasta VENTANA_EVENTOS escaneos previos), umbral y
    si es evento. Sin MIN_PUNTOS_PREVIOS previos, es_evento=False."""
    validos = [(d, m, n) for d, m, n in serie if n >= MIN_LIBROS_DIA]
    resultado = []
    for i, (dia, media, n) in enumerate(validos):
        previos = [m for _, m, _ in validos[max(0, i - VENTANA_EVENTOS):i]]
        habitual = umbral = None
        es_evento = False
        if len(previos) >= MIN_PUNTOS_PREVIOS:
            habitual = statistics.median(previos)
            mad = statistics.median(abs(x - habitual) for x in previos)
            umbral = habitual + max(UMBRAL_EVENTO_PP, K_MAD * mad)
            es_evento = media > umbral
        resultado.append({"dia": dia, "descuento_medio": media, "n_libros": n,
                          "habitual": habitual, "umbral": umbral, "es_evento": es_evento})
    return resultado


# type hint: ev: dict, retorno -> str
def redactar_evento(ev: dict) -> str:
    return (
        f"El {_fmt_dia(ev['dia'])} fue una buena rebaja de la tienda: descuento medio "
        f"{_fmt_pct(ev['descuento_medio'])} vs {_fmt_pct(ev['habitual'])} habitual "
        f"({ev['n_libros']} libros)."
    )


# ── Backtest (calibración, solo lectura) ──

def _backtest(db_path: str) -> None:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    filas = conn.execute(
        "SELECT libro_id, fecha, precio_actual, descuento, estado FROM precios ORDER BY fecha"
    ).fetchall()

    print("── Eventos de tienda (días con ≥%s libros) ──" % MIN_LIBROS_DIA)
    print(f"{'día':<11} {'dcto medio':>10} {'habitual':>9} {'umbral':>7} {'libros':>6}")
    for e in detectar_eventos(serie_descuento_diario(filas)):
        hab = f"{e['habitual']:.1f}" if e["habitual"] is not None else "—"
        umb = f"{e['umbral']:.1f}" if e["umbral"] is not None else "—"
        marca = "  ← EVENTO" if e["es_evento"] else ""
        print(f"{e['dia']:<11} {e['descuento_medio']:>10.1f} {hab:>9} {umb:>7} {e['n_libros']:>6}{marca}")

    # Nuevos mínimos por día: cuántos libros habrían disparado la alerta
    # de la Fase 4 (≥UMBRAL_NUEVO_MINIMO bajo su mínimo anual previo).
    por_libro: dict[int, list] = defaultdict(list)
    for f in filas:
        por_libro[f["libro_id"]].append(f)
    alertas: dict[str, int] = defaultdict(int)
    for historial in por_libro.values():
        dias = sorted({f["fecha"][:10] for f in historial})
        for dia in dias:
            hasta = [f for f in historial if f["fecha"][:10] <= dia]
            ev = evaluar_libro(hasta)
            if ev and ev.get("baja_vs_min_anual", 0) >= UMBRAL_NUEVO_MINIMO:
                alertas[dia] += 1
    print(f"\n── Alertas de 'nuevo mínimo' (≥{UMBRAL_NUEVO_MINIMO:.0%} bajo el mínimo anual previo) ──")
    for dia in sorted(alertas):
        print(f"{dia}  {alertas[dia]:>4} libros")
    total_dias = len({f["fecha"][:10] for f in filas if f["fecha"] >= "2026-06-01"})
    print(f"Total: {sum(alertas.values())} alertas en {len(alertas)} días "
          f"(de {total_dias} días escaneados desde 2026-06)")
    conn.close()


if __name__ == "__main__":
    import os
    parser = argparse.ArgumentParser(description="Análisis de precios (calibración)")
    parser.add_argument("--backtest", action="store_true", help="Eventos y nuevos mínimos por día (solo lectura)")
    args = parser.parse_args()
    if args.backtest:
        _backtest(os.environ.get("BOOKS_DB_PATH", "libros.db"))
    else:
        parser.print_help()
