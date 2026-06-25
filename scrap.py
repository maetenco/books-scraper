import asyncio
import argparse
import json
import sqlite3
import os
from typing import Any
from datetime import datetime
from zoneinfo import ZoneInfo
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, BrowserContext, Page

# ═══════════════════════════════════════════════════════════════
# Conceptos de scrap.py:
#  [1] AUTH_FILE            → Archivo storage_state de Playwright.
#       Persiste cookies tras login manual para ejecución headless.
#  [2] crear_tablas()       → Inicializa esquema SQLite con tablas
#       libros, listas, libros_listas (N:M), precios (histórico).
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


# ── [2] crear_tablas: esquema SQLite ──
# type hint: conexion: sqlite3.Connection, retorno -> None
# Mejora: mypy valida que solo pases conexiones SQLite;
#         el IDE autocompleta .cursor() y .commit().
def crear_tablas(conexion: sqlite3.Connection) -> None:
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
            UNIQUE(libro_id, lista_id)
        )
        """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS precios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            libro_id INTEGER,
            precio_actual INTEGER,
            precio_antes INTEGER,
            descuento TEXT,
            fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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

    conexion.commit()


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
        except Exception:
            print("  timeout, reintentando una vez más...")
            await page_ctx.goto(url, wait_until="domcontentloaded", timeout=timeout_sec * 1000)

        try:
            await page_ctx.wait_for_selector("div.opcionPrecio", timeout=8000)
        except:
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

    except Exception as e:
        print(f"  Error scraping {url}: {e}")
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

    fecha_chile = datetime.now(ZoneInfo("America/Santiago")).strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute(
        "INSERT INTO precios (libro_id, precio_actual, precio_antes, descuento, estado, fecha) VALUES (?, ?, ?, ?, ?, ?)",
        (id_libro, precio_actual, precio_antes, descuento, estado, fecha_chile)
    )

    if precio_actual is not None:
        cursor.execute(
            "SELECT precio_actual FROM precios WHERE libro_id = ? AND estado = 'disponible' ORDER BY fecha DESC LIMIT 1",
            (id_libro,)
        )
        ultimo_precio = cursor.fetchone()

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
                }
    else:
        if estado == "sin_stock":
            print(f"Sin stock: {titulo}")
        else:
            print(f"Precio no disponible: {titulo}")
        precio_cambio = None

    conexion.commit()
    return precio_cambio


# ── [8] main: orquestador del scraping completo ──
# type hint: headless: bool = False, retorno -> list[dict]
# Mejora: bool restringe el parámetro a True/False únicamente;
#         list[dict] documenta que retorna los cambios detectados.
async def main(headless: bool = False) -> list[dict]:
    conexion = sqlite3.connect("libros.db")
    crear_tablas(conexion)
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
            except:
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
            except:
                await page.wait_for_timeout(2000)

            await page.wait_for_timeout(1000)
            print("Lista cargada correctamente")

            html = await page.content()
            soup = BeautifulSoup(html, "html.parser")
            datos_lista = extraer_libros(soup)
            print(f"Libros encontrados: {len(datos_lista)}")
            print("Obteniendo precios reales desde páginas de producto...")

            corregidos = 0
            for idx, libro in enumerate(datos_lista, 1):
                if libro["precio_actual"] is not None and libro["precio_actual"] > 0:
                    print(f"  [{idx}/{len(datos_lista)}] {libro['titulo'][:40]}... ok (desde lista)")
                    continue
                url = libro.get("url", "")
                if url:
                    print(f"  [{idx}/{len(datos_lista)}] {libro['titulo'][:40]}...", end=" ")
                    precio_real = await extraer_precio_producto(url, context)
                    if precio_real:
                        if precio_real.get("sin_stock"):
                            print("sin stock")
                            libro["precio_actual"] = None
                            libro["precio_antes"] = None
                            libro["descuento"] = "Sin stock"
                            libro["estado"] = "sin_stock"
                            corregidos += 1
                        elif precio_real["precio_actual"] is not None:
                            print(f"corregido: ${libro['precio_actual']} → ${precio_real['precio_actual']}")
                            libro["precio_actual"] = precio_real["precio_actual"]
                            libro["precio_antes"] = precio_real["precio_antes"]
                            libro["descuento"] = precio_real["descuento"]
                            libro["estado"] = "disponible"
                            corregidos += 1
                        else:
                            print("sin cambios")
                    else:
                        print("sin cambios")
                else:
                    print(f"  [{idx}/{len(datos_lista)}] {libro['titulo'][:40]}... sin URL")

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
