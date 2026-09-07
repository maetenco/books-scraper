"""
Tests de hacer_backup_db() (Fase 5, punto 19): crea una copia de la DB
antes de escanear y conserva solo las últimas BACKUPS_A_MANTENER.
"""
import os
import sys
import sqlite3

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import scrap
from scrap import crear_tablas, hacer_backup_db


def test_hacer_backup_crea_archivo_con_datos_copiados(tmp_path, monkeypatch):
    monkeypatch.setattr(scrap, "BACKUPS_DIR", str(tmp_path / "backups"))
    monkeypatch.setattr(scrap, "DB_PATH", str(tmp_path / "libros.db"))

    conn = sqlite3.connect(":memory:")
    crear_tablas(conn)
    conn.execute("INSERT INTO listas (lista_id, nombre) VALUES ('L1', 'Mi lista')")
    conn.commit()

    ruta_backup = hacer_backup_db(conn)

    assert ruta_backup is not None
    assert os.path.exists(ruta_backup)

    backup_conn = sqlite3.connect(ruta_backup)
    cursor = backup_conn.cursor()
    cursor.execute("SELECT nombre FROM listas WHERE lista_id = 'L1'")
    assert cursor.fetchone()[0] == "Mi lista"
    backup_conn.close()
    conn.close()


def test_hacer_backup_conserva_solo_los_mas_recientes(tmp_path, monkeypatch):
    monkeypatch.setattr(scrap, "BACKUPS_DIR", str(tmp_path / "backups"))
    monkeypatch.setattr(scrap, "BACKUPS_A_MANTENER", 2)

    conn = sqlite3.connect(":memory:")
    crear_tablas(conn)

    # Simula 4 backups previos con timestamps distintos (nombres que
    # ordenan cronológicamente), más el que se crea ahora.
    os.makedirs(str(tmp_path / "backups"), exist_ok=True)
    for fecha_falsa in ["20260101_000000", "20260102_000000", "20260103_000000"]:
        with open(tmp_path / "backups" / f"libros_{fecha_falsa}.db", "w") as f:
            f.write("")

    hacer_backup_db(conn)
    conn.close()

    restantes = sorted(os.listdir(str(tmp_path / "backups")))
    assert len(restantes) == 2  # BACKUPS_A_MANTENER=2, incluyendo el recién creado
    # El más reciente (recién creado) debe seguir presente.
    assert any(f > "libros_20260103_000000.db" for f in restantes)


def test_hacer_backup_no_lanza_si_falla(tmp_path, monkeypatch):
    # BACKUPS_DIR apunta a una ruta que no se puede crear (un archivo,
    # no un directorio) para forzar el error y confirmar que se maneja.
    archivo_bloqueante = tmp_path / "backups_bloqueado"
    archivo_bloqueante.write_text("no soy un directorio")
    monkeypatch.setattr(scrap, "BACKUPS_DIR", str(archivo_bloqueante / "sub"))

    conn = sqlite3.connect(":memory:")
    crear_tablas(conn)
    resultado = hacer_backup_db(conn)
    conn.close()

    assert resultado is None
