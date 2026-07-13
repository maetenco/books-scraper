import os
import sys
import sqlite3
import pytest
from flask import Flask

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
# Fixtures compartidos (BD en memoria, app Flask)

# '''
#  Fixture app → instancia Flask configurada para testing
# - Fixture client → app.test_client() listo para requests HTTP
# - Fixture db → SQLite en memoria con tablas creadas y datos de prueba
# '''


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


@pytest.fixture
def app(tmp_path):
    import dashboard
    from scrap import crear_tablas

    original_db_path = dashboard.DB_PATH

    test_db = tmp_path / "test.db"
    dashboard.DB_PATH = str(test_db)

    conn = sqlite3.connect(str(test_db))
    crear_tablas(conn)
    conn.close()

    flask_app = dashboard.app
    flask_app.config["TESTING"] = True
    flask_app.config["SERVER_NAME"] = "localhost"

    yield flask_app

    dashboard.DB_PATH = original_db_path


@pytest.fixture
def client(app):
    return app.test_client()
