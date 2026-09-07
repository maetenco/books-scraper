import asyncio
import argparse
import json
import logging
import sqlite3
import os
from typing import Any
from datetime import datetime
from zoneinfo import ZoneInfo
from bs4 import BeautifulSoup
from playwright.async_api import (
    async_playwright,
    BrowserContext,
    Page,
    Error as PlaywrightError,
    TimeoutError as PlaywrightTimeoutError,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# Conceptos de scrap.py:
#  [1] AUTH_FILE            → Archivo storage_state de Playwright.
#       Persiste cookies tras login manual para ejecución headless.
#  [2] crear_tablas()       → Inicializa esquema SQLite con tablas
#       libros, listas, libros_listas (N:M), precios (histórico).
#  [2b] hacer_backup_db()   → Copia libros.db a backups/ antes de
#       cada escaneo (API de backup de sqlite3), conserva las últimas
#       BACKUPS_A_MANTENER copias.
#  [3] limpiar_precio()     → Sanitiza string "$12.990" → int 12990.
#       Elimina signos, separadores de miles y espacios.
#  [4] extraer_precio_producto() → Scraper individual por producto
#       vía Playwright. Extrae precio desde JSON en attributes HTML
#       o desde selectores CSS alternativos.
#  [5] obtener_ids_listas() → Obtiene todos los data-id de wishlists
#       del dashboard de BuscaLibre.
#  [6] extraer_libros()     → Parsea el HTML de una lista de deseos
#       y retorna lista de dicts con título, autor, precios, URL.
#  [7] guardar_libro()      → INSERT/UPSERT del libro y su precio en
#       la DB. Detecta bajas de precio comparando con último registro.
#  [8] main()               → Función async principal: login, recorre
#       wishlists, scrapea productos, llama a guardar_libro().
# ═══════════════════════════════════════════════════════════════

AUTH_FILE = "auth.json"
DB_PATH = "libros.db"
BACKUPS_DIR = "backups"
BACKUPS_A_MANTENER = 7  # backups diarios más recientes que se conservan

# Cuántas páginas de producto se abren en paralelo durante el lazy
# scraping (extraer_precio_producto abre una página nueva por libro;
# cada una es independiente, pero no conviene abrir decenas a la vez
# contra el mismo sitio).
MAX_CONCURRENCIA_LAZY_SCRAPING = 5


# ── [2] crear_tablas: esquema SQLite ──
# type hint: conexion: sqlite3.Connection, retorno -> None
# Mejora: mypy valida que solo pases conexiones SQLite;
#         el IDE autocompleta .cursor() y .commit().
def crear_tablas(conexion: sqlite3.Connection) -> None:
    conexion.execute("PRAGMA foreign_keys = ON")
    cursor = conexion.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS libros (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            titulo TEXT NOT NULL,
            autor TEXT,
            UNIQUE(titulo, autor)
        )
        """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS listas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lista_id TEXT UNIQUE,
            nombre TEXT
        )
        """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS libros_listas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            libro_id INTEGER,
            lista_id INTEGER,
            UNIQUE(libro_id, lista_id),
            FOREIGN KEY(libro_id) REFERENCES libros(id),
            FOREIGN KEY(lista_id) REFERENCES listas(id)
        )
        """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS precios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            libro_id INTEGER,
            precio_actual INTEGER,
            precio_antes INTEGER,
            descuento TEXT,
            fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(libro_id) REFERENCES libros(id)
        )
        """)

    try:
        cursor.execute("ALTER TABLE libros ADD COLUMN url TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        cursor.execute("ALTER TABLE precios ADD COLUMN descuento TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        cursor.execute("ALTER TABLE precios ADD COLUMN estado TEXT DEFAULT 'disponible'")
    except sqlite3.OperationalError:
        pass
    try:
        cursor.execute("ALTER TABLE libros ADD COLUMN imagen_url TEXT")
    except sqlite3.OperationalError:
        pass

    # Las queries de dashboard.py hacen subqueries correlacionadas por
    # libro_id/fecha sobre esta tabla (potencialmente la más grande,
    # crece indefinidamente por ser append-only); este índice evita un
    # full table scan repetido a medida que el historial crece.
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_precios_libro_fecha ON precios(libro_id, fecha)")

    conexion.commit()


# ── [2b] hacer_backup_db: copia de libros.db antes de escanear ──
# type hint: conexion: sqlite3.Connection, retorno -> str | None
# Mejora: str | None documenta que puede no haber backup (p. ej. si
#         falla por disco lleno); el llamador decide si es fatal o no.
def hacer_backup_db(conexion: sqlite3.Connection) -> str | None:
    """Copia libros.db a backups/libros_<fecha>.db usando la API de
    backup de sqlite3 (segura incluso con la conexión abierta, a
    diferencia de copiar el archivo a mano). Conserva solo los
    BACKUPS_A_MANTENER más recientes para no crecer indefinidamente.
    Nunca lanza: si falla, se loguea como warning y se retorna None,
    para que un backup fallido no tumbe el scraping.
    """
    try:
        os.makedirs(BACKUPS_DIR, exist_ok=True)
        fecha = datetime.now(ZoneInfo("America/Santiago")).strftime("%Y%m%d_%H%M%S")
        destino_path = os.path.join(BACKUPS_DIR, f"libros_{fecha}.db")

        destino = sqlite3.connect(destino_path)
        with destino:
            conexion.backup(destino)
        destino.close()

        backups = sorted(
            f for f in os.listdir(BACKUPS_DIR)
            if f.startswith("libros_") and f.endswith(".db")
        )
        for viejo in backups[:-BACKUPS_A_MANTENER]:
            os.remove(os.path.join(BACKUPS_DIR, viejo))

        logger.info("Backup de %s creado en %s", DB_PATH, destino_path)
        return destino_path
    except (OSError, sqlite3.Error) as e:
        logger.warning("No se pudo crear el backup de %s: %s", DB_PATH, e)
        return None


# ── [3] limpiar_precio: "$12.990" → 12990 ──
# type hint: precioTexto: str | None, retorno -> int | None
# Mejora: mypy detecta si le pasas un entero (error común);
#         el retorno opcional fuerza a quien llama a manejar None.
def limpiar_precio(precioTexto: str | None) -> int | None:
    if precioTexto is None:
        return None
    try:
        precio_limpio = precioTexto.replace("$", "")
        precio_limpio = precio_limpio.replace(".", "")
        precio_limpio = precio_limpio.strip()
        return int(precio_limpio)
    except ValueError:
        return None


# ── [4] extraer_precio_producto: scraping individual por producto ──
# type hint: url: str, browser_context: BrowserContext, timeout_sec: int = 60
#           retorno -> dict | None
# Mejora: el parámetro BrowserContext es el tipo exacto de Playwright;
#         al tipar timeout_sec como int mypy evita pasar strings.
async def extraer_precio_producto(url: str, browser_context: BrowserContext, timeout_sec: int = 60) -> dict | None:
    try:
        page_ctx = await browser_context.new_page()
        try:
            await page_ctx.goto(url, wait_until="domcontentloaded", timeout=timeout_sec * 1000)
        except PlaywrightTimeoutError:
            logger.warning("Timeout navegando a %s, reintentando una vez más...", url)
            await page_ctx.goto(url, wait_until="domcontentloaded", timeout=timeout_sec * 1000)

        try:
            await page_ctx.wait_for_selector("div.opcionPrecio", timeout=8000)
        except PlaywrightTimeoutError:
            # Selector opcional: no todas las páginas de producto lo tienen.
            pass

        html = await page_ctx.content()
        await page_ctx.close()

        if "Sin Stock" in html or "sin stock" in html:
            return {"sin_stock": True}

        soup = BeautifulSoup(html, "html.parser")

        opcion = soup.find("div", class_="opcionPrecio", attrs={"data-form": "1"})
        if opcion:
            raw = opcion.get("data-despacho-payload")
            if raw:
                payload = json.loads(raw)
                p_actual_raw = payload.get("precio_moneda_raw")
                p_antes_raw = payload.get("precio_tachado_moneda_raw")
                pct_dcto = payload.get("porcentaje_descuento")

                if p_actual_raw:
                    precio_actual = int(float(p_actual_raw))
                    precio_antes = int(float(p_antes_raw)) if p_antes_raw else None
                    descuento = f"{int(pct_dcto)}%" if pct_dcto is not None else "Sin descuento"
                    return {"precio_actual": precio_actual, "precio_antes": precio_antes, "descuento": descuento}

        col_precio = soup.find("div", class_="colPrecio")
        if col_precio:
            ped = col_precio.find("span", class_="ped")
            pvp = col_precio.find("span", class_="pvp")
            if ped:
                precio_actual = limpiar_precio(ped.get_text(strip=True))
                precio_antes = limpiar_precio(pvp.get_text(strip=True)) if pvp else None
                col_dcto = soup.find("div", class_="colDescuento")
                descuento = col_dcto.get_text(strip=True) if col_dcto else "Sin descuento"
                return {"precio_actual": precio_actual, "precio_antes": precio_antes, "descuento": descuento}

        col_precios = soup.find("div", class_="col-precios")
        if col_precios:
            p_ahora = col_precios.find("p", class_="precioAhora")
            p_antes = col_precios.find("p", class_="precioAntes")
            if p_ahora:
                precio_actual = limpiar_precio(p_ahora.get_text(strip=True))
                precio_antes = limpiar_precio(p_antes.get_text(strip=True)) if p_antes else None
                descuento = "Sin descuento"
                return {"precio_actual": precio_actual, "precio_antes": precio_antes, "descuento": descuento}

    except (PlaywrightError, json.JSONDecodeError, AttributeError, KeyError, ValueError) as e:
        logger.error("Error scraping %s: %s", url, e)
    return None


# ── [5] obtener_ids_listas: IDs de wishlists del dashboard ──
# type hint: page: Page, retorno -> list[str]
# Mejora: Page es el tipo exacto de Playwright;
#         list[str] indica que retorna strings, no números.
async def obtener_ids_listas(page: Page) -> list[str]:
    contenedor = await page.wait_for_selector("ul.ul-wishlist")
    listas = await contenedor.query_selector_all('li[data-view="listaDeseosProductos"]')
    ids_listas = []
    for li in listas:
        data_id = await li.get_attribute("data-id")
        if data_id is not None:
            ids_listas.append(data_id)
    print(f"Tienes {len(ids_listas)} listas")
    return ids_listas


# ── [6] extraer_libros: parsea HTML de wishlist → dicts ──
# type hint: soup: BeautifulSoup, base_url: str, retorno -> list[dict[str, Any]]
# Mejora: BeautifulSoup autocompleta .find() y .find_all();
#         list[dict[str, Any]] documenta la estructura de datos.
def extraer_libros(soup: BeautifulSoup, base_url: str = "https://www.buscalibre.cl") -> list[dict[str, Any]]:
    libros = soup.find_all("div", class_="info-div")
    datos_lista = []
    for libro in libros:
        titulo_elemento = libro.find("div", class_="title")
        if titulo_elemento:
            titulo = titulo_elemento.get_text(strip=True).lower()
        else:
            titulo = "N/A"

        autor_elemento = libro.find("div", class_="autor")
        if autor_elemento:
            autor = autor_elemento.get_text(strip=True).lower()
        else:
            autor = "N/A"

        contenedor_precio = libro.find_parent("div", class_="productoLista")

        link_tag = libro.find_parent("a") or (contenedor_precio.find("a", href=True) if contenedor_precio else None)
        url = ""
        if link_tag and link_tag.get("href"):
            href = link_tag["href"]
            url = href if href.startswith("http") else base_url + href

        img_tag = contenedor_precio.find("img") if contenedor_precio else None
        imagen_url = ""
        if img_tag and img_tag.get("src"):
            src = img_tag["src"]
            imagen_url = src if src.startswith("http") else base_url + src

        contenedor_dcto = contenedor_precio.find("div", class_="etiquetawish")
        if contenedor_dcto:
            dcto_elemento = contenedor_dcto.find("p", class_="numero")
            if dcto_elemento:
                dcto = dcto_elemento.get_text(strip=True)
            else:
                dcto = "Sin descuento"
        else:
            dcto = "Sin descuento"

        precio_ant_elemento = contenedor_precio.find("p", class_="precioAntes")
        if precio_ant_elemento:
            precio_ant_texto = precio_ant_elemento.get_text(strip=True)
            precio_ant = limpiar_precio(precio_ant_texto)
        else:
            precio_ant = None

        precio_act_elemento = contenedor_precio.find("p", class_="precioAhora")
        if precio_act_elemento:
            precio_act_texto = precio_act_elemento.get_text(strip=True)
            precio_act = limpiar_precio(precio_act_texto)
        else:
            precio_act = None

        datos_lista.append({
            "titulo": titulo,
            "autor": autor,
            "precio_antes": precio_ant,
            "precio_actual": precio_act,
            "descuento": dcto,
            "url": url,
            "imagen_url": imagen_url,
        })
    return datos_lista


# ── [7] guardar_libro: INSERT + detección de bajas ──
# type hint: conexion: sqlite3.Connection, datos_libro: dict,
#           id_lista_db: int, retorno -> dict | None
# Mejora: tipar id_lista_db como int evita pasar strings por error;
#         dict | None documenta que puede fallar (libro duplicado).
def guardar_libro(conexion: sqlite3.Connection, datos_libro: dict, id_lista_db: int) -> dict | None:
    cursor = conexion.cursor()
    titulo = datos_libro["titulo"]
    autor = datos_libro["autor"]
    precio_actual = datos_libro["precio_actual"]
    precio_antes = datos_libro["precio_antes"]
    descuento = datos_libro["descuento"]
    estado = datos_libro.get("estado", "disponible")
    url = datos_libro.get("url", "")
    imagen_url = datos_libro.get("imagen_url", "")

    cursor.execute(
        "INSERT OR IGNORE INTO libros (titulo, autor) VALUES (?, ?)",
        (titulo, autor)
    )

    if url:
        cursor.execute(
            "UPDATE libros SET url = ? WHERE titulo = ? AND autor = ? AND (url IS NULL OR url = '')",
            (url, titulo, autor)
        )
    if imagen_url:
        cursor.execute(
            "UPDATE libros SET imagen_url = ? WHERE titulo = ? AND autor = ? AND (imagen_url IS NULL OR imagen_url = '')",
            (imagen_url, titulo, autor)
        )

    cursor.execute(
        "SELECT id FROM libros WHERE titulo = ? AND autor = ?",
        (titulo, autor)
    )
    resultado = cursor.fetchone()
    if resultado is None:
        print(f"Error obteniendo ID de: {titulo}")
        return None

    id_libro = resultado[0]

    cursor.execute(
        "INSERT OR IGNORE INTO libros_listas (libro_id, lista_id) VALUES (?, ?)",
        (id_libro, id_lista_db)
    )

    # Se consulta el último precio ANTES de insertar el nuevo registro:
    # si se consultara después, la fila recién insertada podría ganar
    # el desempate por fecha (mismo segundo) y la comparación terminaría
    # comparando el precio nuevo contra sí mismo, sin detectar bajas.
    ultimo_precio = None
    if precio_actual is not None:
        cursor.execute(
            "SELECT precio_actual FROM precios WHERE libro_id = ? AND estado = 'disponible' ORDER BY fecha DESC, id DESC LIMIT 1",
            (id_libro,)
        )
        ultimo_precio = cursor.fetchone()

    fecha_chile = datetime.now(ZoneInfo("America/Santiago")).strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute(
        "INSERT INTO precios (libro_id, precio_actual, precio_antes, descuento, estado, fecha) VALUES (?, ?, ?, ?, ?, ?)",
        (id_libro, precio_actual, precio_antes, descuento, estado, fecha_chile)
    )

    if precio_actual is not None:
        print(f"Precio registrado: {titulo}")

        precio_cambio = None
        if ultimo_precio is not None and ultimo_precio[0] is not None:
            if precio_actual < ultimo_precio[0]:
                precio_cambio = {
                    "titulo": titulo,
                    "autor": autor,
                    "precio_viejo": ultimo_precio[0],
                    "precio_nuevo": precio_actual,
                    "descuento": descuento,
                    "libro_id": id_libro,
                    "url": url,
                }
    else:
        if estado == "sin_stock":
            print(f"Sin stock: {titulo}")
        else:
            print(f"Precio no disponible: {titulo}")
        precio_cambio = None

    conexion.commit()
    return precio_cambio


# ── [7b] _corregir_precio_libro: lazy scraping de un libro ──
# type hint: libro: dict, idx/total: int, browser_context: BrowserContext,
#           semaforo: asyncio.Semaphore, retorno -> bool
# Mejora: bool indica si el libro terminó con datos corregidos, para
#         poder contarlos con sum() tras el gather().
async def _corregir_precio_libro(
    libro: dict, idx: int, total: int, browser_context: BrowserContext, semaforo: asyncio.Semaphore
) -> bool:
    """Busca el precio real de un libro sin precio visible en la lista.

    Muta `libro` in-place (mismo dict que vive en datos_lista). Cada
    llamada a extraer_precio_producto() abre su propia página, así que
    varios libros pueden procesarse en paralelo con seguridad; el
    semáforo limita cuántas páginas se abren a la vez.
    """
    url = libro.get("url", "")
    if not url:
        print(f"  [{idx}/{total}] {libro['titulo'][:40]}... sin URL")
        return False

    async with semaforo:
        precio_real = await extraer_precio_producto(url, browser_context)

    if not precio_real:
        print(f"  [{idx}/{total}] {libro['titulo'][:40]}... sin cambios")
        return False

    if precio_real.get("sin_stock"):
        print(f"  [{idx}/{total}] {libro['titulo'][:40]}... sin stock")
        libro["precio_actual"] = None
        libro["precio_antes"] = None
        libro["descuento"] = "Sin stock"
        libro["estado"] = "sin_stock"
        return True

    if precio_real["precio_actual"] is not None:
        print(f"  [{idx}/{total}] {libro['titulo'][:40]}... corregido: ${libro['precio_actual']} → ${precio_real['precio_actual']}")
        libro["precio_actual"] = precio_real["precio_actual"]
        libro["precio_antes"] = precio_real["precio_antes"]
        libro["descuento"] = precio_real["descuento"]
        libro["estado"] = "disponible"
        return True

    print(f"  [{idx}/{total}] {libro['titulo'][:40]}... sin cambios")
    return False


# ── [8] main: orquestador del scraping completo ──
# type hint: headless: bool = False, retorno -> list[dict]
# Mejora: bool restringe el parámetro a True/False únicamente;
#         list[dict] documenta que retorna los cambios detectados.
async def main(headless: bool = False) -> list[dict]:
    conexion = sqlite3.connect(DB_PATH)
    crear_tablas(conexion)
    hacer_backup_db(conexion)
    cursor = conexion.cursor()

    url = "https://www.buscalibre.cl"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)

        if headless and os.path.exists(AUTH_FILE):
            context = await browser.new_context(storage_state=AUTH_FILE)
            page = await context.new_page()
            print("Usando sesión guardada (headless)")
        elif not headless:
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto(url)
            print("Haz login manual en el navegador.")
            print("Cuando termines, vuelve aquí y presiona ENTER...")
            input()
            await context.storage_state(path=AUTH_FILE)
            print(f"Sesión guardada en {AUTH_FILE}")
        else:
            print("No hay sesión guardada. Ejecuta primero sin --headless para hacer login.")
            conexion.close()
            return []

        await page.goto("https://www.buscalibre.cl/v2/u/dashboard#lista-deseos")
        await page.wait_for_selector("div.misListas")

        ids_listas = await obtener_ids_listas(page)
        cambios = []

        for lista_id in ids_listas:
            print(f"\nProcesando lista ID: {lista_id}")

            selector = f'li[data-view="listaDeseosProductos"][data-id="{lista_id}"]'
            lista_actual = await page.query_selector(selector)

            if lista_actual is None:
                print("No se encontró la lista")
                continue

            try:
                nombre_antes = await page.locator("span.nombre >> nth=0").inner_text()
            except PlaywrightTimeoutError:
                nombre_antes = ""

            nombre_lista = await lista_actual.query_selector("span.nombre")
            nombre_lista = await nombre_lista.inner_text()
            print(f"Lista: {nombre_lista}")

            cursor.execute(
                "INSERT OR IGNORE INTO listas (lista_id, nombre) VALUES (?, ?)",
                (lista_id, nombre_lista)
            )
            conexion.commit()

            cursor.execute(
                "SELECT id FROM listas WHERE lista_id = ?",
                (lista_id,)
            )
            resultado = cursor.fetchone()
            if resultado is None:
                print("Error obteniendo ID lista")
                continue

            id_lista_db = resultado[0]

            link = await lista_actual.query_selector("a")
            if link is None:
                print("No se encontró link")
                continue

            await page.wait_for_timeout(1500)
            await link.click()

            try:
                await page.wait_for_function(
                    """(nombre) => {
                        const el = document.querySelector("span.nombre");
                        return el && el.innerText !== nombre;
                    }""",
                    arg=nombre_antes
                )
            except PlaywrightTimeoutError:
                await page.wait_for_timeout(2000)

            await page.wait_for_timeout(1000)
            print("Lista cargada correctamente")

            html = await page.content()
            soup = BeautifulSoup(html, "html.parser")
            datos_lista = extraer_libros(soup)
            print(f"Libros encontrados: {len(datos_lista)}")
            print("Obteniendo precios reales desde páginas de producto...")

            semaforo = asyncio.Semaphore(MAX_CONCURRENCIA_LAZY_SCRAPING)
            tareas_lazy = []
            for idx, libro in enumerate(datos_lista, 1):
                if libro["precio_actual"] is not None and libro["precio_actual"] > 0:
                    print(f"  [{idx}/{len(datos_lista)}] {libro['titulo'][:40]}... ok (desde lista)")
                    continue
                tareas_lazy.append(_corregir_precio_libro(libro, idx, len(datos_lista), context, semaforo))

            resultados_lazy = await asyncio.gather(*tareas_lazy)
            corregidos = sum(resultados_lazy)

            print(f"Precios corregidos: {corregidos} de {len(datos_lista)}")

            for libro in datos_lista:
                cambio = guardar_libro(conexion, libro, id_lista_db)
                if cambio:
                    cambios.append(cambio)

            print("Scraping terminado")

        await browser.close()
        conexion.close()

    return cambios


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Books scraper for BuscaLibre")
    parser.add_argument("--headless", action="store_true", help="Run in headless mode")
    args = parser.parse_args()
    asyncio.run(main(headless=args.headless))
