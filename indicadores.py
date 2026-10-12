"""
Valores diarios de la UF desde el SII, cacheados en la tabla `uf`.

La UF se usa en analisis.py para comparar precios de distintos años en
pesos de hoy ("mínimo histórico ajustado por inflación"). Fuente: la
tabla anual oficial del SII (https://www.sii.cl/valores_y_fechas/uf/
uf<año>.htm), gratis y sin registro. mindicador.cl (la fuente prevista
originalmente) no respondía de forma estable.

Nunca lanza hacia afuera: si el SII no responde o cambia su HTML, se
loguea y el análisis simplemente no muestra el nivel "ajustado".
"""

import logging
import sqlite3
from datetime import date

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

URL_UF_SII = "https://www.sii.cl/valores_y_fechas/uf/uf{anio}.htm"
_HEADERS = {"User-Agent": "Mozilla/5.0 (books-scraper; uso personal)"}
_MESES = ["Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]


# type hint: conexion: sqlite3.Connection, retorno -> None
def crear_tabla_uf(conexion: sqlite3.Connection) -> None:
    conexion.execute("CREATE TABLE IF NOT EXISTS uf (fecha TEXT PRIMARY KEY, valor REAL NOT NULL)")


# type hint: html: str, anio: int, retorno -> dict[str, float]
# Mejora: función pura → se testea con un HTML fijo, sin red.
def parsear_tabla_uf_sii(html: str, anio: int) -> dict[str, float]:
    """Tabla #table_export del SII: fila por día (1–31), columna por mes,
    valores con formato chileno ("30.996,73"). Celdas vacías = días que no
    existen o aún no publicados."""
    soup = BeautifulSoup(html, "html.parser")
    tabla = soup.find("table", id="table_export")
    if tabla is None:
        raise ValueError("No se encontró la tabla de UF (table_export) en la página del SII")

    filas = tabla.find_all("tr")
    encabezado = [c.get_text(strip=True) for c in filas[0].find_all(["th", "td"])]
    columnas_mes = {i: _MESES.index(nombre) + 1 for i, nombre in enumerate(encabezado) if nombre in _MESES}
    if len(columnas_mes) != 12:
        raise ValueError(f"Encabezado de UF inesperado: {encabezado}")

    valores: dict[str, float] = {}
    for fila in filas[1:]:
        celdas = [c.get_text(strip=True) for c in fila.find_all(["th", "td"])]
        if not celdas or not celdas[0].isdigit():
            continue
        dia = int(celdas[0])
        for i, mes in columnas_mes.items():
            texto = celdas[i] if i < len(celdas) else ""
            if not texto:
                continue
            try:
                fecha = date(anio, mes, dia)
            except ValueError:  # 30 de febrero, etc.
                continue
            valores[fecha.isoformat()] = float(texto.replace(".", "").replace(",", "."))
    return valores


# type hint: anio: int, retorno -> dict[str, float]
def descargar_uf_anio(anio: int) -> dict[str, float]:
    r = requests.get(URL_UF_SII.format(anio=anio), headers=_HEADERS, timeout=20)
    r.raise_for_status()
    return parsear_tabla_uf_sii(r.text, anio)


# type hint: conexion: sqlite3.Connection, hoy: date | None, retorno -> int
# Mejora: int = filas nuevas, para el log de run.py.
def actualizar_uf(conexion: sqlite3.Connection, hoy: date | None = None) -> int:
    """Descarga los años que faltan, desde el año del primer precio hasta
    hoy. Un año pasado ya completo (≥365 días) no se vuelve a pedir; el año
    en curso se pide solo si falta el valor de hoy. Nunca lanza."""
    hoy = hoy or date.today()
    crear_tabla_uf(conexion)
    try:
        primera = conexion.execute("SELECT MIN(fecha) FROM precios").fetchone()[0]
    except sqlite3.Error:
        primera = None
    anio_inicio = int(primera[:4]) if primera else hoy.year

    nuevas = 0
    for anio in range(anio_inicio, hoy.year + 1):
        if anio < hoy.year:
            ya = conexion.execute(
                "SELECT COUNT(*) FROM uf WHERE fecha LIKE ?", (f"{anio}-%",)
            ).fetchone()[0]
            if ya >= 365:
                continue
        elif conexion.execute("SELECT 1 FROM uf WHERE fecha = ?", (hoy.isoformat(),)).fetchone():
            continue
        try:
            valores = descargar_uf_anio(anio)
        except (requests.RequestException, ValueError) as e:
            logger.warning("No se pudo obtener la UF %s desde el SII: %s", anio, e)
            continue
        antes = conexion.total_changes
        conexion.executemany(
            "INSERT OR IGNORE INTO uf (fecha, valor) VALUES (?, ?)", sorted(valores.items())
        )
        conexion.commit()
        nuevas += conexion.total_changes - antes
    return nuevas


# type hint: conexion: sqlite3.Connection, retorno -> dict[str, float]
def cargar_uf(conexion: sqlite3.Connection) -> dict[str, float]:
    """{'2026-10-12': 39876.54, ...}; vacío si la tabla no existe aún."""
    try:
        return dict(conexion.execute("SELECT fecha, valor FROM uf").fetchall())
    except sqlite3.OperationalError:
        return {}
